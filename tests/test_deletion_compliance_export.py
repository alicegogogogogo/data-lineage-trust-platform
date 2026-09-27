"""Tests for the read-only cross-version snapshot deletion compliance export.

The export aggregates, for every schema version of one dataset, the
registered retention period, the deletion requests (pending, blocked and
confirmed) and a summary of the version's deletion-proof chain into one
deterministic JSON document. It is recomputed on every read and never
writes.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXPORT_PATH = "/datasets/raw/deletion-compliance-export"

TOP_LEVEL_KEYS = ["dataset", "versions", "totals"]
VERSION_KEYS = ["version", "retention_days", "deletion_requests", "proof_chain"]
REQUEST_KEYS = [
    "id",
    "snapshot_id",
    "policy_id",
    "reason",
    "status",
    "impacted",
    "created_at",
]
PROOF_CHAIN_KEYS = ["proof_count", "first_sequence", "last_sequence", "valid"]
TOTAL_KEYS = [
    "version_count",
    "deletion_request_count",
    "confirmed_request_count",
    "proof_count",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_dataset(client: TestClient, name: str = "raw", fields: list[str] = ("id",)) -> None:
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


def add_version(client: TestClient, fields: list[str], dataset: str = "raw") -> int:
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={
            "fields": [
                {"name": field, "type": "string", "nullable": True}
                for field in fields
            ]
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["version"]


def add_link(
    client: TestClient,
    source: tuple[str, int, str],
    target: tuple[str, int, str],
) -> None:
    source_dataset, source_version, source_field = source
    target_dataset, target_version, target_field = target
    response = client.post(
        f"/datasets/{target_dataset}/versions/{target_version}/lineage",
        json={
            "target_dataset": target_dataset,
            "target_version": target_version,
            "target_field": target_field,
            "source_dataset": source_dataset,
            "source_version": source_version,
            "source_field": source_field,
        },
    )
    assert response.status_code == 201, response.text


def make_snapshot(
    client: TestClient, dataset: str = "raw", version: int = 1
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots",
        json={"rows": [{"id": 1}]},
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


def confirm_request(
    client: TestClient,
    snapshot_id: int,
    request_id: int,
    dataset: str = "raw",
    version: int = 1,
) -> dict:
    response = client.post(
        f"{requests_path(dataset, version, snapshot_id)}/{request_id}/confirm"
    )
    assert response.status_code == 200, response.text
    return response.json()


def delete_snapshot(
    client: TestClient,
    snapshot_id: int,
    reason: str = "no longer needed",
    dataset: str = "raw",
    version: int = 1,
) -> dict:
    """Open and confirm a deletion request, returning the confirmation."""
    request = create_request(client, snapshot_id, reason, dataset, version)
    return confirm_request(client, snapshot_id, request["id"], dataset, version)


def proofs_path(dataset: str, version: int) -> str:
    return f"/datasets/{dataset}/versions/{version}/snapshots/deletion-proofs"


def backdate_snapshot(snapshot_id: int, days: int) -> None:
    """Age an existing snapshot directly in the database (bypassing the API)."""
    db_path = os.environ["DATA_LINEAGE_DB"]
    old = (
        datetime.now(timezone.utc) - timedelta(days=days, seconds=1)
    ).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE snapshots SET created_at = ? WHERE id = ?",
            (old, snapshot_id),
        )


def get_export(client: TestClient, dataset: str = "raw") -> dict:
    response = client.get(f"/datasets/{dataset}/deletion-compliance-export")
    assert response.status_code == 200, response.text
    return response.json()


def export_response(client: TestClient, dataset: str = "raw"):
    response = client.get(f"/datasets/{dataset}/deletion-compliance-export")
    assert response.status_code == 200, response.text
    return response


# --------------------------------------------------------------------------- #
# Empty state and wire format
# --------------------------------------------------------------------------- #


def test_export_without_versions_is_empty_not_an_error(client: TestClient) -> None:
    assert client.post("/datasets", json={"name": "raw"}).status_code == 201
    body = get_export(client)
    assert set(body) == set(TOP_LEVEL_KEYS)
    assert body["dataset"] == "raw"
    assert body["versions"] == []
    assert body["totals"] == {
        "version_count": 0,
        "deletion_request_count": 0,
        "confirmed_request_count": 0,
        "proof_count": 0,
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
    make_policy(client, retention_days=30)
    snapshot = make_snapshot(client)
    backdate_snapshot(snapshot["id"], days=31)
    delete_snapshot(client, snapshot["id"])

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
    # Lower-case booleans and literals.
    assert '"valid":true' in text
    assert "True" not in text

    # Top-level and nested key order.
    positions = [text.index(f'"{key}"') for key in TOP_LEVEL_KEYS]
    assert positions == sorted(positions)
    body = response.json()
    assert list(body) == TOP_LEVEL_KEYS
    assert list(body["totals"]) == TOTAL_KEYS
    version_entry = body["versions"][0]
    assert list(version_entry) == VERSION_KEYS
    assert list(version_entry["deletion_requests"][0]) == REQUEST_KEYS
    assert list(version_entry["proof_chain"]) == PROOF_CHAIN_KEYS


# --------------------------------------------------------------------------- #
# Versions, retention periods and ordering
# --------------------------------------------------------------------------- #


def test_export_versions_sort_ascending_and_cover_every_version(
    client: TestClient,
) -> None:
    make_dataset(client)
    make_policy(client, retention_days=30)
    add_version(client, ["id", "email"])
    add_version(client, ["email"])
    make_policy(client, version=3, retention_days=7)

    body = get_export(client)
    assert [entry["version"] for entry in body["versions"]] == [1, 2, 3]
    for entry in body["versions"]:
        assert list(entry) == VERSION_KEYS
    # The retention period is the registered one; a version without a
    # retention policy reports null and the key is not omitted.
    assert body["versions"][0]["retention_days"] == 30
    assert body["versions"][1]["retention_days"] is None
    assert body["versions"][2]["retention_days"] == 7
    assert '"retention_days":null' in export_response(client).text


def test_export_version_without_requests_or_proofs_is_normal(
    client: TestClient,
) -> None:
    make_dataset(client)
    entry = get_export(client)["versions"][0]
    assert entry["retention_days"] is None
    assert entry["deletion_requests"] == []
    assert entry["proof_chain"] == {
        "proof_count": 0,
        "first_sequence": None,
        "last_sequence": None,
        "valid": True,
    }


# --------------------------------------------------------------------------- #
# Deletion requests
# --------------------------------------------------------------------------- #


def test_export_lists_requests_sorted_with_every_status(client: TestClient) -> None:
    make_dataset(client)
    make_policy(client)
    # A downstream link makes this version's requests open as blocked.
    make_dataset(client, name="mart")
    add_link(client, ("raw", 1, "id"), ("mart", 1, "id"))

    blocked_snapshot = make_snapshot(client)
    blocked = create_request(client, blocked_snapshot["id"], reason="blocked one")
    assert blocked["status"] == "blocked"

    # Confirmed and pending requests live on versions without downstream
    # impact; use a second version so the blocked request stays open.
    add_version(client, ["id"])
    make_policy(client, version=2)
    confirmed_snapshot = make_snapshot(client, version=2)
    confirmed = create_request(client, confirmed_snapshot["id"], version=2)
    confirm_request(client, confirmed_snapshot["id"], confirmed["id"], version=2)
    pending_snapshot = make_snapshot(client, version=2)
    pending = create_request(client, pending_snapshot["id"], version=2)
    assert pending["status"] == "pending"

    body = get_export(client)
    first_requests = body["versions"][0]["deletion_requests"]
    assert [request["id"] for request in first_requests] == [blocked["id"]]
    assert first_requests[0]["status"] == "blocked"
    assert first_requests[0]["impacted"] == [
        {"dataset": "mart", "version": 1, "field": "id"}
    ]

    second_requests = body["versions"][1]["deletion_requests"]
    assert [request["id"] for request in second_requests] == [
        confirmed["id"], pending["id"]
    ]
    assert [request["status"] for request in second_requests] == [
        "confirmed", "pending",
    ]
    for entry in body["versions"]:
        for request in entry["deletion_requests"]:
            assert list(request) == REQUEST_KEYS
            assert request["snapshot_id"]
            assert request["policy_id"]
            assert request["reason"]
            assert request["created_at"]


# --------------------------------------------------------------------------- #
# Proof-chain summary
# --------------------------------------------------------------------------- #


def test_export_proof_chain_summary_tracks_written_proofs(
    client: TestClient,
) -> None:
    make_dataset(client)
    make_policy(client)
    first = make_snapshot(client)
    second = make_snapshot(client)
    delete_snapshot(client, first["id"], reason="first")
    delete_snapshot(client, second["id"], reason="second")

    # A second version with no deletions keeps an empty, valid chain.
    add_version(client, ["id"])

    body = get_export(client)
    assert body["versions"][0]["proof_chain"] == {
        "proof_count": 2,
        "first_sequence": 1,
        "last_sequence": 2,
        "valid": True,
    }
    assert body["versions"][1]["proof_chain"] == {
        "proof_count": 0,
        "first_sequence": None,
        "last_sequence": None,
        "valid": True,
    }
    # The summary agrees with the per-version proof reads.
    assert len(client.get(proofs_path("raw", 1)).json()) == 2


def test_export_proof_chain_summary_reports_a_tampered_chain(
    client: TestClient,
) -> None:
    make_dataset(client)
    make_policy(client)
    snapshot = make_snapshot(client)
    delete_snapshot(client, snapshot["id"])
    assert get_export(client)["versions"][0]["proof_chain"]["valid"] is True

    # Rewrite a stored proof directly; the export re-verifies on every read.
    db_path = os.environ["DATA_LINEAGE_DB"]
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TRIGGER trg_snapshot_deletion_proofs_no_update")
        conn.execute(
            "UPDATE snapshot_deletion_proofs SET reason = 'tampered' "
            "WHERE sequence = 1"
        )
    chain = get_export(client)["versions"][0]["proof_chain"]
    assert chain["proof_count"] == 1
    assert chain["first_sequence"] == 1
    assert chain["last_sequence"] == 1
    assert chain["valid"] is False


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #


def test_export_totals_equal_the_sum_of_the_version_entries(
    client: TestClient,
) -> None:
    make_dataset(client)
    make_policy(client)
    first = make_snapshot(client)
    delete_snapshot(client, first["id"])
    pending_snapshot = make_snapshot(client)
    create_request(client, pending_snapshot["id"])

    add_version(client, ["id"])
    make_policy(client, version=2)
    second = make_snapshot(client, version=2)
    delete_snapshot(client, second["id"], version=2)

    add_version(client, ["email"])

    body = get_export(client)
    versions = body["versions"]
    totals = body["totals"]
    assert list(totals) == TOTAL_KEYS
    assert totals["version_count"] == len(versions)
    assert totals["deletion_request_count"] == sum(
        len(entry["deletion_requests"]) for entry in versions
    )
    assert totals["confirmed_request_count"] == sum(
        1
        for entry in versions
        for request in entry["deletion_requests"]
        if request["status"] == "confirmed"
    )
    assert totals["proof_count"] == sum(
        entry["proof_chain"]["proof_count"] for entry in versions
    )
    assert totals == {
        "version_count": 3,
        "deletion_request_count": 3,
        "confirmed_request_count": 2,
        "proof_count": 2,
    }


# --------------------------------------------------------------------------- #
# Errors: 404 / 422, no writes
# --------------------------------------------------------------------------- #


def test_export_unknown_dataset_returns_404(client: TestClient) -> None:
    response = client.get("/datasets/ghost/deletion-compliance-export")
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "not_found"


def test_export_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset(client)
    before = export_response(client).text

    rejected = [
        client.request("GET", EXPORT_PATH, content=b"{}"),
        client.request("GET", EXPORT_PATH, content=b" "),
        client.request("GET", EXPORT_PATH, content=b"  \n\t "),
        client.get(EXPORT_PATH, params={"limit": 1}),
    ]
    for response in rejected:
        assert response.status_code == 422
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]

    # The rejections wrote nothing.
    assert export_response(client).text == before


def test_export_shape_errors_keep_404_precedence(client: TestClient) -> None:
    make_dataset(client)
    assert client.get(
        "/datasets/ghost/deletion-compliance-export", params={"x": "1"}
    ).status_code == 404
    assert (
        client.request(
            "GET", "/datasets/ghost/deletion-compliance-export", content=b"{}"
        ).status_code
        == 404
    )


def test_export_is_strictly_read_only(client: TestClient) -> None:
    make_dataset(client)
    make_policy(client)
    snapshot = make_snapshot(client)
    delete_snapshot(client, snapshot["id"])
    pending_snapshot = make_snapshot(client)
    request = create_request(client, pending_snapshot["id"])

    requests_before = client.get(
        requests_path("raw", 1, pending_snapshot["id"])
    ).json()
    proofs_before = client.get(proofs_path("raw", 1)).json()
    first_text = export_response(client).text

    for _ in range(3):
        response = export_response(client)
        assert response.text == first_text
    assert client.get(requests_path("raw", 1, pending_snapshot["id"])).json() == (
        requests_before
    )
    assert client.get(proofs_path("raw", 1)).json() == proofs_before
    # The pending request was not confirmed away by the reads.
    assert request["status"] == "pending"


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


_CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

assert client.post("/datasets", json={"name": "raw"}).status_code == 201
assert client.post(
    "/datasets/raw/versions",
    json={"fields": [{"name": "id", "type": "string", "nullable": True}]},
).status_code == 201
assert client.post(
    "/datasets/raw/versions/1/retention-policies",
    json={"retention_days": 0},
).status_code == 201
snapshot = client.post(
    "/datasets/raw/versions/1/snapshots", json={"rows": [{"id": 1}]}
).json()
request = client.post(
    f"/datasets/raw/versions/1/snapshots/{snapshot['id']}/deletion-requests",
    json={"reason": "no longer needed"},
)
assert request.status_code == 201, request.text
request_id = request.json()["id"]
confirmed = client.post(
    f"/datasets/raw/versions/1/snapshots/{snapshot['id']}"
    f"/deletion-requests/{request_id}/confirm"
)
assert confirmed.status_code == 200, confirmed.text
pending_snapshot = client.post(
    "/datasets/raw/versions/1/snapshots", json={"rows": [{"id": 2}]}
).json()
pending = client.post(
    f"/datasets/raw/versions/1/snapshots/{pending_snapshot['id']}"
    "/deletion-requests",
    json={"reason": "still open"},
)
assert pending.status_code == 201, pending.text
print("created")
"""

_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

response = client.get("/datasets/raw/deletion-compliance-export")
assert response.status_code == 200, response.text
assert response.text.endswith("\\n")
body = response.json()
assert list(body) == ["dataset", "versions", "totals"]
assert body["dataset"] == "raw"
assert [v["version"] for v in body["versions"]] == [1]
entry = body["versions"][0]
assert list(entry) == [
    "version", "retention_days", "deletion_requests", "proof_chain",
]
assert entry["retention_days"] == 0
assert [r["status"] for r in entry["deletion_requests"]] == [
    "confirmed", "pending",
]
assert entry["proof_chain"] == {
    "proof_count": 1,
    "first_sequence": 1,
    "last_sequence": 1,
    "valid": True,
}
assert body["totals"] == {
    "version_count": 1,
    "deletion_request_count": 2,
    "confirmed_request_count": 1,
    "proof_count": 1,
}
again = client.get("/datasets/raw/deletion-compliance-export")
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
    db_path = tmp_path / "deletion-compliance-export.db"
    assert _run_script(db_path, _CREATE_SCRIPT) == "created"
    assert _run_script(db_path, _VERIFY_SCRIPT) == "verified"
