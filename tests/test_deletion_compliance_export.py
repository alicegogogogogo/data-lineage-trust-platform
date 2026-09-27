"""Tests for the read-only cross-version deletion compliance export."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
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
PROOF_CHAIN_KEYS = ["count", "sequence_range", "valid"]
TOTAL_KEYS = [
    "version_count",
    "deletion_request_count",
    "confirmed_request_count",
    "proof_count",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_dataset(
    client: TestClient, name: str = "raw", fields: list[str] | None = None
) -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    if fields is not None:
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


def add_version(
    client: TestClient, fields: list[str] | None = None, dataset: str = "raw"
) -> int:
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={
            "fields": [
                {"name": field, "type": "string", "nullable": True}
                for field in (fields if fields is not None else ["id"])
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
    client: TestClient,
    dataset: str = "raw",
    version: int = 1,
    rows: list[dict] | None = None,
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots",
        json={"rows": rows if rows is not None else [{"id": 1}]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_policy(
    client: TestClient,
    version: int = 1,
    retention_days: int = 0,
    dataset: str = "raw",
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/retention-policies",
        json={"retention_days": retention_days},
    )
    assert response.status_code == 201, response.text
    return response.json()


def requests_path(
    snapshot_id: int, dataset: str = "raw", version: int = 1
) -> str:
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
        requests_path(snapshot_id, dataset, version), json={"reason": reason}
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
        f"{requests_path(snapshot_id, dataset, version)}/{request_id}/confirm"
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
    request = create_request(client, snapshot_id, reason, dataset, version)
    return confirm_request(
        client, snapshot_id, request["id"], dataset, version
    )


def proofs_path(version: int = 1, dataset: str = "raw") -> str:
    return f"/datasets/{dataset}/versions/{version}/snapshots/deletion-proofs"


def get_export(client: TestClient, dataset: str = "raw") -> dict:
    response = client.get(f"/datasets/{dataset}/deletion-compliance-export")
    assert response.status_code == 200, response.text
    return response.json()


def export_response(client: TestClient, dataset: str = "raw"):
    response = client.get(f"/datasets/{dataset}/deletion-compliance-export")
    assert response.status_code == 200, response.text
    return response


def empty_chain() -> dict:
    return {"count": 0, "sequence_range": [None, None], "valid": True}


# --------------------------------------------------------------------------- #
# Empty state and wire format
# --------------------------------------------------------------------------- #


def test_export_without_versions_is_empty_not_an_error(client: TestClient) -> None:
    make_dataset(client)
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
    make_dataset(client, fields=["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
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
    # Lower-case booleans, no scientific notation for the integer counters.
    assert '"valid":true' in text
    assert "True" not in text
    assert "e+" not in text.lower()

    # Top-level key order.
    positions = [text.index(f'"{key}"') for key in TOP_LEVEL_KEYS]
    assert positions == sorted(positions)
    body = response.json()
    assert list(body) == TOP_LEVEL_KEYS
    assert list(body["totals"]) == TOTAL_KEYS
    version_entry = body["versions"][0]
    assert list(version_entry) == VERSION_KEYS
    assert list(version_entry["proof_chain"]) == PROOF_CHAIN_KEYS
    assert list(version_entry["deletion_requests"][0]) == REQUEST_KEYS


# --------------------------------------------------------------------------- #
# Versions, retention policy and ordering
# --------------------------------------------------------------------------- #


def test_export_versions_sort_ascending_and_cover_every_version(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    make_policy(client, retention_days=30)
    add_version(client)
    make_policy(client, version=2, retention_days=7)
    add_version(client)

    body = get_export(client)
    assert [entry["version"] for entry in body["versions"]] == [1, 2, 3]
    assert [entry["retention_days"] for entry in body["versions"]] == [30, 7, None]
    for entry in body["versions"]:
        assert set(entry) == set(VERSION_KEYS)
        assert entry["deletion_requests"] == []
        assert entry["proof_chain"] == empty_chain()


def test_export_null_retention_days_key_is_retained(client: TestClient) -> None:
    make_dataset(client, fields=["id"])
    response = export_response(client)
    entry = response.json()["versions"][0]
    assert "retention_days" in entry
    assert entry["retention_days"] is None
    assert '"retention_days":null' in response.text


# --------------------------------------------------------------------------- #
# Deletion requests: sorting, all states, field structure
# --------------------------------------------------------------------------- #


def test_export_lists_requests_of_every_snapshot_sorted_with_all_states(
    client: TestClient,
) -> None:
    # Version 1 feeds a downstream field, so its requests open as blocked.
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    make_policy(client, version=1)
    blocked_snapshot = make_snapshot(client, rows=[{"order_id": "a"}])
    blocked = create_request(client, blocked_snapshot["id"], reason="first")
    assert blocked["status"] == "blocked"

    # Version 2 has no downstream field: its requests open pending and one can
    # be confirmed, leaving pending and confirmed requests in one version.
    add_version(client, ["order_id"])
    make_policy(client, version=2)
    pending_snapshot = make_snapshot(client, version=2, rows=[{"order_id": "b"}])
    pending = create_request(
        client, pending_snapshot["id"], reason="held", version=2
    )
    assert pending["status"] == "pending"
    confirmed_snapshot = make_snapshot(client, version=2, rows=[{"order_id": "c"}])
    confirmed = create_request(
        client, confirmed_snapshot["id"], reason="gone", version=2
    )
    confirm_request(client, confirmed_snapshot["id"], confirmed["id"], version=2)

    body = get_export(client)
    first, second = body["versions"]

    blocked_requests = first["deletion_requests"]
    assert [request["id"] for request in blocked_requests] == [blocked["id"]]
    blocked_entry = blocked_requests[0]
    assert list(blocked_entry) == REQUEST_KEYS
    assert blocked_entry == {
        "id": blocked["id"],
        "snapshot_id": blocked_snapshot["id"],
        "policy_id": blocked["policy_id"],
        "reason": "first",
        "status": "blocked",
        "impacted": [{"dataset": "dm", "version": 1, "field": "id"}],
        "created_at": blocked["created_at"],
    }

    second_requests = second["deletion_requests"]
    assert [request["id"] for request in second_requests] == sorted(
        request["id"] for request in second_requests
    )
    assert [request["id"] for request in second_requests] == [
        pending["id"],
        confirmed["id"],
    ]
    by_id = {request["id"]: request for request in second_requests}
    assert by_id[pending["id"]]["status"] == "pending"
    assert by_id[confirmed["id"]]["status"] == "confirmed"
    for request in second_requests:
        assert list(request) == REQUEST_KEYS
        assert "confirmed_at" not in request


def test_export_keeps_confirmed_requests_after_their_snapshots_are_gone(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    confirmation = delete_snapshot(client, snapshot["id"])

    # The snapshot read is gone, but the request record and the export entry
    # survive.
    snapshot_read = client.get(
        f"/datasets/raw/versions/1/snapshots/{snapshot['id']}"
    )
    assert snapshot_read.status_code == 404
    requests_ = client.get(
        requests_path(snapshot["id"])
    ).json()
    assert [request["status"] for request in requests_] == ["confirmed"]
    assert confirmation["confirmed_at"]

    entry = get_export(client)["versions"][0]
    exported = entry["deletion_requests"]
    assert len(exported) == 1
    assert exported[0]["status"] == "confirmed"
    assert "confirmed_at" not in exported[0]
    assert entry["proof_chain"] == {
        "count": 1,
        "sequence_range": [1, 1],
        "valid": True,
    }


# --------------------------------------------------------------------------- #
# Proof chain summary
# --------------------------------------------------------------------------- #


def test_export_proof_chain_summary_matches_the_verify_read(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    make_policy(client)
    for value in range(3):
        snapshot = make_snapshot(client, rows=[{"id": value}])
        delete_snapshot(client, snapshot["id"])

    entry = get_export(client)["versions"][0]
    assert entry["proof_chain"] == {
        "count": 3,
        "sequence_range": [1, 3],
        "valid": True,
    }
    verify = client.get(f"{proofs_path()}/verify").json()
    assert verify["valid"] is True
    assert verify["checked_count"] == 3


def test_export_proof_chain_reports_invalid_like_the_verify_read(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    make_policy(client)
    for value in range(2):
        snapshot = make_snapshot(client, rows=[{"id": value}])
        delete_snapshot(client, snapshot["id"])

    db_file = os.environ["DATA_LINEAGE_DB"]
    with sqlite3.connect(db_file) as direct:
        direct.execute("DROP TRIGGER trg_snapshot_deletion_proofs_no_update")
        direct.execute(
            "UPDATE snapshot_deletion_proofs SET reason = 'tampered' "
            "WHERE sequence = 1"
        )

    summary = get_export(client)["versions"][0]["proof_chain"]
    # Tampering does not turn the export into an error; the count and range
    # still describe the stored rows and only validity flips.
    assert summary == {
        "count": 2,
        "sequence_range": [1, 2],
        "valid": False,
    }
    verify = client.get(f"{proofs_path()}/verify").json()
    assert verify["valid"] is False


def test_export_empty_proof_chain_is_valid_with_null_range(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    make_policy(client)
    # One request without a confirmation writes no proof.
    snapshot = make_snapshot(client)
    create_request(client, snapshot["id"])

    entry = get_export(client)["versions"][0]
    assert entry["proof_chain"] == empty_chain()
    assert '"sequence_range":[null,null]' in export_response(client).text


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #


def test_export_totals_equal_the_sum_of_the_version_entries(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    make_policy(client, version=1)
    blocked_snapshot = make_snapshot(client, rows=[{"order_id": "a"}])
    create_request(client, blocked_snapshot["id"])  # blocked, unconfirmed

    add_version(client, ["order_id"])
    make_policy(client, version=2)
    first = make_snapshot(client, version=2, rows=[{"order_id": "b"}])
    second = make_snapshot(client, version=2, rows=[{"order_id": "c"}])
    third = make_snapshot(client, version=2, rows=[{"order_id": "d"}])
    delete_snapshot(client, first["id"], version=2)
    delete_snapshot(client, second["id"], version=2)
    held = create_request(client, third["id"], reason="hold", version=2)
    assert held["status"] == "pending"

    add_version(client, ["order_id"])  # no policy, no requests, no proofs

    body = get_export(client)
    versions = body["versions"]
    totals = body["totals"]
    assert totals["version_count"] == len(versions) == 3
    assert totals["deletion_request_count"] == sum(
        len(version["deletion_requests"]) for version in versions
    )
    assert totals["confirmed_request_count"] == sum(
        1
        for version in versions
        for request in version["deletion_requests"]
        if request["status"] == "confirmed"
    )
    assert totals["proof_count"] == sum(
        version["proof_chain"]["count"] for version in versions
    )
    assert totals == {
        "version_count": 3,
        # One blocked request on v1 plus three requests on v2.
        "deletion_request_count": 4,
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

    with_body = client.request("GET", EXPORT_PATH, content=b"{}")
    whitespace_body = client.request("GET", EXPORT_PATH, content=b"   ")
    single_space_body = client.request("GET", EXPORT_PATH, content=b" ")
    with_query = client.get(EXPORT_PATH, params={"limit": 1})
    assert with_body.status_code == 422
    assert whitespace_body.status_code == 422
    assert single_space_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, whitespace_body, single_space_body, with_query):
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
    assert (
        client.request(
            "GET",
            "/datasets/ghost/deletion-compliance-export",
            content=b" ",
        ).status_code
        == 404
    )


def test_export_is_strictly_read_only(client: TestClient) -> None:
    make_dataset(client, fields=["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    delete_snapshot(client, snapshot["id"])

    requests_before = client.get(requests_path(snapshot["id"])).json()
    proofs_before = client.get(proofs_path()).json()
    verify_before = client.get(f"{proofs_path()}/verify").json()
    snapshots_before = client.get("/datasets/raw/versions/1/snapshots").json()
    first_text = export_response(client).text

    for _ in range(3):
        response = export_response(client)
        assert response.text == first_text
    assert client.get(requests_path(snapshot["id"])).json() == requests_before
    assert client.get(proofs_path()).json() == proofs_before
    assert client.get(f"{proofs_path()}/verify").json() == verify_before
    assert (
        client.get("/datasets/raw/versions/1/snapshots").json()
        == snapshots_before
    )


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
    json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
).status_code == 201
assert client.post(
    "/datasets/raw/versions/1/retention-policies",
    json={"retention_days": 0},
).status_code == 201
snapshot = client.post(
    "/datasets/raw/versions/1/snapshots", json={"rows": [{"id": 1}]}
)
assert snapshot.status_code == 201, snapshot.text
snapshot_id = snapshot.json()["id"]
request = client.post(
    f"/datasets/raw/versions/1/snapshots/{snapshot_id}/deletion-requests",
    json={"reason": "gone"},
)
assert request.status_code == 201, request.text
confirmed = client.post(
    f"/datasets/raw/versions/1/snapshots/{snapshot_id}/deletion-requests/"
    f"{request.json()['id']}/confirm",
)
assert confirmed.status_code == 200, confirmed.text
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
assert len(entry["deletion_requests"]) == 1
request = entry["deletion_requests"][0]
assert list(request) == [
    "id", "snapshot_id", "policy_id", "reason", "status",
    "impacted", "created_at",
]
assert request["status"] == "confirmed"
assert request["impacted"] == []
assert entry["proof_chain"] == {
    "count": 1, "sequence_range": [1, 1], "valid": True,
}
assert body["totals"] == {
    "version_count": 1,
    "deletion_request_count": 1,
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
