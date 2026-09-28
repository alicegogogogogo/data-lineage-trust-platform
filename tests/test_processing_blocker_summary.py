"""Tests for the read-only cross-version processing blocker summary."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SUMMARY_PATH = "/datasets/orders/processing-blocker-summary"
TASKS_V1 = "/datasets/orders/versions/1/processing-tasks"
SCHEDULE_V1 = f"{TASKS_V1}/schedule"

TOP_LEVEL_KEYS = ["dataset", "versions", "totals"]
VERSION_KEYS = [
    "version",
    "tasks",
    "ready_count",
    "running_count",
    "succeeded_count",
    "failed_count",
    "exhausted_count",
    "blocked_count",
]
TASK_KEYS = [
    "id",
    "name",
    "status",
    "schedule_state",
    "blocking_task_ids",
    "cause",
    "causes",
]
COUNT_KEYS = [
    "ready_count",
    "running_count",
    "succeeded_count",
    "failed_count",
    "exhausted_count",
    "blocked_count",
]
TOTAL_KEYS = ["version_count", "task_count", *COUNT_KEYS]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_dataset(client: TestClient, name: str = "orders") -> None:
    response = client.post("/datasets", json={"name": name})
    assert response.status_code == 201, response.text


def add_version(client: TestClient, dataset: str = "orders") -> int:
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201, response.text
    return response.json()["version"]


def tasks_path(version: int, dataset: str = "orders") -> str:
    return f"/datasets/{dataset}/versions/{version}/processing-tasks"


def create_task(
    client: TestClient,
    name: str,
    *,
    version: int = 1,
    max_attempts: int = 1,
    depends_on: list[int] | None = None,
    dataset: str = "orders",
) -> int:
    body: dict = {"name": name, "max_attempts": max_attempts}
    if depends_on is not None:
        body["depends_on"] = depends_on
    response = client.post(tasks_path(version, dataset), json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def start_run(
    client: TestClient, task_id: int, version: int = 1, dataset: str = "orders"
) -> dict:
    response = client.post(f"{tasks_path(version, dataset)}/{task_id}/runs")
    assert response.status_code == 201, response.text
    return response.json()


def finish_run(
    client: TestClient,
    task_id: int,
    run_id: int,
    *,
    status: str,
    version: int = 1,
    dataset: str = "orders",
    error: str | None = None,
) -> None:
    payload = {"status": status}
    if error is not None:
        payload["error"] = error
    response = client.patch(
        f"{tasks_path(version, dataset)}/{task_id}/runs/{run_id}", json=payload
    )
    assert response.status_code == 200, response.text


def fail_task(client: TestClient, task_id: int) -> None:
    run = start_run(client, task_id)
    finish_run(client, task_id, run["id"], status="failed", error="boom")


def get_summary(client: TestClient, dataset: str = "orders") -> dict:
    response = client.get(f"/datasets/{dataset}/processing-blocker-summary")
    assert response.status_code == 200, response.text
    return response.json()


def summary_response(client: TestClient, dataset: str = "orders"):
    response = client.get(f"/datasets/{dataset}/processing-blocker-summary")
    assert response.status_code == 200, response.text
    return response


def by_name(tasks: list[dict]) -> dict[str, dict]:
    return {task["name"]: task for task in tasks}


# --------------------------------------------------------------------------- #
# Empty state and wire format
# --------------------------------------------------------------------------- #


def test_summary_without_versions_is_empty_not_an_error(client: TestClient) -> None:
    make_dataset(client)
    body = get_summary(client)
    assert list(body) == TOP_LEVEL_KEYS
    assert body["dataset"] == "orders"
    assert body["versions"] == []
    assert body["totals"] == {
        "version_count": 0,
        "task_count": 0,
        "ready_count": 0,
        "running_count": 0,
        "succeeded_count": 0,
        "failed_count": 0,
        "exhausted_count": 0,
        "blocked_count": 0,
    }


def test_summary_is_get_only(client: TestClient) -> None:
    make_dataset(client)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, SUMMARY_PATH)
        assert response.status_code == 405, method


def test_summary_wire_format_is_compact_with_fixed_key_order_and_newline(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    task_id = create_task(client, "extract")
    fail_task(client, task_id)

    response = summary_response(client)
    assert response.headers["content-type"].startswith("application/json")
    text = response.text
    # Compact whitespace: no separator spaces, and the only line break is the
    # single trailing newline.
    assert ": " not in text
    assert ", " not in text
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    assert "\n" not in text[:-1]

    body = response.json()
    assert list(body) == TOP_LEVEL_KEYS
    assert list(body["totals"]) == TOTAL_KEYS
    entry = body["versions"][0]
    assert list(entry) == VERSION_KEYS
    assert list(entry["tasks"][0]) == TASK_KEYS


def test_summary_versions_and_tasks_sort_ascending(client: TestClient) -> None:
    make_dataset(client)
    add_version(client)
    # Tasks are created deliberately out of name order; ordering is by id.
    create_task(client, "gamma")
    create_task(client, "alpha")
    create_task(client, "beta")
    add_version(client)
    create_task(client, "v2-task", version=2)
    add_version(client)  # a version without tasks is still listed

    body = get_summary(client)
    assert [entry["version"] for entry in body["versions"]] == [1, 2, 3]
    first = body["versions"][0]
    assert [task["id"] for task in first["tasks"]] == sorted(
        task["id"] for task in first["tasks"]
    )
    assert [task["name"] for task in first["tasks"]] == [
        "gamma",
        "alpha",
        "beta",
    ]
    assert body["versions"][2]["tasks"] == []
    for entry in body["versions"]:
        assert list(entry) == VERSION_KEYS
        for task in entry["tasks"]:
            assert list(task) == TASK_KEYS


# --------------------------------------------------------------------------- #
# Schedule states, blocker categories and the six counts
# --------------------------------------------------------------------------- #


def test_summary_task_fields_match_the_schedule_view(client: TestClient) -> None:
    make_dataset(client)
    add_version(client)
    dep = create_task(client, "dep")
    target = create_task(client, "target", depends_on=[dep])

    summary = get_summary(client)
    schedule = client.get(SCHEDULE_V1)
    assert schedule.status_code == 200, schedule.text

    summary_tasks = by_name(summary["versions"][0]["tasks"])
    schedule_tasks = by_name(schedule.json()["tasks"])
    for name in ("dep", "target"):
        assert summary_tasks[name]["id"] == schedule_tasks[name]["id"]
        assert (
            summary_tasks[name]["schedule_state"]
            == schedule_tasks[name]["schedule_state"]
        )
        assert (
            summary_tasks[name]["blocking_task_ids"]
            == schedule_tasks[name]["blocking_task_ids"]
        )
        assert summary_tasks[name]["status"] == schedule_tasks[name]["status"]


def test_summary_categories_for_every_state(client: TestClient) -> None:
    make_dataset(client)
    add_version(client)

    # Independent pending task: ready, no blocker.
    ready_id = create_task(client, "ready")
    # Succeeded dependency.
    succeeded_id = create_task(client, "succeeded")
    run = start_run(client, succeeded_id)
    finish_run(client, succeeded_id, run["id"], status="succeeded")
    # Running task.
    running_id = create_task(client, "running", max_attempts=2)
    start_run(client, running_id)
    # Failed with attempts left: retryable -> failed bucket, no blocker.
    retryable_id = create_task(client, "retryable", max_attempts=3)
    fail_task(client, retryable_id)
    # Failed and exhausted: attempts_exhausted.
    exhausted_id = create_task(client, "exhausted")
    fail_task(client, exhausted_id)
    # Pending on a not-yet-succeeded dependency: direct_dependency.
    pending_dep = create_task(client, "pending-dep")
    blocked_id = create_task(
        client, "blocked", depends_on=[succeeded_id, pending_dep]
    )
    # Pending on an exhausted dependency: upstream_failed, and the direct
    # dependency has not succeeded so direct_dependency hits as well.
    upstream_id = create_task(client, "upstream", depends_on=[exhausted_id])
    # The failure propagates through a chain of pending tasks.
    downstream_id = create_task(client, "downstream", depends_on=[upstream_id])
    # A ready task whose only dependency succeeded.
    ready_after_success_id = create_task(
        client, "ready-after-success", depends_on=[succeeded_id]
    )

    tasks = by_name(get_summary(client)["versions"][0]["tasks"])

    ready_task = tasks["ready"]
    assert ready_task["schedule_state"] == "ready"
    assert ready_task["status"] == "pending"
    assert ready_task["blocking_task_ids"] == []
    assert ready_task["cause"] is None
    assert ready_task["causes"] == []

    assert tasks["succeeded"]["schedule_state"] == "succeeded"
    assert tasks["succeeded"]["cause"] is None
    assert tasks["succeeded"]["causes"] == []
    assert tasks["succeeded"]["blocking_task_ids"] == []

    assert tasks["running"]["schedule_state"] == "running"
    assert tasks["running"]["cause"] is None
    assert tasks["running"]["causes"] == []

    retryable_task = tasks["retryable"]
    assert retryable_task["schedule_state"] == "retryable"
    assert retryable_task["status"] == "failed"
    assert retryable_task["cause"] is None
    assert retryable_task["causes"] == []
    assert retryable_task["blocking_task_ids"] == []

    exhausted_task = tasks["exhausted"]
    assert exhausted_task["schedule_state"] == "exhausted"
    assert exhausted_task["cause"] == "attempts_exhausted"
    assert exhausted_task["causes"] == ["attempts_exhausted"]
    assert exhausted_task["blocking_task_ids"] == []

    blocked_task = tasks["blocked"]
    assert blocked_task["schedule_state"] == "blocked"
    assert blocked_task["blocking_task_ids"] == [pending_dep]
    assert blocked_task["cause"] == "direct_dependency"
    assert blocked_task["causes"] == ["direct_dependency"]

    upstream_task = tasks["upstream"]
    assert upstream_task["schedule_state"] == "upstream_failed"
    assert upstream_task["blocking_task_ids"] == [exhausted_id]
    # Both categories hit; causes are alphabetical, cause prefers upstream.
    assert upstream_task["causes"] == [
        "direct_dependency",
        "upstream_failed",
    ]
    assert upstream_task["cause"] == "upstream_failed"

    downstream_task = tasks["downstream"]
    assert downstream_task["schedule_state"] == "upstream_failed"
    assert downstream_task["blocking_task_ids"] == [upstream_id]
    assert downstream_task["causes"] == [
        "direct_dependency",
        "upstream_failed",
    ]
    assert downstream_task["cause"] == "upstream_failed"

    after_success = tasks["ready-after-success"]
    assert after_success["schedule_state"] == "ready"
    assert after_success["blocking_task_ids"] == []
    assert after_success["cause"] is None
    assert after_success["causes"] == []

    # Sanity: ids referenced above are real.
    assert len({ready_id, running_id, blocked_id, downstream_id}) == 4


def test_summary_blocking_ids_sorted_deduped_and_only_for_pending(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    first = create_task(client, "first")
    second = create_task(client, "second")
    third = create_task(client, "third")
    target = create_task(
        client, "target", depends_on=[third, first, second]
    )
    # A non-pending task never carries blockers; it must be independent so a
    # run can actually start while it is still dependency-free.
    non_pending = create_task(
        client, "non-pending", max_attempts=2
    )
    start_run(client, non_pending)

    tasks = by_name(get_summary(client)["versions"][0]["tasks"])
    assert tasks["target"]["blocking_task_ids"] == sorted(
        [first, second, third]
    )
    assert tasks["non-pending"]["blocking_task_ids"] == []
    assert tasks["target"]["id"] == target


def test_summary_six_counts_partition_the_version_and_match_states(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    # ready (independent pending) x1
    create_task(client, "ready-1")
    create_task(client, "ready-2")
    # running x1
    running = create_task(client, "running", max_attempts=2)
    start_run(client, running)
    # succeeded x1
    succeeded = create_task(client, "succeeded")
    run = start_run(client, succeeded)
    finish_run(client, succeeded, run["id"], status="succeeded")
    # retryable -> failed x2
    for name in ("retry-1", "retry-2"):
        task_id = create_task(client, name, max_attempts=2)
        fail_task(client, task_id)
    # exhausted x1
    exhausted = create_task(client, "exhausted")
    fail_task(client, exhausted)
    # blocked x1 (depends on a pending task); the pending root itself is ready.
    pending_root = create_task(client, "pending-root")
    create_task(client, "blocked", depends_on=[pending_root])
    # upstream_failed merges into blocked x1
    create_task(client, "upstream", depends_on=[exhausted])

    entry = get_summary(client)["versions"][0]
    counts = {key: entry[key] for key in COUNT_KEYS}
    assert counts == {
        "ready_count": 3,
        "running_count": 1,
        "succeeded_count": 1,
        "failed_count": 2,
        "exhausted_count": 1,
        "blocked_count": 2,
    }
    # The six counts mutually exclusively cover every task.
    assert sum(counts.values()) == len(entry["tasks"]) == 10

    # And the counts agree with the schedule-state literals on the tasks.
    state_to_bucket = {
        "ready": "ready",
        "running": "running",
        "succeeded": "succeeded",
        "retryable": "failed",
        "exhausted": "exhausted",
        "blocked": "blocked",
        "upstream_failed": "blocked",
    }
    expected = {key: 0 for key in COUNT_KEYS}
    for task in entry["tasks"]:
        expected[f"{state_to_bucket[task['schedule_state']]}_count"] += 1
    assert counts == expected


def test_summary_recomputes_after_state_changes(client: TestClient) -> None:
    make_dataset(client)
    add_version(client)
    flaky = create_task(client, "flaky", max_attempts=2)
    leaf = create_task(client, "leaf", depends_on=[flaky])

    entry = get_summary(client)["versions"][0]
    assert entry["ready_count"] == 1
    assert entry["blocked_count"] == 1
    tasks = by_name(entry["tasks"])
    assert tasks["leaf"]["cause"] == "direct_dependency"

    # A failure with attempts left moves flaky to failed and leaf to blocked.
    fail_task(client, flaky)
    entry = get_summary(client)["versions"][0]
    assert entry["failed_count"] == 1
    assert entry["blocked_count"] == 1
    tasks = by_name(entry["tasks"])
    assert tasks["flaky"]["schedule_state"] == "retryable"
    assert tasks["flaky"]["cause"] is None
    assert tasks["leaf"]["schedule_state"] == "blocked"
    assert tasks["leaf"]["cause"] == "direct_dependency"
    assert tasks["leaf"]["blocking_task_ids"] == [flaky]

    # Once the retry succeeds, leaf becomes ready.
    run = start_run(client, flaky)
    finish_run(client, flaky, run["id"], status="succeeded")
    entry = get_summary(client)["versions"][0]
    assert entry["succeeded_count"] == 1
    assert entry["ready_count"] == 1
    tasks = by_name(entry["tasks"])
    assert tasks["leaf"]["schedule_state"] == "ready"
    assert tasks["leaf"]["causes"] == []
    assert tasks["leaf"]["cause"] is None


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #


def test_summary_totals_equal_the_sum_of_the_version_entries(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    # v1: one ready, one exhausted, one upstream_failed (blocked bucket).
    create_task(client, "v1-ready")
    exhausted = create_task(client, "v1-exhausted")
    fail_task(client, exhausted)
    create_task(client, "v1-upstream", depends_on=[exhausted])

    add_version(client)
    # v2: one running, one retryable (failed bucket).
    running = create_task(client, "v2-running", max_attempts=2, version=2)
    start_run(client, running, version=2)
    retryable = create_task(client, "v2-retry", max_attempts=3, version=2)
    run = start_run(client, retryable, version=2)
    finish_run(
        client, retryable, run["id"], version=2, status="failed", error="z"
    )

    add_version(client)  # v3: no tasks

    body = get_summary(client)
    versions = body["versions"]
    totals = body["totals"]
    assert [entry["version"] for entry in versions] == [1, 2, 3]
    assert totals["version_count"] == 3
    assert totals["task_count"] == 5
    assert list(totals) == TOTAL_KEYS
    for key in COUNT_KEYS:
        assert totals[key] == sum(entry[key] for entry in versions), key
    assert totals == {
        "version_count": 3,
        "task_count": 5,
        "ready_count": 1,
        "running_count": 1,
        "succeeded_count": 0,
        "failed_count": 1,
        "exhausted_count": 1,
        "blocked_count": 1,
    }
    # Per version the counts always partition that version's tasks.
    for entry in versions:
        assert sum(entry[key] for key in COUNT_KEYS) == len(entry["tasks"])


# --------------------------------------------------------------------------- #
# Errors: 404 / 422, no writes
# --------------------------------------------------------------------------- #


def test_summary_unknown_dataset_returns_404(client: TestClient) -> None:
    response = client.get("/datasets/ghost/processing-blocker-summary")
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "not_found"
    assert "sqlite" not in response.text.lower()
    assert "traceback" not in response.text.lower()


def test_summary_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset(client)
    before = summary_response(client).text

    with_body = client.request("GET", SUMMARY_PATH, content=b"{}")
    whitespace_body = client.request("GET", SUMMARY_PATH, content=b"   ")
    single_space_body = client.request("GET", SUMMARY_PATH, content=b" ")
    with_query = client.get(SUMMARY_PATH, params={"limit": 1})
    assert with_body.status_code == 422
    assert whitespace_body.status_code == 422
    assert single_space_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, whitespace_body, single_space_body, with_query):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]
        assert "sqlite" not in response.text.lower()
        assert "traceback" not in response.text.lower()

    # The rejections wrote nothing.
    assert summary_response(client).text == before


def test_summary_shape_errors_keep_404_precedence(client: TestClient) -> None:
    make_dataset(client)
    assert client.get(
        "/datasets/ghost/processing-blocker-summary", params={"x": "1"}
    ).status_code == 404
    assert (
        client.request(
            "GET", "/datasets/ghost/processing-blocker-summary", content=b"{}"
        ).status_code
        == 404
    )
    assert (
        client.request(
            "GET",
            "/datasets/ghost/processing-blocker-summary",
            content=b" ",
        ).status_code
        == 404
    )


def test_summary_is_strictly_read_only(client: TestClient) -> None:
    make_dataset(client)
    add_version(client)
    exhausted = create_task(client, "exhausted")
    fail_task(client, exhausted)
    create_task(client, "upstream", depends_on=[exhausted])

    tasks_before = client.get(TASKS_V1).json()
    schedule_before = client.get(SCHEDULE_V1).json()
    first_text = summary_response(client).text

    for _ in range(3):
        response = summary_response(client)
        assert response.text == first_text
    assert client.get(TASKS_V1).json() == tasks_before
    assert client.get(SCHEDULE_V1).json() == schedule_before


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


_CREATE_SCRIPT = """
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

# Succeeded task.
succeeded = ok(client.post(base, json={"name": "succeeded"}))
run = ok(client.post(f"{base}/{succeeded['id']}/runs"))
assert client.patch(
    f"{base}/{succeeded['id']}/runs/{run['id']}",
    json={"status": "succeeded"},
).status_code == 200

# Exhausted task (single attempt used).
exhausted = ok(client.post(base, json={"name": "exhausted"}))
run = ok(client.post(f"{base}/{exhausted['id']}/runs"))
assert client.patch(
    f"{base}/{exhausted['id']}/runs/{run['id']}",
    json={"status": "failed", "error": "boom"},
).status_code == 200

# Pending task blocked by the exhausted one (upstream_failed, which the
# summary merges into the blocked count).
ok(client.post(base, json={"name": "upstream", "depends_on": [exhausted["id"]]}))

# A second version with no tasks.
ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
))
print("created")
"""

_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

path = "/datasets/orders/processing-blocker-summary"
response = client.get(path)
assert response.status_code == 200, response.text
assert response.text.endswith("\\n")
body = response.json()
assert list(body) == ["dataset", "versions", "totals"]
assert body["dataset"] == "orders"
assert [entry["version"] for entry in body["versions"]] == [1, 2]

first = body["versions"][0]
assert list(first) == [
    "version", "tasks", "ready_count", "running_count", "succeeded_count",
    "failed_count", "exhausted_count", "blocked_count",
]
tasks = {task["name"]: task for task in first["tasks"]}
assert [task["id"] for task in first["tasks"]] == sorted(
    task["id"] for task in first["tasks"]
)

assert tasks["succeeded"]["schedule_state"] == "succeeded"
assert tasks["succeeded"]["cause"] is None
assert tasks["succeeded"]["causes"] == []

assert tasks["exhausted"]["schedule_state"] == "exhausted"
assert tasks["exhausted"]["cause"] == "attempts_exhausted"
assert tasks["exhausted"]["causes"] == ["attempts_exhausted"]
assert tasks["exhausted"]["blocking_task_ids"] == []

upstream = tasks["upstream"]
assert upstream["schedule_state"] == "upstream_failed"
assert upstream["causes"] == ["direct_dependency", "upstream_failed"]
assert upstream["cause"] == "upstream_failed"
assert upstream["blocking_task_ids"] == [tasks["exhausted"]["id"]]

assert first["ready_count"] == 0
assert first["running_count"] == 0
assert first["succeeded_count"] == 1
assert first["failed_count"] == 0
assert first["exhausted_count"] == 1
assert first["blocked_count"] == 1
assert sum(first[key] for key in (
    "ready_count", "running_count", "succeeded_count", "failed_count",
    "exhausted_count", "blocked_count",
)) == len(first["tasks"]) == 3

second = body["versions"][1]
assert second["tasks"] == []
assert all(
    second[key] == 0 for key in (
        "ready_count", "running_count", "succeeded_count", "failed_count",
        "exhausted_count", "blocked_count",
    )
)

assert body["totals"] == {
    "version_count": 2,
    "task_count": 3,
    "ready_count": 0,
    "running_count": 0,
    "succeeded_count": 1,
    "failed_count": 0,
    "exhausted_count": 1,
    "blocked_count": 1,
}

# Repeated reads and a fresh request return byte-identical documents.
again = client.get(path)
assert again.status_code == 200, again.text
assert again.text == response.text
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


def test_summary_survives_a_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "processing-blocker-summary.db"
    assert _run_script(db_path, _CREATE_SCRIPT) == "created"
    assert _run_script(db_path, _VERIFY_SCRIPT) == "verified"
