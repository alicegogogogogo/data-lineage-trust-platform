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

TOP_LEVEL_KEYS = ["dataset", "versions", "totals"]
VERSION_KEYS = [
    "version",
    "task_count",
    "run_count",
    "invalid_audit_runs",
    "succeeded_tasks",
    "failed_tasks",
    "exhausted_tasks",
]
TOTAL_KEYS = VERSION_KEYS[1:]

ZERO_TOTALS = {
    "task_count": 0,
    "run_count": 0,
    "invalid_audit_runs": 0,
    "succeeded_tasks": 0,
    "failed_tasks": 0,
    "exhausted_tasks": 0,
}


def make_dataset(client: TestClient, dataset: str = "orders") -> None:
    response = client.post("/datasets", json={"name": dataset})
    assert response.status_code == 201, response.text


def make_version(client: TestClient, dataset: str = "orders") -> int:
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201, response.text
    return response.json()["version"]


def tasks_path(version: int, dataset: str = "orders") -> str:
    return f"/datasets/{dataset}/versions/{version}/processing-tasks"


def create_task(
    client: TestClient, version: int, name: str, *, max_attempts: int = 1
) -> int:
    response = client.post(
        tasks_path(version), json={"name": name, "max_attempts": max_attempts}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def start_run(client: TestClient, version: int, task_id: int) -> dict:
    response = client.post(f"{tasks_path(version)}/{task_id}/runs")
    assert response.status_code == 201, response.text
    return response.json()


def finish_run(
    client: TestClient,
    version: int,
    task_id: int,
    run_id: int,
    *,
    status: str,
    error: str | None = None,
) -> None:
    payload = {"status": status}
    if error is not None:
        payload["error"] = error
    response = client.patch(
        f"{tasks_path(version)}/{task_id}/runs/{run_id}", json=payload
    )
    assert response.status_code == 200, response.text


def add_event(
    client: TestClient, version: int, task_id: int, run_id: int, event: str = "e"
) -> dict:
    response = client.post(
        f"{tasks_path(version)}/{task_id}/runs/{run_id}/audit-records",
        json={"event": event, "input_summary": "in", "result_summary": "out"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def export_response(client: TestClient, path: str = EXPORT_PATH):
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response


def get_export(client: TestClient, path: str = EXPORT_PATH) -> dict:
    return export_response(client, path).json()


# --------------------------------------------------------------------------- #
# Empty export and response shape
# --------------------------------------------------------------------------- #


def test_empty_export_for_dataset_without_versions(client: TestClient) -> None:
    make_dataset(client)
    body = get_export(client)
    assert list(body) == TOP_LEVEL_KEYS
    assert body["dataset"] == "orders"
    assert body["versions"] == []
    assert body["totals"] == ZERO_TOTALS


def test_export_is_get_only(client: TestClient) -> None:
    make_dataset(client)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, EXPORT_PATH)
        assert response.status_code == 405, method


def test_export_wire_format_is_compact_with_fixed_key_order_and_newline(
    client: TestClient,
) -> None:
    make_dataset(client)
    version = make_version(client)
    task_id = create_task(client, version, "t")
    run = start_run(client, version, task_id)
    add_event(client, version, task_id, run["id"])

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
    # No scientific notation for the integer counters.
    assert "e+" not in text.lower()

    # Key order is fixed at every level.
    positions = [text.index(f'"{key}"') for key in TOP_LEVEL_KEYS]
    assert positions == sorted(positions)
    body = response.json()
    assert list(body) == TOP_LEVEL_KEYS
    assert list(body["totals"]) == TOTAL_KEYS
    assert list(body["versions"][0]) == VERSION_KEYS


# --------------------------------------------------------------------------- #
# Versions, counts and totals
# --------------------------------------------------------------------------- #


def test_export_versions_sort_ascending_and_totals_sum_per_version(
    client: TestClient,
) -> None:
    make_dataset(client)
    first = make_version(client)
    second = make_version(client)
    third = make_version(client)
    assert (first, second, third) == (1, 2, 3)

    # Version 1: one succeeded task with one run, one pending task.
    succeeded = create_task(client, first, "ok")
    run = start_run(client, first, succeeded)
    add_event(client, first, succeeded, run["id"])
    finish_run(client, first, succeeded, run["id"], status="succeeded")
    create_task(client, first, "pending")

    # Version 2: empty.
    # Version 3: one exhausted task and one failed-but-retryable task.
    exhausted = create_task(client, third, "exhausted", max_attempts=1)
    exhausted_run = start_run(client, third, exhausted)
    finish_run(
        client, third, exhausted, exhausted_run["id"],
        status="failed", error="boom",
    )
    retryable = create_task(client, third, "retryable", max_attempts=2)
    retryable_run = start_run(client, third, retryable)
    finish_run(
        client, third, retryable, retryable_run["id"],
        status="failed", error="boom",
    )

    body = get_export(client)
    assert [entry["version"] for entry in body["versions"]] == [1, 2, 3]
    assert body["versions"][0] == {
        "version": 1,
        "task_count": 2,
        "run_count": 1,
        "invalid_audit_runs": 0,
        "succeeded_tasks": 1,
        "failed_tasks": 0,
        "exhausted_tasks": 0,
    }
    assert body["versions"][1] == {"version": 2, **ZERO_TOTALS}
    assert body["versions"][2] == {
        "version": 3,
        "task_count": 2,
        "run_count": 2,
        "invalid_audit_runs": 0,
        "succeeded_tasks": 0,
        "failed_tasks": 2,
        "exhausted_tasks": 1,
    }
    # Every total is the sum of the per-version values.
    assert body["totals"] == {
        key: sum(entry[key] for entry in body["versions"]) for key in TOTAL_KEYS
    }
    assert body["totals"] == {
        "task_count": 4,
        "run_count": 3,
        "invalid_audit_runs": 0,
        "succeeded_tasks": 1,
        "failed_tasks": 2,
        "exhausted_tasks": 1,
    }


def test_export_counts_only_no_task_or_run_details(client: TestClient) -> None:
    make_dataset(client)
    version = make_version(client)
    task_id = create_task(client, version, "t")
    run = start_run(client, version, task_id)
    add_event(client, version, task_id, run["id"])

    body = get_export(client)
    entry = body["versions"][0]
    assert set(entry) == set(VERSION_KEYS)
    # No per-task or per-run expansion anywhere in the document.
    assert "tasks" not in entry
    assert "runs" not in entry


def test_tampered_chain_counts_run_invalid_and_details_stay_readable(
    client: TestClient,
) -> None:
    make_dataset(client)
    version = make_version(client)
    good_task = create_task(client, version, "good")
    good_run = start_run(client, version, good_task)
    add_event(client, version, good_task, good_run["id"], "g0")

    bad_task = create_task(client, version, "bad")
    bad_run = start_run(client, version, bad_task)
    for index in range(2):
        add_event(client, version, bad_task, bad_run["id"], f"b{index}")

    from app.db import database_path

    db_file = database_path()
    # Audit records are immutable through normal connections; bypass the
    # triggers to simulate out-of-band tampering.
    with sqlite3.connect(db_file) as direct:
        direct.execute("DROP TRIGGER trg_processing_audit_records_no_update")
        direct.execute("DROP TRIGGER trg_processing_audit_records_no_delete")
        direct.execute(
            "UPDATE processing_task_audit_records SET event = ? "
            "WHERE run_id = ? AND sequence = 2",
            ("tampered", bad_run["id"]),
        )

    body = get_export(client)
    entry = body["versions"][0]
    assert entry["task_count"] == 2
    assert entry["run_count"] == 2
    assert entry["invalid_audit_runs"] == 1
    assert body["totals"]["invalid_audit_runs"] == 1

    # Task, run and audit-record details remain readable.
    report = client.get(f"{tasks_path(version)}/audit-report")
    assert report.status_code == 200, report.text
    assert report.json()["summary"]["invalid_audit_runs"] == 1
    records = client.get(
        f"{tasks_path(version)}/{bad_task}/runs/{bad_run['id']}/audit-records"
    )
    assert records.status_code == 200, records.text
    assert len(records.json()) == 2


# --------------------------------------------------------------------------- #
# Errors: 404 / 422, no writes
# --------------------------------------------------------------------------- #


def test_export_unknown_dataset_returns_404(client: TestClient) -> None:
    response = client.get(EXPORT_PATH)
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}


def test_export_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset(client)
    make_version(client)
    with_body = client.request(
        "GET",
        EXPORT_PATH,
        content=b'{"limit": 1}',
        headers={"content-type": "application/json"},
    )
    whitespace_body = client.request("GET", EXPORT_PATH, content=b"   ")
    single_space_body = client.request("GET", EXPORT_PATH, content=b" ")
    with_query = client.get(EXPORT_PATH, params={"limit": 1})
    assert with_body.status_code == 422
    assert whitespace_body.status_code == 422
    assert single_space_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, whitespace_body, single_space_body, with_query):
        payload = response.json()
        assert set(payload) == {"error", "detail"}
        # No SQL or stack traces leak through the error payload.
        assert "SELECT" not in payload["detail"]
        assert "Traceback" not in payload["detail"]


def test_export_shape_errors_keep_404_precedence(client: TestClient) -> None:
    assert client.get(EXPORT_PATH, params={"limit": 1}).status_code == 404
    assert (
        client.request(
            "GET",
            EXPORT_PATH,
            content=b" ",
            headers={"content-type": "application/json"},
        ).status_code
        == 404
    )


def test_rejected_request_changes_nothing(client: TestClient) -> None:
    make_dataset(client)
    version = make_version(client)
    task_id = create_task(client, version, "t")
    run = start_run(client, version, task_id)
    add_event(client, version, task_id, run["id"])
    before = get_export(client)

    client.get(EXPORT_PATH + "?bogus=1")
    client.request(
        "GET",
        EXPORT_PATH,
        content=b'{"bogus": 1}',
        headers={"content-type": "application/json"},
    )
    after = get_export(client)
    assert after == before


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


CREATE_SCRIPT = """
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
ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
))
base = "/datasets/orders/versions/2/processing-tasks"

ok(client.post(base, json={"name": "pending"}))

running = ok(client.post(base, json={"name": "running", "max_attempts": 2}))
run = ok(client.post(f"{base}/{running['id']}/runs"))
audit = f"{base}/{running['id']}/runs/{run['id']}/audit-records"
for index in range(2):
    ok(client.post(audit, json={
        "event": f"e{index}",
        "input_summary": "in",
        "result_summary": "out",
    }))

failed = ok(client.post(base, json={"name": "exhausted", "max_attempts": 1}))
frun = ok(client.post(f"{base}/{failed['id']}/runs"))
faudit = f"{base}/{failed['id']}/runs/{frun['id']}/audit-records"
ok(client.post(faudit, json={"event": "x", "input_summary": "i", "result_summary": "o"}))
ok(client.patch(
    f"{base}/{failed['id']}/runs/{frun['id']}",
    json={"status": "failed", "error": "boom"},
))
print("created")
"""

VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
path = "/datasets/orders/processing-audit-export"
response = client.get(path)
assert response.status_code == 200, response.text
body = response.json()
assert body == {
    "dataset": "orders",
    "versions": [
        {
            "version": 1,
            "task_count": 0,
            "run_count": 0,
            "invalid_audit_runs": 0,
            "succeeded_tasks": 0,
            "failed_tasks": 0,
            "exhausted_tasks": 0,
        },
        {
            "version": 2,
            "task_count": 3,
            "run_count": 2,
            "invalid_audit_runs": 0,
            "succeeded_tasks": 0,
            "failed_tasks": 1,
            "exhausted_tasks": 1,
        },
    ],
    "totals": {
        "task_count": 3,
        "run_count": 2,
        "invalid_audit_runs": 0,
        "succeeded_tasks": 0,
        "failed_tasks": 1,
        "exhausted_tasks": 1,
    },
}

# A second read returns the identical document, byte for byte.
again = client.get(path)
assert again.status_code == 200, again.text
assert again.content == response.content
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


def test_processing_audit_export_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-export.db"
    assert _run_script(db_path, CREATE_SCRIPT) == "created"
    assert db_path.exists()
    assert _run_script(db_path, VERIFY_SCRIPT) == "verified"
