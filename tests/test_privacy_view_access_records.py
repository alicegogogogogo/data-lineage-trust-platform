"""Tests for the privacy view per-request access records."""

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

from app import repository
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


def access_path(dataset: str = "orders", version: int = 1) -> str:
    return policies_path(dataset, version) + "/view/access-records"


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
# Recording accesses
# --------------------------------------------------------------------------- #


def test_successful_view_writes_one_access_record(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )

    response = post_view(
        client, "guest", [{"email": "a@b.c"}, {"email": "d@e.f"}, {"email": None}]
    )
    assert response.status_code == 200, response.text

    records = client.get(access_path()).json()
    assert len(records) == 1
    record = records[0]
    assert set(record) == {
        "sequence",
        "role",
        "row_count",
        "masked_count",
        "created_at",
    }
    assert record["sequence"] == 1
    assert record["role"] == "guest"
    assert record["row_count"] == 3
    # Two non-null values were masked; the null value is no hit.
    assert record["masked_count"] == 2
    datetime.fromisoformat(record["created_at"])


def test_masked_count_matches_the_hit_records_of_the_same_view(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": []},
    )
    create_policy(
        client,
        {"field": "ssn", "classification": "secret", "masking": "redact",
         "allowed_roles": []},
    )

    assert (
        post_view(
            client,
            "guest",
            [
                {"email": "alice@example.com", "ssn": "123"},
                {"email": "bob@example.com", "ssn": "456"},
                {"email": None, "id": 3},
            ],
        ).status_code
        == 200
    )
    assert post_view(client, "guest", [{"ssn": "789"}]).status_code == 200

    hits = client.get(audit_path()).json()
    accesses = client.get(access_path()).json()
    assert [record["sequence"] for record in accesses] == [1, 2]
    assert [record["masked_count"] for record in accesses] == [4, 1]
    assert sum(record["masked_count"] for record in accesses) == len(hits)


def test_view_without_any_masking_still_leaves_a_record(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": ["analyst"]},
    )

    # Allowed role sees raw values: no hit, but the access is recorded.
    assert post_view(client, "analyst", [{"email": "a@b.c"}]).status_code == 200
    # Null values are never a hit; the access is still recorded.
    assert post_view(client, "guest", [{"email": None}]).status_code == 200
    # Uncovered fields only; the access is still recorded.
    assert post_view(client, "guest", [{"id": 1, "age": 30}]).status_code == 200

    records = client.get(access_path()).json()
    assert [record["sequence"] for record in records] == [1, 2, 3]
    assert [record["role"] for record in records] == ["analyst", "guest", "guest"]
    assert [record["row_count"] for record in records] == [1, 1, 1]
    assert [record["masked_count"] for record in records] == [0, 0, 0]
    assert client.get(audit_path()).json() == []


def test_empty_row_set_is_recorded_with_zero_counts(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )

    assert post_view(client, "guest", []).status_code == 200

    records = client.get(access_path()).json()
    assert len(records) == 1
    assert records[0]["row_count"] == 0
    assert records[0]["masked_count"] == 0


def test_view_without_policies_still_leaves_a_record(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    records = client.get(access_path()).json()
    assert len(records) == 1
    assert records[0]["row_count"] == 1
    assert records[0]["masked_count"] == 0


def test_each_view_writes_exactly_one_record_with_contiguous_sequences(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )

    for index in range(3):
        assert (
            post_view(client, f"role-{index}", [{"email": "a@b.c"}]).status_code
            == 200
        )

    records = client.get(access_path()).json()
    assert [record["sequence"] for record in records] == [1, 2, 3]
    assert [record["role"] for record in records] == [
        "role-0",
        "role-1",
        "role-2",
    ]


def test_rejected_view_writes_no_access_record(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )

    # Blank role -> 422.
    assert (
        client.post(
            policies_path() + "/view",
            json={"role": "  ", "rows": [{"email": "a@b.c"}]},
        ).status_code
        == 422
    )
    # Rows not a list of objects -> 422.
    assert (
        client.post(
            policies_path() + "/view",
            json={"role": "guest", "rows": ["not-a-row"]},
        ).status_code
        == 422
    )
    # Malformed JSON -> 422.
    assert (
        client.post(
            policies_path() + "/view",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        ).status_code
        == 422
    )
    # Unknown dataset -> 404.
    assert (
        post_view(client, "guest", [{"email": "a@b.c"}], dataset="ghost").status_code  # type: ignore[arg-type]
        == 404
    )

    assert client.get(access_path()).json() == []
    assert client.get(audit_path()).json() == []


def test_access_records_are_scoped_to_their_version(client: TestClient) -> None:
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
    assert client.get(access_path("orders", 2)).json() == []
    assert len(client.get(access_path()).json()) == 1


def test_view_response_is_unchanged_by_the_access_record(
    client: TestClient,
) -> None:
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


# --------------------------------------------------------------------------- #
# Query endpoint shape
# --------------------------------------------------------------------------- #


def test_access_records_unknown_dataset_or_version_returns_404(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    unknown_dataset = client.get(access_path("ghost"))
    unknown_version = client.get(access_path("orders", 9))
    assert unknown_dataset.status_code == 404
    assert unknown_version.status_code == 404
    for response in (unknown_dataset, unknown_version):
        assert response.json()["error"] == "not_found"


def test_access_records_reject_body_and_query_params(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert post_view(client, "guest", [{"id": 1}]).status_code == 200
    before = client.get(access_path()).json()

    with_body = client.request("GET", access_path(), content=b"{}")
    with_query = client.get(access_path(), params={"limit": 1})
    assert with_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, with_query):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"

    # The rejections wrote nothing.
    assert client.get(access_path()).json() == before


def test_access_records_shape_errors_keep_404_precedence(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    assert client.get(access_path("ghost"), params={"x": "1"}).status_code == 404
    assert (
        client.request("GET", access_path("orders", 9), content=b"{}").status_code
        == 404
    )


def test_access_records_are_immutable(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert post_view(client, "guest", [{"id": 1}]).status_code == 200

    with db_session() as conn:
        with pytest.raises(sqlite3.Error):
            conn.execute("UPDATE privacy_view_access_records SET role = 'x'")
        conn.rollback()
        with pytest.raises(sqlite3.Error):
            conn.execute("DELETE FROM privacy_view_access_records")
        conn.rollback()

    assert len(client.get(access_path()).json()) == 1


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_views_have_unique_contiguous_access_sequences(
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
    records = client.get(access_path()).json()
    assert [record["sequence"] for record in records] == list(range(1, count + 1))
    assert sorted(record["role"] for record in records) == sorted(
        f"role-{index}" for index in range(count)
    )
    # Every view also wrote its hit record; the totals cross-check.
    hits = client.get(audit_path()).json()
    assert len(hits) == count
    assert sum(record["masked_count"] for record in records) == len(hits)


def test_view_succeeds_when_optimistic_append_retries_are_exhausted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )

    # Simulate a cross-process writer winning every tail race: the optimistic
    # batch append on the request connection always collides, exhausting the
    # bounded retries. The view must still return its masked result and both
    # the hit records and the access record must still be written (via the
    # serialized fallback).
    real_batch = repository._append_privacy_view_audit_batch
    calls = 0

    def colliding_batch(conn, version_id, role, hits):
        nonlocal calls
        calls += 1
        if calls <= repository._PRIVACY_VIEW_AUDIT_MAX_ATTEMPTS:
            raise sqlite3.IntegrityError("simulated cross-process collision")
        return real_batch(conn, version_id, role, hits)

    monkeypatch.setattr(
        repository, "_append_privacy_view_audit_batch", colliding_batch
    )

    response = post_view(client, "guest", [{"email": "a@b.c"}, {"email": "d@e.f"}])
    assert response.status_code == 200
    assert response.json()["rows"] == [{"email": "***"}, {"email": "***"}]

    hits = client.get(audit_path()).json()
    assert [record["sequence"] for record in hits] == [1, 2]
    accesses = client.get(access_path()).json()
    assert len(accesses) == 1
    assert accesses[0]["sequence"] == 1
    assert accesses[0]["row_count"] == 2
    assert accesses[0]["masked_count"] == len(hits)


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
    json={"role": "guest", "rows": [{"email": "a@b.c"}, {"email": None}]},
)
assert viewed.status_code == 200, viewed.text
# A view that masks nothing is recorded too.
plain = client.post(
    "/datasets/orders/versions/1/privacy-policies/view",
    json={"role": "guest", "rows": []},
)
assert plain.status_code == 200, plain.text
print("created")
"""

_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

records = client.get(
    "/datasets/orders/versions/1/privacy-policies/view/access-records"
)
assert records.status_code == 200, records.text
body = records.json()
assert [record["sequence"] for record in body] == [1, 2]
assert body[0]["role"] == "guest"
assert body[0]["row_count"] == 2
assert body[0]["masked_count"] == 1
assert body[1]["row_count"] == 0
assert body[1]["masked_count"] == 0
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


def test_access_records_survive_a_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "privacy-view-access.db"
    assert _run_script(db_path, _CREATE_AND_VIEW_SCRIPT) == "created"

    # New interpreter: the records written before the restart are still listed.
    assert _run_script(db_path, _VERIFY_SCRIPT) == "verified"
