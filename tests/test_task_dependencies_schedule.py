"""Tests for dependency-graph editing (PUT .../dependencies) and the schedule
view (GET .../processing-tasks/schedule)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

BASE = "/datasets/orders/versions/1/processing-tasks"

TASK_FIELDS = {
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


def setup(client: TestClient, dataset: str = "orders", version: int = 1) -> None:
    response = client.post("/datasets", json={"name": dataset})
    assert response.status_code == 201, response.text
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201, response.text
    for extra in range(2, version + 1):
        response = client.post(
            f"/datasets/{dataset}/versions",
            json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
        )
        assert response.status_code == 201, response.text


def make_task(client: TestClient, name: str, **overrides) -> dict:
    response = client.post(BASE, json={"name": name, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def run_task(client: TestClient, task_id: int) -> dict:
    response = client.post(f"{BASE}/{task_id}/runs")
    assert response.status_code == 201, response.text
    return response.json()


def finish_run(client: TestClient, task_id: int, run_id: int, body: dict) -> None:
    response = client.patch(
        f"{BASE}/{task_id}/runs/{run_id}", json=body
    )
    assert response.status_code == 200, response.text


def put_deps(client: TestClient, task_id: int, depends_on: list[int]):
    return client.put(f"{BASE}/{task_id}/dependencies", json={"depends_on": depends_on})


def schedule(client: TestClient, dataset: str = "orders", version: int = 1):
    return client.get(f"/datasets/{dataset}/versions/{version}/processing-tasks/schedule")


def by_name(tasks: list[dict]) -> dict[str, dict]:
    return {task["name"]: task for task in tasks}


# --------------------------------------------------------------------------- #
# PUT dependencies
# --------------------------------------------------------------------------- #


def test_replace_dependencies_returns_updated_task(client: TestClient) -> None:
    setup(client)
    first = make_task(client, "extract")
    second = make_task(client, "transform")
    target = make_task(client, "load", depends_on=[first["id"]])

    response = put_deps(client, target["id"], [second["id"], first["id"]])
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == TASK_FIELDS
    assert body["depends_on"] == [second["id"], first["id"]]
    assert body["status"] == "pending"

    # The replacement is persisted and atomic (not a merge with the old list).
    listed = client.get(BASE)
    listed_target = next(task for task in listed.json() if task["id"] == target["id"])
    assert listed_target["depends_on"] == [second["id"], first["id"]]


def test_replace_dependencies_with_empty_list_clears_them(client: TestClient) -> None:
    setup(client)
    first = make_task(client, "extract")
    target = make_task(client, "load", depends_on=[first["id"]])

    response = put_deps(client, target["id"], [])
    assert response.status_code == 200, response.text
    assert response.json()["depends_on"] == []
    detail = client.get(f"{BASE}/{target['id']}").json()
    assert detail["depends_on"] == []


def test_replace_dependencies_unknown_resources_are_404(client: TestClient) -> None:
    assert (
        client.put(
            "/datasets/ghost/versions/1/processing-tasks/1/dependencies",
            json={"depends_on": []},
        ).status_code
        == 404
    )
    setup(client)
    task = make_task(client, "load")
    assert put_deps(client, 999, []).status_code == 404
    assert (
        client.put(
            "/datasets/orders/versions/9/processing-tasks/1/dependencies",
            json={"depends_on": []},
        ).status_code
        == 404
    )

    # An unknown dependency id is 404 and nothing is written.
    response = put_deps(client, task["id"], [999])
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert client.get(f"{BASE}/{task['id']}").json()["depends_on"] == []


def test_replace_dependencies_must_reference_same_version(client: TestClient) -> None:
    setup(client, version=2)
    v1_task = make_task(client, "extract")
    v2_task = client.post(
        "/datasets/orders/versions/2/processing-tasks", json={"name": "extract"}
    ).json()
    response = client.put(
        f"/datasets/orders/versions/2/processing-tasks/{v2_task['id']}/dependencies",
        json={"depends_on": [v1_task["id"]]},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_replace_dependencies_self_dependency_is_409(client: TestClient) -> None:
    setup(client)
    task = make_task(client, "load")
    response = put_deps(client, task["id"], [task["id"]])
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    assert client.get(f"{BASE}/{task['id']}").json()["depends_on"] == []


def test_replace_dependencies_duplicate_ids_is_422(client: TestClient) -> None:
    setup(client)
    first = make_task(client, "extract")
    target = make_task(client, "load")
    response = put_deps(client, target["id"], [first["id"], first["id"]])
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get(f"{BASE}/{target['id']}").json()["depends_on"] == []


def test_replace_dependencies_rejecting_a_direct_cycle(client: TestClient) -> None:
    setup(client)
    # b -> a already exists; making a depend on b closes a 2-node cycle.
    first = make_task(client, "extract")
    second = make_task(client, "load", depends_on=[first["id"]])

    response = put_deps(client, first["id"], [second["id"]])
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    # Nothing was written.
    assert client.get(f"{BASE}/{first['id']}").json()["depends_on"] == []
    assert client.get(f"{BASE}/{second['id']}").json()["depends_on"] == [first["id"]]


def test_replace_dependencies_rejecting_an_indirect_cycle(client: TestClient) -> None:
    setup(client)
    # Existing graph: c -> b -> a. Making a depend on c closes a -> c -> b -> a.
    a = make_task(client, "extract")
    b = make_task(client, "transform", depends_on=[a["id"]])
    c = make_task(client, "load", depends_on=[b["id"]])

    response = put_deps(client, a["id"], [c["id"]])
    assert response.status_code == 409
    assert client.get(f"{BASE}/{a['id']}").json()["depends_on"] == []

    # A different, acyclic edge in the same graph is accepted.
    response = put_deps(client, a["id"], [])
    assert response.status_code == 200, response.text


def test_replace_dependencies_only_allowed_while_pending(client: TestClient) -> None:
    setup(client)
    dependency = make_task(client, "extract")

    for status, max_attempts, finish in (
        ("running", 2, None),
        ("succeeded", 1, {"status": "succeeded"}),
        ("failed", 2, {"status": "failed", "error": "boom"}),
    ):
        target = make_task(client, f"task-{status}", max_attempts=max_attempts)
        run = run_task(client, target["id"])
        if finish is not None:
            finish_run(client, target["id"], run["id"], finish)
        response = put_deps(client, target["id"], [dependency["id"]])
        assert response.status_code == 409, (status, response.text)
        assert response.json()["error"] == "conflict"
        # A non-pending task's dependency list never changes.
        assert client.get(f"{BASE}/{target['id']}").json()["depends_on"] == []


def test_replace_dependencies_invalid_bodies_are_422(client: TestClient) -> None:
    setup(client)
    first = make_task(client, "extract")
    target = make_task(client, "load", depends_on=[first["id"]])
    url = f"{BASE}/{target['id']}/dependencies"

    for payload in (
        {},
        {"depends_on": None},
        {"depends_on": 1},
        {"depends_on": "[]"},
        {"depends_on": ["1"]},
        {"depends_on": [True]},
        {"depends_on": [1.5]},
        {"depends_on": [first["id"]], "extra": True},
    ):
        response = client.put(url, json=payload)
        assert response.status_code == 422, (payload, response.text)
        assert response.json()["error"] == "validation_error"

    # Malformed JSON is a safe JSON 422 as well.
    response = client.put(
        url, content="{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"

    # No invalid request wrote anything.
    assert client.get(f"{BASE}/{target['id']}").json()["depends_on"] == [first["id"]]


def test_updated_dependencies_govern_starting_runs(client: TestClient) -> None:
    setup(client)
    first = make_task(client, "extract")
    target = make_task(client, "load", depends_on=[first["id"]])

    # The original edge blocks the start while the dependency is pending.
    assert client.post(f"{BASE}/{target['id']}/runs").status_code == 409

    # Removing the dependency via PUT immediately unblocks the start.
    assert put_deps(client, target["id"], []).status_code == 200
    run = client.post(f"{BASE}/{target['id']}/runs")
    assert run.status_code == 201, run.text

    # A pending task that gains a dependency through PUT is blocked again.
    other = make_task(client, "publish")
    assert put_deps(client, other["id"], [first["id"]]).status_code == 200
    assert client.post(f"{BASE}/{other['id']}/runs").status_code == 409


# --------------------------------------------------------------------------- #
# GET schedule
# --------------------------------------------------------------------------- #


def test_schedule_shape_and_ordering(client: TestClient) -> None:
    setup(client)
    ids = [make_task(client, name)["id"] for name in ("gamma", "alpha", "beta")]
    response = schedule(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert [task["id"] for task in body["tasks"]] == sorted(ids)
    for task in body["tasks"]:
        assert set(task) == TASK_FIELDS | {"schedule_state", "blocking_task_ids"}


def test_schedule_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    assert schedule(client, dataset="ghost").status_code == 404
    setup(client)
    assert schedule(client, version=9).status_code == 404


def test_schedule_states_for_non_pending_tasks(client: TestClient) -> None:
    setup(client)
    running = make_task(client, "running-task", max_attempts=2)
    run_task(client, running["id"])

    succeeded = make_task(client, "succeeded-task")
    run = run_task(client, succeeded["id"])
    finish_run(client, succeeded["id"], run["id"], {"status": "succeeded"})

    retryable = make_task(client, "retryable-task", max_attempts=3)
    run = run_task(client, retryable["id"])
    finish_run(client, retryable["id"], run["id"], {"status": "failed", "error": "x"})

    exhausted = make_task(client, "exhausted-task")
    run = run_task(client, exhausted["id"])
    finish_run(client, exhausted["id"], run["id"], {"status": "failed", "error": "x"})

    tasks = by_name(schedule(client).json()["tasks"])
    assert tasks["running-task"]["schedule_state"] == "running"
    assert tasks["succeeded-task"]["schedule_state"] == "succeeded"
    assert tasks["retryable-task"]["schedule_state"] == "retryable"
    assert tasks["exhausted-task"]["schedule_state"] == "exhausted"
    for name in ("running-task", "succeeded-task", "retryable-task", "exhausted-task"):
        assert tasks[name]["blocking_task_ids"] == []


def test_schedule_states_for_pending_tasks(client: TestClient) -> None:
    setup(client)
    independent = make_task(client, "ready-task")

    pending_dep = make_task(client, "pending-dep")
    blocked = make_task(client, "blocked-task", depends_on=[pending_dep["id"]])

    succeeded_dep = make_task(client, "succeeded-dep")
    run = run_task(client, succeeded_dep["id"])
    finish_run(client, succeeded_dep["id"], run["id"], {"status": "succeeded"})
    ready = make_task(client, "ready-after-success", depends_on=[succeeded_dep["id"]])

    exhausted_dep = make_task(client, "exhausted-dep")
    run = run_task(client, exhausted_dep["id"])
    finish_run(client, exhausted_dep["id"], run["id"], {"status": "failed", "error": "x"})
    upstream = make_task(
        client, "direct-upstream-failed", depends_on=[exhausted_dep["id"]]
    )

    retryable_dep = make_task(client, "retryable-dep", max_attempts=2)
    run = run_task(client, retryable_dep["id"])
    finish_run(client, retryable_dep["id"], run["id"], {"status": "failed", "error": "x"})
    waits_for_retry = make_task(
        client, "waits-for-retry", depends_on=[retryable_dep["id"]]
    )

    other_pending = make_task(client, "other-pending")
    partially_blocked = make_task(
        client,
        "partially-blocked",
        depends_on=[succeeded_dep["id"], pending_dep["id"], other_pending["id"]],
    )

    tasks = by_name(schedule(client).json()["tasks"])
    assert tasks["ready-task"]["schedule_state"] == "ready"
    assert tasks["ready-task"]["blocking_task_ids"] == []
    assert tasks["blocked-task"]["schedule_state"] == "blocked"
    assert tasks["blocked-task"]["blocking_task_ids"] == [pending_dep["id"]]
    assert tasks["ready-after-success"]["schedule_state"] == "ready"
    assert tasks["ready-after-success"]["blocking_task_ids"] == []
    assert tasks["direct-upstream-failed"]["schedule_state"] == "upstream_failed"
    assert tasks["direct-upstream-failed"]["blocking_task_ids"] == [exhausted_dep["id"]]
    # A retryable dependency may still succeed, so its dependents stay blocked.
    assert tasks["waits-for-retry"]["schedule_state"] == "blocked"
    assert tasks["waits-for-retry"]["blocking_task_ids"] == [retryable_dep["id"]]
    # Only the not-yet-succeeded direct dependencies are listed, id-ascending.
    assert tasks["partially-blocked"]["schedule_state"] == "blocked"
    assert tasks["partially-blocked"]["blocking_task_ids"] == sorted(
        [pending_dep["id"], other_pending["id"]]
    )
    # Independent of the above: every non-pending task has no blockers.
    assert tasks["succeeded-dep"]["blocking_task_ids"] == []


def test_schedule_upstream_failure_propagates_through_the_chain(
    client: TestClient,
) -> None:
    setup(client)
    root = make_task(client, "root")
    middle = make_task(client, "middle", depends_on=[root["id"]])
    leaf = make_task(client, "leaf", depends_on=[middle["id"]])
    # Also depends (indirectly) on root through a branch that later succeeds in
    # part: any single exhausted chain marks the task upstream_failed.
    branch = make_task(client, "branch", depends_on=[root["id"]])
    join = make_task(
        client, "join", depends_on=[leaf["id"], branch["id"]]
    )

    run = run_task(client, root["id"])
    finish_run(client, root["id"], run["id"], {"status": "failed", "error": "x"})
    # root has max_attempts=1, so it is exhausted; branch depends on it and the
    # existing start rule rejects the run.
    run = client.post(f"{BASE}/{branch['id']}/runs")
    assert run.status_code == 409, run.text

    tasks = by_name(schedule(client).json()["tasks"])
    assert tasks["root"]["schedule_state"] == "exhausted"
    assert tasks["middle"]["schedule_state"] == "upstream_failed"
    assert tasks["middle"]["blocking_task_ids"] == [root["id"]]
    assert tasks["leaf"]["schedule_state"] == "upstream_failed"
    assert tasks["leaf"]["blocking_task_ids"] == [middle["id"]]
    assert tasks["branch"]["schedule_state"] == "upstream_failed"
    assert tasks["join"]["schedule_state"] == "upstream_failed"
    assert tasks["join"]["blocking_task_ids"] == sorted([leaf["id"], branch["id"]])


def test_schedule_recomputes_after_a_retry_succeeds(client: TestClient) -> None:
    setup(client)
    flaky = make_task(client, "flaky", max_attempts=2)
    middle = make_task(client, "middle", depends_on=[flaky["id"]])
    leaf = make_task(client, "leaf", depends_on=[middle["id"]])

    run = run_task(client, flaky["id"])
    finish_run(client, flaky["id"], run["id"], {"status": "failed", "error": "boom"})

    tasks = by_name(schedule(client).json()["tasks"])
    assert tasks["flaky"]["schedule_state"] == "retryable"
    assert tasks["middle"]["schedule_state"] == "blocked"
    assert tasks["middle"]["blocking_task_ids"] == [flaky["id"]]
    assert tasks["leaf"]["schedule_state"] == "blocked"

    # The retry succeeds: the whole chain recomputes on the next read.
    run = run_task(client, flaky["id"])
    finish_run(client, flaky["id"], run["id"], {"status": "succeeded"})
    tasks = by_name(schedule(client).json()["tasks"])
    assert tasks["flaky"]["schedule_state"] == "succeeded"
    assert tasks["middle"]["schedule_state"] == "ready"
    assert tasks["middle"]["blocking_task_ids"] == []
    assert tasks["leaf"]["schedule_state"] == "blocked"
    assert tasks["leaf"]["blocking_task_ids"] == [middle["id"]]

    run = run_task(client, middle["id"])
    finish_run(client, middle["id"], run["id"], {"status": "succeeded"})
    tasks = by_name(schedule(client).json()["tasks"])
    assert tasks["leaf"]["schedule_state"] == "ready"
    assert tasks["leaf"]["blocking_task_ids"] == []


def test_schedule_reflects_replaced_dependencies(client: TestClient) -> None:
    setup(client)
    blocked_dep = make_task(client, "pending-root")
    target = make_task(client, "target", depends_on=[blocked_dep["id"]])
    tasks = by_name(schedule(client).json()["tasks"])
    assert tasks["target"]["schedule_state"] == "blocked"

    assert put_deps(client, target["id"], []).status_code == 200
    tasks = by_name(schedule(client).json()["tasks"])
    assert tasks["target"]["schedule_state"] == "ready"
    assert tasks["target"]["blocking_task_ids"] == []


def test_schedule_existing_route_still_matches_task_detail(client: TestClient) -> None:
    setup(client)
    task = make_task(client, "extract")
    # The literal "schedule" segment must win over "/{task_id}".
    assert schedule(client).status_code == 200
    detail = client.get(f"{BASE}/{task['id']}")
    assert detail.status_code == 200
    assert "runs" in detail.json()


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
root = ok(client.post(base, json={"name": "root", "max_attempts": 2}))
load = ok(client.post(base, json={"name": "load", "depends_on": [root["id"]]}))
# root fails once but still has attempts left (retryable).
run = ok(client.post(f"{base}/{root['id']}/runs"))
assert client.patch(
    f"{base}/{root['id']}/runs/{run['id']}",
    json={"status": "failed", "error": "boom"},
).status_code == 200
# Edit the graph after runs exist: drop load's dependency on root.
updated = client.put(f"{base}/{load['id']}/dependencies", json={"depends_on": []})
assert updated.status_code == 200, updated.text
ok(client.post(base, json={"name": "independent"}))
print("created")
"""

VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
base = "/datasets/orders/versions/1/processing-tasks"

response = client.get(f"{base}/schedule")
assert response.status_code == 200, response.text
body = response.json()
assert body["dataset"] == "orders"
assert body["version"] == 1
tasks = {task["name"]: task for task in body["tasks"]}
assert [task["id"] for task in body["tasks"]] == sorted(task["id"] for task in body["tasks"])

# The failed root still has attempts left: retryable after the restart.
assert tasks["root"]["status"] == "failed"
assert tasks["root"]["schedule_state"] == "retryable"
assert tasks["root"]["blocking_task_ids"] == []

# The dependency edit survived the restart: load no longer depends on root and is
# ready even though root has not succeeded.
assert tasks["load"]["depends_on"] == []
assert tasks["load"]["schedule_state"] == "ready"
assert tasks["load"]["blocking_task_ids"] == []

assert tasks["independent"]["schedule_state"] == "ready"

# The existing start rule uses the updated dependencies: load starts while root
# is still failed.
started = client.post(f"{base}/{tasks['load']['id']}/runs")
assert started.status_code == 201, started.text

# And root itself can use its remaining attempt.
retry = client.post(f"{base}/{tasks['root']['id']}/runs")
assert retry.status_code == 201, retry.text
assert client.patch(
    f"{base}/{tasks['root']['id']}/runs/{retry.json()['id']}",
    json={"status": "succeeded"},
).status_code == 200
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
