"""Tests for the read-only per-version processing audit report."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

REPORT_PATH = "/datasets/orders/versions/1/processing-tasks/audit-report"
TASKS_PATH = "/datasets/orders/versions/1/processing-tasks"

SUMMARY_FIELDS = {
    "task_count",
    "run_count",
    "pending_tasks",
    "running_tasks",
    "succeeded_tasks",
    "failed_tasks",
    "exhausted_tasks",
    "invalid_audit_runs",
}
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
    "runs",
}
RUN_FIELDS = {
    "id",
    "task_id",
    "attempt",
    "status",
    "started_at",
    "finished_at",
    "error",
    "proof",
}
PROOF_FIELDS = {"valid", "checked_count", "last_evidence_hash"}


def setup_version(client: TestClient, dataset: str = "orders") -> None:
    response = client.post("/datasets", json={"name": dataset})
    assert response.status_code == 201, response.text
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201, response.text


def create_task(client: TestClient, name: str, *, max_attempts: int = 1) -> int:
    response = client.post(
        TASKS_PATH, json={"name": name, "max_attempts": max_attempts}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def start_run(client: TestClient, task_id: int) -> dict:
    response = client.post(f"{TASKS_PATH}/{task_id}/runs")
    assert response.status_code == 201, response.text
    return response.json()


def finish_run(
    client: TestClient, task_id: int, run_id: int, *, status: str, error: str | None = None
) -> None:
    payload = {"status": status}
    if error is not None:
        payload["error"] = error
    response = client.patch(
        f"{TASKS_PATH}/{task_id}/runs/{run_id}", json=payload
    )
    assert response.status_code == 200, response.text


def add_event(client: TestClient, task_id: int, run_id: int, event: str = "e") -> dict:
    path = f"{TASKS_PATH}/{task_id}/runs/{run_id}/audit-records"
    response = client.post(
        path,
        json={"event": event, "input_summary": "in", "result_summary": "out"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def get_report(client: TestClient, path: str = REPORT_PATH):
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Empty report and response shape
# --------------------------------------------------------------------------- #


def test_empty_report_for_version_without_tasks(client: TestClient) -> None:
    setup_version(client)
    body = get_report(client)
    assert set(body) == {"dataset", "version", "summary", "tasks"}
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["tasks"] == []
    assert set(body["summary"]) == SUMMARY_FIELDS
    assert body["summary"] == {
        "task_count": 0,
        "run_count": 0,
        "pending_tasks": 0,
        "running_tasks": 0,
        "succeeded_tasks": 0,
        "failed_tasks": 0,
        "exhausted_tasks": 0,
        "invalid_audit_runs": 0,
    }


def test_report_targets_the_named_version(client: TestClient) -> None:
    setup_version(client)
    create_task(client, "only-in-v1")
    client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    v1 = get_report(client, REPORT_PATH)
    v2_path = "/datasets/orders/versions/2/processing-tasks/audit-report"
    v2 = get_report(client, v2_path)
    assert v1["summary"]["task_count"] == 1
    assert v1["version"] == 1
    assert v2["summary"]["task_count"] == 0
    assert v2["version"] == 2
    assert v2["tasks"] == []


# --------------------------------------------------------------------------- #
# Summary statistics and consistency with the details
# --------------------------------------------------------------------------- #


def _build_mixed_report(client: TestClient) -> dict:
    """Create tasks covering every status and audit-chain shape."""
    setup_version(client)

    # 1) pending, no runs
    pending_id = create_task(client, "pending")

    # 2) running with one running run carrying a 3-record chain
    running_id = create_task(client, "running")
    running_run = start_run(client, running_id)
    running_records = [
        add_event(client, running_id, running_run["id"], f"e{i}") for i in range(3)
    ]

    # 3) succeeded with one succeeded run carrying a 2-record chain
    succeeded_id = create_task(client, "succeeded")
    succeeded_run = start_run(client, succeeded_id)
    succeeded_records = [
        add_event(client, succeeded_id, succeeded_run["id"], f"s{i}") for i in range(2)
    ]
    finish_run(client, succeeded_id, succeeded_run["id"], status="succeeded")
    # Records can be appended after the run finishes; extend to 3 records.
    add_event(client, succeeded_id, succeeded_run["id"], "s2")

    # 4) failed with attempts left: one failed run, chain intact (1 record)
    retryable_id = create_task(client, "retryable", max_attempts=2)
    retryable_run = start_run(client, retryable_id)
    add_event(client, retryable_id, retryable_run["id"], "boom")
    finish_run(client, retryable_id, retryable_run["id"], status="failed", error="boom")

    # 5) failed and exhausted: one failed run with an empty audit chain
    exhausted_id = create_task(client, "exhausted", max_attempts=1)
    start_run(client, exhausted_id)
    finish_run(client, exhausted_id, _run_id_of(client, exhausted_id), status="failed", error="done")

    return {
        "pending": pending_id,
        "running": (running_id, running_run["id"], running_records),
        "succeeded": (succeeded_id, succeeded_run["id"], succeeded_records),
        "retryable": (retryable_id, retryable_run["id"]),
        "exhausted": exhausted_id,
    }


def _run_id_of(client: TestClient, task_id: int) -> int:
    response = client.get(f"{TASKS_PATH}/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()["runs"][0]["id"]


def test_summary_counts_match_statuses_and_runs(client: TestClient) -> None:
    refs = _build_mixed_report(client)
    body = get_report(client)

    assert body["summary"] == {
        "task_count": 5,
        "run_count": 4,
        "pending_tasks": 1,
        "running_tasks": 1,
        "succeeded_tasks": 1,
        "failed_tasks": 2,
        "exhausted_tasks": 1,
        "invalid_audit_runs": 0,
    }

    # The summary is recomputed from the same details: counters must agree.
    tasks = body["tasks"]
    assert len(tasks) == body["summary"]["task_count"]
    assert sum(len(task["runs"]) for task in tasks) == body["summary"]["run_count"]
    assert sum(1 for t in tasks if t["status"] == "pending") == body["summary"]["pending_tasks"]
    assert sum(1 for t in tasks if t["status"] == "running") == body["summary"]["running_tasks"]
    assert sum(1 for t in tasks if t["status"] == "succeeded") == body["summary"]["succeeded_tasks"]
    failed = [t for t in tasks if t["status"] == "failed"]
    assert len(failed) == body["summary"]["failed_tasks"]
    exhausted = [
        t for t in failed if t["attempt_count"] >= t["max_attempts"]
    ]
    assert len(exhausted) == body["summary"]["exhausted_tasks"]
    assert [t["id"] for t in exhausted] == [refs["exhausted"]]
    invalid = [
        run
        for task in tasks
        for run in task["runs"]
        if not run["proof"]["valid"]
    ]
    assert len(invalid) == body["summary"]["invalid_audit_runs"] == 0


def test_task_and_run_fields_and_proofs(client: TestClient) -> None:
    refs = _build_mixed_report(client)
    body = get_report(client)
    by_id = {task["id"]: task for task in body["tasks"]}

    # Task shape: every processing-task field plus runs.
    for task in body["tasks"]:
        assert set(task) == TASK_FIELDS
        assert task["dataset"] == "orders"
        assert task["version"] == 1
        for run in task["runs"]:
            assert set(run) == RUN_FIELDS
            assert set(run["proof"]) == PROOF_FIELDS

    # Pending task has no runs.
    assert by_id[refs["pending"]]["runs"] == []

    # Running run: intact 3-record chain, last hash equals the final record.
    running_task = by_id[refs["running"][0]]
    running_proof = running_task["runs"][0]["proof"]
    assert running_proof == {
        "valid": True,
        "checked_count": 3,
        "last_evidence_hash": refs["running"][2][-1]["evidence_hash"],
    }

    # Succeeded run: the post-finish append is part of the same valid chain.
    succeeded_task = by_id[refs["succeeded"][0]]
    succeeded_run = succeeded_task["runs"][0]
    assert succeeded_run["status"] == "succeeded"
    assert succeeded_run["proof"]["valid"] is True
    assert succeeded_run["proof"]["checked_count"] == 3
    final_record = add_event(
        client, refs["succeeded"][0], refs["succeeded"][1], "s3"
    )
    body = get_report(client)
    refreshed = {t["id"]: t for t in body["tasks"]}[refs["succeeded"][0]]
    proof = refreshed["runs"][0]["proof"]
    assert proof["valid"] is True
    assert proof["checked_count"] == 4
    assert proof["last_evidence_hash"] == final_record["evidence_hash"]

    # Exhausted failed run has an empty chain: valid, zero records, null hash.
    exhausted_task = by_id[refs["exhausted"]]
    assert exhausted_task["runs"][0]["proof"] == {
        "valid": True,
        "checked_count": 0,
        "last_evidence_hash": None,
    }
    assert exhausted_task["runs"][0]["error"] == "done"


def test_tasks_sorted_by_id_and_runs_by_attempt(client: TestClient) -> None:
    setup_version(client)
    multi_id = create_task(client, "multi", max_attempts=3)
    # Two failed attempts followed by a third running attempt on one task.
    for _ in range(2):
        run = start_run(client, multi_id)
        add_event(client, multi_id, run["id"])
        finish_run(client, multi_id, run["id"], status="failed", error="again")
    third = start_run(client, multi_id)
    create_task(client, "later")

    body = get_report(client)
    task_ids = [task["id"] for task in body["tasks"]]
    assert task_ids == sorted(task_ids)
    multi = next(task for task in body["tasks"] if task["id"] == multi_id)
    assert [run["attempt"] for run in multi["runs"]] == [1, 2, 3]
    assert [run["id"] for run in multi["runs"]] == sorted(
        run["id"] for run in multi["runs"]
    )
    assert multi["runs"][-1]["id"] == third["id"]
    assert multi["attempt_count"] == 3
    assert multi["status"] == "running"


# --------------------------------------------------------------------------- #
# Tampered chains
# --------------------------------------------------------------------------- #


def test_tampered_chain_marks_proof_invalid_but_keeps_details(
    client: TestClient,
) -> None:
    setup_version(client)
    good_task = create_task(client, "good", max_attempts=2)
    good_run = start_run(client, good_task)
    for index in range(2):
        add_event(client, good_task, good_run["id"], f"g{index}")

    bad_task = create_task(client, "bad", max_attempts=2)
    bad_run = start_run(client, bad_task)
    bad_records = [
        add_event(client, bad_task, bad_run["id"], f"b{index}") for index in range(3)
    ]
    finish_run(client, bad_task, bad_run["id"], status="failed", error="x")

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

    body = get_report(client)
    assert body["summary"]["invalid_audit_runs"] == 1
    assert body["summary"]["run_count"] == 2
    assert body["summary"]["task_count"] == 2
    # The failed task is not exhausted (2 attempts allowed, 1 used): its chain
    # is invalid but exhausted_tasks stays 0.
    assert body["summary"]["exhausted_tasks"] == 0

    by_id = {task["id"]: task for task in body["tasks"]}
    good_proof = by_id[good_task]["runs"][0]["proof"]
    bad_proof = by_id[bad_task]["runs"][0]["proof"]

    # Details are retained and only the bad run's proof flips to invalid.
    assert good_proof["valid"] is True
    assert good_proof["checked_count"] == 2
    assert good_proof["last_evidence_hash"] is not None
    assert bad_proof["valid"] is False
    assert bad_proof["checked_count"] == 3
    # The stored chain-tail hash is still surfaced even though the chain fails.
    assert bad_proof["last_evidence_hash"] == bad_records[-1]["evidence_hash"]
    assert by_id[bad_task]["runs"][0]["status"] == "failed"


def test_deleted_record_gap_is_detected(client: TestClient) -> None:
    setup_version(client)
    task_id = create_task(client, "t")
    run = start_run(client, task_id)
    records = [add_event(client, task_id, run["id"], f"e{index}") for index in range(3)]

    from app.db import database_path

    with sqlite3.connect(database_path()) as direct:
        direct.execute("DROP TRIGGER trg_processing_audit_records_no_update")
        direct.execute("DROP TRIGGER trg_processing_audit_records_no_delete")
        direct.execute(
            "DELETE FROM processing_task_audit_records "
            "WHERE run_id = ? AND sequence = 2",
            (run["id"],),
        )

    body = get_report(client)
    proof = body["tasks"][0]["runs"][0]["proof"]
    assert proof["valid"] is False
    assert proof["checked_count"] == 2
    # The surviving tail is the original sequence-3 record; its stored hash is
    # still reported even though the chain now has a gap and a broken link.
    assert proof["last_evidence_hash"] == records[-1]["evidence_hash"]
    assert body["summary"]["invalid_audit_runs"] == 1


# --------------------------------------------------------------------------- #
# Errors: 404 / 422 and stable JSON
# --------------------------------------------------------------------------- #


def test_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    setup_version(client)
    create_task(client, "t")
    for path in (
        "/datasets/ghost/versions/1/processing-tasks/audit-report",
        "/datasets/orders/versions/9/processing-tasks/audit-report",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "not_found"


def test_404_takes_precedence_over_query_or_body(client: TestClient) -> None:
    setup_version(client)
    response = client.get(
        "/datasets/ghost/versions/1/processing-tasks/audit-report?bogus=1"
    )
    assert response.status_code == 404
    response = client.request(
        "GET",
        "/datasets/ghost/versions/1/processing-tasks/audit-report",
        content=b'{"bogus": 1}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404
    response = client.get(
        "/datasets/orders/versions/9/processing-tasks/audit-report?bogus=1"
    )
    assert response.status_code == 404


def test_unexpected_query_parameter_is_422(client: TestClient) -> None:
    setup_version(client)
    create_task(client, "t")
    for suffix in ("?x=1", "?x=", "?x=1&y=2", "?="):
        response = client.get(REPORT_PATH + suffix)
        assert response.status_code == 422, suffix
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]


def test_non_empty_body_is_422(client: TestClient) -> None:
    setup_version(client)
    create_task(client, "t")
    for kwargs in (
        {"content": b'{"x": 1}', "headers": {"content-type": "application/json"}},
        {"content": b"plain text"},
        {"content": b"["},
    ):
        response = client.request("GET", REPORT_PATH, **kwargs)
        assert response.status_code == 422, kwargs
        assert response.json()["error"] == "validation_error"
        assert set(response.json()) == {"error", "detail"}


def test_malformed_path_version_is_422(client: TestClient) -> None:
    setup_version(client)
    response = client.get(
        "/datasets/orders/versions/not-an-int/processing-tasks/audit-report"
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_rejected_request_changes_nothing(client: TestClient) -> None:
    setup_version(client)
    task_id = create_task(client, "t")
    run = start_run(client, task_id)
    add_event(client, task_id, run["id"])
    before = get_report(client)

    client.get(REPORT_PATH + "?bogus=1")
    client.request(
        "GET",
        REPORT_PATH,
        content=b'{"bogus": 1}',
        headers={"content-type": "application/json"},
    )
    after = get_report(client)
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
base = "/datasets/orders/versions/1/processing-tasks"

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
path = "/datasets/orders/versions/1/processing-tasks/audit-report"
response = client.get(path)
assert response.status_code == 200, response.text
body = response.json()
assert body["dataset"] == "orders"
assert body["version"] == 1
assert body["summary"] == {
    "task_count": 3,
    "run_count": 2,
    "pending_tasks": 1,
    "running_tasks": 1,
    "succeeded_tasks": 0,
    "failed_tasks": 1,
    "exhausted_tasks": 1,
    "invalid_audit_runs": 0,
}
assert [task["id"] for task in body["tasks"]] == sorted(
    task["id"] for task in body["tasks"]
)
running = body["tasks"][1]
assert [run["attempt"] for run in running["runs"]] == [1]
proof = running["runs"][0]["proof"]
assert proof["valid"] is True
assert proof["checked_count"] == 2
assert isinstance(proof["last_evidence_hash"], str) and len(proof["last_evidence_hash"]) == 64
failed = body["tasks"][2]
assert failed["status"] == "failed"
assert failed["runs"][0]["proof"]["valid"] is True
assert failed["runs"][0]["proof"]["checked_count"] == 1

# A second read returns an identical result (deterministic, read-only).
again = client.get(path)
assert again.status_code == 200, again.text
assert again.json() == body
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


def test_audit_report_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-report.db"
    assert _run_script(db_path, CREATE_SCRIPT) == "created"
    assert db_path.exists()
    assert _run_script(db_path, VERIFY_SCRIPT) == "verified"
