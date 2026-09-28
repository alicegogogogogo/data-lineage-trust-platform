"""Tests for the read-only whole-dataset lineage registration coverage check."""

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


def make_dataset(
    client: TestClient, name: str, fields: list[dict] | None = None
) -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    if fields is not None:
        response = client.post(
            f"/datasets/{name}/versions", json={"fields": fields}
        )
        assert response.status_code == 201, response.text


def add_version(client: TestClient, name: str, fields: list[dict]) -> int:
    response = client.post(f"/datasets/{name}/versions", json={"fields": fields})
    assert response.status_code == 201, response.text
    return response.json()["version"]


def lineage_payload(
    source: str,
    source_version: int,
    source_field: str,
    target_field: str,
    target: str = "dm_orders",
    target_version: int = 1,
) -> dict:
    return {
        "target_dataset": target,
        "target_version": target_version,
        "target_field": target_field,
        "source_dataset": source,
        "source_version": source_version,
        "source_field": source_field,
    }


def register_lineage(client: TestClient, payload: dict) -> None:
    response = client.post(
        f"/datasets/{payload['target_dataset']}/versions/"
        f"{payload['target_version']}/lineage",
        json=payload,
    )
    assert response.status_code == 201, response.text


def coverage_response(client: TestClient, dataset: str = "dm_orders"):
    response = client.get(f"/datasets/{dataset}/lineage-coverage")
    assert response.status_code == 200, response.text
    return response


def get_coverage(client: TestClient, dataset: str = "dm_orders") -> dict:
    return coverage_response(client, dataset).json()


def setup_datasets(client: TestClient) -> None:
    make_dataset(
        client,
        "raw_orders",
        [
            {"name": "order_id", "type": "integer", "nullable": False},
            {"name": "amount", "type": "decimal", "nullable": True},
        ],
    )
    make_dataset(
        client,
        "raw_refunds",
        [{"name": "refund_amount", "type": "decimal", "nullable": True}],
    )
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


def test_coverage_without_versions_is_empty_not_an_error(client: TestClient) -> None:
    make_dataset(client, "dm_orders")
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
    make_dataset(client, "dm_orders")
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, COVERAGE_PATH)
        assert response.status_code == 405, method


def test_coverage_wire_format_is_compact_with_fixed_key_order_and_newline(
    client: TestClient,
) -> None:
    setup_datasets(client)
    register_lineage(
        client, lineage_payload("raw_orders", 1, "order_id", "id")
    )

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
# Field and source ordering, counts and deduplication
# --------------------------------------------------------------------------- #


def test_coverage_lists_unlinked_fields_with_empty_sources(client: TestClient) -> None:
    setup_datasets(client)
    register_lineage(client, lineage_payload("raw_orders", 1, "order_id", "id"))

    fields = get_coverage(client)["versions"][0]["fields"]
    assert [entry["field"] for entry in fields] == ["id", "orphan", "total"]
    by_name = {entry["field"]: entry for entry in fields}

    linked = by_name["id"]
    assert linked["sources"] == [
        {"dataset": "raw_orders", "version": 1, "field": "order_id"}
    ]
    assert linked["source_dataset_count"] == 1

    for name in ("orphan", "total"):
        assert by_name[name]["sources"] == []
        assert by_name[name]["source_dataset_count"] == 0


def test_coverage_sources_sort_by_dataset_version_field(client: TestClient) -> None:
    setup_datasets(client)
    add_version(
        client,
        "raw_orders",
        [
            {"name": "amount", "type": "decimal", "nullable": True},
            {"name": "note", "type": "string", "nullable": True},
        ],
    )
    # Registered deliberately out of the required output order.
    register_lineage(
        client, lineage_payload("raw_refunds", 1, "refund_amount", "total")
    )
    register_lineage(
        client, lineage_payload("raw_orders", 2, "amount", "total")
    )
    register_lineage(
        client, lineage_payload("raw_orders", 1, "amount", "total")
    )

    total = next(
        entry
        for entry in get_coverage(client)["versions"][0]["fields"]
        if entry["field"] == "total"
    )
    assert total["sources"] == [
        {"dataset": "raw_orders", "version": 1, "field": "amount"},
        {"dataset": "raw_orders", "version": 2, "field": "amount"},
        {"dataset": "raw_refunds", "version": 1, "field": "refund_amount"},
    ]
    # Two distinct source datasets regardless of the three references.
    assert total["source_dataset_count"] == 2


def test_coverage_source_dataset_count_counts_distinct_datasets_not_mappings(
    client: TestClient,
) -> None:
    make_dataset(
        client,
        "raw",
        [
            {"name": "a", "type": "string", "nullable": True},
            {"name": "b", "type": "string", "nullable": True},
        ],
    )
    make_dataset(
        client,
        "dm",
        [{"name": "out", "type": "string", "nullable": True}],
    )
    register_lineage(client, lineage_payload("raw", 1, "a", "out", target="dm"))
    register_lineage(client, lineage_payload("raw", 1, "b", "out", target="dm"))

    entry = get_coverage(client, "dm")["versions"][0]["fields"][0]
    assert entry["field"] == "out"
    assert len(entry["sources"]) == 2
    assert entry["source_dataset_count"] == 1


def test_coverage_only_counts_mappings_targeting_this_dataset(
    client: TestClient,
) -> None:
    setup_datasets(client)
    register_lineage(client, lineage_payload("raw_orders", 1, "order_id", "id"))
    # dm_orders acting as a source of another dataset must not appear in its
    # own coverage report.
    make_dataset(
        client,
        "reporting",
        [{"name": "report_id", "type": "integer", "nullable": False}],
    )
    register_lineage(
        client,
        lineage_payload(
            "dm_orders", 1, "id", "report_id", target="reporting"
        ),
    )

    body = get_coverage(client)
    fields = body["versions"][0]["fields"]
    by_name = {entry["field"]: entry for entry in fields}
    assert len(by_name["id"]["sources"]) == 1
    assert by_name["total"]["sources"] == []
    # The raw_orders coverage report sees nothing either: the mapping points
    # away from it, and being a source is not being a target.
    raw = get_coverage(client, "raw_orders")["versions"][0]
    assert raw["mapping_count"] == 0
    assert raw["linked_field_count"] == 0
    assert raw["unlinked_field_count"] == 2


def test_coverage_versions_sort_ascending_with_per_version_counts(
    client: TestClient,
) -> None:
    setup_datasets(client)
    register_lineage(client, lineage_payload("raw_orders", 1, "order_id", "id"))
    add_version(
        client,
        "dm_orders",
        [
            {"name": "id", "type": "bigint", "nullable": False},
            {"name": "label", "type": "string", "nullable": True},
        ],
    )
    v2_mapping = lineage_payload("raw_orders", 1, "amount", "label")
    v2_mapping["target_version"] = 2
    register_lineage(client, v2_mapping)

    body = get_coverage(client)
    assert [entry["version"] for entry in body["versions"]] == [1, 2]

    first, second = body["versions"]
    assert [field["field"] for field in first["fields"]] == [
        "id",
        "orphan",
        "total",
    ]
    assert first["linked_field_count"] == 1
    assert first["unlinked_field_count"] == 2
    assert first["mapping_count"] == 1
    assert first["linked_field_count"] + first["unlinked_field_count"] == len(
        first["fields"]
    )

    assert [field["field"] for field in second["fields"]] == ["id", "label"]
    assert second["linked_field_count"] == 1
    assert second["unlinked_field_count"] == 1
    assert second["mapping_count"] == 1
    assert second["linked_field_count"] + second["unlinked_field_count"] == len(
        second["fields"]
    )


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #


def test_coverage_totals_equal_the_sum_of_the_version_entries(
    client: TestClient,
) -> None:
    setup_datasets(client)
    register_lineage(client, lineage_payload("raw_orders", 1, "order_id", "id"))
    register_lineage(client, lineage_payload("raw_orders", 1, "amount", "total"))
    register_lineage(
        client, lineage_payload("raw_refunds", 1, "refund_amount", "total")
    )
    add_version(
        client,
        "dm_orders",
        [
            {"name": "id", "type": "bigint", "nullable": False},
            {"name": "label", "type": "string", "nullable": True},
        ],
    )

    body = get_coverage(client)
    versions = body["versions"]
    totals = body["totals"]
    assert list(totals) == TOTAL_KEYS
    assert totals["version_count"] == len(versions)
    assert totals["field_count"] == sum(len(v["fields"]) for v in versions)
    assert totals["linked_field_count"] == sum(
        v["linked_field_count"] for v in versions
    )
    assert totals["unlinked_field_count"] == sum(
        v["unlinked_field_count"] for v in versions
    )
    assert totals["mapping_count"] == sum(v["mapping_count"] for v in versions)
    assert totals == {
        "version_count": 2,
        "field_count": 5,
        "linked_field_count": 2,
        "unlinked_field_count": 3,
        "mapping_count": 3,
    }
    for version in versions:
        assert (
            version["linked_field_count"] + version["unlinked_field_count"]
            == len(version["fields"])
        )
        assert version["mapping_count"] == sum(
            len(field["sources"]) for field in version["fields"]
        )


# --------------------------------------------------------------------------- #
# Errors: 404 / 422, no writes
# --------------------------------------------------------------------------- #


def test_coverage_unknown_dataset_returns_404(client: TestClient) -> None:
    response = client.get("/datasets/ghost/lineage-coverage")
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "not_found"


def test_coverage_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset(client, "dm_orders")
    before = coverage_response(client).text

    with_body = client.request("GET", COVERAGE_PATH, content=b"{}")
    with_whitespace = client.request("GET", COVERAGE_PATH, content=b"   ")
    with_query = client.get(COVERAGE_PATH, params={"limit": 1})
    assert with_body.status_code == 422
    assert with_whitespace.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, with_whitespace, with_query):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]

    # The rejections wrote nothing.
    assert coverage_response(client).text == before


def test_coverage_shape_errors_keep_404_precedence(client: TestClient) -> None:
    make_dataset(client, "dm_orders")
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


def test_coverage_is_strictly_read_only_and_does_not_touch_lineage_or_cache(
    client: TestClient,
) -> None:
    setup_datasets(client)
    register_lineage(client, lineage_payload("raw_orders", 1, "order_id", "id"))
    register_lineage(client, lineage_payload("raw_orders", 1, "amount", "total"))

    lineage_before = client.get(
        "/datasets/dm_orders/versions/1/lineage"
    ).text
    invalidations_before = client.get(
        "/datasets/dm_orders/versions/1/lineage/impact/cache-invalidations"
    ).text
    audit_before = client.get(
        "/datasets/dm_orders/versions/1/lineage/impact/cache-audit"
    ).text
    first_text = coverage_response(client).text

    for _ in range(3):
        response = coverage_response(client)
        assert response.text == first_text
    assert (
        client.get("/datasets/dm_orders/versions/1/lineage").text
        == lineage_before
    )
    assert (
        client.get(
            "/datasets/dm_orders/versions/1/lineage/impact/cache-invalidations"
        ).text
        == invalidations_before
    )
    assert (
        client.get(
            "/datasets/dm_orders/versions/1/lineage/impact/cache-audit"
        ).text
        == audit_before
    )


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


_CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

for name, fields in (
    ("raw_refunds", [
        {"name": "refund_amount", "type": "decimal", "nullable": True},
    ]),
    ("raw_orders", [
        {"name": "amount", "type": "decimal", "nullable": True},
        {"name": "order_id", "type": "integer", "nullable": False},
    ]),
    ("dm_orders", [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "orphan", "type": "string", "nullable": True},
        {"name": "total", "type": "decimal", "nullable": True},
    ]),
):
    assert client.post("/datasets", json={"name": name}).status_code == 201
    assert client.post(
        f"/datasets/{name}/versions", json={"fields": fields}
    ).status_code == 201

def register(source, source_version, source_field, target_field,
             target="dm_orders", target_version=1):
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

# Out-of-order registration on purpose: the response ordering must not depend
# on insertion or database order.
register("raw_refunds", 1, "refund_amount", "total")
register("raw_orders", 1, "order_id", "id")
register("raw_orders", 1, "amount", "total")
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
    {"dataset": "raw_refunds", "version": 1, "field": "refund_amount"},
]
assert total["source_dataset_count"] == 2
assert entry["linked_field_count"] == 2
assert entry["unlinked_field_count"] == 1
assert entry["mapping_count"] == 3
assert body["totals"] == {
    "version_count": 1,
    "field_count": 3,
    "linked_field_count": 2,
    "unlinked_field_count": 1,
    "mapping_count": 3,
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
