"""Tests for cancelling a running processing-task run.

Cancellation is a ``POST .../runs/{run_id}/cancel`` transition that atomically
moves a still-``running`` run (and its task) to ``failed`` with the trimmed
reason as the run error, consuming no extra attempt. These tests cover the
status/error/attempt semantics, validation and routing precedence, retry
behaviour, concurrency against finish/start/dispatch, the audit chain and
report, and persistence across a process restart.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TASKS_PATH = "/datasets/orders/versions/1/processing-tasks"

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


def make_task(client: TestClient, name: str, **overrides) -> dict:
    response = client.post(TASKS_PATH, json={"name": name, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def run_task(client: TestClient, task_id: int) -> dict:
    response = client.post(f"{TASKS_PATH}/{task_id}/runs")
    assert response.status_code == 201, response.text
    return response.json()


def cancel_run(client: TestClient, task_id: int, run_id: int, body=None, **kwargs):
    return client.post(
        f"{TASKS_PATH}/{task_id}/runs/{run_id}/cancel",
        json=body if body is not None else {"reason": "cancelled by operator"},
        **kwargs,
    )


def finish_run(client: TestClient, task_id: int, run_id: int, body: dict):
    return client.patch(
        f"{TASKS_PATH}/{task_id}/runs/{run_id}", json=body
    )


def task_detail(client: TestClient, task_id: int) -> dict:
    response = client.get(f"{TASKS_PATH}/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Basic cancellation semantics
# --------------------------------------------------------------------------- #


def test_cancel_moves_run_and_task_to_failed(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=3)
    run = run_task(client, task["id"])

    response = cancel_run(client, task["id"], run["id"], {"reason": "  stop now  "})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == RUN_FIELDS
    assert body["id"] == run["id"]
    assert body["attempt"] == 1
    assert body["status"] == "failed"
    # The reason is trimmed and stored as the run error.
    assert body["error"] == "stop now"
    assert body["finished_at"] is not None
    datetime.fromisoformat(body["finished_at"])
    assert body["started_at"] == run["started_at"]

    detail = task_detail(client, task["id"])
    assert detail["status"] == "failed"
    # attempt_count is not rolled back and no extra attempt is consumed.
    assert detail["attempt_count"] == 1
    assert len(detail["runs"]) == 1
    assert detail["runs"][0]["status"] == "failed"
    assert detail["runs"][0]["error"] == "stop now"
    assert detail["runs"][0]["finished_at"] == body["finished_at"]


def test_cancel_preserves_internal_whitespace(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=2)
    run = run_task(client, task["id"])
    response = cancel_run(client, task["id"], run["id"], {"reason": "  keep   this  "})
    assert response.status_code == 200
    # Leading/trailing whitespace is trimmed; internal whitespace is preserved.
    assert response.json()["error"] == "keep   this"


# --------------------------------------------------------------------------- #
# Only running runs can be cancelled
# --------------------------------------------------------------------------- #


def test_cancel_succeeded_run_conflicts_and_changes_nothing(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=2)
    run = run_task(client, task["id"])
    finish_run(client, task["id"], run["id"], {"status": "succeeded"})

    before = task_detail(client, task["id"])["runs"][0]
    response = cancel_run(client, task["id"], run["id"], {"reason": "late"})
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    after = task_detail(client, task["id"])["runs"][0]
    assert after == before
    assert after["status"] == "succeeded"
    assert after["error"] is None


def test_cancel_failed_run_conflicts_and_changes_nothing(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=3)
    run = run_task(client, task["id"])
    assert cancel_run(client, task["id"], run["id"], {"reason": "first"}).status_code == 200

    before = task_detail(client, task["id"])["runs"][0]
    response = cancel_run(client, task["id"], run["id"], {"reason": "again"})
    assert response.status_code == 409
    after = task_detail(client, task["id"])["runs"][0]
    assert after == before
    assert after["status"] == "failed"
    assert after["error"] == "first"
    assert after["finished_at"] == before["finished_at"]


# --------------------------------------------------------------------------- #
# Retry after cancellation: continuous attempt numbering
# --------------------------------------------------------------------------- #


def test_task_restarts_after_cancel_with_continuous_attempts(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "flaky", max_attempts=3)

    first = run_task(client, task["id"])
    assert first["attempt"] == 1
    assert cancel_run(client, task["id"], first["id"]).status_code == 200

    # The existing start rule applies: failed + attempts left -> a new run.
    second = run_task(client, task["id"])
    assert second["attempt"] == 2
    assert second["id"] != first["id"]
    assert second["status"] == "running"

    detail = task_detail(client, task["id"])
    assert detail["status"] == "running"
    assert detail["attempt_count"] == 2
    assert [run["attempt"] for run in detail["runs"]] == [1, 2]
    assert len({run["id"] for run in detail["runs"]}) == 2


def test_dispatch_claims_cancelled_task_with_attempts_left(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "flaky", max_attempts=2)
    run = run_task(client, task["id"])
    assert cancel_run(client, task["id"], run["id"]).status_code == 200

    response = client.post(f"{TASKS_PATH}/dispatch", json={"limit": 5})
    assert response.status_code == 201, response.text
    claimed = response.json()["runs"]
    assert [run["task_id"] for run in claimed] == [task["id"]]
    assert claimed[0]["attempt"] == 2
    assert claimed[0]["status"] == "running"

    # Cancelling the last allowed attempt exhausts the task; dispatch skips it.
    assert cancel_run(client, task["id"], claimed[0]["id"]).status_code == 200
    response = client.post(f"{TASKS_PATH}/dispatch", json={"limit": 5})
    assert response.json()["runs"] == []
    detail = task_detail(client, task["id"])
    assert detail["status"] == "failed"
    assert detail["attempt_count"] == 2
    assert client.post(f"{TASKS_PATH}/{task['id']}/runs").status_code == 409


def test_cancelled_single_attempt_task_is_exhausted(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "once")
    run = run_task(client, task["id"])
    assert cancel_run(client, task["id"], run["id"]).status_code == 200
    assert client.post(f"{TASKS_PATH}/{task['id']}/runs").status_code == 409

    schedule = client.get(f"{TASKS_PATH}/schedule")
    assert schedule.status_code == 200
    entry = next(t for t in schedule.json()["tasks"] if t["id"] == task["id"])
    assert entry["schedule_state"] == "exhausted"


# --------------------------------------------------------------------------- #
# Routing precedence: 404 / 422
# --------------------------------------------------------------------------- #


def test_cancel_unknown_resources_are_404(client: TestClient) -> None:
    response = cancel_run(client, 1, 1)
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    run = run_task(client, task["id"])

    assert (
        client.post(
            "/datasets/ghost/versions/1/processing-tasks/"
            f"{task['id']}/runs/{run['id']}/cancel",
            json={"reason": "x"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/datasets/orders/versions/9/processing-tasks/{task['id']}/runs/"
            f"{run['id']}/cancel",
            json={"reason": "x"},
        ).status_code
        == 404
    )
    assert cancel_run(client, 999, run["id"]).status_code == 404
    assert cancel_run(client, task["id"], 999).status_code == 404


def test_cancel_run_of_other_task_is_422(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_task(client, "extract")
    second = make_task(client, "load")
    run = run_task(client, first["id"])

    response = cancel_run(client, second["id"], run["id"], {"reason": "x"})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    # The run is untouched and still cancellable by its own task.
    detail = task_detail(client, first["id"])
    assert detail["runs"][0]["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None
    assert cancel_run(client, first["id"], run["id"]).status_code == 200


def test_cancel_run_of_other_version_is_422(client: TestClient) -> None:
    create_dataset_and_version(client)
    task_v1 = make_task(client, "ingest")
    run = run_task(client, task_v1["id"])
    client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    task_v2 = client.post(
        "/datasets/orders/versions/2/processing-tasks", json={"name": "ingest"}
    ).json()
    response = client.post(
        f"/datasets/orders/versions/2/processing-tasks/{task_v2['id']}"
        f"/runs/{run['id']}/cancel",
        json={"reason": "x"},
    )
    assert response.status_code == 422
    detail = task_detail(client, task_v1["id"])
    assert detail["runs"][0]["status"] == "running"


# --------------------------------------------------------------------------- #
# Body and query validation (stable 422, nothing written)
# --------------------------------------------------------------------------- #


def test_cancel_rejects_invalid_bodies_without_writing(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=2)
    run = run_task(client, task["id"])
    url = f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/cancel"

    for body in (
        {},
        {"reason": ""},
        {"reason": "   "},
        {"reason": "\t\n"},
        {"reason": 5},
        {"reason": True},
        {"reason": None},
        {"reason": ["stop"]},
        {"why": "missing reason key"},
        {"reason": "x", "extra": 1},
    ):
        response = client.post(url, json=body)
        assert response.status_code == 422, (body, response.text)
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"

    # Nothing was written: the run is still running with no finish time.
    detail = task_detail(client, task["id"])
    assert detail["status"] == "running"
    assert detail["attempt_count"] == 1
    assert detail["runs"][0]["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None
    assert detail["runs"][0]["error"] is None

    # A valid cancellation still succeeds after all the rejected attempts.
    response = client.post(url, json={"reason": "now"})
    assert response.status_code == 200, response.text


def test_cancel_rejects_empty_and_malformed_bodies(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=2)
    run = run_task(client, task["id"])
    url = f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/cancel"

    response = client.post(url, content=b"", headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"

    response = client.post(
        url, content=b"{not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert isinstance(body["detail"], str) and body["detail"]

    detail = task_detail(client, task["id"])
    assert detail["runs"][0]["status"] == "running"


def test_cancel_rejects_query_parameters(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=2)
    run = run_task(client, task["id"])

    response = client.post(
        f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/cancel?foo=bar",
        json={"reason": "x"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    detail = task_detail(client, task["id"])
    assert detail["runs"][0]["status"] == "running"

    # 404 precedence: an unknown path still wins over the query-parameter 422.
    assert (
        client.post(
            "/datasets/ghost/versions/1/processing-tasks/1/runs/1/cancel?foo=bar",
            json={"reason": "x"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/datasets/orders/versions/9/processing-tasks/1/runs/1/cancel?foo=bar",
            json={"reason": "x"},
        ).status_code
        == 404
    )
    # An unknown path task is likewise 404 even though a query parameter is
    # also present (path resources resolve before parameter rejection).
    assert (
        client.post(
            f"{TASKS_PATH}/999/runs/1/cancel?foo=bar",
            json={"reason": "x"},
        ).status_code
        == 404
    )


# --------------------------------------------------------------------------- #
# Audit records and the audit report stay consistent after cancellation
# --------------------------------------------------------------------------- #


def test_audit_records_after_cancel_record_real_run_status(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=2)
    run = run_task(client, task["id"])
    audit_path = (
        f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/audit-records"
    )

    before = client.post(
        audit_path,
        json={"event": "e1", "input_summary": "in", "result_summary": "running"},
    )
    assert before.status_code == 201
    assert before.json()["run_status"] == "running"

    assert cancel_run(client, task["id"], run["id"]).status_code == 200

    # Audit records remain appendable after cancellation; run_status is the
    # status at write time, i.e. failed.
    after = client.post(
        audit_path,
        json={"event": "e2", "input_summary": "in", "result_summary": "stopped"},
    )
    assert after.status_code == 201, after.text
    assert after.json()["run_status"] == "failed"
    assert after.json()["sequence"] == 2
    assert after.json()["previous_hash"] == before.json()["evidence_hash"]

    records = client.get(audit_path)
    assert [r["run_status"] for r in records.json()] == ["running", "failed"]

    verify = client.get(f"{audit_path}/verify")
    assert verify.status_code == 200
    assert verify.json()["valid"] is True
    assert verify.json()["checked_count"] == 2


def test_audit_report_reflects_cancellation(client: TestClient) -> None:
    create_dataset_and_version(client)
    cancelled = make_task(client, "cancelled", max_attempts=2)
    exhausted = make_task(client, "exhausted")
    succeeded = make_task(client, "succeeded")

    run = run_task(client, cancelled["id"])
    cancel_run(client, cancelled["id"], run["id"], {"reason": "stop"})

    run = run_task(client, exhausted["id"])
    cancel_run(client, exhausted["id"], run["id"], {"reason": "stop"})

    run = run_task(client, succeeded["id"])
    finish_run(client, succeeded["id"], run["id"], {"status": "succeeded"})

    report = client.get(f"{TASKS_PATH}/audit-report")
    assert report.status_code == 200, report.text
    summary = report.json()["summary"]
    assert summary["task_count"] == 3
    assert summary["run_count"] == 3
    assert summary["succeeded_tasks"] == 1
    assert summary["failed_tasks"] == 2
    assert summary["running_tasks"] == 0
    assert summary["exhausted_tasks"] == 1
    assert summary["invalid_audit_runs"] == 0

    by_id = {task["id"]: task for task in report.json()["tasks"]}
    cancelled_runs = by_id[cancelled["id"]]["runs"]
    assert cancelled_runs[0]["status"] == "failed"
    assert cancelled_runs[0]["error"] == "stop"
    assert cancelled_runs[0]["finished_at"] is not None
    assert cancelled_runs[0]["proof"]["valid"] is True
    assert cancelled_runs[0]["proof"]["checked_count"] == 0


def test_audit_report_rejects_body_and_query_params(client: TestClient) -> None:
    create_dataset_and_version(client)
    path = f"{TASKS_PATH}/audit-report"
    assert client.get(path, params={"x": 1}).status_code == 422
    assert (
        client.request(
            "GET", path, content=b"{}",
            headers={"content-type": "application/json"},
        ).status_code
        == 422
    )


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_cancel_and_finish_have_one_winner(client: TestClient) -> None:
    create_dataset_and_version(client)
    tasks = [make_task(client, f"task-{i}", max_attempts=4) for i in range(8)]
    for task in tasks:
        run_task(client, task["id"])

    def race(task: dict, action: str, outcomes: list, lock: threading.Lock) -> None:
        thread_client = TestClient(client.app)
        detail = thread_client.get(f"{TASKS_PATH}/{task['id']}").json()
        run_id = detail["runs"][0]["id"]
        if action == "cancel":
            response = thread_client.post(
                f"{TASKS_PATH}/{task['id']}/runs/{run_id}/cancel",
                json={"reason": "stop"},
            )
        else:
            response = thread_client.patch(
                f"{TASKS_PATH}/{task['id']}/runs/{run_id}",
                json={"status": "succeeded"},
            )
        with lock:
            outcomes.append((task["id"], action, response.status_code))

    for index, task in enumerate(tasks):
        outcomes: list = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def canceller(task=task):
            barrier.wait()
            race(task, "cancel", outcomes, lock)

        def finisher(task=task):
            barrier.wait()
            race(task, "finisher", outcomes, lock)

        threads = [threading.Thread(target=canceller), threading.Thread(target=finisher)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        statuses = sorted(code for _tid, _action, code in outcomes)
        # Exactly one transition commits; the loser sees a 409, never a 5xx and
        # never a second success.
        assert statuses == [200, 409], outcomes

        detail = task_detail(client, task["id"])
        run = detail["runs"][0]
        # No half-written terminal state: task and run agree and the run has a
        # finish time.
        assert run["status"] == detail["status"]
        assert run["status"] in ("succeeded", "failed")
        assert run["finished_at"] is not None
        assert len(detail["runs"]) == 1
        if run["status"] == "failed":
            assert run["error"] == "stop"
        else:
            assert run["error"] is None


def test_concurrent_cancel_start_dispatch_keep_attempts_consistent(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task_ids = [
        make_task(client, f"task-{i}", max_attempts=5)["id"] for i in range(6)
    ]
    thread_count = 6
    barrier = threading.Barrier(thread_count)
    errors: list[AssertionError] = []

    def worker(worker_id: int) -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        try:
            for turn in range(60):
                task_id = task_ids[(turn + worker_id) % len(task_ids)]
                detail = thread_client.get(f"{TASKS_PATH}/{task_id}")
                assert detail.status_code == 200, detail.text
                task = detail.json()
                running = next(
                    (r for r in task["runs"] if r["status"] == "running"), None
                )
                if running is not None:
                    if (turn + worker_id) % 2 == 0:
                        response = thread_client.post(
                            f"{TASKS_PATH}/{task_id}/runs/{running['id']}/cancel",
                            json={"reason": "stop"},
                        )
                        assert response.status_code in (200, 409), response.text
                    else:
                        response = thread_client.patch(
                            f"{TASKS_PATH}/{task_id}/runs/{running['id']}",
                            json={"status": "failed", "error": "boom"},
                        )
                        assert response.status_code in (200, 409), response.text
                elif task["status"] in ("pending", "failed") and task[
                    "attempt_count"
                ] < task["max_attempts"]:
                    if turn % 4 == 0:
                        response = thread_client.post(f"{TASKS_PATH}/dispatch", json={"limit": 2})
                        assert response.status_code == 201, response.text
                    else:
                        response = thread_client.post(f"{TASKS_PATH}/{task_id}/runs")
                        assert response.status_code in (201, 409), response.text
        except AssertionError as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors

    # Settle any run still running so the final-state invariants are terminal.
    settle_client = TestClient(client.app)
    for task_id in task_ids:
        while True:
            task = settle_client.get(f"{TASKS_PATH}/{task_id}").json()
            running = next(
                (r for r in task["runs"] if r["status"] == "running"), None
            )
            if running is None:
                break
            response = settle_client.post(
                f"{TASKS_PATH}/{task_id}/runs/{running['id']}/cancel",
                json={"reason": "settle"},
            )
            assert response.status_code in (200, 409), response.text

    for task_id in task_ids:
        task = task_detail(client, task_id)
        attempts = [run["attempt"] for run in task["runs"]]
        # Attempts are continuous: cancellation never duplicates or skips a
        # number, and attempt_count tracks the run count.
        assert attempts == list(range(1, len(attempts) + 1)), attempts
        assert task["attempt_count"] == len(task["runs"])
        assert len({run["id"] for run in task["runs"]}) == len(task["runs"])
        running_runs = [r for r in task["runs"] if r["status"] == "running"]
        assert len(running_runs) == 0
        last_run = task["runs"][-1]
        assert task["status"] == last_run["status"]
        assert last_run["finished_at"] is not None


# --------------------------------------------------------------------------- #
# Cross-process race and persistence across restart
# --------------------------------------------------------------------------- #


def _run_isolated(db_path: Path, script: str, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATA_LINEAGE_DB"] = str(db_path)
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


SEED_RUNNING_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
assert client.post("/datasets", json={"name": "orders"}).status_code == 201
r = client.post(
    "/datasets/orders/versions",
    json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
)
assert r.status_code == 201, r.text
base = "/datasets/orders/versions/1/processing-tasks"
task = client.post(base, json={"name": "ingest", "max_attempts": 3}).json()
run = client.post(f"{base}/{task['id']}/runs").json()
print(run["id"])
"""

CANCEL_WORKER_SCRIPT = """
import sys
from fastapi.testclient import TestClient
from app.main import app

out = sys.argv[1]
client = TestClient(app)
base = "/datasets/orders/versions/1/processing-tasks"
run = client.get(f"{base}/1").json()["runs"][0]
response = client.post(f"{base}/1/runs/{run['id']}/cancel", json={"reason": "stop"})
open(out, "w").write(str(response.status_code))
"""

FINISH_WORKER_SCRIPT = """
import sys
from fastapi.testclient import TestClient
from app.main import app

out = sys.argv[1]
client = TestClient(app)
base = "/datasets/orders/versions/1/processing-tasks"
run = client.get(f"{base}/1").json()["runs"][0]
response = client.patch(
    f"{base}/1/runs/{run['id']}", json={"status": "succeeded"}
)
open(out, "w").write(str(response.status_code))
"""

VERIFY_FINAL_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
base = "/datasets/orders/versions/1/processing-tasks"
detail = client.get(f"{base}/1").json()
assert detail["attempt_count"] == 1
run = detail["runs"][0]
assert run["status"] in ("succeeded", "failed"), run
assert run["finished_at"] is not None
assert detail["status"] == run["status"]
if run["status"] == "failed":
    assert run["error"] == "stop"
else:
    assert run["error"] is None
print("consistent")
"""


def test_cancel_and_finish_race_across_processes(tmp_path: Path) -> None:
    db_path = tmp_path / "cancel-race.db"
    seeded = _run_isolated(db_path, SEED_RUNNING_SCRIPT)
    assert seeded.returncode == 0, seeded.stderr

    env = os.environ.copy()
    env["DATA_LINEAGE_DB"] = str(db_path)
    cancel_out = tmp_path / "cancel.status"
    finish_out = tmp_path / "finish.status"
    common = dict(cwd=PROJECT_ROOT, env=env, stdout=subprocess.PIPE,
                  stderr=subprocess.PIPE, text=True)
    cancel_proc = subprocess.Popen(
        [sys.executable, "-c", CANCEL_WORKER_SCRIPT, str(cancel_out)], **common
    )
    finish_proc = subprocess.Popen(
        [sys.executable, "-c", FINISH_WORKER_SCRIPT, str(finish_out)], **common
    )
    for proc in (cancel_proc, finish_proc):
        stderr = proc.stderr.read()
        assert proc.wait() == 0, stderr

    statuses = sorted(
        int(path.read_text().strip()) for path in (cancel_out, finish_out)
    )
    # SQLite serializes the two IMMEDIATE transactions: exactly one transition
    # commits, the other receives 409.
    assert statuses == [200, 409], statuses

    verify = _run_isolated(db_path, VERIFY_FINAL_SCRIPT)
    assert verify.returncode == 0, verify.stderr
    assert verify.stdout.strip() == "consistent"


PERSIST_CANCEL_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
assert client.post("/datasets", json={"name": "orders"}).status_code == 201
r = client.post(
    "/datasets/orders/versions",
    json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
)
assert r.status_code == 201, r.text
base = "/datasets/orders/versions/1/processing-tasks"
task = client.post(base, json={"name": "ingest", "max_attempts": 2}).json()
run = client.post(f"{base}/{task['id']}/runs").json()
r = client.post(
    f"{base}/{task['id']}/runs/{run['id']}/cancel",
    json={"reason": "operator abort"},
)
assert r.status_code == 200, r.text
assert r.json()["status"] == "failed"
print("cancelled")
"""

PERSIST_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
base = "/datasets/orders/versions/1/processing-tasks"
detail = client.get(f"{base}/1").json()
assert detail["status"] == "failed"
assert detail["attempt_count"] == 1
run = detail["runs"][0]
assert run["status"] == "failed"
assert run["error"] == "operator abort"
assert run["finished_at"] is not None
assert run["attempt"] == 1

# Attempt 2 is still available after the restart and numbers continuously.
started = client.post(f"{base}/1/runs")
assert started.status_code == 201, started.text
second = started.json()
assert second["attempt"] == 2
assert second["status"] == "running"
detail = client.get(f"{base}/1").json()
assert [r["attempt"] for r in detail["runs"]] == [1, 2]
assert detail["attempt_count"] == 2
print("verified")
"""


def test_cancelled_run_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "cancel-persist.db"
    created = _run_isolated(db_path, PERSIST_CANCEL_SCRIPT)
    assert created.returncode == 0, created.stderr
    assert created.stdout.strip() == "cancelled"
    verified = _run_isolated(db_path, PERSIST_VERIFY_SCRIPT)
    assert verified.returncode == 0, verified.stderr
    assert verified.stdout.strip() == "verified"
