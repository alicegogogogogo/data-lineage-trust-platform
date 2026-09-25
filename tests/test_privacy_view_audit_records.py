"""Tests for the append-only masking-hit audit of privacy view requests."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "email", "type": "string", "nullable": True},
    {"name": "ssn", "type": "string", "nullable": True},
]

RECORD_FIELDS = {
    "id",
    "sequence",
    "field",
    "policy_id",
    "role",
    "masking",
    "created_at",
}


def make_dataset_with_version(client: TestClient, name: str = "orders") -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(f"/datasets/{name}/versions", json={"fields": FIELDS})
    assert response.status_code == 201, response.text


def policies_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/privacy-policies"


def records_path(dataset: str = "orders", version: int = 1) -> str:
    return policies_path(dataset, version) + "/view-records"


def create_policy(
    client: TestClient,
    field: str,
    *,
    masking: str = "redact",
    allowed_roles: list[str] | None = None,
    dataset: str = "orders",
    version: int = 1,
) -> dict:
    response = client.post(
        policies_path(dataset, version),
        json={
            "field": field,
            "classification": "PII",
            "masking": masking,
            "allowed_roles": allowed_roles if allowed_roles is not None else [],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def post_view(
    client: TestClient,
    role: str,
    rows: list[dict],
    *,
    dataset: str = "orders",
    version: int = 1,
):
    return client.post(
        policies_path(dataset, version) + "/view",
        json={"role": role, "rows": rows},
    )


# --------------------------------------------------------------------------- #
# Recording hits
# --------------------------------------------------------------------------- #


def test_masked_fields_are_recorded(client: TestClient) -> None:
    make_dataset_with_version(client)
    email = create_policy(client, "email", masking="partial")
    ssn = create_policy(client, "ssn", masking="redact")

    response = post_view(
        client,
        "guest",
        [
            {"id": 1, "email": "alice@example.com", "ssn": "123-45-6789"},
            {"id": 2, "email": "bob@example.com", "ssn": "987-65-4321"},
        ],
    )
    assert response.status_code == 200, response.text

    records = client.get(records_path()).json()
    assert len(records) == 4
    for record in records:
        assert set(record) == RECORD_FIELDS
        assert isinstance(record["id"], int)
        datetime.fromisoformat(record["created_at"])
    assert [record["sequence"] for record in records] == [1, 2, 3, 4]
    assert [record["field"] for record in records] == [
        "email",
        "ssn",
        "email",
        "ssn",
    ]
    assert [record["policy_id"] for record in records] == [
        email["id"],
        ssn["id"],
        email["id"],
        ssn["id"],
    ]
    assert [record["role"] for record in records] == ["guest"] * 4
    assert [record["masking"] for record in records] == [
        "partial",
        "redact",
        "partial",
        "redact",
    ]


def test_records_accumulate_across_views_in_write_order(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(client, "email")

    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    assert post_view(client, "auditor", [{"email": "x@y.z"}]).status_code == 200

    records = client.get(records_path()).json()
    assert [record["sequence"] for record in records] == [1, 2]
    assert [record["role"] for record in records] == ["guest", "auditor"]


def test_null_absent_and_uncovered_values_do_not_hit(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(client, "email")
    create_policy(client, "ssn")

    response = post_view(
        client,
        "guest",
        [
            {"id": 1, "email": None, "ssn": None},  # null values: no hit
            {"id": 2},  # covered fields absent: no hit
            {"id": 3, "email": "a@b.c"},  # only email hits
        ],
    )
    assert response.status_code == 200, response.text
    # Masking semantics are unchanged: nulls stay null, absent stays absent.
    rows = response.json()["rows"]
    assert rows[0] == {"id": 1, "email": None, "ssn": None}
    assert rows[1] == {"id": 2}
    assert rows[2] == {"id": 3, "email": "***"}

    records = client.get(records_path()).json()
    assert [record["field"] for record in records] == ["email"]
    assert records[0]["sequence"] == 1


def test_allowed_role_produces_no_records(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(client, "email", allowed_roles=["analyst"])

    response = post_view(client, "analyst", [{"email": "a@b.c"}])
    assert response.status_code == 200
    assert response.json()["rows"] == [{"email": "a@b.c"}]
    assert client.get(records_path()).json() == []

    # Another role is masked and does hit.
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    records = client.get(records_path()).json()
    assert [record["role"] for record in records] == ["guest"]


def test_disabled_policy_does_not_hit_until_reenabled(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client, "email")

    assert (
        client.patch(
            f"{policies_path()}/{policy['id']}", json={"enabled": False}
        ).status_code
        == 200
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    assert client.get(records_path()).json() == []

    assert (
        client.patch(
            f"{policies_path()}/{policy['id']}", json={"enabled": True}
        ).status_code
        == 200
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    records = client.get(records_path()).json()
    assert [record["sequence"] for record in records] == [1]
    assert records[0]["policy_id"] == policy["id"]


def test_empty_rows_and_no_policies_write_nothing(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(client, "email")

    response = post_view(client, "guest", [])
    assert response.status_code == 200
    assert response.json() == {"dataset": "orders", "version": 1, "rows": []}
    assert client.get(records_path()).json() == []

    # Rows without any covered field also write nothing.
    assert post_view(client, "guest", [{"id": 1}, {"id": 2}]).status_code == 200
    assert client.get(records_path()).json() == []


def test_view_response_is_unchanged_by_auditing(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(client, "email", masking="partial")
    create_policy(client, "ssn")

    response = post_view(
        client,
        "guest",
        [{"id": 1, "email": "alice@example.com", "ssn": "123-45-6789"}],
    )
    assert response.status_code == 200
    assert response.json() == {
        "dataset": "orders",
        "version": 1,
        "rows": [{"id": 1, "email": "aom", "ssn": "***"}],
    }


def test_failed_views_write_nothing(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(client, "email")

    assert (
        client.post(policies_path() + "/view", json={"role": "  ", "rows": []})
        .status_code
        == 422
    )
    assert (
        post_view(client, "guest", [{"email": "a@b.c"}], dataset="ghost").status_code
        == 404
    )
    assert client.get(records_path()).json() == []


def test_records_are_scoped_per_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(client, "email")
    assert (
        client.post("/datasets/orders/versions", json={"fields": FIELDS}).status_code
        == 201
    )
    create_policy(client, "email", version=2)

    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    assert (
        post_view(client, "guest", [{"email": "a@b.c"}], version=2).status_code == 200
    )

    v1 = client.get(records_path()).json()
    v2 = client.get(records_path(version=2)).json()
    assert [record["sequence"] for record in v1] == [1]
    assert [record["sequence"] for record in v2] == [1]


# --------------------------------------------------------------------------- #
# Listing endpoint shape
# --------------------------------------------------------------------------- #


def test_list_empty_when_no_hits(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.get(records_path())
    assert response.status_code == 200
    assert response.json() == []


def test_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    for path in (records_path("ghost"), records_path("orders", 9)):
        response = client.get(path)
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"
        assert set(response.json()) == {"error", "detail"}


def test_body_and_query_parameters_are_422(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(client, "email")
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200

    with_body = client.request("GET", records_path(), content=b"{}")
    assert with_body.status_code == 422
    assert with_body.json()["error"] == "validation_error"
    assert set(with_body.json()) == {"error", "detail"}

    with_query = client.get(records_path(), params={"limit": 1})
    assert with_query.status_code == 422
    assert with_query.json()["error"] == "validation_error"

    # Nothing was written or removed by the rejected reads.
    assert len(client.get(records_path()).json()) == 1


def test_404_precedence_over_shape_errors(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert (
        client.get(records_path("ghost"), params={"x": "1"}).status_code == 404
    )
    assert (
        client.request("GET", records_path("ghost"), content=b"{}").status_code
        == 404
    )


# --------------------------------------------------------------------------- #
# Immutability
# --------------------------------------------------------------------------- #


def test_records_are_immutable_in_the_database(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(client, "email")
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    record = client.get(records_path()).json()[0]

    from app.db import database_path

    with sqlite3.connect(database_path()) as direct:
        direct.execute("PRAGMA foreign_keys = ON")
        for statement in (
            "UPDATE privacy_view_audit_records SET field = 'x' WHERE id = ?",
            "DELETE FROM privacy_view_audit_records WHERE id = ?",
        ):
            try:
                direct.execute(statement, (record["id"],))
                direct.commit()
            except sqlite3.Error:
                direct.rollback()
            else:  # pragma: no cover - the trigger must always fire
                raise AssertionError("immutability trigger did not fire")

    listed = client.get(records_path()).json()
    assert [row["field"] for row in listed] == ["email"]


def test_no_mutating_http_routes(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(client, "email")
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200

    # There is no per-record route at all, so records cannot be changed or
    # removed over HTTP. PATCH matches the pre-existing
    # ".../privacy-policies/{policy_id}" route with "view-records" as a
    # non-integer id and is rejected with 422 before touching anything; the
    # other methods get 405.
    patch = client.patch(records_path())
    assert patch.status_code == 422
    for method in ("post", "put", "delete"):
        response = getattr(client, method)(records_path())
        assert response.status_code == 405, (method, response.status_code)
    assert len(client.get(records_path()).json()) == 1


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_views_get_continuous_sequences(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(client, "email")
    create_policy(client, "ssn")

    count = 16
    failures: list[Exception] = []
    barrier = threading.Barrier(count)

    def worker(index: int) -> None:
        local = TestClient(client.app)
        barrier.wait()
        try:
            response = local.post(
                policies_path() + "/view",
                json={
                    "role": f"role-{index}",
                    "rows": [{"email": "a@b.c", "ssn": "123"}],
                },
            )
            assert response.status_code == 200, response.text
        except Exception as exc:  # pragma: no cover - reported below
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures, failures
    records = client.get(records_path()).json()
    # Two hits per view: sequences are continuous, unique and complete.
    assert [record["sequence"] for record in records] == list(
        range(1, 2 * count + 1)
    )
    assert len({record["id"] for record in records}) == 2 * count


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response):
    assert response.status_code in (200, 201), response.text
    return response.json()

ok(client.post("/datasets", json={"name": "orders"}))
ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "email", "type": "string", "nullable": True},
    ]},
))
base = "/datasets/orders/versions/1/privacy-policies"
ok(client.post(base, json={
    "field": "email",
    "classification": "PII",
    "masking": "redact",
    "allowed_roles": [],
}))
for index in range(2):
    ok(client.post(base + "/view", json={
        "role": "guest",
        "rows": [{"id": index, "email": "a@b.c"}],
    }))
print("created")
"""

VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
base = "/datasets/orders/versions/1/privacy-policies"

records = client.get(base + "/view-records")
assert records.status_code == 200, records.text
rows = records.json()
assert [row["sequence"] for row in rows] == [1, 2]
assert [row["field"] for row in rows] == ["email", "email"]
assert [row["role"] for row in rows] == ["guest", "guest"]

# A view after the restart continues the same sequence seamlessly.
view = client.post(base + "/view", json={
    "role": "auditor",
    "rows": [{"id": 9, "email": "b@c.d"}],
})
assert view.status_code == 200, view.text
again = client.get(base + "/view-records").json()
assert [row["sequence"] for row in again] == [1, 2, 3]
assert again[-1]["role"] == "auditor"
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


def test_hit_records_survive_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-view-records.db"
    assert _run_script(db_path, CREATE_SCRIPT) == "created"
    assert db_path.exists()
    assert _run_script(db_path, VERIFY_SCRIPT) == "verified"
