"""Tests for the read-only processing audit report.

Covers the empty report, summary statistics, full detail with per-run proof
re-verification, ordering, tampered chains, request validation and stability
across repeated reads and process restarts.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def report_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/processing-tasks/audit-report"


def tasks_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/processing-tasks"


def create_dataset_and_version(client: TestClient, dataset: str = "orders") -> None:
    response = client.post("/datasets", json={"name": dataset})
    assert response.status_code == 201, response.text
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201, response.text


def make_task(
    client: TestClient,
    name: str,
    *,
    max_attempts: int = 1,
    base: str = tasks_path(),
) -> dict:
    response = client.post(
        base, json={"name": name, "max_attempts": max_attempts}
    )
    assert response.status_code == 201, response.text
    return response.json()


def start_run(client: TestClient, base: str, task_id: int) -> dict:
    response = client.post(f"{base}/{task_id}/runs")
    assert response.status_code == 201, response.text
    return response.json()


def finish_run(
    client: TestClient,
    base: str,
    task_id: int,
    run_id: int,
    status: str,
    *,
    error: str | None = None,
) -> None:
    body = {"status": status}
    if error is not None:
        body["error"] = error
    response = client.patch(f"{base}/{task_id}/runs/{run_id}", json=body)
    assert response.status_code == 200, response.text


def add_audit(
    client: TestClient,
    base: str,
    task_id: int,
    run_id: int,
    event: str,
) -> dict:
    response = client.post(
        f"{base}/{task_id}/runs/{run_id}/audit-records",
        json={"event": event, "input_summary": "input", "result_summary": "result"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def get_report(client: TestClient, *args, **kwargs) -> dict:
    response = client.get(report_path(*args, **kwargs))
    assert response.status_code == 200, response.text
    return response.json()


SUMMARY_KEYS = {
    "task_count",
    "run_count",
    "pending_tasks",
    "running_tasks",
    "succeeded_tasks",
    "failed_tasks",
    "exhausted_tasks",
    "invalid_audit_runs",
}

TASK_KEYS = {
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

RUN_KEYS = {
    "id",
    "task_id",
    "attempt",
    "status",
    "started_at",
    "finished_at",
    "error",
    "proof",
}

PROOF_KEYS = {"valid", "checked_count", "last_evidence_hash"}


def assert_summary_matches_detail(report: dict) -> None:
    """The summary totals must be exactly derivable from the report detail."""
    tasks = report["tasks"]
    summary = report["summary"]
    assert summary["task_count"] == len(tasks)
    assert summary["run_count"] == sum(len(task["runs"]) for task in tasks)
    assert summary["pending_tasks"] == sum(t["status"] == "pending" for t in tasks)
    assert summary["running_tasks"] == sum(t["status"] == "running" for t in tasks)
    assert summary["succeeded_tasks"] == sum(
        t["status"] == "succeeded" for t in tasks
    )
    failed = [t for t in tasks if t["status"] == "failed"]
    assert summary["failed_tasks"] == len(failed)
    assert summary["exhausted_tasks"] == sum(
        t["attempt_count"] >= t["max_attempts"] for t in failed
    )
    invalid = 0
    for task in tasks:
        for run in task["runs"]:
            if run["proof"] is not None and not run["proof"]["valid"]:
                invalid += 1
    assert summary["invalid_audit_runs"] == invalid


# --------------------------------------------------------------------------- #
# Empty report
# --------------------------------------------------------------------------- #


def test_empty_report_for_version_without_tasks(client: TestClient) -> None:
    create_dataset_and_version(client)
    report = get_report(client)
    assert set(report) == {"dataset", "version", "summary", "tasks"}
    assert report["dataset"] == "orders"
    assert report["version"] == 1
    assert report["tasks"] == []
    assert set(report["summary"]) == SUMMARY_KEYS
    assert report["summary"] == {key: 0 for key in SUMMARY_KEYS}
    assert_summary_matches_detail(report)


def test_report_includes_pending_task_without_runs(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_task(client, "extract")
    report = get_report(client)
    (task,) = report["tasks"]
    assert set(task) == TASK_KEYS
    assert task["name"] == "extract"
    assert task["status"] == "pending"
    assert task["attempt_count"] == 0
    assert task["runs"] == []
    assert report["summary"]["task_count"] == 1
    assert report["summary"]["pending_tasks"] == 1
    assert report["summary"]["run_count"] == 0


# --------------------------------------------------------------------------- #
# Summary statistics
# --------------------------------------------------------------------------- #


def test_summary_counts_every_task_status_and_exhaustion(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    base = tasks_path()

    # pending (no run)
    make_task(client, "pending")
    # running
    running = make_task(client, "running")
    start_run(client, base, running["id"])
    # succeeded on its first attempt
    succeeded = make_task(client, "succeeded")
    succeeded_run = start_run(client, base, succeeded["id"])
    finish_run(client, base, succeeded["id"], succeeded_run["id"], "succeeded")
    # failed but retryable: max_attempts 2, one failed attempt so far
    retryable = make_task(client, "retryable", max_attempts=2)
    retryable_run = start_run(client, base, retryable["id"])
    finish_run(
        client, base, retryable["id"], retryable_run["id"], "failed", error="boom"
    )
    # failed and exhausted: single failed attempt
    exhausted = make_task(client, "exhausted", max_attempts=1)
    exhausted_run = start_run(client, base, exhausted["id"])
    finish_run(
        client, base, exhausted["id"], exhausted_run["id"], "failed", error="dead"
    )
    # failed after using all attempts of a larger budget
    exhausted2 = make_task(client, "exhausted-twice", max_attempts=2)
    for _ in range(2):
        run = start_run(client, base, exhausted2["id"])
        finish_run(client, base, exhausted2["id"], run["id"], "failed", error="again")

    report = get_report(client)
    summary = report["summary"]
    assert summary == {
        "task_count": 6,
        "run_count": 6,
        "pending_tasks": 1,
        "running_tasks": 1,
        "succeeded_tasks": 1,
        "failed_tasks": 3,
        "exhausted_tasks": 2,
        "invalid_audit_runs": 0,
    }
    assert_summary_matches_detail(report)

    by_name = {task["name"]: task for task in report["tasks"]}
    assert by_name["retryable"]["status"] == "failed"
    assert by_name["retryable"]["attempt_count"] == 1
    assert by_name["exhausted"]["attempt_count"] == 1
    assert by_name["exhausted-twice"]["attempt_count"] == 2


# --------------------------------------------------------------------------- #
# Full detail, ordering and proof
# --------------------------------------------------------------------------- #


def test_full_detail_is_sorted_and_carries_reverified_proofs(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    base = tasks_path()

    first = make_task(client, "first", max_attempts=2)
    second = make_task(client, "second", max_attempts=1)

    # First task: a failed first attempt (two audit records) then a succeeded
    # retry (one record). Records are appended around the run finishing so the
    # snapshot run_statuses in the chain vary.
    run_one = start_run(client, base, first["id"])
    add_audit(client, base, first["id"], run_one["id"], "step-1")
    add_audit(client, base, first["id"], run_one["id"], "step-2")
    finish_run(client, base, first["id"], run_one["id"], "failed", error="boom")
    run_two = start_run(client, base, first["id"])
    retry_record = add_audit(client, base, first["id"], run_two["id"], "retry")
    finish_run(client, base, first["id"], run_two["id"], "succeeded")

    # Second task: a failed run without any audit records (empty chain).
    run_empty = start_run(client, base, second["id"])
    finish_run(client, base, second["id"], run_empty["id"], "failed", error="x")

    report = get_report(client)
    assert [task["id"] for task in report["tasks"]] == [first["id"], second["id"]]

    task_a, task_b = report["tasks"]
    assert set(task_a) == TASK_KEYS
    assert [run["attempt"] for run in task_a["runs"]] == [1, 2]
    assert task_a["status"] == "succeeded"
    assert task_a["attempt_count"] == 2

    run1, run2 = task_a["runs"]
    assert set(run1) == RUN_KEYS
    assert run1["id"] == run_one["id"]
    assert run1["task_id"] == first["id"]
    assert run1["status"] == "failed"
    assert run1["error"] == "boom"
    assert run1["finished_at"] is not None
    assert run2["id"] == run_two["id"]
    assert run2["status"] == "succeeded"
    assert run2["error"] is None

    # Non-empty chains carry a freshly computed proof; the empty chain is null.
    for checked_run, record_count, last_record in (
        (run1, 2, None),
        (run2, 1, retry_record),
    ):
        proof = checked_run["proof"]
        assert set(proof) == PROOF_KEYS
        assert proof["valid"] is True
        assert proof["checked_count"] == record_count
        assert isinstance(proof["last_evidence_hash"], str)
        assert len(proof["last_evidence_hash"]) == 64
        if last_record is not None:
            assert proof["last_evidence_hash"] == last_record["evidence_hash"]
    assert task_b["runs"][0]["proof"] is None

    # The first run's last hash is the tail of its own two-record chain.
    listed = client.get(
        f"{base}/{first['id']}/runs/{run_one['id']}/audit-records"
    ).json()
    assert run1["proof"]["last_evidence_hash"] == listed[-1]["evidence_hash"]
    assert run1["proof"]["checked_count"] == len(listed)
    assert_summary_matches_detail(report)


def test_report_is_read_only_and_stable_across_repeated_reads(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    base = tasks_path()
    task = make_task(client, "extract")
    run = start_run(client, base, task["id"])
    add_audit(client, base, task["id"], run["id"], "e")

    first = client.get(report_path())
    assert first.status_code == 200
    second = client.get(report_path())
    assert second.status_code == 200
    # Nothing about the report is generated at read time: two reads return
    # byte-identical JSON and the underlying run is still running.
    assert first.content == second.content
    detail = client.get(f"{base}/{task['id']}").json()
    assert detail["status"] == "running"


# --------------------------------------------------------------------------- #
# Tampered chains
# --------------------------------------------------------------------------- #


def _drop_audit_triggers(db_file: Path) -> None:
    with sqlite3.connect(db_file) as direct:
        direct.execute("DROP TRIGGER trg_processing_audit_records_no_update")
        direct.execute("DROP TRIGGER trg_processing_audit_records_no_delete")


def test_tampered_record_marks_proof_invalid_but_keeps_detail(
    client: TestClient,
) -> None:
    from app.db import database_path

    create_dataset_and_version(client)
    base = tasks_path()
    task = make_task(client, "extract")
    run = start_run(client, base, task["id"])
    records = [
        add_audit(client, base, task["id"], run["id"], f"step-{index}")
        for index in range(3)
    ]

    _drop_audit_triggers(database_path())
    with sqlite3.connect(database_path()) as direct:
        direct.execute(
            "UPDATE processing_task_audit_records SET event = ? WHERE sequence = 2",
            ("tampered",),
        )

    report = get_report(client)
    (detail_task,) = report["tasks"]
    (detail_run,) = detail_task["runs"]
    proof = detail_run["proof"]
    # All records are still listed by the dedicated endpoint; the report itself
    # remains complete (it carries run fields, not the record rows), and the
    # proof keeps the checked count and tail while flagging invalidity.
    assert proof == {
        "valid": False,
        "checked_count": 3,
        "last_evidence_hash": records[-1]["evidence_hash"],
    }
    assert report["summary"]["invalid_audit_runs"] == 1
    assert report["summary"]["run_count"] == 1
    assert detail_run["status"] == "running"
    assert_summary_matches_detail(report)

    # The dedicated verifier reaches the same verdict.
    verify = client.get(
        f"{base}/{task['id']}/runs/{run['id']}/audit-records/verify"
    ).json()
    assert verify["valid"] is False
    assert verify["checked_count"] == 3


def test_deleted_record_gap_is_detected_and_multiple_bad_runs_are_counted(
    client: TestClient,
) -> None:
    from app.db import database_path

    create_dataset_and_version(client)
    base = tasks_path()
    task = make_task(client, "extract")
    run = start_run(client, base, task["id"])
    for index in range(3):
        add_audit(client, base, task["id"], run["id"], f"step-{index}")

    other = make_task(client, "other")
    other_run = start_run(client, base, other["id"])
    add_audit(client, base, other["id"], other_run["id"], "only")

    assert get_report(client)["summary"]["invalid_audit_runs"] == 0

    _drop_audit_triggers(database_path())
    with sqlite3.connect(database_path()) as direct:
        # Delete the middle record of the first run: the gap breaks sequence
        # continuity and the remaining tail's previous_hash link.
        direct.execute(
            "DELETE FROM processing_task_audit_records "
            "WHERE run_id = ? AND sequence = 2",
            (run["id"],),
        )
        # Tamper the single record of the other run.
        direct.execute(
            "UPDATE processing_task_audit_records SET result_summary = 'x' "
            "WHERE run_id = ?",
            (other_run["id"],),
        )

    report = get_report(client)
    assert report["summary"]["invalid_audit_runs"] == 2
    proofs = {
        run_detail["id"]: run_detail["proof"]
        for task_detail in report["tasks"]
        for run_detail in task_detail["runs"]
    }
    assert proofs[run["id"]]["valid"] is False
    assert proofs[run["id"]]["checked_count"] == 2
    assert proofs[other_run["id"]]["valid"] is False
    assert proofs[other_run["id"]]["checked_count"] == 1
    assert_summary_matches_detail(report)


# --------------------------------------------------------------------------- #
# Unknown resources and malformed requests
# --------------------------------------------------------------------------- #


def test_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    for url in (
        report_path("ghost", 1),
        report_path("orders", 9),
    ):
        response = client.get(url)
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"
        assert set(response.json()) == {"error", "detail"}

    create_dataset_and_version(client)
    assert client.get(report_path("orders", 9)).status_code == 404


def test_non_integer_version_is_422(client: TestClient) -> None:
    response = client.get(
        "/datasets/orders/versions/not-an-int/processing-tasks/audit-report"
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_query_parameters_are_422_but_path_404_takes_precedence(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    for params in ({"x": "1"}, {"format": "json"}, {"limit": "5"}):
        response = client.get(report_path(), params=params)
        assert response.status_code == 422, params
        assert response.json() == {
            "error": "validation_error",
            "detail": "The audit report does not accept query parameters",
        }

    # Unknown dataset/version remains 404 even with junk parameters attached.
    response = client.get(report_path("ghost", 1), params={"x": "1"})
    assert response.status_code == 404


def test_request_body_is_optional_and_must_be_empty(client: TestClient) -> None:
    create_dataset_and_version(client)
    url = report_path()
    headers = {"content-type": "application/json"}

    # No body, an empty object and an explicit null are all accepted.
    assert client.get(url).status_code == 200
    assert client.request("GET", url, content=b"{}", headers=headers).status_code == 200
    assert client.request("GET", url, content=b"null", headers=headers).status_code == 200

    # Any member, or a non-object body, is malformed input.
    for raw in (
        b'{"unexpected": 1}',
        b'{"valid": false}',
        b"[1]",
        b'"something"',
        b"5",
        b"true",
    ):
        response = client.request("GET", url, content=raw, headers=headers)
        assert response.status_code == 422, raw
        assert response.json()["error"] == "validation_error"

    # Malformed JSON uses the same stable error shape.
    response = client.request(
        "GET", url, content=b"{not json", headers=headers
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_invalid_requests_write_nothing(client: TestClient) -> None:
    create_dataset_and_version(client)
    task = make_task(client, "extract")
    base = tasks_path()
    run = start_run(client, base, task["id"])
    add_audit(client, base, task["id"], run["id"], "step")

    before = client.get(report_path())
    assert before.status_code == 200

    headers = {"content-type": "application/json"}
    invalid_calls = [
        lambda: client.get(report_path(), params={"x": "1"}),
        lambda: client.request(
            "GET", report_path(), content=b'{"x": 1}', headers=headers
        ),
        lambda: client.request(
            "GET", report_path(), content=b"{bad", headers=headers
        ),
    ]
    for call in invalid_calls:
        response = call()
        assert response.status_code == 422
        # The error envelope is always the same two stable JSON fields.
        assert set(response.json()) == {"error", "detail"}

    after = client.get(report_path())
    assert after.content == before.content
    # The chain is untouched and still verifies.
    verify = client.get(
        f"{base}/{task['id']}/runs/{run['id']}/audit-records/verify"
    ).json()
    assert verify["valid"] is True
    assert verify["checked_count"] == 1


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


CREATE_REPORT_SCRIPT = """
import json
import sys
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
first = ok(client.post(base, json={"name": "first", "max_attempts": 2}))
second = ok(client.post(base, json={"name": "second"}))
pending = ok(client.post(base, json={"name": "third"}))

run_one = ok(client.post(f"{base}/{first['id']}/runs"))
audit_one = f"{base}/{first['id']}/runs/{run_one['id']}/audit-records"
for event in ("step-1", "step-2"):
    ok(client.post(audit_one, json={
        "event": event, "input_summary": "in", "result_summary": "out",
    }))
finish = client.patch(
    f"{base}/{first['id']}/runs/{run_one['id']}",
    json={"status": "failed", "error": "boom"},
)
assert finish.status_code == 200, finish.text
run_two = ok(client.post(f"{base}/{first['id']}/runs"))
ok(client.post(
    f"{base}/{first['id']}/runs/{run_two['id']}/audit-records",
    json={"event": "retry", "input_summary": "in", "result_summary": "out"},
))
finish = client.patch(
    f"{base}/{first['id']}/runs/{run_two['id']}",
    json={"status": "succeeded"},
)
assert finish.status_code == 200, finish.text

# The second task has one failed run without audit records (empty chain).
empty_run = ok(client.post(f"{base}/{second['id']}/runs"))
finish = client.patch(
    f"{base}/{second['id']}/runs/{empty_run['id']}",
    json={"status": "failed", "error": "dead"},
)
assert finish.status_code == 200, finish.text

report = client.get(f"{base}/audit-report")
assert report.status_code == 200, report.text
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(report.json(), handle)
print("created")
"""


READ_REPORT_SCRIPT = """
import json
import sys
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
base = "/datasets/orders/versions/1/processing-tasks"

with open(sys.argv[1], encoding="utf-8") as handle:
    expected = json.load(handle)

first = client.get(f"{base}/audit-report")
assert first.status_code == 200, first.text
report = first.json()

# Repeated reads in the same process are identical.
second = client.get(f"{base}/audit-report")
assert second.status_code == 200, second.text
assert second.json() == report

# A fresh process recomputes exactly the report persisted by the first one.
assert report == expected

tasks = report["tasks"]
assert [task["id"] for task in tasks] == sorted(task["id"] for task in tasks)
assert report["summary"] == {
    "task_count": 3,
    "run_count": 3,
    "pending_tasks": 1,
    "running_tasks": 0,
    "succeeded_tasks": 1,
    "failed_tasks": 1,
    "exhausted_tasks": 1,
    "invalid_audit_runs": 0,
}
first_task = tasks[0]
assert [run["attempt"] for run in first_task["runs"]] == [1, 2]
failed_proof = first_task["runs"][0]["proof"]
succeeded_proof = first_task["runs"][1]["proof"]
assert failed_proof["valid"] is True
assert failed_proof["checked_count"] == 2
assert succeeded_proof["valid"] is True
assert succeeded_proof["checked_count"] == 1
# The empty chain of the failed second task reports a null proof.
assert tasks[1]["runs"][0]["proof"] is None
assert tasks[2]["runs"] == []
print("verified")
"""


def _run_script(db_path: Path, script: str, *args: str) -> str:
    env = os.environ.copy()
    env["DATA_LINEAGE_DB"] = str(db_path)
    result = subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_audit_report_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-report.db"
    expected_path = tmp_path / "expected-report.json"
    assert _run_script(db_path, CREATE_REPORT_SCRIPT, str(expected_path)) == "created"
    assert db_path.exists()
    assert expected_path.exists()
    assert (
        _run_script(db_path, READ_REPORT_SCRIPT, str(expected_path)) == "verified"
    )
