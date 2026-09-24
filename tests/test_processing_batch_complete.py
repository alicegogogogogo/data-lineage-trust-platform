"""Tests for batch-completing processing-task runs.

POST .../processing-tasks/batch-complete

The batch endpoint validates every item in one transaction and only then
finishes the runs; any rejection leaves every run and task untouched.
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
BATCH_PATH = f"{TASKS_PATH}/batch-complete"
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


def succeeded_item(task: dict, run: dict) -> dict:
    return {"task_id": task["id"], "run_id": run["id"], "status": "succeeded"}


def failed_item(task: dict, run: dict, error: str = "boom") -> dict:
    return {
        "task_id": task["id"],
        "run_id": run["id"],
        "status": "failed",
        "error": error,
    }


# --------------------------------------------------------------------------- #
# Successful batches
# --------------------------------------------------------------------------- #


def test_batch_completes_mixed_runs_sorted_by_task_id(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_task(client, "first", max_attempts=2)
    second = make_task(client, "second")
    third = make_task(client, "third")
    run_first = run_task(client, first["id"])
    run_second = run_task(client, second["id"])
    run_third = run_task(client, third["id"])

    # Items are deliberately submitted out of order. Messages are validated
    # after trimming but stored verbatim, like the single-run finish route.
    response = client.post(
        BATCH_PATH,
        json={
            "runs": [
                failed_item(third, run_third, error=" disk full "),
                succeeded_item(first, run_first),
                failed_item(second, run_second, error="\tkaboom\n"),
            ]
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert [run["task_id"] for run in body["runs"]] == [
        first["id"],
        second["id"],
        third["id"],
    ]
    by_task = {run["task_id"]: run for run in body["runs"]}
    assert set(by_task[first["id"]]) == RUN_FIELDS
    assert by_task[first["id"]]["status"] == "succeeded"
    assert by_task[first["id"]]["error"] is None
    assert by_task[second["id"]]["status"] == "failed"
    assert by_task[second["id"]]["error"] == "\tkaboom\n"
    assert by_task[third["id"]]["status"] == "failed"
    assert by_task[third["id"]]["error"] == " disk full "
    # Every run ends at the same instant and the timestamp is well-formed.
    finished_ats = {run["finished_at"] for run in body["runs"]}
    assert len(finished_ats) == 1
    finished_at = finished_ats.pop()
    datetime.fromisoformat(finished_at)

    # Tasks move to the same status atomically; attempt counts are untouched.
    assert task_detail(client, first["id"])["status"] == "succeeded"
    second_detail = task_detail(client, second["id"])
    assert second_detail["status"] == "failed"
    assert second_detail["attempt_count"] == 1
    third_detail = task_detail(client, third["id"])
    assert third_detail["runs"][0]["finished_at"] == finished_at


def test_failed_task_in_batch_is_retryable_with_continuous_attempts(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "flaky", max_attempts=3)
    run = run_task(client, task["id"])

    response = client.post(BATCH_PATH, json={"runs": [failed_item(task, run)]})
    assert response.status_code == 200, response.text

    # The remaining attempts are still handed out continuously.
    retry = client.post(DISPATCH_PATH, json={"limit": 5})
    assert retry.status_code == 201, retry.text
    started = retry.json()["runs"]
    assert len(started) == 1
    assert started[0]["task_id"] == task["id"]
    assert started[0]["attempt"] == 2
    detail = task_detail(client, task["id"])
    assert detail["status"] == "running"
    assert detail["attempt_count"] == 2
    assert [r["attempt"] for r in detail["runs"]] == [1, 2]
    assert detail["runs"][0]["status"] == "failed"
    assert detail["runs"][1]["status"] == "running"
    assert detail["runs"][1]["finished_at"] is None


def test_succeeded_task_in_batch_is_not_started_again(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=3)
    run = run_task(client, task["id"])
    response = client.post(BATCH_PATH, json={"runs": [succeeded_item(task, run)]})
    assert response.status_code == 200

    response = client.post(DISPATCH_PATH, json={"limit": 10})
    assert response.status_code == 201
    assert response.json()["runs"] == []
    detail = task_detail(client, task["id"])
    assert detail["attempt_count"] == 1
    assert len(detail["runs"]) == 1


def test_dependencies_succeeded_in_same_batch_unblock_dependents(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    root = make_task(client, "root")
    leaf = make_task(client, "leaf", depends_on=[root["id"]])
    root_run = run_task(client, root["id"])
    # The dependent is still pending while its dependency runs.
    schedule = client.get(f"{TASKS_PATH}/schedule").json()
    by_id = {task["id"]: task for task in schedule["tasks"]}
    assert by_id[leaf["id"]]["schedule_state"] == "blocked"

    response = client.post(
        BATCH_PATH, json={"runs": [succeeded_item(root, root_run)]}
    )
    assert response.status_code == 200, response.text

    # The schedule immediately sees the same-batch success.
    schedule = client.get(f"{TASKS_PATH}/schedule").json()
    by_id = {task["id"]: task for task in schedule["tasks"]}
    assert by_id[root["id"]]["schedule_state"] == "succeeded"
    assert by_id[leaf["id"]]["schedule_state"] == "ready"
    assert by_id[leaf["id"]]["blocking_task_ids"] == []

    # Dispatch starts the dependent on its first attempt; the succeeded
    # dependency is neither re-run nor does it consume an attempt number.
    response = client.post(DISPATCH_PATH, json={"limit": 10})
    assert response.status_code == 201
    started = response.json()["runs"]
    assert [(run["task_id"], run["attempt"]) for run in started] == [
        (leaf["id"], 1)
    ]
    assert task_detail(client, root["id"])["attempt_count"] == 1
    assert len(task_detail(client, root["id"])["runs"]) == 1


# --------------------------------------------------------------------------- #
# Request body validation (422)
# --------------------------------------------------------------------------- #


def test_batch_rejects_empty_missing_or_ill_typed_bodies(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    run = run_task(client, task["id"])
    good = succeeded_item(task, run)

    for payload in (
        {"runs": []},
        {},
        {"runs": None},
        {"runs": [good], "extra": True},
        {"runs": [{"task_id": task["id"], "run_id": run["id"], "status": "succeeded",
                   "note": "x"}]},
        {"runs": [{"task_id": str(task["id"]), "run_id": run["id"],
                   "status": "succeeded"}]},
        {"runs": [{"task_id": task["id"] + 0.5, "run_id": run["id"],
                   "status": "succeeded"}]},
        {"runs": [{"task_id": True, "run_id": run["id"], "status": "succeeded"}]},
        {"runs": [{"task_id": task["id"], "run_id": str(run["id"]),
                   "status": "succeeded"}]},
        {"runs": [{"task_id": task["id"], "run_id": 1.5,
                   "status": "succeeded"}]},
        {"runs": [{"task_id": task["id"], "run_id": run["id"]}]},
        {"runs": [{"task_id": task["id"], "run_id": run["id"],
                   "status": "cancelled"}]},
        {"runs": [{"task_id": task["id"], "run_id": run["id"],
                   "status": "failed"}]},
        {"runs": [{"task_id": task["id"], "run_id": run["id"],
                   "status": "failed", "error": ""}]},
        {"runs": [{"task_id": task["id"], "run_id": run["id"],
                   "status": "failed", "error": "   "}]},
        {"runs": [{"task_id": task["id"], "run_id": run["id"],
                   "status": "failed", "error": 5}]},
        {"runs": [{"task_id": task["id"], "run_id": run["id"],
                   "status": "succeeded", "error": "nope"}]},
    ):
        response = client.post(BATCH_PATH, json=payload)
        assert response.status_code == 422, (payload, response.text)
        assert response.json()["error"] == "validation_error"
        assert set(response.json()) == {"error", "detail"}

    detail = task_detail(client, task["id"])
    assert detail["status"] == "running"
    assert detail["runs"][0]["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None
    assert detail["runs"][0]["error"] is None

    response = client.post(BATCH_PATH, json={"runs": [good]})
    assert response.status_code == 200, response.text


def test_batch_empty_body_and_malformed_json_are_422(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    run = run_task(client, task["id"])

    response = client.post(BATCH_PATH)
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"

    response = client.post(
        BATCH_PATH,
        content="{not valid json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"

    detail = task_detail(client, task["id"])
    assert detail["runs"][0]["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None


def test_batch_rejects_query_parameters(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    run = run_task(client, task["id"])
    response = client.post(
        BATCH_PATH + "?notify=false", json={"runs": [succeeded_item(task, run)]}
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    detail = task_detail(client, task["id"])
    assert detail["runs"][0]["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None

    # The same body without the query parameter succeeds.
    response = client.post(BATCH_PATH, json={"runs": [succeeded_item(task, run)]})
    assert response.status_code == 200, response.text

    # 404 still takes precedence over query-parameter validation.
    response = client.post(
        "/datasets/ghost/versions/1/processing-tasks/batch-complete?notify=false",
        json={"runs": []},
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Path and item resolution: 404 / 422
# --------------------------------------------------------------------------- #


def test_batch_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    # Even an (otherwise invalid) empty list is a 404 when the path is unknown.
    response = client.post(
        "/datasets/ghost/versions/1/processing-tasks/batch-complete",
        json={"runs": []},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    create_dataset_and_version(client)
    response = client.post(
        "/datasets/orders/versions/9/processing-tasks/batch-complete",
        json={"runs": []},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_batch_unknown_task_or_run_is_404_and_changes_nothing(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    run = run_task(client, task["id"])

    response = client.post(
        BATCH_PATH,
        json={"runs": [{"task_id": 999, "run_id": run["id"], "status": "succeeded"}]},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    response = client.post(
        BATCH_PATH,
        json={"runs": [{"task_id": task["id"], "run_id": 999,
                        "status": "succeeded"}]},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    detail = task_detail(client, task["id"])
    assert detail["runs"][0]["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None


def test_batch_task_of_other_version_is_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    v1_task = make_task(client, "ingest")
    v1_run = run_task(client, v1_task["id"])
    client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    v2_task = client.post(
        "/datasets/orders/versions/2/processing-tasks", json={"name": "ingest"}
    ).json()
    v2_run = client.post(
        f"/datasets/orders/versions/2/processing-tasks/{v2_task['id']}/runs"
    ).json()

    # A v1 task named in a v2 batch does not exist under that version.
    response = client.post(
        "/datasets/orders/versions/2/processing-tasks/batch-complete",
        json={"runs": [{"task_id": v1_task["id"], "run_id": v1_run["id"],
                        "status": "succeeded"}]},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert task_detail(client, v1_task["id"])["runs"][0]["status"] == "running"
    assert (
        client.get(
            f"/datasets/orders/versions/2/processing-tasks/{v2_task['id']}"
        ).json()["runs"][0]["status"]
        == "running"
    )
    assert v2_run["id"]  # the v2 run is untouched as well


def test_batch_run_of_other_task_same_version_is_422(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_task(client, "extract")
    second = make_task(client, "load")
    run = run_task(client, first["id"])

    response = client.post(
        BATCH_PATH,
        json={"runs": [{"task_id": second["id"], "run_id": run["id"],
                        "status": "succeeded"}]},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    detail = task_detail(client, first["id"])
    assert detail["runs"][0]["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None
    # The run can still be completed through its own task.
    response = client.post(
        BATCH_PATH,
        json={"runs": [{"task_id": first["id"], "run_id": run["id"],
                        "status": "succeeded"}]},
    )
    assert response.status_code == 200


def test_batch_run_of_other_version_is_422(client: TestClient) -> None:
    create_dataset_and_version(client)
    v1_task = make_task(client, "ingest")
    v1_run = run_task(client, v1_task["id"])
    client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    v2_task = client.post(
        "/datasets/orders/versions/2/processing-tasks", json={"name": "ingest"}
    ).json()
    run_task_path_run = client.post(
        f"/datasets/orders/versions/2/processing-tasks/{v2_task['id']}/runs"
    ).json()

    response = client.post(
        "/datasets/orders/versions/2/processing-tasks/batch-complete",
        json={"runs": [{"task_id": v2_task["id"], "run_id": v1_run["id"],
                        "status": "succeeded"}]},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert task_detail(client, v1_task["id"])["runs"][0]["status"] == "running"
    # The v2 current run is still running and completable afterwards.
    response = client.post(
        "/datasets/orders/versions/2/processing-tasks/batch-complete",
        json={"runs": [{"task_id": v2_task["id"],
                        "run_id": run_task_path_run["id"], "status": "succeeded"}]},
    )
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# State conflicts (409) and atomic rollback
# --------------------------------------------------------------------------- #


def test_batch_duplicate_task_is_409_and_changes_nothing(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=2)
    run = run_task(client, task["id"])
    before = task_detail(client, task["id"])

    response = client.post(
        BATCH_PATH,
        json={"runs": [succeeded_item(task, run), failed_item(task, run)]},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    assert task_detail(client, task["id"]) == before


def test_batch_ended_run_is_409_and_changes_nothing(client: TestClient) -> None:
    create_dataset_and_version(client)
    done = make_task(client, "done")
    done_run = run_task(client, done["id"])
    client.patch(
        f"{TASKS_PATH}/{done['id']}/runs/{done_run['id']}",
        json={"status": "succeeded"},
    )
    before = task_detail(client, done["id"])

    response = client.post(
        BATCH_PATH, json={"runs": [succeeded_item(done, done_run)]}
    )
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    assert task_detail(client, done["id"]) == before

    # A currently running task whose batch references a previous (ended) run is
    # likewise a 409: the run is no longer the task's current run.
    flaky = make_task(client, "flaky", max_attempts=3)
    first_run = run_task(client, flaky["id"])
    client.patch(
        f"{TASKS_PATH}/{flaky['id']}/runs/{first_run['id']}",
        json={"status": "failed", "error": "boom"},
    )
    second_run = run_task(client, flaky["id"])
    before = task_detail(client, flaky["id"])
    response = client.post(
        BATCH_PATH, json={"runs": [succeeded_item(flaky, first_run)]}
    )
    assert response.status_code == 409
    assert task_detail(client, flaky["id"]) == before
    assert second_run["status"] == "running"


def test_batch_failed_task_is_not_completable(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "flaky", max_attempts=2)
    run = run_task(client, task["id"])
    client.patch(
        f"{TASKS_PATH}/{task['id']}/runs/{run['id']}",
        json={"status": "failed", "error": "boom"},
    )
    before = task_detail(client, task["id"])
    response = client.post(
        BATCH_PATH, json={"runs": [failed_item(task, run)]}
    )
    assert response.status_code == 409
    assert task_detail(client, task["id"]) == before


def test_batch_rolls_back_when_a_later_item_is_invalid(client: TestClient) -> None:
    create_dataset_and_version(client)
    good = make_task(client, "good")
    bad = make_task(client, "bad")
    good_run = run_task(client, good["id"])
    bad_run = run_task(client, bad["id"])
    # End the second run out of band so the batch contains a finished run.
    client.patch(
        f"{TASKS_PATH}/{bad['id']}/runs/{bad_run['id']}",
        json={"status": "succeeded"},
    )

    response = client.post(
        BATCH_PATH,
        json={"runs": [succeeded_item(good, good_run), succeeded_item(bad, bad_run)]},
    )
    assert response.status_code == 409
    good_detail = task_detail(client, good["id"])
    assert good_detail["status"] == "running"
    assert good_detail["runs"][0]["status"] == "running"
    assert good_detail["runs"][0]["finished_at"] is None
    assert good_detail["runs"][0]["error"] is None

    # A 404 on one item rolls back every other item too.
    other = make_task(client, "other")
    other_run = run_task(client, other["id"])
    response = client.post(
        BATCH_PATH,
        json={
            "runs": [
                succeeded_item(other, other_run),
                {"task_id": good["id"], "run_id": 999, "status": "succeeded"},
            ]
        },
    )
    assert response.status_code == 404
    other_detail = task_detail(client, other["id"])
    assert other_detail["runs"][0]["status"] == "running"
    assert other_detail["runs"][0]["finished_at"] is None


def test_batch_can_still_complete_after_a_conflict(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    run = run_task(client, task["id"])
    done = make_task(client, "done")
    done_run = run_task(client, done["id"])
    client.patch(
        f"{TASKS_PATH}/{done['id']}/runs/{done_run['id']}",
        json={"status": "succeeded"},
    )

    bad = client.post(
        BATCH_PATH,
        json={"runs": [succeeded_item(task, run), succeeded_item(done, done_run)]},
    )
    assert bad.status_code == 409

    good = client.post(BATCH_PATH, json={"runs": [succeeded_item(task, run)]})
    assert good.status_code == 200
    assert good.json()["runs"][0]["status"] == "succeeded"


# --------------------------------------------------------------------------- #
# Audit records, report and schedule read the batch results
# --------------------------------------------------------------------------- #


def test_audit_records_capture_run_status_at_write_time(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=2)
    run = run_task(client, task["id"])
    audit_url = f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/audit-records"

    before = client.post(
        audit_url,
        json={"event": "e1", "input_summary": "in", "result_summary": "running"},
    )
    assert before.status_code == 201
    assert before.json()["run_status"] == "running"

    response = client.post(
        BATCH_PATH, json={"runs": [failed_item(task, run, error="stop")]}
    )
    assert response.status_code == 200

    after = client.post(
        audit_url,
        json={"event": "e2", "input_summary": "in", "result_summary": "failed"},
    )
    assert after.status_code == 201
    assert after.json()["run_status"] == "failed"
    assert after.json()["previous_hash"] == before.json()["evidence_hash"]

    verify = client.get(f"{audit_url}/verify")
    assert verify.status_code == 200
    assert verify.json()["valid"] is True
    assert verify.json()["checked_count"] == 2


def test_audit_report_and_schedule_reflect_batch(client: TestClient) -> None:
    create_dataset_and_version(client)
    succeeded = make_task(client, "succeeded")
    retryable = make_task(client, "retryable", max_attempts=2)
    exhausted = make_task(client, "exhausted")
    s_run = run_task(client, succeeded["id"])
    r_run = run_task(client, retryable["id"])
    e_run = run_task(client, exhausted["id"])

    response = client.post(
        BATCH_PATH,
        json={
            "runs": [
                succeeded_item(succeeded, s_run),
                failed_item(retryable, r_run, error="retry me"),
                failed_item(exhausted, e_run, error="dead"),
            ]
        },
    )
    assert response.status_code == 200, response.text

    report = client.get(f"{TASKS_PATH}/audit-report").json()
    summary = report["summary"]
    assert summary["task_count"] == 3
    assert summary["run_count"] == 3
    assert summary["succeeded_tasks"] == 1
    assert summary["failed_tasks"] == 2
    assert summary["running_tasks"] == 0
    assert summary["exhausted_tasks"] == 1
    assert summary["invalid_audit_runs"] == 0
    by_id = {task["id"]: task for task in report["tasks"]}
    assert by_id[succeeded["id"]]["runs"][0]["status"] == "succeeded"
    assert by_id[retryable["id"]]["runs"][0]["error"] == "retry me"
    assert by_id[retryable["id"]]["runs"][0]["finished_at"] is not None
    assert by_id[exhausted["id"]]["runs"][0]["error"] == "dead"

    schedule = client.get(f"{TASKS_PATH}/schedule").json()
    states = {task["id"]: task["schedule_state"] for task in schedule["tasks"]}
    assert states[succeeded["id"]] == "succeeded"
    assert states[retryable["id"]] == "retryable"
    assert states[exhausted["id"]] == "exhausted"


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_batches_have_a_single_winner(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    run = run_task(client, task["id"])

    thread_count = 6
    barrier = threading.Barrier(thread_count)
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        response = thread_client.post(
            BATCH_PATH, json={"runs": [succeeded_item(task, run)]}
        )
        with statuses_lock:
            statuses.append(response.status_code)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses.count(200) == 1, statuses
    assert all(status == 409 for status in statuses if status != 200)
    detail = task_detail(client, task["id"])
    assert len(detail["runs"]) == 1
    assert detail["attempt_count"] == 1
    assert detail["runs"][0]["status"] == "succeeded"
    assert detail["runs"][0]["finished_at"] is not None


def test_concurrent_batch_and_single_finish_or_cancel_single_winner(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=3)
    run = run_task(client, task["id"])
    run_url = f"{TASKS_PATH}/{task['id']}/runs/{run['id']}"

    thread_count = 8
    barrier = threading.Barrier(thread_count)
    results: list[tuple[str, int, dict]] = []
    results_lock = threading.Lock()

    def worker(kind: str) -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        if kind == "batch-success":
            response = thread_client.post(
                BATCH_PATH, json={"runs": [succeeded_item(task, run)]}
            )
        elif kind == "batch-fail":
            response = thread_client.post(
                BATCH_PATH, json={"runs": [failed_item(task, run, error="batch fail")]}
            )
        elif kind == "finish":
            response = thread_client.patch(
                run_url, json={"status": "succeeded"}
            )
        else:
            response = thread_client.post(
                f"{run_url}/cancel", json={"reason": "operator abort"}
            )
        with results_lock:
            results.append((kind, response.status_code, response.json()))

    kinds = ["batch-success", "batch-fail", "finish", "cancel"]
    threads = [
        threading.Thread(target=worker, args=(kinds[index % len(kinds)],))
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

    detail = task_detail(client, task["id"])
    stored_run = detail["runs"][0]
    assert len(detail["runs"]) == 1
    assert detail["attempt_count"] == 1
    assert stored_run["finished_at"] is not None
    assert stored_run["status"] in ("succeeded", "failed")
    winner_kind = successes[0][0]
    if winner_kind in ("batch-success", "finish"):
        assert stored_run["status"] == "succeeded"
        assert stored_run["error"] is None
    else:
        assert stored_run["status"] == "failed"
        expected = "batch fail" if winner_kind == "batch-fail" else "operator abort"
        assert stored_run["error"] == expected
    assert detail["status"] == stored_run["status"]


def test_losing_batch_leaves_no_half_written_run(client: TestClient) -> None:
    create_dataset_and_version(client)
    task_a = make_task(client, "a")
    task_b = make_task(client, "b")
    run_a = run_task(client, task_a["id"])
    run_b = run_task(client, task_b["id"])

    def batch(thread_client: TestClient, item_for_a: dict) -> int:
        response = thread_client.post(
            BATCH_PATH, json={"runs": [item_for_a, succeeded_item(task_b, run_b)]}
        )
        return response.status_code

    # One batch finishes both; a second batch racing the same two runs must lose
    # wholesale (it cannot finish just one of them).
    first_client = TestClient(client.app)
    second_client = TestClient(client.app)
    outcomes = []

    def run_batch(thread_client: TestClient, item_for_a: dict) -> None:
        outcomes.append(batch(thread_client, item_for_a))

    threads = [
        threading.Thread(
            target=run_batch,
            args=(first_client, succeeded_item(task_a, run_a)),
        ),
        threading.Thread(
            target=run_batch,
            args=(second_client, failed_item(task_a, run_a, error="other")),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == [200, 409], outcomes
    for task, run in ((task_a, run_a), (task_b, run_b)):
        detail = task_detail(client, task["id"])
        assert len(detail["runs"]) == 1
        assert detail["runs"][0]["id"] == run["id"]
        assert detail["runs"][0]["finished_at"] is not None
        assert detail["attempt_count"] == 1


def test_concurrent_batch_and_starts_keep_continuous_attempts(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "flaky", max_attempts=6)
    first = run_task(client, task["id"])
    start_url = f"{TASKS_PATH}/{task['id']}/runs"

    thread_count = 6
    barrier = threading.Barrier(thread_count)
    errors: list[AssertionError] = []

    def worker(is_batcher: bool) -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        try:
            if is_batcher:
                response = thread_client.post(
                    BATCH_PATH,
                    json={"runs": [failed_item(task, first, error="batch")]},
                )
                assert response.status_code in (200, 409), response.text
                return
            for _ in range(100):
                response = thread_client.post(start_url)
                assert response.status_code in (201, 409), response.text
                if response.status_code == 201:
                    started = response.json()
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
    assert attempts == list(range(1, len(attempts) + 1))
    assert len(attempts) <= task["max_attempts"]
    assert len([run for run in detail["runs"] if run["status"] == "running"]) <= 1
    for run in detail["runs"]:
        if run["status"] == "failed":
            assert run["finished_at"] is not None
            assert run["error"]
        else:
            assert run["finished_at"] is None
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


BATCH_CREATE_SCRIPT = """
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
retryable = client.post(base, json={"name": "retryable", "max_attempts": 2}).json()
done = client.post(base, json={"name": "done"}).json()
r1 = client.post(f"{base}/{retryable['id']}/runs").json()
r2 = client.post(f"{base}/{done['id']}/runs").json()
r = client.post(
    f"{base}/batch-complete",
    json={"runs": [
        {"task_id": done["id"], "run_id": r2["id"], "status": "succeeded"},
        {"task_id": retryable["id"], "run_id": r1["id"],
         "status": "failed", "error": "  try again  "},
    ]},
)
assert r.status_code == 200, r.text
assert [run["task_id"] for run in r.json()["runs"]] == [retryable["id"], done["id"]]
print("created")
"""

BATCH_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
base = "/datasets/orders/versions/1/processing-tasks"
tasks = {task["name"]: task for task in client.get(base).json()}
retryable, done = tasks["retryable"], tasks["done"]

detail = client.get(f"{base}/{retryable['id']}").json()
assert detail["status"] == "failed"
assert detail["attempt_count"] == 1
assert detail["runs"][0]["status"] == "failed"
assert detail["runs"][0]["error"] == "  try again  "
assert detail["runs"][0]["finished_at"] is not None

done_detail = client.get(f"{base}/{done['id']}").json()
assert done_detail["status"] == "succeeded"
done_run_id = done_detail["runs"][0]["id"]

# The batch outcome cannot be applied twice after the restart.
repeat = client.post(
    f"{base}/batch-complete",
    json={"runs": [{"task_id": done["id"], "run_id": done_run_id,
                    "status": "succeeded"}]},
)
assert repeat.status_code == 409, repeat.text

# The failed task still has one attempt left and restarts continuously.
started = client.post(f"{base}/{retryable['id']}/runs")
assert started.status_code == 201, started.text
assert started.json()["attempt"] == 2

schedule = {t["id"]: t for t in client.get(f"{base}/schedule").json()["tasks"]}
assert schedule[done["id"]]["schedule_state"] == "succeeded"
assert schedule[retryable["id"]]["schedule_state"] == "running"

report = client.get(f"{base}/audit-report").json()
assert report["summary"]["succeeded_tasks"] == 1
assert report["summary"]["running_tasks"] == 1
assert report["summary"]["exhausted_tasks"] == 0
print("verified")
"""


def test_batch_completion_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "batch-persist.db"
    assert _run_script(db_path, BATCH_CREATE_SCRIPT) == "created"
    assert _run_script(db_path, BATCH_VERIFY_SCRIPT) == "verified"
