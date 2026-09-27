"""Tests for the read-only lineage impact cache invalidation trail."""

from __future__ import annotations

import json
import sqlite3
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


def entries(client: TestClient, dataset: str, version: int) -> list[dict]:
    status_code, payload, _ = trail(client, dataset, version)
    assert status_code == 200
    return payload["entries"]


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


def trail_dump(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT source_dataset, source_version, sequence, cause, field, "
            "created_at FROM lineage_impact_cache_invalidations "
            "ORDER BY source_dataset, source_version, sequence"
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
# Trace semantics
# --------------------------------------------------------------------------- #


def test_no_traces_before_any_invalidation(client: TestClient) -> None:
    setup_branching_graph(client)
    status_code, payload, _ = trail(client, "raw_a", 1)
    assert status_code == 200
    assert payload == {"dataset": "raw_a", "version": 1, "entries": []}


def test_registration_without_cache_records_leaves_no_trace(
    client: TestClient,
) -> None:
    make_dataset(client, "raw_a", ["a1"])
    make_dataset(client, "mid_b", ["b1"])
    add_link(client, ("raw_a", 1, "a1"), ("mid_b", 1, "b1"))
    assert entries(client, "raw_a", 1) == []
    assert entries(client, "mid_b", 1) == []


def test_registration_traces_every_invalidated_cached_field(
    client: TestClient,
) -> None:
    setup_branching_graph(client)
    make_dataset(client, "rep_d", ["d1"])
    impact(client, "raw_a", 1, "a1")
    impact(client, "mart_c", 1, "c1")

    # The new edge invalidates c1 (its source) and b1/b2/a1 (upstream); only
    # a1 and c1 had cache records, so exactly those two are traced, each
    # under its own dataset and version.
    add_link(client, ("mart_c", 1, "c1"), ("rep_d", 1, "d1"))

    raw_entries = entries(client, "raw_a", 1)
    assert [(e["sequence"], e["cause"], e["field"]) for e in raw_entries] == [
        (1, "registered", "a1")
    ]
    mart_entries = entries(client, "mart_c", 1)
    assert [(e["sequence"], e["cause"], e["field"]) for e in mart_entries] == [
        (1, "registered", "c1")
    ]
    # mid_b's fields were invalidated too but had no cache records.
    assert entries(client, "mid_b", 1) == []
    assert entries(client, "rep_d", 1) == []


def test_deletion_traces_with_deleted_cause(client: TestClient) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")
    impact(client, "mid_b", 1, "b1")

    delete_link(client, ("mid_b", 1, "b1"), ("mart_c", 1, "c1"))

    assert [
        (e["sequence"], e["cause"], e["field"])
        for e in entries(client, "raw_a", 1)
    ] == [(1, "deleted", "a1")]
    assert [
        (e["sequence"], e["cause"], e["field"])
        for e in entries(client, "mid_b", 1)
    ] == [(1, "deleted", "b1")]


def test_sequence_continues_per_version_across_causes(
    client: TestClient,
) -> None:
    make_dataset(client, "raw_a", ["a1"])
    make_dataset(client, "mid_b", ["b1"])
    add_link(client, ("raw_a", 1, "a1"), ("mid_b", 1, "b1"))

    # Each successful change invalidates the cached a1 and appends one trace;
    # the sequence continues the version's run across causes.
    impact(client, "raw_a", 1, "a1")
    delete_link(client, ("raw_a", 1, "a1"), ("mid_b", 1, "b1"))
    impact(client, "raw_a", 1, "a1")
    add_link(client, ("raw_a", 1, "a1"), ("mid_b", 1, "b1"))

    got = [
        (e["sequence"], e["cause"], e["field"])
        for e in entries(client, "raw_a", 1)
    ]
    assert got == [
        (1, "deleted", "a1"),
        (2, "registered", "a1"),
    ]


def test_same_field_is_traced_once_per_invalidation(client: TestClient) -> None:
    make_dataset(client, "raw_a", ["a1"])
    make_dataset(client, "mid_b", ["b1"])
    make_dataset(client, "mart_c", ["c1"])
    add_link(client, ("raw_a", 1, "a1"), ("mid_b", 1, "b1"))

    impact(client, "raw_a", 1, "a1")
    add_link(client, ("mid_b", 1, "b1"), ("mart_c", 1, "c1"))
    impact(client, "raw_a", 1, "a1")
    delete_link(client, ("mid_b", 1, "b1"), ("mart_c", 1, "c1"))

    got = [
        (e["sequence"], e["cause"], e["field"])
        for e in entries(client, "raw_a", 1)
    ]
    assert got == [(1, "registered", "a1"), (2, "deleted", "a1")]


def test_schema_version_creation_is_not_traced(client: TestClient) -> None:
    make_dataset(client, "raw_a", ["a1"])
    make_dataset(client, "mid_b", ["b1"])
    add_link(client, ("raw_a", 1, "a1"), ("mid_b", 1, "b1"))
    impact(client, "raw_a", 1, "a1")

    # Creating a schema version invalidates the dataset's cache records but
    # is not one of the two traced causes.
    add_version(client, "raw_a", ["a1", "a2"])
    add_version(client, "mid_b", ["b1", "b2"])

    assert entries(client, "raw_a", 1) == []
    assert entries(client, "mid_b", 1) == []
    assert entries(client, "raw_a", 2) == []


def test_failed_registration_and_deletion_leave_no_trace(
    client: TestClient,
) -> None:
    make_dataset(client, "raw_a", ["a1"])
    make_dataset(client, "mid_b", ["b1"])
    make_dataset(client, "mart_c", ["c1", "c2"])
    add_link(client, ("raw_a", 1, "a1"), ("mid_b", 1, "b1"))
    add_link(client, ("mid_b", 1, "b1"), ("mart_c", 1, "c1"))
    impact(client, "raw_a", 1, "a1")

    # Duplicate registration conflicts; deleting a mapping that was never
    # registered is a 404. Neither writes a trace: the failed delete's
    # invalidation (its source b1 reaches the cached a1) rolls back with the
    # transaction.
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
    missing = client.request(
        "DELETE",
        "/datasets/mart_c/versions/1/lineage",
        json={
            "target_dataset": "mart_c",
            "target_version": 1,
            "target_field": "c2",
            "source_dataset": "mid_b",
            "source_version": 1,
            "source_field": "b1",
        },
    )
    assert missing.status_code == 404

    assert entries(client, "raw_a", 1) == []
    assert entries(client, "mid_b", 1) == []
    assert entries(client, "mart_c", 1) == []
    # The cached record itself survived both rejections.
    assert impact(client, "raw_a", 1, "a1") == [
        {"dataset": "mart_c", "version": 1, "field": "c1"},
        {"dataset": "mid_b", "version": 1, "field": "b1"},
    ]


def test_traces_are_scoped_to_their_own_version(client: TestClient) -> None:
    make_dataset(client, "raw_a", ["a1"])
    make_dataset(client, "mid_b", ["b1"])
    add_version(client, "raw_a", ["a1", "a2"])
    add_link(client, ("raw_a", 1, "a1"), ("mid_b", 1, "b1"))
    impact(client, "raw_a", 1, "a1")

    make_dataset(client, "mart_c", ["c1"])
    add_link(client, ("mid_b", 1, "b1"), ("mart_c", 1, "c1"))

    assert [e["field"] for e in entries(client, "raw_a", 1)] == ["a1"]
    assert entries(client, "raw_a", 2) == []


# --------------------------------------------------------------------------- #
# Entry shape, ordering and deterministic serialization
# --------------------------------------------------------------------------- #


def test_entry_shape_and_created_at_timezone(client: TestClient) -> None:
    make_dataset(client, "raw_a", ["a1"])
    make_dataset(client, "mid_b", ["b1"])
    add_link(client, ("raw_a", 1, "a1"), ("mid_b", 1, "b1"))
    impact(client, "raw_a", 1, "a1")
    make_dataset(client, "mart_c", ["c1"])
    add_link(client, ("mid_b", 1, "b1"), ("mart_c", 1, "c1"))

    (entry,) = entries(client, "raw_a", 1)
    assert list(entry) == ["sequence", "cause", "field", "created_at"]
    assert entry["sequence"] == 1
    assert entry["cause"] == "registered"
    assert entry["field"] == "a1"
    # Timezone-bearing write timestamp.
    assert entry["created_at"].endswith("+00:00")


def test_response_body_is_deterministic_compact_json_with_newline(
    client: TestClient,
) -> None:
    make_dataset(client, "raw_a", ["a1"])
    make_dataset(client, "mid_b", ["b1"])
    add_link(client, ("raw_a", 1, "a1"), ("mid_b", 1, "b1"))
    impact(client, "raw_a", 1, "a1")
    make_dataset(client, "mart_c", ["c1"])
    add_link(client, ("mid_b", 1, "b1"), ("mart_c", 1, "c1"))

    response = client.get(TRAIL_PATH.format(dataset="raw_a", version=1))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.text.endswith("}\n")
    assert not response.text.endswith("}\n\n")

    payload = response.json()
    assert list(payload) == ["dataset", "version", "entries"]
    assert response.text == (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    )

    # Repeated reads are byte-identical.
    assert (
        client.get(TRAIL_PATH.format(dataset="raw_a", version=1)).text
        == response.text
    )


def test_empty_trail_serialization(client: TestClient) -> None:
    make_dataset(client, "raw_a", ["a1"])
    response = client.get(TRAIL_PATH.format(dataset="raw_a", version=1))
    assert response.status_code == 200
    assert response.text == '{"dataset":"raw_a","version":1,"entries":[]}\n'


# --------------------------------------------------------------------------- #
# Read-only behavior and persistence
# --------------------------------------------------------------------------- #


def test_listing_writes_invalidates_or_repairs_nothing(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client, "raw_a", ["a1"])
    make_dataset(client, "mid_b", ["b1"])
    add_link(client, ("raw_a", 1, "a1"), ("mid_b", 1, "b1"))
    impact(client, "raw_a", 1, "a1")
    make_dataset(client, "mart_c", ["c1"])
    add_link(client, ("mid_b", 1, "b1"), ("mart_c", 1, "c1"))

    cache_before = cache_dump(isolated_database)
    trail_before = trail_dump(isolated_database)
    assert trail_before  # the registration above left traces

    for _ in range(2):
        status_code, _, _ = trail(client, "raw_a", 1)
        assert status_code == 200
    assert trail(client, "mid_b", 1)[0] == 200

    assert cache_dump(isolated_database) == cache_before
    assert trail_dump(isolated_database) == trail_before


def test_traces_survive_restart_and_stay_append_only(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client, "raw_a", ["a1"])
    make_dataset(client, "mid_b", ["b1"])
    add_link(client, ("raw_a", 1, "a1"), ("mid_b", 1, "b1"))
    impact(client, "raw_a", 1, "a1")
    make_dataset(client, "mart_c", ["c1"])
    add_link(client, ("mid_b", 1, "b1"), ("mart_c", 1, "c1"))

    before = entries(client, "raw_a", 1)
    assert len(before) == 1

    # A fresh client over the same database file (a restart) sees the exact
    # same traces, sequences and content.
    restarted = TestClient(client.app)
    assert entries(restarted, "raw_a", 1) == before

    # The database itself refuses updates and deletes of the trail.
    conn = sqlite3.connect(isolated_database)
    try:
        for statement in (
            "UPDATE lineage_impact_cache_invalidations SET field = 'x'",
            "DELETE FROM lineage_impact_cache_invalidations",
        ):
            try:
                conn.execute(statement)
                conn.commit()
                raise AssertionError("trail mutation was not refused")
            except sqlite3.IntegrityError:
                conn.rollback()
    finally:
        conn.close()
    assert entries(client, "raw_a", 1) == before


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
    impact(client, "raw_a", 1, "a1")
    make_dataset(client, "rep_d", ["d1"])
    add_link(client, ("mart_c", 1, "c1"), ("rep_d", 1, "d1"))
    cache_before = cache_dump(isolated_database)
    trail_before = trail_dump(isolated_database)

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
    assert trail_dump(isolated_database) == trail_before
    # A valid read still succeeds and reports the same entries.
    assert [e["field"] for e in entries(client, "raw_a", 1)] == ["a1"]


def test_only_get_is_accepted(client: TestClient) -> None:
    setup_branching_graph(client)
    url = TRAIL_PATH.format(dataset="raw_a", version=1)
    for method in ("post", "put", "delete", "patch"):
        response = client.request(method, url, content=b"{}")
        assert response.status_code == 405, method
