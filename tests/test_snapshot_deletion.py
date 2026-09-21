"""Tests for retention policies and lineage-aware snapshot deletion requests."""

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


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_dataset(client: TestClient, name: str, fields: list[str]) -> None:
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


def add_link(
    client: TestClient,
    source: tuple[str, int, str],
    target: tuple[str, int, str],
) -> None:
    source_dataset, source_version, source_field = source
    target_dataset, target_version, target_field = target
    response = client.post(
        f"/datasets/{target_dataset}/versions/{target_version}/lineage",
        json={
            "target_dataset": target_dataset,
            "target_version": target_version,
            "target_field": target_field,
            "source_dataset": source_dataset,
            "source_version": source_version,
            "source_field": source_field,
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
    client: TestClient, dataset: str = "raw", version: int = 1, retention_days: int = 7
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/retention-policies",
        json={"retention_days": retention_days},
    )
    assert response.status_code == 201, response.text
    return response.json()


def requests_path(dataset: str, version: int, snapshot_id: int) -> str:
    return (
        f"/datasets/{dataset}/versions/{version}/snapshots/"
        f"{snapshot_id}/deletion-requests"
    )


def create_request(
    client: TestClient,
    snapshot_id: int,
    reason: str = "no longer needed",
    dataset: str = "raw",
    version: int = 1,
) -> dict:
    response = client.post(
        requests_path(dataset, version, snapshot_id), json={"reason": reason}
    )
    assert response.status_code == 201, response.text
    return response.json()


def backdate_snapshot(snapshot_id: int, days: int) -> None:
    """Age an existing snapshot directly in the database (bypassing the API)."""
    db_path = os.environ["DATA_LINEAGE_DB"]
    old = (
        datetime.now(timezone.utc) - timedelta(days=days, seconds=1)
    ).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE snapshots SET created_at = ? WHERE id = ?",
            (old, snapshot_id),
        )


# --------------------------------------------------------------------------- #
# Retention policies
# --------------------------------------------------------------------------- #


def test_create_retention_policy_returns_record(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    body = make_policy(client, retention_days=30)

    assert set(body) == {"id", "dataset", "version", "retention_days", "created_at"}
    assert isinstance(body["id"], int)
    assert body["dataset"] == "raw"
    assert body["version"] == 1
    assert body["retention_days"] == 30
    datetime.fromisoformat(body["created_at"])


def test_retention_policy_accepts_zero_days(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    assert make_policy(client, retention_days=0)["retention_days"] == 0


def test_retention_policy_is_one_per_version(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    response = client.post(
        "/datasets/raw/versions/1/retention-policies",
        json={"retention_days": 99},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"


def test_retention_policy_rejects_invalid_payloads(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    path = "/datasets/raw/versions/1/retention-policies"
    for payload in (
        {"retention_days": -1},
        {"retention_days": 1.5},
        {"retention_days": True},
        {"retention_days": "7"},
        {},
        {"retention_days": 7, "extra": 1},
    ):
        response = client.post(path, json=payload)
        assert response.status_code == 422, payload
        assert response.json()["error"] == "validation_error"
    # Nothing was written: a valid policy can still be created.
    assert client.post(path, json={"retention_days": 7}).status_code == 201


def test_retention_policy_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    path = "/datasets/ghost/versions/1/retention-policies"
    assert client.post(path, json={"retention_days": 1}).status_code == 404
    make_dataset(client, "raw", ["id"])
    assert client.post(
        "/datasets/raw/versions/9/retention-policies",
        json={"retention_days": 1},
    ).status_code == 404


# --------------------------------------------------------------------------- #
# Deletion requests: creation and lineage-aware status
# --------------------------------------------------------------------------- #


def test_deletion_request_is_pending_without_downstream(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    policy = make_policy(client)
    snapshot = make_snapshot(client)

    body = create_request(client, snapshot["id"])
    assert set(body) == {
        "id",
        "snapshot_id",
        "policy_id",
        "reason",
        "status",
        "impacted",
        "created_at",
    }
    assert body["snapshot_id"] == snapshot["id"]
    assert body["policy_id"] == policy["id"]
    assert body["reason"] == "no longer needed"
    assert body["status"] == "pending"
    assert body["impacted"] == []
    datetime.fromisoformat(body["created_at"])


def test_deletion_request_is_blocked_by_direct_downstream(client: TestClient) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    make_policy(client)
    snapshot = make_snapshot(client)

    body = create_request(client, snapshot["id"])
    assert body["status"] == "blocked"
    assert body["impacted"] == [{"dataset": "dm", "version": 1, "field": "id"}]


def test_deletion_request_includes_indirect_downstream_and_sorts(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    make_dataset(client, "bi", ["report_id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    add_link(client, ("dm", 1, "id"), ("bi", 1, "report_id"))
    make_policy(client)
    snapshot = make_snapshot(client)

    body = create_request(client, snapshot["id"])
    assert body["status"] == "blocked"
    # Direct and indirect downstream, deduplicated and sorted by dataset,
    # version, field ascending.
    assert body["impacted"] == [
        {"dataset": "bi", "version": 1, "field": "report_id"},
        {"dataset": "dm", "version": 1, "field": "id"},
    ]


def test_downstream_is_the_union_of_every_version_field_and_deduplicated(
    client: TestClient,
) -> None:
    # Two fields of the source version both feed one downstream field: it must
    # be listed exactly once.
    make_dataset(client, "raw", ["a", "b"])
    make_dataset(client, "dm", ["x"])
    add_link(client, ("raw", 1, "a"), ("dm", 1, "x"))
    add_link(client, ("raw", 1, "b"), ("dm", 1, "x"))
    make_policy(client)
    snapshot = make_snapshot(client)

    body = create_request(client, snapshot["id"])
    assert body["impacted"] == [{"dataset": "dm", "version": 1, "field": "x"}]


def test_downstream_walk_terminates_on_cycles(client: TestClient) -> None:
    make_dataset(client, "raw", ["a"])
    make_dataset(client, "dm", ["x"])
    make_dataset(client, "bi", ["y"])
    add_link(client, ("raw", 1, "a"), ("dm", 1, "x"))
    add_link(client, ("dm", 1, "x"), ("bi", 1, "y"))
    add_link(client, ("bi", 1, "y"), ("dm", 1, "x"))
    make_policy(client)
    snapshot = make_snapshot(client)

    body = create_request(client, snapshot["id"])
    assert body["impacted"] == [
        {"dataset": "bi", "version": 1, "field": "y"},
        {"dataset": "dm", "version": 1, "field": "x"},
    ]


def test_deletion_request_rejects_invalid_reason(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    path = requests_path("raw", 1, snapshot["id"])

    for payload in (
        {},
        {"reason": ""},
        {"reason": "   "},
        {"reason": 123},
        {"reason": None},
        {"reason": "ok", "extra": True},
    ):
        response = client.post(path, json=payload)
        assert response.status_code == 422, payload
        assert response.json()["error"] == "validation_error"
    # Nothing was written.
    assert client.get(path).json() == []


def test_deletion_request_requires_existing_snapshot_and_policy(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])

    # No policy and an unknown snapshot: policy is reported after the snapshot.
    missing_snapshot = client.post(
        requests_path("raw", 1, 999), json={"reason": "x"}
    )
    assert missing_snapshot.status_code == 404
    assert missing_snapshot.json()["error"] == "not_found"

    snapshot = make_snapshot(client)
    missing_policy = client.post(
        requests_path("raw", 1, snapshot["id"]), json={"reason": "x"}
    )
    assert missing_policy.status_code == 404
    assert missing_policy.json()["error"] == "not_found"
    # Nothing was written.
    assert client.get(requests_path("raw", 1, snapshot["id"])).json() == []


def test_deletion_request_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    path = "/datasets/ghost/versions/1/snapshots/1/deletion-requests"
    assert client.post(path, json={"reason": "x"}).status_code == 404
    make_dataset(client, "raw", ["id"])
    assert client.post(
        "/datasets/raw/versions/9/snapshots/1/deletion-requests",
        json={"reason": "x"},
    ).status_code == 404


def test_repeating_an_open_request_is_409(client: TestClient) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    make_policy(client)
    snapshot = make_snapshot(client)

    first = create_request(client, snapshot["id"])
    assert first["status"] == "blocked"
    repeat = client.post(
        requests_path("raw", 1, snapshot["id"]), json={"reason": "again"}
    )
    assert repeat.status_code == 409
    assert repeat.json()["error"] == "conflict"
    # Still exactly one request.
    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert [item["id"] for item in listed] == [first["id"]]


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


def test_list_deletion_requests_returns_records_by_id(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client, rows=[{"id": 1}])
    request = create_request(client, snapshot["id"], reason="one")

    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert [item["id"] for item in listed] == [request["id"]]
    assert listed[0]["reason"] == "one"
    assert listed[0]["status"] == "pending"
    assert set(listed[0]) == {
        "id",
        "snapshot_id",
        "policy_id",
        "reason",
        "status",
        "impacted",
        "created_at",
    }

    assert client.post(
        f"{requests_path('raw', 1, snapshot['id'])}/{request['id']}/confirm"
    ).status_code == 200

    # After confirmation the collection still lists the record (no body fields
    # beyond the stable seven are added on listing).
    confirmed_list = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert [item["id"] for item in confirmed_list] == [request["id"]]
    assert confirmed_list[0]["status"] == "confirmed"
    assert "confirmed_at" not in confirmed_list[0]


def test_list_deletion_requests_unknown_snapshot_is_404(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    response = client.get(requests_path("raw", 1, 4242))
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_list_deletion_requests_after_snapshot_deleted_still_works(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])

    assert client.post(
        f"{requests_path('raw', 1, snapshot['id'])}/{request['id']}/confirm"
    ).status_code == 200

    # The snapshot is gone but the request collection remains addressable.
    listed = client.get(requests_path("raw", 1, snapshot["id"]))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [request["id"]]


# --------------------------------------------------------------------------- #
# Confirmation
# --------------------------------------------------------------------------- #


def test_confirm_pending_request_deletes_snapshot_atomically(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=7)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    backdate_snapshot(snapshot["id"], days=7)

    response = client.post(
        f"{requests_path('raw', 1, snapshot['id'])}/{request['id']}/confirm"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == request["id"]
    assert body["status"] == "confirmed"
    assert body["impacted"] == []
    assert isinstance(body["confirmed_at"], str)
    datetime.fromisoformat(body["confirmed_at"])

    # The snapshot is gone from every existing snapshot interface.
    base = "/datasets/raw/versions/1/snapshots"
    assert client.get(f"{base}/{snapshot['id']}").status_code == 404
    assert client.get(base).json() == []
    assert client.get(
        f"{base}/{snapshot['id']}/diff/{snapshot['id']}"
    ).status_code == 404
    at = client.get(f"{base}/at", params={"timestamp": "2099-01-01T00:00:00+00:00"})
    assert at.status_code == 404


def test_confirm_too_young_snapshot_is_409_and_keeps_it(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=7)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])

    response = client.post(
        f"{requests_path('raw', 1, snapshot['id'])}/{request['id']}/confirm"
    )
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"

    # Nothing was deleted or modified.
    assert client.get(
        f"/datasets/raw/versions/1/snapshots/{snapshot['id']}"
    ).status_code == 200
    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert listed[0]["status"] == "pending"


def test_confirm_blocked_request_is_409_even_when_old_enough(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    assert request["status"] == "blocked"

    response = client.post(
        f"{requests_path('raw', 1, snapshot['id'])}/{request['id']}/confirm"
    )
    assert response.status_code == 409
    # The snapshot survives.
    assert client.get(
        f"/datasets/raw/versions/1/snapshots/{snapshot['id']}"
    ).status_code == 200


def test_confirm_twice_is_409(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    path = f"{requests_path('raw', 1, snapshot['id'])}/{request['id']}/confirm"
    assert client.post(path).status_code == 200
    second = client.post(path)
    assert second.status_code == 409
    assert second.json()["error"] == "conflict"


def test_confirm_unknown_request_is_404(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)
    # A request id that never existed.
    response = client.post(
        f"{requests_path('raw', 1, snapshot['id'])}/999/confirm"
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_confirm_request_on_another_snapshot_path_is_404(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    first = make_snapshot(client, rows=[{"id": 1}])
    second = make_snapshot(client, rows=[{"id": 2}])
    request = create_request(client, first["id"])

    # Naming the request under a different snapshot is an unknown resource.
    response = client.post(
        f"{requests_path('raw', 1, second['id'])}/{request['id']}/confirm"
    )
    assert response.status_code == 404


def test_confirm_accepts_no_body(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    response = client.post(
        f"{requests_path('raw', 1, snapshot['id'])}/{request['id']}/confirm",
        content=b"",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200, response.text


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


CREATE_STATE_SCRIPT = """
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
    json={"retention_days": 1},
))
snapshot = client.post(
    "/datasets/raw/versions/1/snapshots", json={"rows": [{"id": 1}]}
)
ok(snapshot)
request = client.post(
    f"/datasets/raw/versions/1/snapshots/{snapshot.json()['id']}/deletion-requests",
    json={"reason": "gdpr request"},
)
ok(request)
print(snapshot.json()["id"], request.json()["id"], request.json()["status"])
"""

CHECK_STATE_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
snapshot_id, request_id, status = json.loads(input())
path = f"/datasets/raw/versions/1/snapshots/{snapshot_id}/deletion-requests"
listed = client.get(path)
assert listed.status_code == 200, listed.text
items = listed.json()
assert len(items) == 1
assert items[0]["id"] == request_id
assert items[0]["snapshot_id"] == snapshot_id
assert items[0]["reason"] == "gdpr request"
assert items[0]["status"] == status
assert items[0]["impacted"] == []
print("persisted")
"""

CONFIRM_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
snapshot_id, request_id = json.loads(input())
response = client.post(
    f"/datasets/raw/versions/1/snapshots/{snapshot_id}/deletion-requests/"
    f"{request_id}/confirm"
)
assert response.status_code == 200, response.text
body = response.json()
assert body["status"] == "confirmed"
assert body["confirmed_at"]
read = client.get(f"/datasets/raw/versions/1/snapshots/{snapshot_id}")
assert read.status_code == 404
print("confirmed")
"""


def test_policies_requests_and_deletion_survive_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "deletion-lineage.db"

    # Process 1: create policy, snapshot and a pending request.
    out = _run(db_path, CREATE_STATE_SCRIPT)
    snapshot_id, request_id, status = out.split()
    assert status == "pending"

    # Process 2: the records survive a restart unchanged.
    payload = json.dumps([int(snapshot_id), int(request_id), status])
    assert _run(db_path, CHECK_STATE_SCRIPT, stdin=payload) == "persisted"

    # Age the snapshot past retention without the API.
    old = (
        datetime.now(timezone.utc) - timedelta(days=2)
    ).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE snapshots SET created_at = ? WHERE id = ?",
            (old, int(snapshot_id)),
        )

    # Process 3: confirmation and snapshot deletion commit across a restart.
    confirm_payload = json.dumps([int(snapshot_id), int(request_id)])
    assert _run(db_path, CONFIRM_SCRIPT, stdin=confirm_payload) == "confirmed"
