"""Tests for per-version processing tasks and their persisted runs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "label", "type": "string", "nullable": True},
]


def make_dataset_with_version(client: TestClient, name: str = "orders") -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions",
        json={"fields": BASE_FIELDS},
    )
    assert response.status_code == 201, response.text


def tasks_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/processing-tasks"


def create_task(client: TestClient, payload: dict, **path: object) -> dict:
    response = client.post(tasks_path(**path), json=payload)  # type: ignore[arg-type]
    assert response.status_code == 201, response.text
    return response.json()


def start_run(client: TestClient, task_id: int, **path: object) -> dict:
    response = client.post(
        f"{tasks_path(**path)}/{task_id}/runs"  # type: ignore[arg-type]
    )
    assert response.status_code == 201, response.text
    return response.json()


def finish_run(client: TestClient, task_id: int, run_id: int, payload: dict) -> dict:
    response = client.patch(
        f"{tasks_path()}/{task_id}/runs/{run_id}", json=payload
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #


def test_create_task_applies_defaults(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.post(tasks_path(), json={"name": "extract"})

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
    assert isinstance(body["id"], int)
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["name"] == "extract"
    assert body["depends_on"] == []
    assert body["max_attempts"] == 1
    assert body["status"] == "pending"
    assert body["attempt_count"] == 0
    datetime.fromisoformat(body["created_at"])


def test_create_task_with_dependencies_and_attempts(client: TestClient) -> None:
    make_dataset_with_version(client)
    first = create_task(client, {"name": "extract"})
    second = create_task(
        client,
        {"name": "load", "depends_on": [first["id"]], "max_attempts": 3},
    )
    assert second["depends_on"] == [first["id"]]
    assert second["max_attempts"] == 3


def test_create_task_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert (
        client.post(tasks_path(dataset="missing"), json={"name": "t"}).status_code
        == 404
    )
    assert (
        client.post(tasks_path(version=99), json={"name": "t"}).status_code == 404
    )


def test_create_task_rejects_invalid_payloads(client: TestClient) -> None:
    make_dataset_with_version(client)
    other = create_task(client, {"name": "other"})

    invalid = [
        {},  # missing name
        {"name": ""},
        {"name": "   "},
        {"name": 7},
        {"name": "t", "max_attempts": 0},
        {"name": "t", "max_attempts": -2},
        {"name": "t", "max_attempts": "2"},
        {"name": "t", "max_attempts": True},
        {"name": "t", "depends_on": "x"},
        {"name": "t", "depends_on": [other["id"], other["id"]]},
        {"name": "t", "depends_on": [9999]},
        {"name": "t", "depends_on": [-1]},
        {"name": "t", "surprise": 1},
    ]
    for payload in invalid:
        response = client.post(tasks_path(), json=payload)
        assert response.status_code == 422, (payload, response.text)
        assert response.json()["error"] == "validation_error"

    # Nothing was written by any of the rejected payloads.
    assert [task["name"] for task in client.get(tasks_path()).json()] == ["other"]


def test_create_task_dependency_must_be_in_same_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    task_v1 = create_task(client, {"name": "extract"})
    # A second version of the same dataset.
    response = client.post(
        "/datasets/orders/versions", json={"fields": BASE_FIELDS}
    )
    assert response.status_code == 201

    response = client.post(
        tasks_path(version=2),
        json={"name": "load", "depends_on": [task_v1["id"]]},
    )
    assert response.status_code == 422


def test_create_task_duplicate_name_conflicts(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_task(client, {"name": "extract"})

    response = client.post(tasks_path(), json={"name": "extract"})
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"

    # The same name is still available in another version.
    client.post("/datasets/orders/versions", json={"fields": BASE_FIELDS})
    assert (
        client.post(tasks_path(version=2), json={"name": "extract"}).status_code
        == 201
    )


# --------------------------------------------------------------------------- #
# Listing and detail
# --------------------------------------------------------------------------- #


def test_list_tasks_ordered_by_id(client: TestClient) -> None:
    make_dataset_with_version(client)
    names = ["gamma", "alpha", "beta"]
    for name in names:
        create_task(client, {"name": name})

    response = client.get(tasks_path())
    assert response.status_code == 200
    body = response.json()
    assert [task["name"] for task in body] == names
    assert [task["id"] for task in body] == sorted(task["id"] for task in body)
    assert all("runs" not in task for task in body)


def test_get_task_includes_runs_ordered_by_attempt(client: TestClient) -> None:
    make_dataset_with_version(client)
    task = create_task(client, {"name": "flaky", "max_attempts": 2})

    first = start_run(client, task["id"])
    finish_run(client, task["id"], first["id"], {"status": "failed", "error": "boom"})
    second = start_run(client, task["id"])
    finish_run(client, task["id"], second["id"], {"status": "succeeded"})

    response = client.get(f"{tasks_path()}/{task['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["attempt_count"] == 2
    assert [run["attempt"] for run in body["runs"]] == [1, 2]
    run = body["runs"][0]
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
    assert run["status"] == "failed"
    assert run["error"] == "boom"
    datetime.fromisoformat(run["started_at"])
    datetime.fromisoformat(run["finished_at"])
    assert body["runs"][1]["status"] == "succeeded"
    assert body["runs"][1]["error"] is None


def test_get_task_unknown_ids_are_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    task = create_task(client, {"name": "extract"})

    assert client.get(f"{tasks_path()}/9999").status_code == 404
    assert (
        client.get(f"{tasks_path(dataset='missing')}/{task['id']}").status_code
        == 404
    )
    assert client.get(f"{tasks_path(version=7)}/{task['id']}").status_code == 404


# --------------------------------------------------------------------------- #
# Starting runs
# --------------------------------------------------------------------------- #


def test_start_run_marks_task_running(client: TestClient) -> None:
    make_dataset_with_version(client)
    task = create_task(client, {"name": "extract", "max_attempts": 2})

    response = client.post(f"{tasks_path()}/{task['id']}/runs")
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["task_id"] == task["id"]
    assert run["attempt"] == 1
    assert run["status"] == "running"
    assert run["finished_at"] is None
    assert run["error"] is None
    datetime.fromisoformat(run["started_at"])

    detail = client.get(f"{tasks_path()}/{task['id']}").json()
    assert detail["status"] == "running"
    assert detail["attempt_count"] == 1


def test_start_run_requires_pending_or_failed_task(client: TestClient) -> None:
    make_dataset_with_version(client)
    task = create_task(client, {"name": "extract", "max_attempts": 3})

    start_run(client, task["id"])
    # Already running.
    response = client.post(f"{tasks_path()}/{task['id']}/runs")
    assert response.status_code == 409

    detail = client.get(f"{tasks_path()}/{task['id']}").json()
    assert detail["attempt_count"] == 1
    assert len(detail["runs"]) == 1


def test_start_run_on_succeeded_task_conflicts(client: TestClient) -> None:
    make_dataset_with_version(client)
    task = create_task(client, {"name": "extract", "max_attempts": 2})
    run = start_run(client, task["id"])
    finish_run(client, task["id"], run["id"], {"status": "succeeded"})

    assert client.post(f"{tasks_path()}/{task['id']}/runs").status_code == 409


def test_start_run_exhausted_attempts_conflict(client: TestClient) -> None:
    make_dataset_with_version(client)
    task = create_task(client, {"name": "extract"})  # max_attempts defaults to 1
    run = start_run(client, task["id"])
    finish_run(client, task["id"], run["id"], {"status": "failed", "error": "x"})

    response = client.post(f"{tasks_path()}/{task['id']}/runs")
    assert response.status_code == 409
    detail = client.get(f"{tasks_path()}/{task['id']}").json()
    assert detail["attempt_count"] == 1
    assert detail["status"] == "failed"


def test_start_run_waits_for_dependencies(client: TestClient) -> None:
    make_dataset_with_version(client)
    first = create_task(client, {"name": "extract"})
    second = create_task(
        client, {"name": "load", "depends_on": [first["id"]], "max_attempts": 2}
    )

    # Dependency still pending.
    response = client.post(f"{tasks_path()}/{second['id']}/runs")
    assert response.status_code == 409
    assert client.get(f"{tasks_path()}/{second['id']}").json()["attempt_count"] == 0

    # Dependency running is not enough.
    first_run = start_run(client, first["id"])
    assert client.post(f"{tasks_path()}/{second['id']}/runs").status_code == 409

    # A failed dependency does not unblock either.
    finish_run(client, first["id"], first_run["id"], {"status": "failed", "error": "e"})
    assert client.post(f"{tasks_path()}/{second['id']}/runs").status_code == 409
    assert client.get(f"{tasks_path()}/{second['id']}").json()["attempt_count"] == 0


def test_start_run_after_dependency_succeeds(client: TestClient) -> None:
    make_dataset_with_version(client)
    first = create_task(client, {"name": "extract"})
    second = create_task(client, {"name": "load", "depends_on": [first["id"]]})

    run = start_run(client, first["id"])
    finish_run(client, first["id"], run["id"], {"status": "succeeded"})

    dependent_run = start_run(client, second["id"])
    assert dependent_run["status"] == "running"


def test_start_run_unknown_task_is_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert client.post(f"{tasks_path()}/9999/runs").status_code == 404
    assert client.post(f"{tasks_path(dataset='missing')}/1/runs").status_code == 404


# --------------------------------------------------------------------------- #
# Finishing runs
# --------------------------------------------------------------------------- #


def test_finish_run_succeeded(client: TestClient) -> None:
    make_dataset_with_version(client)
    task = create_task(client, {"name": "extract"})
    run = start_run(client, task["id"])

    response = client.patch(
        f"{tasks_path()}/{task['id']}/runs/{run['id']}",
        json={"status": "succeeded"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == run["id"]
    assert body["status"] == "succeeded"
    assert body["error"] is None
    datetime.fromisoformat(body["finished_at"])

    detail = client.get(f"{tasks_path()}/{task['id']}").json()
    assert detail["status"] == "succeeded"


def test_finish_run_failed_allows_restart_within_attempts(client: TestClient) -> None:
    make_dataset_with_version(client)
    task = create_task(client, {"name": "flaky", "max_attempts": 2})

    first = start_run(client, task["id"])
    finished = finish_run(
        client, task["id"], first["id"], {"status": "failed", "error": "timeout"}
    )
    assert finished["error"] == "timeout"
    assert client.get(f"{tasks_path()}/{task['id']}").json()["status"] == "failed"

    # One attempt remains, so the task can be restarted.
    second = start_run(client, task["id"])
    assert second["attempt"] == 2
    finish_run(client, task["id"], second["id"], {"status": "succeeded"})
    detail = client.get(f"{tasks_path()}/{task['id']}").json()
    assert detail["status"] == "succeeded"
    assert detail["attempt_count"] == 2


def test_finish_run_rejects_invalid_payloads(client: TestClient) -> None:
    make_dataset_with_version(client)
    task = create_task(client, {"name": "extract", "max_attempts": 2})
    run = start_run(client, task["id"])

    invalid = [
        {},
        {"status": "running"},
        {"status": "pending"},
        {"status": "failed"},
        {"status": "failed", "error": ""},
        {"status": "failed", "error": "   "},
        {"status": "failed", "error": 5},
        {"status": "succeeded", "error": "not allowed"},
        {"status": "succeeded", "extra": True},
    ]
    for payload in invalid:
        response = client.patch(
            f"{tasks_path()}/{task['id']}/runs/{run['id']}", json=payload
        )
        assert response.status_code == 422, (payload, response.text)

    # The run is still running and the task unchanged.
    detail = client.get(f"{tasks_path()}/{task['id']}").json()
    assert detail["status"] == "running"
    assert detail["runs"][0]["finished_at"] is None


def test_finish_run_only_current_running_run(client: TestClient) -> None:
    make_dataset_with_version(client)
    task = create_task(client, {"name": "extract", "max_attempts": 2})
    first = start_run(client, task["id"])
    finish_run(client, task["id"], first["id"], {"status": "failed", "error": "x"})

    # The finished run cannot be finished again.
    response = client.patch(
        f"{tasks_path()}/{task['id']}/runs/{first['id']}",
        json={"status": "succeeded"},
    )
    assert response.status_code == 409


def test_finish_run_scoping(client: TestClient) -> None:
    make_dataset_with_version(client)
    first = create_task(client, {"name": "one"})
    second = create_task(client, {"name": "two"})
    run = start_run(client, first["id"])

    # Unknown run id.
    assert (
        client.patch(
            f"{tasks_path()}/{first['id']}/runs/9999",
            json={"status": "succeeded"},
        ).status_code
        == 404
    )
    # The run belongs to another task of the same version.
    response = client.patch(
        f"{tasks_path()}/{second['id']}/runs/{run['id']}",
        json={"status": "succeeded"},
    )
    assert response.status_code == 422
    # Unknown task in the path.
    assert (
        client.patch(
            f"{tasks_path()}/9999/runs/{run['id']}",
            json={"status": "succeeded"},
        ).status_code
        == 404
    )
    # Unknown dataset.
    assert (
        client.patch(
            f"{tasks_path(dataset='missing')}/{first['id']}/runs/{run['id']}",
            json={"status": "succeeded"},
        ).status_code
        == 404
    )

    # Nothing was written.
    detail = client.get(f"{tasks_path()}/{first['id']}").json()
    assert detail["status"] == "running"
    assert detail["runs"][0]["status"] == "running"


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #

CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

assert client.post("/datasets", json={"name": "orders"}).status_code == 201
assert client.post(
    "/datasets/orders/versions",
    json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
).status_code == 201

base = "/datasets/orders/versions/1/processing-tasks"
extract = client.post(base, json={"name": "extract", "max_attempts": 2}).json()
load = client.post(
    base, json={"name": "load", "depends_on": [extract["id"]]}
).json()

run = client.post(f"{base}/{extract['id']}/runs").json()
assert client.patch(
    f"{base}/{extract['id']}/runs/{run['id']}",
    json={"status": "failed", "error": "disk full"},
).status_code == 200
print("created")
"""

VERIFY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
base = "/datasets/orders/versions/1/processing-tasks"

tasks = client.get(base).json()
assert [t["name"] for t in tasks] == ["extract", "load"]
extract, load = tasks
assert extract["status"] == "failed"
assert extract["attempt_count"] == 1
assert extract["max_attempts"] == 2
assert load["depends_on"] == [extract["id"]]
assert load["status"] == "pending"

detail = client.get(f"{base}/{extract['id']}").json()
assert len(detail["runs"]) == 1
run = detail["runs"][0]
assert run["attempt"] == 1
assert run["status"] == "failed"
assert run["error"] == "disk full"
assert run["finished_at"] is not None

# The failed task still has one attempt left after the restart; finishing it
# successfully unblocks the dependent task.
second = client.post(f"{base}/{extract['id']}/runs")
assert second.status_code == 201, second.text
assert second.json()["attempt"] == 2
assert client.patch(
    f"{base}/{extract['id']}/runs/{second.json()['id']}",
    json={"status": "succeeded"},
).status_code == 200
dependent = client.post(f"{base}/{load['id']}/runs")
assert dependent.status_code == 201, dependent.text
print(json.dumps({"tasks": len(tasks)}))
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
    db_path = tmp_path / "processing-tasks.db"

    assert _run_script(db_path, CREATE_SCRIPT) == "created"
    assert db_path.exists()

    output = _run_script(db_path, VERIFY_SCRIPT)
    assert json.loads(output)["tasks"] == 2
