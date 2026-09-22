"""Tests for processing-task dependency editing and the schedule view."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TASKS_PATH = "/datasets/orders/versions/1/processing-tasks"
SCHEDULE_PATH = f"{TASKS_PATH}/schedule"


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


def put_dependencies(client: TestClient, task_id: int, depends_on):
    return client.put(
        f"{TASKS_PATH}/{task_id}/dependencies", json={"depends_on": depends_on}
    )


def run_task(client: TestClient, task_id: int):
    response = client.post(f"{TASKS_PATH}/{task_id}/runs")
    assert response.status_code == 201, response.text
    return response.json()


def finish_run(client: TestClient, task_id: int, run_id: int, body: dict):
    response = client.patch(f"{TASKS_PATH}/{task_id}/runs/{run_id}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def succeed_task(client: TestClient, task_id: int) -> None:
    run = run_task(client, task_id)
    finish_run(client, task_id, run["id"], {"status": "succeeded"})


def fail_task(client: TestClient, task_id: int) -> None:
    run = run_task(client, task_id)
    finish_run(client, task_id, run["id"], {"status": "failed", "error": "boom"})


def schedule_by_name(client: TestClient) -> dict[str, dict]:
    response = client.get(SCHEDULE_PATH)
    assert response.status_code == 200, response.text
    return {task["name"]: task for task in response.json()["tasks"]}


# --------------------------------------------------------------------------- #
# Replacing dependencies
# --------------------------------------------------------------------------- #


def test_put_dependencies_replaces_set_and_returns_task(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_task(client, "extract")
    second = make_task(client, "transform")
    third = make_task(client, "load", depends_on=[first["id"]])

    response = put_dependencies(client, third["id"], [second["id"], first["id"]])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == third["id"]
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["name"] == "load"
    assert body["depends_on"] == [second["id"], first["id"]]
    assert body["status"] == "pending"
    assert "runs" not in body

    # The change is visible through the existing task endpoints.
    assert client.get(f"{TASKS_PATH}/{third['id']}").json()["depends_on"] == [
        second["id"],
        first["id"],
    ]


def test_put_dependencies_empty_array_clears(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_task(client, "extract")
    second = make_task(client, "load", depends_on=[first["id"]])

    response = put_dependencies(client, second["id"], [])
    assert response.status_code == 200, response.text
    assert response.json()["depends_on"] == []
    assert client.get(f"{TASKS_PATH}/{second['id']}").json()["depends_on"] == []


def test_put_dependencies_unknown_resources_are_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "load")

    assert (
        client.put(
            "/datasets/ghost/versions/1/processing-tasks/1/dependencies",
            json={"depends_on": []},
        ).status_code
        == 404
    )
    assert (
        client.put(
            "/datasets/orders/versions/9/processing-tasks/1/dependencies",
            json={"depends_on": []},
        ).status_code
        == 404
    )
    assert (
        client.put(
            f"{TASKS_PATH}/999/dependencies", json={"depends_on": []}
        ).status_code
        == 404
    )
    # Unknown dependency id inside the body.
    response = put_dependencies(client, task["id"], [999])
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    # Nothing was written.
    assert client.get(f"{TASKS_PATH}/{task['id']}").json()["depends_on"] == []


def test_put_dependencies_rejects_invalid_body_without_writing(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    first = make_task(client, "extract")
    second = make_task(client, "load", depends_on=[first["id"]])

    invalid_bodies = (
        {},
        {"depends_on": None},
        {"depends_on": "1"},
        {"depends_on": [first["id"], first["id"]]},
        {"depends_on": ["1"]},
        {"depends_on": [True]},
        {"depends_on": [1.5]},
        {"depends_on": [], "extra": 1},
    )
    for body in invalid_bodies:
        response = client.put(
            f"{TASKS_PATH}/{second['id']}/dependencies", json=body
        )
        assert response.status_code == 422, (body, response.text)
        assert response.json()["error"] == "validation_error"

    # Not JSON at all.
    response = client.put(
        f"{TASKS_PATH}/{second['id']}/dependencies",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422

    # The original dependency set survived every rejected request.
    assert client.get(f"{TASKS_PATH}/{second['id']}").json()["depends_on"] == [
        first["id"]
    ]


def test_put_dependencies_self_dependency_conflicts(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "load")
    response = put_dependencies(client, task["id"], [task["id"]])
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    assert client.get(f"{TASKS_PATH}/{task['id']}").json()["depends_on"] == []


def test_put_dependencies_cycle_conflicts(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_task(client, "extract")
    second = make_task(client, "transform", depends_on=[first["id"]])
    third = make_task(client, "load", depends_on=[second["id"]])

    # Direct two-task cycle.
    response = put_dependencies(client, first["id"], [second["id"]])
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"

    # Longer cycle through the whole chain.
    response = put_dependencies(client, first["id"], [third["id"]])
    assert response.status_code == 409

    # Both rejections left the graph untouched.
    assert client.get(f"{TASKS_PATH}/{first['id']}").json()["depends_on"] == []


def test_put_dependencies_non_pending_target_conflicts(client: TestClient) -> None:
    create_dataset_and_version(client)
    dependency = make_task(client, "extract")

    running = make_task(client, "running", max_attempts=2)
    run_task(client, running["id"])
    response = put_dependencies(client, running["id"], [dependency["id"]])
    assert response.status_code == 409

    succeeded = make_task(client, "succeeded")
    succeed_task(client, succeeded["id"])
    response = put_dependencies(client, succeeded["id"], [dependency["id"]])
    assert response.status_code == 409

    failed = make_task(client, "failed")
    fail_task(client, failed["id"])
    response = put_dependencies(client, failed["id"], [dependency["id"]])
    assert response.status_code == 409

    for task in (running, succeeded, failed):
        assert client.get(f"{TASKS_PATH}/{task['id']}").json()["depends_on"] == []


def test_updated_dependencies_drive_run_start_rules(client: TestClient) -> None:
    create_dataset_and_version(client)
    gate = make_task(client, "gate")
    task = make_task(client, "load")

    # No dependency yet: the task can start. Re-create the scenario with a
    # dependency added instead.
    response = put_dependencies(client, task["id"], [gate["id"]])
    assert response.status_code == 200
    assert client.post(f"{TASKS_PATH}/{task['id']}/runs").status_code == 409

    # Clearing the dependency makes the task startable again.
    response = put_dependencies(client, task["id"], [])
    assert response.status_code == 200
    assert client.post(f"{TASKS_PATH}/{task['id']}/runs").status_code == 201


# --------------------------------------------------------------------------- #
# Schedule view
# --------------------------------------------------------------------------- #


def test_schedule_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    assert client.get(SCHEDULE_PATH).status_code == 404
    create_dataset_and_version(client)
    assert (
        client.get("/datasets/orders/versions/9/processing-tasks/schedule").status_code
        == 404
    )


def test_schedule_empty_version(client: TestClient) -> None:
    create_dataset_and_version(client)
    response = client.get(SCHEDULE_PATH)
    assert response.status_code == 200, response.text
    assert response.json() == {"dataset": "orders", "version": 1, "tasks": []}


def test_schedule_states_and_blocking_ids(client: TestClient) -> None:
    create_dataset_and_version(client)
    extract = make_task(client, "extract")
    transform = make_task(client, "transform", depends_on=[extract["id"]])
    publish = make_task(
        client, "publish", depends_on=[transform["id"], extract["id"]]
    )
    standalone = make_task(client, "standalone")

    schedule = schedule_by_name(client)
    # Tasks are sorted by id and carry the existing fields plus the new ones.
    response = client.get(SCHEDULE_PATH).json()
    assert [task["id"] for task in response["tasks"]] == sorted(
        task["id"] for task in response["tasks"]
    )
    for task in response["tasks"]:
        assert {
            "id",
            "dataset",
            "version",
            "name",
            "depends_on",
            "max_attempts",
            "status",
            "attempt_count",
            "created_at",
            "schedule_state",
            "blocking_task_ids",
        } <= set(task)

    assert schedule["extract"]["schedule_state"] == "ready"
    assert schedule["extract"]["blocking_task_ids"] == []
    assert schedule["standalone"]["schedule_state"] == "ready"
    assert schedule["transform"]["schedule_state"] == "blocked"
    assert schedule["transform"]["blocking_task_ids"] == [extract["id"]]
    assert schedule["publish"]["schedule_state"] == "blocked"
    assert schedule["publish"]["blocking_task_ids"] == [extract["id"], transform["id"]]

    # Running and succeeded tasks report their own state and never block.
    run = run_task(client, extract["id"])
    schedule = schedule_by_name(client)
    assert schedule["extract"]["schedule_state"] == "running"
    assert schedule["extract"]["blocking_task_ids"] == []
    finish_run(client, extract["id"], run["id"], {"status": "succeeded"})

    schedule = schedule_by_name(client)
    assert schedule["extract"]["schedule_state"] == "succeeded"
    assert schedule["extract"]["blocking_task_ids"] == []
    assert schedule["transform"]["schedule_state"] == "ready"
    assert schedule["transform"]["blocking_task_ids"] == []
    assert schedule["publish"]["schedule_state"] == "blocked"
    assert schedule["publish"]["blocking_task_ids"] == [transform["id"]]


def test_schedule_failed_retryable_and_exhausted(client: TestClient) -> None:
    create_dataset_and_version(client)
    flaky = make_task(client, "flaky", max_attempts=2)
    once = make_task(client, "once")

    fail_task(client, flaky["id"])
    fail_task(client, once["id"])

    schedule = schedule_by_name(client)
    assert schedule["flaky"]["schedule_state"] == "retryable"
    assert schedule["flaky"]["blocking_task_ids"] == []
    assert schedule["once"]["schedule_state"] == "exhausted"
    assert schedule["once"]["blocking_task_ids"] == []


def test_schedule_upstream_failed_propagates(client: TestClient) -> None:
    create_dataset_and_version(client)
    root = make_task(client, "root")
    middle = make_task(client, "middle", depends_on=[root["id"]])
    leaf = make_task(client, "leaf", depends_on=[middle["id"]])
    retryable_root = make_task(client, "retryable-root", max_attempts=2)
    child = make_task(client, "child", depends_on=[retryable_root["id"]])

    fail_task(client, root["id"])  # exhausted (max_attempts=1)
    fail_task(client, retryable_root["id"])  # still retryable

    schedule = schedule_by_name(client)
    assert schedule["root"]["schedule_state"] == "exhausted"
    # Direct and transitive dependents of an exhausted task are upstream_failed.
    assert schedule["middle"]["schedule_state"] == "upstream_failed"
    assert schedule["middle"]["blocking_task_ids"] == [root["id"]]
    assert schedule["leaf"]["schedule_state"] == "upstream_failed"
    assert schedule["leaf"]["blocking_task_ids"] == [middle["id"]]
    # A retryable dependency only blocks.
    assert schedule["retryable-root"]["schedule_state"] == "retryable"
    assert schedule["child"]["schedule_state"] == "blocked"
    assert schedule["child"]["blocking_task_ids"] == [retryable_root["id"]]


def test_schedule_recomputes_after_successful_retry(client: TestClient) -> None:
    create_dataset_and_version(client)
    flaky = make_task(client, "flaky", max_attempts=2)
    dependent = make_task(client, "dependent", depends_on=[flaky["id"]])

    fail_task(client, flaky["id"])
    schedule = schedule_by_name(client)
    assert schedule["flaky"]["schedule_state"] == "retryable"
    assert schedule["dependent"]["schedule_state"] == "blocked"

    # The retry succeeds and the dependent becomes ready.
    succeed_task(client, flaky["id"])
    schedule = schedule_by_name(client)
    assert schedule["flaky"]["schedule_state"] == "succeeded"
    assert schedule["dependent"]["schedule_state"] == "ready"
    assert schedule["dependent"]["blocking_task_ids"] == []


def test_schedule_reflects_edited_dependencies(client: TestClient) -> None:
    create_dataset_and_version(client)
    gate = make_task(client, "gate")
    task = make_task(client, "task")

    assert schedule_by_name(client)["task"]["schedule_state"] == "ready"

    put_dependencies(client, task["id"], [gate["id"]])
    schedule = schedule_by_name(client)
    assert schedule["task"]["schedule_state"] == "blocked"
    assert schedule["task"]["blocking_task_ids"] == [gate["id"]]

    succeed_task(client, gate["id"])
    schedule = schedule_by_name(client)
    assert schedule["task"]["schedule_state"] == "ready"
    assert schedule["task"]["blocking_task_ids"] == []


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
extract = ok(client.post(base, json={"name": "extract"}))
load = ok(client.post(base, json={"name": "load"}))
# Edit the dependency graph after creation, then finish the dependency.
ok(client.put(
    f"{base}/{load['id']}/dependencies", json={"depends_on": [extract["id"]]}
), status=200)
run = ok(client.post(f"{base}/{extract['id']}/runs"))
ok(client.patch(
    f"{base}/{extract['id']}/runs/{run['id']}", json={"status": "succeeded"}
), status=200)
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
extract = by_name["extract"]
load = by_name["load"]
# The edited dependency set survived the restart.
assert load["depends_on"] == [extract["id"]]

# The schedule view is recovered as well.
schedule = client.get(f"{base}/schedule")
assert schedule.status_code == 200, schedule.text
states = {task["name"]: task for task in schedule.json()["tasks"]}
assert states["extract"]["schedule_state"] == "succeeded"
assert states["load"]["schedule_state"] == "ready"
assert states["load"]["blocking_task_ids"] == []

# The start rules use the updated dependencies: 'load' may start now.
started = client.post(f"{base}/{load['id']}/runs")
assert started.status_code == 201, started.text
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


def test_dependencies_and_schedule_survive_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-schedule.db"
    assert _run_script(db_path, CREATE_SCRIPT) == "created"
    assert db_path.exists()
    assert _run_script(db_path, VERIFY_SCRIPT) == "verified"
