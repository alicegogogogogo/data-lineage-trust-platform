"""Tests for the read-only cross-version privacy policy coverage check."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

COVERAGE_PATH = "/datasets/orders/privacy-policy-coverage"

FIELDS_V1 = [
    {"name": "email", "type": "string", "nullable": True},
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "note", "type": "string", "nullable": True},
]
FIELDS_V2 = FIELDS_V1 + [
    {"name": "password_hash", "type": "string", "nullable": True},
]

TOP_LEVEL_KEYS = ["dataset", "versions", "totals"]
VERSION_KEYS = ["version", "fields", "candidates"]
FIELD_KEYS = ["field", "status", "classification", "masking", "enabled"]
CANDIDATE_KEYS = ["field", "classification", "masking"]
TOTAL_KEYS = [
    "version_count",
    "field_count",
    "enabled_count",
    "disabled_count",
    "unregistered_count",
    "candidate_count",
]
ZERO_TOTALS = {
    "version_count": 0,
    "field_count": 0,
    "enabled_count": 0,
    "disabled_count": 0,
    "unregistered_count": 0,
    "candidate_count": 0,
}


def make_dataset(client: TestClient, name: str = "orders") -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201


def add_version(
    client: TestClient, fields: list[dict], dataset: str = "orders"
) -> int:
    response = client.post(f"/datasets/{dataset}/versions", json={"fields": fields})
    assert response.status_code == 201, response.text
    return response.json()["version"]


def create_policy(
    client: TestClient,
    version: int,
    field: str,
    dataset: str = "orders",
    classification: str = "PII",
    masking: str = "partial",
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/privacy-policies",
        json={
            "field": field,
            "classification": classification,
            "masking": masking,
            "allowed_roles": [],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def set_policy_enabled(
    client: TestClient,
    version: int,
    policy_id: int,
    enabled: bool,
    dataset: str = "orders",
) -> None:
    response = client.patch(
        f"/datasets/{dataset}/versions/{version}/privacy-policies/{policy_id}",
        json={"enabled": enabled},
    )
    assert response.status_code == 200, response.text


def identify(
    client: TestClient,
    version: int,
    field: str,
    samples: list | None = None,
    dataset: str = "orders",
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/sensitive-identifications",
        json={"field": field, "samples": samples or []},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def get_coverage(client: TestClient, dataset: str = "orders") -> dict:
    response = client.get(f"/datasets/{dataset}/privacy-policy-coverage")
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Empty state and wire format
# --------------------------------------------------------------------------- #


def test_dataset_without_versions_returns_empty_lists_and_zero_totals(
    client: TestClient,
) -> None:
    make_dataset(client)
    response = client.get(COVERAGE_PATH)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "dataset": "orders",
        "versions": [],
        "totals": ZERO_TOTALS,
    }


def test_response_is_compact_json_with_one_trailing_newline(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client, FIELDS_V1)
    response = client.get(COVERAGE_PATH)
    assert response.status_code == 200
    body = response.content.decode("utf-8")
    assert body.endswith("\n")
    assert not body.endswith("\n\n")
    assert body == json.dumps(response.json(), separators=(",", ":")) + "\n"


def test_key_order_is_fixed(client: TestClient) -> None:
    make_dataset(client)
    version = add_version(client, FIELDS_V1)
    policy = create_policy(client, version, "email")
    set_policy_enabled(client, version, policy["id"], False)
    identify(client, version, "note", ["alice@example.com"])

    payload = client.get(COVERAGE_PATH).json()
    assert list(payload.keys()) == TOP_LEVEL_KEYS
    assert list(payload["totals"].keys()) == TOTAL_KEYS
    (version,) = payload["versions"]
    assert list(version.keys()) == VERSION_KEYS
    for field in version["fields"]:
        assert list(field.keys()) == FIELD_KEYS
    for candidate in version["candidates"]:
        assert list(candidate.keys()) == CANDIDATE_KEYS


# --------------------------------------------------------------------------- #
# Coverage statuses
# --------------------------------------------------------------------------- #


def test_fields_cover_all_three_statuses_and_sort_by_name(
    client: TestClient,
) -> None:
    make_dataset(client)
    version = add_version(client, FIELDS_V1)
    create_policy(client, version, "email")
    note_policy = create_policy(
        client, version, "note", classification="INTERNAL", masking="redact"
    )
    set_policy_enabled(client, version, note_policy["id"], False)

    payload = get_coverage(client)
    (version_entry,) = payload["versions"]
    assert version_entry["version"] == 1
    assert version_entry["fields"] == [
        {
            "field": "email",
            "status": "enabled",
            "classification": "PII",
            "masking": "partial",
            "enabled": True,
        },
        {
            "field": "id",
            "status": "unregistered",
            "classification": None,
            "masking": None,
            "enabled": None,
        },
        {
            "field": "note",
            "status": "disabled",
            "classification": "INTERNAL",
            "masking": "redact",
            "enabled": False,
        },
    ]
    assert version_entry["candidates"] == []
    assert payload["totals"] == {
        "version_count": 1,
        "field_count": 3,
        "enabled_count": 1,
        "disabled_count": 1,
        "unregistered_count": 1,
        "candidate_count": 0,
    }


def test_versions_are_ordered_by_version_number(client: TestClient) -> None:
    make_dataset(client)
    add_version(client, FIELDS_V1)
    add_version(client, FIELDS_V2)
    payload = get_coverage(client)
    assert [entry["version"] for entry in payload["versions"]] == [1, 2]
    assert payload["totals"]["version_count"] == 2
    assert payload["totals"]["field_count"] == len(FIELDS_V1) + len(FIELDS_V2)


def test_disabling_a_policy_flips_the_status_on_the_next_read(
    client: TestClient,
) -> None:
    make_dataset(client)
    version = add_version(client, FIELDS_V1)
    policy = create_policy(client, version, "email")

    fields = get_coverage(client)["versions"][0]["fields"]
    assert fields[0]["status"] == "enabled"

    set_policy_enabled(client, version, policy["id"], False)
    fields = get_coverage(client)["versions"][0]["fields"]
    assert fields[0]["status"] == "disabled"
    assert fields[0]["enabled"] is False

    set_policy_enabled(client, version, policy["id"], True)
    fields = get_coverage(client)["versions"][0]["fields"]
    assert fields[0]["status"] == "enabled"


# --------------------------------------------------------------------------- #
# Candidates
# --------------------------------------------------------------------------- #


def test_candidates_follow_identification_id_order_and_skip_unhit_records(
    client: TestClient,
) -> None:
    make_dataset(client)
    version = add_version(client, FIELDS_V2)
    # Identified in this order: a hit-less record (no candidate), a name hit
    # and a sample-only hit.
    identify(client, version, "id")
    identify(client, version, "password_hash")
    identify(client, version, "note", ["alice@example.com"])

    payload = get_coverage(client)
    candidates = payload["versions"][0]["candidates"]
    assert candidates == [
        {"field": "password_hash", "classification": "CREDENTIAL", "masking": "redact"},
        {"field": "note", "classification": "PII", "masking": "partial"},
    ]
    assert payload["totals"]["candidate_count"] == 2


def test_fields_with_a_policy_never_appear_as_candidates(
    client: TestClient,
) -> None:
    make_dataset(client)
    version = add_version(client, FIELDS_V1)
    identify(client, version, "email")
    assert get_coverage(client)["versions"][0]["candidates"] == [
        {"field": "email", "classification": "PII", "masking": "partial"}
    ]

    policy = create_policy(client, version, "email")
    set_policy_enabled(client, version, policy["id"], False)
    payload = get_coverage(client)
    assert payload["versions"][0]["candidates"] == []
    assert payload["totals"]["candidate_count"] == 0
    # The registered field is reported by coverage status instead.
    assert payload["versions"][0]["fields"][0]["status"] == "disabled"


def test_candidates_are_recomputed_after_an_identification_refresh(
    client: TestClient,
) -> None:
    make_dataset(client)
    version = add_version(client, FIELDS_V1)
    # ``note`` hits only through its sample; ``id`` never hits.
    identify(client, version, "note", ["alice@example.com"])
    assert get_coverage(client)["versions"][0]["candidates"] == [
        {"field": "note", "classification": "PII", "masking": "partial"}
    ]
    # A refresh that loses the hit removes the candidate on the next read.
    identify(client, version, "note", [])
    assert get_coverage(client)["versions"][0]["candidates"] == []


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #


def test_totals_sum_the_per_version_values(client: TestClient) -> None:
    make_dataset(client)
    version_one = add_version(client, FIELDS_V1)
    version_two = add_version(client, FIELDS_V2)
    create_policy(client, version_one, "email")
    disabled = create_policy(client, version_two, "email")
    set_policy_enabled(client, version_two, disabled["id"], False)
    identify(client, version_two, "password_hash")

    payload = get_coverage(client)
    totals = payload["totals"]
    assert totals == {
        "version_count": 2,
        "field_count": len(FIELDS_V1) + len(FIELDS_V2),
        "enabled_count": 1,
        "disabled_count": 1,
        "unregistered_count": len(FIELDS_V1) + len(FIELDS_V2) - 2,
        "candidate_count": 1,
    }
    # Each counter is exactly the sum over the version entries.
    versions = payload["versions"]
    assert totals["version_count"] == len(versions)
    assert totals["field_count"] == sum(len(v["fields"]) for v in versions)
    for key, status in (
        ("enabled_count", "enabled"),
        ("disabled_count", "disabled"),
        ("unregistered_count", "unregistered"),
    ):
        assert totals[key] == sum(
            1 for v in versions for f in v["fields"] if f["status"] == status
        )
    assert totals["candidate_count"] == sum(len(v["candidates"]) for v in versions)


# --------------------------------------------------------------------------- #
# Read-only determinism
# --------------------------------------------------------------------------- #


def test_repeated_reads_return_identical_bytes_and_write_nothing(
    client: TestClient,
) -> None:
    make_dataset(client)
    version = add_version(client, FIELDS_V1)
    create_policy(client, version, "email")
    identify(client, version, "note", ["alice@example.com"])

    first = client.get(COVERAGE_PATH)
    second = client.get(COVERAGE_PATH)
    assert first.status_code == 200
    assert first.content == second.content

    # Nothing was written: the policies and identification records are
    # unchanged and no new ones appeared.
    policies = client.get(
        f"/datasets/orders/versions/{version}/privacy-policies"
    ).json()
    assert len(policies) == 1
    identifications = client.get(
        f"/datasets/orders/versions/{version}/sensitive-identifications"
    ).json()
    assert len(identifications) == 1


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def test_unknown_dataset_is_404_with_error_detail_shape(client: TestClient) -> None:
    response = client.get("/datasets/unknown/privacy-policy-coverage")
    assert response.status_code == 404
    payload = response.json()
    assert set(payload.keys()) == {"error", "detail"}
    assert "unknown" in payload["detail"]


def test_unknown_dataset_takes_precedence_over_request_shape(
    client: TestClient,
) -> None:
    with_body = client.request("GET", "/datasets/unknown/privacy-policy-coverage", content=b"{}")
    assert with_body.status_code == 404
    with_query = client.get("/datasets/unknown/privacy-policy-coverage?x=1")
    assert with_query.status_code == 404


def test_request_body_is_422(client: TestClient) -> None:
    make_dataset(client)
    response = client.request("GET", COVERAGE_PATH, content=b"{}")
    assert response.status_code == 422
    payload = response.json()
    assert set(payload.keys()) == {"error", "detail"}


def test_query_parameter_is_422(client: TestClient) -> None:
    make_dataset(client)
    response = client.get(COVERAGE_PATH + "?verbose=true")
    assert response.status_code == 422
    payload = response.json()
    assert set(payload.keys()) == {"error", "detail"}


def test_rejected_requests_write_nothing(client: TestClient) -> None:
    make_dataset(client)
    add_version(client, FIELDS_V1)
    before = client.get(COVERAGE_PATH).content
    assert client.request("GET", COVERAGE_PATH, content=b"{}").status_code == 422
    assert client.get(COVERAGE_PATH + "?x=1").status_code == 422
    assert client.get(COVERAGE_PATH).content == before
