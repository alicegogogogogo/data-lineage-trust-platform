"""Tests for the timestamped time-travel snapshot diff (GET .../snapshots/at/diff)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

DIFF_PATH = "/datasets/orders/versions/1/snapshots/at/diff"


def create_dataset_and_version(client: TestClient, dataset: str = "orders") -> None:
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


def make_snapshot(client: TestClient, rows: list) -> dict:
    response = client.post(
        "/datasets/orders/versions/1/snapshots",
        json={"rows": rows},
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_two_snapshots(client: TestClient) -> tuple[dict, dict]:
    first = make_snapshot(client, [
        {"id": 1, "label": "a"},
        {"id": 2, "label": "b"},
    ])
    # Ensure the second snapshot gets a strictly later created_at timestamp.
    time.sleep(0.01)
    second = make_snapshot(client, [
        {"id": 2, "label": "b"},
        {"id": 3, "label": "c"},
    ])
    return first, second


def _between(earlier_iso: str, later_iso: str) -> str:
    earlier = datetime.fromisoformat(earlier_iso)
    later = datetime.fromisoformat(later_iso)
    return (earlier + (later - earlier) / 2).isoformat()


def diff_at(
    client: TestClient,
    from_ts: str,
    to_ts: str,
    *,
    path: str = DIFF_PATH,
):
    return client.get(path, params={"from": from_ts, "to": to_ts})


# --------------------------------------------------------------------------- #
# Success cases
# --------------------------------------------------------------------------- #


def test_diff_at_reports_row_and_field_changes(client: TestClient) -> None:
    create_dataset_and_version(client)
    first = make_snapshot(client, [
        {"id": 1, "label": "a"},
        {"id": 2, "label": "b"},
    ])
    time.sleep(0.01)
    second = make_snapshot(client, [
        {"id": 2, "note": "b"},
        {"id": 3, "email": "c@example.com"},
    ])

    response = diff_at(client, first["created_at"], second["created_at"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert list(body) == [
        "from_timestamp",
        "to_timestamp",
        "from_snapshot_id",
        "to_snapshot_id",
        "added",
        "removed",
        "fields_added",
        "fields_removed",
    ]
    assert body["from_timestamp"] == first["created_at"]
    assert body["to_timestamp"] == second["created_at"]
    assert body["from_snapshot_id"] == first["id"]
    assert body["to_snapshot_id"] == second["id"]
    # Canonical JSON text sorts "email" before "id".
    assert body["added"] == [
        {"row": {"id": 3, "email": "c@example.com"}, "count": 1},
        {"row": {"id": 2, "note": "b"}, "count": 1},
    ]
    assert body["removed"] == [
        {"row": {"id": 1, "label": "a"}, "count": 1},
        {"row": {"id": 2, "label": "b"}, "count": 1},
    ]
    # "label" exists only on the base side, "email"/"note" only on the target
    # side; "id" is present on both and appears in neither set.
    assert body["fields_added"] == ["email", "note"]
    assert body["fields_removed"] == ["label"]


def test_diff_at_selects_latest_snapshot_not_later_than_each_timestamp(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    first, second = make_two_snapshots(client)

    middle = _between(first["created_at"], second["created_at"])
    future = (
        datetime.fromisoformat(second["created_at"]) + timedelta(days=1)
    ).isoformat()

    # 'from' between the two picks the first; 'to' in the future picks the
    # second.
    body = diff_at(client, middle, future).json()
    assert body["from_snapshot_id"] == first["id"]
    assert body["to_snapshot_id"] == second["id"]


def test_diff_at_same_snapshot_returns_empty_sets(client: TestClient) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [{"id": 1, "label": "a"}])
    future = (
        datetime.fromisoformat(snapshot["created_at"]) + timedelta(hours=1)
    ).isoformat()

    response = diff_at(client, future, future)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["from_snapshot_id"] == body["to_snapshot_id"] == snapshot["id"]
    assert body["added"] == []
    assert body["removed"] == []
    assert body["fields_added"] == []
    assert body["fields_removed"] == []


def test_diff_at_accepts_to_earlier_than_from(client: TestClient) -> None:
    create_dataset_and_version(client)
    first, second = make_two_snapshots(client)
    future = (
        datetime.fromisoformat(second["created_at"]) + timedelta(hours=1)
    ).isoformat()

    # Reversed direction is legal; each side resolves its own snapshot.
    response = diff_at(client, future, first["created_at"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["from_snapshot_id"] == second["id"]
    assert body["to_snapshot_id"] == first["id"]
    assert body["added"] == [{"row": {"id": 1, "label": "a"}, "count": 1}]
    assert body["removed"] == [{"row": {"id": 3, "label": "c"}, "count": 1}]
    assert body["fields_added"] == []
    assert body["fields_removed"] == []


def test_diff_at_row_multiset_matches_by_id_diff(client: TestClient) -> None:
    create_dataset_and_version(client)
    shared = {"id": 1}
    first = make_snapshot(client, [shared, shared, shared, {"id": 2}])
    time.sleep(0.01)
    second = make_snapshot(
        client, [shared, {"id": 2}, {"id": 2}, {"id": 3}]
    )
    future = (
        datetime.fromisoformat(second["created_at"]) + timedelta(hours=1)
    ).isoformat()

    body = diff_at(client, first["created_at"], future).json()
    assert body["added"] == [
        {"row": {"id": 2}, "count": 1},
        {"row": {"id": 3}, "count": 1},
    ]
    assert body["removed"] == [{"row": {"id": 1}, "count": 2}]

    by_id = client.get(
        f"/datasets/orders/versions/1/snapshots/{first['id']}/diff/{second['id']}"
    ).json()
    assert body["added"] == by_id["added"]
    assert body["removed"] == by_id["removed"]


def test_diff_at_fields_are_unioned_across_rows_and_sorted(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    first = make_snapshot(client, [{"id": 1, "zeta": 1}, {"id": 2, "label": "b"}])
    time.sleep(0.01)
    second = make_snapshot(client, [{"id": 1, "alpha": 1}, {"id": 2, "mid": "m"}])
    future = (
        datetime.fromisoformat(second["created_at"]) + timedelta(hours=1)
    ).isoformat()

    body = diff_at(client, first["created_at"], future).json()
    assert body["fields_added"] == ["alpha", "mid"]
    assert body["fields_removed"] == ["label", "zeta"]


def test_diff_at_fields_of_empty_snapshot(client: TestClient) -> None:
    create_dataset_and_version(client)
    empty = make_snapshot(client, [])
    time.sleep(0.01)
    populated = make_snapshot(client, [{"id": 1}])

    body = diff_at(client, empty["created_at"], populated["created_at"]).json()
    assert body["fields_added"] == ["id"]
    assert body["fields_removed"] == []

    reverse = diff_at(client, populated["created_at"], empty["created_at"]).json()
    assert reverse["fields_added"] == []
    assert reverse["fields_removed"] == ["id"]


def test_diff_at_echoes_timestamps_verbatim_and_accepts_offsets_and_z(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    make_snapshot(client, [{"id": 1}])

    raw_from = "2030-01-02T00:00:00+05:30"
    raw_to = "2030-01-01T00:00:00Z"
    body = diff_at(client, raw_from, raw_to).json()
    assert body["from_timestamp"] == raw_from
    assert body["to_timestamp"] == raw_to


def test_diff_at_document_is_deterministic_with_trailing_newline(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    first, second = make_two_snapshots(client)

    response = diff_at(client, first["created_at"], second["created_at"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    text = response.text
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    # Compact whitespace: no ": " or ", " separators.
    assert ": " not in text
    assert ", " not in text
    # Fixed top-level key order in the raw document.
    assert list(json.loads(text)) == [
        "from_timestamp",
        "to_timestamp",
        "from_snapshot_id",
        "to_snapshot_id",
        "added",
        "removed",
        "fields_added",
        "fields_removed",
    ]


# --------------------------------------------------------------------------- #
# 404s
# --------------------------------------------------------------------------- #


def test_diff_at_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    params = {
        "from": "2030-01-01T00:00:00Z",
        "to": "2030-01-02T00:00:00Z",
    }
    assert client.get(
        "/datasets/ghost/versions/1/snapshots/at/diff", params=params
    ).status_code == 404
    create_dataset_and_version(client)
    assert client.get(
        "/datasets/orders/versions/9/snapshots/at/diff", params=params
    ).status_code == 404


def test_diff_at_404_precedes_every_shape_check(client: TestClient) -> None:
    # Missing parameters, malformed timestamps, repeated parameters, an extra
    # parameter and body bytes all stay 404 while the path resource is unknown.
    path = "/datasets/ghost/versions/1/snapshots/at/diff"
    assert client.get(path).status_code == 404
    assert client.get(path, params={"from": "2030-01-01T00:00:00Z"}).status_code == 404
    assert client.get(
        path,
        params=[("from", "2030-01-01T00:00:00Z"), ("from", "2030-01-02T00:00:00Z")],
    ).status_code == 404
    assert client.get(path, params={"x": "1"}).status_code == 404
    assert client.request(
        "GET", path, params={"from": "bad", "to": "worse"}, content=b"{}"
    ).status_code == 404
    assert client.request("GET", path, content=b"  ").status_code == 404


def test_diff_at_missing_snapshot_on_either_side_is_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    after = (
        datetime.fromisoformat(snapshot["created_at"]) + timedelta(hours=1)
    ).isoformat()
    before = (
        datetime.fromisoformat(snapshot["created_at"]) - timedelta(seconds=1)
    ).isoformat()

    missing_from = diff_at(client, before, after)
    assert missing_from.status_code == 404
    assert missing_from.json()["error"] == "not_found"

    missing_to = diff_at(client, after, before)
    assert missing_to.status_code == 404
    assert missing_to.json()["error"] == "not_found"


def test_diff_at_without_any_snapshot_is_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    response = diff_at(
        client, "2030-01-01T00:00:00Z", "2030-01-02T00:00:00Z"
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


# --------------------------------------------------------------------------- #
# 422s
# --------------------------------------------------------------------------- #


def test_diff_at_requires_both_timestamps(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_snapshot(client, [])
    base = DIFF_PATH
    valid = "2030-01-01T00:00:00Z"

    for params in (
        {},
        {"to": valid},
        {"from": valid},
        {"from": "", "to": valid},
        {"from": valid, "to": ""},
    ):
        response = client.get(base, params=params)
        assert response.status_code == 422, params
        assert response.json()["error"] == "validation_error"


def test_diff_at_rejects_unparseable_or_naive_timestamps(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_snapshot(client, [])
    valid = "2030-01-01T00:00:00Z"

    for bad in (
        "not-a-timestamp",
        "2026-13-99T00:00:00+00:00",
        # Date-only and naive date-times lack a timezone.
        "2026-01-01",
        "2026-01-01T00:00:00",
        "2026-01-01T00:00:00.000000",
    ):
        response = client.get(DIFF_PATH, params={"from": bad, "to": valid})
        assert response.status_code == 422, bad
        assert response.json()["error"] == "validation_error"
        response = client.get(DIFF_PATH, params={"from": valid, "to": bad})
        assert response.status_code == 422, bad
        assert response.json()["error"] == "validation_error"


def test_diff_at_rejects_repeated_parameters(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_snapshot(client, [])
    valid = "2030-01-01T00:00:00Z"

    response = client.get(
        DIFF_PATH,
        params=[("from", valid), ("from", valid), ("to", valid)],
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"

    response = client.get(
        DIFF_PATH,
        params=[("from", valid), ("to", valid), ("to", valid)],
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_diff_at_rejects_extra_query_parameters(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_snapshot(client, [])
    valid = "2030-01-01T00:00:00Z"

    for params in (
        {"from": valid, "to": valid, "x": "1"},
        {"from": valid, "to": valid, "timestamp": valid},
    ):
        response = client.get(DIFF_PATH, params=params)
        assert response.status_code == 422, params
        assert response.json()["error"] == "validation_error"
        # Error payloads stay generic: no SQL or internals leak through.
        assert set(response.json()) == {"error", "detail"}


def test_diff_at_rejects_any_request_body(client: TestClient) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    valid = (
        datetime.fromisoformat(snapshot["created_at"]) + timedelta(hours=1)
    ).isoformat()

    for content in (b"{}", b"not json", b" ", b"\t\n ", b"\x00"):
        response = client.request(
            "GET",
            DIFF_PATH,
            params={"from": valid, "to": valid},
            content=content,
        )
        assert response.status_code == 422, content
        assert response.json()["error"] == "validation_error"


def test_diff_at_422_precedes_missing_snapshot_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    # No snapshots exist; shape problems are still 422, never 404.
    assert client.request(
        "GET",
        DIFF_PATH,
        params={"from": "2030-01-01T00:00:00Z", "to": "2030-01-02T00:00:00Z"},
        content=b"{}",
    ).status_code == 422
    assert client.get(
        DIFF_PATH,
        params={"from": "2030-01-01T00:00:00Z", "to": "bad"},
    ).status_code == 422
    assert client.get(
        DIFF_PATH,
        params={"from": "2030-01-01T00:00:00Z", "to": "2030-01-02T00:00:00Z", "x": "1"},
    ).status_code == 422


def test_diff_at_only_accepts_get(client: TestClient) -> None:
    create_dataset_and_version(client)
    make_snapshot(client, [])
    assert client.post(
        DIFF_PATH,
        params={"from": "2030-01-01T00:00:00Z", "to": "2030-01-02T00:00:00Z"},
    ).status_code == 405
    assert client.put(DIFF_PATH).status_code == 405
    assert client.delete(DIFF_PATH).status_code == 405


# --------------------------------------------------------------------------- #
# Read-only guarantees
# --------------------------------------------------------------------------- #


def test_diff_at_writes_nothing(client: TestClient, isolated_database: Path) -> None:
    create_dataset_and_version(client)
    # A policy gives masked reads something to record; the plain diff must not
    # record anything in their place.
    policy = client.post(
        "/datasets/orders/versions/1/privacy-policies",
        json={
            "field": "label",
            "classification": "PII",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    assert policy.status_code == 201, policy.text

    first, second = make_two_snapshots(client)

    def records() -> tuple[list, list]:
        hits = client.get(
            "/datasets/orders/versions/1/privacy-policies/view/audit-records"
        )
        accesses = client.get(
            "/datasets/orders/versions/1/privacy-policies/view/access-records"
        )
        assert hits.status_code == 200
        assert accesses.status_code == 200
        return hits.json(), accesses.json()

    hits_before, accesses_before = records()

    for _ in range(3):
        response = diff_at(client, first["created_at"], second["created_at"])
        assert response.status_code == 200, response.text
    # Reversed direction and the equal-snapshot case read just as much.
    assert diff_at(
        client, second["created_at"], first["created_at"]
    ).status_code == 200
    assert diff_at(
        client, second["created_at"], second["created_at"]
    ).status_code == 200

    hits_after, accesses_after = records()
    assert hits_after == hits_before == []
    assert accesses_after == accesses_before == []

    # Snapshots, their rows and policies are untouched.
    listed = client.get("/datasets/orders/versions/1/snapshots")
    assert [s["id"] for s in listed.json()] == [first["id"], second["id"]]
    policies = client.get("/datasets/orders/versions/1/privacy-policies")
    assert [p["field"] for p in policies.json()] == ["label"]
