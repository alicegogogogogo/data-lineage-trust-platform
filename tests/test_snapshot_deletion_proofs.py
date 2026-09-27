"""Tests for the tamper-evident snapshot deletion-proof chain.

One proof is appended in the same transaction as each successful snapshot
deletion. The chain is independent per schema version, numbered from 1,
linked through SHA-256 evidence hashes and append-only; the list and verify
segments are read-only and return deterministic JSON documents.
"""

from __future__ import annotations

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


def make_dataset(client: TestClient, name: str, fields: list[str]) -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions",
        json={
            "fields": [
                {"name": field, "type": "string", "nullable": True}
                for field in fields
            ]
        },
    )
    assert response.status_code == 201, response.text


def make_snapshot(
    client: TestClient,
    dataset: str = "raw",
    version: int = 1,
    rows: list | None = None,
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots",
        json={"rows": rows if rows is not None else [{"id": 1}]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_policy(
    client: TestClient,
    dataset: str = "raw",
    version: int = 1,
    retention_days: int = 0,
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/retention-policies",
        json={"retention_days": retention_days},
    )
    assert response.status_code == 201, response.text
    return response.json()


def requests_path(dataset: str, version: int, snapshot_id: int) -> str:
    return (
        f"/datasets/{dataset}/versions/{version}/snapshots/"
        f"{snapshot_id}/deletion-requests"
    )


def proofs_path(dataset: str, version: int) -> str:
    return f"/datasets/{dataset}/versions/{version}/snapshots/deletion-proofs"


def create_request(
    client: TestClient,
    snapshot_id: int,
    reason: str = "no longer needed",
    dataset: str = "raw",
    version: int = 1,
) -> dict:
    response = client.post(
        requests_path(dataset, version, snapshot_id), json={"reason": reason}
    )
    assert response.status_code == 201, response.text
    return response.json()


def confirm(
    client: TestClient,
    snapshot_id: int,
    request_id: int,
    dataset: str = "raw",
    version: int = 1,
) -> TestClient:
    return client.post(
        f"{requests_path(dataset, version, snapshot_id)}/{request_id}/confirm"
    )


def delete_snapshot(
    client: TestClient,
    snapshot_id: int,
    reason: str = "no longer needed",
    dataset: str = "raw",
    version: int = 1,
) -> dict:
    """Open and confirm a deletion request, returning the confirmation."""
    request = create_request(client, snapshot_id, reason, dataset, version)
    response = confirm(client, snapshot_id, request["id"], dataset, version)
    assert response.status_code == 200, response.text
    return response.json()


def list_proofs(client: TestClient, dataset: str = "raw", version: int = 1):
    return client.get(proofs_path(dataset, version))


def verify_proofs(client: TestClient, dataset: str = "raw", version: int = 1):
    return client.get(f"{proofs_path(dataset, version)}/verify")


def backdate_snapshot(snapshot_id: int, days: int = 1) -> None:
    db_path = os.environ["DATA_LINEAGE_DB"]
    old = (datetime.now(timezone.utc) - timedelta(days=days, seconds=1)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE snapshots SET created_at = ? WHERE id = ?",
            (old, snapshot_id),
        )


# --------------------------------------------------------------------------- #
# Empty chain and deterministic documents
# --------------------------------------------------------------------------- #


def test_empty_chain_lists_nothing(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)

    response = list_proofs(client)
    assert response.status_code == 200
    assert response.content == b"[]\n"


def test_empty_chain_verifies_valid_with_zero_count(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)

    response = verify_proofs(client)
    assert response.status_code == 200
    assert response.json() == {
        "dataset": "raw",
        "version": 1,
        "valid": True,
        "checked_count": 0,
    }
    # Deterministic document: compact, fixed key order, lowercase boolean,
    # exactly one trailing newline.
    assert response.content == (
        b'{"dataset":"raw","version":1,"valid":true,"checked_count":0}\n'
    )


# --------------------------------------------------------------------------- #
# A successful deletion appends exactly one proof
# --------------------------------------------------------------------------- #


def test_proof_is_appended_on_confirmation(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client, rows=[{"id": 1}, {"id": 2}])
    stored = client.get(
        f"/datasets/raw/versions/1/snapshots/{snapshot['id']}/verify"
    ).json()
    confirmation = delete_snapshot(client, snapshot["id"], reason="legal request")

    listed = list_proofs(client).json()
    assert len(listed) == 1
    proof = listed[0]
    assert list(proof) == [
        "sequence",
        "snapshot_id",
        "row_count",
        "stored_hash",
        "reason",
        "confirmed_at",
        "previous_hash",
        "evidence_hash",
    ]
    assert proof["sequence"] == 1
    assert proof["snapshot_id"] == snapshot["id"]
    # Row count and content fingerprint are the snapshot's on-disk values at
    # the deletion instant.
    assert proof["row_count"] == 2
    assert proof["stored_hash"] == stored["stored_hash"]
    assert proof["reason"] == "legal request"
    assert proof["confirmed_at"] == confirmation["confirmed_at"]
    assert proof["previous_hash"] is None
    assert isinstance(proof["evidence_hash"], str) and len(proof["evidence_hash"]) == 64

    verify = verify_proofs(client).json()
    assert verify == {
        "dataset": "raw",
        "version": 1,
        "valid": True,
        "checked_count": 1,
    }


def test_evidence_hash_covers_the_specified_fields(client: TestClient) -> None:
    import hashlib

    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    delete_snapshot(client, snapshot["id"], reason="hash me")

    proof = list_proofs(client).json()[0]
    # Same canonical-JSON digest convention as the processing audit chain:
    # keys sorted by code point, compact, unescaped non-ASCII, SHA-256 hex;
    # confirmed_at and the hashes themselves are excluded.
    payload = {
        "previous_hash": proof["previous_hash"],
        "reason": proof["reason"],
        "row_count": proof["row_count"],
        "sequence": proof["sequence"],
        "snapshot_id": proof["snapshot_id"],
        "stored_hash": proof["stored_hash"],
    }
    expected = hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    assert proof["evidence_hash"] == expected


def test_confirmed_at_is_excluded_from_the_hash(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    delete_snapshot(client, snapshot["id"])

    proof = list_proofs(client).json()[0]
    assert proof["confirmed_at"] not in proof["evidence_hash"]


def test_two_deletions_form_a_linked_continuous_chain(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    first = make_snapshot(client, rows=[{"id": 1}])
    second = make_snapshot(client, rows=[{"id": 2}])
    delete_snapshot(client, first["id"], reason="one")
    delete_snapshot(client, second["id"], reason="two")

    proofs = list_proofs(client).json()
    assert [proof["sequence"] for proof in proofs] == [1, 2]
    assert proofs[0]["previous_hash"] is None
    assert proofs[1]["previous_hash"] == proofs[0]["evidence_hash"]
    assert proofs[1]["snapshot_id"] == second["id"]
    assert verify_proofs(client).json()["valid"] is True


def test_chain_is_independent_per_version(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    assert (
        client.post(
            "/datasets/raw/versions",
            json={"fields": [{"name": "id", "type": "string", "nullable": True}]},
        ).status_code
        == 201
    )
    make_policy(client, version=2)

    s1 = make_snapshot(client, version=1)
    s2a = make_snapshot(client, version=2)
    s2b = make_snapshot(client, version=2)
    delete_snapshot(client, s1["id"], version=1)
    delete_snapshot(client, s2a["id"], version=2)
    delete_snapshot(client, s2b["id"], version=2)

    v1 = list_proofs(client, version=1).json()
    v2 = list_proofs(client, version=2).json()
    assert [proof["sequence"] for proof in v1] == [1]
    assert [proof["sequence"] for proof in v2] == [1, 2]
    assert v2[0]["previous_hash"] is None
    assert v2[1]["previous_hash"] == v2[0]["evidence_hash"]
    assert verify_proofs(client, version=1).json()["checked_count"] == 1
    assert verify_proofs(client, version=2).json()["checked_count"] == 2


# --------------------------------------------------------------------------- #
# Verification is computed fresh and detects tampering
# --------------------------------------------------------------------------- #


def test_verify_detects_a_rewritten_field(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    delete_snapshot(client, snapshot["id"], reason="original")
    assert verify_proofs(client).json()["valid"] is True

    db_file = os.environ["DATA_LINEAGE_DB"]
    with sqlite3.connect(db_file) as direct:
        direct.execute("DROP TRIGGER trg_snapshot_deletion_proofs_no_update")
        direct.execute(
            "UPDATE snapshot_deletion_proofs SET reason = ? WHERE sequence = 1",
            ("tampered",),
        )

    verification = verify_proofs(client).json()
    assert verification["valid"] is False
    assert verification["checked_count"] == 1


def test_verify_detects_a_broken_link(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshots = [make_snapshot(client, rows=[{"id": i}]) for i in range(2)]
    for snapshot in snapshots:
        delete_snapshot(client, snapshot["id"])

    db_file = os.environ["DATA_LINEAGE_DB"]
    with sqlite3.connect(db_file) as direct:
        direct.execute("DROP TRIGGER trg_snapshot_deletion_proofs_no_update")
        direct.execute(
            "UPDATE snapshot_deletion_proofs SET previous_hash = ? "
            "WHERE sequence = 2",
            ("0" * 64,),
        )

    verification = verify_proofs(client).json()
    assert verification["valid"] is False
    assert verification["checked_count"] == 2


def test_verify_detects_a_sequence_gap_after_removal(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshots = [make_snapshot(client, rows=[{"id": i}]) for i in range(3)]
    for snapshot in snapshots:
        delete_snapshot(client, snapshot["id"])

    db_file = os.environ["DATA_LINEAGE_DB"]
    with sqlite3.connect(db_file) as direct:
        direct.execute("DROP TRIGGER trg_snapshot_deletion_proofs_no_delete")
        direct.execute("DELETE FROM snapshot_deletion_proofs WHERE sequence = 2")

    verification = verify_proofs(client).json()
    assert verification["valid"] is False
    assert verification["checked_count"] == 2


def test_verify_is_a_normal_response_not_an_error_when_invalid(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    delete_snapshot(client, snapshot["id"])

    db_file = os.environ["DATA_LINEAGE_DB"]
    with sqlite3.connect(db_file) as direct:
        direct.execute("DROP TRIGGER trg_snapshot_deletion_proofs_no_update")
        direct.execute(
            "UPDATE snapshot_deletion_proofs SET row_count = 99 WHERE sequence = 1"
        )

    response = verify_proofs(client)
    assert response.status_code == 200
    assert set(response.json()) == {"dataset", "version", "valid", "checked_count"}


def test_proofs_are_immutable_in_the_database(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    delete_snapshot(client, snapshot["id"])

    db_file = os.environ["DATA_LINEAGE_DB"]
    with sqlite3.connect(db_file) as direct:
        direct.execute("PRAGMA foreign_keys = ON")
        row_id = direct.execute(
            "SELECT id FROM snapshot_deletion_proofs"
        ).fetchone()[0]
        for statement in (
            "UPDATE snapshot_deletion_proofs SET reason = 'x' WHERE id = ?",
            "DELETE FROM snapshot_deletion_proofs WHERE id = ?",
        ):
            try:
                direct.execute(statement, (row_id,))
                direct.commit()
            except sqlite3.Error:
                direct.rollback()
            else:  # pragma: no cover - the trigger must always fire
                raise AssertionError("immutability trigger did not fire")

    assert verify_proofs(client).json()["valid"] is True
    assert list_proofs(client).json()[0]["reason"] == "no longer needed"


# --------------------------------------------------------------------------- #
# No proof is written on any unsuccessful deletion path
# --------------------------------------------------------------------------- #


def test_too_young_confirmation_writes_no_proof(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=7)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])

    assert confirm(client, snapshot["id"], request["id"]).status_code == 409
    assert list_proofs(client).json() == []
    assert verify_proofs(client).json()["checked_count"] == 0


def test_blocked_confirmation_writes_no_proof(client: TestClient) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    response = client.post(
        "/datasets/dm/versions/1/lineage",
        json={
            "target_dataset": "dm",
            "target_version": 1,
            "target_field": "id",
            "source_dataset": "raw",
            "source_version": 1,
            "source_field": "order_id",
        },
    )
    assert response.status_code == 201, response.text
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    assert request["status"] == "blocked"

    assert confirm(client, snapshot["id"], request["id"]).status_code == 409
    assert list_proofs(client).json() == []


def test_rejected_request_writes_no_proof(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    path = requests_path("raw", 1, snapshot["id"])
    for payload in ({}, {"reason": ""}, {"reason": "   "}, {"reason": 7}):
        assert client.post(path, json=payload).status_code == 422
    assert list_proofs(client).json() == []


def test_repeated_confirmation_writes_no_second_proof(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    url = f"{requests_path('raw', 1, snapshot['id'])}/{request['id']}/confirm"
    assert client.post(url).status_code == 200
    assert client.post(url).status_code == 409

    proofs = list_proofs(client).json()
    assert len(proofs) == 1
    assert proofs[0]["sequence"] == 1


# --------------------------------------------------------------------------- #
# Previously deleted snapshots are not backfilled
# --------------------------------------------------------------------------- #


def test_previous_deletion_without_proof_is_not_backfilled(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    old = make_snapshot(client, rows=[{"id": 1}])
    later = make_snapshot(client, rows=[{"id": 2}])

    # Simulate a deletion performed before this feature existed: the snapshot
    # row is removed directly, with a confirmed request but no proof.
    db_file = os.environ["DATA_LINEAGE_DB"]
    with sqlite3.connect(db_file) as direct:
        policy_id = direct.execute(
            "SELECT id FROM retention_policies"
        ).fetchone()[0]
        direct.execute(
            "INSERT INTO snapshot_deletion_requests ("
            "version_id, snapshot_id, policy_id, reason, status, impacted, "
            "created_at, confirmed_at"
            ") VALUES (1, ?, ?, 'old', 'confirmed', '[]', ?, ?)",
            (old["id"], policy_id, datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat()),
        )
        direct.execute("DELETE FROM snapshots WHERE id = ?", (old["id"],))

    # A new confirmed deletion starts the chain at sequence 1 and never
    # backfills a proof for the earlier deletion.
    delete_snapshot(client, later["id"], reason="new")
    proofs = list_proofs(client).json()
    assert [proof["sequence"] for proof in proofs] == [1]
    assert proofs[0]["snapshot_id"] == later["id"]
    assert verify_proofs(client).json()["valid"] is True

    # Neither deleted snapshot is listed; snapshot numbers are not reused.
    assert client.get("/datasets/raw/versions/1/snapshots").json() == []
    another = make_snapshot(client, rows=[{"id": 3}])
    assert another["id"] not in {old["id"], later["id"]}


# --------------------------------------------------------------------------- #
# Existing snapshot/deletion-request behavior is unchanged
# --------------------------------------------------------------------------- #


def test_deleted_snapshot_stays_404_and_requests_stay_readable(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    confirmation = delete_snapshot(client, snapshot["id"])

    base = "/datasets/raw/versions/1/snapshots"
    assert client.get(f"{base}/{snapshot['id']}").status_code == 404
    assert (
        client.get(f"{base}/{snapshot['id']}/verify").status_code == 404
    )
    # The deletion-request collection stays addressable and lists the
    # confirmed request.
    listed = client.get(requests_path("raw", 1, snapshot["id"]))
    assert listed.status_code == 200
    assert listed.json()[0]["status"] == "confirmed"
    assert listed.json()[0]["id"] == confirmation["id"]


# --------------------------------------------------------------------------- #
# Resolution and request-shape rules
# --------------------------------------------------------------------------- #


def test_unknown_dataset_or_version_is_404_for_both_segments(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)

    # 404 takes precedence even when a shape error is also present.
    assert (
        client.get(
            "/datasets/ghost/versions/1/snapshots/deletion-proofs",
            params={"q": "1"},
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/datasets/ghost/versions/1/snapshots/deletion-proofs/verify",
            params={"q": "1"},
        ).status_code
        == 404
    )
    assert client.get(
        "/datasets/raw/versions/9/snapshots/deletion-proofs"
    ).status_code == 404
    assert client.get(
        "/datasets/raw/versions/9/snapshots/deletion-proofs/verify"
    ).status_code == 404


def test_query_parameters_and_body_bytes_are_422(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)

    for path in (proofs_path("raw", 1), f"{proofs_path('raw', 1)}/verify"):
        assert client.get(path, params={"q": "1"}).status_code == 422
        whitespace = client.request("GET", path, content=b"   ")
        assert whitespace.status_code == 422
        assert set(whitespace.json()) == {"error", "detail"}
        assert whitespace.json()["error"] == "validation_error"


def test_error_documents_never_expose_internals(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    response = client.get(
        proofs_path("raw", 1), params={"q": "1"}
    )
    body = response.text.lower()
    assert "traceback" not in body
    assert "sqlite" not in body
    assert "select" not in body


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_confirmations_of_different_snapshots_chain_continuously(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshots = [make_snapshot(client, rows=[{"id": i}]) for i in range(2)]
    request_ids = [create_request(client, s["id"])["id"] for s in snapshots]

    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def worker(index: int) -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        statuses.append(
            confirm(
                thread_client,
                snapshots[index]["id"],
                request_ids[index],
            ).status_code
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Both deletions succeed (never a 409 between different snapshots).
    assert sorted(statuses) == [200, 200], statuses

    proofs = list_proofs(client).json()
    # Sequences are continuous, never repeated and never skipped.
    assert sorted(proof["sequence"] for proof in proofs) == [1, 2]
    assert sorted(proof["snapshot_id"] for proof in proofs) == sorted(
        s["id"] for s in snapshots
    )
    first = next(proof for proof in proofs if proof["sequence"] == 1)
    second = next(proof for proof in proofs if proof["sequence"] == 2)
    assert first["previous_hash"] is None
    assert second["previous_hash"] == first["evidence_hash"]
    assert verify_proofs(client).json()["valid"] is True

    for snapshot in snapshots:
        assert (
            client.get(
                f"/datasets/raw/versions/1/snapshots/{snapshot['id']}"
            ).status_code
            == 404
        )


def test_concurrent_confirmations_of_the_same_request_have_one_winner(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    url = f"{requests_path('raw', 1, snapshot['id'])}/{request['id']}/confirm"

    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        statuses.append(thread_client.post(url).status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [200, 409], statuses
    proofs = list_proofs(client).json()
    assert len(proofs) == 1
    assert proofs[0]["sequence"] == 1
    assert verify_proofs(client).json()["valid"] is True


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


def _run(db_path: Path, script: str, stdin: str = "") -> str:
    env = os.environ.copy()
    env["DATA_LINEAGE_DB"] = str(db_path)
    result = subprocess.run(
        [sys.executable, "-c", script],
        input=stdin,
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


CREATE_AND_DELETE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response, code=(200, 201)):
    assert response.status_code in code, response.text

ok(client.post("/datasets", json={"name": "raw"}))
ok(client.post(
    "/datasets/raw/versions",
    json={"fields": [{"name": "id", "type": "string", "nullable": True}]},
))
ok(client.post(
    "/datasets/raw/versions/1/retention-policies",
    json={"retention_days": 0},
))
ids = []
for value in (1, 2):
    snapshot = client.post(
        "/datasets/raw/versions/1/snapshots", json={"rows": [{"id": value}]}
    )
    ok(snapshot)
    sid = snapshot.json()["id"]
    ids.append(sid)
    ok(client.post(
        f"/datasets/raw/versions/1/snapshots/{sid}/deletion-requests",
        json={"reason": f"reason-{value}"},
    ))
for sid in ids:
    request = client.get(
        f"/datasets/raw/versions/1/snapshots/{sid}/deletion-requests"
    ).json()[0]
    ok(client.post(
        f"/datasets/raw/versions/1/snapshots/{sid}/deletion-requests/"
        f"{request['id']}/confirm"
    ))
print("deleted")
"""

READ_AFTER_RESTART_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
listed = client.get("/datasets/raw/versions/1/snapshots/deletion-proofs")
assert listed.status_code == 200, listed.text
proofs = listed.json()
assert [proof["sequence"] for proof in proofs] == [1, 2]
assert proofs[0]["previous_hash"] is None
assert proofs[1]["previous_hash"] == proofs[0]["evidence_hash"]
assert [proof["reason"] for proof in proofs] == ["reason-1", "reason-2"]
verification = client.get(
    "/datasets/raw/versions/1/snapshots/deletion-proofs/verify"
)
assert verification.status_code == 200, verification.text
body = verification.json()
assert body == {"dataset": "raw", "version": 1, "valid": True,
                "checked_count": 2}
print(json.dumps([proofs[0]["evidence_hash"], proofs[1]["evidence_hash"]]))
"""


def test_proof_chain_survives_restart_and_verifies(tmp_path: Path) -> None:
    db_path = tmp_path / "proofs-lineage.db"
    assert _run(db_path, CREATE_AND_DELETE_SCRIPT) == "deleted"
    hashes = json.loads(_run(db_path, READ_AFTER_RESTART_SCRIPT))
    assert len(hashes) == 2
    assert all(len(value) == 64 for value in hashes)
