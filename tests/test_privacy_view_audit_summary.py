"""Tests for the read-only privacy view masking-hit audit summary."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import db_session

PROJECT_ROOT = Path(__file__).resolve().parents[1]


BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "email", "type": "string", "nullable": True},
    {"name": "ssn", "type": "string", "nullable": True},
]


def make_dataset_with_version(
    client: TestClient, name: str = "orders", fields: list[dict] | None = None
) -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions",
        json={"fields": fields if fields is not None else BASE_FIELDS},
    )
    assert response.status_code == 201, response.text


def policies_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/privacy-policies"


def audit_path(dataset: str = "orders", version: int = 1) -> str:
    return policies_path(dataset, version) + "/view/audit-records"


def summary_path(dataset: str = "orders", version: int = 1) -> str:
    return audit_path(dataset, version) + "/summary"


def create_policy(client: TestClient, payload: dict, **path: object) -> dict:
    response = client.post(policies_path(**path), json=payload)  # type: ignore[arg-type]
    assert response.status_code == 201, response.text
    return response.json()


def post_view(client: TestClient, role: str, rows: list[dict], **path: object):
    return client.post(
        policies_path(**path) + "/view",  # type: ignore[arg-type]
        json={"role": role, "rows": rows},
    )


def get_summary(client: TestClient, *path_args: object) -> dict:
    target = summary_path(*path_args) if path_args else summary_path()  # type: ignore[arg-type]
    response = client.get(target)
    assert response.status_code == 200, response.text
    return response.json()


GROUP_FIELDS = {
    "field",
    "policy_id",
    "role",
    "masking",
    "hit_count",
    "first_hit_at",
    "last_hit_at",
}


# --------------------------------------------------------------------------- #
# Empty state and response shape
# --------------------------------------------------------------------------- #


def test_summary_without_records_is_empty_not_an_error(client: TestClient) -> None:
    make_dataset_with_version(client)
    body = get_summary(client)
    assert set(body) == {"dataset", "version", "groups"}
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["groups"] == []


def test_summary_is_get_only(client: TestClient) -> None:
    make_dataset_with_version(client)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, summary_path())
        assert response.status_code == 405, method


# --------------------------------------------------------------------------- #
# Grouping and counts
# --------------------------------------------------------------------------- #


def test_summary_groups_by_field_policy_role_and_masking(client: TestClient) -> None:
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

    assert post_view(
        client, "guest", [{"email": "a@b.c", "ssn": "123"}]
    ).status_code == 200
    assert post_view(
        client, "guest", [{"email": "d@e.f", "ssn": None}]
    ).status_code == 200
    assert post_view(
        client, "auditor", [{"email": "g@h.i"}]
    ).status_code == 200

    body = get_summary(client)
    assert body["dataset"] == "orders"
    assert body["version"] == 1

    groups = body["groups"]
    assert len(groups) == 3
    for group in groups:
        assert set(group) == GROUP_FIELDS

    keyed = {(g["field"], g["policy_id"], g["role"], g["masking"]): g for g in groups}
    email_guest = keyed[("email", email_policy["id"], "guest", "redact")]
    assert email_guest["hit_count"] == 2
    email_auditor = keyed[("email", email_policy["id"], "auditor", "redact")]
    assert email_auditor["hit_count"] == 1
    ssn_guest = keyed[("ssn", ssn_policy["id"], "guest", "partial")]
    assert ssn_guest["hit_count"] == 1

    # Groups sort by field, then policy id, role and masking, all ascending.
    assert [
        (g["field"], g["policy_id"], g["role"], g["masking"]) for g in groups
    ] == sorted(
        (g["field"], g["policy_id"], g["role"], g["masking"]) for g in groups
    )


def test_hit_count_counts_records_not_rows_or_fields(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )

    # Three masked rows in one view still write a single record.
    assert post_view(
        client, "guest", [{"email": "a@b.c"}, {"email": "d@e.f"}, {"email": "g@h.i"}]
    ).status_code == 200
    # Each later view adds exactly one more record for the same group.
    assert post_view(client, "guest", [{"email": "x@y.z"}]).status_code == 200

    groups = get_summary(client)["groups"]
    assert len(groups) == 1
    assert groups[0]["hit_count"] == 2
    assert groups[0]["field"] == "email"


def test_same_field_different_roles_or_masking_never_merge(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )

    for role in ("guest", "analyst", "auditor"):
        assert post_view(client, role, [{"email": "a@b.c"}]).status_code == 200

    groups = get_summary(client)["groups"]
    assert len(groups) == 3
    assert {g["role"] for g in groups} == {"guest", "analyst", "auditor"}
    assert all(g["field"] == "email" for g in groups)
    assert all(g["policy_id"] == policy["id"] for g in groups)
    assert all(g["masking"] == "redact" for g in groups)
    assert [g["role"] for g in groups] == ["analyst", "auditor", "guest"]


def test_first_and_last_hit_timestamps_match_the_records(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    for _ in range(3):
        assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200

    records = client.get(audit_path()).json()
    timestamps = [record["created_at"] for record in records]
    assert len(timestamps) == 3

    group = get_summary(client)["groups"][0]
    assert group["first_hit_at"] == min(timestamps)
    assert group["last_hit_at"] == max(timestamps)
    assert group["first_hit_at"] <= group["last_hit_at"]
    assert group["hit_count"] == len(timestamps)


def test_group_counts_partition_every_record(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    create_policy(
        client,
        {"field": "ssn", "classification": "secret", "masking": "partial",
         "allowed_roles": []},
    )
    for role in ("guest", "auditor"):
        assert post_view(
            client, role, [{"email": "a@b.c", "ssn": "12"}]
        ).status_code == 200

    records = client.get(audit_path()).json()
    groups = get_summary(client)["groups"]
    assert sum(g["hit_count"] for g in groups) == len(records)

    # The summary is recomputed on demand: identical records, identical result.
    assert get_summary(client) == get_summary(client)


def test_group_tie_break_order_includes_policy_and_masking(
    client: TestClient,
) -> None:
    # One policy per (version, field) exists in normal flow, so the policy id
    # and masking tie-breaks are exercised with persisted rows written
    # directly (the summary only reads these records; policy_id is a plain
    # integer reference and never restricts the read).
    make_dataset_with_version(client)
    with db_session() as conn:
        version_id = conn.execute(
            "SELECT id FROM schema_versions ORDER BY version LIMIT 1"
        ).fetchone()["id"]
        rows = [
            # (sequence, field, policy_id, role, masking, created_at)
            (1, "ssn", 3, "guest", "redact", "2026-01-01T00:00:00+00:00"),
            (2, "email", 2, "guest", "redact", "2026-01-02T08:00:00+00:00"),
            (3, "email", 2, "guest", "redact", "2026-01-05T09:00:00+00:00"),
            (4, "email", 2, "guest", "partial", "2026-01-03T00:00:00+00:00"),
            (5, "email", 2, "analyst", "redact", "2026-01-03T00:00:00+00:00"),
            (6, "email", 1, "admin", "partial", "2026-01-03T00:00:00+00:00"),
        ]
        for sequence, field, policy_id, role, masking, created_at in rows:
            conn.execute(
                "INSERT INTO privacy_view_audit_records ("
                "version_id, sequence, field, policy_id, role, masking, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (version_id, sequence, field, policy_id, role, masking, created_at),
            )

    groups = get_summary(client)["groups"]
    assert [
        (g["field"], g["policy_id"], g["role"], g["masking"]) for g in groups
    ] == [
        ("email", 1, "admin", "partial"),
        ("email", 2, "analyst", "redact"),
        ("email", 2, "guest", "partial"),
        ("email", 2, "guest", "redact"),
        ("ssn", 3, "guest", "redact"),
    ]

    redact_guest = next(
        g
        for g in groups
        if (g["field"], g["policy_id"], g["role"], g["masking"])
        == ("email", 2, "guest", "redact")
    )
    assert redact_guest["hit_count"] == 2
    assert redact_guest["first_hit_at"] == "2026-01-02T08:00:00+00:00"
    assert redact_guest["last_hit_at"] == "2026-01-05T09:00:00+00:00"


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
    assert get_summary(client, "orders", 2)["groups"] == []
    assert len(get_summary(client)["groups"]) == 1


# --------------------------------------------------------------------------- #
# Errors: 404 / 422, no writes
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
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "not_found"


def test_summary_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    before = get_summary(client)

    with_body = client.request("GET", summary_path(), content=b"{}")
    with_query = client.get(summary_path(), params={"limit": 1})
    assert with_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, with_query):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]

    # The rejections wrote nothing and the summary still reads identically.
    assert get_summary(client) == before
    assert len(client.get(audit_path()).json()) == 1


def test_summary_shape_errors_keep_404_precedence(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert client.get(summary_path("ghost"), params={"x": "1"}).status_code == 404
    assert (
        client.request(
            "GET", summary_path("orders", 9), content=b"{}"
        ).status_code
        == 404
    )


def test_summary_does_not_modify_records(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    records_before = client.get(audit_path()).json()

    for _ in range(3):
        assert get_summary(client)["groups"][0]["hit_count"] == 1
    assert client.get(audit_path()).json() == records_before


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
for role in ("guest", "auditor"):
    viewed = client.post(
        "/datasets/orders/versions/1/privacy-policies/view",
        json={"role": role, "rows": [{"email": "a@b.c"}]},
    )
    assert viewed.status_code == 200, viewed.text
print(policy.json()["id"])
"""

_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

summary = client.get(
    "/datasets/orders/versions/1/privacy-policies/view/audit-records/summary"
)
assert summary.status_code == 200, summary.text
body = summary.json()
assert body["dataset"] == "orders"
assert body["version"] == 1
groups = body["groups"]
assert len(groups) == 2
assert [g["role"] for g in groups] == ["auditor", "guest"]
for group in groups:
    assert set(group) == {
        "field", "policy_id", "role", "masking",
        "hit_count", "first_hit_at", "last_hit_at",
    }
    assert group["field"] == "email"
    assert group["masking"] == "redact"
    assert group["hit_count"] == 1
    assert group["first_hit_at"] == group["last_hit_at"]

# A second read in the same process returns an identical result.
again = client.get(
    "/datasets/orders/versions/1/privacy-policies/view/audit-records/summary"
)
assert again.status_code == 200, again.text
assert again.json() == body
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
    db_path = tmp_path / "privacy-view-summary.db"
    policy_id = _run_script(db_path, _CREATE_AND_VIEW_SCRIPT)

    # New interpreter: the summary is recomputed from the persisted records.
    assert _run_script(db_path, _VERIFY_SCRIPT) == "verified"
    assert policy_id
