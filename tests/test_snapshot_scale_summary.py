"""Tests for the read-only cross-version snapshot scale summary."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SUMMARY_PATH = "/datasets/raw/snapshot-scale-summary"

TOP_LEVEL_KEYS = ["dataset", "versions", "totals"]
VERSION_KEYS = ["version", "snapshots", "stats"]
SNAPSHOT_KEYS = ["snapshot_id", "row_count", "created_at"]
STATS_KEYS = [
    "snapshot_count",
    "total_row_count",
    "min_row_count",
    "max_row_count",
    "first_created_at",
    "last_created_at",
]
TOTAL_KEYS = STATS_KEYS  # totals carries the same six keys


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


def delete_snapshot(
    client: TestClient,
    snapshot_id: int,
    reason: str = "no longer needed",
    dataset: str = "raw",
    version: int = 1,
) -> dict:
    request = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots/"
        f"{snapshot_id}/deletion-requests",
        json={"reason": reason},
    )
    assert request.status_code == 201, request.text
    confirm = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots/"
        f"{snapshot_id}/deletion-requests/{request.json()['id']}/confirm"
    )
    assert confirm.status_code == 200, confirm.text
    return confirm.json()


def get_summary(client: TestClient, dataset: str = "raw") -> dict:
    response = client.get(f"/datasets/{dataset}/snapshot-scale-summary")
    assert response.status_code == 200, response.text
    return response.json()


def summary_response(client: TestClient, dataset: str = "raw"):
    response = client.get(f"/datasets/{dataset}/snapshot-scale-summary")
    assert response.status_code == 200, response.text
    return response


def empty_stats() -> dict:
    return {
        "snapshot_count": 0,
        "total_row_count": 0,
        "min_row_count": None,
        "max_row_count": None,
        "first_created_at": None,
        "last_created_at": None,
    }


# --------------------------------------------------------------------------- #
# Empty state and wire format
# --------------------------------------------------------------------------- #


def test_summary_without_versions_is_empty_not_an_error(client: TestClient) -> None:
    make_dataset(client)
    body = get_summary(client)
    assert set(body) == set(TOP_LEVEL_KEYS)
    assert body["dataset"] == "raw"
    assert body["versions"] == []
    assert body["totals"] == {
        "version_count": 0,
        "snapshot_count": 0,
        "total_row_count": 0,
        "min_row_count": None,
        "max_row_count": None,
        "first_created_at": None,
        "last_created_at": None,
    }
    assert list(body["totals"]) == ["version_count"] + TOTAL_KEYS


def test_summary_is_get_only(client: TestClient) -> None:
    make_dataset(client)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, SUMMARY_PATH)
        assert response.status_code == 405, method


def test_summary_wire_format_is_compact_with_fixed_key_order_and_newline(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    make_snapshot(client, rows=[{"id": 1}, {"id": 2}])

    response = summary_response(client)
    assert response.headers["content-type"].startswith("application/json")
    text = response.text
    # Compact whitespace: no separator spaces, and the only line break is the
    # single trailing newline.
    assert ": " not in text
    assert ", " not in text
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    assert "\n" not in text[:-1]
    # No scientific notation for the integer counters.
    assert "e+" not in text.lower()

    body = response.json()
    assert list(body) == TOP_LEVEL_KEYS
    version_entry = body["versions"][0]
    assert list(version_entry) == VERSION_KEYS
    assert list(version_entry["snapshots"][0]) == SNAPSHOT_KEYS
    assert list(version_entry["stats"]) == STATS_KEYS
    assert list(body["totals"]) == ["version_count"] + TOTAL_KEYS


# --------------------------------------------------------------------------- #
# Versions, snapshots and ordering
# --------------------------------------------------------------------------- #


def test_summary_versions_sort_ascending_and_include_empty_versions(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    make_snapshot(client, rows=[{"id": 1}])
    add_version(client)  # version 2, no snapshots
    add_version(client)  # version 3, one snapshot
    make_snapshot(client, version=3, rows=[{"id": 9}])

    body = get_summary(client)
    assert [entry["version"] for entry in body["versions"]] == [1, 2, 3]
    first, second, third = body["versions"]
    for entry in body["versions"]:
        assert set(entry) == set(VERSION_KEYS)

    assert len(first["snapshots"]) == 1
    assert first["stats"]["snapshot_count"] == 1

    # The version without snapshots has an empty list and the zero/null stats.
    assert second["snapshots"] == []
    assert second["stats"] == empty_stats()

    assert third["stats"]["snapshot_count"] == 1


def test_summary_snapshots_sort_by_id_and_keep_persisted_fields(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    first = make_snapshot(client, rows=[{"id": 1}])
    second = make_snapshot(client, rows=[{"id": 2}, {"id": 3}, {"id": 4}])
    third = make_snapshot(client, rows=[])

    listed = get_summary(client)["versions"][0]["snapshots"]
    assert [entry["snapshot_id"] for entry in listed] == [
        first["id"],
        second["id"],
        third["id"],
    ]
    by_id = {entry["snapshot_id"]: entry for entry in listed}
    assert by_id[first["id"]] == {
        "snapshot_id": first["id"],
        "row_count": 1,
        "created_at": first["created_at"],
    }
    assert by_id[second["id"]]["row_count"] == 3
    assert by_id[third["id"]]["row_count"] == 0
    for entry in listed:
        assert list(entry) == SNAPSHOT_KEYS


def test_summary_snapshot_order_does_not_depend_on_database_order(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    # Insert two snapshots sharing one write time in descending id-growth order
    # is impossible (ids are autoincrement), so seed the rows directly with an
    # explicit id order and a shared timestamp; the summary must still list by
    # snapshot id ascending.
    db_file = os.environ["DATA_LINEAGE_DB"]
    with sqlite3.connect(db_file) as direct:
        row = direct.execute(
            "SELECT id FROM schema_versions WHERE version = 1"
        ).fetchone()
        version_pk = row[0]
        stamp = "2026-01-01T00:00:00+00:00"
        direct.execute(
            "INSERT INTO snapshots (id, version_id, row_count, rows, "
            "content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (11, version_pk, 5, "[]", "x", stamp),
        )
        direct.execute(
            "INSERT INTO snapshots (id, version_id, row_count, rows, "
            "content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (12, version_pk, 2, "[]", "x", stamp),
        )

    entry = get_summary(client)["versions"][0]
    assert [s["snapshot_id"] for s in entry["snapshots"]] == [11, 12]
    stats = entry["stats"]
    assert stats["snapshot_count"] == 2
    assert stats["total_row_count"] == 7
    assert stats["min_row_count"] == 2
    assert stats["max_row_count"] == 5
    # Same instant: the time range is that instant; the snapshot-id tiebreak
    # keeps the ordering independent of the database's natural order.
    assert stats["first_created_at"] == stamp
    assert stats["last_created_at"] == stamp


# --------------------------------------------------------------------------- #
# Per-version statistics
# --------------------------------------------------------------------------- #


def test_summary_stats_count_total_min_max_and_time_range(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    one = make_snapshot(client, rows=[{"id": 1}, {"id": 2}, {"id": 3}])
    two = make_snapshot(client, rows=[{"id": 4}])
    three = make_snapshot(client, rows=[{"id": 5}, {"id": 6}])

    stats = get_summary(client)["versions"][0]["stats"]
    assert list(stats) == STATS_KEYS
    assert stats["snapshot_count"] == 3
    assert stats["total_row_count"] == 6
    assert stats["min_row_count"] == 1
    assert stats["max_row_count"] == 3
    assert stats["first_created_at"] == one["created_at"]
    assert stats["last_created_at"] == three["created_at"]
    # The write times are the snapshots' timezone-bearing values.
    assert stats["first_created_at"].endswith("+00:00")


def test_summary_empty_snapshot_version_keeps_null_keys(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    add_version(client)
    response = summary_response(client)
    stats = response.json()["versions"][1]["stats"]
    assert stats == empty_stats()
    assert '"min_row_count":null' in response.text
    assert '"first_created_at":null' in response.text


# --------------------------------------------------------------------------- #
# Confirmed-deleted snapshots disappear from every statistic
# --------------------------------------------------------------------------- #


def test_summary_excludes_confirmed_deleted_snapshots(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    make_policy(client)
    gone = make_snapshot(client, rows=[{"id": 1}, {"id": 2}])
    kept = make_snapshot(client, rows=[{"id": 3}, {"id": 4}, {"id": 5}])
    delete_snapshot(client, gone["id"])

    entry = get_summary(client)["versions"][0]
    assert [s["snapshot_id"] for s in entry["snapshots"]] == [kept["id"]]
    stats = entry["stats"]
    assert stats["snapshot_count"] == 1
    assert stats["total_row_count"] == 3
    assert stats["min_row_count"] == 3
    assert stats["max_row_count"] == 3
    assert stats["first_created_at"] == kept["created_at"]
    assert stats["last_created_at"] == kept["created_at"]


def test_summary_version_with_only_deleted_snapshots_is_empty(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    make_policy(client)
    snapshot = make_snapshot(client, rows=[{"id": 1}])
    delete_snapshot(client, snapshot["id"])

    entry = get_summary(client)["versions"][0]
    assert entry["snapshots"] == []
    assert entry["stats"] == empty_stats()


# --------------------------------------------------------------------------- #
# Whole-dataset totals
# --------------------------------------------------------------------------- #


def test_summary_totals_sum_versions_and_span_all_snapshots(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    make_snapshot(client, version=1, rows=[{"id": 1}, {"id": 2}])
    make_snapshot(client, version=1, rows=[{"id": 3}, {"id": 4}, {"id": 5}])
    add_version(client)
    make_snapshot(client, version=2, rows=[{"id": 6}])
    add_version(client)  # version 3, no snapshots

    body = get_summary(client)
    versions = body["versions"]
    totals = body["totals"]

    assert totals["version_count"] == len(versions) == 3
    assert totals["snapshot_count"] == sum(
        v["stats"]["snapshot_count"] for v in versions
    )
    assert totals["total_row_count"] == sum(
        v["stats"]["total_row_count"] for v in versions
    )
    assert totals["snapshot_count"] == 3
    assert totals["total_row_count"] == 6
    # Extrema and time range over every extant snapshot of the whole dataset.
    all_counts = [
        s["row_count"] for v in versions for s in v["snapshots"]
    ]
    all_times = [
        s["created_at"] for v in versions for s in v["snapshots"]
    ]
    assert totals["min_row_count"] == min(all_counts) == 1
    assert totals["max_row_count"] == max(all_counts) == 3
    assert totals["first_created_at"] == min(all_times)
    assert totals["last_created_at"] == max(all_times)


def test_summary_totals_are_null_when_no_snapshot_exists(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    add_version(client)
    add_version(client)
    totals = get_summary(client)["totals"]
    assert totals == {
        "version_count": 3,
        "snapshot_count": 0,
        "total_row_count": 0,
        "min_row_count": None,
        "max_row_count": None,
        "first_created_at": None,
        "last_created_at": None,
    }


# --------------------------------------------------------------------------- #
# Errors: 404 / 422, no writes
# --------------------------------------------------------------------------- #


def test_summary_unknown_dataset_returns_404(client: TestClient) -> None:
    response = client.get("/datasets/ghost/snapshot-scale-summary")
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "not_found"


def test_summary_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset(client)
    before = summary_response(client).text

    with_body = client.request("GET", SUMMARY_PATH, content=b"{}")
    whitespace_body = client.request("GET", SUMMARY_PATH, content=b"   ")
    single_space_body = client.request("GET", SUMMARY_PATH, content=b" ")
    with_query = client.get(SUMMARY_PATH, params={"limit": 1})
    assert with_body.status_code == 422
    assert whitespace_body.status_code == 422
    assert single_space_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, whitespace_body, single_space_body, with_query):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]

    # The rejections wrote nothing.
    assert summary_response(client).text == before


def test_summary_shape_errors_keep_404_precedence(client: TestClient) -> None:
    make_dataset(client)
    assert client.get(
        "/datasets/ghost/snapshot-scale-summary", params={"x": "1"}
    ).status_code == 404
    assert (
        client.request(
            "GET", "/datasets/ghost/snapshot-scale-summary", content=b"{}"
        ).status_code
        == 404
    )
    assert (
        client.request(
            "GET", "/datasets/ghost/snapshot-scale-summary", content=b" "
        ).status_code
        == 404
    )


def test_summary_is_strictly_read_only(client: TestClient) -> None:
    make_dataset(client, fields=["id"])
    make_policy(client)
    snapshot = make_snapshot(client, rows=[{"id": 1}, {"id": 2}])
    delete_snapshot(client, snapshot["id"])

    snapshots_before = client.get(
        "/datasets/raw/versions/1/snapshots"
    ).json()
    verify_before = client.get(
        f"/datasets/raw/versions/1/snapshots/deletion-proofs/verify"
    ).json()
    proofs_before = client.get(
        "/datasets/raw/versions/1/snapshots/deletion-proofs"
    ).json()
    first_text = summary_response(client).text

    for _ in range(3):
        response = summary_response(client)
        assert response.text == first_text
    assert (
        client.get("/datasets/raw/versions/1/snapshots").json()
        == snapshots_before
    )
    assert (
        client.get(
            "/datasets/raw/versions/1/snapshots/deletion-proofs/verify"
        ).json()
        == verify_before
    )
    assert (
        client.get(
            "/datasets/raw/versions/1/snapshots/deletion-proofs"
        ).json()
        == proofs_before
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
for value in (1, 2, 3):
    response = client.post(
        "/datasets/raw/versions/1/snapshots",
        json={"rows": [{"id": value}]},
    )
    assert response.status_code == 201, response.text
assert client.post(
    "/datasets/raw/versions",
    json={"fields": [{"name": "id", "type": "integer", "nullable": True}]},
).status_code == 201
print("created")
"""

_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

response = client.get("/datasets/raw/snapshot-scale-summary")
assert response.status_code == 200, response.text
assert response.text.endswith("\\n")
body = response.json()
assert list(body) == ["dataset", "versions", "totals"]
assert body["dataset"] == "raw"
assert [v["version"] for v in body["versions"]] == [1, 2]
first, second = body["versions"]
assert list(first) == ["version", "snapshots", "stats"]
assert len(first["snapshots"]) == 3
assert [s["snapshot_id"] for s in first["snapshots"]] == [1, 2, 3]
assert all(list(s) == ["snapshot_id", "row_count", "created_at"]
           for s in first["snapshots"])
assert first["stats"] == {
    "snapshot_count": 3,
    "total_row_count": 3,
    "min_row_count": 1,
    "max_row_count": 1,
    "first_created_at": first["snapshots"][0]["created_at"],
    "last_created_at": first["snapshots"][-1]["created_at"],
}
assert second["snapshots"] == []
assert second["stats"]["snapshot_count"] == 0
assert second["stats"]["min_row_count"] is None
assert body["totals"]["version_count"] == 2
assert body["totals"]["snapshot_count"] == 3
assert body["totals"]["total_row_count"] == 3
again = client.get("/datasets/raw/snapshot-scale-summary")
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


def test_summary_survives_a_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot-scale-summary.db"
    assert _run_script(db_path, _CREATE_SCRIPT) == "created"
    assert _run_script(db_path, _VERIFY_SCRIPT) == "verified"
