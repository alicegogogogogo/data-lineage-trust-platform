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
from app import db as app_db

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


def test_same_field_masked_in_several_rows_writes_one_record_per_value(
    client: TestClient,
) -> None:
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

    # Two masked values -> two records (the null is not a hit); records are
    # never merged by field.
    records = client.get(audit_path()).json()
    assert len(records) == 2
    assert [record["field"] for record in records] == ["email", "email"]
    assert [record["sequence"] for record in records] == [1, 2]
    assert all(record["role"] == "guest" for record in records)


def test_each_masked_value_in_each_row_gets_its_own_record_in_order(
    client: TestClient,
) -> None:
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

    rows = [
        {"email": "a@x.io", "ssn": "1", "id": 1},   # email + ssn hit
        {"email": None, "ssn": "2", "id": 2},       # only ssn hits (null)
        {"email": "b@x.io", "id": 3},               # only email hits (field missing)
        {"id": 4},                                  # no covered field
    ]
    response = post_view(client, "guest", rows)
    assert response.status_code == 200, response.text

    records = client.get(audit_path()).json()
    # Hits follow row order; within a row policies sort by id (email first).
    assert [(r["field"], r["policy_id"]) for r in records] == [
        ("email", email_policy["id"]),
        ("ssn", ssn_policy["id"]),
        ("ssn", ssn_policy["id"]),
        ("email", email_policy["id"]),
    ]
    assert [record["sequence"] for record in records] == [1, 2, 3, 4]
    # All records of one view share a single write time.
    assert len({record["created_at"] for record in records}) == 1


def test_records_of_later_views_continue_the_sequence_and_get_new_timestamps(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )

    assert post_view(
        client, "guest", [{"email": "a@b.c"}, {"email": "d@e.f"}]
    ).status_code == 200
    assert post_view(
        client, "guest", [{"email": "g@h.i"}, {"email": "j@k.l"}, {"email": "m@n.o"}]
    ).status_code == 200

    records = client.get(audit_path()).json()
    assert len(records) == 5
    assert [record["sequence"] for record in records] == [1, 2, 3, 4, 5]
    assert [record["field"] for record in records] == ["email"] * 5
    # Each view keeps its own shared write time, monotonic with the sequences.
    assert records[0]["created_at"] == records[1]["created_at"]
    assert records[2]["created_at"] == records[3]["created_at"] == records[4]["created_at"]
    assert records[1]["created_at"] <= records[2]["created_at"]


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
    rows_per_view = 3
    failures: list[object] = []
    barrier = threading.Barrier(count)

    def view(index: int) -> None:
        local = TestClient(client.app)
        barrier.wait()
        try:
            response = local.post(
                policies_path() + "/view",
                json={
                    "role": f"role-{index}",
                    "rows": [{"email": f"u{index}-{row}@b.c"}
                             for row in range(rows_per_view)],
                },
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
    # One record per masked value: no gaps, no duplicate sequences.
    assert len(records) == count * rows_per_view
    assert [record["sequence"] for record in records] == list(
        range(1, count * rows_per_view + 1)
    )
    # Every view landed as a complete batch of its own rows.
    for index in range(count):
        role_records = [r for r in records if r["role"] == f"role-{index}"]
        assert len(role_records) == rows_per_view
        assert len({r["created_at"] for r in role_records}) == 1


# --------------------------------------------------------------------------- #
# Cross-process concurrency
# --------------------------------------------------------------------------- #


_WORKER_VIEW_SCRIPT = """
import sys
from fastapi.testclient import TestClient
from app.main import app

role = sys.argv[1]
count = int(sys.argv[2])
client = TestClient(app)
response = client.post(
    "/datasets/orders/versions/1/privacy-policies/view",
    json={
        "role": role,
        "rows": [{"email": f"{role}-{i}@b.c"} for i in range(count)],
    },
)
assert response.status_code == 200, response.text
assert len(response.json()["rows"]) == count
"""


def test_concurrent_views_across_processes_land_every_record_once(
    client: TestClient, tmp_path: Path
) -> None:
    # The fixture isolates the in-process client to its own database; the
    # workers must use a shared file set up here once.
    db_path = tmp_path / "privacy-view-concurrent.db"
    os.environ["DATA_LINEAGE_DB"] = str(db_path)
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )

    worker_count = 6
    rows_per_view = 4
    env = os.environ.copy()
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER_VIEW_SCRIPT, f"role-{i}",
             str(rows_per_view)],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(worker_count)
    ]
    for process in processes:
        stdout, stderr = process.communicate()
        assert process.returncode == 0, stderr
        assert stdout == ""

    records = client.get(audit_path()).json()
    total = worker_count * rows_per_view
    assert len(records) == total
    # Sequences are unique and contiguous across the processes.
    assert sorted(record["sequence"] for record in records) == list(
        range(1, total + 1)
    )
    # Each process's batch is complete: three... N records per role, one time.
    for index in range(worker_count):
        role_records = [r for r in records if r["role"] == f"role-{index}"]
        assert len(role_records) == rows_per_view
        assert {r["field"] for r in role_records} == {"email"}
        assert len({r["created_at"] for r in role_records}) == 1


# --------------------------------------------------------------------------- #
# Audit persistence never breaks the view
# --------------------------------------------------------------------------- #


def test_view_succeeds_without_half_records_when_audit_write_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )

    real_connect = sqlite3.connect

    class FailingAuditConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[no-untyped-def]
            if "INSERT INTO privacy_view_audit_records" in sql:
                # A non-contention persistence failure that retries could not fix.
                raise sqlite3.OperationalError("disk I/O error")
            return super().execute(sql, *args, **kwargs)

    def failing_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["factory"] = FailingAuditConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(app_db.sqlite3, "connect", failing_connect)

    response = post_view(
        client, "guest", [{"email": "a@b.c"}, {"email": "d@e.f"}]
    )
    # The view still returns the masked result normally.
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == [{"email": "***"}, {"email": "***"}]

    # The failed batch was rolled back: no half records, no broken sequence.
    assert client.get(audit_path()).json() == []


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
