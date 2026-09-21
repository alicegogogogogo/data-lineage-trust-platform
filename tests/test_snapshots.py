"""Tests for persistent row snapshots, time reads and multiset diffs."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient


def make_dataset_version(client: TestClient, dataset: str = "orders") -> None:
    response = client.post("/datasets", json={"name": dataset})
    assert response.status_code == 201
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201


def create_snapshot(client: TestClient, rows: list, dataset: str = "orders",
                    version: int = 1) -> dict:
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
    make_dataset_version(client)
    body = create_snapshot(client, [{"id": 1}, {"id": 2, "extra": ["x", 1, True, None]}])

    assert set(body) == {"id", "dataset", "version", "created_at", "row_count"}
    assert isinstance(body["id"], int)
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["row_count"] == 2
    datetime.fromisoformat(body["created_at"])


def test_empty_snapshot_is_allowed(client: TestClient) -> None:
    make_dataset_version(client)
    body = create_snapshot(client, [])
    assert body["row_count"] == 0

    response = client.get(
        "/datasets/orders/versions/1/snapshots/" + str(body["id"])
    )
    assert response.status_code == 200
    assert response.json()["rows"] == []


def test_snapshot_perserves_values_and_row_order(client: TestClient) -> None:
    make_dataset_version(client)
    rows = [
        {"b": [3, 2, 1], "a": {"z": 1, "y": 2}},
        {"id": "v2", "nested": {"k": [True, False, None, 1.5]}},
    ]
    body = create_snapshot(client, rows)

    response = client.get(
        f"/datasets/orders/versions/1/snapshots/{body['id']}"
    )
    assert response.status_code == 200
    saved = response.json()
    assert set(saved) == {
        "id", "dataset", "version", "created_at", "row_count", "rows"
    }
    assert saved["rows"] == rows
    assert saved["row_count"] == 2


def test_list_snapshots_is_metadata_only_and_id_sorted(client: TestClient) -> None:
    make_dataset_version(client)
    first = create_snapshot(client, [{"id": 1}])
    second = create_snapshot(client, [{"id": 2}, {"id": 3}])
    third = create_snapshot(client, [])

    response = client.get("/datasets/orders/versions/1/snapshots")
    assert response.status_code == 200
    snapshots = response.json()
    assert [s["id"] for s in snapshots] == [first["id"], second["id"], third["id"]]
    for snapshot in snapshots:
        assert set(snapshot) == {"id", "dataset", "version", "created_at", "row_count"}
        assert "rows" not in snapshot
    assert [s["row_count"] for s in snapshots] == [1, 2, 0]


def test_get_snapshot_returns_saved_rows(client: TestClient) -> None:
    make_dataset_version(client)
    body = create_snapshot(client, [{"id": 1}, {"id": 2}])

    response = client.get(
        f"/datasets/orders/versions/1/snapshots/{body['id']}"
    )
    assert response.status_code == 200
    detail = response.json()
    assert detail["id"] == body["id"]
    assert detail["rows"] == [{"id": 1}, {"id": 2}]


def test_create_snapshot_for_unknown_dataset_or_version_returns_404(
    client: TestClient,
) -> None:
    make_dataset_version(client)
    response = client.post(
        "/datasets/ghost/versions/1/snapshots", json={"rows": []}
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    response = client.post(
        "/datasets/orders/versions/99/snapshots", json={"rows": []}
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_get_unknown_dataset_version_snapshot_returns_404(client: TestClient) -> None:
    make_dataset_version(client)
    body = create_snapshot(client, [])

    assert client.get("/datasets/ghost/versions/1/snapshots").status_code == 404
    assert client.get(
        "/datasets/orders/versions/99/snapshots"
    ).status_code == 404
    assert client.get(
        f"/datasets/orders/versions/1/snapshots/{body['id'] + 1000}"
    ).status_code == 404


def test_rows_must_be_an_array_of_objects(client: TestClient) -> None:
    make_dataset_version(client)

    missing = client.post(
        "/datasets/orders/versions/1/snapshots", json={}
    )
    assert missing.status_code == 422
    assert missing.json()["error"] == "validation_error"

    not_array = client.post(
        "/datasets/orders/versions/1/snapshots", json={"rows": {"id": 1}}
    )
    assert not_array.status_code == 422

    scalar_elements = client.post(
        "/datasets/orders/versions/1/snapshots",
        json={"rows": [1, "x", True, None, [1]]},
    )
    assert scalar_elements.status_code == 422

    # Nothing was written.
    assert client.get(
        "/datasets/orders/versions/1/snapshots"
    ).json() == []


# --------------------------------------------------------------------------- #
# Time-based reads
# --------------------------------------------------------------------------- #


def test_get_snapshot_at_returns_latest_not_after_timestamp(
    client: TestClient,
) -> None:
    make_dataset_version(client)
    first = create_snapshot(client, [{"id": 1}])
    time.sleep(0.02)
    second = create_snapshot(client, [{"id": 2}])
    time.sleep(0.02)
    third = create_snapshot(client, [{"id": 3}])

    t1 = datetime.fromisoformat(first["created_at"])
    t2 = datetime.fromisoformat(second["created_at"])
    t3 = datetime.fromisoformat(third["created_at"])

    def at(moment: datetime) -> dict:
        response = client.get(
            "/datasets/orders/versions/1/snapshots/at",
            params={"timestamp": moment.isoformat()},
        )
        assert response.status_code == 200, response.text
        return response.json()

    # Boundary: created_at <= timestamp, so exactly t1 yields the first one.
    assert at(t1)["id"] == first["id"]
    assert at(t1 + timedelta(microseconds=1))["id"] == first["id"]
    assert at(t2)["id"] == second["id"]
    assert at(t3)["id"] == third["id"]
    assert at(t3 + timedelta(days=1))["id"] == third["id"]
    assert at(t3)["rows"] == [{"id": 3}]


def test_get_snapshot_at_honors_timezone_offsets(client: TestClient) -> None:
    make_dataset_version(client)
    snapshot = create_snapshot(client, [{"id": 1}])
    created = datetime.fromisoformat(snapshot["created_at"])

    # 'Z' suffix and positive offsets must parse as the same instant.
    zulu = created.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    response = client.get(
        "/datasets/orders/versions/1/snapshots/at",
        params={"timestamp": zulu},
    )
    assert response.status_code == 200
    assert response.json()["id"] == snapshot["id"]

    shifted = (created + timedelta(hours=5, minutes=30)).astimezone(
        timezone(timedelta(hours=5, minutes=30))
    )
    response = client.get(
        "/datasets/orders/versions/1/snapshots/at",
        params={"timestamp": shifted.isoformat()},
    )
    assert response.status_code == 200
    assert response.json()["id"] == snapshot["id"]


def test_get_snapshot_at_before_first_snapshot_returns_404(
    client: TestClient,
) -> None:
    make_dataset_version(client)
    snapshot = create_snapshot(client, [{"id": 1}])
    created = datetime.fromisoformat(snapshot["created_at"])

    response = client.get(
        "/datasets/orders/versions/1/snapshots/at",
        params={"timestamp": (created - timedelta(seconds=1)).isoformat()},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_get_snapshot_at_without_any_snapshots_returns_404(client: TestClient) -> None:
    make_dataset_version(client)
    response = client.get(
        "/datasets/orders/versions/1/snapshots/at",
        params={"timestamp": "2030-01-01T00:00:00+00:00"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_get_snapshot_at_rejects_missing_invalid_or_naive_timestamp(
    client: TestClient,
) -> None:
    make_dataset_version(client)
    create_snapshot(client, [{"id": 1}])
    base = "/datasets/orders/versions/1/snapshots/at"

    missing = client.get(base)
    assert missing.status_code == 422
    assert missing.json()["error"] == "validation_error"

    empty = client.get(base, params={"timestamp": ""})
    assert empty.status_code == 422
    assert empty.json()["error"] == "validation_error"

    invalid = client.get(base, params={"timestamp": "not-a-timestamp"})
    assert invalid.status_code == 422
    assert invalid.json()["error"] == "validation_error"

    naive = client.get(base, params={"timestamp": "2030-01-01T10:00:00"})
    assert naive.status_code == 422
    assert naive.json()["error"] == "validation_error"

    date_only = client.get(base, params={"timestamp": "2030-01-01"})
    assert date_only.status_code == 422


def test_get_snapshot_at_unknown_dataset_or_version_returns_404(
    client: TestClient,
) -> None:
    make_dataset_version(client)
    params = {"timestamp": "2030-01-01T00:00:00+00:00"}
    assert client.get(
        "/datasets/ghost/versions/1/snapshots/at", params=params
    ).status_code == 404
    assert client.get(
        "/datasets/orders/versions/99/snapshots/at", params=params
    ).status_code == 404


# --------------------------------------------------------------------------- #
# Diffs
# --------------------------------------------------------------------------- #


def test_diff_reports_added_and_removed_as_multiset(client: TestClient) -> None:
    make_dataset_version(client)
    before = create_snapshot(
        client,
        [
            {"id": 1},
            {"id": 1},
            {"id": 2, "tag": "shared"},
            {"only": "before"},
        ],
    )
    time.sleep(0.02)
    after = create_snapshot(
        client,
        [
            {"id": 1},
            {"id": 2, "tag": "shared"},
            {"id": 2, "tag": "shared"},
            {"id": 3},
        ],
    )

    response = client.get(
        f"/datasets/orders/versions/1/snapshots/{before['id']}"
        f"/diff/{after['id']}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"from_snapshot_id", "to_snapshot_id", "added", "removed"}
    assert body["from_snapshot_id"] == before["id"]
    assert body["to_snapshot_id"] == after["id"]
    assert body["added"] == [
        {"row": {"id": 2, "tag": "shared"}, "count": 1},
        {"row": {"id": 3}, "count": 1},
    ]
    assert body["removed"] == [
        {"row": {"id": 1}, "count": 1},
        {"row": {"only": "before"}, "count": 1},
    ]


def test_diff_ignores_object_key_order_but_distinguishes_arrays_and_types(
    client: TestClient,
) -> None:
    make_dataset_version(client)
    before = create_snapshot(
        client,
        [
            {"a": 1, "b": 2},
            {"arr": [1, 2, 3]},
            {"v": 1},
            {"v": True},
            {"v": "1"},
        ],
    )
    time.sleep(0.02)
    after = create_snapshot(
        client,
        [
            {"b": 2, "a": 1},            # equal: key order ignored
            {"arr": [3, 2, 1]},          # different: array order matters
            {"v": 1},                    # unchanged
            {"v": True},                 # unchanged (bool distinct from int)
            {"v": "1"},                  # unchanged (string distinct)
            {"new": {"x": [1], "y": {"z": 0}}},
        ],
    )

    response = client.get(
        f"/datasets/orders/versions/1/snapshots/{before['id']}"
        f"/diff/{after['id']}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["removed"] == [{"row": {"arr": [1, 2, 3]}, "count": 1}]
    assert body["added"] == [
        {"row": {"arr": [3, 2, 1]}, "count": 1},
        {"row": {"new": {"x": [1], "y": {"z": 0}}}, "count": 1},
    ]


def test_diff_entries_are_sorted_by_canonical_json_text(client: TestClient) -> None:
    make_dataset_version(client)
    before = create_snapshot(client, [])
    time.sleep(0.02)
    after = create_snapshot(
        client,
        [
            {"z": 9},
            {"a": 1},
            {"m": [1, 2]},
            {"a": 1},
            {"nested": {"b": 1, "a": 2}},
        ],
    )

    response = client.get(
        f"/datasets/orders/versions/1/snapshots/{before['id']}"
        f"/diff/{after['id']}"
    )
    assert response.status_code == 200
    added = response.json()["added"]
    # {"a":1} occurs twice and must surface as one aggregated entry.
    assert added == [
        {"row": {"a": 1}, "count": 2},
        {"row": {"m": [1, 2]}, "count": 1},
        {"row": {"nested": {"a": 2, "b": 1}}, "count": 1},
        {"row": {"z": 9}, "count": 1},
    ]
    for entry in added:
        assert set(entry) == {"row", "count"}


def test_diff_is_directional(client: TestClient) -> None:
    make_dataset_version(client)
    first = create_snapshot(client, [{"id": 1}])
    time.sleep(0.02)
    second = create_snapshot(client, [{"id": 2}])

    forward = client.get(
        f"/datasets/orders/versions/1/snapshots/{first['id']}"
        f"/diff/{second['id']}"
    ).json()
    assert forward["from_snapshot_id"] == first["id"]
    assert forward["to_snapshot_id"] == second["id"]
    assert forward["added"] == [{"row": {"id": 2}, "count": 1}]
    assert forward["removed"] == [{"row": {"id": 1}, "count": 1}]

    backward = client.get(
        f"/datasets/orders/versions/1/snapshots/{second['id']}"
        f"/diff/{first['id']}"
    ).json()
    assert backward["added"] == [{"row": {"id": 1}, "count": 1}]
    assert backward["removed"] == [{"row": {"id": 2}, "count": 1}]

    same = client.get(
        f"/datasets/orders/versions/1/snapshots/{first['id']}"
        f"/diff/{first['id']}"
    ).json()
    assert same["added"] == []
    assert same["removed"] == []


def test_diff_across_versions_or_datasets_is_rejected(client: TestClient) -> None:
    make_dataset_version(client, "orders")
    make_dataset_version(client, "customers")
    v1_first = create_snapshot(client, [{"id": 1}], "orders", 1)
    time.sleep(0.02)
    v1_second = create_snapshot(client, [{"id": 2}], "orders", 1)

    # A second version of orders.
    response = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201
    other_version = create_snapshot(client, [{"id": 9}], "orders", 2)
    other_dataset = create_snapshot(client, [{"id": 9}], "customers", 1)

    cross_version = client.get(
        f"/datasets/orders/versions/1/snapshots/{v1_first['id']}"
        f"/diff/{other_version['id']}"
    )
    assert cross_version.status_code == 422
    assert cross_version.json()["error"] == "validation_error"

    cross_dataset = client.get(
        f"/datasets/orders/versions/1/snapshots/{v1_first['id']}"
        f"/diff/{other_dataset['id']}"
    )
    assert cross_dataset.status_code == 422
    assert cross_dataset.json()["error"] == "validation_error"

    # Same-scope diff still works.
    ok = client.get(
        f"/datasets/orders/versions/1/snapshots/{v1_first['id']}"
        f"/diff/{v1_second['id']}"
    )
    assert ok.status_code == 200


def test_diff_unknown_snapshots_return_404(client: TestClient) -> None:
    make_dataset_version(client)
    snapshot = create_snapshot(client, [{"id": 1}])

    assert client.get(
        f"/datasets/orders/versions/1/snapshots/{snapshot['id']}"
        f"/diff/{snapshot['id'] + 1000}"
    ).status_code == 404

    assert client.get(
        f"/datasets/orders/versions/1/snapshots/{snapshot['id'] + 1000}"
        f"/diff/{snapshot['id']}"
    ).status_code == 404

    assert client.get(
        "/datasets/ghost/versions/1/snapshots/1/diff/1"
    ).status_code == 404
    assert client.get(
        "/datasets/orders/versions/99/snapshots/1/diff/1"
    ).status_code == 404


def test_snapshots_are_isolated_per_version(client: TestClient) -> None:
    make_dataset_version(client)
    v1 = create_snapshot(client, [{"id": 1}], "orders", 1)

    response = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201
    v2 = create_snapshot(client, [{"id": 2}], "orders", 2)

    assert client.get(
        f"/datasets/orders/versions/1/snapshots/{v2['id']}"
    ).status_code == 404
    assert client.get(
        f"/datasets/orders/versions/2/snapshots/{v1['id']}"
    ).status_code == 404

    listing = client.get("/datasets/orders/versions/2/snapshots")
    assert [s["id"] for s in listing.json()] == [v2["id"]]
