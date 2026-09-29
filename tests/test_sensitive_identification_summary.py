"""Tests for the read-only cross-version sensitive identification summary."""

from __future__ import annotations

import os
import sqlite3
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


def add_version(
    client: TestClient, fields: list[str] | None = None, dataset: str = "orders"
) -> int:
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={
            "fields": [
                {"name": field, "type": "string", "nullable": True}
                for field in (fields if fields is not None else ["id"])
            ]
        },
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


def get_summary(client: TestClient, dataset: str = "orders") -> dict:
    response = client.get(f"/datasets/{dataset}/sensitive-identification-summary")
    assert response.status_code == 200, response.text
    return response.json()


def summary_response(client: TestClient, dataset: str = "orders"):
    response = client.get(f"/datasets/{dataset}/sensitive-identification-summary")
    assert response.status_code == 200, response.text
    return response


def empty_stats() -> dict:
    return {
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


# --------------------------------------------------------------------------- #
# Empty state and wire format
# --------------------------------------------------------------------------- #


def test_summary_without_versions_is_empty_not_an_error(client: TestClient) -> None:
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
    assert list(body["totals"]) == TOTAL_KEYS


def test_summary_is_get_only(client: TestClient) -> None:
    make_dataset(client)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, SUMMARY_PATH)
        assert response.status_code == 405, method


def test_summary_wire_format_is_compact_with_fixed_key_order_and_newline(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client, ["contact_email"])
    identify(client, "contact_email", ["alice@example.com"])

    response = summary_response(client)
    assert response.headers["content-type"].startswith("application/json")
    text = response.text
    # Compact whitespace: no separator spaces, and the only line break is the
    # single trailing newline.
    assert ": " not in text
    assert ", " not in text
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    assert "\n" not in text[:-1]
    # No scientific notation for the integer counters.
    assert "e+" not in text.lower()

    body = response.json()
    assert list(body) == TOP_LEVEL_KEYS
    version_entry = body["versions"][0]
    assert list(version_entry) == VERSION_KEYS
    assert list(version_entry["identifications"][0]) == IDENTIFICATION_KEYS
    assert list(version_entry["suggestions"][0]) == SUGGESTION_KEYS
    assert list(version_entry["stats"]) == STATS_KEYS
    assert list(body["totals"]) == TOTAL_KEYS


# --------------------------------------------------------------------------- #
# Versions, records and suggestions
# --------------------------------------------------------------------------- #


def test_summary_versions_sort_ascending_and_include_empty_versions(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client, ["contact_email"])
    identify(client, "contact_email", ["alice@example.com"])
    add_version(client, ["id"])  # version 2, no identifications
    add_version(client, ["api_token"])  # version 3, one record
    identify(client, "api_token", ["secret"], version=3)

    body = get_summary(client)
    assert [entry["version"] for entry in body["versions"]] == [1, 2, 3]
    for entry in body["versions"]:
        assert set(entry) == set(VERSION_KEYS)

    first, second, third = body["versions"]
    assert len(first["identifications"]) == 1
    assert first["stats"]["identification_count"] == 1

    # The version without records keeps both lists empty and the zero/null
    # stats.
    assert second["identifications"] == []
    assert second["suggestions"] == []
    assert second["stats"] == empty_stats()

    assert third["stats"]["identification_count"] == 1


def test_summary_identifications_sort_by_id_with_flattened_source(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client, ["contact_email", "mobile_phone", "note", "id"])
    first = identify(client, "contact_email", ["alice@example.com"])
    second = identify(client, "note", [])  # no hit: still a record
    third = identify(client, "mobile_phone", ["13800000000"])

    records = get_summary(client)["versions"][0]["identifications"]
    assert [entry["id"] for entry in records] == [
        first["id"],
        second["id"],
        third["id"],
    ]
    by_id = {entry["id"]: entry for entry in records}
    assert by_id[first["id"]] == {
        "id": first["id"],
        "field": "contact_email",
        "field_type": "string",
        "confidence": "high",
        "source_dataset": "orders",
        "source_version": 1,
        "source_field": "contact_email",
        "created_at": first["created_at"],
    }
    # The unhit record is still listed with confidence "none".
    assert by_id[second["id"]]["confidence"] == "none"
    for entry in records:
        assert list(entry) == IDENTIFICATION_KEYS
        # Evidence is intentionally not part of the summary.
        assert "evidence" not in entry


def test_summary_suggestions_follow_record_ids_with_empty_allowed_roles(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client, ["contact_email", "note", "api_token"])
    # id order: email (PII candidate), note (no candidate), token (CREDENTIAL).
    identify(client, "contact_email", ["alice@example.com"])
    identify(client, "note", [])
    identify(client, "api_token", ["secret"])

    entry = get_summary(client)["versions"][0]
    assert entry["suggestions"] == [
        {
            "field": "contact_email",
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
    for suggestion in entry["suggestions"]:
        assert list(suggestion) == SUGGESTION_KEYS

    # The candidates agree byte-for-shape with the baseline suggestions read.
    baseline = client.get(
        "/datasets/orders/versions/1/sensitive-identifications/masking-suggestions"
    ).json()
    assert entry["suggestions"] == baseline


def test_summary_record_order_does_not_depend_on_database_order(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client, ["id"])
    # Seed two records sharing one write time with explicit ids; the summary
    # must still list by record id ascending.
    db_file = os.environ["DATA_LINEAGE_DB"]
    with sqlite3.connect(db_file) as direct:
        version_pk = direct.execute(
            "SELECT id FROM schema_versions WHERE version = 1"
        ).fetchone()[0]
        stamp = "2026-01-01T00:00:00+00:00"
        direct.execute(
            "INSERT INTO sensitive_identifications (id, version_id, field, "
            "field_type, evidence, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (21, version_pk, "later_field", "string", "[]", "none", stamp),
        )
        direct.execute(
            "INSERT INTO sensitive_identifications (id, version_id, field, "
            "field_type, evidence, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (20, version_pk, "earlier_field", "string", "[]", "none", stamp),
        )

    entry = get_summary(client)["versions"][0]
    assert [record["id"] for record in entry["identifications"]] == [20, 21]
    stats = entry["stats"]
    assert stats["identification_count"] == 2
    assert stats["max_id"] == 21
    # Same instant: the range is that instant and never depends on DB order.
    assert stats["first_created_at"] == stamp
    assert stats["last_created_at"] == stamp


# --------------------------------------------------------------------------- #
# Per-version statistics
# --------------------------------------------------------------------------- #


def test_summary_stats_count_distribution_max_id_and_time_range(
    client: TestClient,
) -> None:
    make_dataset(client)
    response = client.post(
        "/datasets/orders/versions",
        json={
            "fields": [
                {"name": "contact_email", "type": "string", "nullable": True},
                {"name": "reach_me", "type": "string", "nullable": True},
                {"name": "api_token", "type": "string", "nullable": True},
                {"name": "note", "type": "string", "nullable": True},
                {"name": "id", "type": "integer", "nullable": False},
            ]
        },
    )
    assert response.status_code == 201, response.text
    one = identify(client, "contact_email", ["alice@example.com"])  # high
    two = identify(client, "reach_me", ["alice@example.com"])  # medium
    three = identify(client, "api_token", ["secret"])  # low (name only)
    four = identify(client, "note", [1, None])  # none: non-strings never hit
    # An integer field is name-matched only even with an email-looking sample.
    five = identify(client, "id", ["alice@example.com"])  # none

    stats = get_summary(client)["versions"][0]["stats"]
    assert list(stats) == STATS_KEYS
    assert stats["identification_count"] == 5
    # The high/medium/low records each carry a candidate; both 'none' records
    # (unhit name on a string field, and an integer field matched by name
    # only) do not.
    assert stats["suggestion_count"] == 3
    assert stats["high_count"] == 1
    assert stats["medium_count"] == 1
    assert stats["low_count"] == 1
    assert stats["none_count"] == 2
    assert (
        stats["high_count"]
        + stats["medium_count"]
        + stats["low_count"]
        + stats["none_count"]
        == stats["identification_count"]
    )
    assert stats["max_id"] == five["id"]
    assert stats["first_created_at"] == one["created_at"]
    assert stats["last_created_at"] == five["created_at"]
    assert stats["first_created_at"].endswith("+00:00")
    assert one["id"] < two["id"] < three["id"] < four["id"] < five["id"]


def test_summary_refresh_changes_stats_and_suggestion_on_next_read(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client, ["note"])
    record = identify(client, "note", [])
    assert record["confidence"] == "none"

    entry = get_summary(client)["versions"][0]
    assert entry["stats"]["none_count"] == 1
    assert entry["suggestions"] == []

    refreshed = identify(client, "note", ["alice@example.com"])
    assert refreshed["id"] == record["id"]

    entry = get_summary(client)["versions"][0]
    assert entry["identifications"][0]["id"] == record["id"]
    assert entry["identifications"][0]["confidence"] == "medium"
    assert entry["stats"]["medium_count"] == 1
    assert entry["stats"]["none_count"] == 0
    assert entry["suggestions"][0]["field"] == "note"


# --------------------------------------------------------------------------- #
# Whole-dataset totals
# --------------------------------------------------------------------------- #


def test_summary_totals_sum_versions_and_span_all_records(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client, ["contact_email", "api_token"])
    identify(client, "contact_email", ["alice@example.com"])  # high
    identify(client, "api_token", ["secret"])  # low
    add_version(client, ["id"])  # version 2, no records
    add_version(client, ["reach_me"])  # version 3
    identify(client, "reach_me", ["alice@example.com"], version=3)  # medium

    body = get_summary(client)
    versions = body["versions"]
    totals = body["totals"]

    assert totals["version_count"] == len(versions) == 3
    assert totals["identified_version_count"] == 2
    assert totals["identification_count"] == sum(
        v["stats"]["identification_count"] for v in versions
    )
    assert totals["suggestion_count"] == sum(
        v["stats"]["suggestion_count"] for v in versions
    )
    assert totals["identification_count"] == 3
    assert totals["suggestion_count"] == 3
    for key in ("high_count", "medium_count", "low_count", "none_count"):
        assert totals[key] == sum(v["stats"][key] for v in versions)
    assert (totals["high_count"], totals["medium_count"], totals["low_count"]) == (
        1,
        1,
        1,
    )

    all_records = [
        record for v in versions for record in v["identifications"]
    ]
    all_times = [record["created_at"] for record in all_records]
    assert totals["max_id"] == max(record["id"] for record in all_records)
    assert totals["first_created_at"] == min(all_times)
    assert totals["last_created_at"] == max(all_times)


def test_summary_totals_are_null_when_no_record_exists(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    add_version(client)
    totals = get_summary(client)["totals"]
    assert totals == {
        "version_count": 2,
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


# --------------------------------------------------------------------------- #
# Errors: 404 / 422, no writes
# --------------------------------------------------------------------------- #


def test_summary_unknown_dataset_returns_404(client: TestClient) -> None:
    response = client.get("/datasets/ghost/sensitive-identification-summary")
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "not_found"


def test_summary_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset(client)
    add_version(client, ["contact_email"])
    identify(client, "contact_email", ["alice@example.com"])
    before = summary_response(client).text

    with_body = client.request("GET", SUMMARY_PATH, content=b"{}")
    whitespace_body = client.request("GET", SUMMARY_PATH, content=b"   ")
    single_space_body = client.request("GET", SUMMARY_PATH, content=b" ")
    with_query = client.get(SUMMARY_PATH, params={"limit": 1})
    assert with_body.status_code == 422
    assert whitespace_body.status_code == 422
    assert single_space_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, whitespace_body, single_space_body, with_query):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]
        assert "sqlite" not in response.text.lower()
        assert "traceback" not in response.text.lower()

    # The rejections wrote nothing.
    assert summary_response(client).text == before


def test_summary_shape_errors_keep_404_precedence(client: TestClient) -> None:
    make_dataset(client)
    assert client.get(
        "/datasets/ghost/sensitive-identification-summary", params={"x": "1"}
    ).status_code == 404
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


def test_summary_is_strictly_read_only(client: TestClient) -> None:
    make_dataset(client)
    add_version(client, ["contact_email", "api_token", "note"])
    identify(client, "contact_email", ["alice@example.com"])
    identify(client, "api_token", ["secret"])
    identify(client, "note", [])

    identifications_before = client.get(
        "/datasets/orders/versions/1/sensitive-identifications"
    ).json()
    suggestions_before = client.get(
        "/datasets/orders/versions/1/sensitive-identifications/masking-suggestions"
    ).json()
    first_text = summary_response(client).text

    for _ in range(3):
        response = summary_response(client)
        assert response.text == first_text

    assert (
        client.get(
            "/datasets/orders/versions/1/sensitive-identifications"
        ).json()
        == identifications_before
    )
    assert (
        client.get(
            "/datasets/orders/versions/1/sensitive-identifications/masking-suggestions"
        ).json()
        == suggestions_before
    )

    # Rejected shapes write nothing either.
    client.request("GET", SUMMARY_PATH, content=b"{}")
    client.get(SUMMARY_PATH, params={"x": 1})
    assert summary_response(client).text == first_text


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


_CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

assert client.post("/datasets", json={"name": "orders"}).status_code == 201
assert client.post(
    "/datasets/orders/versions",
    json={"fields": [
        {"name": "contact_email", "type": "string", "nullable": True},
        {"name": "note", "type": "string", "nullable": True},
    ]},
).status_code == 201
for field, samples in (
    ("contact_email", ["alice@example.com"]),
    ("note", []),
):
    response = client.post(
        "/datasets/orders/versions/1/sensitive-identifications",
        json={"field": field, "samples": samples},
    )
    assert response.status_code == 201, response.text
assert client.post(
    "/datasets/orders/versions",
    json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
).status_code == 201
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
assert [r["field"] for r in first["identifications"]] == [
    "contact_email",
    "note",
]
assert all(list(r) == [
    "id",
    "field",
    "field_type",
    "confidence",
    "source_dataset",
    "source_version",
    "source_field",
    "created_at",
] for r in first["identifications"])
hit, missed = first["identifications"]
assert hit["confidence"] == "high"
assert hit["source_dataset"] == "orders"
assert hit["source_version"] == 1
assert missed["confidence"] == "none"
assert first["suggestions"] == [
    {
        "field": "contact_email",
        "classification": "PII",
        "masking": "partial",
        "allowed_roles": [],
    }
]
assert first["stats"]["identification_count"] == 2
assert first["stats"]["suggestion_count"] == 1
assert first["stats"]["high_count"] == 1
assert first["stats"]["none_count"] == 1
assert first["stats"]["max_id"] == missed["id"]
assert second["identifications"] == []
assert second["suggestions"] == []
assert second["stats"]["max_id"] is None
totals = body["totals"]
assert totals["version_count"] == 2
assert totals["identified_version_count"] == 1
assert totals["identification_count"] == 2
assert totals["suggestion_count"] == 1
assert totals["max_id"] == missed["id"]
assert totals["first_created_at"] == hit["created_at"]
assert totals["last_created_at"] == missed["created_at"]
again = client.get("/datasets/orders/sensitive-identification-summary")
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


def test_summary_survives_a_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "sensitive-identification-summary.db"
    assert _run_script(db_path, _CREATE_SCRIPT) == "created"
    assert _run_script(db_path, _VERIFY_SCRIPT) == "verified"
