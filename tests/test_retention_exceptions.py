"""Tests for retention exceptions (compliance holds on snapshot deletion)."""

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


def make_dataset(client: TestClient, name: str = "raw", fields: list[str] = ["id"]) -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions",
        json={
            "fields": [
                {"name": field, "type": "string", "nullable": True}
                for field in fields
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


def create_exception(
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


def requests_path(snapshot_id: int, dataset: str = "raw", version: int = 1) -> str:
    return (
        f"/datasets/{dataset}/versions/{version}/snapshots/"
        f"{snapshot_id}/deletion-requests"
    )


def backdate_exception(exception_id: int, days: int) -> None:
    """Move an exception's expires_at into the past (bypassing the API)."""
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
    body = create_exception(client, scope="version", snapshot_id=None)

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
    assert body["reason"] == "legal hold"
    assert body["expires_at"] == FUTURE
    assert body["status"] == "active"
    assert body["released_at"] is None
    datetime.fromisoformat(body["created_at"])


def test_create_snapshot_scope_exception_returns_record(client: TestClient) -> None:
    make_dataset(client)
    snapshot = make_snapshot(client)
    body = create_exception(client, scope="snapshot", snapshot_id=snapshot["id"])

    assert body["scope"] == "snapshot"
    assert body["snapshot_id"] == snapshot["id"]
    assert body["status"] == "active"
    assert body["released_at"] is None


def test_create_exception_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    payload = {
        "scope": "version",
        "snapshot_id": None,
        "reason": "hold",
        "expires_at": FUTURE,
    }
    assert client.post(exceptions_path("ghost"), json=payload).status_code == 404
    make_dataset(client)
    assert client.post(exceptions_path("raw", 9), json=payload).status_code == 404


def test_create_snapshot_scope_unknown_snapshot_is_404(client: TestClient) -> None:
    make_dataset(client)
    response = client.post(
        exceptions_path(),
        json={
            "scope": "snapshot",
            "snapshot_id": 999,
            "reason": "hold",
            "expires_at": FUTURE,
        },
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    # Nothing was written.
    assert client.get(exceptions_path()).json() == []


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

    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    payloads = [
        # Missing fields.
        {},
        {"scope": "snapshot", "snapshot_id": snapshot["id"], "reason": "hold"},
        {"scope": "snapshot", "reason": "hold", "expires_at": FUTURE},
        {"scope": "snapshot", "snapshot_id": snapshot["id"], "expires_at": FUTURE},
        {"snapshot_id": snapshot["id"], "reason": "hold", "expires_at": FUTURE},
        # Extra field.
        {**valid, "extra": True},
        # Wrong types.
        {**valid, "scope": 1},
        {**valid, "scope": "dataset"},
        {**valid, "snapshot_id": "1"},
        {**valid, "snapshot_id": True},
        {**valid, "snapshot_id": 1.5},
        {**valid, "reason": 123},
        {**valid, "reason": None},
        {**valid, "expires_at": 123},
        {**valid, "expires_at": None},
        # Empty reason.
        {**valid, "reason": ""},
        {**valid, "reason": "   "},
        # Illegal scope/snapshot_id combinations.
        {**valid, "scope": "version", "snapshot_id": snapshot["id"]},
        {**valid, "scope": "snapshot", "snapshot_id": None},
        # Invalid expires_at values.
        {**valid, "expires_at": "not-a-date"},
        {**valid, "expires_at": "2099-01-01"},
        {**valid, "expires_at": "2099-01-01T00:00:00"},
        {**valid, "expires_at": past},
    ]
    for payload in payloads:
        response = client.post(path, json=payload)
        assert response.status_code == 422, payload
        assert response.json()["error"] == "validation_error"

    # Nothing was written: the collection is still empty and a valid exception
    # can still be created.
    assert client.get(path).json() == []
    assert client.post(path, json=valid).status_code == 201


def test_create_exception_malformed_json_is_422(client: TestClient) -> None:
    make_dataset(client)
    response = client.post(
        exceptions_path(),
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


# --------------------------------------------------------------------------- #
# Listing and derived status
# --------------------------------------------------------------------------- #


def test_list_exceptions_returns_records_by_id(client: TestClient) -> None:
    make_dataset(client)
    snapshot = make_snapshot(client)
    first = create_exception(client, scope="version", snapshot_id=None, reason="one")
    second = create_exception(
        client, scope="snapshot", snapshot_id=snapshot["id"], reason="two"
    )

    listed = client.get(exceptions_path()).json()
    assert [item["id"] for item in listed] == [first["id"], second["id"]]
    assert listed[0]["reason"] == "one"
    assert listed[0]["snapshot_id"] is None
    assert listed[1]["reason"] == "two"
    assert listed[1]["snapshot_id"] == snapshot["id"]
    assert all(item["status"] == "active" for item in listed)


def test_list_exceptions_empty_and_unknown_version(client: TestClient) -> None:
    make_dataset(client)
    assert client.get(exceptions_path()).json() == []
    assert client.get(exceptions_path("ghost")).status_code == 404
    assert client.get(exceptions_path("raw", 9)).status_code == 404


def test_expired_exception_reads_as_expired(client: TestClient) -> None:
    make_dataset(client)
    exception = create_exception(client)
    backdate_exception(exception["id"], days=1)

    listed = client.get(exceptions_path()).json()
    assert listed[0]["status"] == "expired"
    assert listed[0]["released_at"] is None


def test_released_exception_stays_released_after_expiry(client: TestClient) -> None:
    make_dataset(client)
    exception = create_exception(client)
    release = client.post(f"{exceptions_path()}/{exception['id']}/release")
    assert release.status_code == 200

    backdate_exception(exception["id"], days=1)
    listed = client.get(exceptions_path()).json()
    assert listed[0]["status"] == "released"
    assert listed[0]["released_at"] is not None


# --------------------------------------------------------------------------- #
# Release
# --------------------------------------------------------------------------- #


def test_release_active_exception_returns_record(client: TestClient) -> None:
    make_dataset(client)
    exception = create_exception(client)

    response = client.post(f"{exceptions_path()}/{exception['id']}/release")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == exception["id"]
    assert body["status"] == "released"
    assert isinstance(body["released_at"], str)
    datetime.fromisoformat(body["released_at"])
    # The release is persisted.
    listed = client.get(exceptions_path()).json()
    assert listed[0]["status"] == "released"


def test_release_twice_is_409_and_writes_nothing(client: TestClient) -> None:
    make_dataset(client)
    exception = create_exception(client)
    path = f"{exceptions_path()}/{exception['id']}/release"
    first = client.post(path)
    assert first.status_code == 200

    second = client.post(path)
    assert second.status_code == 409
    assert second.json()["error"] == "conflict"
    # The stored record is unchanged.
    listed = client.get(exceptions_path()).json()
    assert listed[0]["released_at"] == first.json()["released_at"]


def test_release_expired_exception_is_409(client: TestClient) -> None:
    make_dataset(client)
    exception = create_exception(client)
    backdate_exception(exception["id"], days=1)

    response = client.post(f"{exceptions_path()}/{exception['id']}/release")
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    listed = client.get(exceptions_path()).json()
    assert listed[0]["status"] == "expired"
    assert listed[0]["released_at"] is None


def test_release_unknown_exception_is_404(client: TestClient) -> None:
    make_dataset(client)
    response = client.post(f"{exceptions_path()}/999/release")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_release_exception_of_another_version_is_404(client: TestClient) -> None:
    make_dataset(client)
    exception = create_exception(client)
    # A second version exists, but the exception belongs to version 1.
    response = client.post(
        "/datasets/raw/versions",
        json={"fields": [{"name": "id", "type": "string", "nullable": True}]},
    )
    assert response.status_code == 201
    assert (
        client.post(f"{exceptions_path('raw', 2)}/{exception['id']}/release").status_code
        == 404
    )


# --------------------------------------------------------------------------- #
# Deletion requests are blocked by active exceptions
# --------------------------------------------------------------------------- #


def test_active_version_exception_blocks_deletion_request(client: TestClient) -> None:
    make_dataset(client)
    make_policy(client)
    snapshot = make_snapshot(client)
    create_exception(client, scope="version", snapshot_id=None)

    response = client.post(requests_path(snapshot["id"]), json={"reason": "cleanup"})
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    # Nothing was written.
    assert client.get(requests_path(snapshot["id"])).json() == []


def test_active_snapshot_exception_blocks_deletion_request(client: TestClient) -> None:
    make_dataset(client)
    make_policy(client)
    snapshot = make_snapshot(client)
    create_exception(client, scope="snapshot", snapshot_id=snapshot["id"])

    response = client.post(requests_path(snapshot["id"]), json={"reason": "cleanup"})
    assert response.status_code == 409
    assert client.get(requests_path(snapshot["id"])).json() == []


def test_exception_on_other_snapshot_does_not_block(client: TestClient) -> None:
    make_dataset(client)
    make_policy(client)
    protected = make_snapshot(client, rows=[{"id": 1}])
    other = make_snapshot(client, rows=[{"id": 2}])
    create_exception(client, scope="snapshot", snapshot_id=protected["id"])

    response = client.post(requests_path(other["id"]), json={"reason": "cleanup"})
    assert response.status_code == 201, response.text


def test_expired_exception_does_not_block_deletion_request(client: TestClient) -> None:
    make_dataset(client)
    make_policy(client)
    snapshot = make_snapshot(client)
    exception = create_exception(client, scope="snapshot", snapshot_id=snapshot["id"])
    backdate_exception(exception["id"], days=1)

    response = client.post(requests_path(snapshot["id"]), json={"reason": "cleanup"})
    assert response.status_code == 201, response.text


def test_released_exception_does_not_block_deletion_request(client: TestClient) -> None:
    make_dataset(client)
    make_policy(client)
    snapshot = make_snapshot(client)
    exception = create_exception(client, scope="version", snapshot_id=None)
    assert (
        client.post(f"{exceptions_path()}/{exception['id']}/release").status_code == 200
    )

    response = client.post(requests_path(snapshot["id"]), json={"reason": "cleanup"})
    assert response.status_code == 201, response.text


def test_active_exception_blocks_confirm_and_preserves_snapshot(
    client: TestClient,
) -> None:
    make_dataset(client)
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)
    request = client.post(requests_path(snapshot["id"]), json={"reason": "cleanup"})
    assert request.status_code == 201, request.text
    request_id = request.json()["id"]

    # The exception is created after the request was opened; it still blocks
    # the confirm.
    create_exception(client, scope="version", snapshot_id=None)
    confirm_path = f"{requests_path(snapshot['id'])}/{request_id}/confirm"
    response = client.post(confirm_path)
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"

    # Nothing was deleted or modified.
    assert (
        client.get(f"/datasets/raw/versions/1/snapshots/{snapshot['id']}").status_code
        == 200
    )
    listed = client.get(requests_path(snapshot["id"])).json()
    assert listed[0]["status"] == "pending"


def test_confirm_succeeds_after_exception_is_released(client: TestClient) -> None:
    make_dataset(client)
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)
    request = client.post(requests_path(snapshot["id"]), json={"reason": "cleanup"})
    assert request.status_code == 201, request.text
    request_id = request.json()["id"]

    exception = create_exception(client, scope="snapshot", snapshot_id=snapshot["id"])
    confirm_path = f"{requests_path(snapshot['id'])}/{request_id}/confirm"
    assert client.post(confirm_path).status_code == 409

    assert (
        client.post(f"{exceptions_path()}/{exception['id']}/release").status_code == 200
    )
    response = client.post(confirm_path)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    assert (
        client.get(f"/datasets/raw/versions/1/snapshots/{snapshot['id']}").status_code
        == 404
    )


def test_exception_does_not_change_lineage_blocking(client: TestClient) -> None:
    # An exception adds a hold on top of the lineage rules; it does not lift
    # the downstream-lineage block.
    assert client.post("/datasets", json={"name": "dm"}).status_code == 201
    make_dataset(client, "raw", ["order_id"])
    response = client.post(
        "/datasets/dm/versions",
        json={"fields": [{"name": "id", "type": "string", "nullable": True}]},
    )
    assert response.status_code == 201
    link = client.post(
        "/datasets/dm/versions/1/lineage",
        json={
            "target_dataset": "dm",
            "target_version": 1,
            "target_field": "id",
            "source_dataset": "raw",
            "source_version": 1,
            "source_field": "order_id",
        },
    )
    assert link.status_code == 201, link.text
    make_policy(client)
    snapshot = make_snapshot(client)
    exception = create_exception(client, scope="snapshot", snapshot_id=snapshot["id"])
    assert (
        client.post(f"{exceptions_path()}/{exception['id']}/release").status_code == 200
    )

    # With the exception released, lineage still blocks the request as before.
    body = client.post(requests_path(snapshot["id"]), json={"reason": "cleanup"})
    assert body.status_code == 201, body.text
    assert body.json()["status"] == "blocked"
    assert body.json()["impacted"] == [{"dataset": "dm", "version": 1, "field": "id"}]


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
print(exception.json()["id"], snapshot.json()["id"])
"""

CHECK_EXCEPTION_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
exception_id, snapshot_id = json.loads(input())
listed = client.get("/datasets/raw/versions/1/retention-exceptions")
assert listed.status_code == 200, listed.text
items = listed.json()
assert len(items) == 1
item = items[0]
assert item["id"] == exception_id
assert item["dataset"] == "raw"
assert item["version"] == 1
assert item["scope"] == "snapshot"
assert item["snapshot_id"] == snapshot_id
assert item["reason"] == "legal hold"
assert item["expires_at"] == "2099-01-01T00:00:00+00:00"
assert item["status"] == "active"
assert item["released_at"] is None
print("persisted")
"""

RELEASE_EXCEPTION_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
exception_id = json.loads(input())
response = client.post(
    f"/datasets/raw/versions/1/retention-exceptions/{exception_id}/release"
)
assert response.status_code == 200, response.text
assert response.json()["status"] == "released"
assert response.json()["released_at"]
listed = client.get("/datasets/raw/versions/1/retention-exceptions").json()
assert listed[0]["status"] == "released"
print("released")
"""


def test_exceptions_survive_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "exceptions-lineage.db"

    # Process 1: create the exception.
    out = _run(db_path, CREATE_EXCEPTION_SCRIPT)
    exception_id, snapshot_id = out.split()

    # Process 2: the record survives a restart unchanged.
    payload = json.dumps([int(exception_id), int(snapshot_id)])
    assert _run(db_path, CHECK_EXCEPTION_SCRIPT, stdin=payload) == "persisted"

    # Process 3: the release commits across a restart.
    assert (
        _run(db_path, RELEASE_EXCEPTION_SCRIPT, stdin=json.dumps(int(exception_id)))
        == "released"
    )
