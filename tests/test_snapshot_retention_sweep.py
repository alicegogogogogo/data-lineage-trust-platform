"""Tests for the batch retention sweep of a version's snapshots."""

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


def sweep_path(dataset: str = "raw", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/snapshots/retention-sweep"


def requests_path(dataset: str, version: int, snapshot_id: int) -> str:
    return (
        f"/datasets/{dataset}/versions/{version}/snapshots/"
        f"{snapshot_id}/deletion-requests"
    )


def sweep(
    client: TestClient,
    reason: str = "retention reached",
    dataset: str = "raw",
    version: int = 1,
) -> dict:
    response = client.post(sweep_path(dataset, version), json={"reason": reason})
    assert response.status_code == 200, response.text
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
# Sweep behaviour
# --------------------------------------------------------------------------- #


def test_sweep_creates_requests_for_expired_snapshots_only(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=7)
    expired = make_snapshot(client, rows=[{"id": 1}])
    young = make_snapshot(client, rows=[{"id": 2}])
    backdate_snapshot(expired["id"], days=10)

    body = sweep(client, reason="  quarterly cleanup  ")

    assert body["dataset"] == "raw"
    assert body["version"] == 1
    # The submitted reason is stored and echoed trimmed.
    assert body["created"] == [
        {
            "snapshot_id": expired["id"],
            "request_id": body["created"][0]["request_id"],
            "status": "pending",
            "reason": "quarterly cleanup",
        }
    ]
    assert body["skipped"] == []
    assert body["created_count"] == 1
    assert body["skipped_count"] == 0
    assert body["not_expired_count"] == 1

    # The new request joined the existing single-snapshot collection.
    listed = client.get(requests_path("raw", 1, expired["id"])).json()
    assert [item["id"] for item in listed] == [body["created"][0]["request_id"]]
    assert listed[0]["reason"] == "quarterly cleanup"
    assert listed[0]["status"] == "pending"
    # The young snapshot has no requests.
    assert client.get(requests_path("raw", 1, young["id"])).json() == []


def test_sweep_response_is_a_deterministic_json_document(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    response = client.post(sweep_path(), json={"reason": "cleanup"})
    assert response.status_code == 200, response.text
    # Fixed key order, compact whitespace and exactly one trailing newline.
    assert response.text == (
        '{"dataset":"raw","version":1,'
        '"created":[{"snapshot_id":%d,"request_id":1,'
        '"status":"pending","reason":"cleanup"}],'
        '"skipped":[],'
        '"created_count":1,"skipped_count":0,"not_expired_count":0}\n'
        % snapshot["id"]
    )


def test_sweep_marks_requests_blocked_when_downstream_exists(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    body = sweep(client)
    assert body["created"][0]["status"] == "blocked"
    assert body["created"][0]["snapshot_id"] == snapshot["id"]

    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert listed[0]["status"] == "blocked"
    assert listed[0]["impacted"] == [{"dataset": "dm", "version": 1, "field": "id"}]


def test_sweep_scans_snapshots_in_snapshot_id_order(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshots = [make_snapshot(client, rows=[{"id": n}]) for n in range(3)]

    body = sweep(client)
    assert [entry["snapshot_id"] for entry in body["created"]] == [
        snapshot["id"] for snapshot in snapshots
    ]
    assert body["created_count"] == 3


def test_sweep_skips_snapshots_with_an_open_request(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    held = make_snapshot(client, rows=[{"id": 1}])
    free = make_snapshot(client, rows=[{"id": 2}])

    # An existing open request on the first snapshot (its own reason kept).
    existing = client.post(
        requests_path("raw", 1, held["id"]), json={"reason": "earlier request"}
    )
    assert existing.status_code == 201, existing.text
    existing = existing.json()

    body = sweep(client, reason="sweep reason")
    assert body["created"] == [
        {
            "snapshot_id": free["id"],
            "request_id": body["created"][0]["request_id"],
            "status": "pending",
            "reason": "sweep reason",
        }
    ]
    assert body["skipped"] == [
        {
            "snapshot_id": held["id"],
            "request_id": existing["id"],
            "status": "pending",
            "reason": "earlier request",
        }
    ]
    assert body["created_count"] == 1
    assert body["skipped_count"] == 1
    assert body["not_expired_count"] == 0

    # The pre-existing request was neither duplicated nor modified.
    listed = client.get(requests_path("raw", 1, held["id"])).json()
    assert [item["id"] for item in listed] == [existing["id"]]
    assert listed[0]["reason"] == "earlier request"


def test_sweep_skips_blocked_open_requests_too(client: TestClient) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)
    existing = client.post(
        requests_path("raw", 1, snapshot["id"]), json={"reason": "first"}
    ).json()
    assert existing["status"] == "blocked"

    body = sweep(client)
    assert body["created"] == []
    assert body["skipped"] == [
        {
            "snapshot_id": snapshot["id"],
            "request_id": existing["id"],
            "status": "blocked",
            "reason": "first",
        }
    ]
    assert body["skipped_count"] == 1


def test_sweep_on_version_without_snapshots_returns_zero_counts(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)

    body = sweep(client)
    assert body == {
        "dataset": "raw",
        "version": 1,
        "created": [],
        "skipped": [],
        "created_count": 0,
        "skipped_count": 0,
        "not_expired_count": 0,
    }


def test_sweep_second_run_skips_everything_it_created(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    first = sweep(client, reason="first sweep")
    assert first["created_count"] == 1

    second = sweep(client, reason="second sweep")
    assert second["created"] == []
    assert second["skipped"] == [
        {
            "snapshot_id": snapshot["id"],
            "request_id": first["created"][0]["request_id"],
            "status": "pending",
            "reason": "first sweep",
        }
    ]
    assert second["created_count"] == 0
    assert second["skipped_count"] == 1


def test_swept_requests_flow_through_the_existing_confirm(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    body = sweep(client)
    request_id = body["created"][0]["request_id"]

    confirm = client.post(
        f"{requests_path('raw', 1, snapshot['id'])}/{request_id}/confirm"
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["status"] == "confirmed"
    # The snapshot is atomically gone.
    assert client.get(
        f"/datasets/raw/versions/1/snapshots/{snapshot['id']}"
    ).status_code == 404


# --------------------------------------------------------------------------- #
# Error mapping and precedence
# --------------------------------------------------------------------------- #


def test_sweep_without_retention_policy_is_404(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_snapshot(client)

    response = client.post(sweep_path(), json={"reason": "x"})
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_sweep_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    assert client.post(
        sweep_path("ghost", 1), json={"reason": "x"}
    ).status_code == 404
    make_dataset(client, "raw", ["id"])
    assert client.post(
        sweep_path("raw", 9), json={"reason": "x"}
    ).status_code == 404


def test_sweep_unknown_resource_check_precedes_body_checks(
    client: TestClient,
) -> None:
    # Even a malformed body stays a 404 when the dataset does not exist.
    response = client.post(
        sweep_path("ghost", 1),
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404
    make_dataset(client, "raw", ["id"])
    response = client.post(
        sweep_path("raw", 9),
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404


def test_sweep_rejects_invalid_reason_and_shape(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    for payload in (
        {},
        {"reason": ""},
        {"reason": "   "},
        {"reason": 123},
        {"reason": None},
        {"reason": ["cleanup"]},
        {"reason": "ok", "extra": True},
    ):
        response = client.post(sweep_path(), json=payload)
        assert response.status_code == 422, payload
        assert response.json()["error"] == "validation_error"

    # Nothing was written: a valid sweep still creates the request.
    body = sweep(client)
    assert [entry["snapshot_id"] for entry in body["created"]] == [snapshot["id"]]


def test_sweep_rejects_empty_whitespace_and_malformed_bodies(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    for raw in (b"", b"   \n\t ", b"not json", b"[1, 2]", b'"just a string"'):
        response = client.post(
            sweep_path(),
            content=raw,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422, raw
        assert response.json()["error"] == "validation_error"

    # Nothing was written: a valid sweep still creates the request.
    body = sweep(client)
    assert [entry["snapshot_id"] for entry in body["created"]] == [snapshot["id"]]


def test_sweep_rejects_any_query_parameter(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    make_snapshot(client)

    response = client.post(sweep_path() + "?dry_run=true", json={"reason": "x"})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"

    # Nothing was written.
    body = sweep(client)
    assert body["created_count"] == 1


def test_sweep_only_accepts_post(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    # PUT/DELETE/PATCH have no matching route and are rejected outright; GET
    # falls through to the existing snapshot-by-id route, where the
    # non-integer "retention-sweep" segment is a 422 (pre-existing routing
    # behaviour for any non-numeric snapshot segment).
    assert client.put(sweep_path(), json={"reason": "x"}).status_code == 405
    assert client.delete(sweep_path()).status_code == 405
    assert client.patch(sweep_path(), json={"reason": "x"}).status_code == 405
    assert client.get(sweep_path()).status_code == 422


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


SWEEP_STATE_SCRIPT = """
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
sweep = client.post(
    "/datasets/raw/versions/1/snapshots/retention-sweep",
    json={"reason": "bulk cleanup"},
)
ok(sweep, code=(200,))
body = sweep.json()
assert body["created_count"] == 1
entry = body["created"][0]
assert entry["snapshot_id"] == snapshot.json()["id"]
print(entry["snapshot_id"], entry["request_id"], entry["status"])
"""

CHECK_SWEEP_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
snapshot_id, request_id, status = json.loads(input())
listed = client.get(
    f"/datasets/raw/versions/1/snapshots/{snapshot_id}/deletion-requests"
)
assert listed.status_code == 200, listed.text
items = listed.json()
assert len(items) == 1
assert items[0]["id"] == request_id
assert items[0]["snapshot_id"] == snapshot_id
assert items[0]["reason"] == "bulk cleanup"
assert items[0]["status"] == status
# A repeated sweep sees the persisted open request and skips the snapshot.
sweep = client.post(
    "/datasets/raw/versions/1/snapshots/retention-sweep",
    json={"reason": "again"},
)
assert sweep.status_code == 200, sweep.text
body = sweep.json()
assert body["created"] == []
assert body["skipped"] == [
    {
        "snapshot_id": snapshot_id,
        "request_id": request_id,
        "status": status,
        "reason": "bulk cleanup",
    }
]
print("persisted")
"""


def test_sweep_results_survive_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "sweep-lineage.db"

    # Process 1: sweep creates the deletion request.
    out = _run(db_path, SWEEP_STATE_SCRIPT)
    snapshot_id, request_id, status = out.split()
    assert status == "pending"

    # Process 2: the created request is listed and deduplicated identically.
    payload = json.dumps([int(snapshot_id), int(request_id), status])
    assert _run(db_path, CHECK_SWEEP_SCRIPT, stdin=payload) == "persisted"
