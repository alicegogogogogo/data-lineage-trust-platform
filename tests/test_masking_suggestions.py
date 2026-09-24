"""Tests for read-only masking-strategy suggestions over identifications."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_version(
    client: TestClient,
    dataset: str = "orders",
    fields: list[dict] | None = None,
) -> None:
    if fields is None:
        fields = [
            {"name": "id", "type": "integer", "nullable": False},
            {"name": "contact_email", "type": "string", "nullable": True},
            {"name": "mobile_phone", "type": "string", "nullable": True},
            {"name": "id_card_no", "type": "string", "nullable": True},
            {"name": "password_hash", "type": "string", "nullable": True},
            {"name": "api_token", "type": "string", "nullable": True},
            {"name": "date_of_birth", "type": "date", "nullable": True},
            {"name": "amount", "type": "decimal", "nullable": True},
        ]
    assert client.post("/datasets", json={"name": dataset}).status_code == 201
    response = client.post(
        f"/datasets/{dataset}/versions", json={"fields": fields}
    )
    assert response.status_code == 201, response.text


def ident_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/sensitive-identifications"


def suggestions_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{ident_path(dataset, version)}/masking-suggestions"


def identify(
    client: TestClient, field: str, samples: list | None = None, **path: object
):
    return client.post(
        ident_path(**path),  # type: ignore[arg-type]
        json={"field": field, "samples": [] if samples is None else samples},
    )


def suggestions(client: TestClient, **path: object):
    return client.get(suggestions_path(**path))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Candidate shape and classification
# --------------------------------------------------------------------------- #


def test_pii_fields_suggest_partial(client: TestClient) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])
    identify(client, "mobile_phone", ["13800138000"])
    identify(client, "id_card_no", [])
    # A non-string declared type still collects name evidence.
    identify(client, "date_of_birth", [])

    body = suggestions(client).json()
    assert {item["field"] for item in body} == {
        "contact_email",
        "mobile_phone",
        "id_card_no",
        "date_of_birth",
    }
    for item in body:
        assert set(item) == {"field", "classification", "masking", "allowed_roles"}
        assert item["classification"] == "PII"
        assert item["masking"] == "partial"
        assert item["allowed_roles"] == []


def test_sample_only_hits_are_pii(client: TestClient) -> None:
    make_version(
        client,
        fields=[{"name": "label", "type": "string", "nullable": True}],
    )
    identify(client, "label", ["13800138000", "plain"])
    [item] = suggestions(client).json()
    assert item == {
        "field": "label",
        "classification": "PII",
        "masking": "partial",
        "allowed_roles": [],
    }

    identify(client, "label", ["alice@example.com"])
    [item] = suggestions(client).json()
    assert item["classification"] == "PII"
    assert item["masking"] == "partial"


def test_credential_fields_suggest_redact_with_empty_roles(client: TestClient) -> None:
    make_version(client)
    identify(client, "password_hash", [])
    identify(client, "api_token", ["not sensitive looking"])

    body = suggestions(client).json()
    assert {item["field"] for item in body} == {"password_hash", "api_token"}
    for item in body:
        assert item["classification"] == "CREDENTIAL"
        assert item["masking"] == "redact"
        # An empty list means every role is masked, not that no role is.
        assert item["allowed_roles"] == []


def test_both_pii_and_credential_hits_stay_pii_partial(client: TestClient) -> None:
    make_version(
        client,
        fields=[{"name": "token_email", "type": "string", "nullable": True}],
    )
    identify(client, "token_email", [])
    [item] = suggestions(client).json()
    assert item == {
        "field": "token_email",
        "classification": "PII",
        "masking": "partial",
        "allowed_roles": [],
    }


# --------------------------------------------------------------------------- #
# Records without hits never produce candidates
# --------------------------------------------------------------------------- #


def test_records_without_any_hit_are_omitted(client: TestClient) -> None:
    make_version(client)
    # Both produce "none" records (no name word, no matching sample).
    identify(client, "amount", [])
    identify(client, "id", ["13800138000"])

    assert suggestions(client).json() == []
    # The records themselves still exist, unchanged, on the identification list.
    records = client.get(ident_path()).json()
    assert {record["field"] for record in records} == {"amount", "id"}
    assert all(record["confidence"] == "none" for record in records)


def test_version_without_records_returns_empty_list_not_error(
    client: TestClient,
) -> None:
    make_version(client)
    response = suggestions(client)
    assert response.status_code == 200
    assert response.json() == []


def test_mix_of_hit_and_unhit_records_only_lists_hits(client: TestClient) -> None:
    make_version(
        client,
        fields=[
            {"name": "label", "type": "string", "nullable": True},
            {"name": "email_addr", "type": "string", "nullable": True},
            {"name": "code", "type": "integer", "nullable": True},
        ],
    )
    identify(client, "label", ["nothing here"])
    identify(client, "email_addr", [])
    identify(client, "code", [])
    [item] = suggestions(client).json()
    assert item["field"] == "email_addr"
    assert item["classification"] == "PII"


# --------------------------------------------------------------------------- #
# Ordering follows identification record id ascending
# --------------------------------------------------------------------------- #


def test_candidates_follow_identification_id_order(client: TestClient) -> None:
    make_version(client)
    # Submission order deliberately differs from alphabetical field order.
    submission_order = ["mobile_phone", "contact_email", "api_token"]
    for field_name in submission_order:
        assert identify(client, field_name).status_code == 201

    body = suggestions(client).json()
    assert [item["field"] for item in body] == submission_order
    # Alphabetical would start with api_token; id order does not.
    assert submission_order != sorted(submission_order)

    # Refreshing the first record in place must not move its candidate.
    assert identify(client, "mobile_phone", ["13800138000"]).status_code == 200
    assert [item["field"] for item in suggestions(client).json()] == submission_order


# --------------------------------------------------------------------------- #
# Recomputed on every read; never written, never registered
# --------------------------------------------------------------------------- #


def test_refresh_recomputes_the_candidate(client: TestClient) -> None:
    make_version(
        client,
        fields=[{"name": "password_hash", "type": "string", "nullable": True}],
    )
    identify(client, "password_hash", [])
    [item] = suggestions(client).json()
    assert item["classification"] == "CREDENTIAL"
    assert item["masking"] == "redact"

    # A PII sample hit added on refresh makes PII win.
    identify(client, "password_hash", ["13800138000"])
    [item] = suggestions(client).json()
    assert item["classification"] == "PII"
    assert item["masking"] == "partial"

    # And dropping the sample hit on the next refresh restores CREDENTIAL.
    identify(client, "password_hash", [])
    [item] = suggestions(client).json()
    assert item["classification"] == "CREDENTIAL"
    assert item["masking"] == "redact"


def test_reads_write_nothing_and_register_no_policy(client: TestClient) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])
    before = client.get(ident_path()).json()

    for _ in range(3):
        assert suggestions(client).status_code == 200

    after = client.get(ident_path()).json()
    assert after == before
    # Suggestions never auto-register a privacy policy.
    policies = client.get(
        "/datasets/orders/versions/1/privacy-policies"
    ).json()
    assert policies == []


def test_view_still_masks_only_registered_policies(client: TestClient) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])
    assert suggestions(client).json()[0]["field"] == "contact_email"

    # No policy registered: the advisory candidate must not mask the view.
    response = client.post(
        "/datasets/orders/versions/1/privacy-policies/view",
        json={
            "role": "guest",
            "rows": [{"contact_email": "alice@example.com"}],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == [{"contact_email": "alice@example.com"}]


# --------------------------------------------------------------------------- #
# Errors and precedence
# --------------------------------------------------------------------------- #


def test_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])

    assert suggestions(client, dataset="ghost").status_code == 404
    assert suggestions(client, version=9).status_code == 404
    for response in (
        suggestions(client, dataset="ghost"),
        suggestions(client, version=9),
    ):
        assert response.json()["error"] == "not_found"
        assert set(response.json()) == {"error", "detail"}


def test_body_and_query_parameters_are_422_without_writes(client: TestClient) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])

    with_body = client.request("GET", suggestions_path(), content=b"{}")
    with_query = client.get(suggestions_path(), params={"x": "1"})
    assert with_body.status_code == 422
    assert with_query.status_code == 422
    assert with_body.json()["error"] == "validation_error"
    assert set(with_body.json()) == {"error", "detail"}

    # 404 takes precedence over the request-shape checks.
    assert (
        client.request("GET", suggestions_path("ghost"), content=b"{}").status_code
        == 404
    )
    assert (
        client.get(suggestions_path("ghost"), params={"x": "1"}).status_code == 404
    )

    # The rejected reads changed nothing.
    assert len(suggestions(client).json()) == 1
    assert len(client.get(ident_path()).json()) == 1


def test_post_is_not_allowed_on_the_suggestions_path(client: TestClient) -> None:
    make_version(client)
    # The sub-resource is read-only; a POST is a routing 405 rather than a
    # create, and it must never be mistaken for an identification submission.
    response = client.post(suggestions_path(), json={})
    assert response.status_code == 405
    assert client.get(ident_path()).json() == []


# --------------------------------------------------------------------------- #
# Persistence and determinism across a process restart
# --------------------------------------------------------------------------- #


CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response, *allowed):
    assert response.status_code in allowed, response.text

ok(client.post("/datasets", json={"name": "orders"}), 201)
ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [
        {"name": "mobile_phone", "type": "string", "nullable": True},
        {"name": "contact_email", "type": "string", "nullable": True},
        {"name": "api_token", "type": "string", "nullable": True},
        {"name": "label", "type": "string", "nullable": True},
    ]},
), 201)
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "mobile_phone", "samples": ["13800138000"]},
), 201)
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "contact_email", "samples": ["alice@example.com"]},
), 201)
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "api_token", "samples": []},
), 201)
# A no-hit record that must not appear in the suggestions.
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "label", "samples": ["plain"]},
), 201)
print("created")
"""

VERIFY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

response = client.get(
    "/datasets/orders/versions/1/sensitive-identifications/masking-suggestions"
)
assert response.status_code == 200, response.text
suggestions = response.json()
assert [item["field"] for item in suggestions] == [
    "mobile_phone", "contact_email", "api_token",
]
assert suggestions[0] == {
    "field": "mobile_phone", "classification": "PII",
    "masking": "partial", "allowed_roles": [],
}
assert suggestions[1] == {
    "field": "contact_email", "classification": "PII",
    "masking": "partial", "allowed_roles": [],
}
assert suggestions[2] == {
    "field": "api_token", "classification": "CREDENTIAL",
    "masking": "redact", "allowed_roles": [],
}
# A refresh after restart is reflected on the next read, with no new record.
rerun = client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "label", "samples": ["alice@example.com"]},
)
assert rerun.status_code == 200, rerun.text
refreshed = client.get(
    "/datasets/orders/versions/1/sensitive-identifications/masking-suggestions"
).json()
assert [item["field"] for item in refreshed] == [
    "mobile_phone", "contact_email", "api_token", "label",
]
assert refreshed[3]["classification"] == "PII"
assert refreshed[3]["masking"] == "partial"
# Still no auto-registered policy.
assert client.get("/datasets/orders/versions/1/privacy-policies").json() == []
print(json.dumps({"count": len(refreshed)}))
"""


def _run(db_path: Path, script: str) -> str:
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


def test_suggestions_survive_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "suggestions.db"
    assert _run(db_path, CREATE_SCRIPT) == "created"
    assert json.loads(_run(db_path, VERIFY_SCRIPT))["count"] == 4
