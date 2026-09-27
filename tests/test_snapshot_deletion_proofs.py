"""Tests for the append-only snapshot deletion-proof chain."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
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
    client: TestClient, dataset: str = "raw", version: int = 1, rows: list | None = None
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots",
        json={"rows": rows if rows is not None else [{"id": 1}]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_policy(
    client: TestClient, dataset: str = "raw", version: int = 1, retention_days: int = 0
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/retention-policies",
        json={"retention_days": retention_days},
    )
    assert response.status_code == 201, response.text
    return response.json()


def snapshots_path(dataset: str = "raw", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/snapshots"


def proofs_path(dataset: str = "raw", version: int = 1) -> str:
    return f"{snapshots_path(dataset, version)}/deletion-proofs"


def delete_snapshot(
    client: TestClient,
    snapshot_id: int,
    reason: str = "no longer needed",
    dataset: str = "raw",
    version: int = 1,
) -> dict:
    """Run the full request/confirm flow and return the confirm response body."""
    base = f"{snapshots_path(dataset, version)}/{snapshot_id}/deletion-requests"
    request = client.post(base, json={"reason": reason})
    assert request.status_code == 201, request.text
    confirmed = client.post(f"{base}/{request.json()['id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def expected_evidence_hash(proof: dict) -> str:
    """Recompute a proof's evidence hash with the audit-chain digest scheme."""
    payload = {
        "previous_hash": proof["previous_hash"],
        "reason": proof["reason"],
        "row_count": proof["row_count"],
        "sequence": proof["sequence"],
        "snapshot_id": proof["snapshot_id"],
        "stored_hash": proof["stored_hash"],
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Empty chain
# --------------------------------------------------------------------------- #


def test_empty_chain_lists_nothing_and_verifies(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)

    listed = client.get(proofs_path())
    assert listed.status_code == 200
    assert listed.text == "[]\n"

    verification = client.get(f"{proofs_path()}/verify")
    assert verification.status_code == 200
    assert (
        verification.text
        == '{"dataset":"raw","version":1,"valid":true,"checked_count":0}\n'
    )


# --------------------------------------------------------------------------- #
# Appending proofs through confirmation
# --------------------------------------------------------------------------- #


def test_confirmed_deletion_appends_one_proof(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client, rows=[{"id": 1}, {"id": 2}])
    stored_hash = client.get(
        f"{snapshots_path()}/{snapshot['id']}/verify"
    ).json()["stored_hash"]

    confirmed = delete_snapshot(client, snapshot["id"], reason="gdpr request")

    listed = client.get(proofs_path())
    assert listed.status_code == 200
    proofs = listed.json()
    assert len(proofs) == 1
    proof = proofs[0]
    # Exactly these keys, in this fixed order.
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
    # Row count and fingerprint are the snapshot's persisted deletion-moment
    # values; the reason comes from the request and the confirmation instant
    # is the deletion commit timestamp.
    assert proof["row_count"] == 2
    assert proof["stored_hash"] == stored_hash
    assert proof["reason"] == "gdpr request"
    assert proof["confirmed_at"] == confirmed["confirmed_at"]
    assert proof["previous_hash"] is None
    assert proof["evidence_hash"] == expected_evidence_hash(proof)

    verification = client.get(f"{proofs_path()}/verify").json()
    assert verification == {
        "dataset": "raw",
        "version": 1,
        "valid": True,
        "checked_count": 1,
    }


def test_chain_links_sequentially_across_deletions(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    first = make_snapshot(client, rows=[{"id": 1}])
    second = make_snapshot(client, rows=[{"id": 2}])

    delete_snapshot(client, first["id"], reason="one")
    delete_snapshot(client, second["id"], reason="two")

    proofs = client.get(proofs_path()).json()
    assert [proof["sequence"] for proof in proofs] == [1, 2]
    assert [proof["snapshot_id"] for proof in proofs] == [first["id"], second["id"]]
    assert proofs[0]["previous_hash"] is None
    assert proofs[1]["previous_hash"] == proofs[0]["evidence_hash"]
    assert all(
        proof["evidence_hash"] == expected_evidence_hash(proof) for proof in proofs
    )

    verification = client.get(f"{proofs_path()}/verify").json()
    assert verification["valid"] is True
    assert verification["checked_count"] == 2


def test_chains_are_independent_per_version(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    # A second schema version of the same dataset.
    assert (
        client.post(
            "/datasets/raw/versions",
            json={"fields": [{"name": "id", "type": "string", "nullable": True}]},
        ).status_code
        == 201
    )
    make_policy(client, version=1)
    make_policy(client, version=2)

    first = make_snapshot(client, version=1, rows=[{"id": 1}])
    second = make_snapshot(client, version=2, rows=[{"id": 2}])
    delete_snapshot(client, first["id"], version=1)
    delete_snapshot(client, second["id"], version=2)

    for version, snapshot in ((1, first), (2, second)):
        proofs = client.get(proofs_path(version=version)).json()
        assert [proof["sequence"] for proof in proofs] == [1]
        assert proofs[0]["snapshot_id"] == snapshot["id"]
        assert proofs[0]["previous_hash"] is None
        verification = client.get(f"{proofs_path(version=version)}/verify").json()
        assert verification == {
            "dataset": "raw",
            "version": version,
            "valid": True,
            "checked_count": 1,
        }


def test_snapshot_ids_are_not_reused_after_deletion(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    deleted = make_snapshot(client, rows=[{"id": 1}])
    delete_snapshot(client, deleted["id"])

    replacement = make_snapshot(client, rows=[{"id": 2}])
    assert replacement["id"] > deleted["id"]


def test_deleted_snapshot_addresses_and_repeat_confirm_behave_as_before(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    base = f"{snapshots_path()}/{snapshot['id']}"
    request = client.post(
        f"{base}/deletion-requests", json={"reason": "cleanup"}
    ).json()
    assert client.post(
        f"{base}/deletion-requests/{request['id']}/confirm"
    ).status_code == 200

    # The deleted snapshot's reads stay 404, the request list stays readable
    # and a repeated confirmation stays 409; none of this adds a proof.
    assert client.get(base).status_code == 404
    assert client.get(f"{base}/verify").status_code == 404
    listed = client.get(f"{base}/deletion-requests")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [request["id"]]
    assert (
        client.post(f"{base}/deletion-requests/{request['id']}/confirm").status_code
        == 409
    )
    assert len(client.get(proofs_path()).json()) == 1


# --------------------------------------------------------------------------- #
# No proof without a committed deletion
# --------------------------------------------------------------------------- #


def test_failed_confirmations_write_no_proof(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=7)
    snapshot = make_snapshot(client)
    base = f"{snapshots_path()}/{snapshot['id']}/deletion-requests"
    request = client.post(base, json={"reason": "too early"}).json()

    # The snapshot has not reached the retention age: 409 and zero writes.
    assert client.post(f"{base}/{request['id']}/confirm").status_code == 409
    assert client.get(proofs_path()).json() == []
    assert client.get(f"{proofs_path()}/verify").json()["checked_count"] == 0


def test_rejected_request_creation_writes_no_proof(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    base = f"{snapshots_path()}/{snapshot['id']}/deletion-requests"

    assert client.post(base, json={"reason": "   "}).status_code == 422
    assert client.get(proofs_path()).json() == []


def test_snapshots_deleted_before_proofs_existed_get_none(client: TestClient) -> None:
    # A deletion committed without the proof writer (as every deletion before
    # this feature) is not backfilled: the chain stays empty and valid.
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    db_path = os.environ["DATA_LINEAGE_DB"]
    with sqlite3.connect(db_path) as direct:
        direct.execute(
            "INSERT INTO snapshot_deletion_requests ("
            "version_id, snapshot_id, policy_id, reason, status, impacted, "
            "created_at, confirmed_at"
            ") VALUES (1, ?, 1, 'legacy', 'confirmed', '[]', "
            "'2020-01-01T00:00:00+00:00', '2020-01-02T00:00:00+00:00')",
            (snapshot["id"],),
        )
        direct.execute("DELETE FROM snapshots WHERE id = ?", (snapshot["id"],))

    assert client.get(proofs_path()).json() == []
    verification = client.get(f"{proofs_path()}/verify").json()
    assert verification == {
        "dataset": "raw",
        "version": 1,
        "valid": True,
        "checked_count": 0,
    }


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


def test_verify_detects_tampering_and_broken_links(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    for row in ({"id": 1}, {"id": 2}, {"id": 3}):
        snapshot = make_snapshot(client, rows=[row])
        delete_snapshot(client, snapshot["id"])

    db_path = os.environ["DATA_LINEAGE_DB"]

    def verify() -> tuple[bool, int]:
        body = client.get(f"{proofs_path()}/verify").json()
        return body["valid"], body["checked_count"]

    assert verify() == (True, 3)

    # Rewrite a stored field (the immutability triggers are dropped for the
    # duration of the tampering). The verifier must still detect it.
    with sqlite3.connect(db_path) as direct:
        direct.execute("DROP TRIGGER trg_snapshot_deletion_proofs_no_update")
        direct.execute("DROP TRIGGER trg_snapshot_deletion_proofs_no_delete")
        direct.execute(
            "UPDATE snapshot_deletion_proofs SET reason = 'tampered' "
            "WHERE sequence = 2"
        )
    assert verify() == (False, 3)

    # Restore, then break the chain link instead; also detected.
    with sqlite3.connect(db_path) as direct:
        direct.execute("DROP TRIGGER trg_snapshot_deletion_proofs_no_update")
        direct.execute("DROP TRIGGER trg_snapshot_deletion_proofs_no_delete")
        direct.execute(
            "UPDATE snapshot_deletion_proofs SET reason = 'no longer needed' "
            "WHERE sequence = 2"
        )
    assert verify() == (True, 3)
    with sqlite3.connect(db_path) as direct:
        direct.execute("DROP TRIGGER trg_snapshot_deletion_proofs_no_update")
        direct.execute("DROP TRIGGER trg_snapshot_deletion_proofs_no_delete")
        direct.execute(
            "UPDATE snapshot_deletion_proofs SET previous_hash = 'forged' "
            "WHERE sequence = 2"
        )
    assert verify() == (False, 3)


def test_proofs_are_immutable_in_the_database(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    delete_snapshot(client, snapshot["id"])

    db_path = os.environ["DATA_LINEAGE_DB"]
    with sqlite3.connect(db_path) as direct:
        for statement in (
            "UPDATE snapshot_deletion_proofs SET reason = 'x' WHERE sequence = 1",
            "DELETE FROM snapshot_deletion_proofs WHERE sequence = 1",
        ):
            try:
                direct.execute(statement)
                raise AssertionError("mutation was not rejected")
            except sqlite3.IntegrityError as exc:
                assert "immutable" in str(exc)

    assert client.get(f"{proofs_path()}/verify").json()["valid"] is True


def test_no_mutating_http_routes(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    delete_snapshot(client, snapshot["id"])

    for path in (proofs_path(), f"{proofs_path()}/verify"):
        for method in ("post", "put", "patch", "delete"):
            response = getattr(client, method)(path)
            assert response.status_code == 405, (method, path, response.status_code)
    assert client.get(f"{proofs_path()}/verify").json()["valid"] is True


# --------------------------------------------------------------------------- #
# Request shape and error precedence
# --------------------------------------------------------------------------- #


def test_unknown_dataset_or_version_is_404_before_shape_checks(
    client: TestClient,
) -> None:
    for path in (
        "/datasets/ghost/versions/1/snapshots/deletion-proofs",
        "/datasets/ghost/versions/1/snapshots/deletion-proofs/verify",
    ):
        response = client.request("GET", path, content=b"{}")
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    make_dataset(client, "raw", ["id"])
    for path in (
        "/datasets/raw/versions/9/snapshots/deletion-proofs",
        "/datasets/raw/versions/9/snapshots/deletion-proofs/verify",
    ):
        assert client.get(path).status_code == 404


def test_body_and_query_parameters_are_422(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    delete_snapshot(client, snapshot["id"])

    for path in (proofs_path(), f"{proofs_path()}/verify"):
        for body in (b"{}", b" ", b"not json"):
            response = client.request("GET", path, content=body)
            assert response.status_code == 422, (path, body)
            assert response.json()["error"] == "validation_error"
        response = client.get(path, params={"x": "1"})
        assert response.status_code == 422
        assert response.json()["error"] == "validation_error"

    # Nothing was written and the chain is undisturbed.
    assert len(client.get(proofs_path()).json()) == 1
    assert client.get(f"{proofs_path()}/verify").json()["valid"] is True


def test_error_json_never_leaks_internals(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    response = client.request(
        "GET", proofs_path(), content=b"{}", params={"x": "1"}
    )
    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"error", "detail"}
    for text in body.values():
        lowered = text.lower()
        assert "sql" not in lowered
        assert "traceback" not in lowered
        assert "sqlite" not in lowered


# --------------------------------------------------------------------------- #
# Deterministic serialization
# --------------------------------------------------------------------------- #


def test_list_and_verify_documents_are_deterministic(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client, rows=[{"id": "é"}])
    delete_snapshot(client, snapshot["id"], reason="données sensibles")

    listed = client.get(proofs_path())
    proofs = listed.json()
    expected = json.dumps(
        [
            {
                "sequence": proofs[0]["sequence"],
                "snapshot_id": proofs[0]["snapshot_id"],
                "row_count": proofs[0]["row_count"],
                "stored_hash": proofs[0]["stored_hash"],
                "reason": "données sensibles",
                "confirmed_at": proofs[0]["confirmed_at"],
                "previous_hash": None,
                "evidence_hash": proofs[0]["evidence_hash"],
            }
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert listed.text == expected + "\n"
    assert not listed.text.endswith("\n\n")

    verification = client.get(f"{proofs_path()}/verify")
    assert (
        verification.text
        == '{"dataset":"raw","version":1,"valid":true,"checked_count":1}\n'
    )
    # Repeat reads are byte-identical.
    assert client.get(proofs_path()).text == listed.text
    assert client.get(f"{proofs_path()}/verify").text == verification.text


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


CREATE_PROOF_SCRIPT = """
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
snapshot = client.post(
    "/datasets/raw/versions/1/snapshots", json={"rows": [{"id": 1}]}
)
ok(snapshot)
request = client.post(
    f"/datasets/raw/versions/1/snapshots/{snapshot.json()['id']}/deletion-requests",
    json={"reason": "gdpr request"},
)
ok(request)
ok(client.post(
    f"/datasets/raw/versions/1/snapshots/{snapshot.json()['id']}/deletion-requests/"
    f"{request.json()['id']}/confirm"
))
import json
proofs = client.get("/datasets/raw/versions/1/snapshots/deletion-proofs")
ok(proofs)
print(json.dumps(proofs.json()))
"""

CHECK_PROOF_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
expected = json.loads(input())
proofs = client.get("/datasets/raw/versions/1/snapshots/deletion-proofs")
assert proofs.status_code == 200, proofs.text
assert proofs.json() == expected
verification = client.get("/datasets/raw/versions/1/snapshots/deletion-proofs/verify")
assert verification.status_code == 200, verification.text
assert verification.json() == {
    "dataset": "raw",
    "version": 1,
    "valid": True,
    "checked_count": 1,
}
print("persisted")
"""


def test_proofs_survive_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "proofs-lineage.db"

    # Process 1: confirm a deletion so its proof commits.
    proof_payload = _run(db_path, CREATE_PROOF_SCRIPT)
    assert json.loads(proof_payload)[0]["sequence"] == 1

    # Process 2: the proof and its verification survive the restart unchanged.
    assert _run(db_path, CHECK_PROOF_SCRIPT, stdin=proof_payload) == "persisted"
