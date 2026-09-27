"""Tests for the read-only lineage impact cache invalidation trail."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

TRAIL_PATH = (
    "/datasets/{dataset}/versions/{version}/lineage/impact/cache-invalidations"
)


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


def impact(
    client: TestClient, dataset: str, version: int, field: str
) -> list[dict]:
    response = client.get(
        f"/datasets/{dataset}/versions/{version}/lineage/impact",
        params={"field": field},
    )
    assert response.status_code == 200, response.text
    return response.json()["impacted"]


def trail(
    client: TestClient, dataset: str, version: int
) -> tuple[int, dict, str]:
    response = client.get(TRAIL_PATH.format(dataset=dataset, version=version))
    return response.status_code, response.json(), response.text


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


def invalidation_dump(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT dataset, version, sequence, cause, field, created_at "
            "FROM lineage_impact_cache_invalidations "
            "ORDER BY dataset, version, sequence"
        ).fetchall()
    finally:
        conn.close()
    return [tuple(row) for row in rows]


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
# Trail writing on registration and deletion
# --------------------------------------------------------------------------- #


def test_empty_trail_returns_an_empty_entry_list(client: TestClient) -> None:
    setup_branching_graph(client)
    status_code, payload, _ = trail(client, "raw_a", 1)
    assert status_code == 200
    assert payload == {"dataset": "raw_a", "version": 1, "entries": []}


def test_registration_trails_each_invalidated_cached_field(
    client: TestClient,
) -> None:
    setup_branching_graph(client)
    make_dataset(client, "rep_d", ["d1"])
    impact(client, "raw_a", 1, "a1")
    impact(client, "mart_c", 1, "c1")

    # The new edge invalidates c1 (its source) and b1, b2, a1 (upstream);
    # only a1 and c1 had cache records, so exactly those leave a record,
    # each under its own version.
    add_link(client, ("mart_c", 1, "c1"), ("rep_d", 1, "d1"))

    status_code, raw_payload, _ = trail(client, "raw_a", 1)
    assert status_code == 200
    assert [
        {key: entry[key] for key in ("sequence", "cause", "field")}
        for entry in raw_payload["entries"]
    ] == [{"sequence": 1, "cause": "registered", "field": "a1"}]

    _, mart_payload, _ = trail(client, "mart_c", 1)
    assert [
        {key: entry[key] for key in ("sequence", "cause", "field")}
        for entry in mart_payload["entries"]
    ] == [{"sequence": 1, "cause": "registered", "field": "c1"}]

    # mid_b's fields were invalidated too, but none had a cache record.
    _, mid_payload, _ = trail(client, "mid_b", 1)
    assert mid_payload["entries"] == []


def test_uncached_fields_leave_no_trail(client: TestClient) -> None:
    setup_branching_graph(client)
    make_dataset(client, "rep_d", ["d1"])
    # Nothing was ever cached: the invalidation drops nothing and trails
    # nothing.
    add_link(client, ("mart_c", 1, "c1"), ("rep_d", 1, "d1"))
    for dataset in ("raw_a", "mid_b", "mart_c"):
        _, payload, _ = trail(client, dataset, 1)
        assert payload["entries"] == []


def test_deletion_trails_deleted_records_and_continues_the_sequence(
    client: TestClient,
) -> None:
    setup_branching_graph(client)
    make_dataset(client, "rep_d", ["d1"])
    impact(client, "raw_a", 1, "a1")
    add_link(client, ("mart_c", 1, "c1"), ("rep_d", 1, "d1"))

    # Re-cache a1 against the extended graph, then delete a mapping whose
    # source (b1) is reachable from a1: a1 leaves a second record.
    impact(client, "raw_a", 1, "a1")
    delete_link(client, ("mid_b", 1, "b1"), ("mart_c", 1, "c1"))

    _, payload, _ = trail(client, "raw_a", 1)
    assert [
        {key: entry[key] for key in ("sequence", "cause", "field")}
        for entry in payload["entries"]
    ] == [
        {"sequence": 1, "cause": "registered", "field": "a1"},
        {"sequence": 2, "cause": "deleted", "field": "a1"},
    ]


def test_repeated_invalidation_of_one_field_writes_one_record_each(
    client: TestClient,
) -> None:
    make_dataset(client, "one", ["x"])
    make_dataset(client, "two", ["y"])
    make_dataset(client, "three", ["z1", "z2"])
    add_link(client, ("one", 1, "x"), ("two", 1, "y"))

    for target_field in ("z1", "z2"):
        impact(client, "one", 1, "x")
        add_link(client, ("two", 1, "y"), ("three", 1, target_field))

    _, payload, _ = trail(client, "one", 1)
    assert [
        {key: entry[key] for key in ("sequence", "cause", "field")}
        for entry in payload["entries"]
    ] == [
        {"sequence": 1, "cause": "registered", "field": "x"},
        {"sequence": 2, "cause": "registered", "field": "x"},
    ]


def test_schema_version_creation_invalidates_without_a_trail(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")
    assert cache_dump(isolated_database) != {}

    add_version(client, "raw_a", ["a1", "a2", "a3"])

    # The cache entries were dropped as before, but the trail only covers
    # mapping registration and deletion.
    assert cache_dump(isolated_database) == {}
    _, payload, _ = trail(client, "raw_a", 1)
    assert payload["entries"] == []


def test_failed_registration_and_deletion_write_no_trail(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")
    before = invalidation_dump(isolated_database)
    assert before == []

    # Duplicate registration is a 409.
    response = client.post(
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
    assert response.status_code == 409

    # Deleting a mapping that was never registered is a 404.
    response = client.request(
        "DELETE",
        "/datasets/mid_b/versions/1/lineage",
        json={
            "target_dataset": "mid_b",
            "target_version": 1,
            "target_field": "b2",
            "source_dataset": "mart_c",
            "source_version": 1,
            "source_field": "c1",
        },
    )
    assert response.status_code == 404

    # A semantically invalid registration (same dataset) is a 422.
    response = client.post(
        "/datasets/raw_a/versions/1/lineage",
        json={
            "target_dataset": "raw_a",
            "target_version": 1,
            "target_field": "a2",
            "source_dataset": "raw_a",
            "source_version": 1,
            "source_field": "a1",
        },
    )
    assert response.status_code == 422

    assert invalidation_dump(isolated_database) == before
    # The rejected writes did not invalidate the cache either.
    assert ("raw_a", 1, "a1") in cache_dump(isolated_database)


# --------------------------------------------------------------------------- #
# Deterministic serialization
# --------------------------------------------------------------------------- #


def test_response_body_is_deterministic_compact_json_with_newline(
    client: TestClient,
) -> None:
    setup_branching_graph(client)
    make_dataset(client, "rep_d", ["d1"])
    impact(client, "raw_a", 1, "a1")
    add_link(client, ("mart_c", 1, "c1"), ("rep_d", 1, "d1"))

    response = client.get(TRAIL_PATH.format(dataset="raw_a", version=1))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.text.endswith("}\n")
    assert not response.text.endswith("}\n\n")

    payload = response.json()
    assert list(payload) == ["dataset", "version", "entries"]
    assert len(payload["entries"]) == 1
    entry = payload["entries"][0]
    assert list(entry) == ["sequence", "cause", "field", "created_at"]
    assert entry["sequence"] == 1
    assert entry["cause"] == "registered"
    assert entry["field"] == "a1"
    # The write time carries a timezone.
    parsed = datetime.fromisoformat(entry["created_at"])
    assert parsed.tzinfo is not None

    # Compact whitespace, fixed key order, exactly one trailing newline.
    assert response.text == (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    )

    # Repeated reads are byte-identical.
    assert (
        client.get(TRAIL_PATH.format(dataset="raw_a", version=1)).text
        == response.text
    )


def test_entries_sort_by_sequence_not_by_database_order(
    client: TestClient,
) -> None:
    make_dataset(client, "one", ["x"])
    make_dataset(client, "two", ["y"])
    make_dataset(client, "three", ["z1", "z2", "z3"])
    add_link(client, ("one", 1, "x"), ("two", 1, "y"))
    for target_field in ("z1", "z2", "z3"):
        impact(client, "one", 1, "x")
        add_link(client, ("two", 1, "y"), ("three", 1, target_field))

    _, payload, _ = trail(client, "one", 1)
    assert [entry["sequence"] for entry in payload["entries"]] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# Read-only behavior and persistence
# --------------------------------------------------------------------------- #


def test_trail_read_writes_invalidates_or_repairs_nothing(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    make_dataset(client, "rep_d", ["d1"])
    impact(client, "raw_a", 1, "a1")
    add_link(client, ("mart_c", 1, "c1"), ("rep_d", 1, "d1"))
    impact(client, "mid_b", 1, "b1")
    cache_before = cache_dump(isolated_database)
    trail_before = invalidation_dump(isolated_database)
    assert trail_before

    for _ in range(2):
        assert trail(client, "raw_a", 1)[0] == 200
    assert trail(client, "mid_b", 1)[0] == 200

    assert cache_dump(isolated_database) == cache_before
    assert invalidation_dump(isolated_database) == trail_before


def test_trail_survives_a_restart(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    make_dataset(client, "rep_d", ["d1"])
    impact(client, "raw_a", 1, "a1")
    add_link(client, ("mart_c", 1, "c1"), ("rep_d", 1, "d1"))
    impact(client, "raw_a", 1, "a1")
    delete_link(client, ("mid_b", 1, "b1"), ("mart_c", 1, "c1"))

    _, _, text_before = trail(client, "raw_a", 1)
    rows_before = invalidation_dump(isolated_database)

    # A fresh client over the same database file sees exactly the same
    # records, sequences and content.
    restarted = TestClient(client.app)
    _, payload, text_after = trail(restarted, "raw_a", 1)
    assert text_after == text_before
    assert [entry["sequence"] for entry in payload["entries"]] == [1, 2]
    assert invalidation_dump(isolated_database) == rows_before


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def test_unknown_dataset_returns_404(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(TRAIL_PATH.format(dataset="ghost", version=1))
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert set(response.json()) == {"error", "detail"}


def test_unknown_version_returns_404(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(TRAIL_PATH.format(dataset="raw_a", version=99))
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert set(response.json()) == {"error", "detail"}


def test_request_body_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.request(
        "GET",
        TRAIL_PATH.format(dataset="raw_a", version=1),
        content=b"{}",
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert set(body) == {"error", "detail"}


def test_whitespace_only_body_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    for raw in (b" ", b"   ", b" \t\n"):
        response = client.request(
            "GET",
            TRAIL_PATH.format(dataset="raw_a", version=1),
            content=raw,
        )
        assert response.status_code == 422, raw


def test_query_parameters_return_422(client: TestClient) -> None:
    setup_branching_graph(client)
    for params in ({"x": "1"}, {"field": "a1"}, {"cause": "registered"}):
        response = client.get(
            TRAIL_PATH.format(dataset="raw_a", version=1), params=params
        )
        assert response.status_code == 422, params
        assert response.json()["error"] == "validation_error"


def test_404_takes_precedence_over_422(client: TestClient) -> None:
    setup_branching_graph(client)

    # Unknown dataset beats body bytes and query parameters.
    response = client.request(
        "GET",
        TRAIL_PATH.format(dataset="ghost", version=1),
        params={"x": "1"},
        content=b"{}",
    )
    assert response.status_code == 404
    response = client.request(
        "GET",
        TRAIL_PATH.format(dataset="ghost", version=1),
        content=b"  ",
    )
    assert response.status_code == 404

    # Unknown version beats body bytes and query parameters.
    response = client.request(
        "GET",
        TRAIL_PATH.format(dataset="raw_a", version=99),
        params={"x": "1"},
        content=b"{}",
    )
    assert response.status_code == 404


def test_rejections_write_nothing(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    make_dataset(client, "rep_d", ["d1"])
    impact(client, "raw_a", 1, "a1")
    add_link(client, ("mart_c", 1, "c1"), ("rep_d", 1, "d1"))
    cache_before = cache_dump(isolated_database)
    trail_before = invalidation_dump(isolated_database)

    client.request(
        "GET", TRAIL_PATH.format(dataset="ghost", version=1), content=b"{}"
    )
    client.request(
        "GET", TRAIL_PATH.format(dataset="raw_a", version=99), content=b"  "
    )
    client.get(TRAIL_PATH.format(dataset="raw_a", version=1), params={"x": "1"})
    client.request(
        "GET",
        TRAIL_PATH.format(dataset="raw_a", version=1),
        content=b" \t\n",
    )

    assert cache_dump(isolated_database) == cache_before
    assert invalidation_dump(isolated_database) == trail_before
    # A valid read still succeeds and still returns the same records.
    status_code, payload, _ = trail(client, "raw_a", 1)
    assert status_code == 200
    assert [entry["sequence"] for entry in payload["entries"]] == [1]


def test_only_get_is_accepted(client: TestClient) -> None:
    setup_branching_graph(client)
    url = TRAIL_PATH.format(dataset="raw_a", version=1)
    for method in ("post", "put", "delete", "patch"):
        response = client.request(method, url, content=b"{}")
        assert response.status_code == 405, method


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_registrations_and_deletions_keep_sequences_continuous(
    client: TestClient,
) -> None:
    # Each worker thread uses its own TestClient (its own event-loop portal
    # and database connection), exercising the write serialization of
    # registration and deletion.
    count = 8
    make_dataset(client, "src_ds", [f"f{i}" for i in range(count)])
    make_dataset(client, "mid_ds", [f"m{i}" for i in range(count)])
    make_dataset(client, "dst_ds", ["out"])
    for i in range(count):
        add_link(client, ("src_ds", 1, f"f{i}"), ("mid_ds", 1, f"m{i}"))
        impact(client, "src_ds", 1, f"f{i}")

    failures: list[Exception] = []

    def run_all(action) -> None:
        barrier = threading.Barrier(count)

        def worker(index: int) -> None:
            local = TestClient(client.app)
            barrier.wait()
            try:
                action(local, index)
            except Exception as exc:  # pragma: no cover - reported below
                failures.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def register(local: TestClient, index: int) -> None:
        response = local.post(
            "/datasets/dst_ds/versions/1/lineage",
            json={
                "target_dataset": "dst_ds",
                "target_version": 1,
                "target_field": "out",
                "source_dataset": "mid_ds",
                "source_version": 1,
                "source_field": f"m{index}",
            },
        )
        assert response.status_code == 201, response.text

    run_all(register)
    assert not failures, failures

    # Each registration invalidated exactly one cached field (its upstream
    # f_i); the sequences of the version run from 1 without gaps or repeats.
    _, payload, _ = trail(client, "src_ds", 1)
    entries = payload["entries"]
    assert [entry["sequence"] for entry in entries] == list(range(1, count + 1))
    assert sorted(entry["field"] for entry in entries) == [
        f"f{i}" for i in range(count)
    ]
    assert all(entry["cause"] == "registered" for entry in entries)

    # Re-cache every field, then delete the same mappings concurrently: the
    # sequences continue where the registrations left off.
    for i in range(count):
        impact(client, "src_ds", 1, f"f{i}")

    def delete(local: TestClient, index: int) -> None:
        response = local.request(
            "DELETE",
            "/datasets/dst_ds/versions/1/lineage",
            json={
                "target_dataset": "dst_ds",
                "target_version": 1,
                "target_field": "out",
                "source_dataset": "mid_ds",
                "source_version": 1,
                "source_field": f"m{index}",
            },
        )
        assert response.status_code == 200, response.text

    run_all(delete)
    assert not failures, failures

    _, payload, _ = trail(client, "src_ds", 1)
    entries = payload["entries"]
    assert [entry["sequence"] for entry in entries] == list(
        range(1, 2 * count + 1)
    )
    deleted = entries[count:]
    assert sorted(entry["field"] for entry in deleted) == [
        f"f{i}" for i in range(count)
    ]
    assert all(entry["cause"] == "deleted" for entry in deleted)
