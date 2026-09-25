"""Tests for the privacy view masking-hit audit summary."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]


BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "email", "type": "string", "nullable": True},
    {"name": "ssn", "type": "string", "nullable": True},
    {"name": "age", "type": "integer", "nullable": True},
]


def make_dataset_with_version(client: TestClient, name: str = "orders") -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions",
        json={"fields": BASE_FIELDS},
    )
    assert response.status_code == 201, response.text


def policies_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/privacy-policies"


def summary_path(dataset: str = "orders", version: int = 1) -> str:
    return policies_path(dataset, version) + "/view/audit-records/summary"


def create_policy(client: TestClient, payload: dict, **path: object) -> dict:
    response = client.post(policies_path(**path), json=payload)  # type: ignore[arg-type]
    assert response.status_code == 201, response.text
    return response.json()


def post_view(client: TestClient, role: str, rows: list[dict], **path: object):
    return client.post(
        policies_path(**path) + "/view",  # type: ignore[arg-type]
        json={"role": role, "rows": rows},
    )


# --------------------------------------------------------------------------- #
# Grouping and counts
# --------------------------------------------------------------------------- #


def test_summary_empty_when_no_records(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.get(summary_path())
    assert response.status_code == 200, response.text
    assert response.json() == {"dataset": "orders", "version": 1, "groups": []}


def test_summary_groups_hits_and_counts_records(client: TestClient) -> None:
    make_dataset_with_version(client)
    email_policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    ssn_policy = create_policy(
        client,
        {"field": "ssn", "classification": "secret", "masking": "partial",
         "allowed_roles": []},
    )

    # Three email hits by guest, one email hit by analyst, one ssn hit.
    for _ in range(3):
        assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    assert post_view(client, "analyst", [{"email": "a@b.c"}]).status_code == 200
    assert (
        post_view(client, "guest", [{"ssn": "123", "email": None}]).status_code
        == 200
    )

    summary = client.get(summary_path()).json()
    assert summary["dataset"] == "orders"
    assert summary["version"] == 1
    groups = summary["groups"]
    assert len(groups) == 3
    for group in groups:
        assert set(group) == {
            "field",
            "policy_id",
            "role",
            "masking",
            "count",
            "first_hit_at",
            "last_hit_at",
        }
        datetime.fromisoformat(group["first_hit_at"])
        datetime.fromisoformat(group["last_hit_at"])
        assert group["first_hit_at"] <= group["last_hit_at"]

    # Sorted by field, then policy_id, role, masking (all ascending).
    keys = [
        (g["field"], g["policy_id"], g["role"], g["masking"]) for g in groups
    ]
    assert keys == sorted(keys)

    by_key = {
        (g["field"], g["role"]): g for g in groups if g["field"] == "email"
    }
    email_guest = by_key[("email", "guest")]
    assert email_guest["policy_id"] == email_policy["id"]
    assert email_guest["masking"] == "redact"
    assert email_guest["count"] == 3
    assert email_guest["first_hit_at"] <= email_guest["last_hit_at"]

    email_analyst = by_key[("email", "analyst")]
    assert email_analyst["count"] == 1
    # A single-record group has identical first and last hit times.
    assert email_analyst["first_hit_at"] == email_analyst["last_hit_at"]

    ssn_group = next(g for g in groups if g["field"] == "ssn")
    assert ssn_group["policy_id"] == ssn_policy["id"]
    assert ssn_group["masking"] == "partial"
    assert ssn_group["count"] == 1


def test_summary_never_merges_groups(client: TestClient) -> None:
    make_dataset_with_version(client)
    email_policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    ssn_policy = create_policy(
        client,
        {"field": "ssn", "classification": "secret", "masking": "partial",
         "allowed_roles": []},
    )

    # Same field hit by two roles stays two groups; the two fields differ in
    # policy and masking and stay separate from both.
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    assert post_view(client, "analyst", [{"email": "a@b.c"}]).status_code == 200
    assert post_view(client, "guest", [{"ssn": "123"}]).status_code == 200

    groups = client.get(summary_path()).json()["groups"]
    combos = {
        (g["field"], g["policy_id"], g["role"], g["masking"]): g["count"]
        for g in groups
    }
    assert combos == {
        ("email", email_policy["id"], "analyst", "redact"): 1,
        ("email", email_policy["id"], "guest", "redact"): 1,
        ("ssn", ssn_policy["id"], "guest", "partial"): 1,
    }


def test_summary_is_scoped_to_its_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200

    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "email", "type": "string", "nullable": True}]},
        ).status_code
        == 201
    )
    assert client.get(summary_path("orders", 2)).json() == {
        "dataset": "orders",
        "version": 2,
        "groups": [],
    }
    assert len(client.get(summary_path()).json()["groups"]) == 1


def test_summary_reflects_new_records_on_every_read(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    first = client.get(summary_path()).json()
    assert first["groups"][0]["count"] == 1

    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    second = client.get(summary_path()).json()
    assert second["groups"][0]["count"] == 2
    assert second["groups"][0]["first_hit_at"] == first["groups"][0]["first_hit_at"]
    assert second["groups"][0]["last_hit_at"] >= first["groups"][0]["last_hit_at"]


# --------------------------------------------------------------------------- #
# Rejections and error shape
# --------------------------------------------------------------------------- #


def test_summary_unknown_dataset_or_version_returns_404(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    unknown_dataset = client.get(summary_path("ghost"))
    unknown_version = client.get(summary_path("orders", 9))
    assert unknown_dataset.status_code == 404
    assert unknown_version.status_code == 404
    for response in (unknown_dataset, unknown_version):
        assert response.json()["error"] == "not_found"


def test_summary_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    before = client.get(summary_path()).json()

    with_body = client.request("GET", summary_path(), content=b"{}")
    with_query = client.get(summary_path(), params={"limit": 1})
    assert with_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, with_query):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"

    # The rejections wrote nothing and the summary is unchanged.
    assert client.get(summary_path()).json() == before


def test_summary_shape_errors_keep_404_precedence(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert client.get(summary_path("ghost"), params={"x": "1"}).status_code == 404
    assert (
        client.request("GET", summary_path("orders", 9), content=b"{}").status_code
        == 404
    )


def test_summary_does_not_write_records(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    records_before = client.get(policies_path() + "/view/audit-records").json()

    assert client.get(summary_path()).status_code == 200
    assert client.get(policies_path() + "/view/audit-records").json() == records_before


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


_CREATE_AND_VIEW_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

assert client.post("/datasets", json={"name": "orders"}).status_code == 201
assert client.post(
    "/datasets/orders/versions",
    json={"fields": [{"name": "email", "type": "string", "nullable": True}]},
).status_code == 201
policy = client.post(
    "/datasets/orders/versions/1/privacy-policies",
    json={
        "field": "email",
        "classification": "PII",
        "masking": "redact",
        "allowed_roles": [],
    },
)
assert policy.status_code == 201, policy.text
for _ in range(2):
    viewed = client.post(
        "/datasets/orders/versions/1/privacy-policies/view",
        json={"role": "guest", "rows": [{"email": "a@b.c"}]},
    )
    assert viewed.status_code == 200, viewed.text
print(policy.json()["id"])
"""

_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

response = client.get(
    "/datasets/orders/versions/1/privacy-policies/view/audit-records/summary"
)
assert response.status_code == 200, response.text
body = response.json()
assert body["dataset"] == "orders"
assert body["version"] == 1
assert len(body["groups"]) == 1
group = body["groups"][0]
assert group["field"] == "email"
assert group["role"] == "guest"
assert group["masking"] == "redact"
assert group["count"] == 2
print(group["policy_id"])
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
    db_path = tmp_path / "privacy-view-audit-summary.db"
    policy_id = _run_script(db_path, _CREATE_AND_VIEW_SCRIPT)

    # New interpreter: the summary is recomputed from the persisted records.
    summarized_policy_id = _run_script(db_path, _VERIFY_SCRIPT)
    assert summarized_policy_id == policy_id
