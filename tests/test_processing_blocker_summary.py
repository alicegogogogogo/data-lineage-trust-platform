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
REPORT_V1 = f"{TASKS_V1}/audit-report"

TOP_LEVEL_KEYS = ["dataset", "versions", "totals"]
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
VERSION_KEYS = ["version", "tasks", *COUNT_KEYS]
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
    response = client.post(
        tasks_path(version, dataset),
        json={
            "name": name,
            "max_attempts": max_attempts,
            "depends_on": depends_on or [],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def start_run(
    client: TestClient, task_id: int, version: int = 1, dataset: str = "orders"
) -> int:
    response = client.post(f"{tasks_path(version, dataset)}/{task_id}/runs")
    assert response.status_code == 201, response.text
    return response.json()["id"]


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


def exhaust(
    client: TestClient, task_id: int, version: int = 1, dataset: str = "orders"
) -> None:
    """Fail a single-attempt task so it is exhausted."""
    finish_run(
        client,
        task_id,
        start_run(client, task_id, version, dataset),
        status="failed",
        error="boom",
        version=version,
        dataset=dataset,
    )


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
    create_task(client, "ready")
    blocked_dep = create_task(client, "pending-dep")
    create_task(client, "blocked", depends_on=[blocked_dep])

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
    for task in entry["tasks"]:
        assert list(task) == TASK_KEYS
        assert list(task["causes"]) == sorted(task["causes"])


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #


def test_summary_versions_and_tasks_sort_ascending(client: TestClient) -> None:
    make_dataset(client)
    add_version(client)
    v1_ids = [
        create_task(client, "v1-gamma"),
        create_task(client, "v1-alpha"),
        create_task(client, "v1-beta"),
    ]
    add_version(client)
    v2_id = create_task(client, "v2-only", version=2)
    add_version(client)  # a version without tasks still appears

    body = get_summary(client)
    assert [entry["version"] for entry in body["versions"]] == [1, 2, 3]
    assert [task["id"] for task in body["versions"][0]["tasks"]] == sorted(v1_ids)
    assert [task["id"] for task in body["versions"][1]["tasks"]] == [v2_id]
    assert body["versions"][2]["tasks"] == []


def test_summary_is_scoped_to_the_named_dataset(client: TestClient) -> None:
    make_dataset(client, "orders")
    make_dataset(client, "other")
    add_version(client, "orders")
    add_version(client, "other")
    create_task(client, "orders-task", dataset="orders")
    other_id = create_task(client, "other-exhausted", dataset="other")
    exhaust(client, other_id, dataset="other")

    orders = get_summary(client, "orders")
    other = get_summary(client, "other")
    assert orders["dataset"] == "orders"
    assert other["dataset"] == "other"
    assert orders["versions"][0]["ready_count"] == 1
    assert other["versions"][0]["exhausted_count"] == 1
    assert orders["totals"]["task_count"] == 1
    assert other["totals"]["task_count"] == 1

# --------------------------------------------------------------------------- #
# Schedule states, blockers and the six-bucket counts
# --------------------------------------------------------------------------- #


def _build_all_seven_schedule_states(client: TestClient) -> dict[str, int]:
    make_dataset(client)
    add_version(client)
    ids = {}
    ids["ready"] = create_task(client, "ready")
    ids["running"] = create_task(client, "running", max_attempts=2)
    start_run(client, ids["running"])
    ids["succeeded"] = create_task(client, "succeeded")
    finish_run(
        client, ids["succeeded"], start_run(client, ids["succeeded"]),
        status="succeeded",
    )
    ids["retryable"] = create_task(client, "retryable", max_attempts=2)
    finish_run(
        client, ids["retryable"], start_run(client, ids["retryable"]),
        status="failed", error="x",
    )
    ids["exhausted"] = create_task(client, "exhausted", max_attempts=1)
    exhaust(client, ids["exhausted"])
    ids["blocked-dep"] = create_task(client, "blocked-dep")
    ids["blocked"] = create_task(client, "blocked", depends_on=[ids["blocked-dep"]])
    ids["upstream"] = create_task(
        client, "upstream", depends_on=[ids["exhausted"]]
    )
    return ids


def test_summary_six_counts_partition_the_seven_schedule_states(
    client: TestClient,
) -> None:
    ids = _build_all_seven_schedule_states(client)
    entry = get_summary(client)["versions"][0]

    # Eight tasks spanning all seven schedule states (the blocked task's own
    # pending dependency is ready, so the ready bucket holds two); the six
    # buckets partition them.
    assert entry["ready_count"] == 2
    assert entry["running_count"] == 1
    assert entry["succeeded_count"] == 1
    # The retryable failed task is counted as failed.
    assert entry["failed_count"] == 1
    assert entry["exhausted_count"] == 1
    # The blocked and upstream_failed pending tasks merge into blocked.
    assert entry["blocked_count"] == 2
    assert len(entry["tasks"]) == 8
    assert sum(entry[key] for key in COUNT_KEYS) == len(entry["tasks"]) == 8

    tasks = by_name(entry["tasks"])
    expected_states = {
        "ready": "ready",
        "running": "running",
        "succeeded": "succeeded",
        "retryable": "retryable",
        "exhausted": "exhausted",
        "blocked": "blocked",
        "upstream": "upstream_failed",
    }
    for name, state in expected_states.items():
        assert tasks[name]["schedule_state"] == state
    assert {task["id"] for task in entry["tasks"]} == set(ids.values())


def test_summary_agrees_with_the_per_version_schedule_view(
    client: TestClient,
) -> None:
    _build_all_seven_schedule_states(client)
    schedule = client.get(SCHEDULE_V1)
    assert schedule.status_code == 200, schedule.text
    scheduled = {task["id"]: task for task in schedule.json()["tasks"]}

    entry = get_summary(client)["versions"][0]
    assert [task["id"] for task in entry["tasks"]] == sorted(scheduled)
    for task in entry["tasks"]:
        full = scheduled[task["id"]]
        assert task["name"] == full["name"]
        assert task["status"] == full["status"]
        assert task["schedule_state"] == full["schedule_state"]
        assert task["blocking_task_ids"] == full["blocking_task_ids"]


def test_summary_blocking_task_ids_are_pending_only_sorted_and_deduped(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    succeeded_dep = create_task(client, "succeeded-dep")
    finish_run(
        client, succeeded_dep, start_run(client, succeeded_dep),
        status="succeeded",
    )
    pending_a = create_task(client, "pending-a")
    pending_b = create_task(client, "pending-b")
    target = create_task(
        client,
        "target",
        depends_on=[succeeded_dep, pending_b, pending_a],
    )

    tasks = by_name(get_summary(client)["versions"][0]["tasks"])
    # Succeeded dependencies drop out; the unsucceeded ones are id-ascending.
    assert tasks["target"]["blocking_task_ids"] == sorted(
        [pending_a, pending_b]
    )
    # Non-pending tasks never carry blockers.
    exhausted = create_task(client, "exhausted-dep")
    exhaust(client, exhausted)
    waiting = create_task(client, "waiting", depends_on=[exhausted, pending_a])
    failed_leaf = create_task(client, "failed-leaf", max_attempts=1)
    exhaust(client, failed_leaf)

    tasks = by_name(get_summary(client)["versions"][0]["tasks"])
    assert tasks["failed-leaf"]["status"] == "failed"
    assert tasks["failed-leaf"]["blocking_task_ids"] == []
    # The waiting task lists every unsucceeded direct dependency, including
    # the exhausted one, id-ascending.
    assert tasks["waiting"]["blocking_task_ids"] == sorted(
        [exhausted, pending_a]
    )
    assert tasks["exhausted-dep"]["blocking_task_ids"] == []


# --------------------------------------------------------------------------- #
# Blocker categories, causes and precedence
# --------------------------------------------------------------------------- #


def test_summary_tasks_without_a_blocker_have_null_cause_and_empty_causes(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    ready = create_task(client, "ready")
    running = create_task(client, "running", max_attempts=2)
    start_run(client, running)
    succeeded = create_task(client, "succeeded")
    finish_run(client, succeeded, start_run(client, succeeded), status="succeeded")
    # A retryable failure is itself re-runnable: it is not "attempts exhausted".
    retryable = create_task(client, "retryable", max_attempts=2)
    finish_run(
        client, retryable, start_run(client, retryable),
        status="failed", error="x",
    )
    # A pending task whose dependencies all succeeded is ready, not blocked.
    done_dep = create_task(client, "done-dep")
    finish_run(client, done_dep, start_run(client, done_dep), status="succeeded")
    ready_after = create_task(client, "ready-after", depends_on=[done_dep])

    tasks = by_name(get_summary(client)["versions"][0]["tasks"])
    for name in ("ready", "running", "succeeded", "retryable", "ready-after"):
        assert tasks[name]["cause"] is None, name
        assert tasks[name]["causes"] == [], name


def test_summary_exhausted_task_matches_attempts_exhausted(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    exhausted = create_task(client, "exhausted", max_attempts=1)
    exhaust(client, exhausted)

    task = by_name(get_summary(client)["versions"][0]["tasks"])["exhausted"]
    assert task["status"] == "failed"
    assert task["schedule_state"] == "exhausted"
    assert task["cause"] == "attempts_exhausted"
    assert task["causes"] == ["attempts_exhausted"]


def test_summary_blocked_task_matches_direct_dependency_only(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    pending_dep = create_task(client, "pending-dep")
    # A retryable (not yet exhausted) dependency keeps the wait recoverable.
    retryable_dep = create_task(client, "retryable-dep", max_attempts=2)
    finish_run(
        client, retryable_dep, start_run(client, retryable_dep),
        status="failed", error="x",
    )
    blocked = create_task(
        client, "blocked", depends_on=[pending_dep, retryable_dep]
    )
    assert blocked

    task = by_name(get_summary(client)["versions"][0]["tasks"])["blocked"]
    assert task["schedule_state"] == "blocked"
    assert task["cause"] == "direct_dependency"
    assert task["causes"] == ["direct_dependency"]
    assert task["blocking_task_ids"] == sorted([pending_dep, retryable_dep])


def test_summary_failed_upstream_hits_both_dependency_categories(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    root = create_task(client, "root")
    exhaust(client, root)
    # Directly waits on the exhausted task: both a direct dependency and a
    # failed upstream chain; the primary cause takes upstream_failed.
    direct = create_task(client, "direct", depends_on=[root])
    # ...and the classification propagates an arbitrary distance.
    middle = create_task(client, "middle", depends_on=[direct])
    leaf = create_task(client, "leaf", depends_on=[middle])
    # A genuinely succeeded sibling dependency does not clear the failed chain.
    ok = create_task(client, "ok")
    finish_run(client, ok, start_run(client, ok), status="succeeded")
    join = create_task(client, "join", depends_on=[leaf, ok])

    tasks = by_name(get_summary(client)["versions"][0]["tasks"])
    for name, blocker in (
        ("direct", root),
        ("middle", direct),
        ("leaf", middle),
    ):
        task = tasks[name]
        assert task["status"] == "pending"
        assert task["schedule_state"] == "upstream_failed"
        assert task["causes"] == ["direct_dependency", "upstream_failed"]
        assert task["cause"] == "upstream_failed"
        assert task["blocking_task_ids"] == [blocker]
    # The succeeded dependency drops out of the blocker list; the failed chain
    # (several hops away here) still classifies the join as upstream_failed.
    assert tasks["join"]["schedule_state"] == "upstream_failed"
    assert tasks["join"]["causes"] == ["direct_dependency", "upstream_failed"]
    assert tasks["join"]["cause"] == "upstream_failed"
    assert tasks["join"]["blocking_task_ids"] == [leaf]


def test_summary_causes_are_alphabetical_for_multi_category_hits(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    root = create_task(client, "root")
    exhaust(client, root)
    create_task(client, "direct", depends_on=[root])

    task = by_name(get_summary(client)["versions"][0]["tasks"])["direct"]
    assert task["causes"] == sorted(task["causes"])
    assert task["causes"] == ["direct_dependency", "upstream_failed"]


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #


def test_summary_totals_equal_the_sum_of_the_version_entries(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    # v1: ready + running + succeeded + retryable(failed) + exhausted + blocked.
    create_task(client, "v1-ready")
    running = create_task(client, "v1-running", max_attempts=2)
    start_run(client, running)
    succeeded = create_task(client, "v1-succeeded")
    finish_run(client, succeeded, start_run(client, succeeded), status="succeeded")
    retryable = create_task(client, "v1-retryable", max_attempts=2)
    finish_run(
        client, retryable, start_run(client, retryable),
        status="failed", error="x",
    )
    exhausted = create_task(client, "v1-exhausted")
    exhaust(client, exhausted)
    create_task(
        client, "v1-upstream", depends_on=[exhausted]
    )

    add_version(client)
    # v2: one blocked pending task only.
    pending = create_task(client, "v2-pending", version=2)
    create_task(client, "v2-blocked", version=2, depends_on=[pending])

    add_version(client)  # v3: no tasks

    body = get_summary(client)
    versions = body["versions"]
    totals = body["totals"]
    assert [entry["version"] for entry in versions] == [1, 2, 3]

    assert versions[0]["ready_count"] == 1
    assert versions[0]["running_count"] == 1
    assert versions[0]["succeeded_count"] == 1
    assert versions[0]["failed_count"] == 1
    assert versions[0]["exhausted_count"] == 1
    assert versions[0]["blocked_count"] == 1
    assert versions[1]["ready_count"] == 1
    assert versions[1]["blocked_count"] == 1
    for entry in versions:
        assert sum(entry[key] for key in COUNT_KEYS) == len(entry["tasks"])
        assert list(entry) == VERSION_KEYS

    assert totals["version_count"] == len(versions) == 3
    assert totals["task_count"] == sum(len(e["tasks"]) for e in versions) == 8
    assert totals == {
        "version_count": 3,
        "task_count": 8,
        "ready_count": 2,
        "running_count": 1,
        "succeeded_count": 1,
        "failed_count": 1,
        "exhausted_count": 1,
        "blocked_count": 2,
    }
    for key in COUNT_KEYS:
        assert totals[key] == sum(entry[key] for entry in versions)


# --------------------------------------------------------------------------- #
# Errors: 404 / 422 / 405, no writes
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


# --------------------------------------------------------------------------- #
# Strictly read-only
# --------------------------------------------------------------------------- #


def test_summary_is_strictly_read_only(client: TestClient) -> None:
    ids = _build_all_seven_schedule_states(client)

    tasks_before = client.get(TASKS_V1).json()
    schedule_before = client.get(SCHEDULE_V1).json()
    report_before = client.get(REPORT_V1).json()
    first_text = summary_response(client).text

    for _ in range(3):
        response = summary_response(client)
        assert response.text == first_text
    assert client.get(TASKS_V1).json() == tasks_before
    assert client.get(SCHEDULE_V1).json() == schedule_before
    assert client.get(REPORT_V1).json() == report_before
    # Dependencies are untouched as well.
    for task_id in ids.values():
        detail = client.get(f"{TASKS_V1}/{task_id}")
        assert detail.status_code == 200
        assert detail.json()["id"] == task_id


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


_CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def ok(response):
    assert response.status_code in (200, 201), response.text
    return response.json()


ok(client.post("/datasets", json={"name": "orders"}))
ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
))
base = "/datasets/orders/versions/1/processing-tasks"

ready = ok(client.post(base, json={"name": "ready"}))
running = ok(client.post(base, json={"name": "running", "max_attempts": 2}))
ok(client.post(f"{base}/{running['id']}/runs"))
succeeded = ok(client.post(base, json={"name": "succeeded"}))
run = ok(client.post(f"{base}/{succeeded['id']}/runs"))
ok(client.patch(
    f"{base}/{succeeded['id']}/runs/{run['id']}",
    json={"status": "succeeded"},
))
retryable = ok(client.post(base, json={"name": "retryable", "max_attempts": 2}))
run = ok(client.post(f"{base}/{retryable['id']}/runs"))
ok(client.patch(
    f"{base}/{retryable['id']}/runs/{run['id']}",
    json={"status": "failed", "error": "boom"},
))
exhausted = ok(client.post(base, json={"name": "exhausted", "max_attempts": 1}))
run = ok(client.post(f"{base}/{exhausted['id']}/runs"))
ok(client.patch(
    f"{base}/{exhausted['id']}/runs/{run['id']}",
    json={"status": "failed", "error": "done"},
))
pending = ok(client.post(base, json={"name": "pending-dep"}))
ok(client.post(base, json={"name": "blocked", "depends_on": [pending['id']]}))
ok(client.post(base, json={"name": "upstream", "depends_on": [exhausted['id']]}))

ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
))
ok(client.post(
    "/datasets/orders/versions/2/processing-tasks", json={"name": "v2-ready"}
))
print("created")
"""

_VERIFY_SCRIPT = r"""
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

path = "/datasets/orders/processing-blocker-summary"
response = client.get(path)
assert response.status_code == 200, response.text
assert response.text.endswith("\n")
assert not response.text.endswith("\n\n")
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
for task in first["tasks"]:
    assert list(task) == [
        "id", "name", "status", "schedule_state", "blocking_task_ids",
        "cause", "causes",
    ]

expected = {
    "ready": ("pending", "ready", None, []),
    "running": ("running", "running", None, []),
    "succeeded": ("succeeded", "succeeded", None, []),
    "retryable": ("failed", "retryable", None, []),
    "exhausted": (
        "failed", "exhausted", "attempts_exhausted", ["attempts_exhausted"],
    ),
    "pending-dep": ("pending", "ready", None, []),
    "blocked": (
        "pending", "blocked", "direct_dependency", ["direct_dependency"],
    ),
    "upstream": (
        "pending", "upstream_failed", "upstream_failed",
        ["direct_dependency", "upstream_failed"],
    ),
}
for name, (status, state, cause, causes) in expected.items():
    task = tasks[name]
    assert task["status"] == status, (name, task)
    assert task["schedule_state"] == state, (name, task)
    assert task["cause"] == cause, (name, task)
    assert task["causes"] == causes, (name, task)

assert first["ready_count"] == 2  # ready + pending-dep
assert first["running_count"] == 1
assert first["succeeded_count"] == 1
assert first["failed_count"] == 1  # the retryable task
assert first["exhausted_count"] == 1
assert first["blocked_count"] == 2  # blocked + upstream
assert sum(first[k] for k in (
    "ready_count", "running_count", "succeeded_count", "failed_count",
    "exhausted_count", "blocked_count",
)) == len(first["tasks"]) == 8

second = body["versions"][1]
assert second["ready_count"] == 1
assert len(second["tasks"]) == 1

assert body["totals"] == {
    "version_count": 2,
    "task_count": 9,
    "ready_count": 3,
    "running_count": 1,
    "succeeded_count": 1,
    "failed_count": 1,
    "exhausted_count": 1,
    "blocked_count": 2,
}
assert list(body["totals"]) == [
    "version_count", "task_count", "ready_count", "running_count",
    "succeeded_count", "failed_count", "exhausted_count", "blocked_count",
]

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
