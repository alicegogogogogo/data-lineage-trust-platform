"""Tests for the read-only cross-version processing audit export."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXPORT_PATH = "/datasets/orders/processing-audit-export"
TASKS_V1 = "/datasets/orders/versions/1/processing-tasks"
REPORT_V1 = f"{TASKS_V1}/audit-report"

TOP_LEVEL_KEYS = ["dataset", "versions", "totals"]
VERSION_KEYS = [
    "version",
    "task_count",
    "run_count",
    "invalid_audit_runs",
    "terminal_tasks",
]
TERMINAL_KEYS = ["succeeded_tasks", "failed_tasks", "exhausted_tasks"]
TOTAL_KEYS = [
    "version_count",
    "task_count",
    "run_count",
    "invalid_audit_runs",
    "succeeded_tasks",
    "failed_tasks",
    "exhausted_tasks",
]


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
    dataset: str = "orders",
) -> int:
    response = client.post(
        tasks_path(version, dataset),
        json={"name": name, "max_attempts": max_attempts},
    )
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


def add_event(
    client: TestClient,
    task_id: int,
    run_id: int,
    event: str = "e",
    version: int = 1,
    dataset: str = "orders",
) -> dict:
    path = (
        f"{tasks_path(version, dataset)}/{task_id}/runs/{run_id}/audit-records"
    )
    response = client.post(
        path,
        json={"event": event, "input_summary": "in", "result_summary": "out"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def get_export(client: TestClient, dataset: str = "orders") -> dict:
    response = client.get(f"/datasets/{dataset}/processing-audit-export")
    assert response.status_code == 200, response.text
    return response.json()


def export_response(client: TestClient, dataset: str = "orders"):
    response = client.get(f"/datasets/{dataset}/processing-audit-export")
    assert response.status_code == 200, response.text
    return response


# --------------------------------------------------------------------------- #
# Empty state and wire format
# --------------------------------------------------------------------------- #


def test_export_without_versions_is_empty_not_an_error(client: TestClient) -> None:
    make_dataset(client)
    body = get_export(client)
    assert list(body) == TOP_LEVEL_KEYS
    assert body["dataset"] == "orders"
    assert body["versions"] == []
    assert body["totals"] == {
        "version_count": 0,
        "task_count": 0,
        "run_count": 0,
        "invalid_audit_runs": 0,
        "succeeded_tasks": 0,
        "failed_tasks": 0,
        "exhausted_tasks": 0,
    }


def test_export_is_get_only(client: TestClient) -> None:
    make_dataset(client)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, EXPORT_PATH)
        assert response.status_code == 405, method


def test_export_wire_format_is_compact_with_fixed_key_order_and_newline(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    task_id = create_task(client, "done")
    run = start_run(client, task_id)
    add_event(client, task_id, run["id"])
    finish_run(client, task_id, run["id"], status="succeeded")

    response = export_response(client)
    assert response.headers["content-type"].startswith("application/json")
    text = response.text
    # Compact whitespace: no separator spaces, and the only line break is the
    # single trailing newline.
    assert ": " not in text
    assert ", " not in text
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    assert "\n" not in text[:-1]
    # Lower-case booleans, no scientific notation for the integer counters.
    assert "True" not in text
    assert "False" not in text
    assert "e+" not in text.lower()

    # Fixed key order at every level.
    body = response.json()
    assert list(body) == TOP_LEVEL_KEYS
    assert list(body["totals"]) == TOTAL_KEYS
    entry = body["versions"][0]
    assert list(entry) == VERSION_KEYS
    assert list(entry["terminal_tasks"]) == TERMINAL_KEYS


def test_export_carries_only_counts_no_task_or_run_details(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    task_id = create_task(client, "extract", max_attempts=2)
    run = start_run(client, task_id)
    add_event(client, task_id, run["id"], "boom")
    finish_run(client, task_id, run["id"], status="failed", error="boom")

    entry = get_export(client)["versions"][0]
    # Nothing identifies an individual task or run: no ids, names, statuses,
    # timestamps, errors or proof chains.
    assert set(entry) == set(VERSION_KEYS)
    assert set(entry["terminal_tasks"]) == set(TERMINAL_KEYS)


# --------------------------------------------------------------------------- #
# Versions, counts and terminal distribution
# --------------------------------------------------------------------------- #


def test_export_versions_sort_ascending_and_cover_every_version(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    create_task(client, "v1-task")
    add_version(client)
    create_task(client, "v2-task", version=2)
    add_version(client)  # no tasks at all

    body = get_export(client)
    assert [entry["version"] for entry in body["versions"]] == [1, 2, 3]
    assert [entry["task_count"] for entry in body["versions"]] == [1, 1, 0]
    for entry in body["versions"]:
        assert list(entry) == VERSION_KEYS
        assert entry["run_count"] == 0
        assert entry["invalid_audit_runs"] == 0
        assert entry["terminal_tasks"] == {
            "succeeded_tasks": 0,
            "failed_tasks": 0,
            "exhausted_tasks": 0,
        }


def test_export_counts_match_the_per_version_report(client: TestClient) -> None:
    make_dataset(client)
    add_version(client)

    # Pending: no runs, appears nowhere in the terminal distribution.
    create_task(client, "pending")

    # Running: one running run with an intact chain.
    running_id = create_task(client, "running")
    running_run = start_run(client, running_id)
    for index in range(3):
        add_event(client, running_id, running_run["id"], f"r{index}")

    # Succeeded.
    succeeded_id = create_task(client, "succeeded")
    succeeded_run = start_run(client, succeeded_id)
    add_event(client, succeeded_id, succeeded_run["id"], "s")
    finish_run(client, succeeded_id, succeeded_run["id"], status="succeeded")

    # Failed with attempts left: retryable, not exhausted.
    retryable_id = create_task(client, "retryable", max_attempts=2)
    retryable_run = start_run(client, retryable_id)
    add_event(client, retryable_id, retryable_run["id"], "boom")
    finish_run(
        client, retryable_id, retryable_run["id"],
        status="failed", error="boom",
    )

    # Failed and exhausted (single allowed attempt used).
    exhausted_id = create_task(client, "exhausted", max_attempts=1)
    exhausted_run = start_run(client, exhausted_id)
    finish_run(
        client, exhausted_id, exhausted_run["id"],
        status="failed", error="done",
    )

    report = client.get(REPORT_V1)
    assert report.status_code == 200, report.text
    report_summary = report.json()["summary"]

    entry = get_export(client)["versions"][0]
    assert entry["version"] == 1
    assert entry["task_count"] == report_summary["task_count"] == 5
    assert entry["run_count"] == report_summary["run_count"] == 4
    assert entry["invalid_audit_runs"] == 0
    assert entry["terminal_tasks"] == {
        "succeeded_tasks": 1,
        # retryable + exhausted
        "failed_tasks": 2,
        "exhausted_tasks": 1,
    }
    # Pending and running are deliberately absent from the terminal counts.
    distinct_terminal = (
        entry["terminal_tasks"]["succeeded_tasks"]
        + entry["terminal_tasks"]["failed_tasks"]
    )
    assert distinct_terminal == 3
    # exhausted_tasks is a subset of (never greater than) failed_tasks.
    assert (
        entry["terminal_tasks"]["exhausted_tasks"]
        <= entry["terminal_tasks"]["failed_tasks"]
    )


def test_export_is_scoped_to_the_named_dataset(client: TestClient) -> None:
    make_dataset(client, "orders")
    make_dataset(client, "other")
    add_version(client, "orders")
    add_version(client, "other")
    create_task(client, "orders-task", dataset="orders")
    other_task = create_task(client, "other-task", dataset="other")
    other_run = start_run(client, other_task, dataset="other")
    finish_run(client, other_task, other_run["id"], status="succeeded",
               dataset="other")

    orders = get_export(client, "orders")
    other = get_export(client, "other")
    assert orders["dataset"] == "orders"
    assert other["dataset"] == "other"
    assert orders["versions"][0]["task_count"] == 1
    assert other["versions"][0]["task_count"] == 1
    assert orders["totals"]["task_count"] == 1
    assert other["totals"]["task_count"] == 1
    assert other["versions"][0]["terminal_tasks"]["succeeded_tasks"] == 1
    assert orders["versions"][0]["terminal_tasks"]["succeeded_tasks"] == 0


# --------------------------------------------------------------------------- #
# Tampered chains
# --------------------------------------------------------------------------- #


def test_tampered_chain_counts_as_invalid_without_an_error(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    good_task = create_task(client, "good", max_attempts=2)
    good_run = start_run(client, good_task)
    add_event(client, good_task, good_run["id"], "g")

    bad_task = create_task(client, "bad", max_attempts=2)
    bad_run = start_run(client, bad_task)
    for index in range(3):
        add_event(client, bad_task, bad_run["id"], f"b{index}")
    finish_run(client, bad_task, bad_run["id"], status="failed", error="x")

    with sqlite3.connect(os.environ["DATA_LINEAGE_DB"]) as direct:
        direct.execute("DROP TRIGGER trg_processing_audit_records_no_update")
        direct.execute("DROP TRIGGER trg_processing_audit_records_no_delete")
        direct.execute(
            "UPDATE processing_task_audit_records SET event = ? "
            "WHERE run_id = ? AND sequence = 2",
            ("tampered", bad_run["id"]),
        )

    # The export stays a normal 200; only the invalid counter moves.
    body = get_export(client)
    entry = body["versions"][0]
    assert entry["invalid_audit_runs"] == 1
    assert entry["run_count"] == 2
    assert entry["task_count"] == 2
    assert body["totals"]["invalid_audit_runs"] == 1

    # The per-version report and the task/run details stay readable.
    report = client.get(REPORT_V1)
    assert report.status_code == 200, report.text
    assert report.json()["summary"]["invalid_audit_runs"] == 1
    task_read = client.get(f"{TASKS_V1}/{bad_task}")
    assert task_read.status_code == 200, task_read.text
    assert task_read.json()["runs"][0]["status"] == "failed"


def test_broken_chain_in_another_version_only_counts_there(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    good_task = create_task(client, "good")
    good_run = start_run(client, good_task)
    add_event(client, good_task, good_run["id"])

    add_version(client)
    bad_task = create_task(client, "bad", version=2)
    bad_run = start_run(client, bad_task, version=2)
    for index in range(2):
        add_event(client, bad_task, bad_run["id"], f"b{index}", version=2)

    with sqlite3.connect(os.environ["DATA_LINEAGE_DB"]) as direct:
        direct.execute("DROP TRIGGER trg_processing_audit_records_no_update")
        direct.execute("DROP TRIGGER trg_processing_audit_records_no_delete")
        direct.execute(
            "DELETE FROM processing_task_audit_records "
            "WHERE run_id = ? AND sequence = 1",
            (bad_run["id"],),
        )

    entries = {entry["version"]: entry for entry in get_export(client)["versions"]}
    assert entries[1]["invalid_audit_runs"] == 0
    assert entries[2]["invalid_audit_runs"] == 1
    assert entries[2]["run_count"] == 1


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #


def test_export_totals_equal_the_sum_of_the_version_entries(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    # v1: one succeeded, one exhausted failure with two attempts.
    succeeded_id = create_task(client, "s1")
    succeeded_run = start_run(client, succeeded_id)
    finish_run(client, succeeded_id, succeeded_run["id"], status="succeeded")

    exhausted_id = create_task(client, "f1", max_attempts=2)
    first = start_run(client, exhausted_id)
    finish_run(client, exhausted_id, first["id"], status="failed", error="x")
    second = start_run(client, exhausted_id)
    add_event(client, exhausted_id, second["id"])
    finish_run(client, exhausted_id, second["id"], status="failed", error="y")

    add_version(client)
    # v2: one pending task, one retryable failure, no invalid chains.
    create_task(client, "p2", version=2)
    retryable_id = create_task(client, "f2", version=2, max_attempts=3)
    retryable_run = start_run(client, retryable_id, version=2)
    finish_run(
        client, retryable_id, retryable_run["id"],
        version=2, status="failed", error="z",
    )

    add_version(client)  # v3: no tasks

    body = get_export(client)
    versions = body["versions"]
    totals = body["totals"]
    assert [entry["version"] for entry in versions] == [1, 2, 3]

    assert totals["version_count"] == len(versions) == 3
    assert totals["task_count"] == sum(e["task_count"] for e in versions) == 4
    assert totals["run_count"] == sum(e["run_count"] for e in versions) == 4
    assert totals["invalid_audit_runs"] == sum(
        e["invalid_audit_runs"] for e in versions
    ) == 0
    assert totals["succeeded_tasks"] == sum(
        e["terminal_tasks"]["succeeded_tasks"] for e in versions
    ) == 1
    assert totals["failed_tasks"] == sum(
        e["terminal_tasks"]["failed_tasks"] for e in versions
    ) == 2
    assert totals["exhausted_tasks"] == sum(
        e["terminal_tasks"]["exhausted_tasks"] for e in versions
    ) == 1
    assert totals == {
        "version_count": 3,
        "task_count": 4,
        "run_count": 4,
        "invalid_audit_runs": 0,
        "succeeded_tasks": 1,
        "failed_tasks": 2,
        "exhausted_tasks": 1,
    }


# --------------------------------------------------------------------------- #
# Errors: 404 / 422, no writes
# --------------------------------------------------------------------------- #


def test_export_unknown_dataset_returns_404(client: TestClient) -> None:
    response = client.get("/datasets/ghost/processing-audit-export")
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "not_found"
    assert "sqlite" not in response.text.lower()
    assert "traceback" not in response.text.lower()


def test_export_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset(client)
    before = export_response(client).text

    with_body = client.request("GET", EXPORT_PATH, content=b"{}")
    whitespace_body = client.request("GET", EXPORT_PATH, content=b"   ")
    single_space_body = client.request("GET", EXPORT_PATH, content=b" ")
    with_query = client.get(EXPORT_PATH, params={"limit": 1})
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
    assert export_response(client).text == before


def test_export_shape_errors_keep_404_precedence(client: TestClient) -> None:
    make_dataset(client)
    assert client.get(
        "/datasets/ghost/processing-audit-export", params={"x": "1"}
    ).status_code == 404
    assert (
        client.request(
            "GET", "/datasets/ghost/processing-audit-export", content=b"{}"
        ).status_code
        == 404
    )
    assert (
        client.request(
            "GET",
            "/datasets/ghost/processing-audit-export",
            content=b" ",
        ).status_code
        == 404
    )


def test_export_is_strictly_read_only(client: TestClient) -> None:
    make_dataset(client)
    add_version(client)
    task_id = create_task(client, "done", max_attempts=2)
    run = start_run(client, task_id)
    add_event(client, task_id, run["id"])
    finish_run(client, task_id, run["id"], status="succeeded")

    tasks_before = client.get(TASKS_V1).json()
    report_before = client.get(REPORT_V1).json()
    audit_before = client.get(
        f"{TASKS_V1}/{task_id}/runs/{run['id']}/audit-records"
    ).json()
    first_text = export_response(client).text

    for _ in range(3):
        response = export_response(client)
        assert response.text == first_text
    assert client.get(TASKS_V1).json() == tasks_before
    assert client.get(REPORT_V1).json() == report_before
    assert (
        client.get(
            f"{TASKS_V1}/{task_id}/runs/{run['id']}/audit-records"
        ).json()
        == audit_before
    )


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

succeeded = ok(client.post(base, json={"name": "succeeded"}))
run = ok(client.post(f"{base}/{succeeded['id']}/runs"))
ok(client.post(
    f"{base}/{succeeded['id']}/runs/{run['id']}/audit-records",
    json={"event": "e", "input_summary": "in", "result_summary": "out"},
))
ok(client.patch(
    f"{base}/{succeeded['id']}/runs/{run['id']}",
    json={"status": "succeeded"},
))

failed = ok(client.post(base, json={"name": "exhausted", "max_attempts": 1}))
frun = ok(client.post(f"{base}/{failed['id']}/runs"))
ok(client.patch(
    f"{base}/{failed['id']}/runs/{frun['id']}",
    json={"status": "failed", "error": "boom"},
))

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

path = "/datasets/orders/processing-audit-export"
response = client.get(path)
assert response.status_code == 200, response.text
assert response.text.endswith("\\n")
body = response.json()
assert list(body) == ["dataset", "versions", "totals"]
assert body["dataset"] == "orders"
assert [entry["version"] for entry in body["versions"]] == [1, 2]

first = body["versions"][0]
assert list(first) == [
    "version", "task_count", "run_count", "invalid_audit_runs",
    "terminal_tasks",
]
assert first["task_count"] == 2
assert first["run_count"] == 2
assert first["invalid_audit_runs"] == 0
assert list(first["terminal_tasks"]) == [
    "succeeded_tasks", "failed_tasks", "exhausted_tasks",
]
assert first["terminal_tasks"] == {
    "succeeded_tasks": 1,
    "failed_tasks": 1,
    "exhausted_tasks": 1,
}

second = body["versions"][1]
assert second["task_count"] == 0
assert second["run_count"] == 0
assert second["invalid_audit_runs"] == 0
assert second["terminal_tasks"] == {
    "succeeded_tasks": 0,
    "failed_tasks": 0,
    "exhausted_tasks": 0,
}

assert body["totals"] == {
    "version_count": 2,
    "task_count": 2,
    "run_count": 2,
    "invalid_audit_runs": 0,
    "succeeded_tasks": 1,
    "failed_tasks": 1,
    "exhausted_tasks": 1,
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


def test_export_survives_a_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "processing-audit-export.db"
    assert _run_script(db_path, _CREATE_SCRIPT) == "created"
    assert _run_script(db_path, _VERIFY_SCRIPT) == "verified"
