"""Tests for revising a registered privacy policy via PUT.

A revision replaces the policy's classification, masking and allowed roles
together while leaving the policy id, field attachment, enabled state and
registration time untouched. The revised values drive the next masked view
read and every read that reports current policy values; historical records
keep their write-time contents and order.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "email", "type": "string", "nullable": True},
    {"name": "ssn", "type": "string", "nullable": True},
]


def setup(client: TestClient, name: str = "orders") -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions", json={"fields": BASE_FIELDS}
    )
    assert response.status_code == 201, response.text


def policies_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/privacy-policies"


def create_policy(client: TestClient, payload: dict) -> dict:
    response = client.post(policies_path(), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def revise(client: TestClient, policy_id: int, payload: dict):
    return client.put(f"{policies_path()}/{policy_id}", json=payload)


def standard_policy(client: TestClient) -> dict:
    return create_policy(
        client,
        {
            "field": "email",
            "classification": "PII",
            "masking": "partial",
            "allowed_roles": ["analyst"],
        },
    )


# --------------------------------------------------------------------------- #
# Success shape
# --------------------------------------------------------------------------- #


def test_revision_returns_updated_policy_with_immutable_identity(
    client: TestClient,
) -> None:
    setup(client)
    policy = standard_policy(client)

    response = revise(
        client,
        policy["id"],
        {
            "classification": "  CONFIDENTIAL ",
            "masking": "redact",
            "allowed_roles": ["auditor", " guest "],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "id",
        "field",
        "classification",
        "masking",
        "allowed_roles",
        "enabled",
        "created_at",
    }
    assert body["id"] == policy["id"]
    assert body["field"] == "email"
    assert body["classification"] == "CONFIDENTIAL"
    assert body["masking"] == "redact"
    assert body["allowed_roles"] == ["auditor", "guest"]
    assert body["enabled"] is True
    assert body["created_at"] == policy["created_at"]

    listed = client.get(policies_path()).json()
    assert len(listed) == 1
    assert listed[0] == body


def test_revision_preserves_disabled_state(client: TestClient) -> None:
    setup(client)
    policy = standard_policy(client)
    disabled = client.patch(
        f"{policies_path()}/{policy['id']}", json={"enabled": False}
    )
    assert disabled.status_code == 200

    response = revise(
        client,
        policy["id"],
        {"classification": "x", "masking": "redact", "allowed_roles": []},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is False
    assert body["masking"] == "redact"
    assert body["id"] == policy["id"]
    assert body["created_at"] == policy["created_at"]


def test_revision_accepts_empty_allowed_roles(client: TestClient) -> None:
    setup(client)
    policy = standard_policy(client)
    response = revise(
        client,
        policy["id"],
        {"classification": "PII", "masking": "redact", "allowed_roles": []},
    )
    assert response.status_code == 200, response.text
    assert response.json()["allowed_roles"] == []


def test_revision_replaces_all_three_fields_even_when_unchanged(
    client: TestClient,
) -> None:
    setup(client)
    policy = standard_policy(client)
    same = {
        "classification": "PII",
        "masking": "partial",
        "allowed_roles": ["analyst"],
    }
    response = revise(client, policy["id"], same)
    assert response.status_code == 200
    assert response.json()["classification"] == "PII"
    assert response.json()["masking"] == "partial"
    assert response.json()["allowed_roles"] == ["analyst"]
    assert response.json()["id"] == policy["id"]
    assert response.json()["created_at"] == policy["created_at"]


# --------------------------------------------------------------------------- #
# Immediate effect on the masked view
# --------------------------------------------------------------------------- #


def test_revised_values_take_effect_on_next_view(client: TestClient) -> None:
    setup(client)
    policy = standard_policy(client)
    rows = [{"email": "alice@example.com"}]

    first = client.post(
        policies_path() + "/view", json={"role": "guest", "rows": rows}
    )
    assert first.status_code == 200
    assert first.json()["rows"] == [{"email": "aom"}]  # partial, analyst only

    # The analyst could see the raw value before the revision.
    analyst = client.post(
        policies_path() + "/view", json={"role": "analyst", "rows": rows}
    )
    assert analyst.json()["rows"] == rows

    revision = revise(
        client,
        policy["id"],
        {"classification": "PII", "masking": "redact", "allowed_roles": ["guest"]},
    )
    assert revision.status_code == 200

    second = client.post(
        policies_path() + "/view", json={"role": "guest", "rows": rows}
    )
    assert second.status_code == 200
    # guest is allowed after the revision, so the value now stays raw.
    assert second.json()["rows"] == rows

    former_analyst = client.post(
        policies_path() + "/view", json={"role": "analyst", "rows": rows}
    )
    assert former_analyst.json()["rows"] == [{"email": "***"}]  # redacted now


def test_revision_takes_effect_without_restart_and_changes_no_other_policy(
    client: TestClient,
) -> None:
    setup(client)
    email_policy = standard_policy(client)
    ssn_policy = create_policy(
        client,
        {
            "field": "ssn",
            "classification": "secret",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    response = revise(
        client,
        email_policy["id"],
        {"classification": "PII", "masking": "redact", "allowed_roles": []},
    )
    assert response.status_code == 200

    rows = [{"email": "alice@example.com", "ssn": "123-45-6789"}]
    viewed = client.post(
        policies_path() + "/view", json={"role": "guest", "rows": rows}
    )
    assert viewed.status_code == 200
    assert viewed.json()["rows"] == [{"email": "***", "ssn": "***"}]

    listed = {p["id"]: p for p in client.get(policies_path()).json()}
    assert listed[ssn_policy["id"]]["masking"] == "redact"
    assert listed[ssn_policy["id"]]["classification"] == "secret"
    assert listed[ssn_policy["id"]]["created_at"] == ssn_policy["created_at"]


# --------------------------------------------------------------------------- #
# Historical records are untouched; aggregates keep their counting rules
# --------------------------------------------------------------------------- #


def test_historical_records_keep_their_content_and_order(client: TestClient) -> None:
    setup(client)
    policy = standard_policy(client)

    before_view = client.post(
        policies_path() + "/view",
        json={"role": "guest", "rows": [{"email": "alice@example.com"}]},
    )
    assert before_view.status_code == 200

    revision = revise(
        client,
        policy["id"],
        {"classification": "CONFIDENTIAL", "masking": "redact", "allowed_roles": []},
    )
    assert revision.status_code == 200

    after_view = client.post(
        policies_path() + "/view",
        json={"role": "guest", "rows": [{"email": "bob@example.com"}]},
    )
    assert after_view.status_code == 200
    assert after_view.json()["rows"] == [{"email": "***"}]

    # The hit records keep the write-time masking and stay sequence ordered.
    records = client.get(policies_path() + "/view/audit-records").json()
    assert [r["sequence"] for r in records] == [1, 2]
    assert records[0]["masking"] == "partial"
    assert records[0]["field"] == "email"
    assert records[0]["policy_id"] == policy["id"]
    assert records[1]["masking"] == "redact"

    # Access records keep their per-view counts.
    access = client.get(policies_path() + "/view/access-records").json()
    assert [r["sequence"] for r in access] == [1, 2]
    assert [r["masked_count"] for r in access] == [1, 1]
    assert [r["role"] for r in access] == ["guest", "guest"]

    # Summary grouping uses the records' write-time masking, so the two hits
    # stay in separate groups with the old and new masking.
    summary = client.get(policies_path() + "/view/audit-records/summary").json()
    groups = {(g["masking"], g["hit_count"]) for g in summary["groups"]}
    assert groups == {("partial", 1), ("redact", 1)}

    # Trend rows report the policy's current classification and masking while
    # the hit count keeps the same counting rule.
    trend = client.get(policies_path() + "/view/audit-records/trend").json()
    assert len(trend["policies"]) == 1
    row = trend["policies"][0]
    assert row["policy_id"] == policy["id"]
    assert row["classification"] == "CONFIDENTIAL"
    assert row["masking"] == "redact"
    assert row["total_hits"] == 2
    assert trend["totals"]["total_hits"] == 2
    assert trend["totals"]["policy_count"] == 1


def test_coverage_and_compliance_export_reflect_revised_values(
    client: TestClient,
) -> None:
    setup(client)
    policy = standard_policy(client)

    revision = revise(
        client,
        policy["id"],
        {"classification": "RESTRICTED", "masking": "redact", "allowed_roles": []},
    )
    assert revision.status_code == 200

    coverage = client.get("/datasets/orders/privacy-policy-coverage").json()
    entry = coverage["versions"][0]
    email_field = next(f for f in entry["fields"] if f["field"] == "email")
    assert email_field["coverage"] == "enabled"
    assert email_field["classification"] == "RESTRICTED"
    assert email_field["masking"] == "redact"

    export = client.get("/datasets/orders/privacy-compliance-export").json()
    exported_policy = next(
        p for p in export["versions"][0]["policies"] if p["id"] == policy["id"]
    )
    assert exported_policy["classification"] == "RESTRICTED"
    assert exported_policy["masking"] == "redact"
    assert exported_policy["allowed_roles"] == []
    assert exported_policy["field"] == "email"


def test_revision_leaves_candidate_suggestions_unchanged(client: TestClient) -> None:
    setup(client)
    standard_policy(client)
    # An identified field without a policy still produces an advisory
    # candidate that revision must neither consume nor rewrite.
    identify = client.post(
        "/datasets/orders/versions/1/sensitive-identifications",
        json={"field": "ssn", "samples": ["510123199001011234"]},
    )
    assert identify.status_code in (200, 201)
    suggestions_before = client.get(
        "/datasets/orders/versions/1/sensitive-identifications/masking-suggestions"
    ).json()

    policy = client.get(policies_path()).json()[0]
    revision = revise(
        client,
        policy["id"],
        {"classification": "x", "masking": "redact", "allowed_roles": []},
    )
    assert revision.status_code == 200

    suggestions_after = client.get(
        "/datasets/orders/versions/1/sensitive-identifications/masking-suggestions"
    ).json()
    assert suggestions_after == suggestions_before
    # The revision created no second policy.
    assert len(client.get(policies_path()).json()) == 1


# --------------------------------------------------------------------------- #
# 422 validation and no writes
# --------------------------------------------------------------------------- #


def test_revision_rejects_invalid_payloads_with_422_and_writes_nothing(
    client: TestClient,
) -> None:
    setup(client)
    policy = standard_policy(client)
    url = f"{policies_path()}/{policy['id']}"
    base = {
        "classification": "PII",
        "masking": "redact",
        "allowed_roles": [],
    }

    def status(payload: dict | None = None, **overrides: object) -> int:
        body = payload if payload is not None else {**base, **overrides}
        return client.put(url, json=body).status_code

    assert status(payload={k: v for k, v in base.items() if k != "classification"}) == 422
    assert status(payload={k: v for k, v in base.items() if k != "masking"}) == 422
    assert status(payload={k: v for k, v in base.items() if k != "allowed_roles"}) == 422
    assert status(**{"classification": ""}) == 422
    assert status(**{"classification": "   "}) == 422
    assert status(**{"classification": 9}) == 422
    assert status(**{"masking": "hide"}) == 422
    assert status(**{"masking": "REDACT"}) == 422
    assert status(**{"masking": 7}) == 422
    assert status(**{"allowed_roles": ["a", "a"]}) == 422
    assert status(**{"allowed_roles": ["ok", ""]}) == 422
    assert status(**{"allowed_roles": ["ok", "  "]}) == 422
    assert status(**{"allowed_roles": ["a", 1]}) == 422
    assert status(**{"allowed_roles": "analyst"}) == 422
    # Extra fields are rejected, including the immutable identity fields.
    assert status(**{"field": "ssn"}) == 422
    assert status(**{"enabled": False}) == 422
    assert status(**{"id": 99}) == 422
    assert status(**{"created_at": "2000-01-01T00:00:00Z"}) == 422

    stored = client.get(policies_path()).json()[0]
    assert stored["classification"] == "PII"
    assert stored["masking"] == "partial"
    assert stored["allowed_roles"] == ["analyst"]
    assert stored["created_at"] == policy["created_at"]


def test_revision_rejects_raw_bodies_with_422(client: TestClient) -> None:
    setup(client)
    policy = standard_policy(client)
    url = f"{policies_path()}/{policy['id']}"
    headers = {"Content-Type": "application/json"}

    for raw in (b"", b"   ", b"\n\t ", b"{not json", b"[1, 2]", b'"x"', b"5", b"null"):
        response = client.put(url, content=raw, headers=headers)
        assert response.status_code == 422, raw
        assert response.json()["error"] == "validation_error"

    stored = client.get(policies_path()).json()[0]
    assert stored["classification"] == "PII"
    assert stored["masking"] == "partial"


def test_revision_rejects_query_parameters_with_422(client: TestClient) -> None:
    setup(client)
    policy = standard_policy(client)
    response = client.put(
        f"{policies_path()}/{policy['id']}?foo=bar",
        json={
            "classification": "PII",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    assert response.status_code == 422
    assert client.get(policies_path()).json()[0]["masking"] == "partial"


# --------------------------------------------------------------------------- #
# 404 precedence
# --------------------------------------------------------------------------- #


def test_revision_unknown_resource_returns_404_before_shape_checks(
    client: TestClient,
) -> None:
    setup(client)
    policy = standard_policy(client)
    invalid_body = b"   "

    unknown_dataset = client.put(
        "/datasets/ghost/versions/1/privacy-policies/1",
        content=invalid_body,
        headers={"Content-Type": "application/json"},
    )
    unknown_version = client.put(
        f"/datasets/orders/versions/9/privacy-policies/{policy['id']}",
        content=invalid_body,
        headers={"Content-Type": "application/json"},
    )
    unknown_policy = client.put(
        f"{policies_path()}/{policy['id'] + 100}",
        content=invalid_body,
        headers={"Content-Type": "application/json"},
    )
    unknown_policy_with_query = client.put(
        f"{policies_path()}/{policy['id'] + 100}?x=1",
        content=invalid_body,
        headers={"Content-Type": "application/json"},
    )

    for response in (
        unknown_dataset,
        unknown_version,
        unknown_policy,
        unknown_policy_with_query,
    ):
        assert response.status_code == 404, response.text
        assert response.json()["error"] == "not_found"

    stored = client.get(policies_path()).json()[0]
    assert stored["masking"] == "partial"


def test_revision_does_not_create_policy_for_unknown_id(client: TestClient) -> None:
    setup(client)
    standard_policy(client)
    response = client.put(
        f"{policies_path()}/999",
        json={"classification": "x", "masking": "redact", "allowed_roles": []},
    )
    assert response.status_code == 404
    assert len(client.get(policies_path()).json()) == 1


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_revisions_have_a_single_winner(client: TestClient) -> None:
    setup(client)
    policy = standard_policy(client)
    url = f"{policies_path()}/{policy['id']}"
    payloads = [
        {"classification": "A", "masking": "redact", "allowed_roles": []},
        {"classification": "B", "masking": "partial", "allowed_roles": ["r"]},
    ]
    barrier = threading.Barrier(2)
    results: list[int] = []

    def worker(payload: dict) -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        response = thread_client.put(url, json=payload)
        results.append(response.status_code)

    threads = [
        threading.Thread(target=worker, args=(payloads[i],)) for i in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [200, 409], results
    stored = client.get(policies_path()).json()[0]
    assert stored["classification"] in {"A", "B"}
    if stored["classification"] == "A":
        assert stored["masking"] == "redact"
        assert stored["allowed_roles"] == []
    else:
        assert stored["masking"] == "partial"
        assert stored["allowed_roles"] == ["r"]
    # The loser changed no field: there is no mix of the two submissions.
    assert stored["created_at"] == policy["created_at"]
    assert stored["enabled"] is True


def test_concurrent_revision_conflict_changes_nothing_else(
    client: TestClient,
) -> None:
    setup(client)
    policy = standard_policy(client)
    url = f"{policies_path()}/{policy['id']}"
    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def worker(payload: dict) -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        statuses.append(
            thread_client.put(
                url,
                json=payload,
            ).status_code
        )

    threads = [
        threading.Thread(
            target=worker,
            args=({"classification": "X", "masking": "redact", "allowed_roles": []},),
        ),
        threading.Thread(
            target=worker,
            args=({"classification": "Y", "masking": "redact", "allowed_roles": []},),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [200, 409]
    stored = client.get(policies_path()).json()[0]
    # Exactly one whole submission is visible; the loser changed nothing.
    assert stored["classification"] in {"X", "Y"}
    assert stored["masking"] == "redact"
    assert stored["allowed_roles"] == []
    # Exactly one policy exists and its identity is intact.
    assert stored["id"] == policy["id"]
    assert stored["field"] == "email"
    assert len(client.get(policies_path()).json()) == 1


# --------------------------------------------------------------------------- #
# Restart persistence
# --------------------------------------------------------------------------- #


REVISION_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
assert client.post("/datasets", json={"name": "orders"}).status_code == 201
version = client.post(
    "/datasets/orders/versions",
    json={"fields": [{"name": "email", "type": "string", "nullable": True}]},
)
assert version.status_code == 201, version.text
created = client.post(
    "/datasets/orders/versions/1/privacy-policies",
    json={
        "field": "email",
        "classification": "PII",
        "masking": "partial",
        "allowed_roles": ["analyst"],
    },
)
assert created.status_code == 201, created.text
policy = created.json()
revised = client.put(
    f"/datasets/orders/versions/1/privacy-policies/{policy['id']}",
    json={
        "classification": "RESTRICTED",
        "masking": "redact",
        "allowed_roles": ["auditor"],
    },
)
assert revised.status_code == 200, revised.text
print(json.dumps({"id": policy["id"], "created_at": policy["created_at"]}))
"""

VERIFY_SCRIPT = """
import json, sys
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
expected = json.loads(sys.argv[1])
policies = client.get("/datasets/orders/versions/1/privacy-policies").json()
assert len(policies) == 1, policies
policy = policies[0]
assert policy["id"] == expected["id"]
assert policy["created_at"] == expected["created_at"]
assert policy["classification"] == "RESTRICTED"
assert policy["masking"] == "redact"
assert policy["allowed_roles"] == ["auditor"]
assert policy["enabled"] is True
viewed = client.post(
    "/datasets/orders/versions/1/privacy-policies/view",
    json={"role": "analyst", "rows": [{"email": "alice@example.com"}]},
)
assert viewed.status_code == 200
# analyst is no longer an allowed role after the revision; redact applies.
assert viewed.json()["rows"] == [{"email": "***"}], viewed.text
print("ok")
"""


def _run(db_path: Path, script: str, *args: str) -> str:
    env = os.environ.copy()
    env["DATA_LINEAGE_DB"] = str(db_path)
    result = subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_revision_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "revision-lineage.db"
    created = json.loads(_run(db_path, REVISION_SCRIPT))
    assert _run(db_path, VERIFY_SCRIPT, json.dumps(created)) == "ok"
