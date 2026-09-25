"""Tests for the privacy view access records (one-per-view access trail)."""

from __future__ import annotations

import json
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


def audit_path(dataset: str = "orders", version: int = 1) -> str:
    return policies_path(dataset, version) + "/view/audit-records"


def access_path(dataset: str = "orders", version: int = 1) -> str:
    return policies_path(dataset, version) + "/view/access-records"


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
# Recording one record per view
# --------------------------------------------------------------------------- #


def test_successful_masked_view_writes_exactly_one_access_record(
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

    response = post_view(
        client,
        "guest",
        [
            {"email": "alice@example.com", "ssn": "123", "id": 1},
            {"email": "bob@example.com", "ssn": "456", "id": 2},
            {"email": None, "id": 3},
        ],
    )
    assert response.status_code == 200, response.text

    # One access record for the whole view, even though four values were
    # masked across its rows and fields.
    records = client.get(access_path()).json()
    assert len(records) == 1
    record = records[0]
    assert set(record) == {"sequence", "role", "row_count", "masked_count",
                           "created_at"}
    assert record["sequence"] == 1
    assert record["role"] == "guest"
    assert record["row_count"] == 3
    assert record["masked_count"] == 4
    datetime.fromisoformat(record["created_at"])

    # The masked_count cross-checks the number of per-value hit records the
    # same view wrote.
    hits = client.get(audit_path()).json()
    assert len(hits) == record["masked_count"]


def test_view_without_any_masked_value_still_writes_one_access_record(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": ["analyst"]},
    )

    # An allowed role sees raw values and masks nothing: still traced once.
    assert post_view(client, "analyst", [{"email": "a@b.c"}]).status_code == 200
    # A null value masks nothing: still traced once.
    assert post_view(client, "guest", [{"email": None}]).status_code == 200
    # Uncovered fields mask nothing: still traced once.
    assert post_view(client, "guest", [{"id": 1, "age": 30}]).status_code == 200

    records = client.get(access_path()).json()
    assert [record["sequence"] for record in records] == [1, 2, 3]
    assert all(record["masked_count"] == 0 for record in records)
    assert [record["row_count"] for record in records] == [1, 1, 1]
    assert [record["role"] for record in records] == ["analyst", "guest", "guest"]

    # No masking-hit records were written at all.
    assert client.get(audit_path()).json() == []


def test_view_without_policies_writes_one_access_record(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200

    records = client.get(access_path()).json()
    assert len(records) == 1
    assert records[0] == {
        "sequence": 1,
        "role": "guest",
        "row_count": 1,
        "masked_count": 0,
        "created_at": records[0]["created_at"],
    }


def test_empty_row_set_is_traced_with_zero_counts(client: TestClient) -> None:
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
    datetime.fromisoformat(records[0]["created_at"])
    assert client.get(audit_path()).json() == []


def test_views_accumulate_contiguous_sequences_independent_of_hits(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": ["analyst"]},
    )

    # Masked, then nothing masked, then two masked values across two rows.
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    assert post_view(client, "analyst", [{"email": "d@e.f"}]).status_code == 200
    assert post_view(
        client,
        "guest",
        [{"email": "g@h.i"}, {"email": "j@k.l"}],
    ).status_code == 200

    records = client.get(access_path()).json()
    assert [record["sequence"] for record in records] == [1, 2, 3]
    assert [(record["role"], record["row_count"], record["masked_count"])
            for record in records] == [
        ("guest", 1, 1),
        ("analyst", 1, 0),
        ("guest", 2, 2),
    ]

    # The hit log has its own independent per-value sequence: one hit from
    # the first view plus two from the third, and the sum of masked_count
    # equals the total hit-record count.
    hits = client.get(audit_path()).json()
    assert [hit["sequence"] for hit in hits] == [1, 2, 3]
    assert sum(record["masked_count"] for record in records) == len(hits)


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
    # Each version starts its own continuous sequence at 1.
    assert client.get(access_path("orders", 2)).json() == []
    assert post_view(
        client, "guest", [{"email": "x@y.z"}], dataset="orders", version=2
    ).status_code == 200
    v2 = client.get(access_path("orders", 2)).json()
    assert [record["sequence"] for record in v2] == [1]
    assert [record["sequence"] for record in client.get(access_path()).json()] == [1]


def test_version_without_views_returns_empty_array_not_error(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    response = client.get(access_path())
    assert response.status_code == 200
    assert response.json() == []


def test_view_response_is_unchanged_by_the_access_trail(
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
# Rejected or failed views leave no trace
# --------------------------------------------------------------------------- #


def test_rejected_views_write_no_access_record(client: TestClient) -> None:
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
            json={"role": "   ", "rows": [{"email": "a@b.c"}]},
        ).status_code
        == 422
    )
    # Rows not a list -> 422.
    assert (
        client.post(
            policies_path() + "/view",
            json={"role": "guest", "rows": {"email": "a@b.c"}},
        ).status_code
        == 422
    )
    # Malformed JSON -> 422.
    malformed = client.post(
        policies_path() + "/view",
        content=b'{"role": "guest", "rows": ',
        headers={"content-type": "application/json"},
    )
    assert malformed.status_code == 422
    # Unknown dataset -> 404.
    assert (
        post_view(client, "guest", [{"email": "a@b.c"}], dataset="ghost").status_code
        == 404
    )

    assert client.get(access_path()).json() == []
    assert client.get(audit_path()).json() == []


# --------------------------------------------------------------------------- #
# Read endpoint shape and precedence
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
        assert set(response.json()) == {"error", "detail"}


def test_access_records_reject_body_and_query_params(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
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


def test_access_records_shape_errors_keep_404_precedence(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert client.get(access_path("ghost"), params={"x": "1"}).status_code == 404
    assert (
        client.request("GET", access_path("orders", 9), content=b"{}").status_code
        == 404
    )


def test_access_records_are_immutable(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200

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
    # One record per view, numbered contiguously without gaps or duplicates.
    assert len(records) == count
    assert [record["sequence"] for record in records] == list(range(1, count + 1))
    assert sorted(record["role"] for record in records) == sorted(
        f"role-{index}" for index in range(count)
    )
    assert all(record["row_count"] == 1 for record in records)
    assert all(record["masked_count"] == 1 for record in records)

    # Every masked value still produced its own hit record.
    hits = client.get(audit_path()).json()
    assert [hit["sequence"] for hit in hits] == list(range(1, count + 1))
    assert sum(record["masked_count"] for record in records) == len(hits)


def test_concurrent_views_with_varying_masked_counts(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    create_policy(
        client,
        {"field": "ssn", "classification": "secret", "masking": "redact",
         "allowed_roles": []},
    )

    count = 10
    failures: list[object] = []
    barrier = threading.Barrier(count)

    def view(index: int) -> None:
        local = TestClient(client.app)
        barrier.wait()
        try:
            # Even-indexed views mask two values per row; odd-indexed views
            # send rows with no covered fields (masked_count stays 0) but
            # still leave their one access record.
            if index % 2 == 0:
                rows = [{"email": "a@b.c", "ssn": "1"}, {"email": "d@e.f"}]
            else:
                rows = [{"id": index, "age": 21}]
            response = local.post(
                policies_path() + "/view",
                json={"role": f"role-{index}", "rows": rows},
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
    assert len(records) == count
    assert [record["sequence"] for record in records] == list(range(1, count + 1))
    by_role = {record["role"]: record for record in records}
    for index in range(count):
        record = by_role[f"role-{index}"]
        if index % 2 == 0:
            assert record["row_count"] == 2
            assert record["masked_count"] == 3
        else:
            assert record["row_count"] == 1
            assert record["masked_count"] == 0

    hits = client.get(audit_path()).json()
    assert sum(record["masked_count"] for record in records) == len(hits)
    # Five even-indexed views times three masked values each.
    assert len(hits) == 15


def test_view_succeeds_when_optimistic_trail_retries_are_exhausted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )

    # Simulate a cross-process writer winning every tail race: the optimistic
    # trail append on the request connection always collides, exhausting the
    # bounded retries. The view must still return its masked result and the
    # whole trail (hit records plus the single access record) must be written
    # through the serialized fallback.
    real_trail = repository._append_privacy_view_trail
    calls = 0

    def colliding_trail(conn, version_id, role, row_count, hits):
        nonlocal calls
        calls += 1
        if calls <= repository._PRIVACY_VIEW_AUDIT_MAX_ATTEMPTS:
            raise sqlite3.IntegrityError("simulated cross-process collision")
        return real_trail(conn, version_id, role, row_count, hits)

    monkeypatch.setattr(
        repository, "_append_privacy_view_trail", colliding_trail
    )

    response = post_view(client, "guest", [{"email": "a@b.c"}, {"email": "d@e.f"}])
    assert response.status_code == 200
    assert response.json()["rows"] == [{"email": "***"}, {"email": "***"}]

    records = client.get(access_path()).json()
    assert len(records) == 1
    assert records[0]["sequence"] == 1
    assert records[0]["role"] == "guest"
    assert records[0]["row_count"] == 2
    assert records[0]["masked_count"] == 2

    hits = client.get(audit_path()).json()
    assert [hit["sequence"] for hit in hits] == [1, 2]
    assert len(hits) == records[0]["masked_count"]


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
    json={"fields": [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "email", "type": "string", "nullable": True},
        {"name": "ssn", "type": "string", "nullable": True},
    ]},
).status_code == 201
for field in ("email", "ssn"):
    policy = client.post(
        "/datasets/orders/versions/1/privacy-policies",
        json={
            "field": field,
            "classification": "PII",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    assert policy.status_code == 201, policy.text

# View 1: two rows, three masked values.
viewed = client.post(
    "/datasets/orders/versions/1/privacy-policies/view",
    json={
        "role": "guest",
        "rows": [
            {"id": 1, "email": "a@b.c", "ssn": "1"},
            {"id": 2, "email": "d@e.f"},
        ],
    },
)
assert viewed.status_code == 200, viewed.text
# View 2: empty row set, nothing masked.
empty = client.post(
    "/datasets/orders/versions/1/privacy-policies/view",
    json={"role": "guest", "rows": []},
)
assert empty.status_code == 200, empty.text
print("created")
"""

_VERIFY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

access = client.get(
    "/datasets/orders/versions/1/privacy-policies/view/access-records"
)
assert access.status_code == 200, access.text
records = access.json()
assert len(records) == 2
assert [record["sequence"] for record in records] == [1, 2]
assert records[0]["role"] == "guest"
assert records[0]["row_count"] == 2
assert records[0]["masked_count"] == 3
assert records[1]["role"] == "guest"
assert records[1]["row_count"] == 0
assert records[1]["masked_count"] == 0

hits = client.get(
    "/datasets/orders/versions/1/privacy-policies/view/audit-records"
)
assert hits.status_code == 200, hits.text
assert len(hits.json()) == 3
print(json.dumps(records, sort_keys=True))
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
    _run_script(db_path, _CREATE_AND_VIEW_SCRIPT)

    # New interpreter: the access records written before the restart keep
    # their count and content.
    printed = _run_script(db_path, _VERIFY_SCRIPT)
    records = json.loads(printed)
    assert [record["sequence"] for record in records] == [1, 2]
