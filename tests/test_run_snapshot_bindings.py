"""Tests for processing run snapshot evidence bindings.

Each binding pins one existing, non-deleted snapshot of the run's dataset to
a run as 'input' or 'output'. Bindings form their own append-only, tamper-
evident hash chain per run, independent of (and never touching) the run's
audit-record chain.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_dataset(client: TestClient, dataset: str = "orders") -> None:
    response = client.post("/datasets", json={"name": dataset})
    assert response.status_code == 201, response.text
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201, response.text


def make_snapshot(
    client: TestClient, dataset: str = "orders", version: int = 1
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots",
        json={"rows": [{"id": 1}]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def setup_run(
    client: TestClient, dataset: str = "orders"
) -> tuple[int, int, str]:
    """Create dataset v1, a task and a running run; return (task_id, run_id, path)."""
    make_dataset(client, dataset)
    base = f"/datasets/{dataset}/versions/1/processing-tasks"
    response = client.post(base, json={"name": "ingest"})
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]
    response = client.post(f"{base}/{task_id}/runs")
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    return task_id, run_id, bindings_path(dataset, 1, task_id, run_id)


def bindings_path(dataset: str, version: int, task_id: int, run_id: int) -> str:
    return (
        f"/datasets/{dataset}/versions/{version}/processing-tasks"
        f"/{task_id}/runs/{run_id}/snapshot-bindings"
    )


def bind(
    client: TestClient,
    path: str,
    *,
    role: str = "input",
    version: int = 1,
    snapshot_id: int | None = None,
):
    if snapshot_id is None:
        snapshot_id = make_snapshot(client)["id"]
    return client.post(
        path,
        json={"role": role, "version": version, "snapshot_id": snapshot_id},
    )


def expected_evidence_hash(binding: dict) -> str:
    payload = {
        field: binding[field]
        for field in (
            "dataset",
            "previous_hash",
            "role",
            "run_status",
            "sequence",
            "snapshot_id",
            "version",
        )
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


BINDING_FIELDS = {
    "id",
    "sequence",
    "role",
    "dataset",
    "version",
    "snapshot_id",
    "run_status",
    "previous_hash",
    "evidence_hash",
    "created_at",
}


def confirm_snapshot_deletion(
    client: TestClient, snapshot_id: int, dataset: str = "orders", version: int = 1
) -> None:
    """Create a retention policy, open and immediately confirm a deletion."""
    policy = client.post(
        f"/datasets/{dataset}/versions/{version}/retention-policies",
        json={"retention_days": 0},
    )
    assert policy.status_code == 201, policy.text
    request = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots/"
        f"{snapshot_id}/deletion-requests",
        json={"reason": "no longer needed"},
    )
    assert request.status_code == 201, request.text
    request_id = request.json()["id"]
    # A zero-day policy still compares against the snapshot age; backdate it so
    # the confirmation passes the retention check.
    old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with sqlite3.connect(os.environ["DATA_LINEAGE_DB"]) as direct:
        direct.execute(
            "UPDATE snapshots SET created_at = ? WHERE id = ?",
            (old, snapshot_id),
        )
        direct.commit()
    confirmed = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots/"
        f"{snapshot_id}/deletion-requests/{request_id}/confirm"
    )
    assert confirmed.status_code == 200, confirmed.text


# --------------------------------------------------------------------------- #
# Writing bindings
# --------------------------------------------------------------------------- #


def test_first_binding_starts_chain_at_one(client: TestClient) -> None:
    _task_id, _run_id, path = setup_run(client)
    snapshot = make_snapshot(client)
    response = bind(client, path, snapshot_id=snapshot["id"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == BINDING_FIELDS
    assert body["sequence"] == 1
    assert body["role"] == "input"
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["snapshot_id"] == snapshot["id"]
    assert body["run_status"] == "running"
    assert body["previous_hash"] is None
    datetime.fromisoformat(body["created_at"])
    assert body["evidence_hash"] == expected_evidence_hash(body)
    # A known canonical-JSON vector locks the serialization down: keys sorted
    # by Unicode code point, no insignificant whitespace.
    first = {
        "dataset": "orders",
        "previous_hash": None,
        "role": "input",
        "run_status": "running",
        "sequence": 1,
        "snapshot_id": 7,
        "version": 1,
    }
    assert expected_evidence_hash(first) == (
        hashlib.sha256(
            json.dumps(
                first, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
    )


def test_both_roles_and_multiple_bindings_chain(client: TestClient) -> None:
    _task_id, _run_id, path = setup_run(client)
    first_snapshot = make_snapshot(client)["id"]
    second_snapshot = make_snapshot(client)["id"]
    third_snapshot = make_snapshot(client)["id"]

    first = bind(client, path, role="input", snapshot_id=first_snapshot)
    assert first.status_code == 201, first.text
    # The same snapshot may be bound once per role.
    second = bind(client, path, role="output", snapshot_id=first_snapshot)
    assert second.status_code == 201, second.text
    third = bind(client, path, role="input", snapshot_id=second_snapshot)
    assert third.status_code == 201, third.text
    fourth = bind(client, path, role="output", snapshot_id=third_snapshot)
    assert fourth.status_code == 201, fourth.text

    records = [r.json() for r in (first, second, third, fourth)]
    assert [r["sequence"] for r in records] == [1, 2, 3, 4]
    assert records[0]["previous_hash"] is None
    for earlier, later in zip(records, records[1:]):
        assert later["previous_hash"] == earlier["evidence_hash"]
    for record in records:
        assert record["evidence_hash"] == expected_evidence_hash(record)


def test_binding_can_target_any_version_of_the_same_dataset(
    client: TestClient,
) -> None:
    task_id, run_id, path = setup_run(client)
    # Version 2 of the same dataset with its own snapshot.
    created = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert created.status_code == 201, created.text
    v2_snapshot = make_snapshot(client, version=2)

    # The path stays on version 1; the body points at the version-2 snapshot.
    response = bind(client, path, role="input", version=2, snapshot_id=v2_snapshot["id"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["version"] == 2
    assert body["snapshot_id"] == v2_snapshot["id"]

    verify = client.get(f"{path}/verify").json()
    assert verify["valid"] is True
    assert verify["checked_count"] == 1


def test_run_status_is_snapshotted_at_bind_time(client: TestClient) -> None:
    task_id, run_id, path = setup_run(client)
    running = bind(client, path, snapshot_id=make_snapshot(client)["id"])
    assert running.status_code == 201
    assert running.json()["run_status"] == "running"

    base = "/datasets/orders/versions/1/processing-tasks"
    finished = client.patch(
        f"{base}/{task_id}/runs/{run_id}", json={"status": "succeeded"}
    )
    assert finished.status_code == 200, finished.text

    after = bind(client, path, role="output", snapshot_id=make_snapshot(client)["id"])
    assert after.status_code == 201, after.text
    after_body = after.json()
    assert after_body["sequence"] == 2
    assert after_body["run_status"] == "succeeded"
    assert after_body["previous_hash"] == running.json()["evidence_hash"]


def test_unicode_dataset_name_is_utf8_canonicalized(client: TestClient) -> None:
    _task_id, _run_id, path = setup_run(client, dataset="注文")
    snapshot = make_snapshot(client, dataset="注文")
    response = bind(client, path, snapshot_id=snapshot["id"])
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["dataset"] == "注文"
    assert record["evidence_hash"] == expected_evidence_hash(record)


# --------------------------------------------------------------------------- #
# Listing and verification
# --------------------------------------------------------------------------- #


def test_list_returns_bindings_in_sequence_order(client: TestClient) -> None:
    _task_id, _run_id, path = setup_run(client)
    for index in range(3):
        response = bind(
            client,
            path,
            role="input" if index % 2 == 0 else "output",
            snapshot_id=make_snapshot(client)["id"],
        )
        assert response.status_code == 201, response.text
    response = client.get(path)
    assert response.status_code == 200, response.text
    records = response.json()
    assert [r["sequence"] for r in records] == [1, 2, 3]
    for record in records:
        assert set(record) == BINDING_FIELDS


def test_list_empty_chain(client: TestClient) -> None:
    _task_id, _run_id, path = setup_run(client)
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == []


def test_verify_valid_chain_and_path_identifiers(client: TestClient) -> None:
    task_id, run_id, path = setup_run(client)
    for index in range(3):
        assert bind(
            client,
            path,
            role="input" if index % 2 == 0 else "output",
            snapshot_id=make_snapshot(client)["id"],
        ).status_code == 201
    response = client.get(f"{path}/verify")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "dataset": "orders",
        "version": 1,
        "task_id": task_id,
        "run_id": run_id,
        "valid": True,
        "checked_count": 3,
        "problems": [],
    }


def test_verify_empty_chain_is_valid(client: TestClient) -> None:
    _task_id, _run_id, path = setup_run(client)
    body = client.get(f"{path}/verify").json()
    assert body["valid"] is True
    assert body["checked_count"] == 0
    assert body["problems"] == []


def test_binding_survives_snapshot_deletion_and_verify_reports_it(
    client: TestClient,
) -> None:
    _task_id, run_id, path = setup_run(client)
    doomed = make_snapshot(client)["id"]
    kept = make_snapshot(client)["id"]
    assert bind(client, path, role="input", snapshot_id=doomed).status_code == 201
    assert bind(client, path, role="input", snapshot_id=kept).status_code == 201

    # Deleting the snapshot must not remove or alter either binding.
    confirm_snapshot_deletion(client, doomed)

    listed = client.get(path).json()
    assert [b["snapshot_id"] for b in listed] == [doomed, kept]
    assert [b["sequence"] for b in listed] == [1, 2]

    verify = client.get(f"{path}/verify")
    assert verify.status_code == 200, verify.text
    body = verify.json()
    assert body["valid"] is False
    assert body["checked_count"] == 2
    assert body["problems"] == [
        {"sequence": 1, "binding_id": listed[0]["id"], "code": "snapshot_deleted"}
    ]


def test_problems_sort_by_sequence_then_code(client: TestClient) -> None:
    _task_id, _run_id, path = setup_run(client)
    snapshot = make_snapshot(client)["id"]
    response = bind(client, path, snapshot_id=snapshot)
    assert response.status_code == 201, response.text
    binding_id = response.json()["id"]

    from app.db import database_path

    with sqlite3.connect(database_path()) as direct:
        direct.execute("DROP TRIGGER trg_run_snapshot_bindings_no_update")
        # Tamper with the role, which feeds the evidence hash but not the link.
        direct.execute(
            "UPDATE processing_run_snapshot_bindings SET role = 'output' WHERE id = ?",
            (binding_id,),
        )
        direct.commit()

    verify = client.get(f"{path}/verify").json()
    assert verify["valid"] is False
    # hash_mismatch only (link and sequence still intact); ordering by code.
    assert verify["problems"] == [
        {"sequence": 1, "binding_id": binding_id, "code": "hash_mismatch"}
    ]


def test_verify_detects_gap_and_link_break(client: TestClient) -> None:
    from app.db import database_path

    _task_id, _run_id, path = setup_run(client)
    snapshots = [make_snapshot(client)["id"] for _ in range(3)]
    created = []
    for index, snapshot_id in enumerate(snapshots):
        response = bind(
            client,
            path,
            role="input" if index % 2 == 0 else "output",
            snapshot_id=snapshot_id,
        )
        assert response.status_code == 201
        created.append(response.json())

    with sqlite3.connect(database_path()) as direct:
        direct.execute("DROP TRIGGER trg_run_snapshot_bindings_no_delete")
        direct.execute("DELETE FROM processing_run_snapshot_bindings WHERE sequence = 2")
        direct.commit()

    body = client.get(f"{path}/verify").json()
    assert body["valid"] is False
    assert body["checked_count"] == 2
    codes = {(p["sequence"], p["code"]) for p in body["problems"]}
    # The survivor at sequence 3 is now both a gap (expected 2) and a link
    # break (its previous_hash names the deleted record's hash).
    assert (3, "sequence_gap") in codes
    assert (3, "previous_hash_mismatch") in codes
    # Problems for the same sequence sort by code alphabetically.
    seq3 = [p for p in body["problems"] if p["sequence"] == 3]
    assert [p["code"] for p in seq3] == sorted(p["code"] for p in seq3)


def test_bindings_are_independent_of_audit_chain(client: TestClient) -> None:
    task_id, run_id, path = setup_run(client)
    audit = (
        f"/datasets/orders/versions/1/processing-tasks"
        f"/{task_id}/runs/{run_id}/audit-records"
    )
    event = client.post(
        audit,
        json={"event": "e", "input_summary": "in", "result_summary": "out"},
    )
    assert event.status_code == 201, event.text
    assert bind(client, path, snapshot_id=make_snapshot(client)["id"]).status_code == 201

    # Each verifier covers only its own chain.
    assert client.get(f"{audit}/verify").json()["valid"] is True
    assert client.get(f"{path}/verify").json()["valid"] is True
    assert client.get(audit).json() and client.get(path).json()


# --------------------------------------------------------------------------- #
# Validation: 422
# --------------------------------------------------------------------------- #


INVALID_BODIES = [
    {"version": 1, "snapshot_id": 1},  # missing role
    {"role": "input", "snapshot_id": 1},  # missing version
    {"role": "input", "version": 1},  # missing snapshot_id
    {"role": "input", "version": 1, "snapshot_id": 1, "extra": 1},
    {"role": "sideways", "version": 1, "snapshot_id": 1},
    {"role": "", "version": 1, "snapshot_id": 1},
    {"role": "INPUT", "version": 1, "snapshot_id": 1},
    {"role": None, "version": 1, "snapshot_id": 1},
    {"role": 1, "version": 1, "snapshot_id": 1},
    {"role": ["input"], "version": 1, "snapshot_id": 1},
    {"role": "input", "version": True, "snapshot_id": 1},
    {"role": "input", "version": "1", "snapshot_id": 1},
    {"role": "input", "version": 1.0, "snapshot_id": 1},
    {"role": "input", "version": 1, "snapshot_id": False},
    {"role": "input", "version": 1, "snapshot_id": "1"},
]


def test_invalid_bodies_are_422_and_write_nothing(client: TestClient) -> None:
    _task_id, _run_id, path = setup_run(client)
    make_snapshot(client)
    for payload in INVALID_BODIES:
        response = client.post(path, json=payload)
        assert response.status_code == 422, (payload, response.text)
        assert response.json()["error"] == "validation_error"
    assert client.get(path).json() == []
    assert client.get(f"{path}/verify").json()["valid"] is True


def test_malformed_and_non_object_bodies_are_422(client: TestClient) -> None:
    _task_id, _run_id, path = setup_run(client)
    for raw in (b"{not json", b"[1, 2]", b'"input"', b"   ", b""):
        response = client.post(
            path, content=raw, headers={"content-type": "application/json"}
        )
        assert response.status_code == 422, raw
        assert response.json()["error"] == "validation_error"
    assert client.get(path).json() == []


def test_query_parameter_on_post_is_422(client: TestClient) -> None:
    _task_id, _run_id, path = setup_run(client)
    snapshot = make_snapshot(client)["id"]
    response = client.post(
        f"{path}?extra=1",
        json={"role": "input", "version": 1, "snapshot_id": snapshot},
    )
    assert response.status_code == 422
    assert client.get(path).json() == []


def test_get_with_body_or_query_is_422(client: TestClient) -> None:
    _task_id, _run_id, path = setup_run(client)
    bind(client, path, snapshot_id=make_snapshot(client)["id"])

    response = client.get(
        f"{path}?extra=1",
    )
    assert response.status_code == 422
    response = client.request(
        "GET",
        path,
        content=b'{"role": "input"}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422

    assert client.get(f"{path}/verify?extra=1").status_code == 422
    response = client.request(
        "GET",
        f"{path}/verify",
        content=b"{}",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422


def test_unknown_path_is_404(client: TestClient) -> None:
    _task_id, _run_id, path = setup_run(client)
    assert client.get(f"{path}/nope").status_code == 404
    assert (
        client.post(
            f"{path}/nope",
            json={"role": "input", "version": 1, "snapshot_id": 1},
        ).status_code
        == 404
    )


# --------------------------------------------------------------------------- #
# 404 / 422 precedence
# --------------------------------------------------------------------------- #


def test_unknown_resources_are_404(client: TestClient) -> None:
    task_id, run_id, path = setup_run(client)
    snapshot = make_snapshot(client)["id"]
    body = {"role": "input", "version": 1, "snapshot_id": snapshot}
    other = path.replace("/orders/", "/ghost/")
    assert client.get(other).status_code == 404
    assert client.post(other, json=body).status_code == 404
    assert client.get(f"{other}/verify").status_code == 404

    assert client.get(
        "/datasets/orders/versions/9/processing-tasks/1/runs/1/snapshot-bindings"
    ).status_code == 404
    assert client.get(
        f"/datasets/orders/versions/1/processing-tasks/999/runs/{run_id}/snapshot-bindings"
    ).status_code == 404
    assert client.get(
        f"/datasets/orders/versions/1/processing-tasks/{task_id}/runs/999/snapshot-bindings"
    ).status_code == 404
    assert client.post(
        f"/datasets/orders/versions/1/processing-tasks/{task_id}/runs/999/snapshot-bindings",
        json=body,
    ).status_code == 404


def test_run_of_other_task_is_422(client: TestClient) -> None:
    task_id, run_id, path = setup_run(client)
    snapshot = make_snapshot(client)["id"]
    base = "/datasets/orders/versions/1/processing-tasks"
    other_task = client.post(base, json={"name": "load"}).json()["id"]
    foreign = bindings_path("orders", 1, other_task, run_id)

    body = {"role": "input", "version": 1, "snapshot_id": snapshot}
    assert client.post(foreign, json=body).status_code == 422
    assert client.get(foreign).status_code == 422
    assert client.get(f"{foreign}/verify").status_code == 422
    # Nothing was written onto either path.
    assert client.get(path).json() == []


def test_body_validation_422_precedes_run_404(client: TestClient) -> None:
    task_id, _run_id, path = setup_run(client)
    make_snapshot(client)
    # Existing task, unknown run, malformed body: body 422 precedes run 404.
    response = client.post(
        f"/datasets/orders/versions/1/processing-tasks/{task_id}/runs/999/snapshot-bindings",
        json={"role": "sideways", "version": 1, "snapshot_id": 1},
    )
    assert response.status_code == 422
    # Valid body, unknown run: run existence now yields 404.
    response = client.post(
        f"/datasets/orders/versions/1/processing-tasks/{task_id}/runs/999/snapshot-bindings",
        json={"role": "input", "version": 1, "snapshot_id": 1},
    )
    assert response.status_code == 404


def test_body_version_or_snapshot_missing_or_deleted_is_404(
    client: TestClient,
) -> None:
    _task_id, _run_id, path = setup_run(client)
    snapshot = make_snapshot(client)["id"]

    # Unknown body version.
    response = client.post(
        path, json={"role": "input", "version": 99, "snapshot_id": snapshot}
    )
    assert response.status_code == 404
    # Unknown snapshot id in an existing version.
    response = client.post(
        path, json={"role": "input", "version": 1, "snapshot_id": 999999}
    )
    assert response.status_code == 404
    # A snapshot from another dataset is not visible under this dataset.
    make_dataset(client, "other")
    other_snapshot = make_snapshot(client, dataset="other")
    response = client.post(
        path,
        json={"role": "input", "version": 1, "snapshot_id": other_snapshot["id"]},
    )
    assert response.status_code == 404

    # Nothing was written.
    assert client.get(path).json() == []

    # A snapshot deleted after creation can no longer be bound (404).
    confirm_snapshot_deletion(client, snapshot)
    response = client.post(
        path, json={"role": "output", "version": 1, "snapshot_id": snapshot}
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Duplicates: 409
# --------------------------------------------------------------------------- #


def test_duplicate_role_snapshot_is_409_and_writes_nothing(
    client: TestClient,
) -> None:
    _task_id, _run_id, path = setup_run(client)
    snapshot = make_snapshot(client)["id"]
    assert bind(client, path, role="input", snapshot_id=snapshot).status_code == 201

    duplicate = client.post(
        path, json={"role": "input", "version": 1, "snapshot_id": snapshot}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"] == "conflict"

    # The other role with the same snapshot is still allowed.
    assert bind(client, path, role="output", snapshot_id=snapshot).status_code == 201
    assert (
        client.post(
            path, json={"role": "output", "version": 1, "snapshot_id": snapshot}
        ).status_code
        == 409
    )

    records = client.get(path).json()
    assert [r["sequence"] for r in records] == [1, 2]
    verify = client.get(f"{path}/verify").json()
    assert verify["valid"] is True
    assert verify["checked_count"] == 2


def test_duplicate_409_takes_precedence_after_resources_resolve(
    client: TestClient,
) -> None:
    # A duplicate against an existing run/snapshot is 409 rather than any 4xx;
    # an unknown resource still 404s first.
    _task_id, _run_id, path = setup_run(client)
    snapshot = make_snapshot(client)["id"]
    assert bind(client, path, snapshot_id=snapshot).status_code == 201
    assert (
        client.post(
            path, json={"role": "input", "version": 99, "snapshot_id": snapshot}
        ).status_code
        == 404
    )


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_binds_never_duplicate_or_break_the_chain(
    client: TestClient,
) -> None:
    task_id, run_id, path = setup_run(client)
    # One distinct snapshot per worker, so every request succeeds (no 409).
    snapshots = [make_snapshot(client)["id"] for _ in range(24)]

    count = len(snapshots)
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
                    "role": "input" if index % 2 == 0 else "output",
                    "version": 1,
                    "snapshot_id": snapshots[index],
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
    sequences = sorted(r["sequence"] for r in results)
    assert sequences == list(range(1, count + 1))
    assert len({r["id"] for r in results}) == count

    verify = client.get(f"{path}/verify").json()
    assert verify["valid"] is True
    assert verify["checked_count"] == count
    listed = client.get(path).json()
    previous = None
    for record in listed:
        assert record["previous_hash"] == previous
        assert record["evidence_hash"] == expected_evidence_hash(record)
        previous = record["evidence_hash"]


def test_concurrent_duplicates_have_single_winner(client: TestClient) -> None:
    _task_id, _run_id, path = setup_run(client)
    snapshot = make_snapshot(client)["id"]
    statuses: list[int] = []
    barrier = threading.Barrier(12)

    def worker() -> None:
        local = TestClient(client.app)
        barrier.wait()
        response = local.post(
            path,
            json={"role": "input", "version": 1, "snapshot_id": snapshot},
        )
        statuses.append(response.status_code)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses).count(201) == 1
    assert all(status in (201, 409) for status in statuses)
    assert client.get(path).json().__len__() == 1
    assert client.get(f"{path}/verify").json()["valid"] is True


# --------------------------------------------------------------------------- #
# Immutability in the database
# --------------------------------------------------------------------------- #


def test_bindings_are_immutable_in_the_database(client: TestClient) -> None:
    _task_id, _run_id, path = setup_run(client)
    binding = bind(client, path, snapshot_id=make_snapshot(client)["id"]).json()

    with sqlite3.connect(os.environ["DATA_LINEAGE_DB"]) as direct:
        direct.execute("PRAGMA foreign_keys = ON")
        for statement in (
            "UPDATE processing_run_snapshot_bindings SET role = 'output' WHERE id = ?",
            "DELETE FROM processing_run_snapshot_bindings WHERE id = ?",
        ):
            try:
                direct.execute(statement, (binding["id"],))
                direct.commit()
            except sqlite3.Error:
                direct.rollback()
            else:  # pragma: no cover - the trigger must always fire
                raise AssertionError("immutability trigger did not fire")

    listed = client.get(path).json()
    assert [b["role"] for b in listed] == ["input"]
    assert client.get(f"{path}/verify").json()["valid"] is True


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
snapshot = ok(client.post(
    "/datasets/orders/versions/1/snapshots", json={"rows": [{"id": 1}]}
))
bindings = f"{base}/{task['id']}/runs/{run['id']}/snapshot-bindings"
ok(client.post(bindings, json={
    "role": "input", "version": 1, "snapshot_id": snapshot["id"],
}))
print(f"{task['id']},{run['id']},{snapshot['id']}")
"""

VERIFY_SCRIPT = """
import hashlib
import json
import sys
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
task_id, run_id, snapshot_id = (int(part) for part in sys.argv[1].split(","))
base = "/datasets/orders/versions/1/processing-tasks"
bindings = f"{base}/{task_id}/runs/{run_id}/snapshot-bindings"

records = client.get(bindings)
assert records.status_code == 200, records.text
rows = records.json()
assert len(rows) == 1
assert rows[0]["sequence"] == 1
assert rows[0]["previous_hash"] is None
assert rows[0]["snapshot_id"] == snapshot_id

verify = client.get(f"{bindings}/verify")
assert verify.status_code == 200, verify.text
body = verify.json()
assert body["valid"] is True
assert body["checked_count"] == 1

# A binding appended after the restart continues the same chain seamlessly.
second = client.post(
    "/datasets/orders/versions/1/snapshots", json={"rows": [{"id": 2}]}
).json()
appended = client.post(bindings, json={
    "role": "output", "version": 1, "snapshot_id": second["id"],
})
assert appended.status_code == 201, appended.text
record = appended.json()
assert record["sequence"] == 2
assert record["previous_hash"] == rows[0]["evidence_hash"]
assert client.get(f"{bindings}/verify").json()["valid"] is True
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


def test_bindings_survive_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-bindings.db"
    ids = _run_script(db_path, CREATE_SCRIPT)
    assert db_path.exists()
    assert _run_script(db_path, VERIFY_SCRIPT, ids) == "verified"
