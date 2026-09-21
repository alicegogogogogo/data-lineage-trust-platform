"""Tests for the persistent, hash-chained run audit records."""

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

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

BASE = "/datasets/orders/versions/1/processing-tasks"


def setup_version(client: TestClient, dataset: str = "orders", version: int = 1) -> None:
    response = client.post("/datasets", json={"name": dataset})
    assert response.status_code == 201, response.text
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201, response.text


def make_task(client: TestClient, name: str = "ingest", **overrides) -> dict:
    response = client.post(BASE, json={"name": name, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def start_run(client: TestClient, task_id: int) -> dict:
    response = client.post(f"{BASE}/{task_id}/runs")
    assert response.status_code == 201, response.text
    return response.json()


def finish_run(client: TestClient, task_id: int, run_id: int, body: dict) -> dict:
    response = client.patch(
        f"{BASE}/{task_id}/runs/{run_id}", json=body
    )
    assert response.status_code == 200, response.text
    return response.json()


def audit_path(task_id: int, run_id: int) -> str:
    return f"{BASE}/{task_id}/runs/{run_id}/audit-records"


def post_record(client: TestClient, task_id: int, run_id: int, body: dict) -> dict:
    response = client.post(audit_path(task_id, run_id), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def expected_evidence_hash(record: dict) -> str:
    """Independently recompute the canonical SHA-256 evidence hash."""
    payload = {
        "event": record["event"],
        "input_summary": record["input_summary"],
        "previous_hash": record["previous_hash"],
        "result_summary": record["result_summary"],
        "run_status": record["run_status"],
        "sequence": record["sequence"],
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


RECORD_FIELDS = {
    "id",
    "sequence",
    "run_status",
    "event",
    "input_summary",
    "result_summary",
    "previous_hash",
    "evidence_hash",
    "created_at",
}


# --------------------------------------------------------------------------- #
# Appending records and chaining
# --------------------------------------------------------------------------- #


def test_first_record_starts_chain_at_sequence_one(client: TestClient) -> None:
    setup_version(client)
    task = make_task(client)
    run = start_run(client, task["id"])

    record = post_record(
        client, task["id"], run["id"],
        {"event": "started", "input_summary": "12 rows", "result_summary": "ok"},
    )
    assert set(record) == RECORD_FIELDS
    assert record["sequence"] == 1
    assert record["previous_hash"] is None
    assert record["run_status"] == "running"
    assert record["event"] == "started"
    assert record["input_summary"] == "12 rows"
    assert record["result_summary"] == "ok"
    assert len(record["evidence_hash"]) == 64
    int(record["evidence_hash"], 16)
    datetime.fromisoformat(record["created_at"])
    assert record["evidence_hash"] == expected_evidence_hash(record)


def test_records_chain_by_evidence_hash_and_increment_sequence(client: TestClient) -> None:
    setup_version(client)
    task = make_task(client)
    run = start_run(client, task["id"])

    first = post_record(
        client, task["id"], run["id"],
        {"event": "a", "input_summary": "in a", "result_summary": "out a"},
    )
    finish_run(client, task["id"], run["id"], {"status": "succeeded"})
    second = post_record(
        client, task["id"], run["id"],
        {"event": "b", "input_summary": "in b", "result_summary": "out b"},
    )

    assert second["sequence"] == 2
    assert second["previous_hash"] == first["evidence_hash"]
    # run_status is the run state at write time.
    assert second["run_status"] == "succeeded"
    assert second["evidence_hash"] == expected_evidence_hash(second)

    # The first record's stored content is unchanged.
    records = client.get(audit_path(task["id"], run["id"])).json()
    assert records[0]["evidence_hash"] == first["evidence_hash"]
    assert records[0]["run_status"] == "running"


def test_run_status_reflects_failed_state(client: TestClient) -> None:
    setup_version(client)
    task = make_task(client, max_attempts=2)
    run = start_run(client, task["id"])
    finish_run(client, task["id"], run["id"], {"status": "failed", "error": "boom"})
    record = post_record(
        client, task["id"], run["id"],
        {"event": "x", "input_summary": "y", "result_summary": "z"},
    )
    assert record["run_status"] == "failed"


def test_chains_are_independent_per_run(client: TestClient) -> None:
    setup_version(client)
    task = make_task(client, max_attempts=2)
    first_run = start_run(client, task["id"])
    finish_run(client, task["id"], first_run["id"],
               {"status": "failed", "error": "boom"})
    post_record(client, task["id"], first_run["id"],
                {"event": "e1", "input_summary": "i", "result_summary": "r"})

    second_run = start_run(client, task["id"])
    record = post_record(client, task["id"], second_run["id"],
                         {"event": "e2", "input_summary": "i", "result_summary": "r"})
    assert record["sequence"] == 1
    assert record["previous_hash"] is None


def test_evidence_hash_canonical_form_covers_unicode(client: TestClient) -> None:
    setup_version(client)
    task = make_task(client)
    run = start_run(client, task["id"])
    record = post_record(
        client, task["id"], run["id"],
        {"event": "事件", "input_summary": "  spaced  ", "result_summary": "λ→∞"},
    )
    # Whitespace inside the strings is significant; only the request-level
    # trimming rejects strings that are blank after strip.
    assert record["input_summary"] == "  spaced  "
    assert record["evidence_hash"] == expected_evidence_hash(record)


def test_list_records_sorted_by_sequence(client: TestClient) -> None:
    setup_version(client)
    task = make_task(client)
    run = start_run(client, task["id"])
    for index in range(4):
        post_record(client, task["id"], run["id"],
                    {"event": f"e{index}", "input_summary": "i",
                     "result_summary": "r"})
    response = client.get(audit_path(task["id"], run["id"]))
    assert response.status_code == 200
    records = response.json()
    assert [record["sequence"] for record in records] == [1, 2, 3, 4]
    assert all(set(record) == RECORD_FIELDS for record in records)
    for previous, current in zip(records, records[1:]):
        assert current["previous_hash"] == previous["evidence_hash"]


def test_list_empty_chain_returns_empty_list(client: TestClient) -> None:
    setup_version(client)
    task = make_task(client)
    run = start_run(client, task["id"])
    response = client.get(audit_path(task["id"], run["id"]))
    assert response.status_code == 200
    assert response.json() == []


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


def test_verify_valid_chain(client: TestClient) -> None:
    setup_version(client)
    task = make_task(client)
    run = start_run(client, task["id"])
    for index in range(3):
        post_record(client, task["id"], run["id"],
                    {"event": f"e{index}", "input_summary": "i",
                     "result_summary": "r"})

    response = client.get(f"{audit_path(task['id'], run['id'])}/verify")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "dataset": "orders",
        "version": 1,
        "task_id": task["id"],
        "run_id": run["id"],
        "valid": True,
        "checked_count": 3,
    }


def test_verify_empty_chain_is_valid(client: TestClient) -> None:
    setup_version(client)
    task = make_task(client)
    run = start_run(client, task["id"])
    response = client.get(f"{audit_path(task['id'], run['id'])}/verify")
    assert response.status_code == 200
    assert response.json()["valid"] is True
    assert response.json()["checked_count"] == 0


def test_verify_detects_tampered_content(client: TestClient) -> None:
    setup_version(client)
    task = make_task(client)
    run = start_run(client, task["id"])
    records = [
        post_record(client, task["id"], run["id"],
                    {"event": f"e{i}", "input_summary": "i",
                     "result_summary": "r"})
        for i in range(3)
    ]

    db = sqlite3.connect(os.environ["DATA_LINEAGE_DB"])
    try:
        db.execute(
            "UPDATE processing_run_audit_records SET event = ? WHERE id = ?",
            ("tampered", records[1]["id"]),
        )
        db.commit()
    finally:
        db.close()

    response = client.get(f"{audit_path(task["id"], run["id"])}/verify")
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["checked_count"] == 3


def test_verify_detects_broken_link(client: TestClient) -> None:
    setup_version(client)
    task = make_task(client)
    run = start_run(client, task["id"])
    records = [
        post_record(client, task["id"], run["id"],
                    {"event": f"e{i}", "input_summary": "i",
                     "result_summary": "r"})
        for i in range(2)
    ]

    db = sqlite3.connect(os.environ["DATA_LINEAGE_DB"])
    try:
        db.execute(
            "UPDATE processing_run_audit_records SET previous_hash = ? WHERE id = ?",
            ("0" * 64, records[1]["id"]),
        )
        db.commit()
    finally:
        db.close()

    body = client.get(f"{audit_path(task['id'], run['id'])}/verify").json()
    assert body["valid"] is False
    assert body["checked_count"] == 2


def test_verify_detects_sequence_gap(client: TestClient) -> None:
    setup_version(client)
    task = make_task(client)
    run = start_run(client, task["id"])
    for i in range(3):
        post_record(client, task["id"], run["id"],
                    {"event": f"e{i}", "input_summary": "i",
                     "result_summary": "r"})

    db = sqlite3.connect(os.environ["DATA_LINEAGE_DB"])
    try:
        # Renumbering the tail desyncs both the sequence sequence and every link.
        db.execute(
            "UPDATE processing_run_audit_records SET sequence = 5 "
            "WHERE run_id = ? AND sequence = 2",
            (run["id"],),
        )
        db.commit()
    finally:
        db.close()

    body = client.get(f"{audit_path(task['id'], run['id'])}/verify").json()
    assert body["valid"] is False


# --------------------------------------------------------------------------- #
# Body validation (422, nothing written)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        {"input_summary": "i", "result_summary": "r"},
        {"event": "e", "result_summary": "r"},
        {"event": "e", "input_summary": "i"},
        {"event": "e", "input_summary": "i", "result_summary": "r", "extra": 1},
        {"event": 1, "input_summary": "i", "result_summary": "r"},
        {"event": True, "input_summary": "i", "result_summary": "r"},
        {"event": None, "input_summary": "i", "result_summary": "r"},
        {"event": ["e"], "input_summary": "i", "result_summary": "r"},
    ],
)
def test_invalid_body_is_422_and_writes_nothing(
    client: TestClient, body: dict
) -> None:
    setup_version(client)
    task = make_task(client)
    run = start_run(client, task["id"])
    response = client.post(audit_path(task["id"], run["id"]), json=body)
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "validation_error"
    assert client.get(audit_path(task["id"], run["id"])).json() == []
    verify = client.get(f"{audit_path(task['id'], run['id'])}/verify").json()
    assert verify["valid"] is True
    assert verify["checked_count"] == 0


@pytest.mark.parametrize("field", ["event", "input_summary", "result_summary"])
def test_blank_string_is_422_and_writes_nothing(client: TestClient, field: str) -> None:
    setup_version(client)
    task = make_task(client)
    run = start_run(client, task["id"])
    for blank in ("", "   ", "\t\n"):
        body = {"event": "e", "input_summary": "i", "result_summary": "r"}
        body[field] = blank
        response = client.post(audit_path(task["id"], run["id"]), json=body)
        assert response.status_code == 422, (blank, response.text)
        assert response.json()["error"] == "validation_error"
    assert client.get(audit_path(task["id"], run["id"])).json() == []


def test_malformed_json_is_422(client: TestClient) -> None:
    setup_version(client)
    task = make_task(client)
    run = start_run(client, task["id"])
    response = client.post(
        audit_path(task["id"], run["id"]),
        content="{not valid json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get(audit_path(task["id"], run["id"])).json() == []


# --------------------------------------------------------------------------- #
# Resource scoping: 404 vs 422
# --------------------------------------------------------------------------- #


def test_unknown_dataset_version_task_or_run_is_404(client: TestClient) -> None:
    url = "/datasets/ghost/versions/1/processing-tasks/1/runs/1/audit-records"
    body = {"event": "e", "input_summary": "i", "result_summary": "r"}
    assert client.post(url, json=body).status_code == 404
    assert client.get(url).status_code == 404
    assert client.get(f"{url}/verify").status_code == 404

    setup_version(client)
    task = make_task(client)
    run = start_run(client, task["id"])

    # Existing dataset/version, unknown task.
    url = audit_path(999, run["id"])
    assert client.post(url, json=body).status_code == 404
    assert client.get(url).status_code == 404
    assert client.get(f"{url}/verify").status_code == 404

    # Existing task, unknown run.
    url = audit_path(task["id"], 999)
    assert client.post(url, json=body).status_code == 404
    assert client.get(url).status_code == 404
    assert client.get(f"{url}/verify").status_code == 404


def test_run_of_other_task_is_422(client: TestClient) -> None:
    setup_version(client)
    first = make_task(client, "extract")
    second = make_task(client, "load")
    run = start_run(client, first["id"])
    body = {"event": "e", "input_summary": "i", "result_summary": "r"}

    url = audit_path(second["id"], run["id"])
    response = client.post(url, json=body)
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get(url).status_code == 422
    assert client.get(f"{url}/verify").status_code == 422
    # Nothing was written to the real chain.
    assert client.get(audit_path(first["id"], run["id"])).json() == []


def test_run_of_other_version_is_422(client: TestClient) -> None:
    setup_version(client)
    task_v1 = make_task(client, "ingest")
    run = start_run(client, task_v1["id"])

    response = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201
    task_v2 = client.post(
        "/datasets/orders/versions/2/processing-tasks", json={"name": "ingest"}
    ).json()

    url = (
        f"/datasets/orders/versions/2/processing-tasks/{task_v2['id']}"
        f"/runs/{run['id']}/audit-records"
    )
    body = {"event": "e", "input_summary": "i", "result_summary": "r"}
    assert client.post(url, json=body).status_code == 422
    assert client.get(url).status_code == 422
    assert client.get(f"{url}/verify").status_code == 422
    assert client.get(audit_path(task_v1["id"], run["id"])).json() == []


def test_422_on_other_task_happens_even_with_invalid_body(
    client: TestClient,
) -> None:
    setup_version(client)
    first = make_task(client, "extract")
    second = make_task(client, "load")
    run = start_run(client, first["id"])
    # Scope mismatch takes precedence over body validation, mirroring the
    # finish-run semantics.
    response = client.post(audit_path(second["id"], run["id"]), json={})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


# --------------------------------------------------------------------------- #
# Immutability
# --------------------------------------------------------------------------- #


def test_audit_records_cannot_be_modified_or_deleted_via_api(
    client: TestClient,
) -> None:
    setup_version(client)
    task = make_task(client)
    run = start_run(client, task["id"])
    record = post_record(client, task["id"], run["id"],
                         {"event": "e", "input_summary": "i",
                          "result_summary": "r"})
    url = audit_path(task["id"], run["id"])

    for method, kwargs in (
        ("DELETE", {}),
        ("PUT", {"json": {"event": "x", "input_summary": "i",
                          "result_summary": "r"}}),
    ):
        response = getattr(client, method.lower())(url, **kwargs)
        assert response.status_code == 405, (method, response.status_code)

    # A per-record sub-resource is not exposed.
    response = client.delete(f"{url}/{record['id']}")
    assert response.status_code == 404

    listing = client.get(url).json()
    assert len(listing) == 1
    assert listing[0]["evidence_hash"] == record["evidence_hash"]


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_appends_keep_sequence_continuous_and_chain_intact(
    client: TestClient,
) -> None:
    setup_version(client)
    task = make_task(client)
    run = start_run(client, task["id"])
    total = 20

    failures: list[Exception] = []

    def append(index: int) -> None:
        # Each thread uses its own client (and therefore its own database
        # connection), the same situation as concurrent HTTP requests.
        thread_client = TestClient(client.app)
        try:
            response = thread_client.post(
                audit_path(task["id"], run["id"]),
                json={"event": f"e{index}", "input_summary": f"in{index}",
                      "result_summary": "ok"},
            )
            assert response.status_code == 201, response.text
        except Exception as exc:  # pragma: no cover - reported below
            failures.append(exc)

    threads = [threading.Thread(target=append, args=(i,)) for i in range(total)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    records = client.get(audit_path(task["id"], run["id"])).json()
    sequences = [record["sequence"] for record in records]
    assert sorted(sequences) == list(range(1, total + 1))
    assert len({record["evidence_hash"] for record in records}) == total
    for previous, current in zip(records, records[1:]):
        assert current["previous_hash"] == previous["evidence_hash"]
    verify = client.get(f"{audit_path(task['id'], run['id'])}/verify").json()
    assert verify["valid"] is True
    assert verify["checked_count"] == total


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
task = ok(client.post(
    "/datasets/orders/versions/1/processing-tasks", json={"name": "ingest"}
))
run = ok(client.post(
    f"/datasets/orders/versions/1/processing-tasks/{task['id']}/runs"
))
base = (
    f"/datasets/orders/versions/1/processing-tasks/{task['id']}"
    f"/runs/{run['id']}/audit-records"
)
first = ok(client.post(base, json={
    "event": "started", "input_summary": "4 rows", "result_summary": "running",
}))
second = ok(client.post(base, json={
    "event": "checkpoint", "input_summary": "2 rows", "result_summary": "running",
}))
ok(client.patch(
    f"/datasets/orders/versions/1/processing-tasks/{task['id']}/runs/{run['id']}",
    json={"status": "succeeded"},
), status=200)
third = ok(client.post(base, json={
    "event": "finished", "input_summary": "4 rows", "result_summary": "all good",
}))
assert first["sequence"] == 1 and first["previous_hash"] is None
assert second["sequence"] == 2 and second["previous_hash"] == first["evidence_hash"]
assert third["sequence"] == 3 and third["run_status"] == "succeeded"
print("created")
"""

VERIFY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

tasks = client.get("/datasets/orders/versions/1/processing-tasks")
assert tasks.status_code == 200, tasks.text
task_id = tasks.json()[0]["id"]
detail = client.get(
    f"/datasets/orders/versions/1/processing-tasks/{task_id}"
).json()
run_id = detail["runs"][0]["id"]
base = (
    f"/datasets/orders/versions/1/processing-tasks/{task_id}"
    f"/runs/{run_id}/audit-records"
)

records = client.get(base)
assert records.status_code == 200, records.text
rows = records.json()
assert [r["sequence"] for r in rows] == [1, 2, 3]
assert rows[0]["previous_hash"] is None
assert rows[1]["previous_hash"] == rows[0]["evidence_hash"]
assert rows[2]["previous_hash"] == rows[1]["evidence_hash"]
assert rows[2]["run_status"] == "succeeded"

verify = client.get(f"{base}/verify")
assert verify.status_code == 200, verify.text
body = verify.json()
assert body["valid"] is True
assert body["checked_count"] == 3
assert body["run_id"] == run_id

# A record appended after the restart chains onto the persisted tail.
fourth = client.post(base, json={
    "event": "post-restart", "input_summary": "x", "result_summary": "y",
})
assert fourth.status_code == 201, fourth.text
assert fourth.json()["sequence"] == 4
assert fourth.json()["previous_hash"] == rows[2]["evidence_hash"]
verify_after = client.get(f"{base}/verify").json()
assert verify_after["valid"] is True
assert verify_after["checked_count"] == 4
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


def test_audit_chain_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-audit.db"
    assert _run_script(db_path, CREATE_SCRIPT) == "created"
    assert db_path.exists()
    assert _run_script(db_path, VERIFY_SCRIPT) == "verified"
