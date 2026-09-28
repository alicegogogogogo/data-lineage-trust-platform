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
    "row_count_total",
    "min_row_count",
    "max_row_count",
    "first_created_at",
    "last_created_at",
]
TOTAL_KEYS = [
    "version_count",
    "snapshot_count",
    "row_count_total",
    "min_row_count",
    "max_row_count",
    "first_created_at",
    "last_created_at",
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
        "row_count_total": 0,
        "min_row_count": None,
        "max_row_count": None,
        "first_created_at": None,
        "last_created_at": None,
    }


# --------------------------------------------------------------------------- #
# Empty state and wire format
# --------------------------------------------------------------------------- #


def test_summary_dataset_without_versions_is_empty_not_an_error(
    client: TestClient,
) -> None:
    make_dataset(client)
    body = get_summary(client)
    assert list(body) == TOP_LEVEL_KEYS
    assert body["dataset"] == "raw"
    assert body["versions"] == []
    assert body["totals"] == {
        "version_count": 0,
        "snapshot_count": 0,
        "row_count_total": 0,
        "min_row_count": None,
        "max_row_count": None,
        "first_created_at": None,
        "last_created_at": None,
    }


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
    add_version(client)  # an empty version keeps the null stats in the text

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

    body = response.json()
    assert list(body) == TOP_LEVEL_KEYS
    assert list(body["totals"]) == TOTAL_KEYS
    first, second = body["versions"]
    assert list(first) == VERSION_KEYS
    assert list(second) == VERSION_KEYS
    assert list(first["snapshots"][0]) == SNAPSHOT_KEYS
    assert list(first["stats"]) == STATS_KEYS
    # The null extreme keys are present in the raw text, never omitted.
    assert '"min_row_count":null' in text
    assert '"last_created_at":null' in text


# --------------------------------------------------------------------------- #
# Versions and snapshot listings
# --------------------------------------------------------------------------- #


def test_summary_versions_sort_ascending_and_include_empty_versions(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    add_version(client)
    make_snapshot(client, version=2)
    add_version(client)

    body = get_summary(client)
    assert [entry["version"] for entry in body["versions"]] == [1, 2, 3]
    assert body["versions"][0]["snapshots"] == []
    assert body["versions"][0]["stats"] == empty_stats()
    assert len(body["versions"][1]["snapshots"]) == 1
    assert body["versions"][2]["snapshots"] == []
    assert body["versions"][2]["stats"] == empty_stats()


def test_summary_lists_snapshots_by_id_with_persisted_rows_and_write_time(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    first = make_snapshot(client, rows=[{"id": 1}])
    second = make_snapshot(client, rows=[{"id": 2}, {"id": 3}])
    third = make_snapshot(client, rows=[])

    listing = get_summary(client)["versions"][0]["snapshots"]
    assert [entry["snapshot_id"] for entry in listing] == [
        first["id"],
        second["id"],
        third["id"],
    ]
    assert [entry["row_count"] for entry in listing] == [1, 2, 0]
    for entry, snapshot in zip(listing, (first, second, third)):
        assert list(entry) == SNAPSHOT_KEYS
        assert entry["snapshot_id"] == snapshot["id"]
        assert entry["row_count"] == snapshot["row_count"]
        assert entry["created_at"] == snapshot["created_at"]


def test_summary_per_version_stats(client: TestClient) -> None:
    make_dataset(client, fields=["id"])
    first = make_snapshot(client, rows=[{"id": v} for v in range(3)])
    second = make_snapshot(client, rows=[{"id": 1}, {"id": 2}])
    third = make_snapshot(client, rows=[])

    stats = get_summary(client)["versions"][0]["stats"]
    assert list(stats) == STATS_KEYS
    assert stats == {
        "snapshot_count": 3,
        "row_count_total": 5,
        "min_row_count": 0,
        "max_row_count": 3,
        "first_created_at": first["created_at"],
        "last_created_at": third["created_at"],
    }


def test_summary_one_snapshot_version_has_equal_min_max_and_times(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    snapshot = make_snapshot(client, rows=[{"id": 1}, {"id": 2}])

    stats = get_summary(client)["versions"][0]["stats"]
    assert stats["snapshot_count"] == 1
    assert stats["row_count_total"] == 2
    assert stats["min_row_count"] == stats["max_row_count"] == 2
    assert stats["first_created_at"] == stats["last_created_at"] == snapshot[
        "created_at"
    ]


def test_summary_same_write_time_tie_breaks_by_snapshot_id_ascending(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    first = make_snapshot(client, rows=[{"id": 1}])
    second = make_snapshot(client, rows=[{"id": 2}])
    third = make_snapshot(client, rows=[{"id": 3}])
    assert first["id"] < second["id"] < third["id"]

    # Rewrite the write times so several snapshots share one instant; the
    # summary must order them by snapshot id, not by database natural order.
    db_file = os.environ["DATA_LINEAGE_DB"]
    same_instant = "2026-01-01T00:00:00+00:00"
    with sqlite3.connect(db_file) as direct:
        direct.execute(
            "UPDATE snapshots SET created_at = ?", (same_instant,)
        )

    version_stats = get_summary(client)["versions"][0]["stats"]
    assert version_stats["first_created_at"] == same_instant
    assert version_stats["last_created_at"] == same_instant

    # The shared instant cannot distinguish ends on the timestamp alone; the
    # earliest/latest snapshot identity is asserted through the listing order.
    listing = get_summary(client)["versions"][0]["snapshots"]
    assert [entry["snapshot_id"] for entry in listing] == [
        first["id"],
        second["id"],
        third["id"],
    ]

    # Give two snapshots one instant and another snapshot a different instant
    # so the tie-break actually decides earliest vs latest inside an instant.
    with sqlite3.connect(db_file) as direct:
        direct.execute(
            "UPDATE snapshots SET created_at = ? WHERE id = ?",
            ("2026-01-02T00:00:00+00:00", second["id"]),
        )
        # first and third still share 2026-01-01; first is the lowest id of
        # that instant and must remain the overall earliest.
    totals = get_summary(client)["totals"]
    assert totals["first_created_at"] == "2026-01-01T00:00:00+00:00"
    assert totals["last_created_at"] == "2026-01-02T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# Confirmed deletions
# --------------------------------------------------------------------------- #


def test_summary_excludes_confirmed_deleted_snapshots_everywhere(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    make_policy(client)
    first = make_snapshot(client, rows=[{"id": 1}])
    gone = make_snapshot(client, rows=[{"id": 2}, {"id": 3}])
    third = make_snapshot(client, rows=[{"id": 4}, {"id": 5}, {"id": 6}, {"id": 7}])
    delete_snapshot(client, gone["id"])

    # The deleted snapshot is no longer addressable as a snapshot.
    assert (
        client.get(f"/datasets/raw/versions/1/snapshots/{gone['id']}").status_code
        == 404
    )

    body = get_summary(client)
    version = body["versions"][0]
    assert [entry["snapshot_id"] for entry in version["snapshots"]] == [
        first["id"],
        third["id"],
    ]
    # Snapshot numbers are not reused: the surviving entries keep their gap.
    assert third["id"] > gone["id"]
    stats = version["stats"]
    assert stats["snapshot_count"] == 2
    assert stats["row_count_total"] == 5
    assert stats["min_row_count"] == 1
    assert stats["max_row_count"] == 4
    assert stats["first_created_at"] == first["created_at"]
    assert stats["last_created_at"] == third["created_at"]
    totals = body["totals"]
    assert totals["snapshot_count"] == 2
    assert totals["row_count_total"] == 5
    assert totals["min_row_count"] == 1
    assert totals["max_row_count"] == 4


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #


def test_summary_totals_are_sums_with_overall_extremes(
    client: TestClient,
) -> None:
    make_dataset(client, fields=["id"])
    # Version 1: snapshots with 1 and 4 rows.
    v1_first = make_snapshot(client, version=1, rows=[{"id": 1}])
    v1_last = make_snapshot(
        client, version=1, rows=[{"id": i} for i in range(4)]
    )
    add_version(client)
    # Version 2: one snapshot with 3 rows, written at an unrelated time.
    v2_only = make_snapshot(
        client, version=2, rows=[{"id": i} for i in range(3)]
    )
    add_version(client)  # version 3 stays empty

    body = get_summary(client)
    versions = body["versions"]
    totals = body["totals"]

    assert totals["version_count"] == len(versions) == 3
    assert totals["snapshot_count"] == sum(
        version["stats"]["snapshot_count"] for version in versions
    )
    assert totals["row_count_total"] == sum(
        version["stats"]["row_count_total"] for version in versions
    )
    assert totals["snapshot_count"] == 3
    assert totals["row_count_total"] == 8
    # Overall extremes across every extant snapshot of the dataset.
    assert totals["min_row_count"] == 1
    assert totals["max_row_count"] == 4
    listing_order = [v1_first, v1_last, v2_only]
    earliest = min(
        listing_order,
        key=lambda snapshot: (snapshot["created_at"], snapshot["id"]),
    )
    latest = max(
        listing_order,
        key=lambda snapshot: (snapshot["created_at"], snapshot["id"]),
    )
    assert totals["first_created_at"] == earliest["created_at"]
    assert totals["last_created_at"] == latest["created_at"]
    assert list(totals) == TOTAL_KEYS


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
            "GET",
            "/datasets/ghost/snapshot-scale-summary",
            content=b" ",
        ).status_code
        == 404
    )


# --------------------------------------------------------------------------- #
# Read-only behavior
# --------------------------------------------------------------------------- #


def test_summary_is_strictly_read_only(client: TestClient) -> None:
    make_dataset(client, fields=["id"])
    make_policy(client)
    first = make_snapshot(client, rows=[{"id": 1}])
    second = make_snapshot(client, rows=[{"id": 2}, {"id": 3}])
    delete_snapshot(client, second["id"])

    snapshots_before = client.get(
        "/datasets/raw/versions/1/snapshots"
    ).json()
    proofs_before = client.get(
        "/datasets/raw/versions/1/snapshots/deletion-proofs"
    ).json()
    cache_audit_before = client.get(
        "/datasets/raw/versions/1/snapshots/cache-audit"
    ).json()
    cache_trail_before = client.get(
        "/datasets/raw/versions/1/snapshots/cache-trail"
    ).json()
    first_text = summary_response(client).text

    for _ in range(3):
        assert summary_response(client).text == first_text

    assert (
        client.get("/datasets/raw/versions/1/snapshots").json()
        == snapshots_before
    )
    assert (
        client.get("/datasets/raw/versions/1/snapshots/deletion-proofs").json()
        == proofs_before
    )
    assert (
        client.get("/datasets/raw/versions/1/snapshots/cache-audit").json()
        == cache_audit_before
    )
    assert (
        client.get("/datasets/raw/versions/1/snapshots/cache-trail").json()
        == cache_trail_before
    )
    assert (
        client.get(f"/datasets/raw/versions/1/snapshots/{first['id']}/verify")
        .json()["valid"]
        is True
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
    "/datasets/raw/versions/1/snapshots",
    json={"rows": [{"id": 1}, {"id": 2}, {"id": 3}]},
).status_code == 201
assert client.post(
    "/datasets/raw/versions/1/snapshots", json={"rows": []},
).status_code == 201
assert client.post(
    "/datasets/raw/versions",
    json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
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
assert [s["snapshot_id"] for s in first["snapshots"]] == [1, 2]
assert [s["row_count"] for s in first["snapshots"]] == [3, 0]
assert first["stats"] == {
    "snapshot_count": 2,
    "row_count_total": 3,
    "min_row_count": 0,
    "max_row_count": 3,
    "first_created_at": first["snapshots"][0]["created_at"],
    "last_created_at": first["snapshots"][1]["created_at"],
}
assert second["snapshots"] == []
assert second["stats"] == {
    "snapshot_count": 0,
    "row_count_total": 0,
    "min_row_count": None,
    "max_row_count": None,
    "first_created_at": None,
    "last_created_at": None,
}
assert body["totals"] == {
    "version_count": 2,
    "snapshot_count": 2,
    "row_count_total": 3,
    "min_row_count": 0,
    "max_row_count": 3,
    "first_created_at": first["snapshots"][0]["created_at"],
    "last_created_at": first["snapshots"][1]["created_at"],
}
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
