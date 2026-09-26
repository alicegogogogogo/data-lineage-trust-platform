"""Tests for the read-only lineage impact cache consistency audit."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

CACHE_AUDIT = "/lineage/impact/cache-audit"


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


def delete_link(
    client: TestClient,
    source: tuple[str, int, str],
    target: tuple[str, int, str],
) -> None:
    source_dataset, source_version, source_field = source
    target_dataset, target_version, target_field = target
    response = client.request(
        "DELETE",
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
    assert response.status_code == 200, response.text


def audit(client: TestClient, dataset: str, version: int) -> dict:
    response = client.get(f"/datasets/{dataset}/versions/{version}{CACHE_AUDIT}")
    assert response.status_code == 200, response.text
    return response.json()


def statuses(body: dict) -> dict[str, str]:
    return {entry["field"]: entry["status"] for entry in body["entries"]}


def query_impact(client: TestClient, dataset: str, version: int, field: str) -> None:
    response = client.get(
        f"/datasets/{dataset}/versions/{version}/lineage/impact",
        params={"field": field},
    )
    assert response.status_code == 200, response.text


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


def cache_records(db_path: Path) -> dict[tuple[str, int, str], list]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT source_dataset, source_version, source_field, impacted "
            "FROM lineage_impact_cache"
        ).fetchall()
        return {
            (dataset, version, field): json.loads(impacted)
            for dataset, version, field, impacted in rows
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Audit semantics
# --------------------------------------------------------------------------- #


def test_unqueried_fields_are_missing_not_error(client: TestClient) -> None:
    setup_branching_graph(client)
    body = audit(client, "raw_a", 1)
    assert body["dataset"] == "raw_a"
    assert body["version"] == 1
    assert statuses(body) == {"a1": "missing", "a2": "missing"}
    assert body["counts"] == {
        "cached_count": 0,
        "missing_count": 2,
        "mismatch_count": 0,
    }


def test_successful_impact_query_marks_field_cached(client: TestClient) -> None:
    setup_branching_graph(client)
    query_impact(client, "raw_a", 1, "a1")

    body = audit(client, "raw_a", 1)
    assert statuses(body) == {"a1": "cached", "a2": "missing"}
    assert body["counts"] == {
        "cached_count": 1,
        "missing_count": 1,
        "mismatch_count": 0,
    }

    # The other datasets are audited independently: caching a1's impact also
    # produced no records for mid_b/mart_c source fields.
    assert statuses(audit(client, "mid_b", 1)) == {
        "b1": "missing",
        "b2": "missing",
    }


def test_empty_impact_result_is_a_cache_hit_too(client: TestClient) -> None:
    setup_branching_graph(client)
    # c1 and a2 have no downstream; their cached empty lists must match a
    # fresh empty recomputation.
    query_impact(client, "mart_c", 1, "c1")
    query_impact(client, "raw_a", 1, "a2")
    assert statuses(audit(client, "mart_c", 1)) == {"c1": "cached"}
    assert statuses(audit(client, "raw_a", 1))["a2"] == "cached"


def test_adding_mapping_invalidates_cached_record_back_to_missing(
    client: TestClient,
) -> None:
    setup_branching_graph(client)
    query_impact(client, "raw_a", 1, "a1")
    assert statuses(audit(client, "raw_a", 1))["a1"] == "cached"

    # A new outgoing edge from a1 invalidates a1's cached impact.
    make_dataset(client, "extra_e", ["e1"])
    add_link(client, ("raw_a", 1, "a1"), ("extra_e", 1, "e1"))
    assert statuses(audit(client, "raw_a", 1))["a1"] == "missing"

    # The next impact query repopulates the record and the audit hits again.
    query_impact(client, "raw_a", 1, "a1")
    assert statuses(audit(client, "raw_a", 1))["a1"] == "cached"


def test_deleting_mapping_invalidates_cached_record_back_to_missing(
    client: TestClient,
) -> None:
    setup_branching_graph(client)
    query_impact(client, "raw_a", 1, "a1")
    assert statuses(audit(client, "raw_a", 1))["a1"] == "cached"

    delete_link(client, ("raw_a", 1, "a1"), ("mid_b", 1, "b2"))
    assert statuses(audit(client, "raw_a", 1))["a1"] == "missing"


def test_stale_cache_record_is_mismatch(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    query_impact(client, "raw_a", 1, "a1")

    # Tamper with the persisted record so it no longer matches the graph.
    conn = sqlite3.connect(isolated_database)
    try:
        conn.execute(
            "UPDATE lineage_impact_cache SET impacted = ? "
            "WHERE source_dataset = ? AND source_version = ? AND source_field = ?",
            (
                json.dumps(
                    [{"dataset": "ghost", "version": 1, "field": "phantom"}]
                ),
                "raw_a",
                1,
                "a1",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    body = audit(client, "raw_a", 1)
    assert statuses(body) == {"a1": "mismatch", "a2": "missing"}
    assert body["counts"] == {
        "cached_count": 0,
        "missing_count": 1,
        "mismatch_count": 1,
    }


def test_cycle_graph_audits_without_unfolding(client: TestClient) -> None:
    make_dataset(client, "cyc_x", ["f"])
    make_dataset(client, "cyc_y", ["f"])
    add_link(client, ("cyc_x", 1, "f"), ("cyc_y", 1, "f"))
    add_link(client, ("cyc_y", 1, "f"), ("cyc_x", 1, "f"))
    query_impact(client, "cyc_x", 1, "f")
    body = audit(client, "cyc_x", 1)
    assert body["entries"] == [{"field": "f", "status": "cached"}]
    assert body["counts"]["cached_count"] == 1


def test_entries_sorted_by_field_name_independent_of_storage_order(
    client: TestClient,
) -> None:
    # Fields are deliberately registered in non-sorted order.
    make_dataset(client, "unsorted_u", ["z", "a", "m"])
    body = audit(client, "unsorted_u", 1)
    assert [entry["field"] for entry in body["entries"]] == ["a", "m", "z"]
    assert [entry["status"] for entry in body["entries"]] == [
        "missing",
        "missing",
        "missing",
    ]


def test_audit_scope_is_one_version_only(client: TestClient) -> None:
    make_dataset(client, "scoped_s", ["f1", "f2"])
    response = client.post(
        "/datasets/scoped_s/versions",
        json={
            "fields": [
                {"name": "f3", "type": "string", "nullable": True}
            ]
        },
    )
    assert response.status_code == 201
    query_impact(client, "scoped_s", 1, "f1")

    assert [entry["field"] for entry in audit(client, "scoped_s", 1)["entries"]] == [
        "f1",
        "f2",
    ]
    assert [entry["field"] for entry in audit(client, "scoped_s", 2)["entries"]] == [
        "f3"
    ]
    assert statuses(audit(client, "scoped_s", 2)) == {"f3": "missing"}


# --------------------------------------------------------------------------- #
# Deterministic serialization
# --------------------------------------------------------------------------- #


def test_document_is_compact_with_fixed_key_order_and_newline(
    client: TestClient,
) -> None:
    setup_branching_graph(client)
    query_impact(client, "raw_a", 1, "a1")
    response = client.get(f"/datasets/raw_a/versions/1{CACHE_AUDIT}")
    assert response.content == (
        b'{"dataset":"raw_a","version":1,'
        b'"entries":[{"field":"a1","status":"cached"},'
        b'{"field":"a2","status":"missing"}],'
        b'"counts":{"cached_count":1,"missing_count":1,"mismatch_count":0}}\n'
    )
    assert response.text.endswith("\n")
    assert not response.text.endswith("\n\n")


def test_single_missing_field_document_also_has_fixed_shape(
    client: TestClient,
) -> None:
    make_dataset(client, "blank_b", ["only"])
    response = client.get(f"/datasets/blank_b/versions/1{CACHE_AUDIT}")
    assert response.content == (
        b'{"dataset":"blank_b","version":1,'
        b'"entries":[{"field":"only","status":"missing"}],'
        b'"counts":{"cached_count":0,"missing_count":1,"mismatch_count":0}}\n'
    )


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


def test_unknown_dataset_returns_404(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(f"/datasets/ghost/versions/1{CACHE_AUDIT}")
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "not_found"
    assert isinstance(body["detail"], str) and body["detail"]
    assert "SQL" not in response.text.upper()


def test_unknown_version_returns_404(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(f"/datasets/raw_a/versions/99{CACHE_AUDIT}")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_query_parameter_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(
        f"/datasets/raw_a/versions/1{CACHE_AUDIT}", params={"bogus": "x"}
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_request_body_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.request(
        "GET", f"/datasets/raw_a/versions/1{CACHE_AUDIT}", content=b"{}"
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_whitespace_only_body_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.request(
        "GET", f"/datasets/raw_a/versions/1{CACHE_AUDIT}", content=b"   "
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_404_takes_precedence_over_422(client: TestClient) -> None:
    setup_branching_graph(client)

    # Unknown dataset beats a query parameter and a request body.
    response = client.request(
        "GET",
        f"/datasets/ghost/versions/1{CACHE_AUDIT}",
        params={"bogus": "x"},
        content=b"{}",
    )
    assert response.status_code == 404

    # Unknown version beats a query parameter and a request body.
    response = client.request(
        "GET",
        f"/datasets/raw_a/versions/99{CACHE_AUDIT}",
        params={"bogus": "x"},
        content=b"{}",
    )
    assert response.status_code == 404


def test_only_get_is_accepted(client: TestClient) -> None:
    setup_branching_graph(client)
    url = f"/datasets/raw_a/versions/1{CACHE_AUDIT}"
    for method in ("post", "put", "delete", "patch"):
        response = client.request(method, url, content=b"{}")
        assert response.status_code == 405, method


# --------------------------------------------------------------------------- #
# Read-only behavior
# --------------------------------------------------------------------------- #


def test_audit_creates_no_cache_records(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    assert cache_records(isolated_database) == {}

    audit(client, "raw_a", 1)
    audit(client, "mid_b", 1)
    assert cache_records(isolated_database) == {}


def test_audit_neither_repairs_nor_invalidates_mismatch(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    query_impact(client, "raw_a", 1, "a1")

    tampered = json.dumps(
        [{"dataset": "ghost", "version": 1, "field": "phantom"}]
    )
    conn = sqlite3.connect(isolated_database)
    try:
        conn.execute(
            "UPDATE lineage_impact_cache SET impacted = ? "
            "WHERE source_dataset = ? AND source_version = ? AND source_field = ?",
            (tampered, "raw_a", 1, "a1"),
        )
        conn.commit()
    finally:
        conn.close()

    assert statuses(audit(client, "raw_a", 1))["a1"] == "mismatch"
    # Repeated audits leave the stale record exactly as it was.
    assert statuses(audit(client, "raw_a", 1))["a1"] == "mismatch"
    records = cache_records(isolated_database)
    assert records[("raw_a", 1, "a1")] == [
        {"dataset": "ghost", "version": 1, "field": "phantom"}
    ]


def test_rejected_requests_write_nothing(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)

    client.get(f"/datasets/raw_a/versions/99{CACHE_AUDIT}")
    client.get(f"/datasets/raw_a/versions/1{CACHE_AUDIT}", params={"x": "1"})
    client.request(
        "GET", f"/datasets/raw_a/versions/1{CACHE_AUDIT}", content=b"{}"
    )

    assert cache_records(isolated_database) == {}
    # The existing lineage mappings are untouched.
    lineage = client.get("/datasets/mid_b/versions/1/lineage")
    assert lineage.status_code == 200
    assert len(lineage.json()["fields"][0]["sources"]) == 1


def test_other_lineage_reads_stay_unaffected_by_audit(client: TestClient) -> None:
    setup_branching_graph(client)
    query_impact(client, "raw_a", 1, "a1")

    audit(client, "raw_a", 1)

    # Impact query, downstream paths and upstream origins are unchanged.
    impact = client.get(
        "/datasets/raw_a/versions/1/lineage/impact", params={"field": "a1"}
    )
    assert impact.status_code == 200
    assert [(r["dataset"], r["field"]) for r in impact.json()["impacted"]] == [
        ("mart_c", "c1"),
        ("mid_b", "b1"),
        ("mid_b", "b2"),
    ]

    impact_paths = client.get(
        "/datasets/raw_a/versions/1/lineage/impact-paths",
        params={"field": "a1"},
    )
    assert impact_paths.status_code == 200
    assert impact_paths.json()["direct_count"] == 2

    source_paths = client.get(
        "/datasets/mart_c/versions/1/lineage/impact/source-paths",
        params={"field": "c1"},
    )
    assert source_paths.status_code == 200
    assert [
        (item["dataset"], item["field"]) for item in source_paths.json()["origins"]
    ] == [("mid_b", "b1"), ("mid_b", "b2"), ("raw_a", "a1")]


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
    ("dst", ["d1", "d2"]),
):
    ok(client.post("/datasets", json={"name": name}))
    ok(client.post(
        f"/datasets/{name}/versions",
        json={"fields": [
            {"name": f, "type": "string", "nullable": True} for f in fields
        ]},
    ))

ok(client.post("/datasets/dst/versions/1/lineage", json={
    "target_dataset": "dst", "target_version": 1, "target_field": "d1",
    "source_dataset": "src", "source_version": 1, "source_field": "s1",
}))

# Populate the cache for src.s1 only.
response = client.get(
    "/datasets/src/versions/1/lineage/impact", params={"field": "s1"}
)
assert response.status_code == 200
print("built")
"""

READ_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

response = client.get("/datasets/src/versions/1/lineage/impact/cache-audit")
assert response.status_code == 200, response.text
assert response.content == (
    b'{"dataset":"src","version":1,'
    b'"entries":[{"field":"s1","status":"cached"}],'
    b'"counts":{"cached_count":1,"missing_count":0,"mismatch_count":0}}\\n'
), response.content

response = client.get("/datasets/dst/versions/1/lineage/impact/cache-audit")
assert response.status_code == 200, response.text
assert response.content == (
    b'{"dataset":"dst","version":1,'
    b'"entries":[{"field":"d1","status":"missing"},'
    b'{"field":"d2","status":"missing"}],'
    b'"counts":{"cached_count":0,"missing_count":2,"mismatch_count":0}}\\n'
), response.content
print("read")
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


def test_audit_is_identical_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "cache-audit-restart.db"

    assert _run(db_path, BUILD_SCRIPT).strip() == "built"
    assert _run(db_path, READ_SCRIPT).strip() == "read"
    assert _run(db_path, READ_SCRIPT).strip() == "read"
