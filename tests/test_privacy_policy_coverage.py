"""Tests for the read-only cross-version privacy policy coverage check."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

COVERAGE_PATH = "/datasets/orders/privacy-policy-coverage"

BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "email", "type": "string", "nullable": True},
    {"name": "ssn", "type": "string", "nullable": True},
]

TOP_LEVEL_KEYS = ["dataset", "versions", "totals"]
VERSION_KEYS = ["version", "fields", "candidates"]
FIELD_KEYS = ["field", "coverage", "classification", "masking", "enabled"]
CANDIDATE_KEYS = ["field", "classification", "masking"]
TOTAL_KEYS = [
    "version_count",
    "field_count",
    "enabled_count",
    "disabled_count",
    "unregistered_count",
    "candidate_count",
]


def make_dataset(
    client: TestClient, name: str = "orders", fields: list[dict] | None = None
) -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    if fields is not None:
        response = client.post(
            f"/datasets/{name}/versions", json={"fields": fields}
        )
        assert response.status_code == 201, response.text


def add_version(client: TestClient, fields: list[dict], dataset: str = "orders") -> int:
    response = client.post(
        f"/datasets/{dataset}/versions", json={"fields": fields}
    )
    assert response.status_code == 201, response.text
    return response.json()["version"]


def policies_path(version: int, dataset: str = "orders") -> str:
    return f"/datasets/{dataset}/versions/{version}/privacy-policies"


def create_policy(
    client: TestClient, version: int, payload: dict, dataset: str = "orders"
) -> dict:
    response = client.post(policies_path(version, dataset), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def identifications_path(version: int, dataset: str = "orders") -> str:
    return f"/datasets/{dataset}/versions/{version}/sensitive-identifications"


def identify(
    client: TestClient,
    version: int,
    field: str,
    samples: list | None = None,
    dataset: str = "orders",
) -> dict:
    response = client.post(
        identifications_path(version, dataset),
        json={"field": field, "samples": samples or []},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def get_coverage(client: TestClient, dataset: str = "orders") -> dict:
    response = client.get(f"/datasets/{dataset}/privacy-policy-coverage")
    assert response.status_code == 200, response.text
    return response.json()


def coverage_response(client: TestClient, dataset: str = "orders"):
    response = client.get(f"/datasets/{dataset}/privacy-policy-coverage")
    assert response.status_code == 200, response.text
    return response


# --------------------------------------------------------------------------- #
# Empty state and wire format
# --------------------------------------------------------------------------- #


def test_coverage_without_versions_is_empty_not_an_error(client: TestClient) -> None:
    make_dataset(client)
    body = get_coverage(client)
    assert list(body) == TOP_LEVEL_KEYS
    assert body["dataset"] == "orders"
    assert body["versions"] == []
    assert body["totals"] == {
        "version_count": 0,
        "field_count": 0,
        "enabled_count": 0,
        "disabled_count": 0,
        "unregistered_count": 0,
        "candidate_count": 0,
    }


def test_coverage_is_get_only(client: TestClient) -> None:
    make_dataset(client)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, COVERAGE_PATH)
        assert response.status_code == 405, method


def test_coverage_wire_format_is_compact_with_fixed_key_order_and_newline(
    client: TestClient,
) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    create_policy(
        client, 1,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": ["analyst"]},
    )
    identify(client, 1, "ssn", ["alice@example.com"])

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
    # Lower-case booleans and nulls.
    assert '"enabled":true' in text
    assert '"enabled":null' in text
    assert "True" not in text
    assert "None" not in text

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
    assert list(version_entry["candidates"][0]) == CANDIDATE_KEYS


# --------------------------------------------------------------------------- #
# Field coverage states and ordering
# --------------------------------------------------------------------------- #


def test_coverage_fields_sort_by_name_and_report_all_three_states(
    client: TestClient,
) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    enabled_policy = create_policy(
        client, 1,
        {"field": "ssn", "classification": "secret", "masking": "partial",
         "allowed_roles": ["auditor"]},
    )
    disabled_policy = create_policy(
        client, 1,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert client.patch(
        policies_path(1) + f"/{disabled_policy['id']}", json={"enabled": False}
    ).status_code == 200

    fields = get_coverage(client)["versions"][0]["fields"]
    assert [entry["field"] for entry in fields] == ["email", "id", "ssn"]
    by_name = {entry["field"]: entry for entry in fields}

    disabled = by_name["email"]
    assert disabled["coverage"] == "disabled"
    assert disabled["classification"] == "PII"
    assert disabled["masking"] == "redact"
    assert disabled["enabled"] is False

    unregistered = by_name["id"]
    assert unregistered["coverage"] == "unregistered"
    assert unregistered["classification"] is None
    assert unregistered["masking"] is None
    assert unregistered["enabled"] is None

    enabled = by_name["ssn"]
    assert enabled["coverage"] == "enabled"
    assert enabled["classification"] == "secret"
    assert enabled["masking"] == "partial"
    assert enabled["enabled"] is True
    assert enabled_policy["field"] == "ssn"


def test_coverage_versions_sort_ascending_and_cover_every_version(
    client: TestClient,
) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    add_version(client, [{"name": "email", "type": "string", "nullable": True}])
    add_version(client, [{"name": "id", "type": "integer", "nullable": False}])

    body = get_coverage(client)
    assert [entry["version"] for entry in body["versions"]] == [1, 2, 3]
    for entry in body["versions"]:
        assert list(entry) == VERSION_KEYS
    assert [f["field"] for f in body["versions"][1]["fields"]] == ["email"]
    assert body["versions"][2]["fields"][0]["coverage"] == "unregistered"


# --------------------------------------------------------------------------- #
# Candidates
# --------------------------------------------------------------------------- #


def test_coverage_candidates_follow_identification_id_order(
    client: TestClient,
) -> None:
    make_dataset(
        client,
        fields=[
            {"name": "contact_token", "type": "string", "nullable": True},
            {"name": "contact_email", "type": "string", "nullable": True},
            {"name": "nickname", "type": "string", "nullable": True},
        ],
    )
    # Identified in this order; the candidate list keeps the record id order,
    # not the field-name order.
    token_record = identify(client, 1, "contact_token")
    email_record = identify(client, 1, "contact_email", ["alice@example.com"])

    candidates = get_coverage(client)["versions"][0]["candidates"]
    assert [c["field"] for c in candidates] == ["contact_token", "contact_email"]
    assert token_record["id"] < email_record["id"]
    by_field = {c["field"]: c for c in candidates}
    assert by_field["contact_token"] == {
        "field": "contact_token",
        "classification": "CREDENTIAL",
        "masking": "redact",
    }
    assert by_field["contact_email"] == {
        "field": "contact_email",
        "classification": "PII",
        "masking": "partial",
    }


def test_coverage_candidates_skip_unhit_and_policy_bearing_fields(
    client: TestClient,
) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    # A hit-bearing identification whose field already has a policy: no
    # candidate.
    identify(client, 1, "email", ["alice@example.com"])
    create_policy(
        client, 1,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    # An identification whose name and samples both missed: no candidate.
    identify(client, 1, "id", [123])

    entry = get_coverage(client)["versions"][0]
    assert entry["candidates"] == []
    assert entry["fields"][0]["coverage"] == "enabled"

    # A later hit-bearing identification of an uncovered field appears.
    identify(client, 1, "ssn", ["13800000000"])
    candidates = get_coverage(client)["versions"][0]["candidates"]
    assert candidates == [
        {"field": "ssn", "classification": "PII", "masking": "partial"}
    ]


def test_coverage_candidates_drop_once_a_policy_is_registered(
    client: TestClient,
) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    identify(client, 1, "email", ["alice@example.com"])
    assert len(get_coverage(client)["versions"][0]["candidates"]) == 1

    create_policy(
        client, 1,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": []},
    )
    entry = get_coverage(client)["versions"][0]
    assert entry["candidates"] == []
    assert entry["fields"][0]["field"] == "email"
    assert entry["fields"][0]["coverage"] == "enabled"


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #


def test_coverage_totals_equal_the_sum_of_the_version_entries(
    client: TestClient,
) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    create_policy(
        client, 1,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    identify(client, 1, "ssn", ["alice@example.com"])

    add_version(
        client,
        [
            {"name": "phone", "type": "string", "nullable": True},
            {"name": "note", "type": "string", "nullable": True},
        ],
    )
    disabled = create_policy(
        client, 2,
        {"field": "phone", "classification": "PII", "masking": "partial",
         "allowed_roles": []},
    )
    assert client.patch(
        policies_path(2) + f"/{disabled['id']}", json={"enabled": False}
    ).status_code == 200
    identify(client, 2, "note", ["13800000000"])

    body = get_coverage(client)
    versions = body["versions"]
    totals = body["totals"]
    assert totals["version_count"] == len(versions)
    assert totals["field_count"] == sum(len(v["fields"]) for v in versions)
    for state, key in (
        ("enabled", "enabled_count"),
        ("disabled", "disabled_count"),
        ("unregistered", "unregistered_count"),
    ):
        assert totals[key] == sum(
            1 for v in versions for f in v["fields"] if f["coverage"] == state
        )
    assert totals["candidate_count"] == sum(len(v["candidates"]) for v in versions)
    assert totals == {
        "version_count": 2,
        "field_count": 5,
        "enabled_count": 1,
        "disabled_count": 1,
        "unregistered_count": 3,
        "candidate_count": 2,
    }


# --------------------------------------------------------------------------- #
# Errors: 404 / 422, no writes
# --------------------------------------------------------------------------- #


def test_coverage_unknown_dataset_returns_404(client: TestClient) -> None:
    response = client.get("/datasets/ghost/privacy-policy-coverage")
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "not_found"


def test_coverage_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset(client)
    before = coverage_response(client).text

    with_body = client.request("GET", COVERAGE_PATH, content=b"{}")
    with_query = client.get(COVERAGE_PATH, params={"limit": 1})
    assert with_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, with_query):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]

    # The rejections wrote nothing.
    assert coverage_response(client).text == before


def test_coverage_shape_errors_keep_404_precedence(client: TestClient) -> None:
    make_dataset(client)
    assert client.get(
        "/datasets/ghost/privacy-policy-coverage", params={"x": "1"}
    ).status_code == 404
    assert (
        client.request(
            "GET", "/datasets/ghost/privacy-policy-coverage", content=b"{}"
        ).status_code
        == 404
    )


def test_coverage_is_strictly_read_only(client: TestClient) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    create_policy(
        client, 1,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    identify(client, 1, "ssn", ["alice@example.com"])
    policies_before = client.get(policies_path(1)).json()
    identifications_before = client.get(identifications_path(1)).json()
    first_text = coverage_response(client).text

    for _ in range(3):
        response = coverage_response(client)
        assert response.text == first_text
    assert client.get(policies_path(1)).json() == policies_before
    assert client.get(identifications_path(1)).json() == identifications_before


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
        {"name": "email", "type": "string", "nullable": True},
        {"name": "ssn", "type": "string", "nullable": True},
    ]},
).status_code == 201
assert client.post(
    "/datasets/orders/versions/1/privacy-policies",
    json={
        "field": "email",
        "classification": "PII",
        "masking": "redact",
        "allowed_roles": [],
    },
).status_code == 201
identified = client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "ssn", "samples": ["alice@example.com"]},
)
assert identified.status_code == 201, identified.text
print("created")
"""

_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

response = client.get("/datasets/orders/privacy-policy-coverage")
assert response.status_code == 200, response.text
assert response.text.endswith("\\n")
body = response.json()
assert list(body) == ["dataset", "versions", "totals"]
assert body["dataset"] == "orders"
assert [v["version"] for v in body["versions"]] == [1]
entry = body["versions"][0]
assert list(entry) == ["version", "fields", "candidates"]
assert [f["field"] for f in entry["fields"]] == ["email", "ssn"]
email, ssn = entry["fields"]
assert email["coverage"] == "enabled"
assert email["classification"] == "PII"
assert email["masking"] == "redact"
assert email["enabled"] is True
assert ssn["coverage"] == "unregistered"
assert ssn["classification"] is None
assert ssn["masking"] is None
assert ssn["enabled"] is None
assert entry["candidates"] == [
    {"field": "ssn", "classification": "PII", "masking": "partial"}
]
assert body["totals"] == {
    "version_count": 1,
    "field_count": 2,
    "enabled_count": 1,
    "disabled_count": 0,
    "unregistered_count": 1,
    "candidate_count": 1,
}
again = client.get("/datasets/orders/privacy-policy-coverage")
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
    db_path = tmp_path / "privacy-policy-coverage.db"
    assert _run_script(db_path, _CREATE_SCRIPT) == "created"
    assert _run_script(db_path, _VERIFY_SCRIPT) == "verified"
