"""Tests for lease-based dispatch: lease-dispatch, lease-heartbeat,
lease-complete and reclaim-leases under .../processing-tasks."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

TASKS_PATH = "/datasets/orders/versions/1/processing-tasks"
LEASE_DISPATCH_PATH = f"{TASKS_PATH}/lease-dispatch"
RECLAIM_PATH = f"{TASKS_PATH}/reclaim-leases"

RUN_FIELDS = {
    "id",
    "task_id",
    "attempt",
    "status",
    "started_at",
    "finished_at",
    "error",
}
LEASE_FIELDS = {"lease_id", "worker_id", "lease_expires_at"}
LEASED_RUN_FIELDS = RUN_FIELDS | LEASE_FIELDS


def create_dataset_and_version(client: TestClient, dataset: str = "orders") -> None:
    response = client.post("/datasets", json={"name": dataset})
    assert response.status_code == 201, response.text
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201, response.text


def make_task(client: TestClient, name: str, **overrides) -> dict:
    response = client.post(TASKS_PATH, json={"name": name, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def lease_dispatch(client: TestClient, body: dict):
    return client.post(LEASE_DISPATCH_PATH, json=body)


def lease_one(client: TestClient, worker: str = "worker-1", **overrides) -> dict:
    response = lease_dispatch(
        client, {"worker_id": worker, "lease_seconds": 300, **overrides}
    )
    assert response.status_code == 201, response.text
    runs = response.json()["runs"]
    assert len(runs) == 1
    return runs[0]


def heartbeat(client: TestClient, run_id: int, body: dict):
    return client.post(f"{TASKS_PATH}/runs/{run_id}/lease-heartbeat", json=body)


def lease_complete(client: TestClient, run_id: int, body: dict):
    return client.post(f"{TASKS_PATH}/runs/{run_id}/lease-complete", json=body)


def reclaim(client: TestClient, as_of: str):
    return client.post(RECLAIM_PATH, json={"as_of": as_of})


def task_detail(client: TestClient, task_id: int) -> dict:
    response = client.get(f"{TASKS_PATH}/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


def force_expiry(isolated_database: Path, run_id: int, *, expired: bool = True) -> None:
    """Move a run's lease expiry into the past (or far future) directly."""
    moment = datetime.now(timezone.utc) + timedelta(seconds=-60 if expired else 3600)
    with sqlite3.connect(isolated_database) as conn:
        conn.execute(
            "UPDATE processing_task_runs SET lease_expires_at = ? WHERE id = ?",
            (moment.isoformat(), run_id),
        )


def future_iso(seconds: float = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


# --------------------------------------------------------------------------- #
# lease-dispatch: shape and selection
# --------------------------------------------------------------------------- #


def test_lease_dispatch_starts_one_task_with_lease_fields(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_task(client, "alpha")
    make_task(client, "beta")

    before = datetime.now(timezone.utc)
    response = lease_dispatch(client, {"worker_id": "worker-7", "lease_seconds": 60})
    after = datetime.now(timezone.utc)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert [run["task_id"] for run in body["runs"]] == [first["id"]]

    run = body["runs"][0]
    assert set(run) == LEASED_RUN_FIELDS
    assert run["attempt"] == 1
    assert run["status"] == "running"
    assert run["finished_at"] is None
    assert run["error"] is None
    assert run["worker_id"] == "worker-7"
    assert isinstance(run["lease_id"], str) and run["lease_id"]

    started_at = datetime.fromisoformat(run["started_at"])
    expires_at = datetime.fromisoformat(run["lease_expires_at"])
    # The expiry is exactly the request time plus lease_seconds.
    assert expires_at - started_at == timedelta(seconds=60)
    assert before <= started_at <= after

    detail = task_detail(client, first["id"])
    assert detail["status"] == "running"
    assert detail["attempt_count"] == 1
    assert len(detail["runs"]) == 1


def test_lease_dispatch_limit_and_order(client: TestClient) -> None:
    create_dataset_and_version(client)
    ids = [make_task(client, name)["id"] for name in ("a", "b", "c", "d")]

    response = lease_dispatch(
        client, {"worker_id": "w", "lease_seconds": 30, "limit": 3}
    )
    assert response.status_code == 201, response.text
    runs = response.json()["runs"]
    assert [run["task_id"] for run in runs] == ids[:3]
    # Lease ids are unique within the dataset.
    lease_ids = [run["lease_id"] for run in runs]
    assert len(set(lease_ids)) == len(lease_ids)

    response = lease_dispatch(client, {"worker_id": "w", "lease_seconds": 30})
    assert [run["task_id"] for run in response.json()["runs"]] == ids[3:]


def test_lease_dispatch_with_no_startable_tasks_returns_empty(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    response = lease_dispatch(client, {"worker_id": "w", "lease_seconds": 30})
    assert response.status_code == 201, response.text
    assert response.json() == {"dataset": "orders", "version": 1, "runs": []}


def test_lease_dispatch_skips_running_succeeded_exhausted_and_blocked(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    running = make_task(client, "running")
    lease_one(client)  # starts 'running' (lowest id)

    upstream = make_task(client, "upstream")
    blocked = make_task(client, "blocked", depends_on=[upstream["id"]])

    exhausted = make_task(client, "exhausted")
    # Starts 'upstream' and 'exhausted' ('blocked' waits on 'upstream').
    response = lease_dispatch(
        client, {"worker_id": "w", "lease_seconds": 300, "limit": 2}
    )
    assert response.status_code == 201, response.text
    runs = response.json()["runs"]
    assert {r["task_id"] for r in runs} == {upstream["id"], exhausted["id"]}
    exhausted_run = next(r for r in runs if r["task_id"] == exhausted["id"])
    response = lease_complete(
        client,
        exhausted_run["id"],
        {
            "lease_id": exhausted_run["lease_id"],
            "status": "failed",
            "error": "boom",
        },
    )
    assert response.status_code == 200, response.text

    # 'running' is leased, 'blocked' waits on 'upstream', 'exhausted' is spent:
    # nothing is startable.
    response = lease_dispatch(client, {"worker_id": "w", "lease_seconds": 30})
    assert response.json()["runs"] == []
    assert task_detail(client, blocked["id"])["status"] == "pending"
    assert task_detail(client, running["id"])["status"] == "running"


def test_lease_dispatch_retries_failed_task_with_attempts_left(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    flaky = make_task(client, "flaky", max_attempts=2)
    run = lease_one(client)
    response = lease_complete(
        client,
        run["id"],
        {"lease_id": run["lease_id"], "status": "failed", "error": "boom"},
    )
    assert response.status_code == 200, response.text

    retry = lease_one(client)
    assert retry["task_id"] == flaky["id"]
    assert retry["attempt"] == 2
    assert retry["lease_id"] != run["lease_id"]


# --------------------------------------------------------------------------- #
# lease-dispatch: validation and unknown resources
# --------------------------------------------------------------------------- #


def test_lease_dispatch_rejects_invalid_bodies(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "alpha")
    for body in (
        {},
        {"lease_seconds": 30},
        {"worker_id": "w"},
        {"worker_id": "", "lease_seconds": 30},
        {"worker_id": "   ", "lease_seconds": 30},
        {"worker_id": None, "lease_seconds": 30},
        {"worker_id": 7, "lease_seconds": 30},
        {"worker_id": "w", "lease_seconds": 0},
        {"worker_id": "w", "lease_seconds": -5},
        {"worker_id": "w", "lease_seconds": 1.5},
        {"worker_id": "w", "lease_seconds": "30"},
        {"worker_id": "w", "lease_seconds": True},
        {"worker_id": "w", "lease_seconds": 30, "limit": 0},
        {"worker_id": "w", "lease_seconds": 30, "limit": -1},
        {"worker_id": "w", "lease_seconds": 30, "limit": "2"},
        {"worker_id": "w", "lease_seconds": 30, "limit": None},
        {"worker_id": "w", "lease_seconds": 30, "unexpected": 1},
    ):
        response = lease_dispatch(client, body)
        assert response.status_code == 422, (body, response.text)
        assert response.json()["error"] == "validation_error"

    # Nothing was written: the task is still pending with no runs.
    detail = task_detail(client, task["id"])
    assert detail["status"] == "pending"
    assert detail["attempt_count"] == 0
    assert detail["runs"] == []


def test_lease_dispatch_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    body = {"worker_id": "w", "lease_seconds": 30}
    response = client.post(
        "/datasets/ghost/versions/1/processing-tasks/lease-dispatch", json=body
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    create_dataset_and_version(client)
    response = client.post(
        "/datasets/orders/versions/9/processing-tasks/lease-dispatch", json=body
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_lease_dispatch_rejects_query_parameters(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    response = client.post(
        f"{LEASE_DISPATCH_PATH}?limit=2",
        json={"worker_id": "w", "lease_seconds": 30},
    )
    assert response.status_code == 422
    assert task_detail(client, 1)["runs"] == []


# --------------------------------------------------------------------------- #
# lease-heartbeat
# --------------------------------------------------------------------------- #


def test_heartbeat_extends_the_lease(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    run = lease_one(client, lease_seconds=60)

    before = datetime.now(timezone.utc)
    response = heartbeat(
        client, run["id"], {"lease_id": run["lease_id"], "lease_seconds": 600}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == LEASED_RUN_FIELDS
    assert body["id"] == run["id"]
    assert body["status"] == "running"
    assert body["lease_id"] == run["lease_id"]
    assert body["worker_id"] == run["worker_id"]
    new_expiry = datetime.fromisoformat(body["lease_expires_at"])
    assert new_expiry >= before + timedelta(seconds=600)
    assert new_expiry > datetime.fromisoformat(run["lease_expires_at"])


def test_heartbeat_rejects_foreign_or_unknown_lease(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "alpha")
    run = lease_one(client)

    for lease_id in ("no-such-lease", "", run["lease_id"] + "x"):
        response = heartbeat(
            client, run["id"], {"lease_id": lease_id, "lease_seconds": 60}
        )
        assert response.status_code == 409, (lease_id, response.text)
        assert response.json()["error"] == "conflict"

    # The lease is unchanged.
    detail = task_detail(client, task["id"])
    assert detail["runs"][0]["status"] == "running"


def test_heartbeat_rejects_expired_lease(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    run = lease_one(client, lease_seconds=60)
    force_expiry(isolated_database, run["id"])

    response = heartbeat(
        client, run["id"], {"lease_id": run["lease_id"], "lease_seconds": 60}
    )
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"


def test_heartbeat_rejects_ended_run(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    run = lease_one(client)
    response = lease_complete(
        client, run["id"], {"lease_id": run["lease_id"], "status": "succeeded"}
    )
    assert response.status_code == 200, response.text

    response = heartbeat(
        client, run["id"], {"lease_id": run["lease_id"], "lease_seconds": 60}
    )
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"


def test_heartbeat_unknown_path_objects_are_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    run = lease_one(client)
    body = {"lease_id": run["lease_id"], "lease_seconds": 60}

    response = client.post(
        "/datasets/ghost/versions/1/processing-tasks/runs/"
        f"{run['id']}/lease-heartbeat",
        json=body,
    )
    assert response.status_code == 404
    response = client.post(
        "/datasets/orders/versions/9/processing-tasks/runs/"
        f"{run['id']}/lease-heartbeat",
        json=body,
    )
    assert response.status_code == 404
    response = heartbeat(client, 9999, body)
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_heartbeat_rejects_invalid_bodies(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    run = lease_one(client)
    for body in (
        {},
        {"lease_id": run["lease_id"]},
        {"lease_seconds": 60},
        {"lease_id": run["lease_id"], "lease_seconds": 0},
        {"lease_id": run["lease_id"], "lease_seconds": -2},
        {"lease_id": run["lease_id"], "lease_seconds": "60"},
        {"lease_id": None, "lease_seconds": 60},
        {"lease_id": run["lease_id"], "lease_seconds": 60, "extra": 1},
    ):
        response = heartbeat(client, run["id"], body)
        assert response.status_code == 422, (body, response.text)


# --------------------------------------------------------------------------- #
# lease-complete
# --------------------------------------------------------------------------- #


def test_lease_complete_success(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "alpha")
    run = lease_one(client)

    response = lease_complete(
        client, run["id"], {"lease_id": run["lease_id"], "status": "succeeded"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == LEASED_RUN_FIELDS
    assert body["status"] == "succeeded"
    assert body["finished_at"] is not None
    assert body["error"] is None
    assert body["lease_id"] == run["lease_id"]

    detail = task_detail(client, task["id"])
    assert detail["status"] == "succeeded"


def test_lease_complete_failure_requires_error(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "alpha")
    run = lease_one(client)

    for body in (
        {"lease_id": run["lease_id"], "status": "failed"},
        {"lease_id": run["lease_id"], "status": "failed", "error": None},
        {"lease_id": run["lease_id"], "status": "failed", "error": "  "},
        {"lease_id": run["lease_id"], "status": "succeeded", "error": "nope"},
        {"lease_id": run["lease_id"], "status": "running"},
        {"status": "succeeded"},
    ):
        response = lease_complete(client, run["id"], body)
        assert response.status_code == 422, (body, response.text)

    detail = task_detail(client, task["id"])
    assert detail["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None

    response = lease_complete(
        client,
        run["id"],
        {"lease_id": run["lease_id"], "status": "failed", "error": "boom"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["error"] == "boom"
    assert task_detail(client, task["id"])["status"] == "failed"


def test_lease_complete_rejects_foreign_expired_and_ended_leases(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    run = lease_one(client)

    # Foreign lease id.
    response = lease_complete(
        client, run["id"], {"lease_id": "other-lease", "status": "succeeded"}
    )
    assert response.status_code == 409

    # Expired lease.
    force_expiry(isolated_database, run["id"])
    response = lease_complete(
        client, run["id"], {"lease_id": run["lease_id"], "status": "succeeded"}
    )
    assert response.status_code == 409
    detail = task_detail(client, 1)
    assert detail["status"] == "running"

    # Reclaim the expired lease, then the ended run rejects completion.
    response = reclaim(client, future_iso())
    assert response.status_code == 200, response.text
    response = lease_complete(
        client, run["id"], {"lease_id": run["lease_id"], "status": "succeeded"}
    )
    assert response.status_code == 409


def test_lease_complete_unknown_path_objects_are_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    run = lease_one(client)
    body = {"lease_id": run["lease_id"], "status": "succeeded"}

    response = client.post(
        "/datasets/ghost/versions/1/processing-tasks/runs/"
        f"{run['id']}/lease-complete",
        json=body,
    )
    assert response.status_code == 404
    response = client.post(
        "/datasets/orders/versions/9/processing-tasks/runs/"
        f"{run['id']}/lease-complete",
        json=body,
    )
    assert response.status_code == 404
    response = lease_complete(client, 9999, body)
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


# --------------------------------------------------------------------------- #
# reclaim-leases
# --------------------------------------------------------------------------- #


def test_reclaim_fails_only_expired_running_runs(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset_and_version(client)
    expired_task = make_task(client, "expired", max_attempts=2)
    fresh_task = make_task(client, "fresh")
    runs = lease_dispatch(
        client, {"worker_id": "w", "lease_seconds": 300, "limit": 2}
    ).json()["runs"]
    expired_run = next(r for r in runs if r["task_id"] == expired_task["id"])
    fresh_run = next(r for r in runs if r["task_id"] == fresh_task["id"])
    force_expiry(isolated_database, expired_run["id"])

    # Reclaim as of now: only the forced-expired run is due; the fresh run's
    # lease is still valid.
    response = reclaim(client, datetime.now(timezone.utc).isoformat())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert [run["id"] for run in body["runs"]] == [expired_run["id"]]
    run = body["runs"][0]
    assert set(run) == LEASED_RUN_FIELDS
    assert run["task_id"] == expired_task["id"]
    assert run["status"] == "failed"
    assert run["error"] == "lease expired"
    assert run["finished_at"] is not None
    assert run["lease_id"] == expired_run["lease_id"]
    assert run["worker_id"] == "w"

    # The task failed with attempts left: it can be dispatched again.
    detail = task_detail(client, expired_task["id"])
    assert detail["status"] == "failed"
    assert detail["attempt_count"] == 1
    retry = lease_one(client)
    assert retry["task_id"] == expired_task["id"]
    assert retry["attempt"] == 2

    # The unexpired run was never touched.
    detail = task_detail(client, fresh_task["id"])
    assert detail["status"] == "running"
    assert detail["runs"][0]["id"] == fresh_run["id"]


def test_reclaim_exhausted_task_is_not_retryable(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "one-shot")  # max_attempts defaults to 1
    run = lease_one(client)
    force_expiry(isolated_database, run["id"])

    response = reclaim(client, future_iso())
    assert [r["task_id"] for r in response.json()["runs"]] == [task["id"]]

    detail = task_detail(client, task["id"])
    assert detail["status"] == "failed"
    assert detail["attempt_count"] == 1
    # Attempts exhausted: neither dispatch entry starts it again.
    response = lease_dispatch(client, {"worker_id": "w", "lease_seconds": 30})
    assert response.json()["runs"] == []
    response = client.post(f"{TASKS_PATH}/dispatch", json={"limit": 5})
    assert response.json()["runs"] == []


def test_reclaim_leaves_unleased_runs_alone(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset_and_version(client)
    leased_task = make_task(client, "leased")
    plain_task = make_task(client, "plain")
    leased_run = lease_one(client)
    response = client.post(f"{TASKS_PATH}/{plain_task['id']}/runs")
    assert response.status_code == 201, response.text
    force_expiry(isolated_database, leased_run["id"])

    response = reclaim(client, future_iso())
    assert [r["task_id"] for r in response.json()["runs"]] == [leased_task["id"]]
    # The lease-free run has no expiry and is never reclaimed.
    assert task_detail(client, plain_task["id"])["status"] == "running"


def test_reclaim_is_idempotent(client: TestClient, isolated_database: Path) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "alpha")
    run = lease_one(client)
    force_expiry(isolated_database, run["id"])

    first = reclaim(client, future_iso())
    assert len(first.json()["runs"]) == 1
    second = reclaim(client, future_iso())
    assert second.status_code == 200
    assert second.json()["runs"] == []

    detail = task_detail(client, task["id"])
    assert detail["status"] == "failed"
    assert len(detail["runs"]) == 1


def test_reclaim_orders_runs_by_run_id(client: TestClient, isolated_database: Path) -> None:
    create_dataset_and_version(client)
    for name in ("a", "b", "c"):
        make_task(client, name)
    runs = lease_dispatch(
        client, {"worker_id": "w", "lease_seconds": 300, "limit": 3}
    ).json()["runs"]
    # Expire only the first and third run.
    force_expiry(isolated_database, runs[0]["id"])
    force_expiry(isolated_database, runs[2]["id"])

    # Reclaim as of now so the still-valid second lease stays running.
    response = reclaim(client, datetime.now(timezone.utc).isoformat())
    reclaimed = response.json()["runs"]
    assert [run["id"] for run in reclaimed] == sorted(
        [runs[0]["id"], runs[2]["id"]]
    )
    assert [run["task_id"] for run in reclaimed] == [
        runs[0]["task_id"],
        runs[2]["task_id"],
    ]


def test_reclaim_respects_as_of_cutoff(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    run = lease_one(client, lease_seconds=300)

    # An as_of before the expiry reclaims nothing.
    response = reclaim(client, datetime.now(timezone.utc).isoformat())
    assert response.json()["runs"] == []
    assert task_detail(client, 1)["status"] == "running"

    # An as_of past the expiry reclaims the run.
    response = reclaim(client, future_iso(600))
    assert [r["id"] for r in response.json()["runs"]] == [run["id"]]


def test_reclaim_rejects_invalid_as_of(client: TestClient, isolated_database: Path) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "alpha")
    run = lease_one(client)
    force_expiry(isolated_database, run["id"])

    for body in (
        {},
        {"as_of": None},
        {"as_of": 123},
        {"as_of": "not-a-date"},
        {"as_of": "2030-01-01T00:00:00"},  # no timezone
        {"as_of": future_iso(), "extra": 1},
    ):
        response = client.post(RECLAIM_PATH, json=body)
        assert response.status_code == 422, (body, response.text)

    # Zero writes: the expired run is still running and reclaimable.
    assert task_detail(client, task["id"])["status"] == "running"
    response = reclaim(client, future_iso())
    assert len(response.json()["runs"]) == 1


def test_reclaim_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    body = {"as_of": future_iso()}
    response = client.post(
        "/datasets/ghost/versions/1/processing-tasks/reclaim-leases", json=body
    )
    assert response.status_code == 404
    create_dataset_and_version(client)
    response = client.post(
        "/datasets/orders/versions/9/processing-tasks/reclaim-leases", json=body
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


# --------------------------------------------------------------------------- #
# Interplay with the lease-free entry points
# --------------------------------------------------------------------------- #


def test_lease_free_entry_points_keep_their_behavior(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_task(client, "alpha")
    second = make_task(client, "beta")

    # The plain dispatch response carries no lease fields.
    response = client.post(f"{TASKS_PATH}/dispatch", json={"limit": 1})
    assert response.status_code == 201, response.text
    run = response.json()["runs"][0]
    assert set(run) == RUN_FIELDS
    assert run["task_id"] == first["id"]

    # The plain finish completes a leased run too, without lease checks.
    leased = lease_one(client)
    assert leased["task_id"] == second["id"]
    response = client.patch(
        f"{TASKS_PATH}/{second['id']}/runs/{leased['id']}",
        json={"status": "succeeded"},
    )
    assert response.status_code == 200, response.text
    assert set(response.json()) == RUN_FIELDS
    assert task_detail(client, second["id"])["status"] == "succeeded"

    # A lease-complete on a lease-free run is a conflict, not a write.
    plain = task_detail(client, first["id"])["runs"][0]
    response = lease_complete(
        client, plain["id"], {"lease_id": "whatever", "status": "succeeded"}
    )
    assert response.status_code == 409
    assert task_detail(client, first["id"])["status"] == "running"


def test_lease_dispatch_shares_the_single_running_run_rule(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "alpha", max_attempts=3)
    lease_one(client)

    # A second start of any flavor conflicts while the task is running.
    response = client.post(f"{TASKS_PATH}/{task['id']}/runs")
    assert response.status_code == 409
    response = lease_dispatch(client, {"worker_id": "w", "lease_seconds": 30})
    assert response.json()["runs"] == []
    response = client.post(f"{TASKS_PATH}/dispatch", json={"limit": 5})
    assert response.json()["runs"] == []
    detail = task_detail(client, task["id"])
    assert len(detail["runs"]) == 1


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_lease_dispatches_never_duplicate_attempts(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task_ids = [make_task(client, f"task-{i}")["id"] for i in range(10)]

    thread_count = 5
    barrier = threading.Barrier(thread_count)
    collected: list[dict] = []
    collection_lock = threading.Lock()

    def worker(index: int) -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        local: list[dict] = []
        for _ in range(20):
            response = thread_client.post(
                LEASE_DISPATCH_PATH,
                json={"worker_id": f"w-{index}", "lease_seconds": 60, "limit": 2},
            )
            assert response.status_code == 201, response.text
            runs = response.json()["runs"]
            local.extend(runs)
            if not runs:
                break
        with collection_lock:
            collected.extend(local)

    threads = [
        threading.Thread(target=worker, args=(i,)) for i in range(thread_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Every task was leased exactly once: no duplicated or skipped attempts,
    # unique run ids and unique lease ids.
    assert sorted(run["task_id"] for run in collected) == sorted(task_ids)
    assert all(run["attempt"] == 1 for run in collected)
    assert len({run["id"] for run in collected}) == len(collected)
    assert len({run["lease_id"] for run in collected}) == len(collected)
    for task_id in task_ids:
        detail = task_detail(client, task_id)
        assert detail["status"] == "running"
        assert len(detail["runs"]) == 1
