"""Tests for per-run snapshot bindings and their tamper-evident evidence chain."""

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


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def bindings_path(dataset: str, version: int, task_id: int, run_id: int) -> str:
    return (
        f"/datasets/{dataset}/versions/{version}/processing-tasks"
        f"/{task_id}/runs/{run_id}/snapshot-bindings"
    )


def audit_path(dataset: str, version: int, task_id: int, run_id: int) -> str:
    return (
        f"/datasets/{dataset}/versions/{version}/processing-tasks"
        f"/{task_id}/runs/{run_id}/audit-records"
    )


def make_dataset_version(
    client: TestClient, dataset: str = "orders", version: int = 1
) -> None:
    if version == 1:
        response = client.post("/datasets", json={"name": dataset})
        assert response.status_code == 201, response.text
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201, response.text


def make_snapshot(
    client: TestClient, dataset: str = "orders", version: int = 1, value: int = 1
) -> int:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots",
        json={"rows": [{"id": value}]},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def setup_run(
    client: TestClient, dataset: str = "orders"
) -> tuple[int, int, int, str]:
    """Create dataset v1, a task and a running run; return (version, task, run, path)."""
    make_dataset_version(client, dataset, 1)
    base = f"/datasets/{dataset}/versions/1/processing-tasks"
    response = client.post(base, json={"name": "ingest"})
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]
    response = client.post(f"{base}/{task_id}/runs")
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    return 1, task_id, run_id, bindings_path(dataset, 1, task_id, run_id)


def bind(
    client: TestClient,
    path: str,
    *,
    role: str = "input",
    version: int = 1,
    snapshot_id: int = 1,
):
    return client.post(
        path,
        json={"role": role, "version": version, "snapshot_id": snapshot_id},
    )


def expected_evidence_hash(record: dict) -> str:
    payload = {
        field: record[field]
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


def delete_snapshot(
    client: TestClient, snapshot_id: int, dataset: str = "orders", version: int = 1
) -> None:
    """Confirmed-delete a snapshot through the retention request flow."""
    policy = client.post(
        f"/datasets/{dataset}/versions/{version}/retention-policies",
        json={"retention_days": 0},
    )
    assert policy.status_code == 201, policy.text
    requests = (
        f"/datasets/{dataset}/versions/{version}/snapshots/"
        f"{snapshot_id}/deletion-requests"
    )
    created = client.post(requests, json={"reason": "bound and removed"})
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    confirmed = client.post(f"{requests}/{request_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text


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

VERIFY_FIELDS = {
    "dataset",
    "version",
    "task_id",
    "run_id",
    "valid",
    "checked_count",
    "problems",
}

PROBLEM_CODES = {
    "sequence_gap",
    "previous_hash_mismatch",
    "hash_mismatch",
    "snapshot_deleted",
}


# --------------------------------------------------------------------------- #
# Writing bindings
# --------------------------------------------------------------------------- #


def test_first_binding_starts_chain_at_one(client: TestClient) -> None:
    _version, _task, _run, path = setup_run(client)
    snapshot_id = make_snapshot(client)
    response = bind(client, path, snapshot_id=snapshot_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == BINDING_FIELDS
    assert body["sequence"] == 1
    assert body["role"] == "input"
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["snapshot_id"] == snapshot_id
    assert body["run_status"] == "running"
    assert body["previous_hash"] is None
    assert isinstance(body["id"], int)
    datetime.fromisoformat(body["created_at"])
    assert body["evidence_hash"] == expected_evidence_hash(body)
    # A known canonical-JSON vector locks the serialization down.
    first = {
        "dataset": "orders",
        "previous_hash": None,
        "role": "input",
        "run_status": "running",
        "sequence": 1,
        "snapshot_id": 1,
        "version": 1,
    }
    assert expected_evidence_hash(first) == (
        "4e492777ab62e6f4640acd33aa251ebf3e9ed9214a1221e56f3d6ba2c58390fc"
    )


def test_sequences_are_continuous_and_hashes_chain(client: TestClient) -> None:
    _version, _task, _run, path = setup_run(client)
    records = []
    for index in range(4):
        snapshot_id = make_snapshot(client, value=index)
        role = "input" if index % 2 == 0 else "output"
        response = bind(client, path, role=role, snapshot_id=snapshot_id)
        assert response.status_code == 201, response.text
        record = response.json()
        assert record["sequence"] == index + 1
        records.append(record)

    assert records[0]["previous_hash"] is None
    for previous, current in zip(records, records[1:]):
        assert current["previous_hash"] == previous["evidence_hash"]
    for record in records:
        assert record["evidence_hash"] == expected_evidence_hash(record)


def test_same_snapshot_binds_once_per_role_but_both_roles_allowed(
    client: TestClient,
) -> None:
    _version, _task, _run, path = setup_run(client)
    snapshot_id = make_snapshot(client)

    first = bind(client, path, role="input", snapshot_id=snapshot_id)
    assert first.status_code == 201, first.text
    second = bind(client, path, role="output", snapshot_id=snapshot_id)
    assert second.status_code == 201, second.text
    assert second.json()["sequence"] == 2

    duplicate = bind(client, path, role="input", snapshot_id=snapshot_id)
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"] == "conflict"
    assert set(duplicate.json()) == {"error", "detail"}
    duplicate_output = bind(client, path, role="output", snapshot_id=snapshot_id)
    assert duplicate_output.status_code == 409

    # The two losing requests wrote nothing.
    listed = client.get(path).json()
    assert [record["sequence"] for record in listed] == [1, 2]


def test_binding_a_snapshot_of_another_version_of_the_same_dataset(
    client: TestClient,
) -> None:
    _version, _task, _run, path = setup_run(client)
    make_dataset_version(client, "orders", 2)
    snapshot_v2 = make_snapshot(client, version=2, value=99)

    response = bind(client, path, role="output", version=2, snapshot_id=snapshot_v2)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["dataset"] == "orders"
    assert body["version"] == 2
    assert body["snapshot_id"] == snapshot_v2
    assert body["sequence"] == 1
    assert client.get(f"{path}/verify").json()["valid"] is True


def test_run_status_is_snapshotted_at_write_time(client: TestClient) -> None:
    _version, task_id, run_id, path = setup_run(client)
    first_snapshot = make_snapshot(client)
    running = bind(client, path, snapshot_id=first_snapshot)
    assert running.status_code == 201
    assert running.json()["run_status"] == "running"

    finished = client.patch(
        f"/datasets/orders/versions/1/processing-tasks/{task_id}/runs/{run_id}",
        json={"status": "succeeded"},
    )
    assert finished.status_code == 200, finished.text

    second_snapshot = make_snapshot(client, value=2)
    after = bind(client, path, role="output", snapshot_id=second_snapshot)
    assert after.status_code == 201, after.text
    assert after.json()["run_status"] == "succeeded"
    assert after.json()["sequence"] == 2
    assert after.json()["previous_hash"] == running.json()["evidence_hash"]


def test_chains_are_independent_per_run(client: TestClient) -> None:
    _version, _task, first_run, first_path = setup_run(client)
    base = "/datasets/orders/versions/1/processing-tasks"
    second_task = client.post(base, json={"name": "load"}).json()["id"]
    second_run = client.post(f"{base}/{second_task}/runs").json()["id"]
    second_path = bindings_path("orders", 1, second_task, second_run)

    snapshot_one = make_snapshot(client, value=1)
    snapshot_two = make_snapshot(client, value=2)
    first = bind(client, first_path, snapshot_id=snapshot_one).json()
    second = bind(client, second_path, snapshot_id=snapshot_two).json()
    assert first["sequence"] == second["sequence"] == 1
    assert first["previous_hash"] is None and second["previous_hash"] is None

    snapshot_three = make_snapshot(client, value=3)
    bind(client, first_path, role="output", snapshot_id=snapshot_three)
    next_second = bind(
        client, second_path, role="output", snapshot_id=snapshot_one
    ).json()
    assert next_second["sequence"] == 2
    assert next_second["previous_hash"] == second["evidence_hash"]


# --------------------------------------------------------------------------- #
# Listing and verification
# --------------------------------------------------------------------------- #


def test_list_returns_bindings_in_sequence_order(client: TestClient) -> None:
    _version, _task, _run, path = setup_run(client)
    for index in range(3):
        snapshot_id = make_snapshot(client, value=index)
        assert bind(client, path, snapshot_id=snapshot_id).status_code == 201
    response = client.get(path)
    assert response.status_code == 200, response.text
    records = response.json()
    assert [record["sequence"] for record in records] == [1, 2, 3]
    for record in records:
        assert set(record) == BINDING_FIELDS


def test_list_empty_chain(client: TestClient) -> None:
    _version, _task, _run, path = setup_run(client)
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == []


def test_verify_valid_chain_and_path_identifiers(client: TestClient) -> None:
    _version, task_id, run_id, path = setup_run(client)
    for index in range(3):
        snapshot_id = make_snapshot(client, value=index)
        assert bind(client, path, snapshot_id=snapshot_id).status_code == 201
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
    assert set(response.json()) == VERIFY_FIELDS


def test_verify_empty_chain_is_valid_with_zero_checks(client: TestClient) -> None:
    _version, _task, _run, path = setup_run(client)
    response = client.get(f"{path}/verify")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is True
    assert body["checked_count"] == 0
    assert body["problems"] == []


# --------------------------------------------------------------------------- #
# Snapshot deletion after binding
# --------------------------------------------------------------------------- #


def test_deleting_a_snapshot_after_binding_keeps_record_and_flags_verify(
    client: TestClient,
) -> None:
    _version, _task, _run, path = setup_run(client)
    snapshot_id = make_snapshot(client)
    created = bind(client, path, snapshot_id=snapshot_id)
    assert created.status_code == 201

    delete_snapshot(client, snapshot_id)

    # The binding record is retained verbatim; only verification changes.
    listed = client.get(path)
    assert listed.status_code == 200
    records = listed.json()
    assert len(records) == 1
    assert records[0]["snapshot_id"] == snapshot_id
    assert records[0]["evidence_hash"] == created.json()["evidence_hash"]

    verified = client.get(f"{path}/verify")
    assert verified.status_code == 200, verified.text
    body = verified.json()
    assert body["valid"] is False
    assert body["checked_count"] == 1
    assert body["problems"] == [
        {
            "sequence": 1,
            "binding_id": records[0]["id"],
            "code": "snapshot_deleted",
        }
    ]
    assert set(body["problems"][0]) == {"sequence", "binding_id", "code"}


def test_binding_a_deleted_snapshot_is_404_and_writes_nothing(
    client: TestClient,
) -> None:
    _version, _task, _run, path = setup_run(client)
    snapshot_id = make_snapshot(client)
    delete_snapshot(client, snapshot_id)

    response = bind(client, path, snapshot_id=snapshot_id)
    assert response.status_code == 404, response.text
    assert response.json()["error"] == "not_found"
    assert client.get(path).json() == []
    assert client.get(f"{path}/verify").json()["valid"] is True


def test_new_binding_after_deletion_continues_sequence_without_gap(
    client: TestClient,
) -> None:
    _version, _task, _run, path = setup_run(client)
    first_snapshot = make_snapshot(client, value=1)
    assert bind(client, path, snapshot_id=first_snapshot).status_code == 201
    delete_snapshot(client, first_snapshot)

    second_snapshot = make_snapshot(client, value=2)
    response = bind(client, path, role="output", snapshot_id=second_snapshot)
    assert response.status_code == 201, response.text
    assert response.json()["sequence"] == 2
    assert response.json()["previous_hash"] == (
        client.get(path).json()[0]["evidence_hash"]
    )

    body = client.get(f"{path}/verify").json()
    assert body["valid"] is False
    assert [problem["code"] for problem in body["problems"]] == [
        "snapshot_deleted"
    ]


# --------------------------------------------------------------------------- #
# Tamper-evident verification
# --------------------------------------------------------------------------- #


def _drop_binding_triggers() -> None:
    from app.db import database_path

    with sqlite3.connect(database_path()) as direct:
        direct.execute("DROP TRIGGER trg_run_snapshot_bindings_no_update")
        direct.execute("DROP TRIGGER trg_run_snapshot_bindings_no_delete")


def test_verify_detects_hash_and_previous_hash_tampering(client: TestClient) -> None:
    _version, _task, _run, path = setup_run(client)
    for value in (1, 2):
        snapshot_id = make_snapshot(client, value=value)
        assert bind(client, path, snapshot_id=snapshot_id).status_code == 201

    _drop_binding_triggers()
    with sqlite3.connect(os.environ["DATA_LINEAGE_DB"]) as direct:
        direct.execute(
            "UPDATE processing_run_snapshot_bindings SET evidence_hash = ? "
            "WHERE sequence = 1",
            ("0" * 64,),
        )
        direct.commit()

    body = client.get(f"{path}/verify").json()
    assert body["valid"] is False
    assert body["checked_count"] == 2
    assert body["problems"] == [
        {"sequence": 1, "binding_id": _binding_id(client, path, 1),
         "code": "hash_mismatch"},
        {"sequence": 2, "binding_id": _binding_id(client, path, 2),
         "code": "previous_hash_mismatch"},
    ]


def _binding_id(client: TestClient, path: str, sequence: int) -> int:
    records = client.get(path).json()
    return next(
        record["id"] for record in records if record["sequence"] == sequence
    )


def test_verify_detects_sequence_gap(client: TestClient) -> None:
    _version, _task, _run, path = setup_run(client)
    for value in (1, 2, 3):
        snapshot_id = make_snapshot(client, value=value)
        assert bind(client, path, snapshot_id=snapshot_id).status_code == 201

    _drop_binding_triggers()
    with sqlite3.connect(os.environ["DATA_LINEAGE_DB"]) as direct:
        direct.execute(
            "DELETE FROM processing_run_snapshot_bindings WHERE sequence = 2"
        )
        direct.commit()

    body = client.get(f"{path}/verify").json()
    assert body["valid"] is False
    assert body["checked_count"] == 2
    third_id = _binding_id(client, path, 3)
    # The surviving third record is both gapped and unlinked; codes sort
    # lexicographically.
    assert body["problems"] == [
        {"sequence": 3, "binding_id": third_id, "code": "previous_hash_mismatch"},
        {"sequence": 3, "binding_id": third_id, "code": "sequence_gap"},
    ]


def test_problem_codes_use_only_the_fixed_vocabulary(client: TestClient) -> None:
    _version, _task, _run, path = setup_run(client)
    for value in (1, 2):
        snapshot_id = make_snapshot(client, value=value)
        assert bind(client, path, snapshot_id=snapshot_id).status_code == 201
    body = client.get(f"{path}/verify").json()
    # No problems on an intact chain; every code elsewhere is in the fixed set.
    assert body["problems"] == []
    assert PROBLEM_CODES == {
        "sequence_gap",
        "previous_hash_mismatch",
        "hash_mismatch",
        "snapshot_deleted",
    }


def test_bindings_are_immutable_in_the_database(client: TestClient) -> None:
    _version, _task, _run, path = setup_run(client)
    snapshot_id = make_snapshot(client)
    record = bind(client, path, snapshot_id=snapshot_id).json()

    with sqlite3.connect(os.environ["DATA_LINEAGE_DB"]) as direct:
        direct.execute("PRAGMA foreign_keys = ON")
        for statement in (
            "UPDATE processing_run_snapshot_bindings SET role = 'output' WHERE id = ?",
            "DELETE FROM processing_run_snapshot_bindings WHERE id = ?",
        ):
            try:
                direct.execute(statement, (record["id"],))
                direct.commit()
            except sqlite3.Error:
                direct.rollback()
            else:  # pragma: no cover - the trigger must always fire
                raise AssertionError("immutability trigger did not fire")

    listed = client.get(path).json()
    assert [item["role"] for item in listed] == ["input"]
    assert client.get(f"{path}/verify").json()["valid"] is True


# --------------------------------------------------------------------------- #
# Validation: body and query shape
# --------------------------------------------------------------------------- #


INVALID_BODIES = [
    {"version": 1, "snapshot_id": 1},  # missing role
    {"role": "input", "snapshot_id": 1},  # missing version
    {"role": "input", "version": 1},  # missing snapshot_id
    {"role": "input", "version": 1, "snapshot_id": 1, "extra": 1},
    {"role": "INPUT", "version": 1, "snapshot_id": 1},
    {"role": "in", "version": 1, "snapshot_id": 1},
    {"role": "", "version": 1, "snapshot_id": 1},
    {"role": 1, "version": 1, "snapshot_id": 1},
    {"role": None, "version": 1, "snapshot_id": 1},
    {"role": True, "version": 1, "snapshot_id": 1},
    {"role": ["input"], "version": 1, "snapshot_id": 1},
    {"role": {"a": 1}, "version": 1, "snapshot_id": 1},
    {"role": "input", "version": "1", "snapshot_id": 1},
    {"role": "input", "version": 1.0, "snapshot_id": 1},
    {"role": "input", "version": True, "snapshot_id": 1},
    {"role": "input", "version": None, "snapshot_id": 1},
    {"role": "input", "version": 1, "snapshot_id": "1"},
    {"role": "input", "version": 1, "snapshot_id": 1.0},
    {"role": "input", "version": 1, "snapshot_id": False},
    {"role": "input", "version": 1, "snapshot_id": None},
]


def test_invalid_bodies_are_422_and_write_nothing(client: TestClient) -> None:
    _version, _task, _run, path = setup_run(client)
    make_snapshot(client)
    for body_value in INVALID_BODIES:
        response = client.post(path, json=body_value)
        assert response.status_code == 422, (body_value, response.text)
        assert response.json()["error"] == "validation_error"
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["detail"]
    assert client.get(path).json() == []
    assert client.get(f"{path}/verify").json()["valid"] is True


def test_malformed_and_non_object_and_empty_bodies_are_422(
    client: TestClient,
) -> None:
    _version, _task, _run, path = setup_run(client)
    make_snapshot(client)
    for content in (b"", b"   ", b"{not json", b"[1,2,3]", b'"input"', b"12", b"null"):
        response = client.post(
            path,
            content=content,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422, (content, response.text)
        assert response.json()["error"] == "validation_error"
    assert client.get(path).json() == []


def test_query_parameters_on_post_are_422(client: TestClient) -> None:
    _version, _task, _run, path = setup_run(client)
    snapshot_id = make_snapshot(client)
    response = client.post(
        f"{path}?x=1",
        json={"role": "input", "version": 1, "snapshot_id": snapshot_id},
    )
    assert response.status_code == 422, response.text
    assert client.get(path).json() == []


def test_get_endpoints_reject_body_and_query(client: TestClient) -> None:
    _version, _task, _run, path = setup_run(client)
    snapshot_id = make_snapshot(client)
    assert bind(client, path, snapshot_id=snapshot_id).status_code == 201

    for target in (path, f"{path}/verify"):
        body_response = client.request(
            "GET",
            target,
            content="{}",
            headers={"content-type": "application/json"},
        )
        assert body_response.status_code == 422, (target, body_response.text)
        whitespace_body = client.request(
            "GET",
            target,
            content=" ",
            headers={"content-type": "application/json"},
        )
        assert whitespace_body.status_code == 422, (target, whitespace_body.text)
        query_response = client.get(f"{target}?x=1")
        assert query_response.status_code == 422, (target, query_response.text)


# --------------------------------------------------------------------------- #
# Path resolution: 404 vs 422
# --------------------------------------------------------------------------- #


def test_unknown_resources_are_404(client: TestClient) -> None:
    _version, task_id, run_id, path = setup_run(client)
    snapshot_id = make_snapshot(client)
    assert bind(client, path, snapshot_id=snapshot_id).status_code == 201

    ghost = path.replace("/orders/", "/ghost/")
    assert client.get(ghost).status_code == 404
    assert client.get(f"{ghost}/verify").status_code == 404
    assert bind(client, ghost, snapshot_id=snapshot_id).status_code == 404

    assert client.get(
        "/datasets/orders/versions/9/processing-tasks/1/runs/1/snapshot-bindings"
    ).status_code == 404
    assert client.get(
        f"/datasets/orders/versions/1/processing-tasks/999/runs/{run_id}"
        "/snapshot-bindings"
    ).status_code == 404
    assert client.get(
        f"/datasets/orders/versions/1/processing-tasks/{task_id}/runs/999"
        "/snapshot-bindings"
    ).status_code == 404
    assert bind(
        client,
        bindings_path("orders", 1, task_id, 999),
        snapshot_id=snapshot_id,
    ).status_code == 404


def test_unknown_body_version_and_snapshot_are_404(client: TestClient) -> None:
    _version, _task, _run, path = setup_run(client)
    snapshot_id = make_snapshot(client)

    missing_version = bind(
        client, path, role="input", version=9, snapshot_id=snapshot_id
    )
    assert missing_version.status_code == 404, missing_version.text
    assert missing_version.json()["error"] == "not_found"

    missing_snapshot = bind(
        client, path, role="input", version=1, snapshot_id=999
    )
    assert missing_snapshot.status_code == 404, missing_snapshot.text
    assert client.get(path).json() == []


def test_run_of_other_task_is_422_on_every_endpoint(client: TestClient) -> None:
    _version, task_id, run_id, path = setup_run(client)
    snapshot_id = make_snapshot(client)
    base = "/datasets/orders/versions/1/processing-tasks"
    other_task = client.post(base, json={"name": "load"}).json()["id"]
    foreign = bindings_path("orders", 1, other_task, run_id)

    response = bind(client, foreign, snapshot_id=snapshot_id)
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "validation_error"
    assert client.get(foreign).status_code == 422
    assert client.get(f"{foreign}/verify").status_code == 422

    # The foreign rejection wrote nothing; the original run is untouched.
    assert client.get(f"{path}/verify").json()["valid"] is True


def test_precedence_path_404_before_body_422(client: TestClient) -> None:
    # Unknown dataset with a malformed body and a query: 404 wins.
    response = client.post(
        "/datasets/ghost/versions/1/processing-tasks/1/runs/1/snapshot-bindings?x=1",
        content="{bad",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    _version, task_id, _run, path = setup_run(client)
    make_snapshot(client)
    # Valid task, unknown run, malformed body: body 422 precedes run 404.
    response = client.post(
        bindings_path("orders", 1, task_id, 999),
        content="{bad",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    # Valid task, unknown run, valid body: now run-existence yields 404.
    response = bind(
        client, bindings_path("orders", 1, task_id, 999), snapshot_id=1
    )
    assert response.status_code == 404
    # Valid path, valid run, but a query parameter: 422.
    response = client.post(
        f"{path}?x=1",
        json={"role": "input", "version": 1, "snapshot_id": 1},
    )
    assert response.status_code == 422


def test_unknown_sub_paths_are_404(client: TestClient) -> None:
    _version, _task, _run, path = setup_run(client)
    for target in (
        f"{path}/bogus",
        f"{path}/verify/extra",
    ):
        assert client.get(target).status_code == 404, target
        assert client.post(
            target,
            json={"role": "input", "version": 1, "snapshot_id": 1},
        ).status_code == 404, target


def test_failed_binding_changes_nothing_including_audit_chain(
    client: TestClient,
) -> None:
    _version, task_id, run_id, path = setup_run(client)
    snapshot_id = make_snapshot(client)

    # Append one run audit record so the separate audit chain has content.
    audit = audit_path("orders", 1, task_id, run_id)
    audit_response = client.post(
        audit,
        json={"event": "e", "input_summary": "i", "result_summary": "r"},
    )
    assert audit_response.status_code == 201

    # A rejected binding (duplicate) writes no binding and consumes no sequence.
    assert bind(client, path, snapshot_id=snapshot_id).status_code == 201
    assert bind(client, path, snapshot_id=snapshot_id).status_code == 409

    second_snapshot = make_snapshot(client, value=2)
    assert bind(client, path, role="output", snapshot_id=second_snapshot).status_code == 201
    assert [item["sequence"] for item in client.get(path).json()] == [1, 2]

    # The run's audit-record chain is exactly as it was and still verifies.
    audit_records = client.get(audit).json()
    assert len(audit_records) == 1
    assert client.get(f"{audit}/verify").json()["valid"] is True


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_bindings_never_reuse_or_skip_a_sequence(
    client: TestClient,
) -> None:
    _version, _task, _run, path = setup_run(client)
    count = 24
    snapshot_ids = [make_snapshot(client, value=index) for index in range(count)]
    results: list[dict] = []
    failures: list[Exception] = []
    barrier = threading.Barrier(count)

    def worker(index: int) -> None:
        local = TestClient(client.app)
        barrier.wait()
        try:
            response = bind(
                local,
                path,
                role="input",
                snapshot_id=snapshot_ids[index],
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
    assert sorted(record["sequence"] for record in results) == list(
        range(1, count + 1)
    )
    assert len({record["id"] for record in results}) == count

    verified = client.get(f"{path}/verify").json()
    assert verified["valid"] is True
    assert verified["checked_count"] == count
    assert verified["problems"] == []
    listed = client.get(path).json()
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
for index in range(2):
    snapshot = ok(client.post(
        "/datasets/orders/versions/1/snapshots", json={"rows": [{"id": index}]}
    ))
    ok(client.post(
        f"{base}/{task['id']}/runs/{run['id']}/snapshot-bindings",
        json={"role": "input", "version": 1, "snapshot_id": snapshot["id"]},
    ))
print(f"{task['id']},{run['id']}")
"""

VERIFY_SCRIPT = """
import sys
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
task_id, run_id = (int(part) for part in sys.argv[1].split(","))
base = f"/datasets/orders/versions/1/processing-tasks/{task_id}/runs/{run_id}"
bindings = f"{base}/snapshot-bindings"

records = client.get(bindings)
assert records.status_code == 200, records.text
rows = records.json()
assert [row["sequence"] for row in rows] == [1, 2]
assert rows[0]["previous_hash"] is None
assert rows[1]["previous_hash"] == rows[0]["evidence_hash"]

verify = client.get(f"{bindings}/verify")
assert verify.status_code == 200, verify.text
body = verify.json()
assert body["valid"] is True
assert body["checked_count"] == 2
assert body["problems"] == []
assert body["run_id"] == run_id

snapshot = client.post(
    "/datasets/orders/versions/1/snapshots", json={"rows": [{"id": 7}]}
).json()
appended = client.post(
    bindings, json={"role": "output", "version": 1, "snapshot_id": snapshot["id"]}
)
assert appended.status_code == 201, appended.text
record = appended.json()
assert record["sequence"] == 3
assert record["previous_hash"] == rows[-1]["evidence_hash"]
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


def test_binding_chain_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-bindings.db"
    task_and_run = _run_script(db_path, CREATE_SCRIPT)
    assert db_path.exists()
    assert _run_script(db_path, VERIFY_SCRIPT, task_and_run) == "verified"
