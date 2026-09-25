"""Tests for the lineage impact shortest-path explanation query."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def add_version(client: TestClient, name: str, fields: list[str]) -> int:
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
    return response.json()["version"]


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


def impact_paths(client: TestClient, dataset: str, version: int, field: str) -> dict:
    response = client.get(
        f"/datasets/{dataset}/versions/{version}/lineage/impact-paths",
        params={"field": field},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == {"dataset": dataset, "version": version, "field": field}
    return body


def ref(dataset: str, version: int, field: str) -> dict:
    return {"dataset": dataset, "version": version, "field": field}


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


def test_impact_paths_direct_downstream(client: TestClient) -> None:
    setup_branching_graph(client)
    body = impact_paths(client, "mid_b", 1, "b1")
    assert body["impacts"] == [
        {
            **ref("mart_c", 1, "c1"),
            "path": [ref("mid_b", 1, "b1"), ref("mart_c", 1, "c1")],
            "path_length": 1,
        }
    ]
    assert body["direct_count"] == 1
    assert body["indirect_count"] == 0


def test_impact_paths_multi_hop_branching_and_dedup(client: TestClient) -> None:
    setup_branching_graph(client)
    body = impact_paths(client, "raw_a", 1, "a1")
    # c1 is reachable via two equally short paths (through b1 or b2) but
    # appears exactly once, with the lexicographically smallest node sequence.
    assert body["impacts"] == [
        {
            **ref("mart_c", 1, "c1"),
            "path": [
                ref("raw_a", 1, "a1"),
                ref("mid_b", 1, "b1"),
                ref("mart_c", 1, "c1"),
            ],
            "path_length": 2,
        },
        {
            **ref("mid_b", 1, "b1"),
            "path": [ref("raw_a", 1, "a1"), ref("mid_b", 1, "b1")],
            "path_length": 1,
        },
        {
            **ref("mid_b", 1, "b2"),
            "path": [ref("raw_a", 1, "a1"), ref("mid_b", 1, "b2")],
            "path_length": 1,
        },
    ]
    assert body["direct_count"] == 2
    assert body["indirect_count"] == 1


def test_impact_paths_shortest_path_wins_over_longer_one(client: TestClient) -> None:
    # src.s -> mid.m -> dst.d and src.s -> dst.d: the direct edge is shorter.
    make_dataset(client, "src", ["s"])
    make_dataset(client, "mid", ["m"])
    make_dataset(client, "dst", ["d"])
    add_link(client, ("src", 1, "s"), ("mid", 1, "m"))
    add_link(client, ("mid", 1, "m"), ("dst", 1, "d"))
    add_link(client, ("src", 1, "s"), ("dst", 1, "d"))

    body = impact_paths(client, "src", 1, "s")
    assert body["impacts"] == [
        {
            **ref("dst", 1, "d"),
            "path": [ref("src", 1, "s"), ref("dst", 1, "d")],
            "path_length": 1,
        },
        {
            **ref("mid", 1, "m"),
            "path": [ref("src", 1, "s"), ref("mid", 1, "m")],
            "path_length": 1,
        },
    ]
    assert body["direct_count"] == 2
    assert body["indirect_count"] == 0


def test_impact_paths_tie_break_uses_node_sequence_order(client: TestClient) -> None:
    # src.s -> a_ds.f -> dst.d and src.s -> b_ds.f -> dst.d: both paths to
    # dst.d have length 2; the one through a_ds.f is lexicographically smaller.
    make_dataset(client, "src", ["s"])
    make_dataset(client, "b_ds", ["f"])
    make_dataset(client, "a_ds", ["f"])
    make_dataset(client, "dst", ["d"])
    add_link(client, ("src", 1, "s"), ("b_ds", 1, "f"))
    add_link(client, ("src", 1, "s"), ("a_ds", 1, "f"))
    add_link(client, ("b_ds", 1, "f"), ("dst", 1, "d"))
    add_link(client, ("a_ds", 1, "f"), ("dst", 1, "d"))

    body = impact_paths(client, "src", 1, "s")
    dst_entry = next(
        item for item in body["impacts"] if item["dataset"] == "dst"
    )
    assert dst_entry["path"] == [
        ref("src", 1, "s"),
        ref("a_ds", 1, "f"),
        ref("dst", 1, "d"),
    ]
    assert dst_entry["path_length"] == 2


def test_impact_paths_result_is_sorted_by_dataset_version_field(
    client: TestClient,
) -> None:
    make_dataset(client, "z_src", ["s"])
    make_dataset(client, "b_ds", ["f"])
    make_dataset(client, "a_ds", ["f2", "f1"])
    add_version(client, "a_ds", ["f1"])
    add_link(client, ("z_src", 1, "s"), ("b_ds", 1, "f"))
    add_link(client, ("z_src", 1, "s"), ("a_ds", 1, "f2"))
    add_link(client, ("z_src", 1, "s"), ("a_ds", 1, "f1"))
    add_link(client, ("z_src", 1, "s"), ("a_ds", 2, "f1"))

    body = impact_paths(client, "z_src", 1, "s")
    assert [
        (item["dataset"], item["version"], item["field"])
        for item in body["impacts"]
    ] == [
        ("a_ds", 1, "f1"),
        ("a_ds", 1, "f2"),
        ("a_ds", 2, "f1"),
        ("b_ds", 1, "f"),
    ]
    assert all(item["path_length"] == 1 for item in body["impacts"])
    assert body["direct_count"] == 4
    assert body["indirect_count"] == 0


def test_impact_paths_without_downstream_is_empty(client: TestClient) -> None:
    setup_branching_graph(client)
    for dataset, field in (("mart_c", "c1"), ("raw_a", "a2")):
        body = impact_paths(client, dataset, 1, field)
        assert body["impacts"] == []
        assert body["direct_count"] == 0
        assert body["indirect_count"] == 0


def test_impact_paths_with_cycle_terminates_and_excludes_source(
    client: TestClient,
) -> None:
    make_dataset(client, "cyc_x", ["f"])
    make_dataset(client, "cyc_y", ["f"])
    make_dataset(client, "cyc_z", ["f"])
    add_link(client, ("cyc_x", 1, "f"), ("cyc_y", 1, "f"))
    add_link(client, ("cyc_y", 1, "f"), ("cyc_z", 1, "f"))
    add_link(client, ("cyc_z", 1, "f"), ("cyc_x", 1, "f"))

    body = impact_paths(client, "cyc_x", 1, "f")
    assert body["impacts"] == [
        {
            **ref("cyc_y", 1, "f"),
            "path": [ref("cyc_x", 1, "f"), ref("cyc_y", 1, "f")],
            "path_length": 1,
        },
        {
            **ref("cyc_z", 1, "f"),
            "path": [ref("cyc_x", 1, "f"), ref("cyc_y", 1, "f"), ref("cyc_z", 1, "f")],
            "path_length": 2,
        },
    ]
    assert body["direct_count"] == 1
    assert body["indirect_count"] == 1


# --------------------------------------------------------------------------- #
# Deterministic serialization
# --------------------------------------------------------------------------- #


def test_impact_paths_response_shape_is_fixed(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(
        "/datasets/raw_a/versions/1/lineage/impact-paths",
        params={"field": "a1"},
    )
    assert response.status_code == 200, response.text

    # Compact whitespace, exactly one trailing newline, fixed key order.
    text = response.text
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert text == json.dumps(json.loads(text), separators=(",", ":")) + "\n"

    body = json.loads(text)
    assert list(body) == ["source", "impacts", "direct_count", "indirect_count"]
    for item in body["impacts"]:
        assert list(item) == ["dataset", "version", "field", "path", "path_length"]
        for node in item["path"]:
            assert list(node) == ["dataset", "version", "field"]

    # Repeated reads return byte-identical documents.
    again = client.get(
        "/datasets/raw_a/versions/1/lineage/impact-paths",
        params={"field": "a1"},
    )
    assert again.text == text


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


def test_impact_paths_unknown_dataset_returns_404(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(
        "/datasets/ghost/versions/1/lineage/impact-paths", params={"field": "a1"}
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_impact_paths_unknown_version_returns_404(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(
        "/datasets/raw_a/versions/99/lineage/impact-paths", params={"field": "a1"}
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_impact_paths_unknown_field_returns_404(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(
        "/datasets/raw_a/versions/1/lineage/impact-paths",
        params={"field": "missing"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_impact_paths_missing_field_param_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get("/datasets/raw_a/versions/1/lineage/impact-paths")
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_impact_paths_blank_field_param_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    for blank in ("", "   "):
        response = client.get(
            "/datasets/raw_a/versions/1/lineage/impact-paths",
            params={"field": blank},
        )
        assert response.status_code == 422
        assert response.json()["error"] == "validation_error"


def test_impact_paths_request_body_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.request(
        "GET",
        "/datasets/raw_a/versions/1/lineage/impact-paths",
        params={"field": "a1"},
        content=b"{}",
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_impact_paths_extra_query_param_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(
        "/datasets/raw_a/versions/1/lineage/impact-paths",
        params={"field": "a1", "other": "x"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_impact_paths_404_precedes_422(client: TestClient) -> None:
    setup_branching_graph(client)
    # Unknown dataset plus a request body and an extra query parameter.
    response = client.request(
        "GET",
        "/datasets/ghost/versions/1/lineage/impact-paths",
        params={"field": "a1", "other": "x"},
        content=b"{}",
    )
    assert response.status_code == 404
    # Unknown field plus a request body.
    response = client.request(
        "GET",
        "/datasets/raw_a/versions/1/lineage/impact-paths",
        params={"field": "missing"},
        content=b"{}",
    )
    assert response.status_code == 404


def test_impact_paths_rejections_write_nothing(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    client.get(
        "/datasets/raw_a/versions/1/lineage/impact-paths",
        params={"field": "a1", "other": "x"},
    )
    client.get(
        "/datasets/raw_a/versions/1/lineage/impact-paths",
        params={"field": "ghost"},
    )
    client.get("/datasets/raw_a/versions/1/lineage/impact-paths")
    assert cache_rows(isolated_database) == []


# --------------------------------------------------------------------------- #
# Read-only behaviour
# --------------------------------------------------------------------------- #


def test_impact_paths_never_writes_the_impact_cache(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    impact_paths(client, "raw_a", 1, "a1")
    impact_paths(client, "mart_c", 1, "c1")
    # Unlike the cached impact query, the path explanation stores nothing.
    assert cache_rows(isolated_database) == []


def test_impact_paths_matches_impact_set_of_cached_query(
    client: TestClient,
) -> None:
    setup_branching_graph(client)
    impact = client.get(
        "/datasets/raw_a/versions/1/lineage/impact", params={"field": "a1"}
    )
    assert impact.status_code == 200, impact.text
    body = impact_paths(client, "raw_a", 1, "a1")
    assert [
        {"dataset": item["dataset"], "version": item["version"], "field": item["field"]}
        for item in body["impacts"]
    ] == impact.json()["impacted"]


def test_existing_endpoints_unaffected_by_impact_paths_queries(
    client: TestClient,
) -> None:
    setup_branching_graph(client)
    impact_paths(client, "raw_a", 1, "a1")

    lineage = client.get("/datasets/mid_b/versions/1/lineage")
    assert lineage.status_code == 200
    by_field = {f["target_field"]: f["sources"] for f in lineage.json()["fields"]}
    assert by_field["b1"] == [ref("raw_a", 1, "a1")]

    impact = client.get(
        "/datasets/raw_a/versions/1/lineage/impact", params={"field": "a1"}
    )
    assert impact.status_code == 200
    assert impact.json()["impacted"] == [
        ref("mart_c", 1, "c1"),
        ref("mid_b", 1, "b1"),
        ref("mid_b", 1, "b2"),
    ]


# --------------------------------------------------------------------------- #
# Restart determinism (separate processes, one database file)
# --------------------------------------------------------------------------- #

CREATE_AND_QUERY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response):
    assert response.status_code in (200, 201), response.text

for name, fields in (
    ("src", ["s1"]),
    ("mid", ["m1"]),
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
ok(client.post("/datasets/dst/versions/1/lineage", json={
    "target_dataset": "dst", "target_version": 1, "target_field": "d1",
    "source_dataset": "mid", "source_version": 1, "source_field": "m1",
}))

first = client.get("/datasets/src/versions/1/lineage/impact-paths",
                   params={"field": "s1"})
assert first.status_code == 200, first.text
print(json.dumps(first.json(), sort_keys=True))
"""

VERIFY_AFTER_RESTART_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

again = client.get("/datasets/src/versions/1/lineage/impact-paths",
                   params={"field": "s1"})
assert again.status_code == 200, again.text
print(json.dumps(again.json(), sort_keys=True))
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
    return result.stdout.strip()


def test_impact_paths_are_identical_across_process_restarts(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "impact-paths-restart.db"

    before = json.loads(_run(db_path, CREATE_AND_QUERY_SCRIPT))
    after = json.loads(_run(db_path, VERIFY_AFTER_RESTART_SCRIPT))

    assert before == after
    assert before["impacts"] == [
        {
            "dataset": "dst",
            "version": 1,
            "field": "d1",
            "path": [
                {"dataset": "src", "version": 1, "field": "s1"},
                {"dataset": "mid", "version": 1, "field": "m1"},
                {"dataset": "dst", "version": 1, "field": "d1"},
            ],
            "path_length": 2,
        },
        {
            "dataset": "mid",
            "version": 1,
            "field": "m1",
            "path": [
                {"dataset": "src", "version": 1, "field": "s1"},
                {"dataset": "mid", "version": 1, "field": "m1"},
            ],
            "path_length": 1,
        },
    ]
    assert before["direct_count"] == 1
    assert before["indirect_count"] == 1
