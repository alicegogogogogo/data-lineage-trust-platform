"""Tests for sensitive-field identification candidate annotations."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
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


def identify(
    client: TestClient, field: str, samples: list | None = None, **path: object
):
    return client.post(
        ident_path(**path),  # type: ignore[arg-type]
        json={"field": field, "samples": [] if samples is None else samples},
    )


# --------------------------------------------------------------------------- #
# Record shape and the two hit kinds
# --------------------------------------------------------------------------- #


def test_first_submission_returns_201_stable_record(client: TestClient) -> None:
    make_version(client)
    response = identify(client, "contact_email", ["alice@example.com"])

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {
        "id",
        "field",
        "field_type",
        "evidence",
        "confidence",
        "source",
        "created_at",
    }
    assert isinstance(body["id"], int)
    assert body["field"] == "contact_email"
    assert body["field_type"] == "string"
    assert body["evidence"] == ["name:email", "sample:email"]
    assert body["confidence"] == "high"
    assert body["source"] == {
        "dataset": "orders",
        "version": 1,
        "field": "contact_email",
    }
    datetime.fromisoformat(body["created_at"])
    # Samples are never echoed back.
    assert "samples" not in body


def test_name_hits_cover_all_words_in_fixed_order(client: TestClient) -> None:
    fields = [
        {"name": "EMAIL", "type": "string", "nullable": True},
        {"name": "user_phone", "type": "string", "nullable": True},
        {"name": "id_card", "type": "string", "nullable": True},
        {"name": "idcard", "type": "string", "nullable": True},
        {"name": "Password", "type": "string", "nullable": True},
        {"name": "refresh_token_value", "type": "string", "nullable": True},
        {"name": "date_of_birth", "type": "string", "nullable": True},
    ]
    make_version(client, fields=fields)

    expected = {
        "EMAIL": ["name:email"],
        "user_phone": ["name:phone"],
        "id_card": ["name:id_card"],
        # The sensitive word is id_card (with underscore), not idcard.
        "idcard": [],
        "Password": ["name:password"],
        "refresh_token_value": ["name:token"],
        "date_of_birth": ["name:birth"],
    }
    for field_name, hits in expected.items():
        body = identify(client, field_name).json()
        assert body["evidence"] == hits, field_name
        assert body["confidence"] == ("low" if hits else "none")


def test_name_with_multiple_words_lists_them_in_word_order(client: TestClient) -> None:
    make_version(
        client,
        fields=[{"name": "birth_email_phone", "type": "string", "nullable": True}],
    )
    body = identify(client, "birth_email_phone").json()
    assert body["evidence"] == ["name:email", "name:phone", "name:birth"]
    assert body["confidence"] == "low"


def test_case_insensitive_substring_name_match(client: TestClient) -> None:
    make_version(
        client,
        fields=[{"name": "CustomerEMAILAddr", "type": "string", "nullable": True}],
    )
    body = identify(client, "CustomerEMAILAddr").json()
    assert body["evidence"] == ["name:email"]
    assert body["confidence"] == "low"


# --------------------------------------------------------------------------- #
# Sample matching
# --------------------------------------------------------------------------- #


def test_email_sample_rules(client: TestClient) -> None:
    make_version(
        client,
        fields=[{"name": "label", "type": "string", "nullable": True}],
    )
    hits = [
        "alice@example.com",
        "a@b.co",
        "x@sub.example.org",
    ]
    no_hits = [
        "aliceexample.com",   # no @
        "alice@example",      # no dot in domain
        "@example.com",       # empty local part
        "alice@",             # empty domain
        "a@@b.c",             # more than one @
        "a b@c.d",            # spaces are fine, still one @ and dotted domain
    ]
    assert identify(client, "label", hits).json()["evidence"] == ["sample:email"]
    assert identify(client, "label", no_hits[:5]).json()["confidence"] == "none"
    # Whitespace around the @ does not break the rule: "a b@c.d" hits.
    assert identify(client, "label", [no_hits[5]]).json()["evidence"] == [
        "sample:email"
    ]


def test_phone_sample_rules(client: TestClient) -> None:
    make_version(
        client,
        fields=[{"name": "label", "type": "string", "nullable": True}],
    )
    assert identify(client, "label", ["13800138000"]).json()["evidence"] == [
        "sample:phone"
    ]
    for value in ["23800138000", "1380013800", "138001380001", "1380013800a"]:
        assert identify(client, "label", [value]).json()["confidence"] == "none", value


def test_either_format_hits_and_both_appear(client: TestClient) -> None:
    make_version(
        client,
        fields=[{"name": "label", "type": "string", "nullable": True}],
    )
    body = identify(
        client, "label", ["not-an-email", "alice@example.com", "13800138000"]
    ).json()
    assert body["evidence"] == ["sample:email", "sample:phone"]
    assert body["confidence"] == "medium"


def test_non_string_samples_never_participate(client: TestClient) -> None:
    make_version(
        client,
        fields=[{"name": "label", "type": "string", "nullable": True}],
    )
    body = identify(
        client,
        "label",
        [13800138000, 1.5, True, False, None, "13800138000.0"],
    ).json()
    assert body["evidence"] == []
    assert body["confidence"] == "none"


def test_non_string_declared_type_is_name_only(client: TestClient) -> None:
    make_version(
        client,
        fields=[
            {"name": "code", "type": "integer", "nullable": True},
            {"name": "contact_email", "type": "decimal", "nullable": True},
        ],
    )
    # Email/phone-looking samples of a non-string field never count as sample
    # hits, even though they would hit for a string-typed field.
    body = identify(client, "code", ["13800138000", "alice@example.com"]).json()
    assert body["evidence"] == []
    assert body["confidence"] == "none"

    # Name evidence is still collected for non-string types; samples never add.
    body = identify(client, "contact_email", ["alice@example.com"]).json()
    assert body["field_type"] == "decimal"
    assert body["evidence"] == ["name:email"]
    assert body["confidence"] == "low"


def test_empty_samples_still_generates_none_record(client: TestClient) -> None:
    make_version(
        client,
        fields=[{"name": "label", "type": "string", "nullable": True}],
    )
    response = identify(client, "label", [])
    assert response.status_code == 201
    body = response.json()
    assert body["evidence"] == []
    assert body["confidence"] == "none"
    assert body["source"]["field"] == "label"


def test_confidence_levels(client: TestClient) -> None:
    make_version(
        client,
        fields=[
            {"name": "email_field", "type": "string", "nullable": True},
            {"name": "label", "type": "string", "nullable": True},
            {"name": "phone_field", "type": "string", "nullable": True},
        ],
    )
    assert identify(client, "email_field", ["alice@example.com"]).json()[
        "confidence"
    ] == "high"
    assert identify(client, "label", ["13800138000"]).json()["confidence"] == "medium"
    assert identify(client, "phone_field", []).json()["confidence"] == "low"
    # Name hit and an unrelated sample kind still count as both sides hitting.
    assert identify(
        client, "phone_field", ["alice@example.com"]
    ).json()["confidence"] == "high"


# --------------------------------------------------------------------------- #
# In-place refresh and ordering
# --------------------------------------------------------------------------- #


def test_rerun_same_field_refreshes_in_place_with_stable_id(client: TestClient) -> None:
    make_version(
        client,
        fields=[{"name": "email_field", "type": "string", "nullable": True}],
    )
    first = identify(client, "email_field", [])
    assert first.status_code == 201
    first_body = first.json()

    rerun = identify(client, "email_field", ["alice@example.com"])
    assert rerun.status_code == 200, rerun.text
    rerun_body = rerun.json()
    assert rerun_body["id"] == first_body["id"]
    assert rerun_body["evidence"] == ["name:email", "sample:email"]
    assert rerun_body["confidence"] == "high"
    assert rerun_body["created_at"] == first_body["created_at"]

    # A third run downgrades the conclusion again, still the same record.
    again = identify(client, "email_field", [])
    assert again.status_code == 200
    assert again.json()["id"] == first_body["id"]
    assert again.json()["confidence"] == "low"
    assert again.json()["evidence"] == ["name:email"]

    listed = client.get(ident_path()).json()
    assert len(listed) == 1
    assert listed[0]["id"] == first_body["id"]


def test_records_are_listed_by_id_ascending(client: TestClient) -> None:
    make_version(client)
    ordered_fields = [
        "api_token",
        "contact_email",
        "mobile_phone",
        "id_card_no",
    ]
    for field_name in ordered_fields:
        assert identify(client, field_name).status_code == 201

    # Insertion order differs from alphabetical field order; listing follows id.
    listed = client.get(ident_path()).json()
    assert [item["field"] for item in listed] == ordered_fields
    assert [item["id"] for item in listed] == sorted(item["id"] for item in listed)
    assert all(item["source"]["version"] == 1 for item in listed)


def test_empty_collection_is_an_empty_list(client: TestClient) -> None:
    make_version(client)
    assert client.get(ident_path()).json() == []


def test_same_field_name_is_independent_per_version(client: TestClient) -> None:
    make_version(
        client,
        fields=[{"name": "email", "type": "string", "nullable": True}],
    )
    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [
                {"name": "email", "type": "integer", "nullable": True}
            ]},
        ).status_code
        == 201
    )
    first = identify(client, "email", ["a@b.c"], version=1).json()
    second = identify(client, "email", ["a@b.c"], version=2).json()
    assert first["id"] != second["id"]
    assert first["confidence"] == "high"
    # Version 2 declares the field integer: samples never match.
    assert second["confidence"] == "low"
    assert second["source"] == {"dataset": "orders", "version": 2, "field": "email"}


def test_whitespace_around_field_matches_existing_field(client: TestClient) -> None:
    make_version(
        client,
        fields=[{"name": "email", "type": "string", "nullable": True}],
    )
    response = identify(client, "  email ", ["a@b.c"])
    assert response.status_code == 201, response.text
    assert response.json()["field"] == "email"
    # The trimmed name re-runs the same record.
    assert identify(client, "email").status_code == 200


# --------------------------------------------------------------------------- #
# Errors and precedence
# --------------------------------------------------------------------------- #


def test_unknown_dataset_version_and_field_return_404(client: TestClient) -> None:
    make_version(client)
    assert identify(client, "email", ["a@b.c"], dataset="ghost").status_code == 404
    assert identify(client, "email", ["a@b.c"], version=9).status_code == 404
    assert identify(client, "ghost", ["a@b.c"]).status_code == 404
    assert client.get(ident_path("ghost")).status_code == 404
    assert client.get(ident_path("orders", 9)).status_code == 404

    for response in (
        identify(client, "ghost", ["a@b.c"]),
        client.get(ident_path("ghost")),
    ):
        assert response.json()["error"] == "not_found"
    assert client.get(ident_path()).json() == []


def test_missing_extra_or_mistyped_fields_return_422(client: TestClient) -> None:
    make_version(
        client,
        fields=[{"name": "email", "type": "string", "nullable": True}],
    )

    def post(payload: object) -> int:
        return client.post(ident_path(), json=payload).status_code

    assert post({"samples": []}) == 422                      # missing field
    assert post({"field": "email"}) == 422                   # missing samples
    assert post({"field": "email", "samples": [], "x": 1}) == 422  # extra
    assert post({"field": 9, "samples": []}) == 422          # non-string field
    assert post({"field": "   ", "samples": []}) == 422      # blank field
    assert post({"field": "email", "samples": {}}) == 422    # samples not list
    assert post({"field": "email", "samples": "x"}) == 422   # samples scalar
    assert post({"field": "email", "samples": None}) == 422  # samples null
    assert post({"field": "email", "samples": [["a"]]}) == 422  # nested array
    assert post({"field": "email", "samples": [{"a": 1}]}) == 422  # object
    assert post({"field": "email", "samples": [1, {}]}) == 422
    assert post(None) == 422                                 # empty body
    assert client.get(ident_path()).json() == []


def test_malformed_json_is_422(client: TestClient) -> None:
    make_version(client)
    response = client.post(
        ident_path(), content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get(ident_path()).json() == []


def test_scalar_samples_are_accepted(client: TestClient) -> None:
    make_version(
        client,
        fields=[{"name": "label", "type": "string", "nullable": True}],
    )
    assert identify(client, "label", [1, 1.5, True, False, None, "x"]).status_code == 201


def test_post_rejects_query_parameters(client: TestClient) -> None:
    make_version(
        client,
        fields=[{"name": "email", "type": "string", "nullable": True}],
    )
    response = client.post(
        ident_path(), json={"field": "email", "samples": []}, params={"x": "1"}
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get(ident_path()).json() == []


def test_get_rejects_body_and_query_parameters(client: TestClient) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])

    with_body = client.request("GET", ident_path(), content=b"{}")
    with_query = client.get(ident_path(), params={"x": "1"})
    assert with_body.status_code == 422
    assert with_query.status_code == 422
    assert with_body.json()["error"] == "validation_error"

    # 404 takes precedence over the shape checks.
    assert client.request("GET", ident_path("ghost"), content=b"{}").status_code == 404
    assert client.get(ident_path("ghost"), params={"x": "1"}).status_code == 404
    # Valid reads are unaffected after the rejected requests.
    assert len(client.get(ident_path()).json()) == 1


def test_404_precedence_on_post_query_parameters(client: TestClient) -> None:
    make_version(client)
    response = client.post(
        ident_path("ghost"),
        json={"field": "email", "samples": []},
        params={"x": "1"},
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Persistence across a process restart
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
        {"name": "email", "type": "string", "nullable": True},
        {"name": "label", "type": "string", "nullable": True},
        {"name": "code", "type": "integer", "nullable": True},
    ]},
), 201)
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "email", "samples": ["alice@example.com"]},
), 201)
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "label", "samples": ["13800138000", "plain"]},
), 201)
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "code", "samples": ["13800138000"]},
), 201)
# Re-run in place before the restart; the id must survive.
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "label", "samples": []},
), 200)
print("created")
"""

VERIFY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

response = client.get("/datasets/orders/versions/1/sensitive-identifications")
assert response.status_code == 200, response.text
records = response.json()
assert [r["field"] for r in records] == ["email", "label", "code"]
by_field = {r["field"]: r for r in records}

assert by_field["email"]["field_type"] == "string"
assert by_field["email"]["evidence"] == ["name:email", "sample:email"]
assert by_field["email"]["confidence"] == "high"
assert by_field["email"]["source"] == {
    "dataset": "orders", "version": 1, "field": "email"
}

# The refreshed record keeps its pre-restart id and now reflects the empty
# samples (no sample evidence).
assert by_field["label"]["evidence"] == []
assert by_field["label"]["confidence"] == "none"

# Non-string declared type: email/phone-looking strings never sample-hit.
assert by_field["code"]["field_type"] == "integer"
assert by_field["code"]["evidence"] == []
assert by_field["code"]["confidence"] == "none"

# A re-run after the restart is still an in-place 200 with the same id.
rerun = client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "label", "samples": ["alice@example.com", "13800138000"]},
)
assert rerun.status_code == 200, rerun.text
assert rerun.json()["id"] == by_field["label"]["id"]
assert rerun.json()["evidence"] == ["sample:email", "sample:phone"]
assert rerun.json()["confidence"] == "medium"
print(json.dumps({"ids": [r["id"] for r in records]}))
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
    ids = json.loads(output)["ids"]
    assert ids == sorted(ids) and len(ids) == 3
