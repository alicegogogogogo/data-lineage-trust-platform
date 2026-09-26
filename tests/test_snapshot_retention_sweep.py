"""Tests for the batch retention sweep of a version's snapshots."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient


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


def backdate_snapshot(snapshot_id: int, days: int) -> None:
    """Age an existing snapshot directly in the database (bypassing the API)."""
    db_path = os.environ["DATA_LINEAGE_DB"]
    old = (datetime.now(timezone.utc) - timedelta(days=days, seconds=1)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE snapshots SET created_at = ? WHERE id = ?",
            (old, snapshot_id),
        )


def sweep(client: TestClient, reason: str = "retention reached") -> dict:
    response = client.post(sweep_path(), json={"reason": reason})
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Sweep behaviour
# --------------------------------------------------------------------------- #


def test_sweep_creates_requests_for_every_expired_snapshot(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=7)
    first = make_snapshot(client, rows=[{"id": 1}])
    second = make_snapshot(client, rows=[{"id": 2}])
    backdate_snapshot(first["id"], days=8)
    backdate_snapshot(second["id"], days=9)

    body = sweep(client)

    assert set(body) == {"dataset", "version", "created", "skipped", "counts"}
    assert body["dataset"] == "raw"
    assert body["version"] == 1
    assert [entry["snapshot_id"] for entry in body["created"]] == [
        first["id"],
        second["id"],
    ]
    for entry in body["created"]:
        assert list(entry) == ["snapshot_id", "request_id", "status", "reason"]
        assert isinstance(entry["request_id"], int)
        assert entry["status"] == "pending"
        assert entry["reason"] == "retention reached"
    assert body["skipped"] == []
    assert body["counts"] == {
        "created_count": 2,
        "skipped_count": 0,
        "not_due_count": 0,
    }

    # The new requests are ordinary deletion requests of each snapshot.
    for entry, snapshot in zip(body["created"], (first, second)):
        listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
        assert [item["id"] for item in listed] == [entry["request_id"]]
        assert listed[0]["reason"] == "retention reached"
        assert listed[0]["status"] == "pending"


def test_sweep_response_is_a_deterministic_document(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    response = client.post(sweep_path(), json={"reason": "cleanup"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert list(payload) == ["dataset", "version", "created", "skipped", "counts"]
    assert list(payload["counts"]) == [
        "created_count",
        "skipped_count",
        "not_due_count",
    ]
    # Compact whitespace, fixed key order, exactly one trailing newline.
    assert response.text == (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    )
    request_id = payload["created"][0]["request_id"]
    assert response.text == (
        '{"dataset":"raw","version":1,"created":[{"snapshot_id":'
        f'{snapshot["id"]},"request_id":{request_id},"status":"pending",'
        '"reason":"cleanup"}],"skipped":[],"counts":{"created_count":1,'
        '"skipped_count":0,"not_due_count":0}}\n'
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

    assert body["created"][0]["snapshot_id"] == snapshot["id"]
    assert body["created"][0]["status"] == "blocked"
    # The stored request carries the same impacted list as the single-snapshot
    # entry would produce.
    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert listed[0]["status"] == "blocked"
    assert listed[0]["impacted"] == [{"dataset": "dm", "version": 1, "field": "id"}]


def test_sweep_skips_snapshots_with_open_requests(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    protected = make_snapshot(client, rows=[{"id": 1}])
    open_request = client.post(
        requests_path("raw", 1, protected["id"]), json={"reason": "original reason"}
    )
    assert open_request.status_code == 201, open_request.text
    fresh = make_snapshot(client, rows=[{"id": 2}])

    body = sweep(client, reason="sweep reason")

    # The snapshot with the open request is skipped: no duplicate is created
    # and the existing request is echoed unchanged.
    assert body["skipped"] == [
        {
            "snapshot_id": protected["id"],
            "request_id": open_request.json()["id"],
            "status": "pending",
            "reason": "original reason",
        }
    ]
    assert [entry["snapshot_id"] for entry in body["created"]] == [fresh["id"]]
    assert body["counts"] == {
        "created_count": 1,
        "skipped_count": 1,
        "not_due_count": 0,
    }
    listed = client.get(requests_path("raw", 1, protected["id"])).json()
    assert [item["id"] for item in listed] == [open_request.json()["id"]]
    assert listed[0]["reason"] == "original reason"


def test_sweep_skips_not_due_snapshots(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=7)
    old = make_snapshot(client, rows=[{"id": 1}])
    young = make_snapshot(client, rows=[{"id": 2}])
    backdate_snapshot(old["id"], days=8)

    body = sweep(client)

    assert [entry["snapshot_id"] for entry in body["created"]] == [old["id"]]
    assert body["skipped"] == []
    assert body["counts"] == {
        "created_count": 1,
        "skipped_count": 0,
        "not_due_count": 1,
    }
    # The not-due snapshot has no deletion request.
    assert client.get(requests_path("raw", 1, young["id"])).json() == []


def test_sweep_of_version_without_snapshots_succeeds(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)

    body = sweep(client)

    assert body == {
        "dataset": "raw",
        "version": 1,
        "created": [],
        "skipped": [],
        "counts": {"created_count": 0, "skipped_count": 0, "not_due_count": 0},
    }


def test_second_sweep_skips_everything_it_created(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    first = make_snapshot(client, rows=[{"id": 1}])
    second = make_snapshot(client, rows=[{"id": 2}])

    created = sweep(client)
    again = sweep(client, reason="another reason")

    assert again["created"] == []
    assert again["counts"] == {
        "created_count": 0,
        "skipped_count": 2,
        "not_due_count": 0,
    }
    # The skipped entries echo the requests opened by the first sweep, sorted
    # by snapshot id ascending; the earlier requests are unchanged.
    by_snapshot = {
        entry["snapshot_id"]: entry for entry in created["created"]
    }
    assert again["skipped"] == [
        {
            "snapshot_id": snapshot_id,
            "request_id": by_snapshot[snapshot_id]["request_id"],
            "status": "pending",
            "reason": "retention reached",
        }
        for snapshot_id in (first["id"], second["id"])
    ]


def test_swept_requests_enter_the_existing_confirm_flow(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    body = sweep(client)
    request_id = body["created"][0]["request_id"]

    response = client.post(
        f"{requests_path('raw', 1, snapshot['id'])}/{request_id}/confirm"
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    # The snapshot is atomically gone.
    assert (
        client.get(f"/datasets/raw/versions/1/snapshots/{snapshot['id']}").status_code
        == 404
    )


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def test_sweep_without_retention_policy_is_404(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_snapshot(client)

    response = client.post(sweep_path(), json={"reason": "x"})
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_sweep_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    assert (
        client.post(sweep_path("ghost"), json={"reason": "x"}).status_code == 404
    )
    make_dataset(client, "raw", ["id"])
    assert (
        client.post(sweep_path("raw", 9), json={"reason": "x"}).status_code == 404
    )


def test_sweep_404_takes_precedence_over_body_shape(client: TestClient) -> None:
    # An unknown dataset/version is a 404 even when the body is invalid.
    assert client.post(sweep_path("ghost"), content=b"").status_code == 404
    assert client.post(sweep_path("ghost"), content=b"not json").status_code == 404
    make_dataset(client, "raw", ["id"])
    assert client.post(sweep_path("raw", 9), json={}).status_code == 404
    # A missing policy is likewise a 404 ahead of body shape errors.
    assert client.post(sweep_path(), content=b"").status_code == 404


def test_sweep_rejects_invalid_bodies_and_writes_nothing(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    for content in (b"", b"   ", b" \t\n", b"not json", b"[1, 2]", b'"reason"'):
        response = client.post(sweep_path(), content=content)
        assert response.status_code == 422, content
        assert response.json()["error"] == "validation_error"

    for payload in (
        {},
        {"reason": ""},
        {"reason": "   "},
        {"reason": 123},
        {"reason": None},
        {"reason": ["x"]},
        {"reason": "ok", "extra": True},
    ):
        response = client.post(sweep_path(), json=payload)
        assert response.status_code == 422, payload
        assert response.json()["error"] == "validation_error"

    # Nothing was written: the snapshot still has no deletion request.
    assert client.get(requests_path("raw", 1, snapshot["id"])).json() == []


def test_sweep_rejects_query_parameters(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    response = client.post(sweep_path(), params={"reason": "x"}, json={"reason": "y"})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get(requests_path("raw", 1, snapshot["id"])).json() == []


def test_sweep_reason_is_trimmed(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    body = sweep(client, reason="  retention reached  ")

    assert body["created"][0]["reason"] == "retention reached"
    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert listed[0]["reason"] == "retention reached"


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_sweeps_have_a_single_winner(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        statuses.append(
            thread_client.post(sweep_path(), json={"reason": "sweep"}).status_code
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [200, 409], statuses
    # Exactly one request was created for the snapshot.
    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert len(listed) == 1
    assert listed[0]["reason"] == "sweep"
