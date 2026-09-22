"""Tests for persistent retention exceptions blocking snapshot deletion."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

FUTURE = "2099-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_dataset(client: TestClient, name: str = "raw", fields: list[str] | None = None) -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions",
        json={
            "fields": [
                {"name": field, "type": "string", "nullable": True}
                for field in (fields or ["id"])
            ]
        },
    )
    assert response.status_code == 201, response.text


def make_snapshot(
    client: TestClient, dataset: str = "raw", version: int = 1, rows: list | None = None
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots",
        json={"rows": rows if rows is not None else [{"id": 1}]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_policy(
    client: TestClient, dataset: str = "raw", version: int = 1, retention_days: int = 0
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/retention-policies",
        json={"retention_days": retention_days},
    )
    assert response.status_code == 201, response.text
    return response.json()


def exceptions_path(dataset: str = "raw", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/retention-exceptions"


def deletion_requests_path(dataset: str, version: int, snapshot_id: int) -> str:
    return (
        f"/datasets/{dataset}/versions/{version}/snapshots/"
        f"{snapshot_id}/deletion-requests"
    )


def make_exception(
    client: TestClient,
    scope: str = "version",
    snapshot_id: int | None = None,
    reason: str = "legal hold",
    expires_at: str = FUTURE,
    dataset: str = "raw",
    version: int = 1,
) -> dict:
    response = client.post(
        exceptions_path(dataset, version),
        json={
            "scope": scope,
            "snapshot_id": snapshot_id,
            "reason": reason,
            "expires_at": expires_at,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def backdate_exception(exception_id: int, days: int) -> None:
    """Move an exception's expiry into the past directly in the database."""
    db_path = os.environ["DATA_LINEAGE_DB"]
    past = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE retention_exceptions SET expires_at = ? WHERE id = ?",
            (past, exception_id),
        )


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #


def test_create_version_scope_exception_returns_record(client: TestClient) -> None:
    make_dataset(client)

    body = make_exception(client, reason="  compliance hold  ")
    assert set(body) == {
        "id",
        "dataset",
        "version",
        "scope",
        "snapshot_id",
        "reason",
        "expires_at",
        "status",
        "created_at",
        "released_at",
    }
    assert isinstance(body["id"], int)
    assert body["dataset"] == "raw"
    assert body["version"] == 1
    assert body["scope"] == "version"
    assert body["snapshot_id"] is None
    assert body["reason"] == "compliance hold"
    assert body["expires_at"] == FUTURE
    assert body["status"] == "active"
    assert body["released_at"] is None
    datetime.fromisoformat(body["created_at"])


def test_create_snapshot_scope_exception_returns_record(client: TestClient) -> None:
    make_dataset(client)
    snapshot = make_snapshot(client)

    body = make_exception(client, scope="snapshot", snapshot_id=snapshot["id"])
    assert body["scope"] == "snapshot"
    assert body["snapshot_id"] == snapshot["id"]
    assert body["status"] == "active"
    assert body["released_at"] is None


def test_create_exception_accepts_non_utc_offset(client: TestClient) -> None:
    make_dataset(client)
    body = make_exception(client, expires_at="2099-06-01T08:30:00+08:00")
    assert body["expires_at"] == "2099-06-01T08:30:00+08:00"


def test_create_exception_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    payload = {
        "scope": "version",
        "snapshot_id": None,
        "reason": "hold",
        "expires_at": FUTURE,
    }
    assert client.post(
        "/datasets/ghost/versions/1/retention-exceptions", json=payload
    ).status_code == 404
    make_dataset(client)
    assert client.post(
        "/datasets/raw/versions/9/retention-exceptions", json=payload
    ).status_code == 404


def test_create_exception_unknown_snapshot_is_404(client: TestClient) -> None:
    make_dataset(client)
    payload = {
        "scope": "snapshot",
        "snapshot_id": 4242,
        "reason": "hold",
        "expires_at": FUTURE,
    }
    response = client.post(exceptions_path(), json=payload)
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    # Nothing was written.
    assert client.get(exceptions_path()).json() == []


def test_create_exception_snapshot_from_other_version_is_404(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    snapshot = make_snapshot(client, dataset="raw", version=1)
    # A second version; the snapshot belongs to version 1 only.
    assert client.post(
        "/datasets/raw/versions",
        json={"fields": [{"name": "id", "type": "string", "nullable": True}]},
    ).status_code == 201

    payload = {
        "scope": "snapshot",
        "snapshot_id": snapshot["id"],
        "reason": "hold",
        "expires_at": FUTURE,
    }
    response = client.post(exceptions_path("raw", 2), json=payload)
    assert response.status_code == 404


def test_create_exception_rejects_invalid_payloads(client: TestClient) -> None:
    make_dataset(client)
    snapshot = make_snapshot(client)
    path = exceptions_path()

    valid = {
        "scope": "snapshot",
        "snapshot_id": snapshot["id"],
        "reason": "hold",
        "expires_at": FUTURE,
    }
    bad_payloads = [
        # Missing fields.
        {},
        {"scope": "version", "snapshot_id": None, "reason": "hold"},
        {"scope": "version", "snapshot_id": None, "expires_at": FUTURE},
        {"scope": "version", "reason": "hold", "expires_at": FUTURE},
        {"snapshot_id": None, "reason": "hold", "expires_at": FUTURE},
        # Extra fields.
        {**valid, "extra": True},
        # Wrong types.
        {**valid, "scope": 1},
        {**valid, "scope": "everything"},
        {**valid, "snapshot_id": "1"},
        {**valid, "snapshot_id": 1.5},
        {**valid, "snapshot_id": True},
        {**valid, "reason": 123},
        {**valid, "reason": None},
        {**valid, "expires_at": 123},
        {**valid, "expires_at": None},
        # Illegal scope/snapshot_id combinations.
        {"scope": "version", "snapshot_id": snapshot["id"],
         "reason": "hold", "expires_at": FUTURE},
        {"scope": "snapshot", "snapshot_id": None,
         "reason": "hold", "expires_at": FUTURE},
        # Empty reason.
        {**valid, "reason": ""},
        {**valid, "reason": "   "},
        # Invalid expires_at values.
        {**valid, "expires_at": "not-a-date"},
        {**valid, "expires_at": "2099-01-01T00:00:00"},  # no timezone
        {**valid, "expires_at": "2000-01-01T00:00:00+00:00"},  # past
    ]
    for payload in bad_payloads:
        response = client.post(path, json=payload)
        assert response.status_code == 422, payload
        assert response.json()["error"] == "validation_error"
    # Nothing was written: no records, and a valid create still succeeds.
    assert client.get(path).json() == []
    assert client.post(path, json=valid).status_code == 201


def test_create_exception_missing_body_is_422(client: TestClient) -> None:
    make_dataset(client)
    response = client.post(
        exceptions_path(),
        content=b"",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


# --------------------------------------------------------------------------- #
# Listing and status computation
# --------------------------------------------------------------------------- #


def test_list_exceptions_returns_records_in_order(client: TestClient) -> None:
    make_dataset(client)
    snapshot = make_snapshot(client)
    first = make_exception(client, reason="one")
    second = make_exception(client, scope="snapshot", snapshot_id=snapshot["id"])

    listed = client.get(exceptions_path()).json()
    assert [item["id"] for item in listed] == [first["id"], second["id"]]
    assert listed[0]["scope"] == "version"
    assert listed[0]["snapshot_id"] is None
    assert listed[1]["scope"] == "snapshot"
    assert listed[1]["snapshot_id"] == snapshot["id"]
    assert all(item["status"] == "active" for item in listed)
    assert all(item["released_at"] is None for item in listed)


def test_list_exceptions_empty_and_unknown_parent(client: TestClient) -> None:
    make_dataset(client)
    assert client.get(exceptions_path()).json() == []
    assert client.get(exceptions_path("ghost", 1)).status_code == 404
    assert client.get(exceptions_path("raw", 9)).status_code == 404


def test_expired_exception_reports_expired_status(client: TestClient) -> None:
    make_dataset(client)
    exception = make_exception(client)

    backdate_exception(exception["id"], days=1)
    listed = client.get(exceptions_path()).json()
    assert listed[0]["id"] == exception["id"]
    assert listed[0]["status"] == "expired"
    assert listed[0]["released_at"] is None


# --------------------------------------------------------------------------- #
# Release
# --------------------------------------------------------------------------- #


def test_release_active_exception(client: TestClient) -> None:
    make_dataset(client)
    exception = make_exception(client)

    response = client.post(f"{exceptions_path()}/{exception['id']}/release")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == exception["id"]
    assert body["status"] == "released"
    assert isinstance(body["released_at"], str)
    datetime.fromisoformat(body["released_at"])

    # The released status persists on listing.
    listed = client.get(exceptions_path()).json()
    assert listed[0]["status"] == "released"
    assert listed[0]["released_at"] == body["released_at"]


def test_release_twice_is_409_and_does_not_rewrite(client: TestClient) -> None:
    make_dataset(client)
    exception = make_exception(client)
    path = f"{exceptions_path()}/{exception['id']}/release"

    first = client.post(path)
    assert first.status_code == 200
    second = client.post(path)
    assert second.status_code == 409
    assert second.json()["error"] == "conflict"
    # The original release timestamp is unchanged.
    listed = client.get(exceptions_path()).json()
    assert listed[0]["released_at"] == first.json()["released_at"]


def test_release_expired_exception_is_409(client: TestClient) -> None:
    make_dataset(client)
    exception = make_exception(client)
    backdate_exception(exception["id"], days=1)

    response = client.post(f"{exceptions_path()}/{exception['id']}/release")
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    # Still expired, not released.
    listed = client.get(exceptions_path()).json()
    assert listed[0]["status"] == "expired"
    assert listed[0]["released_at"] is None


def test_release_unknown_exception_is_404(client: TestClient) -> None:
    make_dataset(client)
    response = client.post(f"{exceptions_path()}/999/release")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_release_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    assert client.post(
        "/datasets/ghost/versions/1/retention-exceptions/1/release"
    ).status_code == 404
    make_dataset(client)
    assert client.post(
        "/datasets/raw/versions/9/retention-exceptions/1/release"
    ).status_code == 404


def test_release_accepts_no_body(client: TestClient) -> None:
    make_dataset(client)
    exception = make_exception(client)
    response = client.post(
        f"{exceptions_path()}/{exception['id']}/release",
        content=b"",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# Deletion blocking
# --------------------------------------------------------------------------- #


def test_active_version_scope_exception_blocks_deletion_request(
    client: TestClient,
) -> None:
    make_dataset(client)
    make_policy(client)
    snapshot = make_snapshot(client)
    make_exception(client)

    response = client.post(
        deletion_requests_path("raw", 1, snapshot["id"]), json={"reason": "x"}
    )
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    # Nothing was written.
    assert client.get(deletion_requests_path("raw", 1, snapshot["id"])).json() == []


def test_active_snapshot_scope_exception_blocks_deletion_request(
    client: TestClient,
) -> None:
    make_dataset(client)
    make_policy(client)
    snapshot = make_snapshot(client)
    make_exception(client, scope="snapshot", snapshot_id=snapshot["id"])

    response = client.post(
        deletion_requests_path("raw", 1, snapshot["id"]), json={"reason": "x"}
    )
    assert response.status_code == 409
    assert client.get(deletion_requests_path("raw", 1, snapshot["id"])).json() == []


def test_snapshot_scope_exception_does_not_block_other_snapshots(
    client: TestClient,
) -> None:
    make_dataset(client)
    make_policy(client)
    protected = make_snapshot(client, rows=[{"id": 1}])
    other = make_snapshot(client, rows=[{"id": 2}])
    make_exception(client, scope="snapshot", snapshot_id=protected["id"])

    response = client.post(
        deletion_requests_path("raw", 1, other["id"]), json={"reason": "x"}
    )
    assert response.status_code == 201, response.text


def test_active_exception_blocks_confirmation(client: TestClient) -> None:
    make_dataset(client)
    make_policy(client)
    snapshot = make_snapshot(client)
    # The deletion request is opened before the exception exists.
    request = client.post(
        deletion_requests_path("raw", 1, snapshot["id"]), json={"reason": "x"}
    )
    assert request.status_code == 201, request.text
    request_id = request.json()["id"]

    make_exception(client)
    confirm = client.post(
        f"{deletion_requests_path('raw', 1, snapshot['id'])}/{request_id}/confirm"
    )
    assert confirm.status_code == 409
    assert confirm.json()["error"] == "conflict"

    # Nothing was deleted or modified.
    assert client.get(
        f"/datasets/raw/versions/1/snapshots/{snapshot['id']}"
    ).status_code == 200
    listed = client.get(deletion_requests_path("raw", 1, snapshot["id"])).json()
    assert listed[0]["status"] == "pending"


def test_expired_and_released_exceptions_do_not_block(client: TestClient) -> None:
    make_dataset(client)
    make_policy(client)
    snapshot = make_snapshot(client, rows=[{"id": 1}])
    other = make_snapshot(client, rows=[{"id": 2}])

    expired = make_exception(client, reason="expired hold")
    released = make_exception(client, reason="released hold")
    backdate_exception(expired["id"], days=1)
    assert client.post(
        f"{exceptions_path()}/{released['id']}/release"
    ).status_code == 200

    # Creation is not blocked by expired/released exceptions.
    created = client.post(
        deletion_requests_path("raw", 1, snapshot["id"]), json={"reason": "x"}
    )
    assert created.status_code == 201, created.text

    # Confirmation is not blocked either.
    confirm = client.post(
        f"{deletion_requests_path('raw', 1, snapshot['id'])}/"
        f"{created.json()['id']}/confirm"
    )
    assert confirm.status_code == 200, confirm.text
    assert client.get(
        f"/datasets/raw/versions/1/snapshots/{snapshot['id']}"
    ).status_code == 404
    # The untouched snapshot is still there.
    assert client.get(
        f"/datasets/raw/versions/1/snapshots/{other['id']}"
    ).status_code == 200


def test_releasing_the_exception_unblocks_deletion(client: TestClient) -> None:
    make_dataset(client)
    make_policy(client)
    snapshot = make_snapshot(client)
    exception = make_exception(client)

    blocked = client.post(
        deletion_requests_path("raw", 1, snapshot["id"]), json={"reason": "x"}
    )
    assert blocked.status_code == 409

    assert client.post(
        f"{exceptions_path()}/{exception['id']}/release"
    ).status_code == 200
    created = client.post(
        deletion_requests_path("raw", 1, snapshot["id"]), json={"reason": "x"}
    )
    assert created.status_code == 201, created.text


def test_exception_does_not_change_lineage_or_retention_behavior(
    client: TestClient,
) -> None:
    # An expired exception leaves the lineage-aware status and the retention
    # age check exactly as they were without any exception.
    for name in ("raw", "dm"):
        assert client.post("/datasets", json={"name": name}).status_code == 201
    for name, field in (("raw", "order_id"), ("dm", "id")):
        assert client.post(
            f"/datasets/{name}/versions",
            json={"fields": [{"name": field, "type": "string", "nullable": True}]},
        ).status_code == 201
    assert client.post(
        "/datasets/dm/versions/1/lineage",
        json={
            "target_dataset": "dm",
            "target_version": 1,
            "target_field": "id",
            "source_dataset": "raw",
            "source_version": 1,
            "source_field": "order_id",
        },
    ).status_code == 201
    make_policy(client, retention_days=7)
    snapshot = make_snapshot(client)

    exception = make_exception(client)
    backdate_exception(exception["id"], days=1)

    # Lineage impact is still computed (blocked status), retention still applies.
    created = client.post(
        deletion_requests_path("raw", 1, snapshot["id"]), json={"reason": "x"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "blocked"
    assert created.json()["impacted"] == [
        {"dataset": "dm", "version": 1, "field": "id"}
    ]


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


def _run(db_path: Path, script: str, stdin: str = "") -> str:
    env = os.environ.copy()
    env["DATA_LINEAGE_DB"] = str(db_path)
    result = subprocess.run(
        [sys.executable, "-c", script],
        input=stdin,
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


CREATE_EXCEPTION_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response, code=(200, 201)):
    assert response.status_code in code, response.text

ok(client.post("/datasets", json={"name": "raw"}))
ok(client.post(
    "/datasets/raw/versions",
    json={"fields": [{"name": "id", "type": "string", "nullable": True}]},
))
ok(client.post(
    "/datasets/raw/versions/1/retention-policies",
    json={"retention_days": 0},
))
snapshot = client.post(
    "/datasets/raw/versions/1/snapshots", json={"rows": [{"id": 1}]}
)
ok(snapshot)
exception = client.post(
    "/datasets/raw/versions/1/retention-exceptions",
    json={
        "scope": "snapshot",
        "snapshot_id": snapshot.json()["id"],
        "reason": "legal hold",
        "expires_at": "2099-01-01T00:00:00+00:00",
    },
)
ok(exception)
blocked = client.post(
    f"/datasets/raw/versions/1/snapshots/{snapshot.json()['id']}/deletion-requests",
    json={"reason": "x"},
)
assert blocked.status_code == 409, blocked.text
print(snapshot.json()["id"], exception.json()["id"])
"""

CHECK_EXCEPTION_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
snapshot_id, exception_id = json.loads(input())
listed = client.get("/datasets/raw/versions/1/retention-exceptions")
assert listed.status_code == 200, listed.text
items = listed.json()
assert len(items) == 1
assert items[0]["id"] == exception_id
assert items[0]["scope"] == "snapshot"
assert items[0]["snapshot_id"] == snapshot_id
assert items[0]["reason"] == "legal hold"
assert items[0]["status"] == "active"
assert items[0]["released_at"] is None

# The exception still blocks deletion after the restart.
blocked = client.post(
    f"/datasets/raw/versions/1/snapshots/{snapshot_id}/deletion-requests",
    json={"reason": "x"},
)
assert blocked.status_code == 409, blocked.text

# Release survives as well.
released = client.post(
    f"/datasets/raw/versions/1/retention-exceptions/{exception_id}/release"
)
assert released.status_code == 200, released.text
assert released.json()["status"] == "released"
print("persisted")
"""

VERIFY_RELEASE_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
snapshot_id, exception_id = json.loads(input())
listed = client.get("/datasets/raw/versions/1/retention-exceptions").json()
assert listed[0]["status"] == "released"
assert listed[0]["released_at"] is not None
# With the exception released, deletion proceeds.
created = client.post(
    f"/datasets/raw/versions/1/snapshots/{snapshot_id}/deletion-requests",
    json={"reason": "x"},
)
assert created.status_code == 201, created.text
print("released-persisted")
"""


def test_retention_exceptions_survive_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "exceptions-lineage.db"

    # Process 1: create an exception and observe it blocking deletion.
    out = _run(db_path, CREATE_EXCEPTION_SCRIPT)
    snapshot_id, exception_id = out.split()

    # Process 2: the record and its blocking effect survive a restart; release
    # it in the same process.
    payload = json.dumps([int(snapshot_id), int(exception_id)])
    assert _run(db_path, CHECK_EXCEPTION_SCRIPT, stdin=payload) == "persisted"

    # Process 3: the released state persisted and deletion is unblocked.
    assert (
        _run(db_path, VERIFY_RELEASE_SCRIPT, stdin=payload) == "released-persisted"
    )
