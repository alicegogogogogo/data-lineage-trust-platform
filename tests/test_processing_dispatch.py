"""Tests for batch dispatch of processing-task runs (worker claim endpoint)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TASKS_PATH = "/datasets/orders/versions/1/processing-tasks"
DISPATCH_PATH = f"{TASKS_PATH}/dispatch"

RUN_FIELDS = {
    "id",
    "task_id",
    "attempt",
    "status",
    "started_at",
    "finished_at",
    "error",
}


def create_dataset_and_version(client: TestClient, dataset: str = "orders") -> None:
    response = client.post("/datasets", json={"name": dataset})
    assert response.status_code == 201, response.text
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201, response.text


def make_task(client: TestClient, name: str, **overrides):
    response = client.post(TASKS_PATH, json={"name": name, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def run_task(client: TestClient, task_id: int):
    response = client.post(f"{TASKS_PATH}/{task_id}/runs")
    assert response.status_code == 201, response.text
    return response.json()


def finish_run(client: TestClient, task_id: int, run_id: int, body: dict):
    response = client.patch(f"{TASKS_PATH}/{task_id}/runs/{run_id}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def dispatch(client: TestClient, body: dict | None = {}, path: str = DISPATCH_PATH):
    if body is None:
        return client.post(path)
    return client.post(path, json=body)


def task_detail(client: TestClient, task_id: int) -> dict:
    response = client.get(f"{TASKS_PATH}/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Selection and response shape
# --------------------------------------------------------------------------- #


def test_dispatch_default_limit_starts_single_task(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_task(client, "alpha")
    second = make_task(client, "beta")

    response = dispatch(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert len(body["runs"]) == 1

    run = body["runs"][0]
    assert set(run) == RUN_FIELDS
    assert run["task_id"] == first["id"]
    assert run["attempt"] == 1
    assert run["status"] == "running"
    assert run["finished_at"] is None
    assert run["error"] is None
    datetime.fromisoformat(run["started_at"])

    assert task_detail(client, first["id"])["status"] == "running"
    assert task_detail(client, first["id"])["attempt_count"] == 1
    assert task_detail(client, second["id"])["status"] == "pending"
    assert task_detail(client, second["id"])["attempt_count"] == 0


def test_dispatch_without_body_uses_default_limit(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    make_task(client, "beta")
    response = dispatch(client, body=None)
    assert response.status_code == 201, response.text
    assert len(response.json()["runs"]) == 1


def test_dispatch_limit_selects_tasks_in_id_order(client: TestClient) -> None:
    create_dataset_and_version(client)
    tasks = [make_task(client, f"task-{index}") for index in range(4)]

    body = dispatch(client, {"limit": 3}).json()
    assert [run["task_id"] for run in body["runs"]] == [
        task["id"] for task in tasks[:3]
    ]
    assert [run["attempt"] for run in body["runs"]] == [1, 1, 1]
    assert task_detail(client, tasks[3]["id"])["status"] == "pending"


def test_dispatch_limit_larger_than_startable_count(client: TestClient) -> None:
    create_dataset_and_version(client)
    tasks = [make_task(client, f"task-{index}") for index in range(2)]
    body = dispatch(client, {"limit": 10}).json()
    assert [run["task_id"] for run in body["runs"]] == [
        task["id"] for task in tasks
    ]


def test_dispatch_with_no_startable_tasks_returns_empty_runs(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    body = dispatch(client, {"limit": 5}).json()
    assert body == {"dataset": "orders", "version": 1, "runs": []}


def test_dispatch_skips_running_succeeded_and_exhausted_tasks(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    running = make_task(client, "running", max_attempts=2)
    run_task(client, running["id"])
    succeeded = make_task(client, "succeeded")
    finish_run(
        client,
        succeeded["id"],
        run_task(client, succeeded["id"])["id"],
        {"status": "succeeded"},
    )
    exhausted = make_task(client, "exhausted", max_attempts=1)
    finish_run(
        client,
        exhausted["id"],
        run_task(client, exhausted["id"])["id"],
        {"status": "failed", "error": "boom"},
    )
    pending = make_task(client, "pending")

    body = dispatch(client, {"limit": 10}).json()
    assert [run["task_id"] for run in body["runs"]] == [pending["id"]]
    # The skipped tasks are untouched.
    assert task_detail(client, running["id"])["attempt_count"] == 1
    assert len(task_detail(client, running["id"])["runs"]) == 1
    assert task_detail(client, succeeded["id"])["status"] == "succeeded"
    assert task_detail(client, exhausted["id"])["status"] == "failed"


def test_dispatch_retries_failed_task_with_remaining_attempts(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "flaky", max_attempts=3)
    first = run_task(client, task["id"])
    finish_run(client, task["id"], first["id"], {"status": "failed", "error": "x"})

    body = dispatch(client).json()
    assert len(body["runs"]) == 1
    run = body["runs"][0]
    assert run["task_id"] == task["id"]
    assert run["attempt"] == 2
    assert run["status"] == "running"

    detail = task_detail(client, task["id"])
    assert detail["status"] == "running"
    assert detail["attempt_count"] == 2
    assert [r["attempt"] for r in detail["runs"]] == [1, 2]


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #


def test_dispatch_respects_dependencies(client: TestClient) -> None:
    create_dataset_and_version(client)
    extract = make_task(client, "extract")
    load = make_task(client, "load", depends_on=[extract["id"]])

    # The dependency has not succeeded, so only it can start.
    body = dispatch(client, {"limit": 10}).json()
    assert [run["task_id"] for run in body["runs"]] == [extract["id"]]

    # A task started by this dispatch does not unblock its dependent.
    body = dispatch(client, {"limit": 10}).json()
    assert body["runs"] == []

    finish_run(
        client,
        extract["id"],
        task_detail(client, extract["id"])["runs"][0]["id"],
        {"status": "succeeded"},
    )
    body = dispatch(client, {"limit": 10}).json()
    assert [run["task_id"] for run in body["runs"]] == [load["id"]]


def test_dispatch_skips_task_whose_dependency_failed(client: TestClient) -> None:
    create_dataset_and_version(client)
    extract = make_task(client, "extract", max_attempts=2)
    load = make_task(client, "load", depends_on=[extract["id"]])
    run = run_task(client, extract["id"])
    finish_run(client, extract["id"], run["id"], {"status": "failed", "error": "x"})

    # The failed dependency is retryable and starts first; the dependent stays
    # blocked because its dependency has not succeeded.
    body = dispatch(client, {"limit": 10}).json()
    assert [run["task_id"] for run in body["runs"]] == [extract["id"]]
    assert task_detail(client, load["id"])["status"] == "pending"
    assert task_detail(client, load["id"])["runs"] == []


def test_dispatch_started_task_does_not_unblock_dependent_in_same_request(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    first = make_task(client, "first")
    second = make_task(client, "second", depends_on=[first["id"]])
    third = make_task(client, "third", depends_on=[second["id"]])

    body = dispatch(client, {"limit": 3}).json()
    assert [run["task_id"] for run in body["runs"]] == [first["id"]]


# --------------------------------------------------------------------------- #
# Validation and unknown resources
# --------------------------------------------------------------------------- #


def test_dispatch_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    response = dispatch(client, {"limit": 1})
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    create_dataset_and_version(client)
    response = dispatch(
        client, {"limit": 1}, path="/datasets/orders/versions/9/processing-tasks/dispatch"
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_dispatch_rejects_invalid_limit_and_writes_nothing(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    for value in (0, -1, 1.5, "2", True, None):
        response = dispatch(client, {"limit": value})
        assert response.status_code == 422, (value, response.text)
        assert response.json()["error"] == "validation_error"
    detail = task_detail(client, task["id"])
    assert detail["status"] == "pending"
    assert detail["attempt_count"] == 0
    assert detail["runs"] == []


def test_dispatch_rejects_extra_fields_and_writes_nothing(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    response = dispatch(client, {"limit": 1, "worker": "w-1"})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert task_detail(client, task["id"])["runs"] == []


def test_dispatch_rejects_non_object_body(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "ingest")
    for body in ([1], "limit", 3):
        response = client.post(DISPATCH_PATH, json=body)
        assert response.status_code == 422, (body, response.text)


def test_dispatch_error_shape_is_safe_json(client: TestClient) -> None:
    create_dataset_and_version(client)
    response = dispatch(client, {"limit": 0})
    body = response.json()
    assert set(body) == {"error", "detail"}
    detail = body["detail"].lower()
    for leaked in ("traceback", "sql", "sqlite", "select ", "insert "):
        assert leaked not in detail


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def _concurrent_dispatch_and_single_starts(
    client: TestClient, task_ids: list[int], dispatch_workers: int
) -> tuple[list[dict], list[int]]:
    """Fire concurrent dispatches and single-task starts; collect results."""
    runs: list[dict] = []
    single_statuses: list[int] = []
    failures: list[Exception] = []
    barrier = threading.Barrier(dispatch_workers + len(task_ids))

    def dispatch_worker() -> None:
        local = TestClient(client.app)
        barrier.wait()
        try:
            response = local.post(DISPATCH_PATH, json={"limit": 1})
            assert response.status_code == 201, response.text
            runs.extend(response.json()["runs"])
        except Exception as exc:  # pragma: no cover - reported below
            failures.append(exc)

    def single_worker(task_id: int) -> None:
        local = TestClient(client.app)
        barrier.wait()
        try:
            response = local.post(f"{TASKS_PATH}/{task_id}/runs")
            assert response.status_code in (201, 409), response.text
            single_statuses.append(response.status_code)
        except Exception as exc:  # pragma: no cover - reported below
            failures.append(exc)

    threads = [threading.Thread(target=dispatch_worker) for _ in range(dispatch_workers)]
    threads += [
        threading.Thread(target=single_worker, args=(task_id,))
        for task_id in task_ids
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures, failures
    return runs, single_statuses


def test_concurrent_dispatches_never_duplicate_attempts(client: TestClient) -> None:
    create_dataset_and_version(client)
    tasks = [make_task(client, f"task-{index}") for index in range(4)]

    runs, _ = _concurrent_dispatch_and_single_starts(
        client, task_ids=[], dispatch_workers=12
    )

    # Every startable task was claimed exactly once and no task was claimed
    # twice, so attempts are unique per task and all runs are running.
    assert len(runs) == len(tasks)
    assert {run["task_id"] for run in runs} == {task["id"] for task in tasks}
    for task in tasks:
        detail = task_detail(client, task["id"])
        assert detail["status"] == "running"
        assert detail["attempt_count"] == 1
        assert len(detail["runs"]) == 1
        assert detail["runs"][0]["attempt"] == 1
        assert detail["runs"][0]["status"] == "running"


def test_concurrent_dispatch_and_single_start_do_not_double_start(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    tasks = [make_task(client, f"task-{index}") for index in range(3)]

    runs, single_statuses = _concurrent_dispatch_and_single_starts(
        client,
        task_ids=[task["id"] for task in tasks],
        dispatch_workers=6,
    )

    # Each task was started exactly once across both interfaces: exactly one
    # running run each, attempt 1, and no duplicate attempts.
    started_by_dispatch = {run["task_id"] for run in runs}
    assert len(runs) == len(started_by_dispatch)
    assert single_statuses.count(201) + len(runs) == len(tasks)
    for task in tasks:
        detail = task_detail(client, task["id"])
        assert detail["attempt_count"] == 1
        assert [run["attempt"] for run in detail["runs"]] == [1]
        assert detail["runs"][0]["status"] == "running"


def test_dispatch_response_never_exceeds_limit_under_concurrency(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    for index in range(6):
        make_task(client, f"task-{index}")

    lengths: list[int] = []
    failures: list[Exception] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        local = TestClient(client.app)
        barrier.wait()
        try:
            response = local.post(DISPATCH_PATH, json={"limit": 2})
            assert response.status_code == 201, response.text
            lengths.append(len(response.json()["runs"]))
        except Exception as exc:  # pragma: no cover - reported below
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures, failures
    assert all(length <= 2 for length in lengths)
    assert sum(lengths) == 6


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #

CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response, status=201):
    assert response.status_code == status, response.text
    return response.json()

ok(client.post("/datasets", json={"name": "orders"}))
ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
))
base = "/datasets/orders/versions/1/processing-tasks"
ok(client.post(base, json={"name": "extract"}))
ok(client.post(base, json={"name": "load"}))
result = ok(client.post(f"{base}/dispatch", json={"limit": 1}))
assert len(result["runs"]) == 1
print("created")
"""

VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
base = "/datasets/orders/versions/1/processing-tasks"

tasks = client.get(base)
assert tasks.status_code == 200, tasks.text
by_name = {task["name"]: task for task in tasks.json()}
assert by_name["extract"]["status"] == "running"
assert by_name["extract"]["attempt_count"] == 1
assert by_name["load"]["status"] == "pending"

# The already claimed task is not handed out again after the restart; the
# remaining pending task is dispatched instead.
result = client.post(f"{base}/dispatch", json={"limit": 5})
assert result.status_code == 201, result.text
runs = result.json()["runs"]
assert [run["task_id"] for run in runs] == [by_name["load"]["id"]]
assert runs[0]["attempt"] == 1
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


def test_dispatched_runs_survive_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-dispatch.db"
    assert _run_script(db_path, CREATE_SCRIPT) == "created"
    assert db_path.exists()
    assert _run_script(db_path, VERIFY_SCRIPT) == "verified"
