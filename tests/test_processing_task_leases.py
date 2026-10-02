"""Tests for the lease-based task flow.

Covers lease-dispatch, lease-heartbeat, lease-complete and reclaim-leases
under /datasets/{dataset}/versions/{version}/processing-tasks, including
selection rules, lease uniqueness and expiry, holder checks, precedence
(404 before 422 before 409), zero-write rejections, retry/exhaustion
semantics, interaction with the legacy entry points and concurrency.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

BASE = "/datasets/orders/versions/1/processing-tasks"


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
    response = client.post(BASE, json={"name": name, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def task_detail(client: TestClient, task_id: int, base: str = BASE) -> dict:
    response = client.get(f"{base}/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


def lease_dispatch(
    client: TestClient,
    body=None,
    *,
    raw=None,
    base: str = BASE,
    query: str = "",
):
    if raw is not None:
        return client.post(
            f"{base}/lease-dispatch{query}",
            content=raw,
            headers={"content-type": "application/json"},
        )
    return client.post(f"{base}/lease-dispatch{query}", json=body)


def heartbeat(client: TestClient, run_id: int, body=None, *, raw=None, query=""):
    if raw is not None:
        return client.post(
            f"{BASE}/runs/{run_id}/lease-heartbeat{query}",
            content=raw,
            headers={"content-type": "application/json"},
        )
    return client.post(
        f"{BASE}/runs/{run_id}/lease-heartbeat{query}", json=body
    )


def lease_complete(client: TestClient, run_id: int, body=None, *, raw=None, query=""):
    if raw is not None:
        return client.post(
            f"{BASE}/runs/{run_id}/lease-complete{query}",
            content=raw,
            headers={"content-type": "application/json"},
        )
    return client.post(f"{BASE}/runs/{run_id}/lease-complete{query}", json=body)


def reclaim(client: TestClient, as_of, *, raw=None, query=""):
    if raw is not None:
        return client.post(
            f"{BASE}/reclaim-leases{query}",
            content=raw,
            headers={"content-type": "application/json"},
        )
    return client.post(f"{BASE}/reclaim-leases{query}", json={"as_of": as_of})


def claim_one(client: TestClient, worker="w-1", lease_seconds=60) -> dict:
    response = lease_dispatch(
        client, {"worker_id": worker, "lease_seconds": lease_seconds}
    )
    assert response.status_code == 201, response.text
    runs = response.json()["runs"]
    assert len(runs) == 1
    return runs[0]


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


# --------------------------------------------------------------------------- #
# Lease dispatch
# --------------------------------------------------------------------------- #


def test_lease_dispatch_default_limit_and_element_shape(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "alpha")

    before = datetime.now(timezone.utc)
    response = lease_dispatch(client, {"worker_id": "w-1", "lease_seconds": 90})
    after = datetime.now(timezone.utc)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert len(body["runs"]) == 1
    run = body["runs"][0]
    assert set(run) == LEASED_RUN_FIELDS
    assert run["task_id"] == task["id"]
    assert run["attempt"] == 1
    assert run["status"] == "running"
    assert run["finished_at"] is None
    assert run["error"] is None
    assert run["worker_id"] == "w-1"
    assert isinstance(run["lease_id"], str) and run["lease_id"]
    expires = parse_iso(run["lease_expires_at"])
    assert before + timedelta(seconds=90) <= expires <= after + timedelta(seconds=90)

    detail = task_detail(client, task["id"])
    assert detail["status"] == "running"
    assert detail["attempt_count"] == 1
    assert len(detail["runs"]) == 1
    assert detail["runs"][0]["id"] == run["id"]
    # Lease fields are not part of the ordinary run read.
    assert set(detail["runs"][0]) == RUN_FIELDS


def test_lease_dispatch_empty_when_nothing_startable(client: TestClient) -> None:
    create_dataset_and_version(client)
    response = lease_dispatch(
        client, {"worker_id": "w-1", "lease_seconds": 30, "limit": 5}
    )
    assert response.status_code == 201
    assert response.json() == {"dataset": "orders", "version": 1, "runs": []}


def test_lease_dispatch_ascending_and_limit(client: TestClient) -> None:
    create_dataset_and_version(client)
    ids = [make_task(client, f"t{i}")["id"] for i in range(4)]
    response = lease_dispatch(
        client, {"worker_id": "w-1", "lease_seconds": 30, "limit": 3}
    )
    runs = response.json()["runs"]
    assert [run["task_id"] for run in runs] == ids[:3]
    assert [run["id"] for run in runs] == sorted(run["id"] for run in runs)

    response = lease_dispatch(
        client, {"worker_id": "w-1", "lease_seconds": 30, "limit": 3}
    )
    assert [run["task_id"] for run in response.json()["runs"]] == ids[3:]


def test_lease_dispatch_retries_failed_and_respects_attempts(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "flaky", max_attempts=3)
    run = claim_one(client, lease_seconds=60)
    response = lease_complete(
        client, run["id"], {"lease_id": run["lease_id"], "status": "failed",
                            "error": "boom"}
    )
    assert response.status_code == 200, response.text

    response = lease_dispatch(
        client, {"worker_id": "w-2", "lease_seconds": 60, "limit": 5}
    )
    runs = response.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["task_id"] == task["id"]
    assert runs[0]["attempt"] == 2
    detail = task_detail(client, task["id"])
    assert [r["attempt"] for r in detail["runs"]] == [1, 2]


def test_lease_dispatch_respects_dependencies_without_same_request_chaining(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    upstream = make_task(client, "upstream")
    dependent = make_task(client, "dependent", depends_on=[upstream["id"]])

    response = lease_dispatch(
        client, {"worker_id": "w", "lease_seconds": 60, "limit": 10}
    )
    assert [run["task_id"] for run in response.json()["runs"]] == [upstream["id"]]
    assert task_detail(client, dependent["id"])["status"] == "pending"

    run = task_detail(client, upstream["id"])["runs"][0]
    upstream_lease = lease_dispatch(client, {"worker_id": "w", "lease_seconds": 60})
    # Nothing left for a second dispatch while the upstream is still running.
    assert upstream_lease.json()["runs"] == []

    # Finish out of band through the lease endpoint requires the lease id; use
    # the legacy finish, which must keep its old behavior.
    response = client.patch(
        f"{BASE}/{upstream['id']}/runs/{run['id']}", json={"status": "succeeded"}
    )
    assert response.status_code == 200
    response = lease_dispatch(
        client, {"worker_id": "w", "lease_seconds": 60, "limit": 10}
    )
    assert [run["task_id"] for run in response.json()["runs"]] == [dependent["id"]]


def test_lease_ids_are_unique_within_dataset_across_versions(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    for name in ("a", "b", "c"):
        make_task(client, name)
    response = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201
    base2 = "/datasets/orders/versions/2/processing-tasks"
    for name in ("d", "e", "f"):
        r = client.post(base2, json={"name": name})
        assert r.status_code == 201

    r1 = lease_dispatch(
        client, {"worker_id": "w", "lease_seconds": 60, "limit": 10}
    ).json()["runs"]
    r2 = lease_dispatch(
        client, {"worker_id": "w", "lease_seconds": 60, "limit": 10}, base=base2
    ).json()["runs"]
    ids = [run["lease_id"] for run in r1 + r2]
    assert len(ids) == 6
    assert len(set(ids)) == 6


def test_lease_dispatch_validation_422_and_zero_writes(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "alpha")
    valid = {"worker_id": "w-1", "lease_seconds": 10}
    bad_bodies = [
        {},
        {"lease_seconds": 10},
        {"worker_id": "w-1"},
        {"worker_id": "", "lease_seconds": 10},
        {"worker_id": 7, "lease_seconds": 10},
        {"worker_id": None, "lease_seconds": 10},
        {"worker_id": "w", "lease_seconds": 0},
        {"worker_id": "w", "lease_seconds": -1},
        {"worker_id": "w", "lease_seconds": 1.5},
        {"worker_id": "w", "lease_seconds": "10"},
        {"worker_id": "w", "lease_seconds": True},
        {"worker_id": "w", "lease_seconds": None},
        {"worker_id": "w", "lease_seconds": 10, "limit": 0},
        {"worker_id": "w", "lease_seconds": 10, "limit": -2},
        {"worker_id": "w", "lease_seconds": 10, "limit": 1.5},
        {"worker_id": "w", "lease_seconds": 10, "limit": "2"},
        {"worker_id": "w", "lease_seconds": 10, "limit": True},
        {"worker_id": "w", "lease_seconds": 10, "limit": None},
        {"worker_id": "w", "lease_seconds": 10, "unexpected": 1},
    ]
    for body in bad_bodies:
        response = lease_dispatch(client, body)
        assert response.status_code == 422, (body, response.text)
        assert response.json()["error"] == "validation_error"

    for raw in (b"", b"   ", b"{not json", b"[1,2]", b'"worker"'):
        response = lease_dispatch(client, raw=raw)
        assert response.status_code == 422, raw
        assert response.json()["error"] == "validation_error"

    response = lease_dispatch(client, valid, query="?x=1")
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"

    detail = task_detail(client, task["id"])
    assert detail["status"] == "pending"
    assert detail["attempt_count"] == 0
    assert detail["runs"] == []


def test_whitespace_worker_id_is_non_empty_and_kept(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    response = lease_dispatch(client, {"worker_id": "  ", "lease_seconds": 10})
    assert response.status_code == 201, response.text
    assert response.json()["runs"][0]["worker_id"] == "  "


def test_lease_dispatch_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    body = {"worker_id": "w", "lease_seconds": 10}
    response = lease_dispatch(
        client, body, base="/datasets/ghost/versions/1/processing-tasks"
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    # 404 takes precedence over an invalid body.
    response = lease_dispatch(
        client, {}, base="/datasets/ghost/versions/1/processing-tasks"
    )
    assert response.status_code == 404

    create_dataset_and_version(client)
    response = lease_dispatch(
        client, body, base="/datasets/orders/versions/9/processing-tasks"
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Heartbeat
# --------------------------------------------------------------------------- #


def test_heartbeat_extends_active_lease(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    run = claim_one(client, lease_seconds=30)
    old_expires = parse_iso(run["lease_expires_at"])

    time.sleep(0.01)
    before = datetime.now(timezone.utc)
    response = heartbeat(
        client, run["id"], {"lease_id": run["lease_id"], "lease_seconds": 120}
    )
    after = datetime.now(timezone.utc)
    assert response.status_code == 200, response.text
    updated = response.json()
    assert set(updated) == LEASED_RUN_FIELDS
    assert updated["id"] == run["id"]
    assert updated["status"] == "running"
    new_expires = parse_iso(updated["lease_expires_at"])
    assert new_expires > old_expires
    assert before + timedelta(seconds=120) <= new_expires <= after + timedelta(
        seconds=120
    )


def test_heartbeat_unknown_path_run_is_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    body = {"lease_id": "lease-x", "lease_seconds": 10}
    response = heartbeat(client, 999, body)
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    # 404 precedes body validation.
    response = heartbeat(client, 999, {})
    assert response.status_code == 404


def test_heartbeat_run_of_another_version_is_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    response = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201
    base2 = "/datasets/orders/versions/2/processing-tasks"
    make_task(client, "v1-alpha")
    r = client.post(base2, json={"name": "v2-alpha"})
    assert r.status_code == 201
    v2_run = lease_dispatch(
        client, {"worker_id": "w", "lease_seconds": 60}, base=base2
    ).json()["runs"][0]
    response = heartbeat(
        client, v2_run["id"],
        {"lease_id": v2_run["lease_id"], "lease_seconds": 30},
    )
    assert response.status_code == 404


def test_heartbeat_conflicts_409(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "a")
    make_task(client, "b")
    run1 = claim_one(client, worker="w-1", lease_seconds=60)
    run2 = claim_one(client, worker="w-2", lease_seconds=60)

    # Unknown lease token.
    response = heartbeat(client, run1["id"], {"lease_id": "lease-nope",
                                              "lease_seconds": 10})
    assert response.status_code == 409, response.text

    # A valid lease of another run is not the holder of this one.
    response = heartbeat(
        client, run1["id"], {"lease_id": run2["lease_id"], "lease_seconds": 10}
    )
    assert response.status_code == 409

    # Ended run: complete it, then the (formerly active) lease is rejected.
    response = lease_complete(
        client, run2["id"], {"lease_id": run2["lease_id"], "status": "succeeded"}
    )
    assert response.status_code == 200
    response = heartbeat(
        client, run2["id"], {"lease_id": run2["lease_id"], "lease_seconds": 10}
    )
    assert response.status_code == 409


def test_heartbeat_expired_lease_is_409(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    run = claim_one(client, lease_seconds=1)
    time.sleep(1.05)
    response = heartbeat(
        client, run["id"], {"lease_id": run["lease_id"], "lease_seconds": 30}
    )
    assert response.status_code == 409
    # The failed heartbeat must not extend anything: the stored expiry is in
    # the past and a reclaim now collects the run.
    cutoff = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    response = reclaim(client, cutoff)
    assert response.status_code == 200
    assert [r["id"] for r in response.json()["runs"]] == [run["id"]]


def test_heartbeat_validation_422_and_zero_writes(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    run = claim_one(client, lease_seconds=60)
    bodies = [
        {},
        {"lease_seconds": 10},
        {"lease_id": run["lease_id"]},
        {"lease_id": "", "lease_seconds": 10},
        {"lease_id": 9, "lease_seconds": 10},
        {"lease_id": run["lease_id"], "lease_seconds": 0},
        {"lease_id": run["lease_id"], "lease_seconds": -1},
        {"lease_id": run["lease_id"], "lease_seconds": 1.5},
        {"lease_id": run["lease_id"], "lease_seconds": "10"},
        {"lease_id": run["lease_id"], "lease_seconds": True},
        {"lease_id": run["lease_id"], "lease_seconds": 10, "extra": 1},
    ]
    for body in bodies:
        response = heartbeat(client, run["id"], body)
        assert response.status_code == 422, (body, response.text)
    for raw in (b"", b"  ", b"{bad", b"[]", b"7"):
        response = heartbeat(client, run["id"], raw=raw)
        assert response.status_code == 422, raw

    response = heartbeat(
        client, run["id"],
        {"lease_id": run["lease_id"], "lease_seconds": 10},
        query="?x=1",
    )
    assert response.status_code == 422

    # A valid heartbeat still works; the rejected calls extended nothing.
    response = heartbeat(
        client, run["id"], {"lease_id": run["lease_id"], "lease_seconds": 10}
    )
    assert response.status_code == 200
    new_expires = parse_iso(response.json()["lease_expires_at"])
    assert new_expires > datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Lease complete
# --------------------------------------------------------------------------- #


def test_lease_complete_succeeds_run_and_task(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "alpha")
    run = claim_one(client)
    response = lease_complete(
        client, run["id"], {"lease_id": run["lease_id"], "status": "succeeded"}
    )
    assert response.status_code == 200, response.text
    completed = response.json()
    assert set(completed) == LEASED_RUN_FIELDS
    assert completed["status"] == "succeeded"
    assert completed["finished_at"] is not None
    assert completed["error"] is None
    detail = task_detail(client, task["id"])
    assert detail["status"] == "succeeded"
    assert detail["runs"][0]["status"] == "succeeded"


def test_lease_complete_explicit_null_error_on_success_is_allowed(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    run = claim_one(client)
    response = lease_complete(
        client,
        run["id"],
        {"lease_id": run["lease_id"], "status": "succeeded", "error": None},
    )
    assert response.status_code == 200, response.text
    assert response.json()["error"] is None


def test_lease_complete_failure_is_retryable_then_exhausted(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "flaky", max_attempts=2)
    run = claim_one(client, lease_seconds=60)
    response = lease_complete(
        client,
        run["id"],
        {"lease_id": run["lease_id"], "status": "failed", "error": "  boom  "},
    )
    assert response.status_code == 200
    assert response.json()["error"] == "  boom  "
    detail = task_detail(client, task["id"])
    assert detail["status"] == "failed"
    assert detail["attempt_count"] == 1

    # Attempts remain: the task is claimable again at its next attempt.
    run2 = claim_one(client, worker="w-2", lease_seconds=60)
    assert run2["attempt"] == 2
    response = lease_complete(
        client,
        run2["id"],
        {"lease_id": run2["lease_id"], "status": "failed", "error": "again"},
    )
    assert response.status_code == 200
    response = lease_dispatch(
        client, {"worker_id": "w", "lease_seconds": 60, "limit": 5}
    )
    assert response.json()["runs"] == []
    detail = task_detail(client, task["id"])
    assert detail["status"] == "failed"
    assert detail["attempt_count"] == 2


def test_lease_complete_conflicts_409(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "a")
    make_task(client, "b")
    run1 = claim_one(client, worker="w-1", lease_seconds=60)
    run2 = claim_one(client, worker="w-2", lease_seconds=60)

    response = lease_complete(
        client, run1["id"], {"lease_id": "lease-nope", "status": "succeeded"}
    )
    assert response.status_code == 409
    response = lease_complete(
        client,
        run1["id"],
        {"lease_id": run2["lease_id"], "status": "succeeded"},
    )
    assert response.status_code == 409

    assert lease_complete(
        client, run2["id"], {"lease_id": run2["lease_id"], "status": "succeeded"}
    ).status_code == 200
    # Already ended.
    response = lease_complete(
        client, run2["id"], {"lease_id": run2["lease_id"], "status": "succeeded"}
    )
    assert response.status_code == 409

    detail = task_detail(client, run1["task_id"])
    assert detail["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None


def test_lease_complete_expired_lease_is_409(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "alpha")
    run = claim_one(client, lease_seconds=1)
    time.sleep(1.05)
    response = lease_complete(
        client,
        run["id"],
        {"lease_id": run["lease_id"], "status": "succeeded"},
    )
    assert response.status_code == 409
    detail = task_detail(client, task["id"])
    assert detail["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None


def test_lease_complete_validation_422_and_zero_writes(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    run = claim_one(client, lease_seconds=60)
    bodies = [
        {},
        {"status": "succeeded"},
        {"lease_id": run["lease_id"], "status": "weird"},
        {"lease_id": run["lease_id"], "status": "failed"},
        {"lease_id": run["lease_id"], "status": "failed", "error": ""},
        {"lease_id": run["lease_id"], "status": "failed", "error": "   "},
        {"lease_id": run["lease_id"], "status": "failed", "error": 7},
        {"lease_id": run["lease_id"], "status": "failed", "error": None},
        {"lease_id": run["lease_id"], "status": "succeeded", "error": "x"},
        {"lease_id": "", "status": "succeeded"},
        {"lease_id": 5, "status": "succeeded"},
        {"lease_id": run["lease_id"], "status": "succeeded", "extra": 1},
    ]
    for body in bodies:
        response = lease_complete(client, run["id"], body)
        assert response.status_code == 422, (body, response.text)
    for raw in (b"", b" ", b"{nope", b"{}"):
        response = lease_complete(client, run["id"], raw=raw)
        assert response.status_code == 422, raw

    response = lease_complete(
        client,
        run["id"],
        {"lease_id": run["lease_id"], "status": "succeeded"},
        query="?x=1",
    )
    assert response.status_code == 422

    detail = task_detail(client, run["task_id"])
    assert detail["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None

    # Unknown path run is 404, ahead of the body checks.
    response = lease_complete(client, 999, {})
    assert response.status_code == 404


def test_lease_complete_racing_legacy_finish_has_single_winner(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "alpha")
    run = claim_one(client, lease_seconds=600)

    barrier = threading.Barrier(2)
    outcomes: list[int] = []
    lock = threading.Lock()

    def via_lease() -> None:
        barrier.wait()
        response = lease_complete(
            client,
            run["id"],
            {"lease_id": run["lease_id"], "status": "succeeded"},
        )
        with lock:
            outcomes.append(response.status_code)

    def via_patch() -> None:
        barrier.wait()
        response = client.patch(
            f"{BASE}/{task['id']}/runs/{run['id']}",
            json={"status": "succeeded"},
        )
        with lock:
            outcomes.append(response.status_code)

    threads = [threading.Thread(target=via_lease), threading.Thread(target=via_patch)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == [200, 409]
    detail = task_detail(client, task["id"])
    assert detail["status"] == "succeeded"
    assert len([r for r in detail["runs"] if r["status"] == "succeeded"]) == 1


# --------------------------------------------------------------------------- #
# Reclaim
# --------------------------------------------------------------------------- #


FUTURE = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def test_reclaim_fails_expired_runs_sorted_by_run_id(client: TestClient) -> None:
    create_dataset_and_version(client)
    tasks = [make_task(client, f"t{i}", max_attempts=3) for i in range(3)]
    runs = [claim_one(client, worker=f"w{i}", lease_seconds=60) for i in range(3)]

    response = reclaim(client, FUTURE)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    ids = [r["id"] for r in body["runs"]]
    assert ids == sorted(ids)
    assert ids == [run["id"] for run in runs]
    for element, run, task in zip(body["runs"], runs, tasks):
        assert set(element) == LEASED_RUN_FIELDS
        assert element["id"] == run["id"]
        assert element["task_id"] == task["id"]
        assert element["status"] == "failed"
        assert element["error"] == "lease expired"
        assert element["finished_at"] is not None
        assert element["lease_id"] == run["lease_id"]
        assert element["worker_id"] == run["worker_id"]
        assert element["lease_expires_at"] == run["lease_expires_at"]
        detail = task_detail(client, task["id"])
        assert detail["status"] == "failed"
        assert detail["attempt_count"] == 1


def test_reclaimed_task_is_claimable_again_until_attempts_exhausted(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "flaky", max_attempts=2)
    run = claim_one(client, lease_seconds=60)
    assert reclaim(client, FUTURE).status_code == 200

    again = claim_one(client, worker="w-2", lease_seconds=60)
    assert again["attempt"] == 2
    assert again["id"] != run["id"]
    assert reclaim(client, FUTURE).status_code == 200

    response = lease_dispatch(
        client, {"worker_id": "w", "lease_seconds": 60, "limit": 5}
    )
    assert response.json()["runs"] == []
    detail = task_detail(client, task["id"])
    assert detail["status"] == "failed"
    assert detail["attempt_count"] == 2
    assert [r["error"] for r in detail["runs"]] == ["lease expired", "lease expired"]


def test_reclaim_skips_unexpired_legacy_and_ended_runs(client: TestClient) -> None:
    create_dataset_and_version(client)
    long_task = make_task(client, "long")
    expired_a = make_task(client, "expired-a", max_attempts=2)
    expired_b = make_task(client, "expired-b")
    ended_task = make_task(client, "ended")
    legacy_task = make_task(client, "legacy")

    long_run = claim_one(client, worker="w-long", lease_seconds=3600)
    run_a = claim_one(client, worker="w-a", lease_seconds=60)
    run_b = claim_one(client, worker="w-b", lease_seconds=60)
    ended_run = claim_one(client, worker="w-end", lease_seconds=3600)
    assert ended_run["task_id"] == ended_task["id"]

    # The legacy flow starts its own run for the one task still pending; it
    # carries no lease row and must be invisible to reclaim.
    response = client.post(f"{BASE}/dispatch", json={"limit": 5})
    assert response.status_code == 201
    assert [run["task_id"] for run in response.json()["runs"]] == [
        legacy_task["id"]
    ]
    legacy_run = response.json()["runs"][0]

    response = lease_complete(
        client,
        ended_run["id"],
        {"lease_id": ended_run["lease_id"], "status": "succeeded"},
    )
    assert response.status_code == 200

    cutoff = (datetime.now(timezone.utc) + timedelta(seconds=100)).isoformat()
    response = reclaim(client, cutoff)
    assert response.status_code == 200
    reclaimed_ids = {element["id"] for element in response.json()["runs"]}
    assert reclaimed_ids == {run_a["id"], run_b["id"]}

    assert long_run["id"] not in reclaimed_ids
    assert legacy_run["id"] not in reclaimed_ids
    assert ended_run["id"] not in reclaimed_ids

    assert task_detail(client, long_task["id"])["status"] == "running"
    legacy_detail = task_detail(client, legacy_task["id"])
    assert legacy_detail["status"] == "running"
    assert legacy_detail["runs"][0]["error"] is None
    assert task_detail(client, ended_task["id"])["status"] == "succeeded"
    assert task_detail(client, expired_a["id"])["status"] == "failed"
    assert task_detail(client, expired_b["id"])["status"] == "failed"


def test_reclaim_boundary_is_inclusive(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    run = claim_one(client, lease_seconds=120)
    expiry = parse_iso(run["lease_expires_at"])

    # One microsecond before the expiry instant: still active.
    response = reclaim(client, (expiry - timedelta(microseconds=1)).isoformat())
    assert response.json()["runs"] == []
    assert task_detail(client, run["task_id"])["status"] == "running"

    # Exactly the expiry instant: expired and reclaimed.
    response = reclaim(client, expiry.isoformat())
    assert [r["id"] for r in response.json()["runs"]] == [run["id"]]


def test_reclaim_is_idempotent_and_changes_nothing_on_repeat(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "alpha", max_attempts=3)
    run = claim_one(client, lease_seconds=60)
    first = reclaim(client, FUTURE)
    assert [r["id"] for r in first.json()["runs"]] == [run["id"]]
    first_finished = task_detail(client, task["id"])["runs"][0]["finished_at"]

    second = reclaim(client, FUTURE)
    assert second.status_code == 200
    assert second.json()["runs"] == []
    detail = task_detail(client, task["id"])
    assert detail["status"] == "failed"
    assert detail["attempt_count"] == 1
    assert detail["runs"][0]["finished_at"] == first_finished

    # An earlier cutoff is an empty no-op as well.
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert reclaim(client, past).json()["runs"] == []


def test_reclaim_validation_422_and_zero_writes(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "alpha")
    claim_one(client, lease_seconds=3600)

    raw_bodies = [
        b"",
        b"   ",
        b"{not json",
        b"[]",
        b'"2026-01-01T00:00:00Z"',
        b"{}",
        b'{"as_of": "2026-01-01T00:00:00"}',  # timezone-less
        b'{"as_of": "not-a-time"}',
        b'{"as_of": 123456}',
        b'{"as_of": null}',
        b'{"as_of": ""}',
        b'{"as_of": "2026-01-01T00:00:00Z", "extra": 1}',
    ]
    for raw in raw_bodies:
        response = reclaim(client, None, raw=raw)
        assert response.status_code == 422, (raw, response.text)
        assert response.json()["error"] == "validation_error"

    response = reclaim(client, FUTURE, query="?x=1")
    assert response.status_code == 422

    # Nothing was reclaimed: the run is still running even with a far-future
    # cutoff once the request is valid... validate first via a pre-expiry
    # cutoff showing the run untouched, then state directly.
    detail = task_detail(client, task["id"])
    assert detail["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None


def test_reclaim_offset_timezone_is_accepted(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    run = claim_one(client, lease_seconds=60)
    # Explicit non-UTC offset two hours in the future covers the 60s lease.
    response = reclaim(client, "2099-01-01T12:00:00+09:00")
    assert response.status_code == 200, response.text
    assert [r["id"] for r in response.json()["runs"]] == [run["id"]]


def test_reclaim_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    response = client.post(
        "/datasets/ghost/versions/1/processing-tasks/reclaim-leases",
        content=b'{"as_of": "2026-01-01T00:00:00Z"}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404
    # 404 precedes the body-shape check.
    response = client.post(
        "/datasets/ghost/versions/1/processing-tasks/reclaim-leases",
        content=b"{}",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404

    create_dataset_and_version(client)
    response = client.post(
        "/datasets/orders/versions/9/processing-tasks/reclaim-leases",
        content=b'{"as_of": "2026-01-01T00:00:00Z"}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404


def test_reclaim_preserves_audit_records_and_snapshot_bindings(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "alpha")
    run = claim_one(client, lease_seconds=60)

    audit = client.post(
        f"{BASE}/{task['id']}/runs/{run['id']}/audit-records",
        json={
            "event": "started",
            "input_summary": "in",
            "result_summary": "out",
        },
    )
    assert audit.status_code == 201, audit.text

    snapshot = client.post(
        "/datasets/orders/versions/1/snapshots", json={"rows": [{"id": 1}]}
    )
    assert snapshot.status_code == 201, snapshot.text
    snapshot_id = snapshot.json()["id"]
    binding = client.post(
        f"{BASE}/{task['id']}/runs/{run['id']}/snapshot-bindings",
        json={"role": "input", "version": 1, "snapshot_id": snapshot_id},
    )
    assert binding.status_code == 201, binding.text

    assert reclaim(client, FUTURE).status_code == 200

    records = client.get(
        f"{BASE}/{task['id']}/runs/{run['id']}/audit-records"
    ).json()
    assert [record["event"] for record in records] == ["started"]
    verify = client.get(
        f"{BASE}/{task['id']}/runs/{run['id']}/audit-records/verify"
    )
    assert verify.json()["valid"] is True

    bindings = client.get(
        f"{BASE}/{task['id']}/runs/{run['id']}/snapshot-bindings"
    ).json()
    assert len(bindings) == 1
    binding_verify = client.get(
        f"{BASE}/{task['id']}/runs/{run['id']}/snapshot-bindings/verify"
    )
    assert binding_verify.status_code == 200
    assert binding_verify.json()["valid"] is True


# --------------------------------------------------------------------------- #
# Interaction with the legacy entry points
# --------------------------------------------------------------------------- #


def test_legacy_endpoints_keep_behavior_and_ignore_leases(client: TestClient) -> None:
    create_dataset_and_version(client)
    leased_task = make_task(client, "leased")
    legacy_task = make_task(client, "legacy")

    # The lease flow claims the first startable task; the legacy dispatch then
    # claims the remaining one with an ordinary, lease-less run.
    leased_run = claim_one(client, lease_seconds=60)
    assert leased_run["task_id"] == leased_task["id"]
    response = client.post(f"{BASE}/dispatch", json={"limit": 5})
    assert response.status_code == 201
    assert [run["task_id"] for run in response.json()["runs"]] == [
        legacy_task["id"]
    ]
    legacy_run = response.json()["runs"][0]
    assert set(legacy_run) == RUN_FIELDS

    response = reclaim(client, FUTURE)
    assert [r["task_id"] for r in response.json()["runs"]] == [leased_task["id"]]
    assert task_detail(client, legacy_task["id"])["status"] == "running"

    # The legacy finish still ends the legacy run normally.
    response = client.patch(
        f"{BASE}/{legacy_task['id']}/runs/{legacy_run['id']}",
        json={"status": "succeeded"},
    )
    assert response.status_code == 200
    assert task_detail(client, legacy_task["id"])["status"] == "succeeded"


def test_leased_runs_survive_restart(tmp_path, monkeypatch) -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "leases.db"

    seed = """
from fastapi.testclient import TestClient
from app.main import app
client = TestClient(app)
assert client.post('/datasets', json={'name': 'orders'}).status_code == 201
r = client.post('/datasets/orders/versions', json={'fields': [
    {'name': 'id', 'type': 'integer', 'nullable': False}]})
assert r.status_code == 201
base = '/datasets/orders/versions/1/processing-tasks'
assert client.post(base, json={'name': 'alpha'}).status_code == 201
r = client.post(base + '/lease-dispatch', json={
    'worker_id': 'w-1', 'lease_seconds': 60})
assert r.status_code == 201, r.text
run = r.json()['runs'][0]
assert run['status'] == 'running'
print(run['id'], run['lease_id'], run['lease_expires_at'])
""".strip()

    env = os.environ.copy()
    env["DATA_LINEAGE_DB"] = str(db_path)
    seeded = subprocess.run(
        [sys.executable, "-c", seed],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert seeded.returncode == 0, seeded.stderr
    run_id, lease_id, expires_at = seeded.stdout.split()

    monkeypatch.setenv("DATA_LINEAGE_DB", str(db_path))
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    # The lease survived: its holder can heartbeat after the restart.
    response = heartbeat(
        client, int(run_id), {"lease_id": lease_id, "lease_seconds": 60}
    )
    assert response.status_code == 200, response.text
    assert parse_iso(response.json()["lease_expires_at"]) > parse_iso(expires_at)
    response = reclaim(
        client,
        (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    assert response.json()["runs"] == []


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_lease_dispatches_never_duplicate_or_skip(client: TestClient) -> None:
    create_dataset_and_version(client)
    task_ids = [make_task(client, f"t{i}")["id"] for i in range(12)]

    thread_count = 6
    barrier = threading.Barrier(thread_count)
    collected: list[dict] = []
    collection_lock = threading.Lock()

    def worker(worker_index: int) -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        local: list[dict] = []
        for _ in range(30):
            response = lease_dispatch(
                thread_client,
                {
                    "worker_id": f"worker-{worker_index}",
                    "lease_seconds": 300,
                    "limit": 3,
                },
            )
            assert response.status_code == 201, response.text
            runs = response.json()["runs"]
            assert len(runs) <= 3
            local.extend(runs)
            if not runs:
                break
        with collection_lock:
            collected.extend(local)

    threads = [
        threading.Thread(target=worker, args=(index,))
        for index in range(thread_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(run["task_id"] for run in collected) == sorted(task_ids)
    assert len({run["id"] for run in collected}) == len(collected)
    assert all(run["attempt"] == 1 for run in collected)
    for task_id in task_ids:
        detail = task_detail(client, task_id)
        assert detail["status"] == "running"
        assert detail["attempt_count"] == 1
        assert len(detail["runs"]) == 1
