"""Tests for registering privacy policies from masking suggestions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
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
            {"name": "password_hash", "type": "string", "nullable": True},
            {"name": "api_token", "type": "string", "nullable": True},
            {"name": "amount", "type": "decimal", "nullable": True},
        ]
    assert client.post("/datasets", json={"name": dataset}).status_code == 201
    response = client.post(
        f"/datasets/{dataset}/versions", json={"fields": fields}
    )
    assert response.status_code == 201, response.text


def ident_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/sensitive-identifications"


def register_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{ident_path(dataset, version)}/masking-suggestions/register"


def policies_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/privacy-policies"


def identify(
    client: TestClient, field: str, samples: list | None = None, **path: object
):
    return client.post(
        ident_path(**path),  # type: ignore[arg-type]
        json={"field": field, "samples": [] if samples is None else samples},
    )


def register(client: TestClient, fields: list, **path: object):
    return client.post(
        register_path(**path), json={"fields": fields}  # type: ignore[arg-type]
    )


def assert_error_shape(body: dict) -> None:
    assert set(body) == {"error", "detail"}
    assert isinstance(body["detail"], str)
    assert body["detail"]
    # Errors never leak SQL, stack traces or internal objects.
    for forbidden in ("Traceback", "sqlite", "SELECT", "INSERT", "0x"):
        assert forbidden not in body["detail"]


def identify_a_mixed_set(client: TestClient) -> None:
    identify(client, "contact_email", ["alice@example.com"])
    identify(client, "mobile_phone", ["13800138000"])
    identify(client, "password_hash", [])
    identify(client, "api_token", [])


# --------------------------------------------------------------------------- #
# Successful registration
# --------------------------------------------------------------------------- #


def test_register_creates_policies_from_candidates(client: TestClient) -> None:
    make_version(client)
    identify_a_mixed_set(client)

    response = register(
        client, ["api_token", "contact_email", "mobile_phone", "password_hash"]
    )
    assert response.status_code == 201, response.text
    policies = response.json()

    # Ordered by the request field names, not by candidate/id order.
    assert [policy["field"] for policy in policies] == [
        "api_token",
        "contact_email",
        "mobile_phone",
        "password_hash",
    ]
    by_field = {policy["field"]: policy for policy in policies}
    expected = {
        "api_token": ("CREDENTIAL", "redact"),
        "contact_email": ("PII", "partial"),
        "mobile_phone": ("PII", "partial"),
        "password_hash": ("CREDENTIAL", "redact"),
    }
    for field, (classification, masking) in expected.items():
        policy = by_field[field]
        assert set(policy) == {
            "id",
            "field",
            "classification",
            "masking",
            "allowed_roles",
            "enabled",
            "created_at",
        }
        assert isinstance(policy["id"], int)
        assert policy["classification"] == classification
        assert policy["masking"] == masking
        # The candidate carries an empty role list, copied verbatim.
        assert policy["allowed_roles"] == []
        assert policy["enabled"] is True
        assert policy["created_at"]

    # The same records are persisted and the list endpoint sorts them by id.
    persisted = client.get(policies_path()).json()
    assert sorted(policy["id"] for policy in persisted) == sorted(
        policy["id"] for policy in policies
    )
    assert [policy["id"] for policy in persisted] == sorted(
        policy["id"] for policy in persisted
    )


def test_response_order_follows_request_not_candidate_order(
    client: TestClient,
) -> None:
    make_version(
        client,
        fields=[
            {"name": "mobile_phone", "type": "string", "nullable": True},
            {"name": "contact_email", "type": "string", "nullable": True},
        ],
    )
    # mobile_phone gets the smaller identification id ...
    identify(client, "mobile_phone", ["13800138000"])
    identify(client, "contact_email", ["alice@example.com"])

    # ... but the request names contact_email first.
    response = register(client, ["contact_email", "mobile_phone"])
    assert response.status_code == 201, response.text
    assert [policy["field"] for policy in response.json()] == [
        "contact_email",
        "mobile_phone",
    ]
    # Ids were nevertheless assigned in request order.
    ids = [policy["id"] for policy in response.json()]
    assert ids == sorted(ids)


def test_registered_policies_mask_for_every_role_on_next_view(
    client: TestClient,
) -> None:
    make_version(client)
    identify_a_mixed_set(client)
    response = register(client, ["contact_email", "password_hash"])
    assert response.status_code == 201, response.text

    rows = [
        {
            "contact_email": "alice@example.com",
            "password_hash": "secret",
            "amount": 12,
        }
    ]
    # An empty allowed_roles list masks for every role, including arbitrary
    # role names.
    for role in ("guest", "analyst", "admin"):
        view = client.post(
            f"{policies_path()}/view", json={"role": role, "rows": rows}
        )
        assert view.status_code == 200, view.text
        assert view.json()["rows"] == [
            {
                # partial keeps first + last two of a string longer than 4
                "contact_email": "aom",
                # redact replaces every non-null value
                "password_hash": "***",
                # no policy -> unchanged
                "amount": 12,
            }
        ]


def test_trimming_field_names_matches_existing_fields(client: TestClient) -> None:
    make_version(
        client,
        fields=[{"name": "email", "type": "string", "nullable": True}],
    )
    identify(client, "email", ["alice@example.com"])
    response = register(client, ["  email "])
    assert response.status_code == 201, response.text
    [policy] = response.json()
    assert policy["field"] == "email"


# --------------------------------------------------------------------------- #
# Conflicts: no candidate / existing policy, whole-batch rollback
# --------------------------------------------------------------------------- #


def test_field_never_identified_is_409(client: TestClient) -> None:
    make_version(client)
    response = register(client, ["contact_email"])
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    assert_error_shape(response.json())
    assert client.get(policies_path()).json() == []


def test_identified_field_without_any_hit_is_409(client: TestClient) -> None:
    make_version(client)
    # "amount" has no sensitive name word and no string samples hit.
    identify(client, "amount", [])
    response = register(client, ["amount"])
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    assert_error_shape(response.json())
    assert client.get(policies_path()).json() == []


def test_non_string_declared_type_without_name_hit_is_409(
    client: TestClient,
) -> None:
    make_version(client)
    # A non-string field is matched by name alone; "id" never hits.
    identify(client, "id", ["13800138000"])
    response = register(client, ["id"])
    assert response.status_code == 409
    assert client.get(policies_path()).json() == []


def test_registering_a_field_with_a_policy_is_409(client: TestClient) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])
    assert register(client, ["contact_email"]).status_code == 201

    response = register(client, ["contact_email"])
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    assert_error_shape(response.json())
    assert len(client.get(policies_path()).json()) == 1


def test_batch_with_one_missing_candidate_writes_nothing(client: TestClient) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])
    # api_token was never identified; contact_email is valid but must not be
    # written either.
    response = register(client, ["contact_email", "api_token"])
    assert response.status_code == 409, response.text
    assert client.get(policies_path()).json() == []

    # Order independence: the invalid field first as well.
    response = register(client, ["api_token", "contact_email"])
    assert response.status_code == 409, response.text
    assert client.get(policies_path()).json() == []


def test_batch_with_one_existing_policy_writes_nothing(client: TestClient) -> None:
    make_version(client)
    identify_a_mixed_set(client)
    assert register(client, ["contact_email"]).status_code == 201

    # password_hash has a candidate but contact_email already has a policy.
    response = register(client, ["password_hash", "contact_email"])
    assert response.status_code == 409, response.text
    policies = client.get(policies_path()).json()
    assert [policy["field"] for policy in policies] == ["contact_email"]


def test_single_policy_create_then_suggestion_register_is_409(
    client: TestClient,
) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])
    created = client.post(
        policies_path(),
        json={
            "field": "contact_email",
            "classification": "PII",
            "masking": "partial",
            "allowed_roles": ["analyst"],
        },
    )
    assert created.status_code == 201, created.text

    response = register(client, ["contact_email"])
    assert response.status_code == 409
    assert len(client.get(policies_path()).json()) == 1


# --------------------------------------------------------------------------- #
# Unknown dataset / version / field
# --------------------------------------------------------------------------- #


def test_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])

    assert register(client, ["contact_email"], dataset="ghost").status_code == 404
    assert register(client, ["contact_email"], version=9).status_code == 404
    for response in (
        register(client, ["contact_email"], dataset="ghost"),
        register(client, ["contact_email"], version=9),
    ):
        assert response.json()["error"] == "not_found"
        assert_error_shape(response.json())
    assert client.get(policies_path()).json() == []


def test_field_not_in_version_is_404_and_writes_nothing(client: TestClient) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])

    response = register(client, ["contact_email", "ghost_field"])
    assert response.status_code == 404, response.text
    assert response.json()["error"] == "not_found"
    assert_error_shape(response.json())
    # The valid member of the batch was not written either.
    assert client.get(policies_path()).json() == []


# --------------------------------------------------------------------------- #
# Request shape: 422 and no writes
# --------------------------------------------------------------------------- #


def assert_422_no_write(client: TestClient, response) -> None:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"] == "validation_error"
    assert_error_shape(body)
    assert client.get(policies_path()).json() == []


def test_missing_fields_key_is_422(client: TestClient) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])
    assert_422_no_write(client, client.post(register_path(), json={}))


def test_extra_body_field_is_422(client: TestClient) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])
    assert_422_no_write(
        client,
        client.post(
            register_path(),
            json={"fields": ["contact_email"], "mode": "all"},
        ),
    )


def test_empty_array_is_422(client: TestClient) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])
    assert_422_no_write(client, register(client, []))


def test_duplicate_names_are_422(client: TestClient) -> None:
    make_version(client)
    identify_a_mixed_set(client)
    assert_422_no_write(
        client, register(client, ["contact_email", "contact_email"])
    )
    # Duplicates are detected after trimming whitespace.
    assert_422_no_write(client, register(client, ["contact_email", " contact_email "]))


def test_non_string_names_are_422(client: TestClient) -> None:
    make_version(client)
    identify_a_mixed_set(client)
    for payload in (
        {"fields": [1]},
        {"fields": [None]},
        {"fields": [True]},
        {"fields": ["contact_email", 2]},
        {"fields": "contact_email"},
    ):
        response = client.post(register_path(), json=payload)
        assert response.status_code == 422, payload
    assert client.get(policies_path()).json() == []


def test_blank_name_after_trimming_is_422_and_writes_nothing(
    client: TestClient,
) -> None:
    make_version(client)
    identify_a_mixed_set(client)
    # A blank member rejects the batch even when another member is valid.
    assert_422_no_write(client, register(client, ["contact_email", "   "]))
    assert_422_no_write(client, register(client, ["\t"]))


def test_empty_body_whitespace_body_and_malformed_json_are_422(
    client: TestClient,
) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])

    assert_422_no_write(client, client.post(register_path()))
    assert_422_no_write(
        client,
        client.post(
            register_path(),
            content=b"   ",
            headers={"content-type": "application/json"},
        ),
    )
    assert_422_no_write(
        client,
        client.post(
            register_path(),
            content=b"{not json",
            headers={"content-type": "application/json"},
        ),
    )


def test_query_parameters_are_422_and_not_written(client: TestClient) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])
    response = client.post(
        register_path() + "?notify=false", json={"fields": ["contact_email"]}
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get(policies_path()).json() == []

    # The same body succeeds without the query parameter.
    assert register(client, ["contact_email"]).status_code == 201

    # 404 still takes precedence over query-parameter validation.
    response = client.post(
        register_path(dataset="ghost") + "?notify=false",
        json={"fields": ["contact_email"]},
    )
    assert response.status_code == 404, response.text


# --------------------------------------------------------------------------- #
# Read-only suggestions and identification records stay untouched
# --------------------------------------------------------------------------- #


def test_registration_does_not_change_identifications_or_suggestions(
    client: TestClient,
) -> None:
    make_version(client)
    identify_a_mixed_set(client)
    identifications_before = client.get(ident_path()).json()
    suggestions_before = client.get(
        f"{ident_path()}/masking-suggestions"
    ).json()

    response = register(
        client, ["contact_email", "mobile_phone", "password_hash", "api_token"]
    )
    assert response.status_code == 201, response.text

    assert client.get(ident_path()).json() == identifications_before
    suggestions_after = client.get(f"{ident_path()}/masking-suggestions").json()
    assert suggestions_after == suggestions_before
    # The advisory candidates are still reported after policies exist.
    assert {item["field"] for item in suggestions_after} == {
        "contact_email",
        "mobile_phone",
        "password_hash",
        "api_token",
    }


def test_get_is_not_allowed_on_the_register_path(client: TestClient) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])
    response = client.get(register_path())
    assert response.status_code == 405
    assert client.get(policies_path()).json() == []


# --------------------------------------------------------------------------- #
# Concurrent registration: single winner
# --------------------------------------------------------------------------- #


def test_concurrent_registration_of_one_field_has_single_winner(
    client: TestClient,
) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])

    results: list = []
    results_guard = threading.Lock()
    barrier = threading.Barrier(2)

    def submit() -> None:
        barrier.wait()
        response = client.post(
            register_path(), json={"fields": ["contact_email"]}
        )
        with results_guard:
            results.append(response)

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(response.status_code for response in results) == [201, 409]
    policies = client.get(policies_path()).json()
    assert len(policies) == 1
    assert policies[0]["field"] == "contact_email"


def test_concurrent_overlapping_batches_never_leave_a_duplicate(
    client: TestClient,
) -> None:
    make_version(client)
    identify_a_mixed_set(client)

    results: list = []
    results_guard = threading.Lock()
    barrier = threading.Barrier(2)
    batches = [
        ["contact_email", "mobile_phone"],
        ["mobile_phone", "password_hash"],
    ]

    def submit(fields: list[str]) -> None:
        barrier.wait()
        response = client.post(register_path(), json={"fields": fields})
        with results_guard:
            results.append((fields, response))

    threads = [
        threading.Thread(target=submit, args=(batch,)) for batch in batches
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Exactly one batch commits; the overlapping loser is rejected wholesale.
    assert sorted(response.status_code for _, response in results) == [201, 409]
    policies = client.get(policies_path()).json()
    fields = sorted(policy["field"] for policy in policies)
    assert fields == sorted(set(fields))
    winner = next(fields_batch for fields_batch, response in results
                  if response.status_code == 201)
    assert fields == sorted(winner)


# --------------------------------------------------------------------------- #
# Persistence across a process restart
# --------------------------------------------------------------------------- #


CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response, status):
    assert response.status_code == status, response.text

ok(client.post("/datasets", json={"name": "orders"}), 201)
ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [
        {"name": "contact_email", "type": "string", "nullable": True},
        {"name": "api_token", "type": "string", "nullable": True},
        {"name": "label", "type": "string", "nullable": True},
    ]},
), 201)
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "contact_email", "samples": ["alice@example.com"]},
), 201)
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "api_token", "samples": []},
), 201)
# Identified but never a candidate.
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "label", "samples": ["plain"]},
), 201)
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications/masking-suggestions/register",
    json={"fields": ["api_token", "contact_email"]},
), 201)
print("created")
"""

VERIFY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

policies = client.get("/datasets/orders/versions/1/privacy-policies").json()
assert len(policies) == 2
by_field = {policy["field"]: policy for policy in policies}
email = by_field["contact_email"]
assert email["classification"] == "PII"
assert email["masking"] == "partial"
assert email["allowed_roles"] == []
assert email["enabled"] is True
token = by_field["api_token"]
assert token["classification"] == "CREDENTIAL"
assert token["masking"] == "redact"
assert token["allowed_roles"] == []

# The policies mask immediately for every role after the restart.
view = client.post(
    "/datasets/orders/versions/1/privacy-policies/view",
    json={"role": "guest", "rows": [{
        "contact_email": "alice@example.com",
        "api_token": "abcdef",
        "label": "plain",
    }]},
)
assert view.status_code == 200, view.text
assert view.json()["rows"] == [{
    "contact_email": "aom",
    "api_token": "***",
    "label": "plain",
}]

# Re-registering after restart is still a conflict with no write.
again = client.post(
    "/datasets/orders/versions/1/sensitive-identifications/masking-suggestions/register",
    json={"fields": ["contact_email"]},
)
assert again.status_code == 409, again.text
assert len(client.get("/datasets/orders/versions/1/privacy-policies").json()) == 2

# Identification records are intact and the suggestions read is unchanged.
idents = client.get(
    "/datasets/orders/versions/1/sensitive-identifications"
).json()
assert sorted(record["field"] for record in idents) == [
    "api_token", "contact_email", "label",
]
suggestions = client.get(
    "/datasets/orders/versions/1/sensitive-identifications/masking-suggestions"
).json()
assert sorted(item["field"] for item in suggestions) == [
    "api_token", "contact_email",
]
print(json.dumps({"count": len(policies)}))
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


def test_registered_policies_survive_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "register.db"
    assert _run(db_path, CREATE_SCRIPT) == "created"
    assert json.loads(_run(db_path, VERIFY_SCRIPT))["count"] == 2
