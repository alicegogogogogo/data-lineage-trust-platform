"""Tests for the controlled lineage impact cache repair endpoint."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import repository

PROJECT_ROOT = Path(__file__).resolve().parents[1]

REPAIR_PATH = (
    "/datasets/{dataset}/versions/{version}/lineage/impact/cache-audit/repair"
)
AUDIT_PATH = "/datasets/{dataset}/versions/{version}/lineage/impact/cache-audit"


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
    return response.json()["impacted"]


def repair(client: TestClient, dataset: str, version: int) -> tuple[int, dict, str]:
    response = client.post(REPAIR_PATH.format(dataset=dataset, version=version))
    return response.status_code, response.json(), response.text


def audit_statuses(client: TestClient, dataset: str, version: int) -> dict[str, str]:
    response = client.get(AUDIT_PATH.format(dataset=dataset, version=version))
    assert response.status_code == 200, response.text
    return {entry["field"]: entry["status"] for entry in response.json()["entries"]}


def ref(dataset: str, version: int, field: str) -> dict:
    return {"dataset": dataset, "version": version, "field": field}


def cache_dump(db_path: Path) -> dict[tuple[str, int, str], str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT source_dataset, source_version, source_field, impacted "
            "FROM lineage_impact_cache"
        ).fetchall()
    finally:
        conn.close()
    return {
        (dataset, version, field): impacted
        for dataset, version, field, impacted in rows
    }


def actions(payload: dict) -> dict[str, str]:
    return {entry["field"]: entry["action"] for entry in payload["entries"]}


def setup_branching_graph(client: TestClient) -> None:
    # raw_a.a1 -> mid_b.b1 -> mart_c.c1
    #          -> mid_b.b2 -> mart_c.c1
    make_dataset(client, "raw_a", ["a1", "a2"])
    make_dataset(client, "mid_b", ["b1", "b2"])
    make_dataset(client, "mart_c", ["c1"])
    add_link(client, ("raw_a", 1, "a1"), ("mid_b", 1, "b1"))
    add_link(client, ("raw_a", 1, "a1"), ("mid_b", 1, "b2"))
    add_link(client, ("mid_b", 1, "b1"), ("mart_c", 1, "c1"))
    add_link(client, ("mid_b", 1, "b2"), ("mart_c", 1, "c1"))


def corrupt_cache_record(
    db_path: Path, dataset: str, version: int, field: str, impacted: list[dict]
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE lineage_impact_cache SET impacted = ? "
            "WHERE source_dataset = ? AND source_version = ? AND source_field = ?",
            (json.dumps(impacted), dataset, version, field),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Repair actions
# --------------------------------------------------------------------------- #


def test_repair_creates_missing_records(client: TestClient) -> None:
    setup_branching_graph(client)

    status_code, payload, _ = repair(client, "raw_a", 1)
    assert status_code == 200
    assert payload["dataset"] == "raw_a"
    assert payload["version"] == 1
    assert actions(payload) == {"a1": "created", "a2": "created"}
    assert payload["counts"] == {
        "created_count": 2,
        "updated_count": 0,
        "unchanged_count": 0,
    }

    # The created records are exactly the impact-query results.
    assert impact(client, "raw_a", 1, "a1") == [
        ref("mart_c", 1, "c1"),
        ref("mid_b", 1, "b1"),
        ref("mid_b", 1, "b2"),
    ]
    assert impact(client, "raw_a", 1, "a2") == []


def test_repair_updates_stale_records_and_keeps_consistent_ones(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")
    impact(client, "mid_b", 1, "b1")

    # Stale: the stored record claims a field the graph cannot reach.
    corrupt_cache_record(isolated_database, "raw_a", 1, "a1", [ref("ghost", 7, "g1")])

    status_code, payload, _ = repair(client, "raw_a", 1)
    assert status_code == 200
    assert actions(payload) == {"a1": "updated", "a2": "created"}
    assert payload["counts"] == {
        "created_count": 1,
        "updated_count": 1,
        "unchanged_count": 0,
    }

    stored = cache_dump(isolated_database)
    assert json.loads(stored[("raw_a", 1, "a1")]) == [
        ref("mart_c", 1, "c1"),
        ref("mid_b", 1, "b1"),
        ref("mid_b", 1, "b2"),
    ]
    # The consistent mid_b record was not part of this version's repair.
    assert ("mid_b", 1, "b1") in stored

    # mid_b's own repair leaves its consistent record unchanged.
    _, mid_payload, _ = repair(client, "mid_b", 1)
    assert actions(mid_payload) == {"b1": "unchanged", "b2": "created"}


def test_consistent_records_are_not_rewritten(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")
    before = cache_dump(isolated_database)

    _, payload, _ = repair(client, "raw_a", 1)
    assert actions(payload) == {"a1": "unchanged", "a2": "created"}

    # The consistent record is byte-for-byte identical (not rewritten).
    after = cache_dump(isolated_database)
    assert after[("raw_a", 1, "a1")] == before[("raw_a", 1, "a1")]

    # A second repair changes nothing at all.
    _, second, _ = repair(client, "raw_a", 1)
    assert actions(second) == {"a1": "unchanged", "a2": "unchanged"}
    assert second["counts"] == {
        "created_count": 0,
        "updated_count": 0,
        "unchanged_count": 2,
    }
    assert cache_dump(isolated_database) == after


def test_repair_is_scoped_to_the_path_version(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client, "raw_a", ["a1", "a2"])
    make_dataset(client, "other", ["o1"])
    v2 = add_version(client, "raw_a", ["a1", "a2", "a3"])
    add_link(client, ("raw_a", 1, "a1"), ("other", 1, "o1"))
    impact(client, "other", 1, "o1")
    before = cache_dump(isolated_database)

    _, payload, _ = repair(client, "raw_a", 1)
    assert [entry["field"] for entry in payload["entries"]] == ["a1", "a2"]

    # Version 2 and the other dataset's cache record are untouched.
    after = cache_dump(isolated_database)
    assert after[("other", 1, "o1")] == before[("other", 1, "o1")]
    assert not any(dataset == "raw_a" and ver == v2 for dataset, ver, _ in after)


def test_cyclic_graph_repairs_and_terminates(client: TestClient) -> None:
    make_dataset(client, "cyc_x", ["f"])
    make_dataset(client, "cyc_y", ["f"])
    add_link(client, ("cyc_x", 1, "f"), ("cyc_y", 1, "f"))
    add_link(client, ("cyc_y", 1, "f"), ("cyc_x", 1, "f"))

    status_code, payload, _ = repair(client, "cyc_x", 1)
    assert status_code == 200
    assert actions(payload) == {"f": "created"}
    # The cycle terminates and the start field never appears in its own impact.
    assert impact(client, "cyc_x", 1, "f") == [ref("cyc_y", 1, "f")]


# --------------------------------------------------------------------------- #
# Effect on the audit, the impact query and persistence
# --------------------------------------------------------------------------- #


def test_audit_is_fully_cached_after_repair(client: TestClient) -> None:
    setup_branching_graph(client)
    assert audit_statuses(client, "raw_a", 1) == {"a1": "missing", "a2": "missing"}

    repair(client, "raw_a", 1)

    assert audit_statuses(client, "raw_a", 1) == {"a1": "cached", "a2": "cached"}
    response = client.get(AUDIT_PATH.format(dataset="raw_a", version=1))
    assert response.json()["counts"] == {
        "cached_count": 2,
        "missing_count": 0,
        "mismatch_count": 0,
    }


def test_repaired_records_are_visible_to_impact_and_path_queries(
    client: TestClient,
) -> None:
    setup_branching_graph(client)
    repair(client, "raw_a", 1)

    # The impact query is answered from the repaired cache immediately.
    assert impact(client, "raw_a", 1, "a1") == [
        ref("mart_c", 1, "c1"),
        ref("mid_b", 1, "b1"),
        ref("mid_b", 1, "b2"),
    ]

    paths = client.get(
        "/datasets/raw_a/versions/1/lineage/impact-paths",
        params={"field": "a1"},
    )
    assert paths.status_code == 200
    assert [item["field"] for item in paths.json()["impacts"]] == ["c1", "b1", "b2"]

    sources = client.get(
        "/datasets/mart_c/versions/1/lineage/impact/source-paths",
        params={"field": "c1"},
    )
    assert sources.status_code == 200
    assert [item["field"] for item in sources.json()["origins"]] == ["b1", "b2", "a1"]


RESTART_CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response):
    assert response.status_code in (200, 201), response.text

ok(client.post("/datasets", json={"name": "raw_a"}))
ok(client.post("/datasets", json={"name": "mid_b"}))
ok(client.post(
    "/datasets/raw_a/versions",
    json={"fields": [{"name": "a1", "type": "string", "nullable": True}]},
))
ok(client.post(
    "/datasets/mid_b/versions",
    json={"fields": [{"name": "b1", "type": "string", "nullable": True}]},
))
ok(client.post(
    "/datasets/mid_b/versions/1/lineage",
    json={
        "target_dataset": "mid_b",
        "target_version": 1,
        "target_field": "b1",
        "source_dataset": "raw_a",
        "source_version": 1,
        "source_field": "a1",
    },
))
repaired = client.post(
    "/datasets/raw_a/versions/1/lineage/impact/cache-audit/repair"
)
assert repaired.status_code == 200, repaired.text
assert repaired.json()["entries"] == [{"field": "a1", "action": "created"}]
print("repaired")
"""

RESTART_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

# The repaired record survives the restart and serves the impact query.
impact = client.get(
    "/datasets/raw_a/versions/1/lineage/impact", params={"field": "a1"}
)
assert impact.status_code == 200, impact.text
assert impact.json()["impacted"] == [
    {"dataset": "mid_b", "version": 1, "field": "b1"}
]

audit = client.get("/datasets/raw_a/versions/1/lineage/impact/cache-audit")
assert audit.status_code == 200, audit.text
assert audit.json()["entries"] == [{"field": "a1", "status": "cached"}]

# A post-restart repair finds everything consistent and writes nothing.
repaired = client.post(
    "/datasets/raw_a/versions/1/lineage/impact/cache-audit/repair"
)
assert repaired.status_code == 200, repaired.text
assert repaired.json()["entries"] == [{"field": "a1", "action": "unchanged"}]
print("verified")
"""


def _run_script(db_path: Path, script: str) -> str:
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


def test_repair_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "repair-persistence.db"
    assert _run_script(db_path, RESTART_CREATE_SCRIPT) == "repaired"
    assert _run_script(db_path, RESTART_VERIFY_SCRIPT) == "verified"


# --------------------------------------------------------------------------- #
# Ordering, counts and deterministic serialization
# --------------------------------------------------------------------------- #


def test_entries_sort_by_field_name_independent_of_insertion_order(
    client: TestClient,
) -> None:
    make_dataset(client, "zeta_ds", ["zebra", "apple", "mango"])

    _, payload, _ = repair(client, "zeta_ds", 1)
    assert [entry["field"] for entry in payload["entries"]] == [
        "apple",
        "mango",
        "zebra",
    ]


def test_counts_equal_the_entry_totals(client: TestClient) -> None:
    setup_branching_graph(client)
    _, payload, _ = repair(client, "raw_a", 1)
    counts = payload["counts"]
    for action in ("created", "updated", "unchanged"):
        assert counts[f"{action}_count"] == sum(
            1 for entry in payload["entries"] if entry["action"] == action
        )
    assert sum(counts.values()) == len(payload["entries"]) == 2


def test_response_body_is_deterministic_compact_json_with_newline(
    client: TestClient,
) -> None:
    make_dataset(client, "raw_a", ["a1", "a2"])

    response = client.post(REPAIR_PATH.format(dataset="raw_a", version=1))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.text.endswith("}\n")
    assert not response.text.endswith("}\n\n")

    payload = response.json()
    assert list(payload) == ["dataset", "version", "entries", "counts"]
    assert all(list(entry) == ["field", "action"] for entry in payload["entries"])
    assert list(payload["counts"]) == [
        "created_count",
        "updated_count",
        "unchanged_count",
    ]
    # Compact whitespace, fixed key order, exactly one trailing newline.
    assert response.text == (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    )

    expected = (
        '{"dataset":"raw_a","version":1,"entries":['
        '{"field":"a1","action":"created"},'
        '{"field":"a2","action":"created"}],'
        '"counts":{"created_count":2,"updated_count":0,"unchanged_count":0}}\n'
    )
    assert response.text == expected


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def test_unknown_dataset_returns_404(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.post(REPAIR_PATH.format(dataset="ghost", version=1))
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert set(response.json()) == {"error", "detail"}


def test_unknown_version_returns_404(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.post(REPAIR_PATH.format(dataset="raw_a", version=99))
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert set(response.json()) == {"error", "detail"}


def test_request_body_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.post(
        REPAIR_PATH.format(dataset="raw_a", version=1),
        content=b"{}",
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert set(body) == {"error", "detail"}


def test_whitespace_only_body_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    for raw in (b" ", b"   ", b" \t\n"):
        response = client.post(
            REPAIR_PATH.format(dataset="raw_a", version=1),
            content=raw,
        )
        assert response.status_code == 422, raw


def test_query_parameters_return_422(client: TestClient) -> None:
    setup_branching_graph(client)
    for params in ({"x": "1"}, {"field": "a1"}, {"field": "a1", "x": "1"}):
        response = client.post(
            REPAIR_PATH.format(dataset="raw_a", version=1), params=params
        )
        assert response.status_code == 422, params
        assert response.json()["error"] == "validation_error"


def test_404_takes_precedence_over_422(client: TestClient) -> None:
    setup_branching_graph(client)

    # Unknown dataset beats body bytes and query parameters.
    response = client.post(
        REPAIR_PATH.format(dataset="ghost", version=1),
        params={"x": "1"},
        content=b"{}",
    )
    assert response.status_code == 404
    response = client.post(
        REPAIR_PATH.format(dataset="ghost", version=1),
        content=b"  ",
    )
    assert response.status_code == 404

    # Unknown version beats body bytes and query parameters.
    response = client.post(
        REPAIR_PATH.format(dataset="raw_a", version=99),
        params={"x": "1"},
        content=b"{}",
    )
    assert response.status_code == 404


def test_rejections_write_nothing(client: TestClient, isolated_database: Path) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")
    before = cache_dump(isolated_database)

    client.post(REPAIR_PATH.format(dataset="ghost", version=1), content=b"{}")
    client.post(REPAIR_PATH.format(dataset="raw_a", version=99), content=b"  ")
    client.post(REPAIR_PATH.format(dataset="raw_a", version=1), params={"x": "1"})
    client.post(REPAIR_PATH.format(dataset="raw_a", version=1), content=b" \t\n")

    assert cache_dump(isolated_database) == before
    # The cache is still exactly as the rejections found it.
    assert audit_statuses(client, "raw_a", 1) == {"a1": "cached", "a2": "missing"}


def test_only_post_is_accepted(client: TestClient) -> None:
    setup_branching_graph(client)
    url = REPAIR_PATH.format(dataset="raw_a", version=1)
    for method in ("get", "put", "delete", "patch"):
        response = client.request(method, url)
        assert response.status_code == 405, method


# --------------------------------------------------------------------------- #
# Concurrency: a single repair may be in flight per version
# --------------------------------------------------------------------------- #


def test_concurrent_repair_has_single_winner(
    client: TestClient,
    isolated_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_branching_graph(client)
    before = cache_dump(isolated_database)

    # Hold the winner inside its repair (after the lock is taken) so the
    # concurrent attempt deterministically overlaps with it.
    entered = threading.Event()
    release = threading.Event()
    real_edges = repository._lineage_forward_edges

    def blocking_edges(conn):
        entered.set()
        assert release.wait(5)
        return real_edges(conn)

    monkeypatch.setattr(repository, "_lineage_forward_edges", blocking_edges)

    winner: dict[str, object] = {}

    def run_winner() -> None:
        thread_client = TestClient(client.app)
        response = thread_client.post(REPAIR_PATH.format(dataset="raw_a", version=1))
        winner["status"] = response.status_code
        winner["body"] = response.json()

    thread = threading.Thread(target=run_winner)
    thread.start()
    try:
        assert entered.wait(5)

        # The overlapping attempt is refused and changes nothing.
        response = client.post(REPAIR_PATH.format(dataset="raw_a", version=1))
        assert response.status_code == 409
        assert response.json()["error"] == "conflict"
        assert set(response.json()) == {"error", "detail"}
        assert cache_dump(isolated_database) == before
    finally:
        release.set()
    thread.join()

    assert winner["status"] == 200
    assert winner["body"]["counts"] == {
        "created_count": 2,
        "updated_count": 0,
        "unchanged_count": 0,
    }

    # The winner's repair committed exactly once and is fully visible.
    assert audit_statuses(client, "raw_a", 1) == {"a1": "cached", "a2": "cached"}

    # A later, sequential repair proceeds normally.
    status_code, payload, _ = repair(client, "raw_a", 1)
    assert status_code == 200
    assert actions(payload) == {"a1": "unchanged", "a2": "unchanged"}


def test_repairs_of_different_versions_do_not_conflict(client: TestClient) -> None:
    make_dataset(client, "raw_a", ["a1"])
    v2 = add_version(client, "raw_a", ["a1", "a2"])

    lock = repository._impact_cache_repair_lock("raw_a", 1)
    assert lock.acquire(blocking=False)
    try:
        # Another version's repair is unaffected by the in-flight one.
        status_code, payload, _ = repair(client, "raw_a", v2)
        assert status_code == 200
        assert actions(payload) == {"a1": "created", "a2": "created"}
    finally:
        lock.release()
