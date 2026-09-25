"""Tests for the read-only lineage upstream source-path query."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SOURCE_PATHS = "/lineage/impact/source-paths"


def make_dataset(client: TestClient, name: str, fields: list[str]) -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions",
        json={
            "fields": [
                {"name": field, "type": "string", "nullable": True}
                for field in fields
            ]
        },
    )
    assert response.status_code == 201, response.text


def add_link(
    client: TestClient,
    source: tuple[str, int, str],
    target: tuple[str, int, str],
) -> None:
    source_dataset, source_version, source_field = source
    target_dataset, target_version, target_field = target
    response = client.post(
        f"/datasets/{target_dataset}/versions/{target_version}/lineage",
        json={
            "target_dataset": target_dataset,
            "target_version": target_version,
            "target_field": target_field,
            "source_dataset": source_dataset,
            "source_version": source_version,
            "source_field": source_field,
        },
    )
    assert response.status_code == 201, response.text


def ref(dataset: str, version: int, field: str) -> dict:
    return {"dataset": dataset, "version": version, "field": field}


def origins(
    client: TestClient, dataset: str, version: int, field: str
) -> dict:
    response = client.get(
        f"/datasets/{dataset}/versions/{version}{SOURCE_PATHS}",
        params={"field": field},
    )
    assert response.status_code == 200, response.text
    return response.json()


def setup_branching_graph(client: TestClient) -> None:
    # raw_a.a1 -> mid_b.b1 -> mart_c.c1
    #          -> mid_b.b2 -> mart_c.c1   (branch + diamond + multi-hop)
    make_dataset(client, "raw_a", ["a1", "a2"])
    make_dataset(client, "mid_b", ["b1", "b2"])
    make_dataset(client, "mart_c", ["c1"])
    add_link(client, ("raw_a", 1, "a1"), ("mid_b", 1, "b1"))
    add_link(client, ("raw_a", 1, "a1"), ("mid_b", 1, "b2"))
    add_link(client, ("mid_b", 1, "b1"), ("mart_c", 1, "c1"))
    add_link(client, ("mid_b", 1, "b2"), ("mart_c", 1, "c1"))


def cache_rows(db_path: Path) -> list[tuple[str, int, str]]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT source_dataset, source_version, source_field "
            "FROM lineage_impact_cache ORDER BY 1, 2, 3"
        ).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Path semantics
# --------------------------------------------------------------------------- #


def test_direct_upstream_has_path_length_one(client: TestClient) -> None:
    setup_branching_graph(client)
    body = origins(client, "mid_b", 1, "b1")
    assert body["source"] == ref("mid_b", 1, "b1")
    assert body["origins"] == [
        {
            "dataset": "raw_a",
            "version": 1,
            "field": "a1",
            "path": [ref("mid_b", 1, "b1"), ref("raw_a", 1, "a1")],
            "path_length": 1,
        }
    ]
    assert body["direct_count"] == 1
    assert body["indirect_count"] == 0
    assert body["source_dataset_count"] == 1


def test_multi_hop_paths_and_diamond_tie_break(client: TestClient) -> None:
    setup_branching_graph(client)
    body = origins(client, "mart_c", 1, "c1")

    # Origins are sorted by location key (dataset, version, field): mid_b
    # before raw_a; b1 before b2.
    assert [
        (item["dataset"], item["version"], item["field"])
        for item in body["origins"]
    ] == [
        ("mid_b", 1, "b1"),
        ("mid_b", 1, "b2"),
        ("raw_a", 1, "a1"),
    ]

    assert body["origins"][0]["path"] == [
        ref("mart_c", 1, "c1"),
        ref("mid_b", 1, "b1"),
    ]
    assert body["origins"][1]["path"] == [
        ref("mart_c", 1, "c1"),
        ref("mid_b", 1, "b2"),
    ]
    a1 = body["origins"][2]
    # a1 has two length-2 paths (via b1 and via b2); the lexicographically
    # smallest node sequence goes via b1.
    assert a1["path_length"] == 2
    assert a1["path"] == [
        ref("mart_c", 1, "c1"),
        ref("mid_b", 1, "b1"),
        ref("raw_a", 1, "a1"),
    ]

    assert body["direct_count"] == 2
    assert body["indirect_count"] == 1
    assert body["source_dataset_count"] == 2


def test_shortest_path_beats_longer_route(client: TestClient) -> None:
    # s.f -> a.f -> b.f -> d.f and a separate direct s.f -> d.f edge; the
    # direct edge wins regardless of insertion order.
    make_dataset(client, "s", ["f"])
    make_dataset(client, "a", ["f"])
    make_dataset(client, "b", ["f"])
    make_dataset(client, "d", ["f"])
    add_link(client, ("s", 1, "f"), ("a", 1, "f"))
    add_link(client, ("a", 1, "f"), ("b", 1, "f"))
    add_link(client, ("b", 1, "f"), ("d", 1, "f"))
    add_link(client, ("s", 1, "f"), ("d", 1, "f"))

    items = origins(client, "d", 1, "f")["origins"]
    s_origin = next(item for item in items if item["dataset"] == "s")
    assert s_origin["path_length"] == 1
    assert s_origin["path"] == [ref("d", 1, "f"), ref("s", 1, "f")]


def test_equal_length_tie_break_by_first_diverging_node(
    client: TestClient,
) -> None:
    # s.f -> a.w -> d.z and s.f -> a.x -> d.z: equal length, the route via
    # field "w" is lexicographically smaller than via "x".
    make_dataset(client, "s", ["f"])
    make_dataset(client, "a", ["w", "x"])
    make_dataset(client, "d", ["z"])
    # Insert the "losing" edge first so the choice cannot depend on order.
    add_link(client, ("s", 1, "f"), ("a", 1, "x"))
    add_link(client, ("s", 1, "f"), ("a", 1, "w"))
    add_link(client, ("a", 1, "x"), ("d", 1, "z"))
    add_link(client, ("a", 1, "w"), ("d", 1, "z"))

    items = origins(client, "d", 1, "z")["origins"]
    s_origin = next(item for item in items if item["dataset"] == "s")
    assert s_origin["path"] == [
        ref("d", 1, "z"),
        ref("a", 1, "w"),
        ref("s", 1, "f"),
    ]
    assert s_origin["path_length"] == 2


def test_without_upstream_origins_is_empty(client: TestClient) -> None:
    setup_branching_graph(client)
    body = origins(client, "raw_a", 1, "a1")
    assert body == {
        "source": ref("raw_a", 1, "a1"),
        "origins": [],
        "direct_count": 0,
        "indirect_count": 0,
        "source_dataset_count": 0,
    }
    assert origins(client, "raw_a", 1, "a2")["origins"] == []


def test_cycle_terminates_with_shortest_paths(client: TestClient) -> None:
    make_dataset(client, "cyc_x", ["f"])
    make_dataset(client, "cyc_y", ["f"])
    make_dataset(client, "cyc_z", ["f"])
    add_link(client, ("cyc_x", 1, "f"), ("cyc_y", 1, "f"))
    add_link(client, ("cyc_y", 1, "f"), ("cyc_z", 1, "f"))
    add_link(client, ("cyc_z", 1, "f"), ("cyc_x", 1, "f"))

    body = origins(client, "cyc_x", 1, "f")
    assert body["source"] == ref("cyc_x", 1, "f")
    assert [
        (item["dataset"], item["field"]) for item in body["origins"]
    ] == [("cyc_y", "f"), ("cyc_z", "f")]
    y_item = body["origins"][0]
    z_item = body["origins"][1]
    assert y_item["path"] == [
        ref("cyc_x", 1, "f"),
        ref("cyc_z", 1, "f"),
        ref("cyc_y", 1, "f"),
    ]
    assert y_item["path_length"] == 2
    assert z_item["path"] == [ref("cyc_x", 1, "f"), ref("cyc_z", 1, "f")]
    assert z_item["path_length"] == 1
    assert body["direct_count"] == 1
    assert body["indirect_count"] == 1
    assert body["source_dataset_count"] == 2


def test_origins_sorted_by_dataset_version_field(client: TestClient) -> None:
    make_dataset(client, "t", ["f"])
    make_dataset(client, "z_src", ["s"])
    make_dataset(client, "b_ds", ["f"])
    make_dataset(client, "a_ds", ["f2", "f1"])
    response = client.post(
        "/datasets/a_ds/versions",
        json={
            "fields": [
                {"name": "f1", "type": "string", "nullable": True}
            ]
        },
    )
    assert response.status_code == 201
    add_link(client, ("z_src", 1, "s"), ("t", 1, "f"))
    add_link(client, ("b_ds", 1, "f"), ("t", 1, "f"))
    add_link(client, ("a_ds", 1, "f2"), ("t", 1, "f"))
    add_link(client, ("a_ds", 1, "f1"), ("t", 1, "f"))
    add_link(client, ("a_ds", 2, "f1"), ("t", 1, "f"))

    body = origins(client, "t", 1, "f")
    assert [
        (item["dataset"], item["version"], item["field"])
        for item in body["origins"]
    ] == [
        ("a_ds", 1, "f1"),
        ("a_ds", 1, "f2"),
        ("a_ds", 2, "f1"),
        ("b_ds", 1, "f"),
        ("z_src", 1, "s"),
    ]
    assert body["direct_count"] == 5
    assert body["indirect_count"] == 0
    assert body["source_dataset_count"] == 3


def test_field_parameter_is_matched_literally(client: TestClient) -> None:
    # Unlike the downstream queries the value is not trimmed: a padded name
    # matches no stored field and is a 404, not a 200.
    setup_branching_graph(client)
    response = client.get(
        f"/datasets/mid_b/versions/1{SOURCE_PATHS}",
        params={"field": "  b1  "},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


# --------------------------------------------------------------------------- #
# Deterministic serialization
# --------------------------------------------------------------------------- #


def test_document_is_compact_with_fixed_key_order_and_newline(
    client: TestClient,
) -> None:
    setup_branching_graph(client)
    response = client.get(
        f"/datasets/mart_c/versions/1{SOURCE_PATHS}", params={"field": "c1"}
    )
    expected = (
        b'{"source":{"dataset":"mart_c","version":1,"field":"c1"},'
        b'"origins":['
        b'{"dataset":"mid_b","version":1,"field":"b1",'
        b'"path":[{"dataset":"mart_c","version":1,"field":"c1"},'
        b'{"dataset":"mid_b","version":1,"field":"b1"}],'
        b'"path_length":1},'
        b'{"dataset":"mid_b","version":1,"field":"b2",'
        b'"path":[{"dataset":"mart_c","version":1,"field":"c1"},'
        b'{"dataset":"mid_b","version":1,"field":"b2"}],'
        b'"path_length":1},'
        b'{"dataset":"raw_a","version":1,"field":"a1",'
        b'"path":[{"dataset":"mart_c","version":1,"field":"c1"},'
        b'{"dataset":"mid_b","version":1,"field":"b1"},'
        b'{"dataset":"raw_a","version":1,"field":"a1"}],'
        b'"path_length":2}],'
        b'"direct_count":2,"indirect_count":1,"source_dataset_count":2}\n'
    )
    assert response.content == expected
    assert response.text.endswith("\n")
    assert not response.text.endswith("\n\n")


def test_empty_origins_document_also_has_fixed_shape(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(
        f"/datasets/raw_a/versions/1{SOURCE_PATHS}", params={"field": "a1"}
    )
    assert response.content == (
        b'{"source":{"dataset":"raw_a","version":1,"field":"a1"},'
        b'"origins":[],'
        b'"direct_count":0,"indirect_count":0,"source_dataset_count":0}\n'
    )


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


def test_unknown_dataset_returns_404(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(
        f"/datasets/ghost/versions/1{SOURCE_PATHS}", params={"field": "a1"}
    )
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "not_found"
    assert isinstance(body["detail"], str) and body["detail"]
    assert "SQL" not in response.text.upper()


def test_unknown_version_returns_404(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(
        f"/datasets/raw_a/versions/99{SOURCE_PATHS}", params={"field": "a1"}
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_unknown_field_returns_404(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(
        f"/datasets/raw_a/versions/1{SOURCE_PATHS}",
        params={"field": "missing"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_missing_field_param_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(f"/datasets/raw_a/versions/1{SOURCE_PATHS}")
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_blank_field_param_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    for blank in ("", "   "):
        response = client.get(
            f"/datasets/raw_a/versions/1{SOURCE_PATHS}",
            params={"field": blank},
        )
        assert response.status_code == 422
        assert response.json()["error"] == "validation_error"


def test_unknown_query_parameter_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(
        f"/datasets/raw_a/versions/1{SOURCE_PATHS}",
        params={"field": "a1", "bogus": "x"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_repeated_field_parameter_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(
        f"/datasets/raw_a/versions/1{SOURCE_PATHS}",
        params=[("field", "a1"), ("field", "a2")],
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_request_body_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.request(
        "GET",
        f"/datasets/raw_a/versions/1{SOURCE_PATHS}",
        params={"field": "a1"},
        content=b"{}",
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_whitespace_only_body_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.request(
        "GET",
        f"/datasets/raw_a/versions/1{SOURCE_PATHS}",
        params={"field": "a1"},
        content=b"  ",
    )
    assert response.status_code == 422


def test_404_takes_precedence_over_422(client: TestClient) -> None:
    setup_branching_graph(client)

    # Unknown dataset beats a missing field parameter.
    response = client.get(f"/datasets/ghost/versions/1{SOURCE_PATHS}")
    assert response.status_code == 404

    # Unknown dataset beats an extra query parameter and a request body.
    response = client.request(
        "GET",
        f"/datasets/ghost/versions/1{SOURCE_PATHS}",
        params={"field": "a1", "bogus": "x"},
        content=b"{}",
    )
    assert response.status_code == 404

    # Unknown version beats an extra query parameter.
    response = client.get(
        f"/datasets/raw_a/versions/99{SOURCE_PATHS}",
        params={"field": "a1", "bogus": "x"},
    )
    assert response.status_code == 404

    # Unknown field beats body/parameter 422s.
    response = client.request(
        "GET",
        f"/datasets/raw_a/versions/1{SOURCE_PATHS}",
        params={"field": "missing", "bogus": "x"},
        content=b"{}",
    )
    assert response.status_code == 404


def test_only_get_is_accepted(client: TestClient) -> None:
    setup_branching_graph(client)
    url = f"/datasets/raw_a/versions/1{SOURCE_PATHS}"
    for method in ("post", "put", "delete", "patch"):
        response = client.request(method, url, content=b"{}")
        assert response.status_code == 405, method


# --------------------------------------------------------------------------- #
# Read-only behavior
# --------------------------------------------------------------------------- #


def test_query_is_read_only_and_never_uses_cache(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    assert cache_rows(isolated_database) == []

    body = origins(client, "mart_c", 1, "c1")
    assert body["origins"]
    # The endpoint neither reads nor writes the persistent impact cache.
    assert cache_rows(isolated_database) == []

    # Repeated reads are recomputed and identical.
    assert origins(client, "mart_c", 1, "c1") == body


def test_rejected_requests_write_nothing(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)

    client.get(
        f"/datasets/raw_a/versions/1{SOURCE_PATHS}",
        params={"field": "missing"},
    )
    client.get(f"/datasets/raw_a/versions/1{SOURCE_PATHS}")
    client.get(
        f"/datasets/raw_a/versions/1{SOURCE_PATHS}",
        params={"field": "a1", "bogus": "x"},
    )

    assert cache_rows(isolated_database) == []
    # The existing lineage mappings are untouched.
    lineage = client.get("/datasets/mid_b/versions/1/lineage")
    assert lineage.status_code == 200
    assert len(lineage.json()["fields"][0]["sources"]) == 1


def test_existing_impact_endpoints_are_unaffected(client: TestClient) -> None:
    setup_branching_graph(client)
    body = origins(client, "mart_c", 1, "c1")
    assert [
        (item["dataset"], item["version"], item["field"])
        for item in body["origins"]
    ] == [
        ("mid_b", 1, "b1"),
        ("mid_b", 1, "b2"),
        ("raw_a", 1, "a1"),
    ]

    impact = client.get(
        "/datasets/raw_a/versions/1/lineage/impact", params={"field": "a1"}
    )
    assert impact.status_code == 200
    assert impact.json()["impacted"] == [
        ref("mart_c", 1, "c1"),
        ref("mid_b", 1, "b1"),
        ref("mid_b", 1, "b2"),
    ]

    impact_paths = client.get(
        "/datasets/raw_a/versions/1/lineage/impact-paths",
        params={"field": "a1"},
    )
    assert impact_paths.status_code == 200
    assert impact_paths.json()["direct_count"] == 2
    assert impact_paths.json()["indirect_count"] == 1


# --------------------------------------------------------------------------- #
# Restart persistence (separate processes, one database file)
# --------------------------------------------------------------------------- #


BUILD_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response):
    assert response.status_code in (200, 201), response.text

for name, fields in (
    ("src", ["s1"]),
    ("mid", ["m1", "m2"]),
    ("dst", ["d1"]),
):
    ok(client.post("/datasets", json={"name": name}))
    ok(client.post(
        f"/datasets/{name}/versions",
        json={"fields": [
            {"name": f, "type": "string", "nullable": True} for f in fields
        ]},
    ))

ok(client.post("/datasets/mid/versions/1/lineage", json={
    "target_dataset": "mid", "target_version": 1, "target_field": "m1",
    "source_dataset": "src", "source_version": 1, "source_field": "s1",
}))
ok(client.post("/datasets/mid/versions/1/lineage", json={
    "target_dataset": "mid", "target_version": 1, "target_field": "m2",
    "source_dataset": "src", "source_version": 1, "source_field": "s1",
}))
ok(client.post("/datasets/dst/versions/1/lineage", json={
    "target_dataset": "dst", "target_version": 1, "target_field": "d1",
    "source_dataset": "mid", "source_version": 1, "source_field": "m1",
}))
ok(client.post("/datasets/dst/versions/1/lineage", json={
    "target_dataset": "dst", "target_version": 1, "target_field": "d1",
    "source_dataset": "mid", "source_version": 1, "source_field": "m2",
}))
print("built")
"""

READ_SCRIPT = """
import json
import os
import sqlite3
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

response = client.get(
    "/datasets/dst/versions/1/lineage/impact/source-paths",
    params={"field": "d1"},
)
assert response.status_code == 200, response.text
document = response.content
body = json.loads(document)
assert body["direct_count"] == 2
assert body["indirect_count"] == 1
assert body["source_dataset_count"] == 2
assert [
    (item["dataset"], item["version"], item["field"])
    for item in body["origins"]
] == [
    ("mid", 1, "m1"),
    ("mid", 1, "m2"),
    ("src", 1, "s1"),
]
src = body["origins"][2]
# Two equal-length routes dst.d1 -> mid.m1|m2 -> src.s1; m1 wins the tie.
assert src["path"] == [
    {"dataset": "dst", "version": 1, "field": "d1"},
    {"dataset": "mid", "version": 1, "field": "m1"},
    {"dataset": "src", "version": 1, "field": "s1"},
]
assert src["path_length"] == 2
assert document.endswith(b"\\n")

# The read-only endpoint leaves no cache footprint.
conn = sqlite3.connect(os.environ["DATA_LINEAGE_DB"])
try:
    assert conn.execute("SELECT COUNT(*) FROM lineage_impact_cache").fetchone()[0] == 0
finally:
    conn.close()

print(document.decode("utf-8"), end="")
"""


def _run(db_path: Path, script: str) -> str:
    env = os.environ.copy()
    env["DATA_LINEAGE_DB"] = str(db_path)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_paths_are_identical_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "source-paths-restart.db"

    assert _run(db_path, BUILD_SCRIPT).strip() == "built"
    first = _run(db_path, READ_SCRIPT)
    second = _run(db_path, READ_SCRIPT)
    assert first == second
    # The restarted process recomputed identical paths, lengths and counts.
    document = json.loads(second)
    assert document["origins"][2]["path_length"] == 2
    assert document["direct_count"] == 2
    assert document["indirect_count"] == 1
    assert document["source_dataset_count"] == 2
