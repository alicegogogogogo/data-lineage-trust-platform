"""Tests for the append-only audit proof chain of processing task runs."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def audit_path(dataset: str, version: int, task_id: int, run_id: int) -> str:
    return (
        f"/datasets/{dataset}/versions/{version}/processing-tasks"
        f"/{task_id}/runs/{run_id}/audit-records"
    )


def setup_task_with_run(
    client: TestClient, dataset: str = "orders"
) -> tuple[int, int, str]:
    """Create dataset v1, a task and a running run; return (task_id, run_id, path)."""
    response = client.post("/datasets", json={"name": dataset})
    assert response.status_code == 201, response.text
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201, response.text
    base = f"/datasets/{dataset}/versions/1/processing-tasks"
    response = client.post(base, json={"name": "ingest"})
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]
    response = client.post(f"{base}/{task_id}/runs")
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    return task_id, run_id, audit_path(dataset, 1, task_id, run_id)


def post_event(
    client: TestClient,
    path: str,
    *,
    event: str = "started",
    input_summary: str = "input",
    result_summary: str = "result",
):
    return client.post(
        path,
        json={
            "event": event,
            "input_summary": input_summary,
            "result_summary": result_summary,
        },
    )


def expected_evidence_hash(record: dict) -> str:
    payload = {field: record[field] for field in (
        "event",
        "input_summary",
        "previous_hash",
        "result_summary",
        "run_status",
        "sequence",
    )}
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


RECORD_FIELDS = {
    "id",
    "sequence",
    "event",
    "input_summary",
    "result_summary",
    "run_status",
    "previous_hash",
    "evidence_hash",
    "created_at",
}


# --------------------------------------------------------------------------- #
# Writing records
# --------------------------------------------------------------------------- #


def test_first_record_starts_chain_at_one(client: TestClient) -> None:
    _task_id, _run_id, path = setup_task_with_run(client)
    response = post_event(client, path)
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == RECORD_FIELDS
    assert body["sequence"] == 1
    assert body["previous_hash"] is None
    assert body["run_status"] == "running"
    assert body["event"] == "started"
    assert body["input_summary"] == "input"
    assert body["result_summary"] == "result"
    assert isinstance(body["id"], int)
    datetime.fromisoformat(body["created_at"])
    assert body["evidence_hash"] == expected_evidence_hash(body)
    # A known canonical-JSON vector locks the serialization down.
    first = {
        "event": "started",
        "input_summary": "in",
        "previous_hash": None,
        "result_summary": "ok",
        "run_status": "running",
        "sequence": 1,
    }
    assert expected_evidence_hash(first) == (
        "64b64a39aac12e937d36e6a62a0a51cc6359e415c366d786499f336d2fdb22f6"
    )


def test_sequence_is_continuous_and_hashes_chain(client: TestClient) -> None:
    _task_id, _run_id, path = setup_task_with_run(client)
    records = []
    for index in range(3):
        response = post_event(
            client,
            path,
            event=f"step-{index}",
            input_summary=f"in {index}",
            result_summary=f"out {index}",
        )
        assert response.status_code == 201, response.text
        record = response.json()
        assert record["sequence"] == index + 1
        records.append(record)

    assert records[0]["previous_hash"] is None
    for previous, current in zip(records, records[1:]):
        assert current["previous_hash"] == previous["evidence_hash"]
    for record in records:
        assert record["evidence_hash"] == expected_evidence_hash(record)


def test_run_status_is_snapshotted_at_write_time(client: TestClient) -> None:
    task_id, run_id, path = setup_task_with_run(client)
    running = post_event(client, path, event="tick")
    assert running.status_code == 201
    assert running.json()["run_status"] == "running"

    base = "/datasets/orders/versions/1/processing-tasks"
    finished = client.patch(
        f"{base}/{task_id}/runs/{run_id}", json={"status": "succeeded"}
    )
    assert finished.status_code == 200, finished.text

    after = post_event(client, path, event="post-mortem")
    assert after.status_code == 201, after.text
    after_body = after.json()
    assert after_body["sequence"] == 2
    assert after_body["run_status"] == "succeeded"
    assert after_body["previous_hash"] == running.json()["evidence_hash"]


def test_chains_are_independent_per_run(client: TestClient) -> None:
    _task_id, first_run, first_path = setup_task_with_run(client)
    base = "/datasets/orders/versions/1/processing-tasks"
    # Finish the first run (task allows a single attempt only fails allow retry,
    # so use a second task to get an independent run).
    second_task = client.post(base, json={"name": "load"}).json()["id"]
    second_run = client.post(f"{base}/{second_task}/runs").json()["id"]
    second_path = audit_path("orders", 1, second_task, second_run)

    assert first_run != second_run
    first = post_event(client, first_path).json()
    second = post_event(client, second_path).json()
    assert first["sequence"] == second["sequence"] == 1
    assert first["previous_hash"] is None and second["previous_hash"] is None
    post_event(client, first_path).json()
    second_next = post_event(client, second_path).json()
    # The second run's chain is unaffected by the extra record on the first run.
    assert second_next["sequence"] == 2
    assert second_next["previous_hash"] == second["evidence_hash"]


# --------------------------------------------------------------------------- #
# Listing and verification
# --------------------------------------------------------------------------- #


def test_list_returns_records_in_sequence_order(client: TestClient) -> None:
    _task_id, _run_id, path = setup_task_with_run(client)
    for index in range(4):
        assert post_event(client, path, event=f"e{index}").status_code == 201
    response = client.get(path)
    assert response.status_code == 200, response.text
    records = response.json()
    assert [record["sequence"] for record in records] == [1, 2, 3, 4]
    for record in records:
        assert set(record) == RECORD_FIELDS


def test_list_empty_chain(client: TestClient) -> None:
    _task_id, _run_id, path = setup_task_with_run(client)
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == []


def test_verify_valid_chain_and_path_identifiers(client: TestClient) -> None:
    task_id, run_id, path = setup_task_with_run(client)
    for index in range(3):
        assert post_event(client, path, event=f"e{index}").status_code == 201
    response = client.get(f"{path}/verify")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "dataset": "orders",
        "version": 1,
        "task_id": task_id,
        "run_id": run_id,
        "valid": True,
        "checked_count": 3,
    }


def test_verify_empty_chain_is_valid(client: TestClient) -> None:
    _task_id, _run_id, path = setup_task_with_run(client)
    response = client.get(f"{path}/verify")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is True
    assert body["checked_count"] == 0


def test_verify_detects_tampering_and_gaps(client: TestClient) -> None:
    _task_id, _run_id, path = setup_task_with_run(client)
    for index in range(3):
        assert post_event(client, path, event=f"e{index}").status_code == 201

    from app.db import database_path

    db_file = database_path()

    def chain_state() -> tuple[bool, int]:
        verified = client.get(f"{path}/verify")
        assert verified.status_code == 200
        body = verified.json()
        return body["valid"], body["checked_count"]

    assert chain_state() == (True, 3)

    # Simulate corruption that bypassed the application: the application-level
    # triggers make this impossible through a normal connection, so drop them
    # for the duration of the tampering. The verifier must still detect it.
    with sqlite3.connect(db_file) as direct:
        direct.execute("DROP TRIGGER trg_processing_audit_records_no_update")
        direct.execute("DROP TRIGGER trg_processing_audit_records_no_delete")
        direct.execute(
            "UPDATE processing_task_audit_records SET event = ? WHERE sequence = 2",
            ("tampered",),
        )
    assert chain_state() == (False, 3)

    # The /verify call above opened an application connection, which recreated
    # the idempotent triggers; drop them again before the next tampering.
    with sqlite3.connect(db_file) as direct:
        direct.execute("DROP TRIGGER trg_processing_audit_records_no_update")
        direct.execute("DROP TRIGGER trg_processing_audit_records_no_delete")
        direct.execute(
            "UPDATE processing_task_audit_records SET event = ? WHERE sequence = 2",
            ("e1",),
        )
        direct.execute("DELETE FROM processing_task_audit_records WHERE sequence = 2")
    assert chain_state() == (False, 2)


def test_records_are_immutable_in_the_database(client: TestClient) -> None:
    _task_id, _run_id, path = setup_task_with_run(client)
    record = post_event(client, path).json()

    from app.db import database_path

    with sqlite3.connect(database_path()) as direct:
        direct.execute("PRAGMA foreign_keys = ON")
        for statement in (
            "UPDATE processing_task_audit_records SET event = 'x' WHERE id = ?",
            "DELETE FROM processing_task_audit_records WHERE id = ?",
        ):
            try:
                direct.execute(statement, (record["id"],))
                direct.commit()
            except sqlite3.Error:
                direct.rollback()
            else:  # pragma: no cover - the trigger must always fire
                raise AssertionError("immutability trigger did not fire")

    # The record is intact and the chain still verifies.
    listed = client.get(path).json()
    assert [record["event"] for record in listed] == ["started"]
    assert client.get(f"{path}/verify").json()["valid"] is True


def test_no_mutating_http_routes(client: TestClient) -> None:
    _task_id, _run_id, path = setup_task_with_run(client)
    post_event(client, path)
    # The collection supports only GET (list) and POST (append); there is no
    # per-record route at all, so records cannot be changed or removed over
    # HTTP.
    for method in ("patch", "put", "delete"):
        request_kwargs = {"json": {"event": "x"}} if method != "delete" else {}
        response = getattr(client, method)(path, **request_kwargs)
        assert response.status_code == 405, (method, response.status_code)
        assert client.get(f"{path}/verify").json()["valid"] is True


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


INVALID_BODIES = [
    {"input_summary": "in", "result_summary": "out"},  # missing event
    {"event": "e", "result_summary": "out"},  # missing input_summary
    {"event": "e", "input_summary": "in"},  # missing result_summary
    {"event": "e", "input_summary": "in", "result_summary": "out", "extra": 1},
    {"event": 1, "input_summary": "in", "result_summary": "out"},
    {"event": None, "input_summary": "in", "result_summary": "out"},
    {"event": True, "input_summary": "in", "result_summary": "out"},
    {"event": ["e"], "input_summary": "in", "result_summary": "out"},
    {"event": {"a": 1}, "input_summary": "in", "result_summary": "out"},
    {"event": "e", "input_summary": 5, "result_summary": "out"},
    {"event": "e", "input_summary": "in", "result_summary": False},
]

EMPTY_BODIES = [
    {"event": "", "input_summary": "in", "result_summary": "out"},
    {"event": "   ", "input_summary": "in", "result_summary": "out"},
    {"event": "e", "input_summary": "\t\n", "result_summary": "out"},
    {"event": "e", "input_summary": "in", "result_summary": " "},
]


def test_invalid_bodies_are_422_and_write_nothing(client: TestClient) -> None:
    _task_id, _run_id, path = setup_task_with_run(client)
    for body in INVALID_BODIES + EMPTY_BODIES:
        response = client.post(path, json=body)
        assert response.status_code == 422, (body, response.text)
        assert response.json()["error"] == "validation_error"
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["detail"]
    assert client.get(path).json() == []
    assert client.get(f"{path}/verify").json()["valid"] is True


def test_malformed_json_is_422(client: TestClient) -> None:
    _task_id, _run_id, path = setup_task_with_run(client)
    response = client.post(
        path, content="{not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get(path).json() == []


def test_error_precedence_path_before_body(client: TestClient) -> None:
    # Matches the run-finish precedence: an unknown dataset/version/task is 404
    # even with a blank body; the blank-body check (422) runs after the path
    # task is known but before the run itself is looked up.
    response = client.post(
        "/datasets/ghost/versions/1/processing-tasks/1/runs/1/audit-records",
        json={"event": "   ", "input_summary": "i", "result_summary": "r"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    task_id, _run_id, path = setup_task_with_run(client)
    # Valid task, unknown run, blank body: body 422 precedes run-existence 404.
    response = client.post(
        f"/datasets/orders/versions/1/processing-tasks/{task_id}/runs/999/audit-records",
        json={"event": "   ", "input_summary": "i", "result_summary": "r"},
    )
    assert response.status_code == 422
    # Valid task, unknown run, valid body: now run-existence yields 404.
    response = client.post(
        f"/datasets/orders/versions/1/processing-tasks/{task_id}/runs/999/audit-records",
        json={"event": "e", "input_summary": "i", "result_summary": "r"},
    )
    assert response.status_code == 404
    # Valid run but blank body: 422 and nothing is written.
    response = client.post(
        path, json={"event": "  ", "input_summary": "i", "result_summary": "r"}
    )
    assert response.status_code == 422
    assert client.get(path).json() == []


def test_values_are_stored_verbatim_but_whitespace_only_rejected(
    client: TestClient,
) -> None:
    _task_id, _run_id, path = setup_task_with_run(client)
    response = post_event(
        client, path, event="  spaced event  ", input_summary=" in ", result_summary=" out "
    )
    assert response.status_code == 201, response.text
    record = response.json()
    # Only the emptiness check strips; the stored value keeps its padding.
    assert record["event"] == "  spaced event  "
    assert record["input_summary"] == " in "
    assert record["result_summary"] == " out "
    assert record["evidence_hash"] == expected_evidence_hash(record)


def test_unicode_is_utf8_canonicalized(client: TestClient) -> None:
    _task_id, _run_id, path = setup_task_with_run(client)
    response = post_event(
        client, path, event="事象", input_summary="入力", result_summary="結果"
    )
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["evidence_hash"] == expected_evidence_hash(record)


# --------------------------------------------------------------------------- #
# Path resolution: 404 vs 422
# --------------------------------------------------------------------------- #


def test_unknown_resources_are_404(client: TestClient) -> None:
    task_id, run_id, path = setup_task_with_run(client)
    post_event(client, path)
    other = path.replace("/orders/", "/ghost/")

    assert client.get(other).status_code == 404
    assert post_event(client, other).status_code == 404
    assert client.get(f"{other}/verify").status_code == 404

    # Existing dataset, missing version / task / run.
    assert client.get(
        "/datasets/orders/versions/9/processing-tasks/1/runs/1/audit-records"
    ).status_code == 404
    assert client.get(
        f"/datasets/orders/versions/1/processing-tasks/999/runs/{run_id}/audit-records"
    ).status_code == 404
    assert client.get(
        f"/datasets/orders/versions/1/processing-tasks/{task_id}/runs/999/audit-records"
    ).status_code == 404
    assert client.post(
        f"/datasets/orders/versions/1/processing-tasks/{task_id}/runs/999/audit-records",
        json={"event": "e", "input_summary": "i", "result_summary": "r"},
    ).status_code == 404


def test_run_of_other_task_in_same_version_is_422(client: TestClient) -> None:
    task_id, run_id, path = setup_task_with_run(client)
    post_event(client, path)
    base = "/datasets/orders/versions/1/processing-tasks"
    other_task = client.post(base, json={"name": "load"}).json()["id"]
    foreign = audit_path("orders", 1, other_task, run_id)

    response = post_event(client, foreign)
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get(foreign).status_code == 422
    assert client.get(f"{foreign}/verify").status_code == 422

    # Nothing was written onto the foreign path; the original chain is intact.
    assert client.get(f"{path}/verify").json()["valid"] is True


def test_run_of_task_in_other_version_is_422(client: TestClient) -> None:
    task_id, run_id, path = setup_task_with_run(client)
    post_event(client, path)
    # Create version 2 with a task so the run/task mismatch is a 422 (the
    # version and task themselves exist).
    created = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert created.status_code == 201, created.text
    v2_task = client.post(
        "/datasets/orders/versions/2/processing-tasks", json={"name": "ingest"}
    ).json()["id"]
    foreign = audit_path("orders", 2, v2_task, run_id)

    assert post_event(client, foreign).status_code == 422
    assert client.get(foreign).status_code == 422
    assert client.get(f"{foreign}/verify").status_code == 422


def test_error_json_never_leaks_internals(client: TestClient) -> None:
    _task_id, _run_id, path = setup_task_with_run(client)
    for response in (
        post_event(client, path.replace("/orders/", "/ghost/")),
        post_event(client, path, event="  ", input_summary="i", result_summary="r"),
        client.post(path, json={"event": "e"}),
    ):
        body = response.json()
        assert set(body) == {"error", "detail"}
        detail = body["detail"].lower()
        for leaked in ("traceback", "sql", "sqlite", "select ", "insert "):
            assert leaked not in detail


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_appends_never_duplicate_or_break_the_chain(
    client: TestClient,
) -> None:
    # Each worker thread uses its own TestClient (its own event-loop portal and
    # database connection), exercising the app's own append serialization.
    task_id, run_id, path = setup_task_with_run(client)

    count = 24
    results: list[dict] = []
    failures: list[Exception] = []
    barrier = threading.Barrier(count)

    def worker(index: int) -> None:
        local = TestClient(client.app)
        barrier.wait()
        try:
            response = local.post(
                path,
                json={
                    "event": f"event-{index}",
                    "input_summary": f"input-{index}",
                    "result_summary": f"result-{index}",
                },
            )
            assert response.status_code == 201, response.text
            results.append(response.json())
        except Exception as exc:  # pragma: no cover - reported below
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures, failures
    sequences = sorted(record["sequence"] for record in results)
    assert sequences == list(range(1, count + 1))
    assert len({record["id"] for record in results}) == count

    verified = client.get(f"{path}/verify").json()
    assert verified == {
        "dataset": "orders",
        "version": 1,
        "task_id": task_id,
        "run_id": run_id,
        "valid": True,
        "checked_count": count,
    }
    listed = client.get(path).json()
    assert [r["sequence"] for r in listed] == list(range(1, count + 1))
    previous = None
    for record in listed:
        assert record["previous_hash"] == previous
        assert record["evidence_hash"] == expected_evidence_hash(record)
        previous = record["evidence_hash"]


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
task = ok(client.post(base, json={"name": "ingest"}))
run = ok(client.post(f"{base}/{task['id']}/runs"))
audit = f"{base}/{task['id']}/runs/{run['id']}/audit-records"
for index in range(3):
    ok(client.post(audit, json={
        "event": f"event-{index}",
        "input_summary": f"input-{index}",
        "result_summary": f"result-{index}",
    }))
print(f"{task['id']},{run['id']}")
"""

VERIFY_SCRIPT = """
import hashlib
import json
import sys
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
task_id, run_id = (int(part) for part in sys.argv[1].split(","))
base = "/datasets/orders/versions/1/processing-tasks"
audit = f"{base}/{task_id}/runs/{run_id}/audit-records"

records = client.get(audit)
assert records.status_code == 200, records.text
rows = records.json()
assert [row["sequence"] for row in rows] == [1, 2, 3]
assert rows[0]["previous_hash"] is None
for earlier, later in zip(rows, rows[1:]):
    assert later["previous_hash"] == earlier["evidence_hash"]

verify = client.get(f"{audit}/verify")
assert verify.status_code == 200, verify.text
body = verify.json()
assert body["valid"] is True
assert body["checked_count"] == 3
assert body["run_id"] == run_id

# A record appended after the restart continues the same chain seamlessly.
appended = client.post(audit, json={
    "event": "after-restart",
    "input_summary": "in",
    "result_summary": "out",
})
assert appended.status_code == 201, appended.text
record = appended.json()
assert record["sequence"] == 4
assert record["previous_hash"] == rows[-1]["evidence_hash"]
assert client.get(f"{audit}/verify").json()["valid"] is True
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


def test_audit_chain_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-audit.db"
    task_and_run = _run_script(db_path, CREATE_SCRIPT)
    assert db_path.exists()
    assert _run_script(db_path, VERIFY_SCRIPT, task_and_run) == "verified"
