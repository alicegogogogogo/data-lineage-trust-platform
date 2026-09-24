"""Tests for sensitive field identification candidates.

Identifications are candidate annotations derived from field names and sample
values; they never register privacy policies. They persist across restarts.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def identifications_path(dataset: str = "users", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/sensitive-identifications"


def make_version(client: TestClient, fields: list[dict]) -> None:
    assert client.post("/datasets", json={"name": "users"}).status_code == 201
    response = client.post(
        "/datasets/users/versions", json={"fields": fields}
    )
    assert response.status_code == 201, response.text


def identify(
    client: TestClient, field: str, samples: list, *, dataset: str = "users",
    version: int = 1,
):
    return client.post(
        identifications_path(dataset, version),
        json={"field": field, "samples": samples},
    )


MIXED_FIELDS = [
    {"name": "user_email", "type": "string", "nullable": True},
    {"name": "contact_phone", "type": "string", "nullable": True},
    {"name": "id_card_no", "type": "string", "nullable": True},
    {"name": "password_hash", "type": "string", "nullable": True},
    {"name": "auth_token", "type": "string", "nullable": True},
    {"name": "date_of_birth", "type": "string", "nullable": True},
    {"name": "login_id", "type": "integer", "nullable": False},
    {"name": "note", "type": "string", "nullable": True},
]


# --------------------------------------------------------------------------- #
# Record shape and name hits
# --------------------------------------------------------------------------- #


def test_first_identification_returns_201_with_full_record(client: TestClient) -> None:
    make_version(client, MIXED_FIELDS)
    response = identify(client, "user_email", ["alice@example.com"])

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {
        "sequence",
        "dataset",
        "version",
        "field",
        "evidence",
        "confidence",
        "source",
        "created_at",
    }
    assert body["sequence"] == 1
    assert body["dataset"] == "users"
    assert body["version"] == 1
    assert body["field"] == "user_email"
    assert body["source"] == {
        "dataset": "users",
        "version": 1,
        "field": "user_email",
    }
    datetime.fromisoformat(body["created_at"])


def test_name_hit_is_case_insensitive_substring(client: TestClient) -> None:
    fields = [
        {"name": "USER_EMAIL_ADDR", "type": "string", "nullable": True},
        {"name": "PhoneLine", "type": "string", "nullable": True},
        {"name": "IDCARD", "type": "string", "nullable": True},
    ]
    make_version(client, fields)

    email = identify(client, "USER_EMAIL_ADDR", [])
    assert email.status_code == 201
    assert email.json()["evidence"] == [
        {"kind": "name", "category": "email", "value": None}
    ]
    assert email.json()["confidence"] == "low"

    phone = identify(client, "PhoneLine", [])
    assert phone.json()["evidence"] == [
        {"kind": "name", "category": "phone", "value": None}
    ]

    # "idcard" without the underscore is not the "id_card" sensitive word.
    idcard = identify(client, "IDCARD", [])
    assert idcard.json()["evidence"] == []
    assert idcard.json()["confidence"] == "none"


def test_every_sensitive_word_matches_as_substring(client: TestClient) -> None:
    make_version(client, MIXED_FIELDS)
    expected = {
        "id_card_no": "id_card",
        "password_hash": "password",
        "auth_token": "token",
        "date_of_birth": "birth",
    }
    for field, word in expected.items():
        body = identify(client, field, []).json()
        assert body["evidence"] == [{"kind": "name", "category": word, "value": None}]
        assert body["confidence"] == "low"


def test_multiple_name_hits_listed_in_canonical_word_order(
    client: TestClient,
) -> None:
    fields = [{"name": "phone_email_token", "type": "string", "nullable": True}]
    make_version(client, fields)
    body = identify(client, "phone_email_token", []).json()
    assert [item["category"] for item in body["evidence"]] == [
        "email",
        "phone",
        "token",
    ]
    assert all(item["kind"] == "name" for item in body["evidence"])


# --------------------------------------------------------------------------- #
# Sample hits
# --------------------------------------------------------------------------- #


def test_email_sample_hit_rules(client: TestClient) -> None:
    make_version(client, MIXED_FIELDS)
    valid = ["a@b.co", "alice@example.com", "x.y@sub.example.co"]
    body = identify(client, "note", valid).json()
    # One sample hit per category, carrying the first matching value.
    assert body["evidence"] == [
        {"kind": "sample", "category": "email", "value": "a@b.co"}
    ]
    assert body["confidence"] == "medium"

    for invalid in ["@b.co", "a@", "a@b", "a@@b.co", "plain", "a@b@c.d"]:
        response = identify(client, "note", [invalid])
        assert response.status_code == 200
        assert response.json()["confidence"] == "none", invalid
        assert response.json()["evidence"] == [], invalid


def test_phone_sample_hit_rules(client: TestClient) -> None:
    make_version(client, MIXED_FIELDS)
    body = identify(client, "note", ["13800138000"]).json()
    assert body["evidence"] == [
        {"kind": "sample", "category": "phone", "value": "13800138000"}
    ]
    assert body["confidence"] == "medium"

    for invalid in [
        "23800138000",  # does not start with 1
        "1380013800",   # 10 digits
        "138001380001",  # 12 digits
        "1380013800a",   # not all digits
    ]:
        response = identify(client, "note", [invalid])
        assert response.status_code == 200
        assert response.json()["confidence"] == "none", invalid


def test_email_and_phone_samples_both_hit_in_first_occurrence_order(
    client: TestClient,
) -> None:
    make_version(client, MIXED_FIELDS)
    body = identify(
        client, "note", ["13800138000", "alice@example.com"]
    ).json()
    assert [item["category"] for item in body["evidence"]] == ["phone", "email"]
    assert all(item["kind"] == "sample" for item in body["evidence"])

    swapped = identify(
        client, "user_email", ["alice@example.com", "13800138000"]
    ).json()
    assert [item["kind"] for item in swapped["evidence"]] == ["name", "sample", "sample"]
    assert swapped["confidence"] == "high"


def test_non_string_declared_types_never_use_samples(client: TestClient) -> None:
    make_version(client, MIXED_FIELDS)
    # Even an email-looking (or phone-looking) sample is ignored: only the
    # name is consulted for a field not declared as a string.
    body = identify(
        client, "login_id", ["alice@example.com", "13800138000"]
    ).json()
    assert body["evidence"] == []
    assert body["confidence"] == "none"


def test_non_string_samples_of_string_fields_are_ignored(
    client: TestClient,
) -> None:
    make_version(client, MIXED_FIELDS)
    body = identify(
        client,
        "note",
        [13800138000, 1, 1.5, True, None, "alice@example.com"],
    ).json()
    assert body["evidence"] == [
        {"kind": "sample", "category": "email", "value": "alice@example.com"}
    ]
    assert body["confidence"] == "medium"


def test_empty_samples_and_no_name_hit_still_creates_none_record(
    client: TestClient,
) -> None:
    make_version(client, MIXED_FIELDS)
    response = identify(client, "note", [])
    assert response.status_code == 201
    body = response.json()
    assert body["evidence"] == []
    assert body["confidence"] == "none"
    assert body["sequence"] == 1


def test_confidence_levels(client: TestClient) -> None:
    fields = [
        {"name": "email_field", "type": "string", "nullable": True},
        {"name": "plain_field", "type": "string", "nullable": True},
    ]
    make_version(client, fields)

    assert identify(client, "email_field", ["a@b.co"]).json()["confidence"] == "high"
    assert identify(client, "plain_field", ["a@b.co"]).json()["confidence"] == "medium"
    assert identify(client, "email_field", []).json()["confidence"] == "low"


# --------------------------------------------------------------------------- #
# Refresh semantics and ordering
# --------------------------------------------------------------------------- #


def test_rerun_same_field_refreshes_in_place_with_200(client: TestClient) -> None:
    make_version(client, MIXED_FIELDS)
    first = identify(client, "user_email", ["a@b.co"])
    assert first.status_code == 201
    assert first.json()["sequence"] == 1

    second = identify(client, "note", [])
    assert second.status_code == 201
    assert second.json()["sequence"] == 2

    rerun = identify(client, "user_email", [])
    assert rerun.status_code == 200, rerun.text
    body = rerun.json()
    assert body["sequence"] == 1
    assert body["evidence"] == [
        {"kind": "name", "category": "email", "value": None}
    ]
    assert body["confidence"] == "low"

    listed = client.get(identifications_path()).json()
    assert [item["field"] for item in listed] == ["user_email", "note"]
    assert [item["sequence"] for item in listed] == [1, 2]


def test_listing_is_sorted_by_sequence(client: TestClient) -> None:
    make_version(client, MIXED_FIELDS)
    for index, field in enumerate(
        ["date_of_birth", "auth_token", "password_hash", "note"], start=1
    ):
        assert identify(client, field, []).json()["sequence"] == index

    # Refresh the first one; the order must not change.
    assert identify(client, "date_of_birth", []).status_code == 200

    listed = client.get(identifications_path()).json()
    assert [item["sequence"] for item in listed] == [1, 2, 3, 4]
    assert [item["field"] for item in listed] == [
        "date_of_birth",
        "auth_token",
        "password_hash",
        "note",
    ]


def test_listing_empty_version_returns_empty_list(client: TestClient) -> None:
    make_version(client, MIXED_FIELDS)
    response = client.get(identifications_path())
    assert response.status_code == 200
    assert response.json() == []


def test_whitespace_field_is_matched_to_existing_field(client: TestClient) -> None:
    make_version(client, MIXED_FIELDS)
    response = identify(client, "  user_email ", [])
    assert response.status_code == 201, response.text
    assert response.json()["field"] == "user_email"


# --------------------------------------------------------------------------- #
# 404s
# --------------------------------------------------------------------------- #


def test_unknown_dataset_and_version_return_404(client: TestClient) -> None:
    make_version(client, MIXED_FIELDS)
    post_unknown_dataset = client.post(
        identifications_path("ghost"), json={"field": "note", "samples": []}
    )
    post_unknown_version = client.post(
        identifications_path("users", 9), json={"field": "note", "samples": []}
    )
    post_unknown_field = identify(client, "ghost", [])
    get_unknown_dataset = client.get(identifications_path("ghost"))
    get_unknown_version = client.get(identifications_path("users", 9))

    for response in (
        post_unknown_dataset,
        post_unknown_version,
        post_unknown_field,
        get_unknown_dataset,
        get_unknown_version,
    ):
        assert response.status_code == 404, response.text
        assert response.json()["error"] == "not_found"


def test_failed_post_writes_nothing(client: TestClient) -> None:
    make_version(client, MIXED_FIELDS)
    assert identify(client, "ghost", []).status_code == 404
    assert identify(client, "note", [{"x": 1}]).status_code == 422
    assert client.get(identifications_path()).json() == []


# --------------------------------------------------------------------------- #
# 422 validation
# --------------------------------------------------------------------------- #


def test_invalid_post_bodies_return_422(client: TestClient) -> None:
    make_version(client, MIXED_FIELDS)
    path = identifications_path()

    def status(payload: object) -> int:
        return client.post(path, json=payload).status_code

    # Missing fields.
    assert status({"samples": []}) == 422
    assert status({"field": "note"}) == 422
    assert status({}) == 422
    # Extra fields.
    assert status({"field": "note", "samples": [], "other": 1}) == 422
    # Field must be a non-blank string.
    assert status({"field": 9, "samples": []}) == 422
    assert status({"field": True, "samples": []}) == 422
    assert status({"field": None, "samples": []}) == 422
    assert status({"field": "   ", "samples": []}) == 422
    # Samples must be an array of scalars.
    assert status({"field": "note", "samples": {}}) == 422
    assert status({"field": "note", "samples": "x"}) == 422
    assert status({"field": "note", "samples": 7}) == 422
    assert status({"field": "note", "samples": [{}]}) == 422
    assert status({"field": "note", "samples": [[]]}) == 422
    assert status({"field": "note", "samples": [["a@b.co"]]}) == 422
    # Scalars are fine.
    assert status({"field": "note", "samples": ["a", 1, 1.5, True, None]}) == 201

    # Only the final valid request wrote one record; every 422 wrote nothing.
    listed = client.get(path).json()
    assert [item["field"] for item in listed] == ["note"]


def test_post_with_query_parameters_returns_422(client: TestClient) -> None:
    make_version(client, MIXED_FIELDS)
    response = client.post(
        identifications_path() + "?x=1", json={"field": "note", "samples": []}
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get(identifications_path()).json() == []


def test_unknown_path_404_takes_precedence_over_query_422(
    client: TestClient,
) -> None:
    make_version(client, MIXED_FIELDS)
    assert (
        client.post(
            identifications_path("ghost") + "?x=1",
            json={"field": "note", "samples": []},
        ).status_code
        == 404
    )
    assert (
        client.get(identifications_path("users", 9) + "?x=1").status_code == 404
    )


def test_get_rejects_body_and_query_parameters(client: TestClient) -> None:
    make_version(client, MIXED_FIELDS)
    identify(client, "note", [])

    with_query = client.get(identifications_path() + "?x=1")
    assert with_query.status_code == 422
    assert with_query.json()["error"] == "validation_error"

    with_body = client.request(
        "GET", identifications_path(), json={"unexpected": True}
    )
    assert with_body.status_code == 422
    assert with_body.json()["error"] == "validation_error"


def test_identification_does_not_register_privacy_policy(
    client: TestClient,
) -> None:
    make_version(client, MIXED_FIELDS)
    assert identify(client, "user_email", ["a@b.co"]).status_code == 201
    policies = client.get("/datasets/users/versions/1/privacy-policies")
    assert policies.status_code == 200
    assert policies.json() == []


# --------------------------------------------------------------------------- #
# Restart persistence
# --------------------------------------------------------------------------- #


CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
assert client.post("/datasets", json={"name": "users"}).status_code == 201
fields = [
    {"name": "user_email", "type": "string", "nullable": True},
    {"name": "mobile", "type": "string", "nullable": True},
    {"name": "login_id", "type": "integer", "nullable": False},
    {"name": "note", "type": "string", "nullable": True},
]
assert client.post(
    "/datasets/users/versions", json={"fields": fields}
).status_code == 201

def post(field, samples):
    response = client.post(
        "/datasets/users/versions/1/sensitive-identifications",
        json={"field": field, "samples": samples},
    )
    assert response.status_code in (200, 201), response.text
    return response

post("user_email", ["a@b.co"])
post("note", [])
post("mobile", ["13800138000"])
# Refresh in place: sequence stays 1.
rerun = post("user_email", [])
assert rerun.status_code == 200
assert rerun.json()["sequence"] == 1
print("created")
"""

VERIFY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
response = client.get("/datasets/users/versions/1/sensitive-identifications")
assert response.status_code == 200, response.text
records = response.json()
assert [r["sequence"] for r in records] == [1, 2, 3]
assert [r["field"] for r in records] == ["user_email", "note", "mobile"]
refreshed = records[0]
assert refreshed["confidence"] == "low"
assert refreshed["evidence"] == [
    {"kind": "name", "category": "email", "value": None}
]
assert refreshed["source"] == {
    "dataset": "users", "version": 1, "field": "user_email"
}
assert records[1]["confidence"] == "none"
assert records[1]["evidence"] == []
assert records[2]["confidence"] == "medium"
assert records[2]["evidence"] == [
    {"kind": "sample", "category": "phone", "value": "13800138000"}
]
# A rerun in the fresh process is still a 200 with a stable sequence.
again = client.post(
    "/datasets/users/versions/1/sensitive-identifications",
    json={"field": "mobile", "samples": ["13800138000", "a@b.co"]},
)
assert again.status_code == 200, again.text
assert again.json()["sequence"] == 3
assert [item["category"] for item in again.json()["evidence"]] == ["phone", "email"]
print(json.dumps({"sequences": [r["sequence"] for r in records]}))
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


def test_identifications_survive_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "identifications.db"
    assert _run(db_path, CREATE_SCRIPT) == "created"
    output = _run(db_path, VERIFY_SCRIPT)
    assert output  # fresh interpreter read the persisted records
