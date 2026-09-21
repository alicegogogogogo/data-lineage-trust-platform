"""Tests for persistent processing tasks and their runs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TASKS_PATH = "/datasets/orders/versions/1/processing-tasks"


def create_dataset_and_version(client: TestClient, dataset: str = "orders") -> None:
    response = client.post("/datasets", json={"name": dataset})
    assert response.status_code == 201, response.text
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201, response.text


def make_task(client: TestClient, name: str, **overrides):
    body = {"name": name, **overrides}
    response = client.post(TASKS_PATH, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def run_task(client: TestClient, task_id: int):
    response = client.post(f"{TASKS_PATH}/{task_id}/runs")
    assert response.status_code == 201, response.text
    return response.json()


def finish_run(client: TestClient, task_id: int, run_id: int, body: dict):
    return client.patch(f"{TASKS_PATH}/{task_id}/runs/{run_id}", json=body)


# --------------------------------------------------------------------------- #
# Task creation
# --------------------------------------------------------------------------- #


def test_create_task_defaults(client: TestClient) -> None:
    create_dataset_and_version(client)
    response = client.post(TASKS_PATH, json={"name": "ingest"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {
        "id",
        "dataset",
        "version",
        "name",
        "depends_on",
        "max_attempts",
        "status",
        "attempt_count",
        "created_at",
    }
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["name"] == "ingest"
    assert body["depends_on"] == []
    assert body["max_attempts"] == 1
    assert body["status"] == "pending"
    assert body["attempt_count"] == 0
    datetime.fromisoformat(body["created_at"])


def test_create_task_with_dependencies_and_attempts(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_task(client, "extract")
    second = make_task(client, "load", depends_on=[first["id"]], max_attempts=3)
    assert second["depends_on"] == [first["id"]]
    assert second["max_attempts"] == 3


def test_create_task_name_is_trimmed_and_unique(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "  ingest  ")
    conflict = client.post(TASKS_PATH, json={"name": "ingest"})
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "conflict"


def test_create_task_rejects_empty_name(client: TestClient) -> None:
    create_dataset_and_version(client)
    for name in ("", "   "):
        response = client.post(TASKS_PATH, json={"name": name})
        assert response.status_code == 422, response.text
        assert response.json()["error"] == "validation_error"
    assert client.get(TASKS_PATH).json() == []


def test_create_task_rejects_invalid_max_attempts(client: TestClient) -> None:
    create_dataset_and_version(client)
    for value in (0, -2, 1.5, "two", True):
        response = client.post(TASKS_PATH, json={"name": "t", "max_attempts": value})
        assert response.status_code == 422, (value, response.text)
    assert client.get(TASKS_PATH).json() == []


def test_create_task_rejects_duplicate_dependency_ids(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_task(client, "extract")
    response = client.post(
        TASKS_PATH,
        json={"name": "load", "depends_on": [first["id"], first["id"]]},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_create_task_rejects_non_integer_dependency_ids(client: TestClient) -> None:
    create_dataset_and_version(client)
    response = client.post(TASKS_PATH, json={"name": "load", "depends_on": ["1"]})
    assert response.status_code == 422
    response = client.post(TASKS_PATH, json={"name": "load", "depends_on": [True]})
    assert response.status_code == 422
    response = client.post(TASKS_PATH, json={"name": "load", "depends_on": [1.5]})
    assert response.status_code == 422


def test_create_task_unknown_dependency_is_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    response = client.post(TASKS_PATH, json={"name": "load", "depends_on": [999]})
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_create_task_dependency_must_be_in_same_version(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_task(client, "extract")
    # A second version of the same dataset cannot depend on v1 tasks.
    response = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201, response.text
    response = client.post(
        "/datasets/orders/versions/2/processing-tasks",
        json={"name": "load", "depends_on": [first["id"]]},
    )
    assert response.status_code == 404


def test_create_task_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    response = client.post(TASKS_PATH, json={"name": "ingest"})
    assert response.status_code == 404
    create_dataset_and_version(client)
    response = client.post(
        "/datasets/orders/versions/9/processing-tasks", json={"name": "ingest"}
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Listing and detail
# --------------------------------------------------------------------------- #


def test_list_tasks_sorted_by_id(client: TestClient) -> None:
    create_dataset_and_version(client)
    names = ["gamma", "alpha", "beta"]
    ids = [make_task(client, name)["id"] for name in names]
    response = client.get(TASKS_PATH)
    assert response.status_code == 200
    body = response.json()
    assert [task["id"] for task in body] == sorted(ids)
    assert [task["name"] for task in body] == ["gamma", "alpha", "beta"]
    assert all("runs" not in task for task in body)


def test_get_task_includes_runs_sorted_by_attempt(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "flaky", max_attempts=3)
    first = run_task(client, task["id"])
    finish_run(client, task["id"], first["id"], {"status": "failed", "error": "boom"})
    second = run_task(client, task["id"])
    finish_run(client, task["id"], second["id"], {"status": "succeeded"})

    response = client.get(f"{TASKS_PATH}/{task['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["attempt_count"] == 2
    assert [run["attempt"] for run in body["runs"]] == [1, 2]
    assert body["runs"][0]["status"] == "failed"
    assert body["runs"][0]["error"] == "boom"
    assert body["runs"][1]["status"] == "succeeded"
    assert body["runs"][1]["error"] is None
    for run in body["runs"]:
        assert set(run) == {
            "id",
            "task_id",
            "attempt",
            "status",
            "started_at",
            "finished_at",
            "error",
        }
        assert run["task_id"] == task["id"]
        datetime.fromisoformat(run["started_at"])
        datetime.fromisoformat(run["finished_at"])


def test_get_task_unknown_ids_are_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    assert client.get(f"{TASKS_PATH}/999").status_code == 404
    # The task exists but not under version 2.
    client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    response = client.get(f"/datasets/orders/versions/2/processing-tasks/{task['id']}")
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Starting runs
# --------------------------------------------------------------------------- #


def test_start_run_marks_task_running(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=2)
    run = run_task(client, task["id"])
    assert run["status"] == "running"
    assert run["attempt"] == 1
    assert run["finished_at"] is None
    assert run["error"] is None

    detail = client.get(f"{TASKS_PATH}/{task['id']}").json()
    assert detail["status"] == "running"
    assert detail["attempt_count"] == 1


def test_start_run_while_running_conflicts(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=2)
    run_task(client, task["id"])
    response = client.post(f"{TASKS_PATH}/{task['id']}/runs")
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    detail = client.get(f"{TASKS_PATH}/{task['id']}").json()
    assert detail["attempt_count"] == 1
    assert len(detail["runs"]) == 1


def test_start_run_after_success_conflicts(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=2)
    run = run_task(client, task["id"])
    finish_run(client, task["id"], run["id"], {"status": "succeeded"})
    response = client.post(f"{TASKS_PATH}/{task['id']}/runs")
    assert response.status_code == 409


def test_start_run_with_unsucceeded_dependency_conflicts(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_task(client, "extract")
    second = make_task(client, "load", depends_on=[first["id"]])

    # Dependency still pending.
    response = client.post(f"{TASKS_PATH}/{second['id']}/runs")
    assert response.status_code == 409

    # Dependency failed: still not startable.
    run = run_task(client, first["id"])
    finish_run(client, first["id"], run["id"], {"status": "failed", "error": "x"})
    response = client.post(f"{TASKS_PATH}/{second['id']}/runs")
    assert response.status_code == 409

    # Dependency succeeded (after a retry with a larger budget is impossible
    # here, so use a fresh dependency chain).
    third = make_task(client, "transform")
    run = run_task(client, third["id"])
    finish_run(client, third["id"], run["id"], {"status": "succeeded"})
    fourth = make_task(client, "publish", depends_on=[third["id"]])
    started = client.post(f"{TASKS_PATH}/{fourth['id']}/runs")
    assert started.status_code == 201, started.text


def test_start_run_attempts_exhausted_conflicts(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "once")
    run = run_task(client, task["id"])
    finish_run(client, task["id"], run["id"], {"status": "failed", "error": "boom"})
    response = client.post(f"{TASKS_PATH}/{task['id']}/runs")
    assert response.status_code == 409
    detail = client.get(f"{TASKS_PATH}/{task['id']}").json()
    assert detail["attempt_count"] == 1
    assert len(detail["runs"]) == 1


def test_failed_task_can_restart_until_attempts_run_out(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "flaky", max_attempts=3)
    for attempt in (1, 2):
        run = run_task(client, task["id"])
        assert run["attempt"] == attempt
        response = finish_run(
            client, task["id"], run["id"], {"status": "failed", "error": "boom"}
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"{TASKS_PATH}/{task['id']}").json()
        assert detail["status"] == "failed"
        assert detail["attempt_count"] == attempt

    third = run_task(client, task["id"])
    assert third["attempt"] == 3
    finish_run(client, task["id"], third["id"], {"status": "succeeded"})
    detail = client.get(f"{TASKS_PATH}/{task['id']}").json()
    assert detail["status"] == "succeeded"
    assert detail["attempt_count"] == 3


def test_start_run_unknown_task_is_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    assert client.post(f"{TASKS_PATH}/999/runs").status_code == 404
    assert (
        client.post("/datasets/ghost/versions/1/processing-tasks/1/runs").status_code
        == 404
    )


# --------------------------------------------------------------------------- #
# Finishing runs
# --------------------------------------------------------------------------- #


def test_finish_run_succeeded(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    run = run_task(client, task["id"])
    response = finish_run(client, task["id"], run["id"], {"status": "succeeded"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["error"] is None
    assert body["finished_at"] is not None
    detail = client.get(f"{TASKS_PATH}/{task['id']}").json()
    assert detail["status"] == "succeeded"


def test_finish_run_failed_requires_error(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=2)
    run = run_task(client, task["id"])
    for body in ({"status": "failed"}, {"status": "failed", "error": ""},
                 {"status": "failed", "error": "   "}):
        response = finish_run(client, task["id"], run["id"], body)
        assert response.status_code == 422, (body, response.text)
    # Nothing was written: the run is still running and can be finished.
    detail = client.get(f"{TASKS_PATH}/{task['id']}").json()
    assert detail["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None

    response = finish_run(
        client, task["id"], run["id"], {"status": "failed", "error": "disk full"}
    )
    assert response.status_code == 200
    assert response.json()["error"] == "disk full"
    assert client.get(f"{TASKS_PATH}/{task['id']}").json()["status"] == "failed"


def test_finish_run_succeeded_rejects_error(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    run = run_task(client, task["id"])
    response = finish_run(
        client, task["id"], run["id"], {"status": "succeeded", "error": "nope"}
    )
    assert response.status_code == 422
    detail = client.get(f"{TASKS_PATH}/{task['id']}").json()
    assert detail["status"] == "running"


def test_finish_run_rejects_unknown_status(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    run = run_task(client, task["id"])
    response = finish_run(client, task["id"], run["id"], {"status": "cancelled"})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_finish_run_twice_conflicts(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest", max_attempts=2)
    run = run_task(client, task["id"])
    finish_run(client, task["id"], run["id"], {"status": "succeeded"})
    response = finish_run(client, task["id"], run["id"], {"status": "succeeded"})
    assert response.status_code == 409


def test_finish_run_of_other_task_is_422(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_task(client, "extract")
    second = make_task(client, "load")
    run = run_task(client, first["id"])
    # The run exists but belongs to another task of the same version.
    response = finish_run(client, second["id"], run["id"], {"status": "succeeded"})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    # The run is untouched.
    detail = client.get(f"{TASKS_PATH}/{first['id']}").json()
    assert detail["runs"][0]["status"] == "running"


def test_finish_run_of_other_version_is_422(client: TestClient) -> None:
    create_dataset_and_version(client)
    task_v1 = make_task(client, "ingest")
    client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    task_v2 = client.post(
        "/datasets/orders/versions/2/processing-tasks", json={"name": "ingest"}
    ).json()
    run = run_task(client, task_v1["id"])
    response = client.patch(
        f"/datasets/orders/versions/2/processing-tasks/{task_v2['id']}/runs/{run['id']}",
        json={"status": "succeeded"},
    )
    assert response.status_code == 422


def test_finish_run_unknown_run_is_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    response = finish_run(client, task["id"], 999, {"status": "succeeded"})
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_finish_run_unknown_task_is_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "ingest")
    run = run_task(client, task["id"])
    response = finish_run(client, 999, run["id"], {"status": "succeeded"})
    assert response.status_code == 404


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
extract = ok(client.post(base, json={"name": "extract", "max_attempts": 2}))
load = ok(client.post(base, json={"name": "load", "depends_on": [extract["id"]]}))
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
assert set(by_name) == {"extract", "load"}
extract = by_name["extract"]
assert extract["status"] == "succeeded"
assert extract["attempt_count"] == 1
assert extract["max_attempts"] == 2
assert by_name["load"]["depends_on"] == [extract["id"]]
assert by_name["load"]["status"] == "pending"

detail = client.get(f"{base}/{extract['id']}")
assert detail.status_code == 200, detail.text
runs = detail.json()["runs"]
assert len(runs) == 1
assert runs[0]["status"] == "succeeded"
assert runs[0]["finished_at"] is not None

# The dependency is satisfied after the restart, so 'load' can start.
started = client.post(f"{base}/{by_name['load']['id']}/runs")
assert started.status_code == 201, started.text
assert started.json()["status"] == "running"
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


def test_tasks_and_runs_survive_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-tasks.db"
    assert _run_script(db_path, CREATE_SCRIPT) == "created"
    assert db_path.exists()
    assert _run_script(db_path, VERIFY_SCRIPT) == "verified"
