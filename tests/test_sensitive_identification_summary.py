"""Tests for the read-only cross-version sensitive identification summary."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SUMMARY_PATH = "/datasets/orders/sensitive-identification-summary"

TOP_LEVEL_KEYS = ["dataset", "versions", "totals"]
VERSION_KEYS = ["version", "identifications", "suggestions", "stats"]
IDENTIFICATION_KEYS = [
    "id",
    "field",
    "field_type",
    "confidence",
    "source_dataset",
    "source_version",
    "source_field",
    "created_at",
]
SUGGESTION_KEYS = ["field", "classification", "masking", "allowed_roles"]
STATS_KEYS = [
    "identification_count",
    "suggestion_count",
    "high_count",
    "medium_count",
    "low_count",
    "none_count",
    "max_id",
    "first_created_at",
    "last_created_at",
]
TOTAL_KEYS = ["version_count", "identified_version_count", *STATS_KEYS]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_dataset(client: TestClient, name: str = "orders") -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201


def make_version(
    client: TestClient,
    fields: list[dict],
    dataset: str = "orders",
) -> int:
    response = client.post(
        f"/datasets/{dataset}/versions", json={"fields": fields}
    )
    assert response.status_code == 201, response.text
    return response.json()["version"]


def identify(
    client: TestClient,
    field: str,
    samples: list | None = None,
    *,
    dataset: str = "orders",
    version: int = 1,
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/sensitive-identifications",
        json={"field": field, "samples": [] if samples is None else samples},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def summary_response(client: TestClient, dataset: str = "orders"):
    return client.get(f"/datasets/{dataset}/sensitive-identification-summary")


def get_summary(client: TestClient, dataset: str = "orders") -> dict:
    response = summary_response(client, dataset)
    assert response.status_code == 200, response.text
    return response.json()


def setup_dataset(client: TestClient) -> dict:
    """Three versions: four records (one per confidence), one record, none."""
    make_dataset(client)
    make_version(
        client,
        [
            {"name": "contact_email", "type": "string", "nullable": True},
            {"name": "mobile_phone", "type": "string", "nullable": True},
            {"name": "label", "type": "string", "nullable": True},
            {"name": "api_token", "type": "string", "nullable": True},
            {"name": "amount", "type": "decimal", "nullable": True},
        ],
    )
    # Inserted deliberately in record-id order; confidence spans all four
    # levels and every suggestion classification appears.
    email = identify(client, "contact_email", ["alice@example.com"])
    phone = identify(client, "label", ["13800138000"])
    token = identify(client, "api_token", [])
    none_record = identify(client, "amount", [])
    make_version(
        client,
        [{"name": "email", "type": "string", "nullable": True}],
    )
    other = identify(
        client, "email", ["bob@example.com"], version=2
    )
    make_version(
        client,
        [{"name": "id", "type": "integer", "nullable": False}],
    )
    return {
        "email": email,
        "phone": phone,
        "token": token,
        "none": none_record,
        "other": other,
    }


# --------------------------------------------------------------------------- #
# Empty datasets
# --------------------------------------------------------------------------- #


def test_dataset_without_versions_is_empty_not_an_error(client: TestClient) -> None:
    make_dataset(client)
    body = get_summary(client)
    assert list(body) == TOP_LEVEL_KEYS
    assert body["dataset"] == "orders"
    assert body["versions"] == []
    assert body["totals"] == {
        "version_count": 0,
        "identified_version_count": 0,
        "identification_count": 0,
        "suggestion_count": 0,
        "high_count": 0,
        "medium_count": 0,
        "low_count": 0,
        "none_count": 0,
        "max_id": None,
        "first_created_at": None,
        "last_created_at": None,
    }


def test_versions_without_records_have_empty_lists_zero_counts_and_null_ranges(
    client: TestClient,
) -> None:
    make_dataset(client)
    make_version(
        client, [{"name": "id", "type": "integer", "nullable": False}]
    )
    make_version(
        client, [{"name": "id", "type": "string", "nullable": True}]
    )
    body = get_summary(client)
    assert [entry["version"] for entry in body["versions"]] == [1, 2]
    for entry in body["versions"]:
        assert list(entry) == VERSION_KEYS
        assert entry["identifications"] == []
        assert entry["suggestions"] == []
        assert entry["stats"] == {
            "identification_count": 0,
            "suggestion_count": 0,
            "high_count": 0,
            "medium_count": 0,
            "low_count": 0,
            "none_count": 0,
            "max_id": None,
            "first_created_at": None,
            "last_created_at": None,
        }
    totals = body["totals"]
    assert totals["version_count"] == 2
    assert totals["identified_version_count"] == 0
    assert totals["identification_count"] == 0
    assert totals["suggestion_count"] == 0
    assert totals["max_id"] is None
    assert totals["first_created_at"] is None
    assert totals["last_created_at"] is None


def test_summary_is_get_only(client: TestClient) -> None:
    make_dataset(client)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, SUMMARY_PATH)
        assert response.status_code == 405, method


# --------------------------------------------------------------------------- #
# Wire format and key order
# --------------------------------------------------------------------------- #


def test_wire_format_is_compact_fixed_order_with_one_newline(
    client: TestClient,
) -> None:
    setup_dataset(client)
    response = summary_response(client)
    assert response.headers["content-type"].startswith("application/json")
    text = response.text
    assert ": " not in text
    assert ", " not in text
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    assert "\n" not in text[:-1]

    body = response.json()
    assert list(body) == TOP_LEVEL_KEYS
    top_positions = [text.index(f'"{key}"') for key in TOP_LEVEL_KEYS]
    assert top_positions == sorted(top_positions)
    assert list(body["totals"]) == TOTAL_KEYS

    for entry in body["versions"]:
        assert list(entry) == VERSION_KEYS
        assert list(entry["stats"]) == STATS_KEYS
        for record in entry["identifications"]:
            assert list(record) == IDENTIFICATION_KEYS
        for suggestion in entry["suggestions"]:
            assert list(suggestion) == SUGGESTION_KEYS


def test_summary_is_deterministic_across_reads(client: TestClient) -> None:
    setup_dataset(client)
    first = summary_response(client).text
    second = summary_response(client).text
    assert first == second


# --------------------------------------------------------------------------- #
# Version entries, records and suggestions
# --------------------------------------------------------------------------- #


def test_versions_are_ordered_by_version_number_ascending(
    client: TestClient,
) -> None:
    setup_dataset(client)
    body = get_summary(client)
    assert [entry["version"] for entry in body["versions"]] == [1, 2, 3]


def test_identifications_follow_record_id_ascending_with_flat_source(
    client: TestClient,
) -> None:
    records = setup_dataset(client)
    first = get_summary(client)["versions"][0]

    ordered_ids = [
        records["email"]["id"],
        records["phone"]["id"],
        records["token"]["id"],
        records["none"]["id"],
    ]
    identifications = first["identifications"]
    assert [item["id"] for item in identifications] == ordered_ids
    assert [item["field"] for item in identifications] == [
        "contact_email",
        "label",
        "api_token",
        "amount",
    ]
    by_field = {item["field"]: item for item in identifications}
    assert by_field["contact_email"] == {
        "id": records["email"]["id"],
        "field": "contact_email",
        "field_type": "string",
        "confidence": "high",
        "source_dataset": "orders",
        "source_version": 1,
        "source_field": "contact_email",
        "created_at": records["email"]["created_at"],
    }
    assert by_field["label"]["confidence"] == "medium"
    assert by_field["api_token"]["confidence"] == "low"
    assert by_field["amount"]["confidence"] == "none"
    assert by_field["amount"]["field_type"] == "decimal"
    for item in identifications:
        assert item["source_dataset"] == "orders"
        assert item["source_version"] == 1
        assert item["source_field"] == item["field"]
        assert "evidence" not in item
        assert "source" not in item

    second = get_summary(client)["versions"][1]
    assert [item["id"] for item in second["identifications"]] == [
        records["other"]["id"]
    ]
    other = second["identifications"][0]
    assert other["field"] == "email"
    assert other["confidence"] == "high"
    assert other["source_version"] == 2
    assert other["source_field"] == "email"


def test_suggestions_follow_record_id_and_only_hit_records_contribute(
    client: TestClient,
) -> None:
    records = setup_dataset(client)
    first, second, third = get_summary(client)["versions"]

    assert first["suggestions"] == [
        {
            "field": "contact_email",
            "classification": "PII",
            "masking": "partial",
            "allowed_roles": [],
        },
        {
            "field": "label",
            "classification": "PII",
            "masking": "partial",
            "allowed_roles": [],
        },
        {
            "field": "api_token",
            "classification": "CREDENTIAL",
            "masking": "redact",
            "allowed_roles": [],
        },
    ]
    # The 'none' record (amount) contributes no suggestion.
    assert [s["field"] for s in first["suggestions"]] == [
        item["field"]
        for item in first["identifications"]
        if item["field"] != "amount"
    ]
    assert second["suggestions"] == [
        {
            "field": "email",
            "classification": "PII",
            "masking": "partial",
            "allowed_roles": [],
        }
    ]
    assert third["suggestions"] == []

    # Suggestions match the read-only per-version suggestions endpoint exactly,
    # including the fixed empty role list.
    baseline = client.get(
        "/datasets/orders/versions/1/sensitive-identifications/masking-suggestions"
    )
    assert baseline.status_code == 200
    assert baseline.json() == first["suggestions"]
    assert records["email"]["id"] < records["none"]["id"]


def test_per_version_stats_count_levels_suggestions_and_ranges(
    client: TestClient,
) -> None:
    records = setup_dataset(client)
    first, second, third = get_summary(client)["versions"]

    assert first["stats"] == {
        "identification_count": 4,
        "suggestion_count": 3,
        "high_count": 1,
        "medium_count": 1,
        "low_count": 1,
        "none_count": 1,
        "max_id": records["none"]["id"],
        "first_created_at": records["email"]["created_at"],
        "last_created_at": records["none"]["created_at"],
    }
    assert second["stats"] == {
        "identification_count": 1,
        "suggestion_count": 1,
        "high_count": 1,
        "medium_count": 0,
        "low_count": 0,
        "none_count": 0,
        "max_id": records["other"]["id"],
        "first_created_at": records["other"]["created_at"],
        "last_created_at": records["other"]["created_at"],
    }
    assert third["stats"]["identification_count"] == 0
    assert third["stats"]["suggestion_count"] == 0
    assert third["stats"]["max_id"] is None


def test_totals_count_versions_and_sum_the_per_version_values(
    client: TestClient,
) -> None:
    records = setup_dataset(client)
    body = get_summary(client)
    all_records = [
        item
        for entry in body["versions"]
        for item in entry["identifications"]
    ]
    totals = body["totals"]
    assert list(totals) == TOTAL_KEYS
    assert totals["version_count"] == 3
    assert totals["identified_version_count"] == 2
    assert totals["identification_count"] == 5
    assert totals["suggestion_count"] == 4
    assert totals["high_count"] == 2
    assert totals["medium_count"] == 1
    assert totals["low_count"] == 1
    assert totals["none_count"] == 1
    assert totals["max_id"] == max(item["id"] for item in all_records)
    assert totals["max_id"] == records["other"]["id"]
    assert totals["first_created_at"] == min(
        item["created_at"] for item in all_records
    )
    assert totals["last_created_at"] == max(
        item["created_at"] for item in all_records
    )


# --------------------------------------------------------------------------- #
# Errors: 404 / 422, no writes
# --------------------------------------------------------------------------- #


def test_unknown_dataset_returns_404(client: TestClient) -> None:
    response = client.get("/datasets/ghost/sensitive-identification-summary")
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "not_found"


def test_rejects_body_bytes_and_query_parameters(client: TestClient) -> None:
    make_dataset(client)
    before = summary_response(client).text

    with_body = client.request("GET", SUMMARY_PATH, content=b"{}")
    with_whitespace = client.request("GET", SUMMARY_PATH, content=b"   ")
    with_single_space = client.request("GET", SUMMARY_PATH, content=b" ")
    with_tabs_newlines = client.request("GET", SUMMARY_PATH, content=b" \t\n")
    with_query = client.get(SUMMARY_PATH, params={"x": "1"})
    for response in (
        with_body,
        with_whitespace,
        with_single_space,
        with_tabs_newlines,
        with_query,
    ):
        assert response.status_code == 422
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]

    # The rejections wrote nothing.
    assert summary_response(client).text == before


def test_shape_errors_keep_404_precedence(client: TestClient) -> None:
    assert (
        client.get(
            "/datasets/ghost/sensitive-identification-summary",
            params={"x": "1"},
        ).status_code
        == 404
    )
    assert (
        client.request(
            "GET",
            "/datasets/ghost/sensitive-identification-summary",
            content=b"{}",
        ).status_code
        == 404
    )
    assert (
        client.request(
            "GET",
            "/datasets/ghost/sensitive-identification-summary",
            content=b" ",
        ).status_code
        == 404
    )


# --------------------------------------------------------------------------- #
# Read-only behavior: baseline endpoints and persisted records are untouched
# --------------------------------------------------------------------------- #


def test_summary_read_changes_nothing(client: TestClient) -> None:
    records = setup_dataset(client)
    policies_before = client.get(
        "/datasets/orders/versions/1/privacy-policies"
    ).json()
    assert policies_before == []
    listed_before = client.get(
        "/datasets/orders/versions/1/sensitive-identifications"
    ).text

    for _ in range(3):
        assert summary_response(client).status_code == 200

    # No identification record was written or refreshed.
    listed_after = client.get(
        "/datasets/orders/versions/1/sensitive-identifications"
    ).text
    assert listed_after == listed_before
    # The advisory suggestions registered no privacy policy.
    assert (
        client.get(
            "/datasets/orders/versions/1/privacy-policies"
        ).json()
        == []
    )
    # A subsequent refresh of a record still keeps its stable id, proving the
    # summary reads did not interfere with the identification write path.
    rerun = identify(client, "contact_email", ["alice@example.com"])
    assert rerun["id"] == records["email"]["id"]


# --------------------------------------------------------------------------- #
# Persistence across a process restart
# --------------------------------------------------------------------------- #


_CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response, *allowed):
    assert response.status_code in allowed, response.text

ok(client.post("/datasets", json={"name": "orders"}), 201)
ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [
        {"name": "contact_email", "type": "string", "nullable": True},
        {"name": "label", "type": "string", "nullable": True},
        {"name": "api_token", "type": "string", "nullable": True},
        {"name": "amount", "type": "decimal", "nullable": True},
    ]},
), 201)
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "contact_email", "samples": ["alice@example.com"]},
), 201)
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "label", "samples": ["13800138000"]},
), 201)
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "api_token", "samples": []},
), 201)
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "amount", "samples": []},
), 201)
ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
), 201)
print("created")
"""

_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

response = client.get("/datasets/orders/sensitive-identification-summary")
assert response.status_code == 200, response.text
assert response.text.endswith("\\n")
body = response.json()
assert list(body) == ["dataset", "versions", "totals"]
assert body["dataset"] == "orders"
assert [v["version"] for v in body["versions"]] == [1, 2]
first, second = body["versions"]
assert list(first) == ["version", "identifications", "suggestions", "stats"]
assert [i["field"] for i in first["identifications"]] == [
    "contact_email", "label", "api_token", "amount"
]
assert all(
    list(item) == [
        "id", "field", "field_type", "confidence",
        "source_dataset", "source_version", "source_field", "created_at",
    ]
    for item in first["identifications"]
)
assert all(
    item["source_dataset"] == "orders" and item["source_version"] == 1
    for item in first["identifications"]
)
assert [s["field"] for s in first["suggestions"]] == [
    "contact_email", "label", "api_token"
]
assert all(list(s) == ["field", "classification", "masking", "allowed_roles"]
           for s in first["suggestions"])
assert all(s["allowed_roles"] == [] for s in first["suggestions"])
assert first["stats"] == {
    "identification_count": 4,
    "suggestion_count": 3,
    "high_count": 1,
    "medium_count": 1,
    "low_count": 1,
    "none_count": 1,
    "max_id": first["identifications"][-1]["id"],
    "first_created_at": first["identifications"][0]["created_at"],
    "last_created_at": first["identifications"][-1]["created_at"],
}
assert second["identifications"] == []
assert second["suggestions"] == []
assert second["stats"]["identification_count"] == 0
assert second["stats"]["max_id"] is None
totals = body["totals"]
assert list(totals) == [
    "version_count", "identified_version_count",
    "identification_count", "suggestion_count",
    "high_count", "medium_count", "low_count", "none_count",
    "max_id", "first_created_at", "last_created_at",
]
assert totals["version_count"] == 2
assert totals["identified_version_count"] == 1
assert totals["identification_count"] == 4
assert totals["suggestion_count"] == 3
assert totals["max_id"] == first["stats"]["max_id"]
assert client.get(
    "/datasets/orders/sensitive-identification-summary"
).text == response.text
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


def test_summary_survives_a_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "sensitive-identification-summary.db"
    assert _run_script(db_path, _CREATE_SCRIPT) == "created"
    assert _run_script(db_path, _VERIFY_SCRIPT) == "verified"
