"""Tests for the read-only cross-version snapshot diff."""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import repository

V1_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "label", "type": "string", "nullable": True},
    {"name": "amount", "type": "float", "nullable": True},
    {"name": "note", "type": "string", "nullable": False},
]

# Against V1_FIELDS: id tightened, label retyped, amount loosened... defined
# per test below.


def create_dataset(client: TestClient, dataset: str = "orders") -> None:
    response = client.post("/datasets", json={"name": dataset})
    assert response.status_code == 201, response.text


def create_version(client: TestClient, fields: list, dataset: str = "orders") -> None:
    response = client.post(
        f"/datasets/{dataset}/versions", json={"fields": fields}
    )
    assert response.status_code == 201, response.text


def make_snapshot(
    client: TestClient, rows: list, dataset: str = "orders", version: int = 1
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots",
        json={"rows": rows},
    )
    assert response.status_code == 201, response.text
    return response.json()


def cross_diff(
    client: TestClient, base_id: int, target_id: int, dataset: str = "orders"
):
    return client.get(
        f"/datasets/{dataset}/snapshots/{base_id}/diff/{target_id}"
    )


def make_two_versions(client: TestClient) -> None:
    """v1 -> v2: id tightened, label retyped, note removed, extra added."""
    create_dataset(client)
    create_version(client, V1_FIELDS)
    create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "label", "type": "string", "nullable": False},
        {"name": "amount", "type": "string", "nullable": True},
        {"name": "extra", "type": "integer", "nullable": True},
    ])


# --------------------------------------------------------------------------- #
# Field changes
# --------------------------------------------------------------------------- #


def test_field_changes_use_trajectory_literals_and_collapse(client: TestClient) -> None:
    make_two_versions(client)
    base = make_snapshot(client, [], version=1)
    target = make_snapshot(client, [], version=2)

    response = cross_diff(client, base["id"], target["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["base_snapshot_id"] == base["id"]
    assert body["base_version"] == 1
    assert body["target_snapshot_id"] == target["id"]
    assert body["target_version"] == 2
    assert body["field_changes"] == [
        {
            "field": "amount",
            "kind": "type_changed",
            "before": {"type": "float", "nullable": True},
            "after": {"type": "string", "nullable": True},
        },
        {
            "field": "extra",
            "kind": "added",
            "before": None,
            "after": {"type": "integer", "nullable": True},
        },
        {
            "field": "label",
            "kind": "nullable_tightened",
            "before": {"type": "string", "nullable": True},
            "after": {"type": "string", "nullable": False},
        },
        {
            "field": "note",
            "kind": "removed",
            "before": {"type": "string", "nullable": False},
            "after": None,
        },
    ]
    # No rows on either side: both row collections are empty.
    assert body["added"] == []
    assert body["removed"] == []


def test_type_and_nullable_change_collapse_into_type_changed(
    client: TestClient,
) -> None:
    create_dataset(client)
    create_version(client, [
        {"name": "v", "type": "integer", "nullable": True},
    ])
    create_version(client, [
        {"name": "v", "type": "string", "nullable": False},
    ])
    base = make_snapshot(client, [], version=1)
    target = make_snapshot(client, [], version=2)

    body = cross_diff(client, base["id"], target["id"]).json()
    assert body["field_changes"] == [
        {
            "field": "v",
            "kind": "type_changed",
            "before": {"type": "integer", "nullable": True},
            "after": {"type": "string", "nullable": False},
        }
    ]


def test_nullable_loosening_is_reported(client: TestClient) -> None:
    create_dataset(client)
    create_version(client, [{"name": "v", "type": "integer", "nullable": False}])
    create_version(client, [{"name": "v", "type": "integer", "nullable": True}])
    base = make_snapshot(client, [], version=1)
    target = make_snapshot(client, [], version=2)

    body = cross_diff(client, base["id"], target["id"]).json()
    assert body["field_changes"] == [
        {
            "field": "v",
            "kind": "nullable_loosened",
            "before": {"type": "integer", "nullable": False},
            "after": {"type": "integer", "nullable": True},
        }
    ]


def test_version_numbers_may_run_in_either_order(client: TestClient) -> None:
    make_two_versions(client)
    v1 = make_snapshot(client, [{"id": 1}], version=1)
    v2 = make_snapshot(client, [{"id": 1}], version=2)

    # The newer version as the baseline is equally valid.
    body = cross_diff(client, v2["id"], v1["id"]).json()
    assert body["base_version"] == 2
    assert body["target_version"] == 1
    kinds = {change["field"]: change["kind"] for change in body["field_changes"]}
    assert kinds == {
        "amount": "type_changed",
        "extra": "removed",
        "label": "nullable_loosened",
        "note": "added",
    }


# --------------------------------------------------------------------------- #
# Row comparison on the projected rows
# --------------------------------------------------------------------------- #


def test_rows_are_compared_only_through_shared_fields(client: TestClient) -> None:
    make_two_versions(client)
    # "note" exists only in v1, "extra" only in v2: neither participates.
    base = make_snapshot(client, [
        {"id": 1, "label": "a", "amount": 1.5, "note": "x"},
        {"id": 2, "label": "b", "amount": 2.5, "note": "y"},
    ], version=1)
    target = make_snapshot(client, [
        {"id": 1, "label": "a", "amount": 1.5, "extra": 9},
        {"id": 3, "label": "c", "amount": 3.5, "extra": 8},
    ], version=2)

    body = cross_diff(client, base["id"], target["id"]).json()
    # The projected rows carry only id/label/amount; the first rows of both
    # sides project to the same row and cancel out.
    assert body["added"] == [
        {"row": {"id": 3, "label": "c", "amount": 3.5}, "count": 1}
    ]
    assert body["removed"] == [
        {"row": {"id": 2, "label": "b", "amount": 2.5}, "count": 1}
    ]


def test_projection_drops_keys_from_the_listed_rows(client: TestClient) -> None:
    make_two_versions(client)
    base = make_snapshot(client, [{"id": 1, "note": "only-v1"}], version=1)
    target = make_snapshot(client, [{"id": 1, "extra": 42}], version=2)

    body = cross_diff(client, base["id"], target["id"]).json()
    # Both rows project to {"id": 1}: no row difference at all.
    assert body["added"] == []
    assert body["removed"] == []


def test_fields_missing_from_a_row_do_not_participate(client: TestClient) -> None:
    make_two_versions(client)
    base = make_snapshot(client, [{"id": 1}], version=1)
    target = make_snapshot(client, [{"id": 1, "label": None}], version=2)

    body = cross_diff(client, base["id"], target["id"]).json()
    # A missing key and an explicit null are different rows.
    assert body["added"] == [{"row": {"id": 1, "label": None}, "count": 1}]
    assert body["removed"] == [{"row": {"id": 1}, "count": 1}]


def test_row_multiset_semantics_match_the_same_version_diff(
    client: TestClient,
) -> None:
    make_two_versions(client)
    shared = {"id": 1, "label": "a"}
    base = make_snapshot(client, [
        shared, {"label": "a", "id": 1}, shared, {"id": 2},
    ], version=1)
    target = make_snapshot(client, [
        {"label": "a", "id": 1}, {"id": 2}, {"id": 2}, {"id": 3},
    ], version=2)

    body = cross_diff(client, base["id"], target["id"]).json()
    # Key order is irrelevant and duplicates are counted.
    assert body["added"] == [
        {"row": {"id": 2}, "count": 1},
        {"row": {"id": 3}, "count": 1},
    ]
    assert body["removed"] == [{"row": {"id": 1, "label": "a"}, "count": 2}]


def test_row_entries_are_sorted_by_canonical_row_text(client: TestClient) -> None:
    make_two_versions(client)
    base = make_snapshot(client, [{"id": "z"}, {"id": 1}], version=1)
    target = make_snapshot(
        client, [{"id": "a"}, {"id": 1}, {"id": 9}, {"id": "Z"}], version=2
    )

    body = cross_diff(client, base["id"], target["id"]).json()
    added_keys = [
        json.dumps(entry["row"], sort_keys=True, separators=(",", ":"))
        for entry in body["added"]
    ]
    assert added_keys == sorted(added_keys)
    removed_keys = [
        json.dumps(entry["row"], sort_keys=True, separators=(",", ":"))
        for entry in body["removed"]
    ]
    assert removed_keys == sorted(removed_keys)


# --------------------------------------------------------------------------- #
# Deterministic document shape
# --------------------------------------------------------------------------- #


def test_document_is_compact_with_fixed_key_order_and_newline(
    client: TestClient,
) -> None:
    make_two_versions(client)
    base = make_snapshot(client, [{"id": 1, "note": "x"}], version=1)
    target = make_snapshot(client, [{"id": 2, "extra": 1}], version=2)

    response = cross_diff(client, base["id"], target["id"])
    assert response.status_code == 200, response.text
    text = response.text
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert text == json.dumps(json.loads(text), separators=(",", ":")) + "\n"
    assert list(json.loads(text)) == [
        "base_snapshot_id",
        "base_version",
        "target_snapshot_id",
        "target_version",
        "field_changes",
        "added",
        "removed",
    ]
    # Booleans render lowercase inside the compact document.
    assert '"nullable":false' in text
    assert '"nullable":true' in text
    # Missing sides are explicit nulls, never omitted keys.
    for change in json.loads(text)["field_changes"]:
        assert set(change) == {"field", "kind", "before", "after"}


# --------------------------------------------------------------------------- #
# Rejections
# --------------------------------------------------------------------------- #


def test_same_version_snapshots_are_422(client: TestClient) -> None:
    make_two_versions(client)
    first = make_snapshot(client, [{"id": 1}], version=1)
    second = make_snapshot(client, [{"id": 2}], version=1)

    response = cross_diff(client, first["id"], second["id"])
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"

    # The same-version comparison still works through the versioned path.
    same_version = client.get(
        f"/datasets/orders/versions/1/snapshots/{first['id']}/diff/{second['id']}"
    )
    assert same_version.status_code == 200, same_version.text


def test_snapshot_of_another_dataset_is_422(client: TestClient) -> None:
    make_two_versions(client)
    create_dataset(client, "customers")
    create_version(client, V1_FIELDS, dataset="customers")
    base = make_snapshot(client, [{"id": 1}], version=1)
    foreign = make_snapshot(client, [{"id": 1}], dataset="customers")

    for pair in ((base["id"], foreign["id"]), (foreign["id"], base["id"])):
        response = cross_diff(client, pair[0], pair[1])
        assert response.status_code == 422
        assert response.json()["error"] == "validation_error"

    # Reached through the foreign dataset's own path it is still 422: both
    # snapshots must belong to the path dataset.
    response = cross_diff(client, foreign["id"], base["id"], dataset="customers")
    assert response.status_code == 422


def test_unknown_dataset_or_snapshot_is_404(client: TestClient) -> None:
    make_two_versions(client)
    known = make_snapshot(client, [{"id": 1}], version=1)

    assert cross_diff(client, known["id"], known["id"], dataset="ghost").status_code == 404
    assert cross_diff(client, 999, known["id"]).status_code == 404
    assert cross_diff(client, known["id"], 1000).status_code == 404
    for response in (
        cross_diff(client, known["id"], known["id"], dataset="ghost"),
        cross_diff(client, 999, known["id"]),
    ):
        assert response.json()["error"] == "not_found"


def test_404_precedes_request_shape_checks(client: TestClient) -> None:
    make_two_versions(client)
    known = make_snapshot(client, [{"id": 1}], version=1)

    # Unknown snapshot plus a body and a query parameter: still 404.
    response = client.request(
        "GET",
        f"/datasets/orders/snapshots/999/diff/{known['id']}?x=1",
        content=b"{}",
    )
    assert response.status_code == 404
    # Unknown dataset plus a body: still 404.
    response = client.request(
        "GET",
        f"/datasets/ghost/snapshots/{known['id']}/diff/{known['id']}",
        content=b"{}",
    )
    assert response.status_code == 404


def test_request_body_and_query_parameters_are_422(client: TestClient) -> None:
    make_two_versions(client)
    base = make_snapshot(client, [{"id": 1}], version=1)
    target = make_snapshot(client, [{"id": 1}], version=2)
    path = f"/datasets/orders/snapshots/{base['id']}/diff/{target['id']}"

    for content in (b"{}", b" ", b"null"):
        response = client.request("GET", path, content=content)
        assert response.status_code == 422, content
        assert response.json()["error"] == "validation_error"
    response = client.get(path + "?from=1")
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_rejections_write_nothing(client: TestClient) -> None:
    make_two_versions(client)
    base = make_snapshot(client, [{"id": 1, "note": "x"}], version=1)
    target = make_snapshot(client, [{"id": 1, "extra": 1}], version=2)

    client.get(f"/datasets/orders/snapshots/{base['id']}/diff/{base['id']}")
    client.request(
        "GET",
        f"/datasets/orders/snapshots/{base['id']}/diff/{target['id']}",
        content=b"{}",
    )
    # The snapshots and their rows are untouched.
    assert client.get(
        "/datasets/orders/versions/1/snapshots/" + str(base["id"])
    ).json()["rows"] == [{"id": 1, "note": "x"}]
    assert client.get(
        "/datasets/orders/versions/2/snapshots/" + str(target["id"])
    ).json()["rows"] == [{"id": 1, "extra": 1}]


def test_comparison_is_read_only(client: TestClient) -> None:
    make_two_versions(client)
    base = make_snapshot(client, [{"id": 1, "note": "x"}], version=1)
    target = make_snapshot(client, [{"id": 2, "extra": 1}], version=2)

    first = cross_diff(client, base["id"], target["id"])
    second = cross_diff(client, base["id"], target["id"])
    assert first.text == second.text
    # No privacy trail is left behind by the comparison.
    assert client.get(
        "/datasets/orders/versions/1/privacy-policies/view/audit-records"
    ).json() == []
    assert client.get(
        "/datasets/orders/versions/1/privacy-policies/view/access-records"
    ).json() == []


# --------------------------------------------------------------------------- #
# Masked-view fault tolerance
# --------------------------------------------------------------------------- #


def _make_masked_view_setup(client: TestClient) -> str:
    create_dataset(client)
    create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "email", "type": "string", "nullable": True},
    ])
    response = client.post(
        "/datasets/orders/versions/1/privacy-policies",
        json={
            "field": "email",
            "classification": "PII",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    assert response.status_code == 201, response.text
    snapshot = make_snapshot(client, [
        {"id": 1, "email": "alice@example.com"},
        {"id": 2, "email": "bob@example.com"},
    ])
    return snapshot["created_at"]


def _masked_view(client: TestClient, timestamp: str):
    return client.post(
        "/datasets/orders/versions/1/snapshots/at/masked-view",
        json={"role": "guest", "timestamp": timestamp},
    )


def test_masked_view_returns_rows_when_the_trail_write_is_blocked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamp = _make_masked_view_setup(client)

    def blocked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repository, "_record_privacy_view_trail", blocked)
    response = _masked_view(client, timestamp)
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == [
        {"id": 1, "email": "***"}, {"id": 2, "email": "***"}
    ]
    # The blocked write left no trail behind.
    assert client.get(
        "/datasets/orders/versions/1/privacy-policies/view/audit-records"
    ).json() == []
    assert client.get(
        "/datasets/orders/versions/1/privacy-policies/view/access-records"
    ).json() == []


def test_masked_view_blocked_trail_leaves_no_half_records(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamp = _make_masked_view_setup(client)

    def half_written(conn, version_id, role, row_count, hits):
        # The hit batch lands on the request connection, then the write is
        # obstructed before the access record is appended.
        repository._append_privacy_view_trail(
            conn, version_id, role, row_count, hits
        )
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repository, "_record_privacy_view_trail", half_written)
    response = _masked_view(client, timestamp)
    assert response.status_code == 200, response.text
    # Neither the hits nor the access record survived: no half trail.
    assert client.get(
        "/datasets/orders/versions/1/privacy-policies/view/audit-records"
    ).json() == []
    assert client.get(
        "/datasets/orders/versions/1/privacy-policies/view/access-records"
    ).json() == []


def test_masked_view_trail_records_share_one_write_timestamp(
    client: TestClient,
) -> None:
    timestamp = _make_masked_view_setup(client)
    response = _masked_view(client, timestamp)
    assert response.status_code == 200, response.text

    hits = client.get(
        "/datasets/orders/versions/1/privacy-policies/view/audit-records"
    ).json()
    access = client.get(
        "/datasets/orders/versions/1/privacy-policies/view/access-records"
    ).json()
    assert len(hits) == 2
    assert len(access) == 1
    assert {hit["created_at"] for hit in hits} == {access[0]["created_at"]}
