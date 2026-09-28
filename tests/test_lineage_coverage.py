"""Tests for the read-only cross-version lineage coverage check."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

COVERAGE_PATH = "/datasets/dm_orders/lineage-coverage"

TOP_LEVEL_KEYS = ["dataset", "versions", "totals"]
VERSION_KEYS = [
    "version",
    "fields",
    "linked_field_count",
    "unlinked_field_count",
    "mapping_count",
]
FIELD_KEYS = ["field", "sources", "source_dataset_count"]
SOURCE_KEYS = ["dataset", "version", "field"]
TOTAL_KEYS = [
    "version_count",
    "field_count",
    "linked_field_count",
    "unlinked_field_count",
    "mapping_count",
]


def make_dataset(client: TestClient, name: str, fields: list[dict]) -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions", json={"fields": fields}
    )
    assert response.status_code == 201, response.text


def add_version(client: TestClient, name: str, fields: list[dict]) -> int:
    response = client.post(f"/datasets/{name}/versions", json={"fields": fields})
    assert response.status_code == 201, response.text
    return response.json()["version"]


def register(
    client: TestClient,
    source: str,
    source_version: int,
    source_field: str,
    target_field: str,
    target: str = "dm_orders",
    target_version: int = 1,
) -> None:
    response = client.post(
        f"/datasets/{target}/versions/{target_version}/lineage",
        json={
            "target_dataset": target,
            "target_version": target_version,
            "target_field": target_field,
            "source_dataset": source,
            "source_version": source_version,
            "source_field": source_field,
        },
    )
    assert response.status_code == 201, response.text


def get_coverage(client: TestClient, dataset: str = "dm_orders") -> dict:
    response = client.get(f"/datasets/{dataset}/lineage-coverage")
    assert response.status_code == 200, response.text
    return response.json()


def coverage_response(client: TestClient, dataset: str = "dm_orders"):
    response = client.get(f"/datasets/{dataset}/lineage-coverage")
    assert response.status_code == 200, response.text
    return response


def setup_target(client: TestClient) -> None:
    make_dataset(
        client,
        "dm_orders",
        [
            {"name": "id", "type": "integer", "nullable": False},
            {"name": "total", "type": "decimal", "nullable": True},
            {"name": "orphan", "type": "string", "nullable": True},
        ],
    )


# --------------------------------------------------------------------------- #
# Empty state and wire format
# --------------------------------------------------------------------------- #


def test_coverage_dataset_without_versions_is_empty_not_an_error(
    client: TestClient,
) -> None:
    assert client.post("/datasets", json={"name": "dm_orders"}).status_code == 201
    body = get_coverage(client)
    assert list(body) == TOP_LEVEL_KEYS
    assert body["dataset"] == "dm_orders"
    assert body["versions"] == []
    assert body["totals"] == {
        "version_count": 0,
        "field_count": 0,
        "linked_field_count": 0,
        "unlinked_field_count": 0,
        "mapping_count": 0,
    }


def test_coverage_is_get_only(client: TestClient) -> None:
    assert client.post("/datasets", json={"name": "dm_orders"}).status_code == 201
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, COVERAGE_PATH)
        assert response.status_code == 405, method


def test_coverage_wire_format_is_compact_with_fixed_key_order_and_newline(
    client: TestClient,
) -> None:
    setup_target(client)
    make_dataset(
        client,
        "raw_orders",
        [{"name": "order_id", "type": "integer", "nullable": False}],
    )
    register(client, "raw_orders", 1, "order_id", "id")

    response = coverage_response(client)
    assert response.headers["content-type"].startswith("application/json")
    text = response.text
    # Compact whitespace: no separator spaces, and the only line break is the
    # single trailing newline.
    assert ": " not in text
    assert ", " not in text
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    assert "\n" not in text[:-1]

    # Key order at every level.
    assert list(response.json()) == TOP_LEVEL_KEYS
    positions = [text.index(f'"{key}"') for key in TOP_LEVEL_KEYS]
    assert positions == sorted(positions)
    body = response.json()
    assert list(body["totals"]) == TOTAL_KEYS
    version_entry = body["versions"][0]
    assert list(version_entry) == VERSION_KEYS
    for field_entry in version_entry["fields"]:
        assert list(field_entry) == FIELD_KEYS
        for source in field_entry["sources"]:
            assert list(source) == SOURCE_KEYS


# --------------------------------------------------------------------------- #
# Field entries, source references and ordering
# --------------------------------------------------------------------------- #


def test_coverage_lists_every_field_with_unlinked_fields_empty(
    client: TestClient,
) -> None:
    setup_target(client)
    make_dataset(
        client,
        "raw_orders",
        [{"name": "order_id", "type": "integer", "nullable": False}],
    )
    register(client, "raw_orders", 1, "order_id", "id")

    fields = get_coverage(client)["versions"][0]["fields"]
    assert [entry["field"] for entry in fields] == ["id", "orphan", "total"]
    by_name = {entry["field"]: entry for entry in fields}

    assert by_name["id"]["sources"] == [
        {"dataset": "raw_orders", "version": 1, "field": "order_id"}
    ]
    assert by_name["id"]["source_dataset_count"] == 1

    # Fields without sources are listed with an empty list and zero count.
    assert by_name["orphan"]["sources"] == []
    assert by_name["orphan"]["source_dataset_count"] == 0
    assert by_name["total"]["sources"] == []
    assert by_name["total"]["source_dataset_count"] == 0


def test_coverage_sources_sorted_by_dataset_version_field_and_deduped(
    client: TestClient,
) -> None:
    setup_target(client)
    make_dataset(
        client,
        "raw_orders",
        [
            {"name": "amount", "type": "decimal", "nullable": True},
            {"name": "gross", "type": "decimal", "nullable": True},
            {"name": "order_id", "type": "integer", "nullable": False},
        ],
    )
    add_version(
        client,
        "raw_orders",
        [
            {"name": "amount", "type": "decimal", "nullable": True},
            {"name": "order_id", "type": "integer", "nullable": False},
        ],
    )
    make_dataset(
        client,
        "raw_refunds",
        [{"name": "refund_amount", "type": "decimal", "nullable": True}],
    )

    # Registered deliberately out of (dataset, version, field) order.
    register(client, "raw_orders", 2, "amount", "total")
    register(client, "raw_refunds", 1, "refund_amount", "total")
    register(client, "raw_orders", 1, "gross", "total")
    register(client, "raw_orders", 1, "amount", "total")

    total = next(
        entry
        for entry in get_coverage(client)["versions"][0]["fields"]
        if entry["field"] == "total"
    )
    assert total["sources"] == [
        {"dataset": "raw_orders", "version": 1, "field": "amount"},
        {"dataset": "raw_orders", "version": 1, "field": "gross"},
        {"dataset": "raw_orders", "version": 2, "field": "amount"},
        {"dataset": "raw_refunds", "version": 1, "field": "refund_amount"},
    ]
    # Distinct source datasets, not mappings: raw_orders (two versions) plus
    # raw_refunds is two, while the field has four source references.
    assert total["source_dataset_count"] == 2
    assert len(total["sources"]) == 4


def test_coverage_same_dataset_over_versions_never_counts_twice(
    client: TestClient,
) -> None:
    setup_target(client)
    make_dataset(
        client,
        "raw_orders",
        [{"name": "order_id", "type": "integer", "nullable": False}],
    )
    add_version(
        client,
        "raw_orders",
        [{"name": "order_id", "type": "integer", "nullable": False}],
    )
    register(client, "raw_orders", 1, "order_id", "id")
    register(client, "raw_orders", 2, "order_id", "id")

    field = get_coverage(client)["versions"][0]["fields"][0]
    assert field["field"] == "id"
    assert len(field["sources"]) == 2
    assert field["source_dataset_count"] == 1


def test_coverage_versions_sort_ascending_and_cover_every_version(
    client: TestClient,
) -> None:
    make_dataset(
        client,
        "dm_orders",
        [{"name": "a", "type": "string", "nullable": True}],
    )
    add_version(
        client,
        "dm_orders",
        [
            {"name": "b", "type": "string", "nullable": True},
            {"name": "c", "type": "string", "nullable": True},
        ],
    )
    make_dataset(
        client,
        "raw_orders",
        [{"name": "src", "type": "string", "nullable": True}],
    )
    register(
        client,
        "raw_orders", 1, "src", "c",
        target_version=2,
    )

    body = get_coverage(client)
    assert [entry["version"] for entry in body["versions"]] == [1, 2]
    assert [f["field"] for f in body["versions"][0]["fields"]] == ["a"]
    second = body["versions"][1]
    assert [f["field"] for f in second["fields"]] == ["b", "c"]
    assert second["fields"][0]["sources"] == []
    assert second["fields"][1]["sources"] == [
        {"dataset": "raw_orders", "version": 1, "field": "src"}
    ]


# --------------------------------------------------------------------------- #
# Per-version counts and totals
# --------------------------------------------------------------------------- #


def test_coverage_version_counts_and_totals_match_the_entries(
    client: TestClient,
) -> None:
    setup_target(client)
    make_dataset(
        client,
        "raw_orders",
        [
            {"name": "amount", "type": "decimal", "nullable": True},
            {"name": "gross", "type": "decimal", "nullable": True},
            {"name": "order_id", "type": "integer", "nullable": False},
        ],
    )
    make_dataset(
        client,
        "raw_refunds",
        [{"name": "refund_amount", "type": "decimal", "nullable": True}],
    )
    register(client, "raw_orders", 1, "order_id", "id")
    register(client, "raw_orders", 1, "amount", "total")
    register(client, "raw_orders", 1, "gross", "total")
    register(client, "raw_refunds", 1, "refund_amount", "total")

    second_fields = [
        {"name": "phone", "type": "string", "nullable": True},
        {"name": "note", "type": "string", "nullable": True},
    ]
    add_version(client, "dm_orders", second_fields)
    register(
        client, "raw_orders", 1, "order_id", "phone", target_version=2
    )

    body = get_coverage(client)
    first, second = body["versions"]

    # Version 1: 'id' and 'total' linked, 'orphan' unlinked; four mappings.
    assert first["linked_field_count"] == 2
    assert first["unlinked_field_count"] == 1
    assert first["mapping_count"] == 4
    assert (
        first["linked_field_count"] + first["unlinked_field_count"]
        == len(first["fields"])
    )
    assert first["mapping_count"] == sum(
        len(f["sources"]) for f in first["fields"]
    )

    # Version 2: 'phone' linked, 'note' unlinked; one mapping.
    assert second["linked_field_count"] == 1
    assert second["unlinked_field_count"] == 1
    assert second["mapping_count"] == 1

    totals = body["totals"]
    assert totals == {
        "version_count": 2,
        "field_count": 5,
        "linked_field_count": 3,
        "unlinked_field_count": 2,
        "mapping_count": 5,
    }
    for key in (
        "linked_field_count",
        "unlinked_field_count",
        "mapping_count",
    ):
        assert totals[key] == sum(version[key] for version in body["versions"])
    assert (
        totals["linked_field_count"] + totals["unlinked_field_count"]
        == totals["field_count"]
    )
    assert totals["mapping_count"] == sum(
        len(f["sources"])
        for version in body["versions"]
        for f in version["fields"]
    )


def test_coverage_totals_are_all_zero_for_a_dataset_without_versions(
    client: TestClient,
) -> None:
    assert client.post("/datasets", json={"name": "empty"}).status_code == 201
    totals = get_coverage(client, "empty")["totals"]
    assert all(value == 0 for value in totals.values())


# --------------------------------------------------------------------------- #
# Errors: 404 / 422, no writes
# --------------------------------------------------------------------------- #


def test_coverage_unknown_dataset_returns_404(client: TestClient) -> None:
    response = client.get("/datasets/ghost/lineage-coverage")
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "not_found"


def test_coverage_rejects_body_and_query_params(client: TestClient) -> None:
    assert client.post("/datasets", json={"name": "dm_orders"}).status_code == 201
    before = coverage_response(client).text

    with_body = client.request("GET", COVERAGE_PATH, content=b"{}")
    with_whitespace = client.request("GET", COVERAGE_PATH, content=b"   ")
    with_single_space = client.request("GET", COVERAGE_PATH, content=b" ")
    with_query = client.get(COVERAGE_PATH, params={"limit": 1})
    assert with_body.status_code == 422
    assert with_whitespace.status_code == 422
    assert with_single_space.status_code == 422
    assert with_query.status_code == 422
    for response in (
        with_body,
        with_whitespace,
        with_single_space,
        with_query,
    ):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]

    # The rejections wrote nothing.
    assert coverage_response(client).text == before


def test_coverage_shape_errors_keep_404_precedence(client: TestClient) -> None:
    assert client.get(
        "/datasets/ghost/lineage-coverage", params={"x": "1"}
    ).status_code == 404
    assert (
        client.request(
            "GET", "/datasets/ghost/lineage-coverage", content=b"{}"
        ).status_code
        == 404
    )
    assert (
        client.request(
            "GET", "/datasets/ghost/lineage-coverage", content=b" "
        ).status_code
        == 404
    )


def test_coverage_is_strictly_read_only_and_leaves_the_impact_cache_alone(
    client: TestClient,
) -> None:
    setup_target(client)
    make_dataset(
        client,
        "raw_orders",
        [
            {"name": "amount", "type": "decimal", "nullable": True},
            {"name": "order_id", "type": "integer", "nullable": False},
        ],
    )
    register(client, "raw_orders", 1, "order_id", "id")
    register(client, "raw_orders", 1, "amount", "total")

    # Populate an impact cache entry through the ordinary impact query.
    impact = client.get(
        "/datasets/raw_orders/versions/1/lineage/impact",
        params={"field": "order_id"},
    )
    assert impact.status_code == 200, impact.text
    cache_audit = client.get(
        "/datasets/raw_orders/versions/1/lineage/impact/cache-audit"
    )
    assert cache_audit.status_code == 200
    invalidations = client.get(
        "/datasets/raw_orders/versions/1/lineage/impact/cache-invalidations"
    )
    assert invalidations.status_code == 200
    lineage_before = client.get(
        "/datasets/dm_orders/versions/1/lineage"
    ).text

    first_text = coverage_response(client).text
    for _ in range(3):
        assert coverage_response(client).text == first_text

    # No lineage mapping changed and the impact cache gained, lost or revised
    # nothing: the audit statuses and the invalidation trail are unchanged.
    assert (
        client.get(
            "/datasets/raw_orders/versions/1/lineage/impact/cache-audit"
        ).text
        == cache_audit.text
    )
    assert (
        client.get(
            "/datasets/raw_orders/versions/1/lineage/impact/cache-invalidations"
        ).text
        == invalidations.text
    )
    assert (
        client.get("/datasets/dm_orders/versions/1/lineage").text
        == lineage_before
    )


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


_CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

assert client.post("/datasets", json={"name": "raw_orders"}).status_code == 201
assert client.post(
    "/datasets/raw_orders/versions",
    json={"fields": [
        {"name": "amount", "type": "decimal", "nullable": True},
        {"name": "gross", "type": "decimal", "nullable": True},
        {"name": "order_id", "type": "integer", "nullable": False},
    ]},
).status_code == 201
assert client.post("/datasets", json={"name": "raw_refunds"}).status_code == 201
assert client.post(
    "/datasets/raw_refunds/versions",
    json={"fields": [
        {"name": "refund_amount", "type": "decimal", "nullable": True},
    ]},
).status_code == 201
assert client.post("/datasets", json={"name": "dm_orders"}).status_code == 201
assert client.post(
    "/datasets/dm_orders/versions",
    json={"fields": [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "orphan", "type": "string", "nullable": True},
        {"name": "total", "type": "decimal", "nullable": True},
    ]},
).status_code == 201


def register(source, source_version, source_field, target_field,
             target_version=1):
    response = client.post(
        f"/datasets/dm_orders/versions/{target_version}/lineage",
        json={
            "target_dataset": "dm_orders",
            "target_version": target_version,
            "target_field": target_field,
            "source_dataset": source,
            "source_version": source_version,
            "source_field": source_field,
        },
    )
    assert response.status_code == 201, response.text


register("raw_orders", 1, "order_id", "id")
register("raw_orders", 1, "amount", "total")
register("raw_orders", 1, "gross", "total")
register("raw_refunds", 1, "refund_amount", "total")
print("created")
"""

_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

response = client.get("/datasets/dm_orders/lineage-coverage")
assert response.status_code == 200, response.text
assert response.text.endswith("\\n")
body = response.json()
assert list(body) == ["dataset", "versions", "totals"]
assert body["dataset"] == "dm_orders"
assert [v["version"] for v in body["versions"]] == [1]
entry = body["versions"][0]
assert list(entry) == [
    "version", "fields", "linked_field_count",
    "unlinked_field_count", "mapping_count",
]
assert [f["field"] for f in entry["fields"]] == ["id", "orphan", "total"]
id_field, orphan, total = entry["fields"]
assert list(id_field) == ["field", "sources", "source_dataset_count"]
assert id_field["sources"] == [
    {"dataset": "raw_orders", "version": 1, "field": "order_id"}
]
assert id_field["source_dataset_count"] == 1
assert orphan["sources"] == []
assert orphan["source_dataset_count"] == 0
assert total["sources"] == [
    {"dataset": "raw_orders", "version": 1, "field": "amount"},
    {"dataset": "raw_orders", "version": 1, "field": "gross"},
    {"dataset": "raw_refunds", "version": 1, "field": "refund_amount"},
]
assert total["source_dataset_count"] == 2
assert entry["linked_field_count"] == 2
assert entry["unlinked_field_count"] == 1
assert entry["mapping_count"] == 4
assert body["totals"] == {
    "version_count": 1,
    "field_count": 3,
    "linked_field_count": 2,
    "unlinked_field_count": 1,
    "mapping_count": 4,
}
again = client.get("/datasets/dm_orders/lineage-coverage")
assert again.text == response.text
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


def test_coverage_survives_a_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "lineage-coverage.db"
    assert _run_script(db_path, _CREATE_SCRIPT) == "created"
    assert _run_script(db_path, _VERIFY_SCRIPT) == "verified"
