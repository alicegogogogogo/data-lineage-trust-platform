"""Tests for worker batch dispatch: POST .../processing-tasks/dispatch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DISPATCH_PATH = "/datasets/orders/versions/1/processing-tasks/dispatch"
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


def finish_run(client: TestClient, task_id: int, run_id: int, body: dict) -> None:
    response = client.patch(
        f"{TASKS_PATH}/{task_id}/runs/{run_id}", json=body
    )
    assert response.status_code == 200, response.text


def dispatch(client: TestClient, body=None, *, raw: bool = False):
    if raw:
        return client.post(
            DISPATCH_PATH,
            content=body,
            headers={"content-type": "application/json"},
        )
    if body is None:
        return client.post(DISPATCH_PATH)
    return client.post(DISPATCH_PATH, json=body)


def task_detail(client: TestClient, task_id: int) -> dict:
    response = client.get(f"{TASKS_PATH}/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Selection rules and response shape
# --------------------------------------------------------------------------- #


def test_dispatch_without_body_defaults_to_one_task(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_task(client, "alpha")
    make_task(client, "beta")

    response = dispatch(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert [run["task_id"] for run in body["runs"]] == [first["id"]]
    run = body["runs"][0]
    assert set(run) == RUN_FIELDS
    assert run["attempt"] == 1
    assert run["status"] == "running"
    assert run["finished_at"] is None
    assert run["error"] is None

    detail = task_detail(client, first["id"])
    assert detail["status"] == "running"
    assert detail["attempt_count"] == 1
    assert len(detail["runs"]) == 1
    assert detail["runs"][0]["id"] == run["id"]


def test_dispatch_empty_object_also_defaults_to_one(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "alpha")
    make_task(client, "beta")
    response = dispatch(client, {})
    assert response.status_code == 201, response.text
    assert len(response.json()["runs"]) == 1


def test_dispatch_selects_by_task_id_ascending_up_to_limit(client: TestClient) -> None:
    create_dataset_and_version(client)
    ids = [make_task(client, name)["id"] for name in ("a", "b", "c", "d")]

    response = dispatch(client, {"limit": 3})
    assert response.status_code == 201, response.text
    runs = response.json()["runs"]
    assert [run["task_id"] for run in runs] == ids[:3]
    task_ids = [run["task_id"] for run in runs]
    assert task_ids == sorted(task_ids)
    assert all(run["attempt"] == 1 for run in runs)
    assert all(set(run) == RUN_FIELDS for run in runs)

    # The one remaining task is still pending and dispatched on the next call.
    response = dispatch(client, {"limit": 3})
    assert [run["task_id"] for run in response.json()["runs"]] == ids[3:]


def test_dispatch_never_returns_more_than_limit(client: TestClient) -> None:
    create_dataset_and_version(client)
    for name in ("a", "b"):
        make_task(client, name)
    response = dispatch(client, {"limit": 1})
    assert len(response.json()["runs"]) == 1


def test_dispatch_with_no_startable_tasks_is_201_empty(client: TestClient) -> None:
    create_dataset_and_version(client)
    response = dispatch(client, {"limit": 5})
    assert response.status_code == 201
    assert response.json() == {"dataset": "orders", "version": 1, "runs": []}

    # Tasks that exist but are all running also yield an empty dispatch.
    task = make_task(client, "alpha")
    run_task(client, task["id"])
    response = dispatch(client, {"limit": 5})
    assert response.status_code == 201
    assert response.json()["runs"] == []


def test_dispatch_skips_running_succeeded_and_exhausted(client: TestClient) -> None:
    create_dataset_and_version(client)
    running = make_task(client, "running")
    run_task(client, running["id"])

    succeeded = make_task(client, "succeeded")
    run = run_task(client, succeeded["id"])
    finish_run(client, succeeded["id"], run["id"], {"status": "succeeded"})

    exhausted = make_task(client, "exhausted")
    run = run_task(client, exhausted["id"])
    finish_run(client, exhausted["id"], run["id"], {"status": "failed", "error": "x"})

    ready = make_task(client, "ready")

    response = dispatch(client, {"limit": 10})
    assert response.status_code == 201, response.text
    assert [run["task_id"] for run in response.json()["runs"]] == [ready["id"]]


def test_dispatch_retries_failed_tasks_with_attempts_left(client: TestClient) -> None:
    create_dataset_and_version(client)
    flaky = make_task(client, "flaky", max_attempts=3)
    for _ in range(2):
        run = run_task(client, flaky["id"])
        finish_run(client, flaky["id"], run["id"], {"status": "failed", "error": "boom"})
    detail = task_detail(client, flaky["id"])
    assert detail["status"] == "failed"
    assert detail["attempt_count"] == 2

    response = dispatch(client, {"limit": 5})
    assert response.status_code == 201, response.text
    runs = response.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["task_id"] == flaky["id"]
    assert runs[0]["attempt"] == 3
    detail = task_detail(client, flaky["id"])
    assert detail["status"] == "running"
    assert detail["attempt_count"] == 3
    assert [run["attempt"] for run in detail["runs"]] == [1, 2, 3]


def test_dispatch_respects_direct_dependencies(client: TestClient) -> None:
    create_dataset_and_version(client)
    upstream = make_task(client, "upstream")
    blocked = make_task(client, "blocked", depends_on=[upstream["id"]])
    independent = make_task(client, "independent")

    # Upstream is pending: the blocked task must not be selected even with a
    # generous limit.
    response = dispatch(client, {"limit": 10})
    assert [run["task_id"] for run in response.json()["runs"]] == [
        upstream["id"],
        independent["id"],
    ]

    # Both are now running; the blocked task still waits. After the upstream
    # succeeds, the blocked task becomes startable.
    upstream_run = task_detail(client, upstream["id"])["runs"][0]
    finish_run(client, upstream["id"], upstream_run["id"], {"status": "succeeded"})
    response = dispatch(client, {"limit": 10})
    assert [run["task_id"] for run in response.json()["runs"]] == [blocked["id"]]


def test_dispatch_does_not_chain_tasks_started_in_same_request(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    upstream = make_task(client, "upstream")
    dependent = make_task(client, "dependent", depends_on=[upstream["id"]])

    # A single request must not start the dependent: the upstream only becomes
    # 'running' here, not 'succeeded'.
    response = dispatch(client, {"limit": 10})
    assert [run["task_id"] for run in response.json()["runs"]] == [upstream["id"]]
    assert task_detail(client, dependent["id"])["status"] == "pending"

    # Even after the upstream succeeds, a limit of 1 starts only one task.
    run = task_detail(client, upstream["id"])["runs"][0]
    finish_run(client, upstream["id"], run["id"], {"status": "succeeded"})
    response = dispatch(client)
    assert [run["task_id"] for run in response.json()["runs"]] == [dependent["id"]]


def test_dispatch_skips_failed_dependency(client: TestClient) -> None:
    create_dataset_and_version(client)
    upstream = make_task(client, "upstream")
    dependent = make_task(client, "dependent", depends_on=[upstream["id"]])
    run = run_task(client, upstream["id"])
    finish_run(client, upstream["id"], run["id"], {"status": "failed", "error": "x"})

    response = dispatch(client, {"limit": 10})
    # The upstream (max_attempts default 1) is exhausted; the dependent is
    # blocked by a non-succeeded dependency: nothing starts.
    assert response.json()["runs"] == []
    assert task_detail(client, dependent["id"])["status"] == "pending"


def test_dispatch_only_considers_tasks_of_the_path_version(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    make_task(client, "v1-task")
    response = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201, response.text
    v2_task = client.post(
        "/datasets/orders/versions/2/processing-tasks", json={"name": "v2-task"}
    ).json()

    response = client.post(
        "/datasets/orders/versions/2/processing-tasks/dispatch",
        json={"limit": 10},
    )
    assert response.status_code == 201, response.text
    assert [run["task_id"] for run in response.json()["runs"]] == [v2_task["id"]]


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_dispatch_rejects_extra_fields_and_non_positive_ints(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "alpha")
    for body in (
        {"limit": 0},
        {"limit": -3},
        {"limit": 1.5},
        {"limit": "2"},
        {"limit": True},
        {"limit": None},
        {"unexpected": 2},
        {"limit": 2, "unexpected": 2},
    ):
        response = dispatch(client, body)
        assert response.status_code == 422, (body, response.text)
        assert response.json()["error"] == "validation_error"

    # Nothing was written: the task is still pending with no runs.
    detail = task_detail(client, task["id"])
    assert detail["status"] == "pending"
    assert detail["attempt_count"] == 0
    assert detail["runs"] == []


def test_dispatch_rejects_malformed_json(client: TestClient) -> None:
    create_dataset_and_version(client)
    response = dispatch(client, "{not json", raw=True)
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


# --------------------------------------------------------------------------- #
# Unknown resources
# --------------------------------------------------------------------------- #


def test_dispatch_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    response = client.post(
        "/datasets/ghost/versions/1/processing-tasks/dispatch", json={"limit": 1}
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    create_dataset_and_version(client)
    response = client.post(
        "/datasets/orders/versions/9/processing-tasks/dispatch", json={"limit": 1}
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_dispatches_start_each_task_exactly_once(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task_ids = [make_task(client, f"task-{i}")["id"] for i in range(12)]

    thread_count = 6
    barrier = threading.Barrier(thread_count)
    collected: list[list[dict]] = []
    collection_lock = threading.Lock()

    def worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        local: list[dict] = []
        for _ in range(30):
            response = thread_client.post(DISPATCH_PATH, json={"limit": 3})
            assert response.status_code == 201, response.text
            runs = response.json()["runs"]
            assert len(runs) <= 3
            local.extend(runs)
            if not runs:
                break
        with collection_lock:
            collected.append(local)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    runs = [run for local in collected for run in local]
    # Every startable task started exactly once; no duplicate attempts and no
    # duplicate run ids.
    assert sorted(run["task_id"] for run in runs) == sorted(task_ids)
    assert len({run["id"] for run in runs}) == len(runs)
    assert all(run["attempt"] == 1 for run in runs)

    for task_id in task_ids:
        detail = task_detail(client, task_id)
        assert detail["status"] == "running"
        assert detail["attempt_count"] == 1
        assert len(detail["runs"]) == 1
        assert detail["runs"][0]["status"] == "running"


def test_concurrent_dispatch_and_single_starts_never_duplicate_runs(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    task_ids = [
        make_task(client, f"task-{i}", max_attempts=3)["id"] for i in range(10)
    ]

    thread_count = 5
    barrier = threading.Barrier(thread_count)
    errors: list[AssertionError] = []

    def worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        try:
            for turn in range(40):
                if turn % 3 == 0:
                    response = thread_client.post(DISPATCH_PATH, json={"limit": 2})
                    assert response.status_code == 201, response.text
                    if not response.json()["runs"]:
                        break
                else:
                    listing = thread_client.get(TASKS_PATH)
                    assert listing.status_code == 200
                    candidates = [
                        task
                        for task in listing.json()
                        if task["status"] in ("pending", "failed")
                        and task["attempt_count"] < task["max_attempts"]
                    ]
                    if not candidates:
                        break
                    response = thread_client.post(
                        f"{TASKS_PATH}/{candidates[0]['id']}/runs"
                    )
                    # Losing the race is a legitimate 409; anything else is not.
                    assert response.status_code in (201, 409), response.text
        except AssertionError as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors

    for task_id in task_ids:
        detail = task_detail(client, task_id)
        # Exactly one running run at most and no duplicated attempt numbers.
        running_runs = [run for run in detail["runs"] if run["status"] == "running"]
        assert len(running_runs) <= 1
        attempts = [run["attempt"] for run in detail["runs"]]
        assert attempts == list(range(1, len(attempts) + 1))
        assert detail["attempt_count"] == len(detail["runs"])


# --------------------------------------------------------------------------- #
# Cross-process concurrency and persistence across restarts
# --------------------------------------------------------------------------- #


def _run_isolated(db_path: Path, script: str) -> str:
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


SEED_SCRIPT = """
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
for index in range(10):
    r = client.post(base, json={"name": f"task-{index}"})
    assert r.status_code == 201, r.text
print("seeded")
"""

WORKER_SCRIPT = textwrap.dedent(
    """
    import json
    import sys
    import threading

    from fastapi.testclient import TestClient
    from app.main import app

    output_path = sys.argv[1]
    thread_count = int(sys.argv[2])
    path = "/datasets/orders/versions/1/processing-tasks/dispatch"
    barrier = threading.Barrier(thread_count)
    results = []
    results_lock = threading.Lock()

    def worker():
        client = TestClient(app)
        barrier.wait()
        local = []
        for _ in range(50):
            response = client.post(path, json={"limit": 3})
            assert response.status_code == 201, response.text
            runs = response.json()["runs"]
            assert len(runs) <= 3
            local.extend(
                (run["task_id"], run["attempt"], run["id"]) for run in runs
            )
            if not runs:
                break
        with results_lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(results, handle)
    """
)


def test_dispatch_concurrent_across_processes(tmp_path: Path) -> None:
    db_path = tmp_path / "dispatch-processes.db"
    assert _run_isolated(db_path, SEED_SCRIPT) == "seeded"

    env = os.environ.copy()
    env["DATA_LINEAGE_DB"] = str(db_path)
    outputs = [tmp_path / f"out-{index}.json" for index in range(2)]
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", WORKER_SCRIPT, str(output), "3"],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for output in outputs
    ]
    for process, output in zip(processes, outputs):
        stderr = process.stderr.read()
        assert process.wait() == 0, stderr
        assert output.exists()

    started = []
    for output in outputs:
        started.extend(json.loads(output.read_text(encoding="utf-8")))

    # The ten tasks were partitioned between the two processes: each task got
    # exactly one attempt-1 run with a unique run id.
    assert len(started) == 10
    assert sorted(task_id for task_id, _attempt, _run_id in started) == list(
        range(1, 11)
    )
    assert all(attempt == 1 for _task_id, attempt, _run_id in started)
    run_ids = [run_id for _task_id, _attempt, run_id in started]
    assert len(set(run_ids)) == len(run_ids)


PERSIST_CREATE_SCRIPT = """
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
upstream = client.post(base, json={"name": "upstream"}).json()
dependent = client.post(
    base, json={"name": "dependent", "depends_on": [upstream["id"]]}
).json()
client.post(base, json={"name": "independent"})
r = client.post(base + "/dispatch", json={"limit": 10})
assert r.status_code == 201, r.text
started = sorted(run["task_id"] for run in r.json()["runs"])
assert started == sorted([upstream["id"], 3]), started
run = client.get(f"{base}/{upstream['id']}").json()["runs"][0]
r = client.patch(
    f"{base}/{upstream['id']}/runs/{run['id']}", json={"status": "succeeded"}
)
assert r.status_code == 200, r.text
print("created")
"""

PERSIST_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
base = "/datasets/orders/versions/1/processing-tasks"
tasks = {task["name"]: task for task in client.get(base).json()}
assert tasks["upstream"]["status"] == "succeeded"
assert tasks["upstream"]["attempt_count"] == 1
assert tasks["independent"]["status"] == "running"
assert tasks["dependent"]["status"] == "pending"

# The dependency succeeded before the restart, so dispatch now starts it.
r = client.post(base + "/dispatch", json={"limit": 10})
assert r.status_code == 201, r.text
started = [run["task_id"] for run in r.json()["runs"]]
assert started == [tasks["dependent"]["id"]], started
detail = client.get(f"{base}/{tasks['dependent']['id']}").json()
assert detail["status"] == "running"
assert detail["attempt_count"] == 1
assert len(detail["runs"]) == 1

# Nothing left to start.
r = client.post(base + "/dispatch", json={"limit": 10})
assert r.json()["runs"] == []
print("verified")
"""


def test_dispatched_runs_survive_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "dispatch-persist.db"
    assert _run_isolated(db_path, PERSIST_CREATE_SCRIPT) == "created"
    assert _run_isolated(db_path, PERSIST_VERIFY_SCRIPT) == "verified"
