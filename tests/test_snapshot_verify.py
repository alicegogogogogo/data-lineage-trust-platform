"""Tests for the read-only snapshot content-fingerprint verification endpoint."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def create_dataset_and_version(
    client: TestClient, dataset: str = "orders", version: int = 1
) -> None:
    response = client.post("/datasets", json={"name": dataset})
    assert response.status_code == 201, response.text
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={"fields": [
            {"name": "id", "type": "integer", "nullable": False},
            {"name": "label", "type": "string", "nullable": True},
        ]},
    )
    assert response.status_code == 201, response.text
    for _ in range(1, version):
        extra = client.post(
            f"/datasets/{dataset}/versions",
            json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
        )
        assert extra.status_code == 201, extra.text


def make_snapshot(
    client: TestClient,
    rows: list,
    dataset: str = "orders",
    version: int = 1,
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots",
        json={"rows": rows},
    )
    assert response.status_code == 201, response.text
    return response.json()


def verify_path(snapshot_id: int, dataset: str = "orders", version: int = 1) -> str:
    return (
        f"/datasets/{dataset}/versions/{version}/snapshots/{snapshot_id}/verify"
    )


def expected_hash(rows: list) -> str:
    """Independent recomputation of the fingerprint the service must use."""
    text = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def replace_stored_rows(snapshot_id: int, rows: list) -> None:
    """Tamper with a snapshot's stored rows directly, bypassing the API."""
    db_path = os.environ["DATA_LINEAGE_DB"]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE snapshots SET rows = ? WHERE id = ?",
            (json.dumps(rows), snapshot_id),
        )


# --------------------------------------------------------------------------- #
# Successful verification
# --------------------------------------------------------------------------- #


def test_verify_reports_valid_for_untampered_snapshot(client: TestClient) -> None:
    create_dataset_and_version(client)
    rows = [{"id": 1, "label": "a"}, {"id": 2, "nested": {"z": 1, "a": [True, None]}}]
    snapshot = make_snapshot(client, rows)

    response = client.get(verify_path(snapshot["id"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert list(body) == [
        "dataset",
        "version",
        "snapshot_id",
        "row_count",
        "stored_hash",
        "computed_hash",
        "valid",
    ]
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["snapshot_id"] == snapshot["id"]
    assert body["row_count"] == 2
    assert body["stored_hash"] == expected_hash(rows)
    assert body["computed_hash"] == body["stored_hash"]
    assert body["valid"] is True
    assert len(body["stored_hash"]) == 64
    assert all(ch in "0123456789abcdef" for ch in body["stored_hash"])


def test_verify_document_is_compact_fixed_order_with_one_newline(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [{"label": "x"}])

    response = client.get(verify_path(snapshot["id"]))
    assert response.status_code == 200
    body = response.json()
    # Exact deterministic bytes: compact separators, lowercase boolean,
    # exactly one trailing newline.
    assert response.text == (
        json.dumps(body, separators=(",", ":"), ensure_ascii=False) + "\n"
    )
    assert response.text.endswith("\n")
    assert not response.text.endswith("\n\n")
    assert ": " not in response.text
    assert ", " not in response.text
    assert '"valid":true' in response.text
    # Fixed key order regardless of model insertion order.
    text = response.text
    assert text.index("dataset") < text.index("version") < text.index("snapshot_id")
    assert text.index("snapshot_id") < text.index("row_count")
    assert text.index("row_count") < text.index("stored_hash")
    assert text.index("stored_hash") < text.index("computed_hash")
    assert text.index("computed_hash") < text.index("valid")


def test_verify_empty_snapshot_has_a_stable_fingerprint(client: TestClient) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [])

    body = client.get(verify_path(snapshot["id"])).json()
    assert body["row_count"] == 0
    assert body["stored_hash"] == expected_hash([])
    assert body["computed_hash"] == body["stored_hash"]
    assert body["valid"] is True


def test_verify_hash_canonicalizes_object_keys_but_keeps_row_order(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    # Object key order is irrelevant to the fingerprint; the deep nesting is
    # canonicalized recursively.
    first = make_snapshot(client, [{"b": 2, "a": 1, "o": {"z": 1, "m": [1, 2]}}])
    second = make_snapshot(client, [{"o": {"m": [1, 2], "z": 1}, "a": 1, "b": 2}])
    first_hash = client.get(verify_path(first["id"])).json()["stored_hash"]
    second_hash = client.get(verify_path(second["id"])).json()["stored_hash"]
    assert first_hash == second_hash

    # The saved row sequence order is significant.
    reversed_rows = make_snapshot(client, [{"id": 2}, {"id": 1}])
    ordered_rows = make_snapshot(client, [{"id": 1}, {"id": 2}])
    reversed_hash = client.get(verify_path(reversed_rows["id"])).json()["stored_hash"]
    ordered_hash = client.get(verify_path(ordered_rows["id"])).json()["stored_hash"]
    assert reversed_hash != ordered_hash
    assert ordered_hash == expected_hash([{"id": 1}, {"id": 2}])


def test_verify_distinguishes_value_types_and_negative_zero(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    variants = [
        [{"v": 1}],
        [{"v": 1.0}],
        [{"v": "1"}],
        [{"v": True}],
        [{"v": -0.0}],
        [{"v": 0.0}],
    ]
    digests = set()
    for rows in variants:
        snapshot = make_snapshot(client, rows)
        body = client.get(verify_path(snapshot["id"])).json()
        assert body["valid"] is True
        assert body["stored_hash"] == expected_hash(rows)
        digests.add(body["stored_hash"])
    # Every representation — including the two zeroes — gets its own digest.
    assert len(digests) == len(variants)


def test_verify_hashes_non_ascii_text_unescaped(client: TestClient) -> None:
    create_dataset_and_version(client)
    rows = [{"label": "héllo→世界"}]
    snapshot = make_snapshot(client, rows)

    body = client.get(verify_path(snapshot["id"])).json()
    assert body["stored_hash"] == expected_hash(rows)
    # The fingerprint is taken over the raw UTF-8 text; escaping non-ASCII
    # code points would yield a different digest.
    escaped_text = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    escaped_hash = hashlib.sha256(escaped_text.encode("utf-8")).hexdigest()
    assert escaped_hash != body["stored_hash"]


# --------------------------------------------------------------------------- #
# Tampering detection
# --------------------------------------------------------------------------- #


def test_verify_reports_invalid_after_row_modification(client: TestClient) -> None:
    create_dataset_and_version(client)
    rows = [{"id": 1, "label": "a"}, {"id": 2, "label": "b"}]
    snapshot = make_snapshot(client, rows)
    stored_hash = expected_hash(rows)

    replace_stored_rows(snapshot["id"], [{"id": 1, "label": "a"}, {"id": 3, "label": "b"}])

    response = client.get(verify_path(snapshot["id"]))
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["stored_hash"] == stored_hash
    assert body["computed_hash"] == expected_hash(
        [{"id": 1, "label": "a"}, {"id": 3, "label": "b"}]
    )
    assert body["stored_hash"] != body["computed_hash"]


def test_verify_reports_invalid_after_row_addition_and_deletion(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    original = make_snapshot(client, [{"id": 1}])
    original_hash = client.get(verify_path(original["id"])).json()["stored_hash"]

    replace_stored_rows(original["id"], [{"id": 1}, {"id": 2}])
    added = client.get(verify_path(original["id"])).json()
    assert added["valid"] is False
    assert added["stored_hash"] == original_hash
    assert added["computed_hash"] != original_hash

    replace_stored_rows(original["id"], [])
    removed = client.get(verify_path(original["id"])).json()
    assert removed["valid"] is False
    assert removed["row_count"] == 1  # metadata untouched; only rows were tampered
    assert removed["computed_hash"] == expected_hash([])


def test_verify_reports_invalid_whole_row_replacement(client: TestClient) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [{"id": 1, "label": "a"}])

    replace_stored_rows(snapshot["id"], [{"id": 1, "label": "a", "extra": True}])
    body = client.get(verify_path(snapshot["id"])).json()
    assert body["valid"] is False
    assert body["stored_hash"] != body["computed_hash"]


def test_verify_is_unaffected_by_metadata_only_changes(client: TestClient) -> None:
    create_dataset_and_version(client)
    rows = [{"id": 1}]
    snapshot = make_snapshot(client, rows)
    db_path = os.environ["DATA_LINEAGE_DB"]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE snapshots SET created_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", snapshot["id"]),
        )
    body = client.get(verify_path(snapshot["id"])).json()
    # The fingerprint covers the saved row sequence only, never the metadata.
    assert body["valid"] is True
    assert body["stored_hash"] == expected_hash(rows)


def test_verify_never_writes_even_on_mismatch(client: TestClient) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    replace_stored_rows(snapshot["id"], [{"id": 2}])

    first = client.get(verify_path(snapshot["id"]))
    second = client.get(verify_path(snapshot["id"]))
    assert first.text == second.text
    assert first.json()["valid"] is False

    # The tampered rows and snapshot metadata are exactly as left by the
    # direct update — verification neither repairs nor records anything.
    db_path = os.environ["DATA_LINEAGE_DB"]
    with sqlite3.connect(db_path) as conn:
        stored = conn.execute(
            "SELECT rows, row_count, content_hash FROM snapshots WHERE id = ?",
            (snapshot["id"],),
        ).fetchone()
    assert json.loads(stored[0]) == [{"id": 2}]
    assert stored[1] == 1
    assert stored[2] == first.json()["stored_hash"]

    # No privacy-view side effects either.
    access = client.get(
        "/datasets/orders/versions/1/privacy-policies/view/access-records"
    )
    assert access.json() == []


# --------------------------------------------------------------------------- #
# Resolution and request shape
# --------------------------------------------------------------------------- #


def test_verify_unknown_dataset_version_or_snapshot_is_404(client: TestClient) -> None:
    missing_dataset = client.get(verify_path(1, dataset="ghost"))
    assert missing_dataset.status_code == 404
    assert missing_dataset.json()["error"] == "not_found"
    assert set(missing_dataset.json()) == {"error", "detail"}

    create_dataset_and_version(client)
    missing_version = client.get(verify_path(1, version=9))
    assert missing_version.status_code == 404
    assert missing_version.json()["error"] == "not_found"

    make_snapshot(client, [])
    missing_snapshot = client.get(verify_path(999))
    assert missing_snapshot.status_code == 404
    assert missing_snapshot.json()["error"] == "not_found"


def test_verify_snapshot_of_another_version_or_dataset_is_404(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    create_dataset_and_version(client, "other")
    foreign = make_snapshot(client, [{"id": 1}], dataset="other")

    # Same version number, different dataset.
    response = client.get(verify_path(foreign["id"], dataset="orders"))
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    # A second version of orders: the v1 snapshot is invisible under v2.
    extra = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert extra.status_code == 201, extra.text
    own = make_snapshot(client, [{"id": 1}], version=1)
    response = client.get(verify_path(own["id"], version=2))
    assert response.status_code == 404


def test_verify_404_takes_precedence_over_body_and_query_shape(
    client: TestClient,
) -> None:
    # Even a body and bogus query parameters cannot turn a missing resource
    # into a 422.
    with_body = client.request(
        "GET", verify_path(1, dataset="ghost"), content=b"   \n\t "
    )
    assert with_body.status_code == 404
    with_query = client.get(
        verify_path(1, dataset="ghost"), params={"bogus": "1"}
    )
    assert with_query.status_code == 404

    create_dataset_and_version(client)
    make_snapshot(client, [])
    missing_with_everything = client.request(
        "GET",
        verify_path(999),
        params={"bogus": "1"},
        content=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert missing_with_everything.status_code == 404


def test_verify_rejects_any_body_and_query_parameters(client: TestClient) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])

    whitespace = client.request(
        "GET", verify_path(snapshot["id"]), content=b"   \n\t "
    )
    assert whitespace.status_code == 422
    assert whitespace.json()["error"] == "validation_error"
    assert set(whitespace.json()) == {"error", "detail"}

    json_body = client.request(
        "GET",
        verify_path(snapshot["id"]),
        content=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert json_body.status_code == 422
    assert json_body.json()["error"] == "validation_error"

    for params in ({"x": "1"}, {"x": ""}):
        response = client.get(verify_path(snapshot["id"]), params=params)
        assert response.status_code == 422, params
        assert response.json()["error"] == "validation_error"
        assert set(response.json()) == {"error", "detail"}


def test_verify_accepts_only_get(client: TestClient) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)(verify_path(snapshot["id"]))
        assert response.status_code == 405, method


def test_verify_is_strictly_read_only(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_snapshot(client, [{"id": 1}])
    second = make_snapshot(client, [{"id": 1}, {"id": 2}])

    for _ in range(3):
        response = client.get(verify_path(second["id"]))
        assert response.status_code == 200

    base = "/datasets/orders/versions/1/snapshots"
    listed = client.get(base).json()
    assert [s["id"] for s in listed] == [first["id"], second["id"]]
    assert client.get(f"{base}/{first['id']}").json()["rows"] == [{"id": 1}]
    assert client.get(f"{base}/{second['id']}").json()["rows"] == [
        {"id": 1},
        {"id": 2},
    ]
    # Lineage/quality/privacy state plays no role and nothing is appended.
    assert client.get(
        "/datasets/orders/versions/1/privacy-policies/view/access-records"
    ).json() == []


# --------------------------------------------------------------------------- #
# Deletion and persistence
# --------------------------------------------------------------------------- #


def test_verify_returns_404_after_confirmed_snapshot_deletion(
    client: TestClient,
) -> None:
    # Set up the two-stage deletion flow with an immediate-age policy and no
    # downstream lineage, so confirmation removes the snapshot.
    assert client.post("/datasets", json={"name": "raw"}).status_code == 201
    assert client.post(
        "/datasets/raw/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    ).status_code == 201
    assert client.post(
        "/datasets/raw/versions/1/retention-policies",
        json={"retention_days": 0},
    ).status_code == 201
    snapshot = make_snapshot(client, [{"id": 1}], dataset="raw")
    assert client.get(verify_path(snapshot["id"], dataset="raw")).status_code == 200

    request = client.post(
        f"/datasets/raw/versions/1/snapshots/{snapshot['id']}/deletion-requests",
        json={"reason": "no longer needed"},
    )
    assert request.status_code == 201, request.text
    confirm = client.post(
        f"/datasets/raw/versions/1/snapshots/{snapshot['id']}"
        f"/deletion-requests/{request.json()['id']}/confirm"
    )
    assert confirm.status_code == 200, confirm.text

    # The snapshot read and its verification address are both gone; the
    # deletion-request collection remains addressable as before.
    assert (
        client.get(f"/datasets/raw/versions/1/snapshots/{snapshot['id']}").status_code
        == 404
    )
    assert client.get(verify_path(snapshot["id"], dataset="raw")).status_code == 404
    assert (
        client.get(
            f"/datasets/raw/versions/1/snapshots/{snapshot['id']}/deletion-requests"
        ).status_code
        == 200
    )


def test_create_snapshot_response_structure_is_unchanged(client: TestClient) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    assert set(snapshot) == {"id", "dataset", "version", "created_at", "row_count"}
    assert "stored_hash" not in snapshot
    assert "content_hash" not in snapshot

    listed = client.get("/datasets/orders/versions/1/snapshots").json()
    assert set(listed[0]) == {"id", "dataset", "version", "created_at", "row_count"}


CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

assert client.post("/datasets", json={"name": "orders"}).status_code == 201
assert client.post(
    "/datasets/orders/versions",
    json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
).status_code == 201
created = client.post(
    "/datasets/orders/versions/1/snapshots",
    json={"rows": [{"b": 1, "a": "你好"}, {"v": -0.0}, {"v": 1.0}]},
)
assert created.status_code == 201, created.text
print(created.json()["id"])
"""

VERIFY_SCRIPT = """
import hashlib
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
snapshot_id = int(input())
rows = [{"b": 1, "a": "你好"}, {"v": -0.0}, {"v": 1.0}]
canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

response = client.get(
    f"/datasets/orders/versions/1/snapshots/{snapshot_id}/verify"
)
assert response.status_code == 200, response.text
body = response.json()
assert body["valid"] is True
assert body["stored_hash"] == expected == body["computed_hash"]
assert body["row_count"] == 3
assert response.text == (
    json.dumps(body, separators=(",", ":"), ensure_ascii=False) + "\\n"
)
print("verified")
"""


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


def test_verify_fingerprint_is_stable_across_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "verify-lineage.db"
    snapshot_id = _run(db_path, CREATE_SCRIPT)
    output = _run(db_path, VERIFY_SCRIPT, stdin=snapshot_id)
    assert output == "verified"


# --------------------------------------------------------------------------- #
# Upgrade of a database created before fingerprints existed
# --------------------------------------------------------------------------- #


def test_verify_backfits_fingerprints_of_legacy_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "legacy-lineage.db"
    rows = [{"id": 1}, {"id": 2, "label": "a"}]
    # A database with the pre-feature snapshots table (no content_hash column).
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE datasets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE schema_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dataset_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (dataset_id, version)
            );
            CREATE TABLE snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version_id INTEGER NOT NULL,
                row_count INTEGER NOT NULL,
                rows TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO datasets (name, created_at) VALUES (?, ?)",
            ("legacy", "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO schema_versions (dataset_id, version, created_at) "
            "VALUES (1, 1, '2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO snapshots (version_id, row_count, rows, created_at) "
            "VALUES (1, 2, ?, '2026-01-02T00:00:00+00:00')",
            (json.dumps(rows),),
        )

    monkeypatch.setenv("DATA_LINEAGE_DB", str(db_path))
    from app.main import app

    client = TestClient(app)
    result = client.get("/datasets/legacy/versions/1/snapshots/1/verify")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["valid"] is True
    assert body["stored_hash"] == expected_hash(rows) == body["computed_hash"]
