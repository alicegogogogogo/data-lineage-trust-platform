"""Tests for batch-completing currently running processing-task runs.

POST .../processing-tasks/batch-complete
"""

from __future__ import annotations

import os
import sqlite3
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


def finish_run(client: TestClient, task_id: int, run_id: int, body: dict) -> dict:
    response = client.patch(f"{TASKS_PATH}/{task_id}/runs/{run_id}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def task_detail(client: TestClient, task_id: int) -> dict:
    response = client.get(f"{TASKS_PATH}/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


def running_task(client: TestClient, name: str, **overrides) -> tuple[dict, dict]:
    task = make_task(client, name, **overrides)
    run = run_task(client, task["id"])
    return task, run


def assert_error_shape(body: dict) -> None:
    assert set(body) == {"error", "detail"}
    assert isinstance(body["detail"], str) and body["detail"]


# --------------------------------------------------------------------------- #
# Successful batches
# --------------------------------------------------------------------------- #


def test_batch_completes_succeeded_and_failed_runs_together(client: TestClient) -> None:
    create_dataset_and_version(client)
    ok_task, ok_run = running_task(client, "ok", max_attempts=2)
    bad_task, bad_run = running_task(client, "bad", max_attempts=3)

    response = client.post(
        BATCH_PATH,
        json={
            "runs": [
                {"task_id": bad_task["id"], "run_id": bad_run["id"],
                 "status": "failed", "error": "disk full"},
                {"task_id": ok_task["id"], "run_id": ok_run["id"],
                 "status": "succeeded"},
            ]
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"dataset", "version", "runs"}
    assert body["dataset"] == "orders"
    assert body["version"] == 1

    # Response runs are sorted by task id ascending regardless of request order.
    assert [run["task_id"] for run in body["runs"]] == sorted(
        run["task_id"] for run in body["runs"]
    )
    assert [run["task_id"] for run in body["runs"]] == [ok_task["id"], bad_task["id"]]
    by_task = {run["task_id"]: run for run in body["runs"]}

    succeeded_run = by_task[ok_task["id"]]
    assert set(succeeded_run) == RUN_FIELDS
    assert succeeded_run["id"] == ok_run["id"]
    assert succeeded_run["attempt"] == 1
    assert succeeded_run["status"] == "succeeded"
    assert succeeded_run["error"] is None
    assert succeeded_run["finished_at"] is not None
    datetime.fromisoformat(succeeded_run["finished_at"])

    failed_run = by_task[bad_task["id"]]
    assert failed_run["status"] == "failed"
    assert failed_run["error"] == "disk full"
    assert failed_run["finished_at"] is not None

    ok_detail = task_detail(client, ok_task["id"])
    assert ok_detail["status"] == "succeeded"
    assert ok_detail["attempt_count"] == 1
    assert ok_detail["runs"][0]["status"] == "succeeded"
    assert ok_detail["runs"][0]["finished_at"] == succeeded_run["finished_at"]

    bad_detail = task_detail(client, bad_task["id"])
    assert bad_detail["status"] == "failed"
    # The consumed attempt is not rolled back ...
    assert bad_detail["attempt_count"] == 1
    assert bad_detail["runs"][0]["status"] == "failed"
    # ... and the task can still retry with the continuous next attempt.
    second = run_task(client, bad_task["id"])
    assert second["attempt"] == 2
    assert second["status"] == "running"


def test_batch_failed_error_must_only_be_non_blank_after_trim(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task, run = running_task(client, "bad", max_attempts=2)
    # Mirrors the single finish endpoint: surrounding whitespace does not
    # invalidate the message and the submitted text is stored verbatim.
    response = client.post(
        BATCH_PATH,
        json={"runs": [
            {"task_id": task["id"], "run_id": run["id"],
             "status": "failed", "error": "\t boom \n"},
        ]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["runs"][0]["error"] == "\t boom \n"
    assert task_detail(client, task["id"])["status"] == "failed"


def test_batch_response_sorted_by_task_id(client: TestClient) -> None:
    create_dataset_and_version(client)
    started = [running_task(client, f"task-{index}") for index in range(5)]
    items = [
        {"task_id": task["id"], "run_id": run["id"], "status": "succeeded"}
        for task, run in reversed(started)
    ]
    response = client.post(BATCH_PATH, json={"runs": items})
    assert response.status_code == 200, response.text
    ids = [run["task_id"] for run in response.json()["runs"]]
    assert ids == sorted(ids)
    assert ids == [task["id"] for task, _run in started]


def test_batch_completing_dependency_makes_dependents_schedulable(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    extract, extract_run = running_task(client, "extract")
    load = make_task(client, "load", depends_on=[extract["id"]], max_attempts=2)

    # The dependent could not be claimed while the dependency was running.
    blocked = client.post(DISPATCH_PATH, json={"limit": 10})
    assert blocked.status_code == 201
    assert blocked.json()["runs"] == []

    response = client.post(
        BATCH_PATH,
        json={"runs": [
            {"task_id": extract["id"], "run_id": extract_run["id"],
             "status": "succeeded"},
        ]},
    )
    assert response.status_code == 200

    # The scheduler now sees the succeeded dependency: the dependent starts
    # exactly once at attempt 1 and the dependency is never run again.
    claimed = client.post(DISPATCH_PATH, json={"limit": 10})
    assert claimed.status_code == 201, claimed.text
    runs = claimed.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["task_id"] == load["id"]
    assert runs[0]["attempt"] == 1

    schedule = client.get(f"{TASKS_PATH}/schedule")
    assert schedule.status_code == 200
    by_id = {task["id"]: task for task in schedule.json()["tasks"]}
    assert by_id[extract["id"]]["schedule_state"] == "succeeded"
    assert by_id[load["id"]]["schedule_state"] == "running"

    extract_detail = task_detail(client, extract["id"])
    assert len(extract_detail["runs"]) == 1
    assert extract_detail["attempt_count"] == 1


def test_batch_finishes_all_items_in_one_transaction(client: TestClient) -> None:
    create_dataset_and_version(client)
    first, first_run = running_task(client, "first")
    second, second_run = running_task(client, "second", max_attempts=2)
    third, third_run = running_task(client, "third", max_attempts=3)

    response = client.post(
        BATCH_PATH,
        json={"runs": [
            {"task_id": first["id"], "run_id": first_run["id"],
             "status": "succeeded"},
            {"task_id": second["id"], "run_id": second_run["id"],
             "status": "failed", "error": "once"},
            {"task_id": third["id"], "run_id": third_run["id"],
             "status": "failed", "error": "twice"},
        ]},
    )
    assert response.status_code == 200, response.text
    statuses = {run["task_id"]: run["status"] for run in response.json()["runs"]}
    assert statuses == {
        first["id"]: "succeeded",
        second["id"]: "failed",
        third["id"]: "failed",
    }
    # Every run shares the write moment's finished_at.
    finished_ats = {run["finished_at"] for run in response.json()["runs"]}
    assert len(finished_ats) == 1
    assert task_detail(client, first["id"])["status"] == "succeeded"
    assert task_detail(client, second["id"])["status"] == "failed"
    assert task_detail(client, third["id"])["status"] == "failed"


# --------------------------------------------------------------------------- #
# Body validation (422, nothing written)
# --------------------------------------------------------------------------- #


def test_batch_rejects_missing_or_empty_runs(client: TestClient) -> None:
    create_dataset_and_version(client)
    task, run = running_task(client, "ingest")

    for payload in (
        {},
        {"runs": []},
        {"runs": None},
        {"runs": {}},
        {"runs": "nope"},
        {"runs": [{"task_id": task["id"], "run_id": run["id"],
                   "status": "succeeded"}], "extra": 1},
    ):
        response = client.post(BATCH_PATH, json=payload)
        assert response.status_code == 422, (payload, response.text)
        body = response.json()
        assert body["error"] == "validation_error"
        assert_error_shape(body)

    detail = task_detail(client, task["id"])
    assert detail["status"] == "running"
    assert detail["runs"][0]["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None


def test_batch_rejects_extra_or_missing_item_fields(client: TestClient) -> None:
    create_dataset_and_version(client)
    task, run = running_task(client, "ingest")
    good = {"task_id": task["id"], "run_id": run["id"], "status": "succeeded"}

    payloads = [
        {"runs": [{"run_id": run["id"], "status": "succeeded"}]},
        {"runs": [{"task_id": task["id"], "status": "succeeded"}]},
        {"runs": [{"task_id": task["id"], "run_id": run["id"]}]},
        {"runs": [{**good, "force": True}]},
        {"runs": [{**good, "error": None, "reason": "x"}]},
        {"runs": ["not-an-object"]},
        {"runs": [42]},
    ]
    for payload in payloads:
        response = client.post(BATCH_PATH, json=payload)
        assert response.status_code == 422, (payload, response.text)
        assert response.json()["error"] == "validation_error"

    assert task_detail(client, task["id"])["status"] == "running"


def test_batch_rejects_wrong_id_types(client: TestClient) -> None:
    create_dataset_and_version(client)
    for task_id, run_id in (
        ("1", 1),
        (1.0, 1),
        (True, 1),
        (None, 1),
        ([1], 1),
        (1, "1"),
        (1, 1.5),
        (1, False),
        (1, None),
    ):
        response = client.post(
            BATCH_PATH,
            json={"runs": [
                {"task_id": task_id, "run_id": run_id, "status": "succeeded"},
            ]},
        )
        assert response.status_code == 422, (task_id, run_id, response.text)
        assert response.json()["error"] == "validation_error"


def test_batch_rejects_unknown_status_and_bad_error(client: TestClient) -> None:
    create_dataset_and_version(client)
    task, run = running_task(client, "ingest", max_attempts=3)

    payloads = [
        {"runs": [{"task_id": task["id"], "run_id": run["id"],
                   "status": "cancelled"}]},
        {"runs": [{"task_id": task["id"], "run_id": run["id"],
                   "status": "running"}]},
        {"runs": [{"task_id": task["id"], "run_id": run["id"],
                   "status": "failed"}]},
        {"runs": [{"task_id": task["id"], "run_id": run["id"],
                   "status": "failed", "error": ""}]},
        {"runs": [{"task_id": task["id"], "run_id": run["id"],
                   "status": "failed", "error": "   "}]},
        {"runs": [{"task_id": task["id"], "run_id": run["id"],
                   "status": "failed", "error": "\t\n"}]},
        {"runs": [{"task_id": task["id"], "run_id": run["id"],
                   "status": "succeeded", "error": "nope"}]},
        {"runs": [{"task_id": task["id"], "run_id": run["id"],
                   "status": "succeeded", "error": None}]},
        {"runs": [{"task_id": task["id"], "run_id": run["id"],
                   "status": "failed", "error": 5}]},
        {"runs": [{"task_id": task["id"], "run_id": run["id"],
                   "status": "failed", "error": True}]},
    ]
    for payload in payloads:
        response = client.post(BATCH_PATH, json=payload)
        assert response.status_code == 422, (payload, response.text)
        assert response.json()["error"] == "validation_error"

    # Nothing was written by any rejected batch.
    detail = task_detail(client, task["id"])
    assert detail["status"] == "running"
    assert detail["attempt_count"] == 1
    assert detail["runs"][0]["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None
    assert detail["runs"][0]["error"] is None

    # The run can still be completed with a valid batch.
    response = client.post(
        BATCH_PATH,
        json={"runs": [{"task_id": task["id"], "run_id": run["id"],
                        "status": "failed", "error": "real failure"}]},
    )
    assert response.status_code == 200


def test_batch_empty_body_and_malformed_json_are_422(client: TestClient) -> None:
    create_dataset_and_version(client)
    task, run = running_task(client, "ingest")

    response = client.post(BATCH_PATH)
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert_error_shape(response.json())

    response = client.post(
        BATCH_PATH,
        content="{not valid json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert_error_shape(response.json())

    assert task_detail(client, task["id"])["status"] == "running"


def test_batch_rejects_query_parameters(client: TestClient) -> None:
    create_dataset_and_version(client)
    task, run = running_task(client, "ingest")
    body = {"runs": [{"task_id": task["id"], "run_id": run["id"],
                      "status": "succeeded"}]}

    response = client.post(BATCH_PATH + "?notify=false", json=body)
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert task_detail(client, task["id"])["status"] == "running"

    # The same body succeeds without the query parameter.
    response = client.post(BATCH_PATH, json=body)
    assert response.status_code == 200

    # 404 still takes precedence over query-parameter validation.
    response = client.post(
        "/datasets/ghost/versions/1/processing-tasks/batch-complete?notify=false",
        json=body,
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Path / item resolution: 404 and 422
# --------------------------------------------------------------------------- #


def test_batch_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    body = {"runs": [{"task_id": 1, "run_id": 1, "status": "succeeded"}]}
    response = client.post(
        "/datasets/ghost/versions/1/processing-tasks/batch-complete", json=body
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert_error_shape(response.json())

    # 404 path resolution takes precedence over the per-item error rule: an
    # explicit null on a success item is a 422 only once the path resolves.
    body_null_error = {
        "runs": [{"task_id": 1, "run_id": 1, "status": "succeeded", "error": None}]
    }
    response = client.post(
        "/datasets/ghost/versions/1/processing-tasks/batch-complete",
        json=body_null_error,
    )
    assert response.status_code == 404

    create_dataset_and_version(client)
    response = client.post(
        "/datasets/orders/versions/9/processing-tasks/batch-complete", json=body
    )
    assert response.status_code == 404


def test_batch_unknown_task_is_404_and_writes_nothing(client: TestClient) -> None:
    create_dataset_and_version(client)
    task, run = running_task(client, "ingest")

    response = client.post(
        BATCH_PATH,
        json={"runs": [
            {"task_id": task["id"], "run_id": run["id"], "status": "succeeded"},
            {"task_id": 999, "run_id": 1, "status": "succeeded"},
        ]},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    detail = task_detail(client, task["id"])
    assert detail["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None


def test_batch_task_of_other_version_is_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    task_v1, run_v1 = running_task(client, "ingest")
    response = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201
    task_v2 = client.post(
        "/datasets/orders/versions/2/processing-tasks", json={"name": "ingest"}
    ).json()

    # The v2 task id named against version 1 does not exist there.
    response = client.post(
        "/datasets/orders/versions/1/processing-tasks/batch-complete",
        json={"runs": [
            {"task_id": task_v2["id"], "run_id": 1, "status": "succeeded"},
        ]},
    )
    assert response.status_code == 404

    # A v1 task/run named through the version-2 collection is 404 as well.
    response = client.post(
        "/datasets/orders/versions/2/processing-tasks/batch-complete",
        json={"runs": [
            {"task_id": task_v1["id"], "run_id": run_v1["id"],
             "status": "succeeded"},
        ]},
    )
    assert response.status_code == 404
    assert task_detail(client, task_v1["id"])["runs"][0]["status"] == "running"


def test_batch_unknown_run_is_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    task, run = running_task(client, "ingest")
    response = client.post(
        BATCH_PATH,
        json={"runs": [
            {"task_id": task["id"], "run_id": run["id"], "status": "succeeded"},
            {"task_id": task["id"] + 999, "run_id": run["id"] + 999,
             "status": "succeeded"},
        ]},
    )
    # Duplicate-free batch: the second task does not exist either, but its run
    # is also unknown; either resolution order yields a 404 and no writes.
    assert response.status_code == 404
    assert task_detail(client, task["id"])["status"] == "running"

    # Plain unknown run against the existing task is 404.
    response = client.post(
        BATCH_PATH,
        json={"runs": [
            {"task_id": task["id"], "run_id": 999, "status": "succeeded"},
        ]},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_batch_run_of_other_task_same_version_is_422(client: TestClient) -> None:
    create_dataset_and_version(client)
    first, first_run = running_task(client, "extract")
    second, _second_run = running_task(client, "load")

    response = client.post(
        BATCH_PATH,
        json={"runs": [
            {"task_id": second["id"], "run_id": first_run["id"],
             "status": "succeeded"},
        ]},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert_error_shape(response.json())

    # Neither run nor either task changed.
    first_detail = task_detail(client, first["id"])
    assert first_detail["status"] == "running"
    assert first_detail["runs"][0]["status"] == "running"
    assert first_detail["runs"][0]["finished_at"] is None
    assert task_detail(client, second["id"])["status"] == "running"

    # The run can still be completed through its own task.
    response = client.post(
        BATCH_PATH,
        json={"runs": [
            {"task_id": first["id"], "run_id": first_run["id"],
             "status": "succeeded"},
        ]},
    )
    assert response.status_code == 200


def test_batch_run_of_task_in_other_version_is_404_not_422(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task_v1, run_v1 = running_task(client, "ingest")
    client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    task_v2 = client.post(
        "/datasets/orders/versions/2/processing-tasks", json={"name": "ingest"}
    ).json()

    # The run exists in the same dataset but another version and is named by
    # that other version's task: out of scope -> 404.
    response = client.post(
        "/datasets/orders/versions/2/processing-tasks/batch-complete",
        json={"runs": [
            {"task_id": task_v2["id"], "run_id": run_v1["id"],
             "status": "succeeded"},
        ]},
    )
    assert response.status_code == 404
    assert task_detail(client, task_v1["id"])["runs"][0]["status"] == "running"


# --------------------------------------------------------------------------- #
# Conflicts (409, whole batch rolled back)
# --------------------------------------------------------------------------- #


def test_batch_duplicate_task_is_409_and_writes_nothing(client: TestClient) -> None:
    create_dataset_and_version(client)
    task, run = running_task(client, "ingest", max_attempts=3)
    other, other_run = running_task(client, "other")

    response = client.post(
        BATCH_PATH,
        json={"runs": [
            {"task_id": task["id"], "run_id": run["id"], "status": "succeeded"},
            {"task_id": task["id"], "run_id": run["id"],
             "status": "failed", "error": "dup"},
        ]},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    assert task_detail(client, task["id"])["status"] == "running"

    # A duplicate named alongside an otherwise-completable other task also
    # rolls the entire batch back.
    response = client.post(
        BATCH_PATH,
        json={"runs": [
            {"task_id": other["id"], "run_id": other_run["id"],
             "status": "succeeded"},
            {"task_id": task["id"], "run_id": run["id"], "status": "succeeded"},
            {"task_id": task["id"], "run_id": run["id"],
             "status": "failed", "error": "dup"},
        ]},
    )
    assert response.status_code == 409
    assert task_detail(client, task["id"])["status"] == "running"
    assert task_detail(client, other["id"])["status"] == "running"


def test_batch_already_finished_run_is_409_and_atomic(client: TestClient) -> None:
    create_dataset_and_version(client)
    first, first_run = running_task(client, "first")
    second, second_run = running_task(client, "second", max_attempts=2)

    # Finish the first run through the single-item endpoint beforehand.
    finish_run(client, first["id"], first_run["id"], {"status": "succeeded"})
    before_second = task_detail(client, second["id"])

    response = client.post(
        BATCH_PATH,
        json={"runs": [
            {"task_id": first["id"], "run_id": first_run["id"],
             "status": "failed", "error": "too late"},
            {"task_id": second["id"], "run_id": second_run["id"],
             "status": "succeeded"},
        ]},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"

    # Nothing in the batch was committed: the finished run keeps its original
    # terminal state and the second run is untouched.
    first_detail = task_detail(client, first["id"])
    assert first_detail["status"] == "succeeded"
    assert first_detail["runs"][0]["status"] == "succeeded"
    assert first_detail["runs"][0]["error"] is None
    after_second = task_detail(client, second["id"])
    assert after_second == before_second
    assert after_second["runs"][0]["status"] == "running"
    assert after_second["runs"][0]["finished_at"] is None


def test_batch_semantic_failure_rolls_back_earlier_items(client: TestClient) -> None:
    create_dataset_and_version(client)
    good, good_run = running_task(client, "good")
    bad, bad_run = running_task(client, "bad", max_attempts=2)

    # The second item fails payload semantics: the whole batch must validate
    # before any write even though the first item is perfectly valid.
    response = client.post(
        BATCH_PATH,
        json={"runs": [
            {"task_id": good["id"], "run_id": good_run["id"], "status": "succeeded"},
            {"task_id": bad["id"], "run_id": bad_run["id"], "status": "failed"},
        ]},
    )
    assert response.status_code == 422
    assert task_detail(client, good["id"])["status"] == "running"
    assert task_detail(client, bad["id"])["status"] == "running"
    for task in (good, bad):
        detail = task_detail(client, task["id"])
        assert detail["runs"][0]["finished_at"] is None
        assert detail["attempt_count"] == 1


def test_batch_old_finished_run_while_task_retryable_is_409(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task, first_run = running_task(client, "flaky", max_attempts=3)
    finish_run(
        client, task["id"], first_run["id"],
        {"status": "failed", "error": "boom"},
    )
    second_run = run_task(client, task["id"])

    # Referencing the ended attempt while the task runs its retry is a 409;
    # the current run must stay running.
    response = client.post(
        BATCH_PATH,
        json={"runs": [
            {"task_id": task["id"], "run_id": first_run["id"],
             "status": "succeeded"},
        ]},
    )
    assert response.status_code == 409
    detail = task_detail(client, task["id"])
    assert detail["status"] == "running"
    assert [run["status"] for run in detail["runs"]] == ["failed", "running"]
    assert detail["runs"][1]["finished_at"] is None

    response = client.post(
        BATCH_PATH,
        json={"runs": [
            {"task_id": task["id"], "run_id": second_run["id"],
             "status": "succeeded"},
        ]},
    )
    assert response.status_code == 200


def test_batch_task_not_currently_completable_is_409(client: TestClient) -> None:
    create_dataset_and_version(client)
    task, run = running_task(client, "ingest")

    # Corrupt the invariant directly: run still running but task moved back to
    # pending. The endpoint must refuse the completion and write nothing.
    db_path = os.environ["DATA_LINEAGE_DB"]
    direct = sqlite3.connect(db_path)
    try:
        direct.execute(
            "UPDATE processing_tasks SET status = 'pending' WHERE id = ?",
            (task["id"],),
        )
        direct.commit()
    finally:
        direct.close()

    response = client.post(
        BATCH_PATH,
        json={"runs": [
            {"task_id": task["id"], "run_id": run["id"], "status": "succeeded"},
        ]},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"

    direct = sqlite3.connect(db_path)
    try:
        stored_task = direct.execute(
            "SELECT status FROM processing_tasks WHERE id = ?", (task["id"],)
        ).fetchone()
        stored_run = direct.execute(
            "SELECT status, finished_at FROM processing_task_runs WHERE id = ?",
            (run["id"],),
        ).fetchone()
    finally:
        direct.close()
    assert stored_task[0] == "pending"
    assert stored_run[0] == "running"
    assert stored_run[1] is None


# --------------------------------------------------------------------------- #
# Audit records, schedule and report
# --------------------------------------------------------------------------- #


def test_batch_completion_seen_by_audit_records(client: TestClient) -> None:
    create_dataset_and_version(client)
    task, run = running_task(client, "ingest", max_attempts=2)
    audit_url = f"{TASKS_PATH}/{task['id']}/runs/{run['id']}/audit-records"

    before = client.post(
        audit_url,
        json={"event": "e1", "input_summary": "in", "result_summary": "running"},
    )
    assert before.status_code == 201
    assert before.json()["run_status"] == "running"

    response = client.post(
        BATCH_PATH,
        json={"runs": [
            {"task_id": task["id"], "run_id": run["id"],
             "status": "failed", "error": "boom"},
        ]},
    )
    assert response.status_code == 200

    after = client.post(
        audit_url,
        json={"event": "e2", "input_summary": "in", "result_summary": "failed"},
    )
    assert after.status_code == 201, after.text
    # The record captures the run status at the write instant.
    assert after.json()["run_status"] == "failed"
    assert after.json()["sequence"] == 2
    assert after.json()["previous_hash"] == before.json()["evidence_hash"]

    verify = client.get(f"{audit_url}/verify")
    assert verify.status_code == 200
    assert verify.json()["valid"] is True
    assert verify.json()["checked_count"] == 2


def test_batch_results_reflected_in_schedule_and_report(client: TestClient) -> None:
    create_dataset_and_version(client)
    retryable, retryable_run = running_task(client, "retryable", max_attempts=2)
    exhausted, exhausted_run = running_task(client, "exhausted", max_attempts=1)
    succeeded, succeeded_run = running_task(client, "succeeded")
    dependent = make_task(
        client, "dependent", depends_on=[succeeded["id"]]
    )

    response = client.post(
        BATCH_PATH,
        json={"runs": [
            {"task_id": retryable["id"], "run_id": retryable_run["id"],
             "status": "failed", "error": "again"},
            {"task_id": exhausted["id"], "run_id": exhausted_run["id"],
             "status": "failed", "error": "done"},
            {"task_id": succeeded["id"], "run_id": succeeded_run["id"],
             "status": "succeeded"},
        ]},
    )
    assert response.status_code == 200, response.text

    schedule = client.get(f"{TASKS_PATH}/schedule").json()
    by_id = {task["id"]: task for task in schedule["tasks"]}
    assert by_id[retryable["id"]]["schedule_state"] == "retryable"
    assert by_id[exhausted["id"]]["schedule_state"] == "exhausted"
    assert by_id[succeeded["id"]]["schedule_state"] == "succeeded"
    # The dependency completed by the batch is now satisfied.
    assert by_id[dependent["id"]]["schedule_state"] == "ready"
    assert by_id[dependent["id"]]["blocking_task_ids"] == []

    report = client.get(f"{TASKS_PATH}/audit-report")
    assert report.status_code == 200, report.text
    summary = report.json()["summary"]
    assert summary["task_count"] == 4
    assert summary["run_count"] == 3
    assert summary["succeeded_tasks"] == 1
    assert summary["failed_tasks"] == 2
    assert summary["exhausted_tasks"] == 1
    assert summary["invalid_audit_runs"] == 0
    report_by_id = {task["id"]: task for task in report.json()["tasks"]}
    assert report_by_id[retryable["id"]]["runs"][0]["error"] == "again"
    assert report_by_id[retryable["id"]]["runs"][0]["proof"]["valid"] is True


# --------------------------------------------------------------------------- #
# Concurrency: batch vs finish / cancel / batch / dispatch
# --------------------------------------------------------------------------- #


def test_concurrent_batch_and_finish_have_single_winner(client: TestClient) -> None:
    create_dataset_and_version(client)
    task, run = running_task(client, "ingest")
    url = f"{TASKS_PATH}/{task['id']}/runs/{run['id']}"

    thread_count = 8
    barrier = threading.Barrier(thread_count)
    results: list[tuple[str, int, dict]] = []
    results_lock = threading.Lock()

    def worker(use_batch: bool) -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        if use_batch:
            response = thread_client.post(
                BATCH_PATH,
                json={"runs": [
                    {"task_id": task["id"], "run_id": run["id"],
                     "status": "succeeded"},
                ]},
            )
            kind = "batch"
        else:
            response = thread_client.patch(url, json={"status": "succeeded"})
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
        status_code == 409
        for _kind, status_code, _body in results
        if status_code != 200
    )

    detail = task_detail(client, task["id"])
    assert len(detail["runs"]) == 1
    assert detail["runs"][0]["status"] == "succeeded"
    assert detail["runs"][0]["finished_at"] is not None
    assert detail["attempt_count"] == 1


def test_concurrent_batches_have_single_winner(client: TestClient) -> None:
    create_dataset_and_version(client)
    started = [running_task(client, f"task-{index}") for index in range(4)]
    items = [
        {"task_id": task["id"], "run_id": run["id"], "status": "succeeded"}
        for task, run in started
    ]

    thread_count = 6
    barrier = threading.Barrier(thread_count)
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        response = thread_client.post(BATCH_PATH, json={"runs": items})
        with statuses_lock:
            statuses.append(response.status_code)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses.count(200) == 1, statuses
    assert all(status_code == 409 for status_code in statuses if status_code != 200)

    for task, _run in started:
        detail = task_detail(client, task["id"])
        assert detail["status"] == "succeeded"
        assert len(detail["runs"]) == 1
        assert detail["attempt_count"] == 1
        assert detail["runs"][0]["finished_at"] is not None


def test_concurrent_batch_and_cancel_have_single_winner(client: TestClient) -> None:
    create_dataset_and_version(client)
    task, run = running_task(client, "ingest")
    url = f"{TASKS_PATH}/{task['id']}/runs/{run['id']}"

    barrier = threading.Barrier(2)
    outcomes: dict[str, int] = {}

    def batch_worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        response = thread_client.post(
            BATCH_PATH,
            json={"runs": [
                {"task_id": task["id"], "run_id": run["id"],
                 "status": "succeeded"},
            ]},
        )
        outcomes["batch"] = response.status_code
        if response.status_code == 200:
            outcomes["batch_status"] = response.json()["runs"][0]["status"]

    def cancel_worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        response = thread_client.post(
            f"{url}/cancel", json={"reason": "operator abort"}
        )
        outcomes["cancel"] = response.status_code
        if response.status_code == 200:
            outcomes["cancel_status"] = response.json()["status"]

    threads = [threading.Thread(target=batch_worker),
               threading.Thread(target=cancel_worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted((outcomes["batch"], outcomes["cancel"])) == [200, 409], outcomes
    detail = task_detail(client, task["id"])
    stored = detail["runs"][0]
    assert stored["status"] in ("succeeded", "failed")
    assert stored["finished_at"] is not None
    assert detail["status"] == stored["status"]
    assert detail["attempt_count"] == 1
    assert len(detail["runs"]) == 1
    if stored["status"] == "failed":
        assert stored["error"] == "operator abort"
    else:
        assert stored["error"] is None


def test_concurrent_batch_and_dispatch_do_not_interfere(client: TestClient) -> None:
    create_dataset_and_version(client)
    # target is running and completed by the batch; retryable is failed with
    # attempts left and therefore claimable by dispatch concurrently.
    target, target_run = running_task(client, "target")
    retryable = make_task(client, "retryable", max_attempts=2)
    first = run_task(client, retryable["id"])
    finish_run(
        client, retryable["id"], first["id"],
        {"status": "failed", "error": "boom"},
    )

    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def batch_worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        response = thread_client.post(
            BATCH_PATH,
            json={"runs": [
                {"task_id": target["id"], "run_id": target_run["id"],
                 "status": "succeeded"},
            ]},
        )
        outcomes["batch"] = response.status_code

    def dispatch_worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        response = thread_client.post(DISPATCH_PATH, json={"limit": 10})
        outcomes["dispatch"] = response.status_code
        outcomes["dispatch_runs"] = [
            run["task_id"] for run in response.json()["runs"]
        ]

    threads = [threading.Thread(target=batch_worker),
               threading.Thread(target=dispatch_worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes["batch"] == 200, outcomes
    assert outcomes["dispatch"] == 201, outcomes
    # Dispatch claims only the retryable task, never the batch's task.
    assert outcomes["dispatch_runs"] == [retryable["id"]], outcomes

    target_detail = task_detail(client, target["id"])
    assert target_detail["status"] == "succeeded"
    assert len(target_detail["runs"]) == 1
    retryable_detail = task_detail(client, retryable["id"])
    assert retryable_detail["status"] == "running"
    assert [run["attempt"] for run in retryable_detail["runs"]] == [1, 2]


def test_losing_batch_leaves_no_half_written_state(client: TestClient) -> None:
    create_dataset_and_version(client)
    # One run of the batch is finished by a concurrent single request first;
    # the whole batch must lose with 409 and leave every run untouched.
    first, first_run = running_task(client, "first")
    second, second_run = running_task(client, "second")

    # Simulate the race deterministically: commit the single finish, then fire
    # a batch that covers both runs.
    finish_run(client, first["id"], first_run["id"], {"status": "succeeded"})
    before_second = task_detail(client, second["id"])

    response = client.post(
        BATCH_PATH,
        json={"runs": [
            {"task_id": first["id"], "run_id": first_run["id"],
             "status": "failed", "error": "late"},
            {"task_id": second["id"], "run_id": second_run["id"],
             "status": "succeeded"},
        ]},
    )
    assert response.status_code == 409

    first_detail = task_detail(client, first["id"])
    assert first_detail["runs"][0]["status"] == "succeeded"
    assert first_detail["runs"][0]["error"] is None
    second_detail = task_detail(client, second["id"])
    assert second_detail == before_second
    assert second_detail["runs"][0]["status"] == "running"
    assert second_detail["runs"][0]["finished_at"] is None


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
extract = client.post(base, json={"name": "extract", "max_attempts": 2}).json()
load = client.post(base, json={"name": "load", "depends_on": [extract["id"]]}).json()
run = client.post(f"{base}/{extract['id']}/runs").json()
r = client.post(
    f"{base}/batch-complete",
    json={"runs": [{"task_id": extract["id"], "run_id": run["id"],
                    "status": "failed", "error": "  retry me  "}]},
)
assert r.status_code == 200, r.text
finished = r.json()["runs"][0]
assert finished["status"] == "failed"
assert finished["finished_at"]
print("created")
"""

BATCH_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
base = "/datasets/orders/versions/1/processing-tasks"

tasks = {task["name"]: task for task in client.get(base).json()}
extract = tasks["extract"]
assert extract["status"] == "failed"
assert extract["attempt_count"] == 1

detail = client.get(f"{base}/{extract['id']}")
assert detail.status_code == 200, detail.text
runs = detail.json()["runs"]
assert len(runs) == 1
assert runs[0]["status"] == "failed"
assert runs[0]["finished_at"] is not None

# The batch result is durable and the failed task still has its retry.
started = client.post(f"{base}/{extract['id']}/runs")
assert started.status_code == 201, started.text
assert started.json()["attempt"] == 2

# The completed batch cannot be replayed against the finished first run.
repeat = client.post(
    f"{base}/batch-complete",
    json={"runs": [{"task_id": extract["id"], "run_id": runs[0]["id"],
                    "status": "succeeded"}]},
)
assert repeat.status_code == 409, repeat.text

# Schedule and report read the persisted batch results.
schedule = client.get(f"{base}/schedule").json()
by_id = {item["id"]: item for item in schedule["tasks"]}
assert by_id[extract["id"]]["schedule_state"] == "running"
report = client.get(f"{base}/audit-report").json()
assert report["summary"]["run_count"] == 2
assert report["summary"]["failed_tasks"] == 0
assert report["summary"]["running_tasks"] == 1
assert report["summary"]["pending_tasks"] == 1
print("verified")
"""


def test_batch_completion_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "batch-persist.db"
    assert _run_script(db_path, BATCH_CREATE_SCRIPT) == "created"
    assert _run_script(db_path, BATCH_VERIFY_SCRIPT) == "verified"
