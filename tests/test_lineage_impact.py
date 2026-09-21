"""Tests for the field-level lineage impact query and its persistent cache."""

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


def impact(
    client: TestClient, dataset: str, version: int, field: str
) -> list[dict]:
    response = client.get(
        f"/datasets/{dataset}/versions/{version}/lineage/impact",
        params={"field": field},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == {"dataset": dataset, "version": version, "field": field}
    return body["impacted"]


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
# Traversal semantics
# --------------------------------------------------------------------------- #


def test_impact_direct_downstream(client: TestClient) -> None:
    setup_branching_graph(client)
    assert impact(client, "mid_b", 1, "b1") == [ref("mart_c", 1, "c1")]


def test_impact_multi_hop_branching_and_dedup(client: TestClient) -> None:
    setup_branching_graph(client)
    # c1 is reachable via two paths (b1 and b2) but must appear exactly once.
    assert impact(client, "raw_a", 1, "a1") == [
        ref("mart_c", 1, "c1"),
        ref("mid_b", 1, "b1"),
        ref("mid_b", 1, "b2"),
    ]


def test_impact_without_downstream_is_empty(client: TestClient) -> None:
    setup_branching_graph(client)
    assert impact(client, "mart_c", 1, "c1") == []
    assert impact(client, "raw_a", 1, "a2") == []


def test_impact_result_is_sorted_by_dataset_version_field(
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

    assert impact(client, "z_src", 1, "s") == [
        ref("a_ds", 1, "f1"),
        ref("a_ds", 1, "f2"),
        ref("a_ds", 2, "f1"),
        ref("b_ds", 1, "f"),
    ]


def test_impact_with_cycle_terminates_and_excludes_source(
    client: TestClient,
) -> None:
    make_dataset(client, "cyc_x", ["f"])
    make_dataset(client, "cyc_y", ["f"])
    make_dataset(client, "cyc_z", ["f"])
    add_link(client, ("cyc_x", 1, "f"), ("cyc_y", 1, "f"))
    add_link(client, ("cyc_y", 1, "f"), ("cyc_z", 1, "f"))
    add_link(client, ("cyc_z", 1, "f"), ("cyc_x", 1, "f"))

    # The cycle leads back to the source, which must not list itself.
    assert impact(client, "cyc_x", 1, "f") == [
        ref("cyc_y", 1, "f"),
        ref("cyc_z", 1, "f"),
    ]
    assert impact(client, "cyc_y", 1, "f") == [
        ref("cyc_x", 1, "f"),
        ref("cyc_z", 1, "f"),
    ]


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


def test_impact_unknown_dataset_returns_404(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(
        "/datasets/ghost/versions/1/lineage/impact", params={"field": "a1"}
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_impact_unknown_version_returns_404(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(
        "/datasets/raw_a/versions/99/lineage/impact", params={"field": "a1"}
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_impact_unknown_field_returns_404(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(
        "/datasets/raw_a/versions/1/lineage/impact", params={"field": "missing"}
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_impact_missing_field_param_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get("/datasets/raw_a/versions/1/lineage/impact")
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_impact_blank_field_param_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    for blank in ("", "   "):
        response = client.get(
            "/datasets/raw_a/versions/1/lineage/impact", params={"field": blank}
        )
        assert response.status_code == 422
        assert response.json()["error"] == "validation_error"


# --------------------------------------------------------------------------- #
# Persistent cache
# --------------------------------------------------------------------------- #


def test_impact_results_are_cached_persistently(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    expected = impact(client, "raw_a", 1, "a1")

    assert ("raw_a", 1, "a1") in cache_rows(isolated_database)

    # A fresh client (nothing cached in process memory) serves the same result.
    fresh = TestClient(client.app)
    assert impact(fresh, "raw_a", 1, "a1") == expected


def test_new_mapping_invalidates_source_and_upstream_cache(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    make_dataset(client, "rep_d", ["d1"])
    make_dataset(client, "lonely", ["l1"])

    # Populate the cache: upstream (raw_a.a1), direct source (mart_c.c1) and
    # an unrelated entry that must survive the invalidation.
    impact(client, "raw_a", 1, "a1")
    impact(client, "mart_c", 1, "c1")
    impact(client, "lonely", 1, "l1")

    add_link(client, ("mart_c", 1, "c1"), ("rep_d", 1, "d1"))

    # The new mapping's source and every field that reaches it are dropped...
    remaining = cache_rows(isolated_database)
    assert ("raw_a", 1, "a1") not in remaining
    assert ("mart_c", 1, "c1") not in remaining
    # ...while unrelated cache entries are kept.
    assert ("lonely", 1, "l1") in remaining

    # Recomputed results reflect the extended graph, upstream included.
    assert impact(client, "mart_c", 1, "c1") == [ref("rep_d", 1, "d1")]
    assert impact(client, "raw_a", 1, "a1") == [
        ref("mart_c", 1, "c1"),
        ref("mid_b", 1, "b1"),
        ref("mid_b", 1, "b2"),
        ref("rep_d", 1, "d1"),
    ]
    # The untouched entry still answers from its preserved cache.
    assert impact(client, "lonely", 1, "l1") == []


def test_new_mapping_invalidates_upstream_across_multiple_hops(
    client: TestClient,
) -> None:
    # Chain one -> two -> three, cache every impact, then extend the chain.
    make_dataset(client, "one", ["f"])
    make_dataset(client, "two", ["f"])
    make_dataset(client, "three", ["f"])
    make_dataset(client, "four", ["f"])
    add_link(client, ("one", 1, "f"), ("two", 1, "f"))
    add_link(client, ("two", 1, "f"), ("three", 1, "f"))

    assert impact(client, "one", 1, "f") == [
        ref("three", 1, "f"),
        ref("two", 1, "f"),
    ]
    assert impact(client, "two", 1, "f") == [ref("three", 1, "f")]
    assert impact(client, "three", 1, "f") == []

    add_link(client, ("three", 1, "f"), ("four", 1, "f"))

    assert impact(client, "three", 1, "f") == [ref("four", 1, "f")]
    assert impact(client, "two", 1, "f") == [
        ref("four", 1, "f"),
        ref("three", 1, "f"),
    ]
    assert impact(client, "one", 1, "f") == [
        ref("four", 1, "f"),
        ref("three", 1, "f"),
        ref("two", 1, "f"),
    ]


def test_new_schema_version_invalidates_dataset_cache(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    make_dataset(client, "lonely", ["l1"])

    impact(client, "raw_a", 1, "a1")
    impact(client, "lonely", 1, "l1")

    # mid_b is part of raw_a.a1's cached impact, so a new mid_b version drops
    # that entry; the unrelated entry is preserved.
    add_version(client, "mid_b", ["b1", "b2", "b3"])

    remaining = cache_rows(isolated_database)
    assert ("raw_a", 1, "a1") not in remaining
    assert ("lonely", 1, "l1") in remaining

    # The recomputed result is unchanged (no new links) and gets re-cached.
    assert impact(client, "raw_a", 1, "a1") == [
        ref("mart_c", 1, "c1"),
        ref("mid_b", 1, "b1"),
        ref("mid_b", 1, "b2"),
    ]
    assert ("raw_a", 1, "a1") in cache_rows(isolated_database)


def test_failed_writes_do_not_change_cache_or_data(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    before = impact(client, "raw_a", 1, "a1")
    cached_before = cache_rows(isolated_database)

    # 409: duplicate mapping mapping; 404: unknown source field; 422: body/path
    # target mismatch. None of them may touch the cache or the stored graph.
    duplicate = client.post(
        "/datasets/mid_b/versions/1/lineage",
        json={
            "target_dataset": "mid_b",
            "target_version": 1,
            "target_field": "b1",
            "source_dataset": "raw_a",
            "source_version": 1,
            "source_field": "a1",
        },
    )
    assert duplicate.status_code == 409

    unknown = client.post(
        "/datasets/mid_b/versions/1/lineage",
        json={
            "target_dataset": "mid_b",
            "target_version": 1,
            "target_field": "b1",
            "source_dataset": "raw_a",
            "source_version": 1,
            "source_field": "ghost",
        },
    )
    assert unknown.status_code == 404

    invalid = client.post(
        "/datasets/mid_b/versions/1/lineage",
        json={
            "target_dataset": "other",
            "target_version": 1,
            "target_field": "b1",
            "source_dataset": "raw_a",
            "source_version": 1,
            "source_field": "a1",
        },
    )
    assert invalid.status_code == 422

    assert cache_rows(isolated_database) == cached_before
    assert impact(client, "raw_a", 1, "a1") == before


def test_existing_lineage_endpoints_unaffected_by_impact_queries(
    client: TestClient,
) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")

    lineage = client.get("/datasets/mid_b/versions/1/lineage")
    assert lineage.status_code == 200
    body = lineage.json()
    assert body["target_dataset"] == "mid_b"
    assert body["target_version"] == 1
    by_field = {f["target_field"]: f["sources"] for f in body["fields"]}
    assert by_field["b1"] == [ref("raw_a", 1, "a1")]
    assert by_field["b2"] == [ref("raw_a", 1, "a1")]

    created = client.post(
        "/datasets/mart_c/versions/1/lineage",
        json={
            "target_dataset": "mart_c",
            "target_version": 1,
            "target_field": "c1",
            "source_dataset": "raw_a",
            "source_version": 1,
            "source_field": "a2",
        },
    )
    assert created.status_code == 201
    assert created.json()["source"] == ref("raw_a", 1, "a2")


# --------------------------------------------------------------------------- #
# Restart persistence (separate processes, one database file)
# --------------------------------------------------------------------------- #

CREATE_AND_CACHE_SCRIPT = """
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

# Populate the persistent cache before the "restart".
first = client.get("/datasets/src/versions/1/lineage/impact",
                   params={"field": "s1"})
assert first.status_code == 200, first.text
assert first.json()["impacted"] == [
    {"dataset": "dst", "version": 1, "field": "d1"},
    {"dataset": "mid", "version": 1, "field": "m1"},
]
print("cached")
"""

VERIFY_AFTER_RESTART_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

# The cached result survives the restart and is served as-is.
cached = client.get("/datasets/src/versions/1/lineage/impact",
                    params={"field": "s1"})
assert cached.status_code == 200, cached.text
assert cached.json() == {
    "source": {"dataset": "src", "version": 1, "field": "s1"},
    "impacted": [
        {"dataset": "dst", "version": 1, "field": "d1"},
        {"dataset": "mid", "version": 1, "field": "m1"},
    ],
}

# A mapping committed after the restart invalidates the stale cached entry.
# src.s1 -> dst.d1 is a new direct edge alongside the indirect path via mid.
created = client.post("/datasets/dst/versions/1/lineage", json={
    "target_dataset": "dst", "target_version": 1, "target_field": "d1",
    "source_dataset": "src", "source_version": 1, "source_field": "s1",
})
assert created.status_code == 201, created.text

# The impact set is unchanged by the redundant edge but is recomputed cleanly.
fresh = client.get("/datasets/src/versions/1/lineage/impact",
                   params={"field": "s1"})
assert fresh.status_code == 200, fresh.text
assert fresh.json()["impacted"] == [
    {"dataset": "dst", "version": 1, "field": "d1"},
    {"dataset": "mid", "version": 1, "field": "m1"},
]

# Extending the graph is visible immediately, proving no stale cache reuse.
ok = client.post("/datasets", json={"name": "sink"})
assert ok.status_code == 201, ok.text
ok = client.post(
    "/datasets/sink/versions",
    json={"fields": [{"name": "k1", "type": "string", "nullable": True}]},
)
assert ok.status_code == 201, ok.text
ok = client.post("/datasets/sink/versions/1/lineage", json={
    "target_dataset": "sink", "target_version": 1, "target_field": "k1",
    "source_dataset": "dst", "source_version": 1, "source_field": "d1",
})
assert ok.status_code == 201, ok.text

extended = client.get("/datasets/src/versions/1/lineage/impact",
                      params={"field": "s1"})
assert extended.status_code == 200, extended.text
assert extended.json()["impacted"] == [
    {"dataset": "dst", "version": 1, "field": "d1"},
    {"dataset": "mid", "version": 1, "field": "m1"},
    {"dataset": "sink", "version": 1, "field": "k1"},
]
print(json.dumps({"impacted_count": len(extended.json()["impacted"])}))
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


def test_impact_cache_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "impact-restart.db"

    assert _run(db_path, CREATE_AND_CACHE_SCRIPT) == "cached"
    output = _run(db_path, VERIFY_AFTER_RESTART_SCRIPT)
    assert json.loads(output)["impacted_count"] == 3
