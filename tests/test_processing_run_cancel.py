"""Tests for cancelling a running processing-task run.

POST .../processing-tasks/{task_id}/runs/{run_id}/cancel
"""

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


def make_task(client: TestClient, name: str, **overrides) -> dict:
    response = client.post(TASKS_PATH, json={"name": name, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def run_task(client: TestClient, task_id: int) -> dict:
    response = client.post(f"{TASKS_PATH}/{task_id}/runs")
    assert response.status_code == 201, response.text
    return response.json()


def task_detail(client: TestClient, task_id: int) -> dict:
    response = client.get(f"{TASKS_PATH}/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Successful cancellation
# --------------------------------------------------------------------------- #


def test_cancel_running_run_marks_run_and_task_failed(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=3)
    run = run_task(client, task["id"])

    response = client.post(
        f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/cancel",
        json={"reason": "operator abort"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == RUN_FIELDS
    assert body["id"] == run["id"]
    assert body["task_id"] == task["id"]
    assert body["attempt"] == 1
    assert body["status"] == "failed"
    assert body["error"] == "operator abort"
    assert body["finished_at"] is not None
    datetime.fromisoformat(body["finished_at"])
    assert datetime.fromisoformat(body["finished_at"]) >= datetime.fromisoformat(
        body["started_at"]
    )

    detail = task_detail(client, task["id"])
    # The task is failed but the consumed attempt is not rolled back.
    assert detail["status"] == "failed"
    assert detail["attempt_count"] == 1
    assert len(detail["runs"]) == 1
    assert detail["runs"][0]["status"] == "failed"
    assert detail["runs"][0]["finished_at"] == body["finished_at"]


def test_cancel_trims_reason_before_storing(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    run = run_task(client, task["id"])
    response = client.post(
        f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/cancel",
        json={"reason": "  stop it now \t"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["error"] == "stop it now"


def test_canceled_task_can_start_again_with_continuous_attempts(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "flaky", max_attempts=3)

    first = run_task(client, task["id"])
    assert first["attempt"] == 1
    response = client.post(
        f"{TASKS_PATH}/{task['id']}/runs/{first['id']}/cancel",
        json={"reason": "cancel 1"},
    )
    assert response.status_code == 200

    second = run_task(client, task["id"])
    assert second["attempt"] == 2
    response = client.post(
        f"{TASKS_PATH}/{task['id']}/runs/{second['id']}/cancel",
        json={"reason": "cancel 2"},
    )
    assert response.status_code == 200
    detail = task_detail(client, task["id"])
    assert detail["status"] == "failed"
    assert detail["attempt_count"] == 2

    # The last remaining attempt is still handed out continuously.
    third = run_task(client, task["id"])
    assert third["attempt"] == 3
    response = client.post(
        f"{TASKS_PATH}/{task['id']}/runs/{third['id']}/cancel",
        json={"reason": "cancel 3"},
    )
    assert response.status_code == 200

    # Cancelling the final attempt exhausts the task: no further run starts.
    response = client.post(f"{TASKS_PATH}/{task['id']}/runs")
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    # No duplicate or skipped attempt numbers were introduced.
    detail = task_detail(client, task["id"])
    assert detail["status"] == "failed"
    assert detail["attempt_count"] == 3
    assert [run["attempt"] for run in detail["runs"]] == [1, 2, 3]


def test_canceled_run_is_selectable_by_dispatch_when_attempts_remain(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "flaky", max_attempts=2)
    run = run_task(client, task["id"])
    response = client.post(
        f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/cancel",
        json={"reason": "stop"},
    )
    assert response.status_code == 200

    response = client.post(DISPATCH_PATH, json={"limit": 5})
    assert response.status_code == 201, response.text
    runs = response.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["task_id"] == task["id"]
    assert runs[0]["attempt"] == 2
    detail = task_detail(client, task["id"])
    assert detail["status"] == "running"
    assert [r["attempt"] for r in detail["runs"]] == [1, 2]


# --------------------------------------------------------------------------- #
# Already-finished runs
# --------------------------------------------------------------------------- #


def test_cancel_succeeded_run_conflicts_and_changes_nothing(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=2)
    run = run_task(client, task["id"])
    finish = client.patch(
        f"{TASKS_PATH}/{task['id']}/runs/{run['id']}",
        json={"status": "succeeded"},
    )
    assert finish.status_code == 200
    before = task_detail(client, task["id"])

    response = client.post(
        f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/cancel",
        json={"reason": "too late"},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"

    after = task_detail(client, task["id"])
    assert after == before
    assert after["status"] == "succeeded"
    assert after["runs"][0]["status"] == "succeeded"
    assert after["runs"][0]["error"] is None
    assert after["runs"][0]["finished_at"] == before["runs"][0]["finished_at"]


def test_cancel_failed_run_conflicts_and_changes_nothing(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=3)
    run = run_task(client, task["id"])
    failed = client.patch(
        f"{TASKS_PATH}/{task['id']}/runs/{run['id']}",
        json={"status": "failed", "error": "disk full"},
    )
    assert failed.status_code == 200
    before = task_detail(client, task["id"])

    response = client.post(
        f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/cancel",
        json={"reason": "too late"},
    )
    assert response.status_code == 409
    after = task_detail(client, task["id"])
    assert after == before
    assert after["runs"][0]["error"] == "disk full"


def test_cancel_twice_only_first_succeeds(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=2)
    run = run_task(client, task["id"])
    first = client.post(
        f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/cancel",
        json={"reason": "first"},
    )
    assert first.status_code == 200
    before = task_detail(client, task["id"])

    second = client.post(
        f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/cancel",
        json={"reason": "second"},
    )
    assert second.status_code == 409
    assert second.json()["error"] == "conflict"
    after = task_detail(client, task["id"])
    assert after == before
    assert after["runs"][0]["error"] == "first"


# --------------------------------------------------------------------------- #
# Path resolution: 404 / 422
# --------------------------------------------------------------------------- #


def test_cancel_unknown_dataset_version_task_or_run_is_404(
    client: TestClient,
) -> None:
    body = {"reason": "stop"}
    assert (
        client.post(
            "/datasets/ghost/versions/1/processing-tasks/1/runs/1/cancel", json=body
        ).status_code
        == 404
    )

    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    run = run_task(client, task["id"])
    url = f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/cancel"

    assert (
        client.post(
            f"/datasets/orders/versions/9/processing-tasks/{task['id']}/runs/"
            f"{run['id']}/cancel",
            json=body,
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{TASKS_PATH}/999/runs/{run['id']}/cancel", json=body
        ).status_code
        == 404
    )
    assert (
        client.post(f"{TASKS_PATH}/{task['id']}/runs/999/cancel", json=body).status_code
        == 404
    )
    # The run is still running after all the failed requests.
    assert task_detail(client, task["id"])["runs"][0]["status"] == "running"
    assert client.post(url, json=body).status_code == 200


def test_cancel_run_of_other_task_is_422_and_untouched(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_task(client, "extract")
    second = make_task(client, "load")
    run = run_task(client, first["id"])

    response = client.post(
        f"{TASKS_PATH}/{second['id']}/runs/{run['id']}/cancel",
        json={"reason": "stop"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    detail = task_detail(client, first["id"])
    assert detail["runs"][0]["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None
    # The run can still be cancelled through its own task.
    response = client.post(
        f"{TASKS_PATH}/{first['id']}/runs/{run['id']}/cancel",
        json={"reason": "stop"},
    )
    assert response.status_code == 200


def test_cancel_run_through_other_version_is_422(client: TestClient) -> None:
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
        f"/datasets/orders/versions/2/processing-tasks/{task_v2['id']}/runs/"
        f"{run['id']}/cancel",
        json={"reason": "stop"},
    )
    assert response.status_code == 422
    assert task_detail(client, task_v1["id"])["runs"][0]["status"] == "running"


# --------------------------------------------------------------------------- #
# Body validation
# --------------------------------------------------------------------------- #


def test_cancel_rejects_missing_extra_non_string_or_blank_reason(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    run = run_task(client, task["id"])
    url = f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/cancel"

    for payload in (
        {},
        {"reason": ""},
        {"reason": "   "},
        {"reason": "\t\n"},
        {"reason": 5},
        {"reason": 1.5},
        {"reason": True},
        {"reason": None},
        {"reason": ["stop"]},
        {"reason": "stop", "force": True},
        {"reason": "stop", "status": "succeeded"},
    ):
        response = client.post(url, json=payload)
        assert response.status_code == 422, (payload, response.text)
        assert response.json()["error"] == "validation_error"
        assert set(response.json()) == {"error", "detail"}

    # Nothing was written: the run is still running with no finish timestamp.
    detail = task_detail(client, task["id"])
    assert detail["status"] == "running"
    assert detail["runs"][0]["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None
    assert detail["runs"][0]["error"] is None

    response = client.post(url, json={"reason": "now really"})
    assert response.status_code == 200
    assert response.json()["error"] == "now really"


def test_cancel_empty_body_is_422(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    run = run_task(client, task["id"])
    url = f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/cancel"

    response = client.post(url)
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert set(response.json()) == {"error", "detail"}
    assert task_detail(client, task["id"])["runs"][0]["status"] == "running"


def test_cancel_malformed_json_is_422(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    run = run_task(client, task["id"])
    response = client.post(
        f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/cancel",
        content="{not valid json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert task_detail(client, task["id"])["runs"][0]["status"] == "running"


def test_cancel_rejects_query_parameters(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    run = run_task(client, task["id"])
    url = f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/cancel"

    response = client.post(url + "?notify=false", json={"reason": "stop"})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    detail = task_detail(client, task["id"])
    assert detail["runs"][0]["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None

    # Without the query parameter the same body succeeds.
    response = client.post(url, json={"reason": "stop"})
    assert response.status_code == 200, response.text

    # 404 still takes precedence over query-parameter validation.
    response = client.post(
        "/datasets/ghost/versions/1/processing-tasks/1/runs/1/cancel?notify=false",
        json={"reason": "stop"},
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Audit records and the audit report after cancellation
# --------------------------------------------------------------------------- #


def test_audit_records_record_true_run_status_around_cancel(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=2)
    run = run_task(client, task["id"])
    audit_url = (
        f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/audit-records"
    )

    before = client.post(
        audit_url,
        json={"event": "e1", "input_summary": "in", "result_summary": "running"},
    )
    assert before.status_code == 201, before.text
    assert before.json()["run_status"] == "running"

    response = client.post(
        f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/cancel",
        json={"reason": "stop"},
    )
    assert response.status_code == 200

    after = client.post(
        audit_url,
        json={"event": "e2", "input_summary": "in", "result_summary": "canceled"},
    )
    assert after.status_code == 201, after.text
    assert after.json()["run_status"] == "failed"
    assert after.json()["sequence"] == 2
    assert after.json()["previous_hash"] == before.json()["evidence_hash"]

    records = client.get(audit_url).json()
    assert [record["sequence"] for record in records] == [1, 2]
    assert [record["run_status"] for record in records] == ["running", "failed"]

    verify = client.get(f"{audit_url}/verify")
    assert verify.status_code == 200
    assert verify.json()["valid"] is True
    assert verify.json()["checked_count"] == 2


def test_audit_report_reflects_cancelled_runs_and_exhaustion(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    canceled = make_task(client, "canceled", max_attempts=2)
    exhausted = make_task(client, "exhausted", max_attempts=1)
    running = make_task(client, "running")

    first = run_task(client, canceled["id"])
    client.post(
        f"{TASKS_PATH}/{canceled['id']}/runs/{first['id']}/cancel",
        json={"reason": "stop 1"},
    )
    only = run_task(client, exhausted["id"])
    client.post(
        f"{TASKS_PATH}/{exhausted['id']}/runs/{only['id']}/cancel",
        json={"reason": "stop 2"},
    )
    run_task(client, running["id"])

    response = client.get(f"{TASKS_PATH}/audit-report")
    assert response.status_code == 200, response.text
    report = response.json()
    summary = report["summary"]
    assert summary["task_count"] == 3
    assert summary["run_count"] == 3
    assert summary["running_tasks"] == 1
    assert summary["failed_tasks"] == 2
    assert summary["exhausted_tasks"] == 1
    assert summary["invalid_audit_runs"] == 0

    by_id = {task["id"]: task for task in report["tasks"]}
    canceled_runs = by_id[canceled["id"]]["runs"]
    assert canceled_runs[0]["status"] == "failed"
    assert canceled_runs[0]["error"] == "stop 1"
    assert canceled_runs[0]["finished_at"] is not None
    assert canceled_runs[0]["proof"] == {
        "valid": True,
        "checked_count": 0,
        "last_evidence_hash": None,
    }
    assert by_id[exhausted["id"]]["runs"][0]["error"] == "stop 2"
    assert by_id[running["id"]]["runs"][0]["status"] == "running"


def test_schedule_marks_canceled_tasks_retryable_or_exhausted(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    retryable = make_task(client, "retryable", max_attempts=2)
    exhausted = make_task(client, "exhausted", max_attempts=1)
    dependent = make_task(client, "dependent", depends_on=[exhausted["id"]])

    run = run_task(client, retryable["id"])
    client.post(
        f"{TASKS_PATH}/{retryable['id']}/runs/{run['id']}/cancel",
        json={"reason": "stop"},
    )
    run = run_task(client, exhausted["id"])
    client.post(
        f"{TASKS_PATH}/{exhausted['id']}/runs/{run['id']}/cancel",
        json={"reason": "stop"},
    )

    response = client.get(f"{TASKS_PATH}/schedule")
    assert response.status_code == 200, response.text
    by_id = {task["id"]: task for task in response.json()["tasks"]}
    assert by_id[retryable["id"]]["schedule_state"] == "retryable"
    assert by_id[retryable["id"]]["blocking_task_ids"] == []
    assert by_id[exhausted["id"]]["schedule_state"] == "exhausted"
    assert by_id[dependent["id"]]["schedule_state"] == "upstream_failed"


# --------------------------------------------------------------------------- #
# Concurrency: cancel vs finish / start / dispatch
# --------------------------------------------------------------------------- #


def test_concurrent_cancel_and_finish_have_single_winner(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=3)
    run = run_task(client, task["id"])
    url = f"{TASKS_PATH}/{task['id']}/runs/{run['id']}"

    thread_count = 8
    barrier = threading.Barrier(thread_count)
    results: list[tuple[str, int, dict]] = []
    results_lock = threading.Lock()

    def worker(cancel: bool) -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        if cancel:
            response = thread_client.post(
                f"{url}/cancel", json={"reason": "operator abort"}
            )
            kind = "cancel"
        else:
            response = thread_client.patch(
                url, json={"status": "succeeded"}
            )
            kind = "finish"
        with results_lock:
            results.append((kind, response.status_code, response.json()))

    threads = [
        threading.Thread(target=worker, args=(index % 2 == 0,))
        for index in range(thread_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    successes = [result for result in results if result[1] == 200]
    assert len(successes) == 1, results
    assert all(
        status_code == 409 for _kind, status_code, _body in results if status_code != 200
    )

    # The stored end state is exactly the single committed transition.
    detail = task_detail(client, task["id"])
    stored_run = detail["runs"][0]
    winner_kind, _status, winner_body = successes[0]
    assert stored_run["status"] == winner_body["status"]
    assert stored_run["finished_at"] == winner_body["finished_at"]
    assert stored_run["error"] == winner_body["error"]
    assert stored_run["status"] in ("succeeded", "failed")
    assert stored_run["finished_at"] is not None
    if winner_kind == "cancel":
        assert stored_run["status"] == "failed"
        assert stored_run["error"] == "operator abort"
    else:
        assert stored_run["status"] == "succeeded"
        assert stored_run["error"] is None
    assert detail["status"] == stored_run["status"]
    # No extra runs were created by the racing requests.
    assert len(detail["runs"]) == 1
    assert detail["attempt_count"] == 1


def test_concurrent_cancel_and_starts_keep_one_running_run(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "flaky", max_attempts=5)
    first = run_task(client, task["id"])
    cancel_url = f"{TASKS_PATH}/{task['id']}/runs/{first['id']}/cancel"
    start_url = f"{TASKS_PATH}/{task['id']}/runs"

    thread_count = 6
    barrier = threading.Barrier(thread_count)
    errors: list[AssertionError] = []

    def worker(is_canceller: bool) -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        try:
            if is_canceller:
                response = thread_client.post(cancel_url, json={"reason": "stop"})
                assert response.status_code in (200, 409), response.text
                return
            for _ in range(100):
                response = thread_client.post(start_url)
                assert response.status_code in (201, 409), response.text
                if response.status_code == 201:
                    started = response.json()
                    # A started run is itself canceled so the task becomes
                    # retryable again and attempts keep advancing.
                    response = thread_client.post(
                        f"{TASKS_PATH}/{task['id']}/runs/{started['id']}/cancel",
                        json={"reason": "stop"},
                    )
                    assert response.status_code in (200, 409), response.text
        except AssertionError as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(index == 0,))
        for index in range(thread_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors

    detail = task_detail(client, task["id"])
    attempts = [run["attempt"] for run in detail["runs"]]
    # Continuous numbering, no duplicates or gaps and never past the budget.
    assert attempts == list(range(1, len(attempts) + 1))
    assert len(attempts) <= task["max_attempts"]
    running_runs = [run for run in detail["runs"] if run["status"] == "running"]
    assert len(running_runs) <= 1
    for run in detail["runs"]:
        if run["status"] == "failed":
            assert run["finished_at"] is not None
            assert run["error"]
        else:
            assert run["finished_at"] is None
    assert detail["attempt_count"] == len(detail["runs"])


def test_concurrent_cancel_and_dispatch_have_single_transition(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "flaky", max_attempts=2)
    run = run_task(client, task["id"])
    cancel_url = f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/cancel"

    barrier = threading.Barrier(2)
    outcomes: dict[str, int] = {}

    def canceller() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        response = thread_client.post(cancel_url, json={"reason": "stop"})
        outcomes["cancel"] = response.status_code

    def dispatcher() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        response = thread_client.post(DISPATCH_PATH, json={"limit": 10})
        outcomes["dispatch"] = response.status_code
        outcomes["dispatch_runs"] = len(response.json()["runs"])

    threads = [threading.Thread(target=canceller), threading.Thread(target=dispatcher)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # While the run is 'running' dispatch cannot touch the task; once the
    # cancel commits, dispatch may claim the remaining attempt. Either way the
    # two operations never both transition the running run.
    detail = task_detail(client, task["id"])
    assert len(detail["runs"]) <= 2
    attempts = [r["attempt"] for r in detail["runs"]]
    assert attempts == list(range(1, len(attempts) + 1))
    assert len([r for r in detail["runs"] if r["status"] == "running"]) <= 1
    assert detail["runs"][0]["status"] == "failed"
    assert detail["runs"][0]["finished_at"] is not None
    assert detail["attempt_count"] == len(detail["runs"])


# --------------------------------------------------------------------------- #
# Persistence across a process restart
# --------------------------------------------------------------------------- #


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


CANCEL_CREATE_SCRIPT = """
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
task = client.post(base, json={"name": "flaky", "max_attempts": 2}).json()
run = client.post(f"{base}/{task['id']}/runs").json()
r = client.post(
    f"{base}/{task['id']}/runs/{run['id']}/cancel",
    json={"reason": "  stopped by operator  "},
)
assert r.status_code == 200, r.text
body = r.json()
assert body["status"] == "failed"
assert body["error"] == "stopped by operator"
assert body["finished_at"]
print("created")
"""

CANCEL_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
base = "/datasets/orders/versions/1/processing-tasks"
task = client.get(base).json()[0]
assert task["status"] == "failed", task
assert task["attempt_count"] == 1

detail = client.get(f"{base}/{task['id']}")
assert detail.status_code == 200, detail.text
runs = detail.json()["runs"]
assert len(runs) == 1
assert runs[0]["status"] == "failed"
assert runs[0]["error"] == "stopped by operator"
assert runs[0]["finished_at"] is not None

# The canceled run cannot be canceled again after the restart.
repeat = client.post(
    f"{base}/{task['id']}/runs/{runs[0]['id']}/cancel",
    json={"reason": "again"},
)
assert repeat.status_code == 409, repeat.text

# One attempt remains, so the task can run again.
started = client.post(f"{base}/{task['id']}/runs")
assert started.status_code == 201, started.text
assert started.json()["attempt"] == 2
assert started.json()["status"] == "running"

# The schedule and report read the same persisted state.
schedule = client.get(f"{base}/schedule").json()
by_id = {item["id"]: item for item in schedule["tasks"]}
assert by_id[task["id"]]["schedule_state"] == "running"
report = client.get(f"{base}/audit-report").json()
assert report["summary"]["run_count"] == 2
assert report["summary"]["failed_tasks"] == 0
assert report["summary"]["running_tasks"] == 1
print("verified")
"""


def test_cancel_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "cancel-persist.db"
    assert _run_script(db_path, CANCEL_CREATE_SCRIPT) == "created"
    assert _run_script(db_path, CANCEL_VERIFY_SCRIPT) == "verified"
