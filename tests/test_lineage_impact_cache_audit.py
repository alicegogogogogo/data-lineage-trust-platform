"""Tests for the read-only lineage impact cache consistency audit."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

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


def audit(
    client: TestClient, dataset: str, version: int
) -> tuple[int, dict, str]:
    response = client.get(AUDIT_PATH.format(dataset=dataset, version=version))
    return response.status_code, response.json(), response.text


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


def statuses(payload: dict) -> dict[str, str]:
    return {entry["field"]: entry["status"] for entry in payload["entries"]}


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
# Status semantics
# --------------------------------------------------------------------------- #


def test_every_field_is_missing_before_any_impact_query(client: TestClient) -> None:
    setup_branching_graph(client)
    status_code, payload, _ = audit(client, "raw_a", 1)
    assert status_code == 200
    assert payload["dataset"] == "raw_a"
    assert payload["version"] == 1
    assert statuses(payload) == {"a1": "missing", "a2": "missing"}
    assert payload["counts"] == {
        "cached_count": 0,
        "missing_count": 2,
        "mismatch_count": 0,
    }


def test_successful_impact_query_makes_the_field_cached(client: TestClient) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")
    # A field with no downstream is cached too; its cached result is [].
    assert impact(client, "mart_c", 1, "c1") == []

    _, raw_payload, _ = audit(client, "raw_a", 1)
    assert statuses(raw_payload) == {"a1": "cached", "a2": "missing"}
    assert raw_payload["counts"] == {
        "cached_count": 1,
        "missing_count": 1,
        "mismatch_count": 0,
    }

    _, mart_payload, _ = audit(client, "mart_c", 1)
    assert statuses(mart_payload) == {"c1": "cached"}
    assert mart_payload["counts"] == {
        "cached_count": 1,
        "missing_count": 0,
        "mismatch_count": 0,
    }


def test_added_mapping_returns_invalidated_entries_to_missing(
    client: TestClient,
) -> None:
    setup_branching_graph(client)
    make_dataset(client, "rep_d", ["d1"])
    impact(client, "raw_a", 1, "a1")
    impact(client, "mart_c", 1, "c1")
    assert statuses(audit(client, "raw_a", 1)[1]) == {
        "a1": "cached",
        "a2": "missing",
    }

    # The new edge invalidates c1 (its source) and a1 (upstream of c1).
    add_link(client, ("mart_c", 1, "c1"), ("rep_d", 1, "d1"))

    assert statuses(audit(client, "raw_a", 1)[1]) == {
        "a1": "missing",
        "a2": "missing",
    }
    assert statuses(audit(client, "mart_c", 1)[1]) == {"c1": "missing"}

    # A fresh successful impact query hits again, now against the extended graph.
    assert impact(client, "raw_a", 1, "a1") == [
        ref("mart_c", 1, "c1"),
        ref("mid_b", 1, "b1"),
        ref("mid_b", 1, "b2"),
        ref("rep_d", 1, "d1"),
    ]
    assert statuses(audit(client, "raw_a", 1)[1]) == {
        "a1": "cached",
        "a2": "missing",
    }


def test_deleted_mapping_returns_invalidated_entries_to_missing(
    client: TestClient,
) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")
    impact(client, "mid_b", 1, "b1")
    assert statuses(audit(client, "raw_a", 1)[1])["a1"] == "cached"

    delete_link(client, ("mid_b", 1, "b1"), ("mart_c", 1, "c1"))

    assert statuses(audit(client, "raw_a", 1)[1])["a1"] == "missing"
    assert statuses(audit(client, "mid_b", 1)[1])["b1"] == "missing"

    # Re-querying caches the recomputed, shrunk impact and audits as a hit.
    assert impact(client, "mid_b", 1, "b1") == []
    assert statuses(audit(client, "mid_b", 1)[1])["b1"] == "cached"


def test_stale_cache_record_is_mismatch_and_is_not_repaired(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")

    # Simulate a stale record: the stored impacted list claims a field that
    # the current graph cannot reach.
    conn = sqlite3.connect(isolated_database)
    try:
        conn.execute(
            "UPDATE lineage_impact_cache SET impacted = ? "
            "WHERE source_dataset = ? AND source_version = ? AND source_field = ?",
            (
                json.dumps([ref("ghost", 7, "g1")]),
                "raw_a",
                1,
                "a1",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    _, payload, _ = audit(client, "raw_a", 1)
    assert statuses(payload) == {"a1": "mismatch", "a2": "missing"}
    assert payload["counts"] == {
        "cached_count": 0,
        "missing_count": 1,
        "mismatch_count": 1,
    }

    # The audit never repairs, invalidates or writes: a second audit reports
    # the same status and the stored record is byte-for-byte unchanged.
    assert statuses(audit(client, "raw_a", 1)[1])["a1"] == "mismatch"
    stored = cache_dump(isolated_database)
    assert stored[("raw_a", 1, "a1")] == [ref("ghost", 7, "g1")]

    # A reordered (same-set) record is likewise a mismatch.
    conn = sqlite3.connect(isolated_database)
    try:
        conn.execute(
            "UPDATE lineage_impact_cache SET impacted = ? "
            "WHERE source_dataset = ? AND source_version = ? AND source_field = ?",
            (
                json.dumps(
                    [
                        ref("mid_b", 1, "b2"),
                        ref("mid_b", 1, "b1"),
                        ref("mart_c", 1, "c1"),
                    ]
                ),
                "raw_a",
                1,
                "a1",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    assert statuses(audit(client, "raw_a", 1)[1])["a1"] == "mismatch"


def test_cyclic_graph_audits_as_cached(client: TestClient) -> None:
    make_dataset(client, "cyc_x", ["f"])
    make_dataset(client, "cyc_y", ["f"])
    add_link(client, ("cyc_x", 1, "f"), ("cyc_y", 1, "f"))
    add_link(client, ("cyc_y", 1, "f"), ("cyc_x", 1, "f"))

    assert impact(client, "cyc_x", 1, "f") == [ref("cyc_y", 1, "f")]
    assert statuses(audit(client, "cyc_x", 1)[1]) == {"f": "cached"}


def test_audit_is_scoped_to_the_path_version(client: TestClient) -> None:
    make_dataset(client, "raw_a", ["a1", "a2"])
    make_dataset(client, "other", ["o1"])
    v2 = add_version(client, "raw_a", ["a1", "a2", "a3"])
    add_link(client, ("raw_a", 1, "a1"), ("other", 1, "o1"))

    impact(client, "raw_a", 1, "a1")
    impact(client, "other", 1, "o1")

    # Version 1 audits only its own two fields; the other-dataset cache row
    # and the not-yet-existing-at-query-time version 2 never appear.
    _, v1_payload, _ = audit(client, "raw_a", 1)
    assert [entry["field"] for entry in v1_payload["entries"]] == ["a1", "a2"]
    assert statuses(v1_payload) == {"a1": "cached", "a2": "missing"}

    # Version 2 has no cache records of its own; every field is missing.
    _, v2_payload, _ = audit(client, "raw_a", v2)
    assert [entry["field"] for entry in v2_payload["entries"]] == [
        "a1",
        "a2",
        "a3",
    ]
    assert v2_payload["counts"] == {
        "cached_count": 0,
        "missing_count": 3,
        "mismatch_count": 0,
    }


# --------------------------------------------------------------------------- #
# Ordering, counts and deterministic serialization
# --------------------------------------------------------------------------- #


def test_entries_sort_by_field_name_independent_of_insertion_order(
    client: TestClient,
) -> None:
    make_dataset(client, "zeta_ds", ["zebra", "apple", "mango"])
    impact(client, "zeta_ds", 1, "mango")

    _, payload, _ = audit(client, "zeta_ds", 1)
    assert [entry["field"] for entry in payload["entries"]] == [
        "apple",
        "mango",
        "zebra",
    ]
    assert [entry["status"] for entry in payload["entries"]] == [
        "missing",
        "cached",
        "missing",
    ]


def test_counts_equal_the_entry_totals(client: TestClient) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")
    _, payload, _ = audit(client, "raw_a", 1)
    counts = payload["counts"]
    for status in ("cached", "missing", "mismatch"):
        assert counts[f"{status}_count"] == sum(
            1 for entry in payload["entries"] if entry["status"] == status
        )
    assert sum(counts.values()) == len(payload["entries"]) == 2


def test_response_body_is_deterministic_compact_json_with_newline(
    client: TestClient,
) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")

    response = client.get(AUDIT_PATH.format(dataset="raw_a", version=1))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.text.endswith("}\n")
    assert not response.text.endswith("}\n\n")

    payload = response.json()
    assert list(payload) == ["dataset", "version", "entries", "counts"]
    assert all(list(entry) == ["field", "status"] for entry in payload["entries"])
    assert list(payload["counts"]) == [
        "cached_count",
        "missing_count",
        "mismatch_count",
    ]
    # Compact whitespace, fixed key order, exactly one trailing newline.
    assert response.text == (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    )

    expected = (
        '{"dataset":"raw_a","version":1,"entries":['
        '{"field":"a1","status":"cached"},'
        '{"field":"a2","status":"missing"}],'
        '"counts":{"cached_count":1,"missing_count":1,"mismatch_count":0}}\n'
    )
    assert response.text == expected

    # Repeated reads are byte-identical.
    assert (
        client.get(AUDIT_PATH.format(dataset="raw_a", version=1)).text
        == response.text
    )


# --------------------------------------------------------------------------- #
# Read-only behavior
# --------------------------------------------------------------------------- #


def test_audit_writes_invalidates_or_repairs_nothing(
    client: TestClient, isolated_database: Path
) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")
    impact(client, "mart_c", 1, "c1")
    before = cache_dump(isolated_database)

    for _ in range(2):
        status_code, _, _ = audit(client, "raw_a", 1)
        assert status_code == 200
    assert audit(client, "mart_c", 1)[0] == 200

    # No row was added (missing a2 stayed uncached), removed or changed.
    assert cache_dump(isolated_database) == before


def test_audit_leaves_impact_and_path_queries_unaffected(
    client: TestClient,
) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")
    audit(client, "raw_a", 1)

    # The impact query still answers from its cache unchanged.
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

    # The path companions never cache, so the audit still sees only a1/c1.
    assert statuses(audit(client, "raw_a", 1)[1]) == {
        "a1": "cached",
        "a2": "missing",
    }


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def test_unknown_dataset_returns_404(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(AUDIT_PATH.format(dataset="ghost", version=1))
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert set(response.json()) == {"error", "detail"}


def test_unknown_version_returns_404(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.get(AUDIT_PATH.format(dataset="raw_a", version=99))
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert set(response.json()) == {"error", "detail"}


def test_request_body_returns_422(client: TestClient) -> None:
    setup_branching_graph(client)
    response = client.request(
        "GET",
        AUDIT_PATH.format(dataset="raw_a", version=1),
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
            AUDIT_PATH.format(dataset="raw_a", version=1),
            content=raw,
        )
        assert response.status_code == 422, raw


def test_query_parameters_return_422(client: TestClient) -> None:
    setup_branching_graph(client)
    for params in ({"x": "1"}, {"field": "a1"}, {"field": "a1", "x": "1"}):
        response = client.get(
            AUDIT_PATH.format(dataset="raw_a", version=1), params=params
        )
        assert response.status_code == 422, params
        assert response.json()["error"] == "validation_error"


def test_404_takes_precedence_over_422(client: TestClient) -> None:
    setup_branching_graph(client)

    # Unknown dataset beats body bytes and query parameters.
    response = client.request(
        "GET",
        AUDIT_PATH.format(dataset="ghost", version=1),
        params={"x": "1"},
        content=b"{}",
    )
    assert response.status_code == 404
    response = client.request(
        "GET",
        AUDIT_PATH.format(dataset="ghost", version=1),
        content=b"  ",
    )
    assert response.status_code == 404

    # Unknown version beats body bytes and query parameters.
    response = client.request(
        "GET",
        AUDIT_PATH.format(dataset="raw_a", version=99),
        params={"x": "1"},
        content=b"{}",
    )
    assert response.status_code == 404


def test_rejections_write_nothing(client: TestClient, isolated_database: Path) -> None:
    setup_branching_graph(client)
    impact(client, "raw_a", 1, "a1")
    before = cache_dump(isolated_database)

    client.request(
        "GET", AUDIT_PATH.format(dataset="ghost", version=1), content=b"{}"
    )
    client.request(
        "GET", AUDIT_PATH.format(dataset="raw_a", version=99), content=b"  "
    )
    client.get(AUDIT_PATH.format(dataset="raw_a", version=1), params={"x": "1"})
    client.request(
        "GET",
        AUDIT_PATH.format(dataset="raw_a", version=1),
        content=b" \t\n",
    )

    assert cache_dump(isolated_database) == before
    # A valid audit still succeeds and still reports the same statuses.
    assert statuses(audit(client, "raw_a", 1)[1]) == {
        "a1": "cached",
        "a2": "missing",
    }


def test_only_get_is_accepted(client: TestClient) -> None:
    setup_branching_graph(client)
    url = AUDIT_PATH.format(dataset="raw_a", version=1)
    for method in ("post", "put", "delete", "patch"):
        response = client.request(method, url, content=b"{}")
        assert response.status_code == 405, method
