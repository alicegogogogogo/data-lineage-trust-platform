"""Tests for the privacy view masking-hit audit records."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import db_session

PROJECT_ROOT = Path(__file__).resolve().parents[1]


BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "email", "type": "string", "nullable": True},
    {"name": "ssn", "type": "string", "nullable": True},
    {"name": "age", "type": "integer", "nullable": True},
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
# Recording hits
# --------------------------------------------------------------------------- #


def test_masked_view_writes_one_record_per_masked_field(client: TestClient) -> None:
    make_dataset_with_version(client)
    email_policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": []},
    )
    ssn_policy = create_policy(
        client,
        {"field": "ssn", "classification": "secret", "masking": "redact",
         "allowed_roles": []},
    )

    response = post_view(
        client, "guest", [{"email": "alice@example.com", "ssn": "123", "id": 1}]
    )
    assert response.status_code == 200, response.text

    records = client.get(audit_path()).json()
    assert len(records) == 2
    by_field = {record["field"]: record for record in records}
    assert set(by_field) == {"email", "ssn"}
    for record in records:
        assert set(record) == {
            "sequence",
            "field",
            "policy_id",
            "role",
            "masking",
            "created_at",
        }
        assert record["role"] == "guest"
        datetime.fromisoformat(record["created_at"])
    assert by_field["email"]["policy_id"] == email_policy["id"]
    assert by_field["email"]["masking"] == "partial"
    assert by_field["ssn"]["policy_id"] == ssn_policy["id"]
    assert by_field["ssn"]["masking"] == "redact"
    assert [record["sequence"] for record in records] == [1, 2]


def test_same_field_masked_in_several_rows_is_one_record(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )

    response = post_view(
        client, "guest", [{"email": "a@b.c"}, {"email": "d@e.f"}, {"email": None}]
    )
    assert response.status_code == 200

    records = client.get(audit_path()).json()
    assert len(records) == 1
    assert records[0]["field"] == "email"
    assert records[0]["sequence"] == 1


def test_records_accumulate_across_views_with_contiguous_sequences(
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
    assert [record["sequence"] for record in records] == [1, 2, 3]
    assert [record["field"] for record in records] == ["email"] * 3


def test_no_hit_no_record(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": ["analyst"]},
    )

    # Role in the allowed list sees raw values: no hit.
    assert post_view(client, "analyst", [{"email": "a@b.c"}]).status_code == 200
    # Null values are never a hit.
    assert post_view(client, "guest", [{"email": None}]).status_code == 200
    # Uncovered fields are never a hit.
    assert post_view(client, "guest", [{"id": 1, "age": 30}]).status_code == 200
    # An empty row set is a normal response without any record.
    assert post_view(client, "guest", []).status_code == 200

    assert client.get(audit_path()).json() == []


def test_view_without_policies_writes_no_record(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    assert client.get(audit_path()).json() == []


def test_disabled_policy_writes_no_record_until_re_enabled(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert (
        client.patch(
            f"{policies_path()}/{policy['id']}", json={"enabled": False}
        ).status_code
        == 200
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    assert client.get(audit_path()).json() == []

    assert (
        client.patch(
            f"{policies_path()}/{policy['id']}", json={"enabled": True}
        ).status_code
        == 200
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    records = client.get(audit_path()).json()
    assert len(records) == 1
    assert records[0]["sequence"] == 1
    assert records[0]["policy_id"] == policy["id"]


def test_view_response_is_unchanged_by_the_audit(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": ["analyst"]},
    )
    rows = [{"id": 1, "email": "alice@example.com"}, {"id": 2, "email": None}]

    response = post_view(client, "guest", rows)
    assert response.status_code == 200
    assert response.json() == {
        "dataset": "orders",
        "version": 1,
        "rows": [
            {"id": 1, "email": "aom"},
            {"id": 2, "email": None},
        ],
    }


def test_rejected_view_writes_no_record(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )

    assert (
        client.post(
            policies_path() + "/view",
            json={"role": "  ", "rows": [{"email": "a@b.c"}]},
        ).status_code
        == 422
    )
    assert (
        post_view(client, "guest", [{"email": "a@b.c"}], dataset="ghost").status_code  # type: ignore[arg-type]
        == 404
    )
    assert client.get(audit_path()).json() == []


def test_records_are_scoped_to_their_version(client: TestClient) -> None:
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
    assert client.get(audit_path("orders", 2)).json() == []
    assert len(client.get(audit_path()).json()) == 1


# --------------------------------------------------------------------------- #
# Query endpoint shape
# --------------------------------------------------------------------------- #


def test_audit_records_unknown_dataset_or_version_returns_404(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    unknown_dataset = client.get(audit_path("ghost"))
    unknown_version = client.get(audit_path("orders", 9))
    assert unknown_dataset.status_code == 404
    assert unknown_version.status_code == 404
    for response in (unknown_dataset, unknown_version):
        assert response.json()["error"] == "not_found"


def test_audit_records_reject_body_and_query_params(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    before = client.get(audit_path()).json()

    with_body = client.request("GET", audit_path(), content=b"{}")
    with_query = client.get(audit_path(), params={"limit": 1})
    assert with_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, with_query):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"

    # The rejections wrote nothing.
    assert client.get(audit_path()).json() == before


def test_audit_records_shape_errors_keep_404_precedence(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert client.get(audit_path("ghost"), params={"x": "1"}).status_code == 404
    assert (
        client.request("GET", audit_path("orders", 9), content=b"{}").status_code
        == 404
    )


def test_audit_records_are_immutable(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200

    with db_session() as conn:
        with pytest.raises(sqlite3.Error):
            conn.execute("UPDATE privacy_view_audit_records SET role = 'x'")
        conn.rollback()
        with pytest.raises(sqlite3.Error):
            conn.execute("DELETE FROM privacy_view_audit_records")
        conn.rollback()

    assert len(client.get(audit_path()).json()) == 1


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_views_have_unique_contiguous_sequences(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )

    count = 12
    failures: list[object] = []
    barrier = threading.Barrier(count)

    def view(index: int) -> None:
        local = TestClient(client.app)
        barrier.wait()
        try:
            response = local.post(
                policies_path() + "/view",
                json={"role": f"role-{index}", "rows": [{"email": "a@b.c"}]},
            )
            assert response.status_code == 200, response.text
        except BaseException as exc:  # pragma: no cover - failure reporting
            failures.append(exc)

    threads = [threading.Thread(target=view, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures
    records = client.get(audit_path()).json()
    assert [record["sequence"] for record in records] == list(range(1, count + 1))
    assert sorted(record["role"] for record in records) == sorted(
        f"role-{index}" for index in range(count)
    )


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

records = client.get("/datasets/orders/versions/1/privacy-policies/view/audit-records")
assert records.status_code == 200, records.text
body = records.json()
assert len(body) == 1
record = body[0]
assert record["sequence"] == 1
assert record["field"] == "email"
assert record["role"] == "guest"
assert record["masking"] == "redact"
print(record["policy_id"])
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


def test_audit_records_survive_a_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "privacy-view-audit.db"
    policy_id = _run_script(db_path, _CREATE_AND_VIEW_SCRIPT)

    # New interpreter: the record written before the restart is still listed.
    recorded_policy_id = _run_script(db_path, _VERIFY_SCRIPT)
    assert recorded_policy_id == policy_id
