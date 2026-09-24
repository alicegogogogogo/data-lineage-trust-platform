"""Tests for turning masking candidates into registered privacy policies."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
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


def suggestions_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{ident_path(dataset, version)}/masking-suggestions"


def register_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{suggestions_path(dataset, version)}/register"


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
    return client.post(register_path(**path), json={"fields": fields})  # type: ignore[arg-type]


def seed_candidates(client: TestClient) -> None:
    identify(client, "contact_email", ["alice@example.com"])
    identify(client, "mobile_phone", ["13800138000"])
    identify(client, "password_hash", [])
    identify(client, "api_token", [])
    # A no-hit record that never yields a candidate.
    identify(client, "amount", [])


POLICY_KEYS = {
    "id",
    "field",
    "classification",
    "masking",
    "allowed_roles",
    "enabled",
    "created_at",
}


# --------------------------------------------------------------------------- #
# Successful registration
# --------------------------------------------------------------------------- #


def test_register_returns_policies_in_request_field_order(client: TestClient) -> None:
    make_version(client)
    seed_candidates(client)

    requested = ["api_token", "contact_email", "mobile_phone"]
    response = register(client, requested)
    assert response.status_code == 201, response.text
    body = response.json()

    # Order follows the request, not the candidate (identification id) order.
    assert [policy["field"] for policy in body] == requested
    for policy in body:
        assert set(policy) == POLICY_KEYS
        assert isinstance(policy["id"], int)
        assert policy["enabled"] is True
        assert policy["allowed_roles"] == []
        datetime.fromisoformat(policy["created_at"])

    by_field = {policy["field"]: policy for policy in body}
    assert by_field["contact_email"]["classification"] == "PII"
    assert by_field["contact_email"]["masking"] == "partial"
    assert by_field["mobile_phone"]["classification"] == "PII"
    assert by_field["mobile_phone"]["masking"] == "partial"
    assert by_field["api_token"]["classification"] == "CREDENTIAL"
    assert by_field["api_token"]["masking"] == "redact"

    # The records are now ordinary policies, listed by id ascending.
    listed = client.get(policies_path()).json()
    assert {policy["field"] for policy in listed} == set(requested)
    assert sorted(policy["id"] for policy in listed) == sorted(
        policy["id"] for policy in body
    )


def test_single_field_registration(client: TestClient) -> None:
    make_version(client)
    seed_candidates(client)
    response = register(client, ["password_hash"])
    assert response.status_code == 201, response.text
    [policy] = response.json()
    assert policy["field"] == "password_hash"
    assert policy["classification"] == "CREDENTIAL"
    assert policy["masking"] == "redact"
    assert policy["allowed_roles"] == []
    assert policy["enabled"] is True
    assert isinstance(policy["id"], int)
    datetime.fromisoformat(policy["created_at"])


def test_surrounding_whitespace_is_trimmed_to_schema_field(
    client: TestClient,
) -> None:
    make_version(client)
    seed_candidates(client)
    response = register(client, ["  contact_email "])
    assert response.status_code == 201, response.text
    assert response.json()[0]["field"] == "contact_email"


def test_each_policy_gets_a_distinct_id_and_timestamp(client: TestClient) -> None:
    make_version(client)
    seed_candidates(client)
    body = register(client, ["contact_email", "mobile_phone"]).json()
    assert body[0]["id"] != body[1]["id"]
    for policy in body:
        datetime.fromisoformat(policy["created_at"])


# --------------------------------------------------------------------------- #
# Immediate effect on the privacy view
# --------------------------------------------------------------------------- #


def test_registered_candidate_masks_every_role_on_next_view(
    client: TestClient,
) -> None:
    make_version(client)
    seed_candidates(client)
    assert register(client, ["contact_email", "password_hash"]).status_code == 201

    rows = [{"contact_email": "alice@example.com", "password_hash": "secret"}]
    for role in ("guest", "analyst", "anyone-else"):
        response = client.post(
            policies_path() + "/view", json={"role": role, "rows": rows}
        )
        assert response.status_code == 200, response.text
        # Empty allowed_roles means the suggested masking applies to every role.
        assert response.json()["rows"] == [
            {"contact_email": "aom", "password_hash": "***"}
        ]


def test_fields_registered_later_are_masked_without_restart(client: TestClient) -> None:
    make_version(client)
    seed_candidates(client)
    rows = [{"contact_email": "alice@example.com", "mobile_phone": "13800138000"}]
    view = client.post(policies_path() + "/view", json={"role": "x", "rows": rows})
    assert view.json()["rows"] == rows

    assert register(client, ["contact_email"]).status_code == 201
    view = client.post(policies_path() + "/view", json={"role": "x", "rows": rows})
    assert view.json()["rows"] == [
        {"contact_email": "aom", "mobile_phone": "13800138000"}
    ]

    assert register(client, ["mobile_phone"]).status_code == 201
    view = client.post(policies_path() + "/view", json={"role": "x", "rows": rows})
    assert view.json()["rows"] == [
        {"contact_email": "aom", "mobile_phone": "100"}
    ]


# --------------------------------------------------------------------------- #
# Candidate reads and identification records stay untouched
# --------------------------------------------------------------------------- #


def test_registration_reads_but_never_writes_identifications(
    client: TestClient,
) -> None:
    make_version(client)
    seed_candidates(client)
    identifications_before = client.get(ident_path()).json()
    suggestions_before = client.get(suggestions_path()).json()

    assert register(client, ["contact_email"]).status_code == 201

    assert client.get(ident_path()).json() == identifications_before
    # The candidate is still listed: registration does not consume it.
    assert client.get(suggestions_path()).json() == suggestions_before
    # And the read-only suggestions endpoint still never registers anything.
    assert client.get(suggestions_path()).status_code == 200


def test_refreshed_identification_keeps_its_record_and_registered_policy(
    client: TestClient,
) -> None:
    make_version(client)
    identify(client, "contact_email", ["alice@example.com"])
    created = register(client, ["contact_email"]).json()[0]
    records = client.get(ident_path()).json()
    assert len(records) == 1

    # Re-running the identification refreshes in place (200, stable id); the
    # registered policy is unaffected.
    refreshed = identify(client, "contact_email", ["bob@example.com"])
    assert refreshed.status_code == 200
    [record] = client.get(ident_path()).json()
    assert record["id"] == records[0]["id"]

    [policy] = client.get(policies_path()).json()
    assert policy["id"] == created["id"]
    assert policy["field"] == "contact_email"


# --------------------------------------------------------------------------- #
# 404: dataset, version and field resolution
# --------------------------------------------------------------------------- #


def test_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    make_version(client)
    seed_candidates(client)
    for response in (
        register(client, ["contact_email"], dataset="ghost"),
        register(client, ["contact_email"], version=9),
    ):
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"
        assert set(response.json()) == {"error", "detail"}
    assert client.get(policies_path()).json() == []


def test_field_not_in_version_is_404_and_writes_nothing(client: TestClient) -> None:
    make_version(client)
    seed_candidates(client)
    response = register(client, ["contact_email", "ghost_field"])
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert client.get(policies_path()).json() == []
    # The good field in the same batch was not registered either.
    assert client.get(policies_path()).json() == []


def test_404_takes_precedence_over_shape_and_query_checks(client: TestClient) -> None:
    make_version(client)
    # Blank names and query parameters are repository-level checks that run
    # only after the path dataset/version resolves.
    assert register(client, ["   "], version=9).status_code == 404
    response = client.post(
        register_path(dataset="ghost") + "?notify=false",
        json={"fields": ["contact_email"]},
    )
    assert response.status_code == 404
    assert register(client, ["contact_email"], dataset="ghost").status_code == 404


# --------------------------------------------------------------------------- #
# 409: no candidate or existing policy
# --------------------------------------------------------------------------- #


def test_never_identified_field_is_409(client: TestClient) -> None:
    make_version(client)
    seed_candidates(client)
    response = register(client, ["id"])
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    assert set(response.json()) == {"error", "detail"}
    assert client.get(policies_path()).json() == []


def test_identified_but_unhit_field_is_409(client: TestClient) -> None:
    make_version(client)
    seed_candidates(client)
    # "amount" has an identification record with no evidence -> no candidate.
    candidate_fields = {
        item["field"] for item in client.get(suggestions_path()).json()
    }
    assert candidate_fields and "amount" not in candidate_fields
    response = register(client, ["amount"])
    assert response.status_code == 409
    assert client.get(policies_path()).json() == []


def test_batch_with_one_field_missing_a_candidate_is_409_atomic(
    client: TestClient,
) -> None:
    make_version(client)
    seed_candidates(client)
    response = register(client, ["contact_email", "amount"])
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    # Whole batch rejected: even the valid field is absent.
    assert client.get(policies_path()).json() == []


def test_field_with_policy_is_409_and_first_policy_unchanged(
    client: TestClient,
) -> None:
    make_version(client)
    seed_candidates(client)
    first = register(client, ["contact_email"]).json()[0]

    again = register(client, ["contact_email"])
    assert again.status_code == 409
    assert again.json()["error"] == "conflict"

    [listed] = client.get(policies_path()).json()
    assert listed["id"] == first["id"]
    assert listed["created_at"] == first["created_at"]


def test_batch_touching_an_already_registered_field_is_fully_409(
    client: TestClient,
) -> None:
    make_version(client)
    seed_candidates(client)
    assert register(client, ["contact_email"]).status_code == 201
    # mobile_phone is free, but the shared field makes the whole batch fail.
    response = register(client, ["mobile_phone", "contact_email"])
    assert response.status_code == 409
    assert [p["field"] for p in client.get(policies_path()).json()] == [
        "contact_email"
    ]


# --------------------------------------------------------------------------- #
# 422: request shape (nothing written)
# --------------------------------------------------------------------------- #


def test_invalid_bodies_are_422_and_write_nothing(client: TestClient) -> None:
    make_version(client)
    seed_candidates(client)

    def status(payload: object) -> int:
        return client.post(register_path(), json=payload).status_code

    assert status({}) == 422                                   # missing fields
    assert status({"fields": []}) == 422                      # empty array
    assert status({"fields": None}) == 422                    # null
    assert status({"fields": "contact_email"}) == 422         # not an array
    assert status({"fields": {"contact_email": 1}}) == 422    # object
    assert status({"fields": [7]}) == 422                     # non-string item
    assert status({"fields": [None]}) == 422                  # null item
    assert status({"fields": [["contact_email"]]}) == 422     # nested array
    assert status({"fields": [{}]}) == 422                    # object item
    assert status(
        {"fields": ["contact_email"], "extra": 1}
    ) == 422                                                  # extra field
    assert client.get(policies_path()).json() == []


def test_blank_and_duplicate_names_are_422(client: TestClient) -> None:
    make_version(client)
    seed_candidates(client)

    for names in (
        [""],
        ["   "],
        ["\t\n"],
        ["contact_email", ""],
        ["contact_email", "contact_email"],
        ["contact_email", " contact_email "],
    ):
        response = register(client, names)
        assert response.status_code == 422, names
        assert response.json()["error"] == "validation_error"
        assert set(response.json()) == {"error", "detail"}

    assert client.get(policies_path()).json() == []


def test_empty_whitespace_and_malformed_body_are_422(client: TestClient) -> None:
    make_version(client)
    seed_candidates(client)

    empty = client.post(register_path())
    assert empty.status_code == 422
    assert empty.json()["error"] == "validation_error"

    whitespace = client.post(
        register_path(),
        content=b"   ",
        headers={"content-type": "application/json"},
    )
    assert whitespace.status_code == 422
    assert whitespace.json()["error"] == "validation_error"

    malformed = client.post(
        register_path(),
        content=b"{not valid json",
        headers={"content-type": "application/json"},
    )
    assert malformed.status_code == 422
    assert malformed.json()["error"] == "validation_error"

    assert client.get(policies_path()).json() == []


def test_query_parameters_are_422_and_body_then_succeeds(client: TestClient) -> None:
    make_version(client)
    seed_candidates(client)
    body = {"fields": ["contact_email"]}

    rejected = client.post(register_path() + "?notify=false", json=body)
    assert rejected.status_code == 422
    assert rejected.json()["error"] == "validation_error"
    assert client.get(policies_path()).json() == []

    accepted = client.post(register_path(), json=body)
    assert accepted.status_code == 201


def test_error_responses_do_not_leak_internals(client: TestClient) -> None:
    make_version(client)
    seed_candidates(client)
    for response in (
        client.post(register_path(), json={"fields": ["amount"]}),
        client.post(register_path(), json={"fields": ["ghost"]}),
        client.post(register_path(), json={"fields": []}),
    ):
        text = response.text.lower()
        assert "traceback" not in text
        assert "sqlite" not in text
        assert "select" not in text
        assert "insert" not in text


# --------------------------------------------------------------------------- #
# Concurrency: exactly one registration wins per field
# --------------------------------------------------------------------------- #


def test_concurrent_registrations_of_same_field_have_one_winner(
    client: TestClient,
) -> None:
    make_version(client)
    seed_candidates(client)

    thread_count = 8
    barrier = threading.Barrier(thread_count)
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        response = thread_client.post(
            register_path(), json={"fields": ["contact_email"]}
        )
        with statuses_lock:
            statuses.append(response.status_code)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses).count(201) == 1
    assert sorted(statuses).count(409) == thread_count - 1
    policies = client.get(policies_path()).json()
    assert [p["field"] for p in policies] == ["contact_email"]


def test_concurrent_registrations_of_disjoint_fields_all_succeed(
    client: TestClient,
) -> None:
    make_version(client)
    seed_candidates(client)
    fields = ["contact_email", "mobile_phone", "password_hash", "api_token"]

    barrier = threading.Barrier(len(fields))
    errors: list[AssertionError] = []

    def worker(field_name: str) -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        response = thread_client.post(register_path(), json={"fields": [field_name]})
        if response.status_code != 201:  # pragma: no cover - reported below
            errors.append(AssertionError(response.text))

    threads = [threading.Thread(target=worker, args=(name,)) for name in fields]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert {p["field"] for p in client.get(policies_path()).json()} == set(fields)


def test_concurrent_overlapping_batches_leave_one_winner(client: TestClient) -> None:
    make_version(client)
    seed_candidates(client)
    thread_count = 6
    barrier = threading.Barrier(thread_count)
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        response = thread_client.post(
            register_path(),
            json={"fields": ["contact_email", "mobile_phone"]},
        )
        with statuses_lock:
            statuses.append(response.status_code)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses.count(201) == 1
    assert statuses.count(409) == thread_count - 1
    assert len(client.get(policies_path()).json()) == 2


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
        {"name": "contact_email", "type": "string", "nullable": True},
        {"name": "password_hash", "type": "string", "nullable": True},
        {"name": "label", "type": "string", "nullable": True},
    ]},
), 201)
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "contact_email", "samples": ["alice@example.com"]},
), 201)
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "password_hash", "samples": []},
), 201)
ok(client.post(
    "/datasets/orders/versions/1/sensitive-identifications",
    json={"field": "label", "samples": ["plain"]},
), 201)
response = client.post(
    "/datasets/orders/versions/1/sensitive-identifications/masking-suggestions/register",
    json={"fields": ["contact_email", "password_hash"]},
)
assert response.status_code == 201, response.text
policies = response.json()
assert [p["field"] for p in policies] == ["contact_email", "password_hash"]
print("created")
"""

VERIFY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

policies = client.get("/datasets/orders/versions/1/privacy-policies").json()
assert [p["field"] for p in policies] == ["contact_email", "password_hash"]
assert policies[0]["classification"] == "PII"
assert policies[0]["masking"] == "partial"
assert policies[0]["allowed_roles"] == []
assert policies[0]["enabled"] is True
assert policies[1]["classification"] == "CREDENTIAL"
assert policies[1]["masking"] == "redact"
for policy in policies:
    assert isinstance(policy["id"], int) and policy["created_at"]

# The next view read after restart masks according to the registered policies.
view = client.post(
    "/datasets/orders/versions/1/privacy-policies/view",
    json={"role": "guest", "rows": [{
        "contact_email": "alice@example.com",
        "password_hash": "secret",
        "label": "plain",
    }]},
)
assert view.status_code == 200, view.text
assert view.json()["rows"] == [{
    "contact_email": "aom",
    "password_hash": "***",
    "label": "plain",
}]

# The unhit field still has no candidate and therefore cannot be registered.
rejected = client.post(
    "/datasets/orders/versions/1/sensitive-identifications/masking-suggestions/register",
    json={"fields": ["label"]},
)
assert rejected.status_code == 409, rejected.text

# Identification records are intact and the candidate read is unchanged.
assert len(client.get(
    "/datasets/orders/versions/1/sensitive-identifications"
).json()) == 3
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
