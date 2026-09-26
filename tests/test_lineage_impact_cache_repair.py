"""Tests for the controlled lineage impact cache repair endpoint."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from fastapi.testclient import TestClient

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


def repair(
    client: TestClient, dataset: str, version: int
) -> tuple[int, dict, str]:
    response = client.post(REPAIR_PATH.format(dataset=dataset, version=version))
    return response.status_code, response.json(), response.text


def audit_statuses(client: TestClient, dataset: str, version: int) -> dict[str, str]:
    response = client.get(AUDIT_PATH.format(dataset=dataset, version=version))
    assert response.status_code == 200, response.text
    return {entry["field"]: entry["status"] for entry in response.json()["entries"]}


def ref(dataset: str, version: int, field: str) -> dict:
    return {"dataset": dataset, "version": version, "field": field}


def cache_dump(db_path: Path) -> dict[tuple[str, int, str], list]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT source_dataset, source_version, source_field, impacted "
            "FROM lineage_impact_cache"
        ).fetchall()
    finally:
        conn.close()
    return {
        (dataset, version, field): json.loads(impacted)
        for dataset, version, field, impacted in rows
    }


def cache_row_ids(db_path: Path) -> dict[tuple[str, int, str], int]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, source_dataset, source_version, source_field "
            "FROM lineage_impact_cache"
        ).fetchall()
    finally:
        conn.close()
    return {(dataset, version, field): row_id for row_id, dataset, version, field in rows}


def actions(payload: dict) -> dict[str, str]:
    return {entry["field"]: entry["action"] for entry in payload["entries"]}


def corrupt_cache(
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

    # Every field of the version now audits as a hit.
    assert audit_statuses(client, "raw_a", 1) == {"a1": "cached", "a2": "cached"}


def test_repair_rewrites_stale_records(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")
    corrupt_cache(isolated_database, "raw_a", 1, "a1", [ref("ghost", 7, "g1")])

    status_code, payload, _ = repair(client, "raw_a", 1)
    assert status_code == 200
    assert actions(payload) == {"a1": "updated", "a2": "created"}
    assert payload["counts"] == {
        "created_count": 1,
        "updated_count": 1,
        "unchanged_count": 0,
    }

    # The stored record now equals the recomputed result.
    assert cache_dump(isolated_database)[("raw_a", 1, "a1")] == [
        ref("mart_c", 1, "c1"),
        ref("mid_b", 1, "b1"),
        ref("mid_b", 1, "b2"),
    ]
    assert audit_statuses(client, "raw_a", 1) == {"a1": "cached", "a2": "cached"}


def test_repair_leaves_matching_records_untouched(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")
    ids_before = cache_row_ids(isolated_database)
    dump_before = cache_dump(isolated_database)

    status_code, payload, _ = repair(client, "raw_a", 1)
    assert status_code == 200
    assert actions(payload) == {"a1": "unchanged", "a2": "created"}
    assert payload["counts"] == {
        "created_count": 1,
        "updated_count": 0,
        "unchanged_count": 1,
    }

    # The matching row kept its identity (no delete/reinsert) and its bytes.
    ids_after = cache_row_ids(isolated_database)
    assert ids_after[("raw_a", 1, "a1")] == ids_before[("raw_a", 1, "a1")]
    assert dump_before[("raw_a", 1, "a1")] == cache_dump(isolated_database)[
        ("raw_a", 1, "a1")
    ]


def test_second_repair_is_all_unchanged(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    repair(client, "raw_a", 1)
    ids_before = cache_row_ids(isolated_database)

    status_code, payload, _ = repair(client, "raw_a", 1)
    assert status_code == 200
    assert actions(payload) == {"a1": "unchanged", "a2": "unchanged"}
    assert payload["counts"] == {
        "created_count": 0,
        "updated_count": 0,
        "unchanged_count": 2,
    }
    assert cache_row_ids(isolated_database) == ids_before


def test_repair_uses_the_impact_query_traversal_with_cycles(
    client: TestClient,
) -> None:
    make_dataset(client, "cyc_x", ["f"])
    make_dataset(client, "cyc_y", ["f"])
    add_link(client, ("cyc_x", 1, "f"), ("cyc_y", 1, "f"))
    add_link(client, ("cyc_y", 1, "f"), ("cyc_x", 1, "f"))

    status_code, payload, _ = repair(client, "cyc_x", 1)
    assert status_code == 200
    assert actions(payload) == {"f": "created"}
    # The cycle terminates and the start field is not its own impact.
    assert impact(client, "cyc_x", 1, "f") == [ref("cyc_y", 1, "f")]
    assert audit_statuses(client, "cyc_x", 1) == {"f": "cached"}


def test_repair_is_scoped_to_the_path_version(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client, "raw_a", ["a1", "a2"])
    make_dataset(client, "other", ["o1"])
    v2 = add_version(client, "raw_a", ["a1", "a2", "a3"])
    add_link(client, ("raw_a", 1, "a1"), ("other", 1, "o1"))

    status_code, payload, _ = repair(client, "raw_a", 1)
    assert status_code == 200
    assert [entry["field"] for entry in payload["entries"]] == ["a1", "a2"]

    # Only version 1 cache rows exist; version 2 and the other dataset were
    # not repaired.
    assert set(cache_dump(isolated_database)) == {
        ("raw_a", 1, "a1"),
        ("raw_a", 1, "a2"),
    }
    assert audit_statuses(client, "raw_a", v2) == {
        "a1": "missing",
        "a2": "missing",
        "a3": "missing",
    }
    assert audit_statuses(client, "other", 1) == {"o1": "missing"}


# --------------------------------------------------------------------------- #
# Visibility of the repaired records
# --------------------------------------------------------------------------- #


def test_repaired_records_are_immediately_visible_to_impact_queries(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")
    corrupt_cache(isolated_database, "raw_a", 1, "a1", [ref("ghost", 7, "g1")])
    # The impact query answers from the stale cache record.
    assert impact(client, "raw_a", 1, "a1") == [ref("ghost", 7, "g1")]

    repair(client, "raw_a", 1)

    # The same query now answers with the repaired record, and the path
    # explanation agrees with it.
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
    assert [item["field"] for item in paths.json()["impacts"]] == [
        "c1",
        "b1",
        "b2",
    ]


def test_repair_survives_restart(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    repair(client, "raw_a", 1)

    # A fresh client (nothing cached in process memory) sees the repaired
    # records: every field audits as a hit and impact queries agree.
    fresh = TestClient(client.app)
    assert audit_statuses(fresh, "raw_a", 1) == {"a1": "cached", "a2": "cached"}
    assert impact(fresh, "raw_a", 1, "a1") == [
        ref("mart_c", 1, "c1"),
        ref("mid_b", 1, "b1"),
        ref("mid_b", 1, "b2"),
    ]


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
    impact(client, "raw_a", 1, "a1")
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
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")

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
        '{"field":"a1","action":"unchanged"},'
        '{"field":"a2","action":"created"}],'
        '"counts":{"created_count":1,"updated_count":0,"unchanged_count":1}}\n'
    )
    assert response.text == expected


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_repairs_have_a_single_winner(client: TestClient) -> None:
    setup_branching_graph(client)
    make_dataset(client, "rep_d", ["d1", "d2"])
    add_link(client, ("mart_c", 1, "c1"), ("rep_d", 1, "d1"))
    add_link(client, ("mart_c", 1, "c1"), ("rep_d", 1, "d2"))

    url = REPAIR_PATH.format(dataset="raw_a", version=1)
    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        statuses.append(thread_client.post(url).status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [200, 409], statuses
    # The winner's repair committed in full; the loser changed nothing.
    assert audit_statuses(client, "raw_a", 1) == {"a1": "cached", "a2": "cached"}


def test_concurrent_repair_conflict_writes_nothing(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")
    before = cache_dump(isolated_database)

    url = REPAIR_PATH.format(dataset="raw_a", version=1)
    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        statuses.append(thread_client.post(url).status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [200, 409], statuses
    # Exactly the winner's repair is visible: a2 was created, a1 untouched.
    after = cache_dump(isolated_database)
    assert after[("raw_a", 1, "a1")] == before[("raw_a", 1, "a1")]
    assert set(after) == {("raw_a", 1, "a1"), ("raw_a", 1, "a2")}


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


def test_rejections_write_nothing(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")
    before = cache_dump(isolated_database)

    client.post(REPAIR_PATH.format(dataset="ghost", version=1), content=b"{}")
    client.post(REPAIR_PATH.format(dataset="raw_a", version=99), content=b"  ")
    client.post(REPAIR_PATH.format(dataset="raw_a", version=1), params={"x": "1"})
    client.post(REPAIR_PATH.format(dataset="raw_a", version=1), content=b" \t\n")

    assert cache_dump(isolated_database) == before
    # A valid repair still succeeds afterwards.
    status_code, payload, _ = repair(client, "raw_a", 1)
    assert status_code == 200
    assert actions(payload) == {"a1": "unchanged", "a2": "created"}


def test_only_post_is_accepted(client: TestClient) -> None:
    setup_branching_graph(client)
    url = REPAIR_PATH.format(dataset="raw_a", version=1)
    for method in ("get", "put", "delete", "patch"):
        response = client.request(method, url)
        assert response.status_code == 405, method


def test_repair_does_not_disturb_other_reads(client: TestClient) -> None:
    setup_branching_graph(client)
    repair(client, "raw_a", 1)

    # Lineage registration, deletion and the read-only audit still behave.
    assert audit_statuses(client, "raw_a", 1) == {"a1": "cached", "a2": "cached"}
    sources = client.get(
        "/datasets/mart_c/versions/1/lineage/impact/source-paths",
        params={"field": "c1"},
    )
    assert sources.status_code == 200
    assert [item["field"] for item in sources.json()["origins"]] == [
        "b1",
        "b2",
        "a1",
    ]
