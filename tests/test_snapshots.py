"""Tests for persistent row snapshots, time lookup and multiset diffs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def create_dataset_and_version(
    client: TestClient, dataset: str = "orders", version: int | None = None
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
    if version is not None:
        for _ in range(1, version):
            extra = client.post(
                f"/datasets/{dataset}/versions",
                json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
            )
            assert extra.status_code == 201, extra.text


def make_snapshot(client: TestClient, rows: list, dataset: str = "orders", version: int = 1):
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots",
        json={"rows": rows},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Creation and retrieval
# --------------------------------------------------------------------------- #


def test_create_snapshot_returns_metadata(client: TestClient) -> None:
    create_dataset_and_version(client)
    body = make_snapshot(client, [{"id": 1, "label": "a"}, {"id": 2}])

    assert set(body) == {"id", "dataset", "version", "created_at", "row_count"}
    assert isinstance(body["id"], int)
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["row_count"] == 2
    datetime.fromisoformat(body["created_at"])


def test_create_empty_snapshot(client: TestClient) -> None:
    create_dataset_and_version(client)
    body = make_snapshot(client, [])
    assert body["row_count"] == 0

    read = client.get(f"/datasets/orders/versions/1/snapshots/{body['id']}")
    assert read.status_code == 200
    assert read.json()["rows"] == []


def test_snapshot_perserves_values_nesting_and_key_order(client: TestClient) -> None:
    create_dataset_and_version(client)
    rows = [
        {"zeta": [1, 2, {"deep": True, "alpha": None}], "mid": "x", "a": 1.5},
        {"label": "b", "id": 7},
    ]
    created = make_snapshot(client, rows)

    read = client.get(f"/datasets/orders/versions/1/snapshots/{created['id']}")
    assert read.status_code == 200, read.text
    body = read.json()
    assert set(body) == {
        "id", "dataset", "version", "created_at", "row_count", "rows"
    }
    assert body["rows"] == rows
    # Object key order is stored and returned verbatim; array order is kept.
    assert list(body["rows"][0]) == ["zeta", "mid", "a"]
    assert list(body["rows"][1]) == ["label", "id"]
    assert body["rows"][0]["zeta"][2] == {"deep": True, "alpha": None}


def test_rows_must_be_an_array_of_objects(client: TestClient) -> None:
    create_dataset_and_version(client)
    path = "/datasets/orders/versions/1/snapshots"
    for payload in (
        {"rows": {}},
        {"rows": "not-a-list"},
        {"rows": [1, 2, 3]},
        {"rows": ["x"]},
        {"rows": [None]},
        {"rows": [[]]},
        {"rows": [{"id": 1}, "not-an-object"]},
    ):
        response = client.post(path, json=payload)
        assert response.status_code == 422, payload
        assert response.json()["error"] == "validation_error"
    # Nothing was written.
    assert client.get(path).json() == []


def test_missing_rows_key_is_rejected(client: TestClient) -> None:
    create_dataset_and_version(client)
    response = client.post(
        "/datasets/orders/versions/1/snapshots", json={}
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert "rows" in response.json()["detail"]


def test_malformed_json_body_is_rejected(client: TestClient) -> None:
    create_dataset_and_version(client)
    response = client.post(
        "/datasets/orders/versions/1/snapshots",
        content="{not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_create_snapshot_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    assert client.post(
        "/datasets/ghost/versions/1/snapshots", json={"rows": []}
    ).status_code == 404

    create_dataset_and_version(client)
    response = client.post(
        "/datasets/orders/versions/9/snapshots", json={"rows": []}
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_list_snapshots_lists_metadata_only_sorted_by_id(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_snapshot(client, [{"id": 1}])
    second = make_snapshot(client, [{"id": 2}, {"id": 3}])
    third = make_snapshot(client, [])

    response = client.get("/datasets/orders/versions/1/snapshots")
    assert response.status_code == 200
    listed = response.json()
    assert [item["id"] for item in listed] == [first["id"], second["id"], third["id"]]
    assert [item["row_count"] for item in listed] == [1, 2, 0]
    for item in listed:
        assert set(item) == {"id", "dataset", "version", "created_at", "row_count"}
        assert "rows" not in item


def test_get_snapshot_returns_rows(client: TestClient) -> None:
    create_dataset_and_version(client)
    created = make_snapshot(client, [{"id": 9, "label": "kept"}])

    response = client.get(f"/datasets/orders/versions/1/snapshots/{created['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["id"]
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["created_at"] == created["created_at"]
    assert body["row_count"] == 1
    assert body["rows"] == [{"id": 9, "label": "kept"}]


def test_get_unknown_snapshot_is_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_snapshot(client, [])

    missing = client.get("/datasets/orders/versions/1/snapshots/999")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
    # A snapshot id from a different dataset/version is also invisible here.
    create_dataset_and_version(client, "other")
    foreign = make_snapshot(client, [], dataset="other")
    scoped = client.get(
        f"/datasets/orders/versions/1/snapshots/{foreign['id']}"
    )
    assert scoped.status_code == 404


def test_list_snapshots_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    assert client.get("/datasets/ghost/versions/1/snapshots").status_code == 404
    create_dataset_and_version(client)
    assert client.get("/datasets/orders/versions/3/snapshots").status_code == 404


# --------------------------------------------------------------------------- #
# Time lookup
# --------------------------------------------------------------------------- #


def _between(earlier_iso: str, later_iso: str) -> str:
    earlier = datetime.fromisoformat(earlier_iso)
    later = datetime.fromisoformat(later_iso)
    return (earlier + (later - earlier) / 2).isoformat()


def test_get_snapshot_at_returns_latest_not_later_than_timestamp(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    first = make_snapshot(client, [{"id": 1}])
    # Ensure the second snapshot gets a strictly later created_at timestamp.
    time.sleep(0.01)
    second = make_snapshot(client, [{"id": 1}, {"id": 2}])

    base = "/datasets/orders/versions/1/snapshots/at"

    # Boundary: created_at <= timestamp, so the exact first instant returns it.
    at_first = client.get(base, params={"timestamp": first["created_at"]})
    assert at_first.status_code == 200
    assert at_first.json()["id"] == first["id"]
    assert at_first.json()["rows"] == [{"id": 1}]

    # Any instant strictly between the two returns the first.
    middle = _between(first["created_at"], second["created_at"])
    between = client.get(base, params={"timestamp": middle})
    assert between.status_code == 200
    assert between.json()["id"] == first["id"]

    # The exact second instant and any later instant return the second.
    at_second = client.get(base, params={"timestamp": second["created_at"]})
    assert at_second.status_code == 200
    assert at_second.json()["id"] == second["id"]

    future = (
        datetime.fromisoformat(second["created_at"]) + timedelta(days=1)
    ).isoformat()
    latest = client.get(base, params={"timestamp": future})
    assert latest.status_code == 200
    assert latest.json()["id"] == second["id"]


def test_get_snapshot_at_accepts_other_offsets_and_z(client: TestClient) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    stored = datetime.fromisoformat(snapshot["created_at"])

    # The same absolute instant expressed with a positive numeric offset must
    # still be compared chronologically (not by raw string).
    future = stored + timedelta(hours=1)
    plus_five = future.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert plus_five.utcoffset() == timedelta(hours=5, minutes=30)
    response = client.get(
        "/datasets/orders/versions/1/snapshots/at",
        params={"timestamp": plus_five.isoformat()},
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == snapshot["id"]

    # The trailing 'Z' designates UTC.
    z_value = future.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    z_response = client.get(
        "/datasets/orders/versions/1/snapshots/at",
        params={"timestamp": z_value},
    )
    assert z_response.status_code == 200, z_response.text
    assert z_response.json()["id"] == snapshot["id"]


def test_get_snapshot_at_before_any_snapshot_is_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    before = (
        datetime.fromisoformat(snapshot["created_at"]) - timedelta(seconds=1)
    ).isoformat()
    response = client.get(
        "/datasets/orders/versions/1/snapshots/at",
        params={"timestamp": before},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_get_snapshot_at_without_snapshots_is_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    response = client.get(
        "/datasets/orders/versions/1/snapshots/at",
        params={"timestamp": "2030-01-01T00:00:00+00:00"},
    )
    assert response.status_code == 404


def test_get_snapshot_at_requires_timestamp(client: TestClient) -> None:
    create_dataset_and_version(client)
    base = "/datasets/orders/versions/1/snapshots/at"
    missing = client.get(base)
    assert missing.status_code == 422
    assert missing.json()["error"] == "validation_error"

    empty = client.get(base, params={"timestamp": ""})
    assert empty.status_code == 422
    assert empty.json()["error"] == "validation_error"


def test_get_snapshot_at_rejects_invalid_or_naive_timestamps(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    base = "/datasets/orders/versions/1/snapshots/at"
    for raw in (
        "not-a-timestamp",
        "2026-13-99T00:00:00+00:00",
        # Date-only and naive date-times lack a timezone.
        "2026-01-01",
        "2026-01-01T00:00:00",
        "2026-01-01T00:00:00.000000",
    ):
        response = client.get(base, params={"timestamp": raw})
        assert response.status_code == 422, raw
        assert response.json()["error"] == "validation_error"


def test_get_snapshot_at_unknown_dataset_or_version_is_404(
    client: TestClient,
) -> None:
    params = {"timestamp": "2030-01-01T00:00:00+00:00"}
    assert client.get(
        "/datasets/ghost/versions/1/snapshots/at", params=params
    ).status_code == 404
    create_dataset_and_version(client)
    assert client.get(
        "/datasets/orders/versions/5/snapshots/at", params=params
    ).status_code == 404


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #


def diff(client: TestClient, from_id: int, to_id: int, dataset: str = "orders", version: int = 1):
    return client.get(
        f"/datasets/{dataset}/versions/{version}/snapshots/{from_id}/diff/{to_id}"
    )


def test_diff_reports_added_and_removed(client: TestClient) -> None:
    create_dataset_and_version(client)
    before = make_snapshot(client, [
        {"id": 1, "label": "a"},
        {"id": 2, "label": "b"},
    ])
    after = make_snapshot(client, [
        {"id": 2, "label": "b"},
        {"id": 3, "label": "c"},
    ])

    response = diff(client, before["id"], after["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"from_snapshot_id", "to_snapshot_id", "added", "removed"}
    assert body["from_snapshot_id"] == before["id"]
    assert body["to_snapshot_id"] == after["id"]
    assert body["added"] == [{"row": {"id": 3, "label": "c"}, "count": 1}]
    assert body["removed"] == [{"row": {"id": 1, "label": "a"}, "count": 1}]


def test_diff_ignores_object_key_order(client: TestClient) -> None:
    create_dataset_and_version(client)
    before = make_snapshot(client, [{"b": 2, "a": 1}])
    after = make_snapshot(client, [{"a": 1, "b": 2}])

    body = diff(client, before["id"], after["id"]).json()
    assert body["added"] == []
    assert body["removed"] == []


def test_diff_distinguishes_array_order(client: TestClient) -> None:
    create_dataset_and_version(client)
    before = make_snapshot(client, [{"v": [1, 2]}])
    after = make_snapshot(client, [{"v": [2, 1]}])

    body = diff(client, before["id"], after["id"]).json()
    assert body["added"] == [{"row": {"v": [2, 1]}, "count": 1}]
    assert body["removed"] == [{"row": {"v": [1, 2]}, "count": 1}]


def test_diff_distinguishes_value_types(client: TestClient) -> None:
    create_dataset_and_version(client)
    before = make_snapshot(client, [
        {"v": 1}, {"v": True}, {"v": None},
    ])
    after = make_snapshot(client, [
        {"v": "1"}, {"v": 1.0}, {"v": None},
    ])

    body = diff(client, before["id"], after["id"]).json()
    added_rows = [entry["row"] for entry in body["added"]]
    removed_rows = [entry["row"] for entry in body["removed"]]
    # Canonical text order: '"' (0x22) sorts before '1' (0x31), so the quoted
    # string precedes the float.
    assert added_rows == [{"v": "1"}, {"v": 1.0}]
    assert removed_rows == [{"v": 1}, {"v": True}]
    for entry in body["added"] + body["removed"]:
        assert entry["count"] == 1
        assert set(entry) == {"row", "count"}


def test_diff_counts_duplicates_as_a_multiset(client: TestClient) -> None:
    create_dataset_and_version(client)
    shared = {"id": 1}
    before = make_snapshot(client, [shared, shared, shared, {"id": 2}])
    after = make_snapshot(client, [shared, {"id": 2}, {"id": 2}, {"id": 3}])

    body = diff(client, before["id"], after["id"]).json()
    assert body["added"] == [
        {"row": {"id": 2}, "count": 1},
        {"row": {"id": 3}, "count": 1},
    ]
    assert body["removed"] == [{"row": {"id": 1}, "count": 2}]


def test_diff_entries_sorted_by_canonical_json_text(client: TestClient) -> None:
    create_dataset_and_version(client)
    before = make_snapshot(client, [{"k": "z"}, {"k": 1}])
    after = make_snapshot(client, [{"k": "a"}, {"k": 1}, {"k": 9}, {"k": "Z"}])

    body = diff(client, before["id"], after["id"]).json()
    # Canonical text ordering: JSON strings quote values and digits/letters
    # sort by Unicode code point; the implementation order is asserted by the
    # canonical serialization the server uses.
    added_keys = [
        json.dumps(entry["row"], sort_keys=True, separators=(",", ":"))
        for entry in body["added"]
    ]
    assert added_keys == sorted(added_keys)
    assert len(body["added"]) == 3
    removed_keys = [
        json.dumps(entry["row"], sort_keys=True, separators=(",", ":"))
        for entry in body["removed"]
    ]
    assert removed_keys == sorted(removed_keys)


def test_diff_is_directional_and_supports_equal_snapshots(client: TestClient) -> None:
    create_dataset_and_version(client)
    before = make_snapshot(client, [{"id": 1}])
    after = make_snapshot(client, [{"id": 2}])

    reverse = diff(client, after["id"], before["id"]).json()
    assert reverse["added"] == [{"row": {"id": 1}, "count": 1}]
    assert reverse["removed"] == [{"row": {"id": 2}, "count": 1}]

    same = diff(client, before["id"], before["id"]).json()
    assert same["added"] == []
    assert same["removed"] == []
    assert same["from_snapshot_id"] == same["to_snapshot_id"] == before["id"]


def test_diff_rejects_snapshots_from_another_version_or_dataset(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    create_dataset_and_version(client, "customers")
    # A second version of orders.
    response = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201

    orders_v1 = make_snapshot(client, [{"id": 1}])
    orders_v2 = make_snapshot(client, [{"id": 1}], version=2)
    customers_v1 = make_snapshot(
        client, [{"id": 1}], dataset="customers"
    )

    cross_version = diff(client, orders_v1["id"], orders_v2["id"])
    assert cross_version.status_code == 422
    assert cross_version.json()["error"] == "validation_error"

    cross_dataset = diff(client, orders_v1["id"], customers_v1["id"])
    assert cross_dataset.status_code == 422
    assert cross_dataset.json()["error"] == "validation_error"

    # Reached through the foreign version's own path it is still 422: the
    # snapshots must belong to the dataset/version named in the path.
    foreign_path = client.get(
        f"/datasets/orders/versions/2/snapshots/"
        f"{orders_v2['id']}/diff/{orders_v1['id']}"
    )
    assert foreign_path.status_code == 422


def test_diff_unknown_snapshots_are_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    known = make_snapshot(client, [{"id": 1}])

    missing_from = diff(client, 999, known["id"])
    assert missing_from.status_code == 404
    assert missing_from.json()["error"] == "not_found"

    missing_to = diff(client, known["id"], 1000)
    assert missing_to.status_code == 404
    assert missing_to.json()["error"] == "not_found"


def test_diff_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [])
    assert client.get(
        f"/datasets/ghost/versions/1/snapshots/{snapshot['id']}/diff/{snapshot['id']}"
    ).status_code == 404
    assert client.get(
        f"/datasets/orders/versions/9/snapshots/{snapshot['id']}/diff/{snapshot['id']}"
    ).status_code == 404


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response):
    assert response.status_code in (200, 201), response.text

ok(client.post("/datasets", json={"name": "orders"}))
ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
))
first = client.post(
    "/datasets/orders/versions/1/snapshots",
    json={"rows": [{"id": 1}, {"id": 1}, {"id": 2}]},
)
assert first.status_code == 201, first.text
second = client.post(
    "/datasets/orders/versions/1/snapshots",
    json={"rows": [{"id": 2}, {"id": 3}]},
)
assert second.status_code == 201, second.text
print(first.json()["id"], second.json()["id"])
"""

VERIFY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
ids = json.loads(input())
first_id, second_id = ids

listed = client.get("/datasets/orders/versions/1/snapshots")
assert listed.status_code == 200, listed.text
assert [s["id"] for s in listed.json()] == [first_id, second_id]
assert [s["row_count"] for s in listed.json()] == [3, 2]
assert all("rows" not in s for s in listed.json())

first = client.get(f"/datasets/orders/versions/1/snapshots/{first_id}")
assert first.status_code == 200, first.text
assert first.json()["rows"] == [{"id": 1}, {"id": 1}, {"id": 2}]

future = "2099-01-01T00:00:00+00:00"
at = client.get("/datasets/orders/versions/1/snapshots/at", params={"timestamp": future})
assert at.status_code == 200, at.text
assert at.json()["id"] == second_id
assert at.json()["rows"] == [{"id": 2}, {"id": 3}]

difference = client.get(
    f"/datasets/orders/versions/1/snapshots/{first_id}/diff/{second_id}"
)
assert difference.status_code == 200, difference.text
assert difference.json() == {
    "from_snapshot_id": first_id,
    "to_snapshot_id": second_id,
    "added": [{"row": {"id": 3}, "count": 1}],
    "removed": [{"row": {"id": 1}, "count": 2}],
}
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


def test_snapshots_survive_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshots-lineage.db"

    ids = _run(db_path, CREATE_SCRIPT)
    [first_id, second_id] = ids.split()
    output = _run(db_path, VERIFY_SCRIPT, stdin=json.dumps([int(first_id), int(second_id)]))
    assert output == "verified"
