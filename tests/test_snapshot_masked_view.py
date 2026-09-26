"""Tests for the role-masked time-travel read of row snapshots.

The endpoint is ``POST .../snapshots/at/masked-view``: it selects the latest
snapshot created at or before the body timestamp and returns its rows masked
exactly like the row-submission privacy view, leaving the same masking-hit and
access records. These tests cover selection, masking semantics, the audit
trail, 404/422 precedence, snapshot immutability and persistence across
restarts.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app import repository

PROJECT_ROOT = Path(__file__).resolve().parents[1]

MASKED_VIEW_PATH = "/datasets/orders/versions/1/snapshots/at/masked-view"
AUDIT_PATH = "/datasets/orders/versions/1/privacy-policies/view/audit-records"
ACCESS_PATH = "/datasets/orders/versions/1/privacy-policies/view/access-records"
POLICIES_PATH = "/datasets/orders/versions/1/privacy-policies"
SNAPSHOTS_PATH = "/datasets/orders/versions/1/snapshots"


def make_dataset_with_version(client: TestClient, name: str = "orders") -> None:
    response = client.post("/datasets", json={"name": name})
    assert response.status_code == 201, response.text
    response = client.post(
        f"/datasets/{name}/versions",
        json={"fields": [
            {"name": "id", "type": "integer", "nullable": False},
            {"name": "email", "type": "string", "nullable": True},
            {"name": "ssn", "type": "string", "nullable": True},
            {"name": "age", "type": "integer", "nullable": True},
        ]},
    )
    assert response.status_code == 201, response.text


def make_snapshot(client: TestClient, rows: list) -> dict:
    response = client.post(SNAPSHOTS_PATH, json={"rows": rows})
    assert response.status_code == 201, response.text
    return response.json()


def create_policy(client: TestClient, payload: dict) -> dict:
    response = client.post(POLICIES_PATH, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def masked_view(
    client: TestClient,
    role: str,
    timestamp: str,
    *,
    path: str = MASKED_VIEW_PATH,
):
    return client.post(path, json={"role": role, "timestamp": timestamp})


# --------------------------------------------------------------------------- #
# Selection and masking
# --------------------------------------------------------------------------- #


def test_masked_view_selects_latest_snapshot_and_masks_rows(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    email_policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": ["analyst"]},
    )
    create_policy(
        client,
        {"field": "ssn", "classification": "secret", "masking": "redact",
         "allowed_roles": []},
    )

    first = make_snapshot(
        client,
        [{"id": 1, "email": "alice@example.com", "ssn": "111-22-3333"}],
    )
    time.sleep(0.01)
    second = make_snapshot(client, [
        {"id": 2, "email": "bob@example.com", "ssn": "222-33-4444", "age": 40},
        {"id": 3, "email": None, "ssn": None},
    ])

    future = (
        datetime.fromisoformat(second["created_at"]) + timedelta(days=1)
    ).isoformat()

    response = masked_view(client, "guest", future)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "dataset", "version", "snapshot_id", "created_at", "rows"
    }
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["snapshot_id"] == second["id"]
    assert body["created_at"] == second["created_at"]
    # Row order is preserved and each value is masked like the regular view:
    # a long email becomes first + last two characters, every other covered
    # non-null value is "***", nulls stay null and uncovered fields pass
    # through.
    assert body["rows"] == [
        {"id": 2, "email": "bom", "ssn": "***", "age": 40},
        {"id": 3, "email": None, "ssn": None},
    ]

    # An instant between the two snapshots resolves to the first one.
    middle = (
        datetime.fromisoformat(first["created_at"])
        + (
            datetime.fromisoformat(second["created_at"])
            - datetime.fromisoformat(first["created_at"])
        )
        / 2
    ).isoformat()
    between = masked_view(client, "guest", middle)
    assert between.status_code == 200, between.text
    between_body = between.json()
    assert between_body["snapshot_id"] == first["id"]
    assert between_body["created_at"] == first["created_at"]
    assert between_body["rows"] == [
        {"id": 1, "email": "aom", "ssn": "***"}
    ]

    # The boundary instant (created_at <= timestamp) keeps the first snapshot.
    exact = masked_view(client, "guest", first["created_at"])
    assert exact.status_code == 200
    assert exact.json()["snapshot_id"] == first["id"]

    # An allowed role sees the email unchanged; ssn allows no role, so it
    # stays masked and hits as usual.
    allowed = masked_view(client, "analyst", future)
    assert allowed.status_code == 200
    assert allowed.json()["rows"] == [
        {"id": 2, "email": "bob@example.com", "ssn": "***", "age": 40},
        {"id": 3, "email": None, "ssn": None},
    ]

    # The policy id carried by the hits is the policy that did the masking.
    records = client.get(AUDIT_PATH).json()
    guest_hits = [record for record in records if record["role"] == "guest"]
    assert {record["field"] for record in guest_hits} == {"email", "ssn"}
    assert all(
        record["policy_id"] == email_policy["id"]
        for record in guest_hits
        if record["field"] == "email"
    )
    # The analyst's read only hit ssn.
    analyst_hits = [record for record in records if record["role"] == "analyst"]
    assert [record["field"] for record in analyst_hits] == ["ssn"]
    analyst_access = [
        record for record in client.get(ACCESS_PATH).json()
        if record["role"] == "analyst"
    ]
    assert len(analyst_access) == 1
    assert analyst_access[0]["masked_count"] == 1
    assert analyst_access[0]["row_count"] == 2


def test_masked_view_preserves_row_and_key_order(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    snapshot = make_snapshot(client, [
        {"zeta": 1, "email": "alice@example.com", "alpha": None},
        {"email": "bob@example.com"},
        {"email": "carol@example.com"},
    ])

    response = masked_view(
        client, "guest",
        (datetime.fromisoformat(snapshot["created_at"]) + timedelta(seconds=1))
        .isoformat(),
    )
    assert response.status_code == 200
    body = response.json()
    assert [list(row) for row in body["rows"]] == [
        ["zeta", "email", "alpha"],
        ["email"],
        ["email"],
    ]
    assert [row.get("zeta") for row in body["rows"]] == [1, None, None]


def test_masked_view_accepts_z_and_offset_timestamps(client: TestClient) -> None:
    make_dataset_with_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    stored = datetime.fromisoformat(snapshot["created_at"])
    future = stored + timedelta(hours=1)

    z_value = future.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    z_response = masked_view(client, "guest", z_value)
    assert z_response.status_code == 200, z_response.text
    assert z_response.json()["snapshot_id"] == snapshot["id"]

    plus_five = future.astimezone(timezone(timedelta(hours=5, minutes=30)))
    offset_response = masked_view(client, "guest", plus_five.isoformat())
    assert offset_response.status_code == 200, offset_response.text
    assert offset_response.json()["snapshot_id"] == snapshot["id"]


# --------------------------------------------------------------------------- #
# Edge cases that still succeed and only leave an access record
# --------------------------------------------------------------------------- #


def test_empty_snapshot_leaves_only_an_access_record(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    snapshot = make_snapshot(client, [])

    response = masked_view(
        client, "guest",
        (datetime.fromisoformat(snapshot["created_at"]) + timedelta(seconds=1))
        .isoformat(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["snapshot_id"] == snapshot["id"]
    assert body["rows"] == []

    assert client.get(AUDIT_PATH).json() == []
    access = client.get(ACCESS_PATH).json()
    assert len(access) == 1
    assert access[0]["role"] == "guest"
    assert access[0]["row_count"] == 0
    assert access[0]["masked_count"] == 0


def test_allowed_role_leaves_only_an_access_record(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": ["analyst"]},
    )
    snapshot = make_snapshot(
        client, [{"email": "alice@example.com"}, {"email": "bob@example.com"}]
    )

    response = masked_view(
        client, "analyst",
        (datetime.fromisoformat(snapshot["created_at"]) + timedelta(seconds=1))
        .isoformat(),
    )
    assert response.status_code == 200
    assert response.json()["rows"] == [
        {"email": "alice@example.com"}, {"email": "bob@example.com"}
    ]
    assert client.get(AUDIT_PATH).json() == []
    access = client.get(ACCESS_PATH).json()
    assert len(access) == 1
    assert access[0]["row_count"] == 2
    assert access[0]["masked_count"] == 0


def test_all_null_values_leave_only_an_access_record(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": []},
    )
    snapshot = make_snapshot(
        client, [{"email": None, "id": 1}, {"id": 2}, {"email": None}]
    )

    response = masked_view(
        client, "guest",
        (datetime.fromisoformat(snapshot["created_at"]) + timedelta(seconds=1))
        .isoformat(),
    )
    assert response.status_code == 200
    assert response.json()["rows"] == [
        {"email": None, "id": 1}, {"id": 2}, {"email": None}
    ]
    assert client.get(AUDIT_PATH).json() == []
    access = client.get(ACCESS_PATH).json()
    assert access[0]["row_count"] == 3
    assert access[0]["masked_count"] == 0


def test_version_without_policies_leaves_only_an_access_record(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    snapshot = make_snapshot(client, [{"email": "alice@example.com", "id": 1}])

    response = masked_view(
        client, "guest",
        (datetime.fromisoformat(snapshot["created_at"]) + timedelta(seconds=1))
        .isoformat(),
    )
    assert response.status_code == 200
    assert response.json()["rows"] == [
        {"email": "alice@example.com", "id": 1}
    ]
    assert client.get(AUDIT_PATH).json() == []
    assert client.get(ACCESS_PATH).json()[0]["masked_count"] == 0


def test_disabled_policy_is_not_applied(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    disabled = client.patch(
        f"{POLICIES_PATH}/{policy['id']}", json={"enabled": False}
    )
    assert disabled.status_code == 200
    snapshot = make_snapshot(client, [{"email": "alice@example.com"}])

    response = masked_view(
        client, "guest",
        (datetime.fromisoformat(snapshot["created_at"]) + timedelta(seconds=1))
        .isoformat(),
    )
    assert response.status_code == 200
    assert response.json()["rows"] == [{"email": "alice@example.com"}]
    assert client.get(AUDIT_PATH).json() == []
    assert len(client.get(ACCESS_PATH).json()) == 1


# --------------------------------------------------------------------------- #
# Audit trail: hit records, access record, continuity and shared write time
# --------------------------------------------------------------------------- #


def test_successful_masked_view_writes_hit_and_access_records(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    email_policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": []},
    )
    ssn_policy = create_policy(
        client,
        {"field": "ssn", "classification": "secret", "masking": "redact",
         "allowed_roles": []},
    )
    snapshot = make_snapshot(client, [
        {"id": 1, "email": "alice@example.com", "ssn": "111"},
        {"id": 2, "email": "bob@example.com"},
        {"id": 3, "email": None, "ssn": None},
    ])
    timestamp = (
        datetime.fromisoformat(snapshot["created_at"]) + timedelta(seconds=1)
    ).isoformat()

    response = masked_view(client, "guest", timestamp)
    assert response.status_code == 200

    hits = client.get(AUDIT_PATH).json()
    # Three values masked: two emails and one ssn, in row order.
    assert [(hit["field"], hit["masking"]) for hit in hits] == [
        ("email", "partial"),
        ("ssn", "redact"),
        ("email", "partial"),
    ]
    assert [hit["sequence"] for hit in hits] == [1, 2, 3]
    assert [hit["policy_id"] for hit in hits] == [
        email_policy["id"], ssn_policy["id"], email_policy["id"]
    ]
    assert {hit["role"] for hit in hits} == {"guest"}
    # All hits of one read share a single write time.
    assert len({hit["created_at"] for hit in hits}) == 1
    for hit in hits:
        assert set(hit) == {
            "sequence", "field", "policy_id", "role", "masking", "created_at"
        }

    access = client.get(ACCESS_PATH).json()
    assert len(access) == 1
    assert set(access[0]) == {
        "sequence", "role", "row_count", "masked_count", "created_at"
    }
    assert access[0]["sequence"] == 1
    assert access[0]["role"] == "guest"
    assert access[0]["row_count"] == 3
    assert access[0]["masked_count"] == 3
    # The hit batch and the access record of one read share one write time.
    assert access[0]["created_at"] == hits[0]["created_at"]

    # Reading again appends a second, independent trail and the hit run
    # continues without reusing sequences.
    again = masked_view(client, "guest", timestamp)
    assert again.status_code == 200
    second_hits = client.get(AUDIT_PATH).json()
    assert [hit["sequence"] for hit in second_hits] == [1, 2, 3, 4, 5, 6]
    second_access = client.get(ACCESS_PATH).json()
    assert [record["sequence"] for record in second_access] == [1, 2]
    assert second_access[1]["masked_count"] == 3


def test_masked_view_trail_is_continuous_with_regular_view(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    snapshot = make_snapshot(client, [{"email": "snapshot@example.com"}])
    timestamp = (
        datetime.fromisoformat(snapshot["created_at"]) + timedelta(seconds=1)
    ).isoformat()

    regular = client.post(
        f"{POLICIES_PATH}/view",
        json={"role": "guest", "rows": [{"email": "submitted@example.com"}]},
    )
    assert regular.status_code == 200
    time_travel = masked_view(client, "guest", timestamp)
    assert time_travel.status_code == 200

    hits = client.get(AUDIT_PATH).json()
    assert [hit["sequence"] for hit in hits] == [1, 2]
    assert [hit["field"] for hit in hits] == ["email", "email"]
    access = client.get(ACCESS_PATH).json()
    assert [record["sequence"] for record in access] == [1, 2]
    assert [record["row_count"] for record in access] == [1, 1]
    assert [record["masked_count"] for record in access] == [1, 1]


def test_masked_view_trail_enters_the_existing_aggregates(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    snapshot = make_snapshot(client, [{"email": "alice@example.com"}])
    timestamp = (
        datetime.fromisoformat(snapshot["created_at"]) + timedelta(seconds=1)
    ).isoformat()
    assert masked_view(client, "guest", timestamp).status_code == 200

    base = POLICIES_PATH + "/view"

    # Search filter (role/field) finds the masked-view hit.
    search = client.get(
        base + "/audit-records/search",
        params={"role": "GUEST", "field": "email"},
    )
    assert search.status_code == 200
    assert len(search.json()) == 1

    # Summary groups it.
    summary = client.get(base + "/audit-records/summary")
    assert summary.status_code == 200
    groups = summary.json()["groups"]
    assert len(groups) == 1
    assert groups[0]["hit_count"] == 1

    # Trend counts the policy and the day.
    trend = client.get(base + "/audit-records/trend")
    assert trend.status_code == 200
    assert trend.json()["totals"]["total_hits"] == 1
    assert trend.json()["policies"][0]["total_hits"] == 1

    # Reconciliation matches one hit against one masked value.
    reconcile = client.get(base + "/audit-records/reconcile")
    assert reconcile.status_code == 200
    day = reconcile.json()["days"][0]
    assert day["hit_count"] == 1
    assert day["masked_count"] == 1
    assert day["view_count"] == 1
    assert day["consistent"] is True

    # Cleanup preview selects the freshly written hit with a later cutoff.
    preview = client.post(
        base + "/audit-records/cleanup-requests",
        json={"reason": "hold expired",
              "before": "2099-01-01T00:00:00+00:00"},
    )
    assert preview.status_code == 201, preview.text
    assert preview.json()["preview"]["hit_count"] == 1

    # Cross-version compliance export reflects both logs.
    export = client.get("/datasets/orders/privacy-compliance-export")
    assert export.status_code == 200
    version_state = export.json()["versions"][0]
    assert version_state["hit_count"] == 1
    assert version_state["masked_count"] == 1
    assert version_state["view_count"] == 1


# --------------------------------------------------------------------------- #
# Immutability
# --------------------------------------------------------------------------- #


def test_masked_view_does_not_modify_snapshot_or_policies(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": []},
    )
    rows = [{"id": 1, "email": "alice@example.com"}]
    snapshot = make_snapshot(client, rows)
    timestamp = (
        datetime.fromisoformat(snapshot["created_at"]) + timedelta(seconds=1)
    ).isoformat()

    assert masked_view(client, "guest", timestamp).status_code == 200
    assert masked_view(client, "guest", timestamp).status_code == 200

    # The stored snapshot still carries the raw, unmasked values.
    stored = client.get(f"{SNAPSHOTS_PATH}/{snapshot['id']}")
    assert stored.status_code == 200
    assert stored.json()["rows"] == rows
    assert stored.json()["created_at"] == snapshot["created_at"]
    assert stored.json()["row_count"] == 1

    # Policies are untouched (still enabled, same shape).
    policies = client.get(POLICIES_PATH).json()
    assert policies == [policy]


# --------------------------------------------------------------------------- #
# 404s, checked ahead of every shape check
# --------------------------------------------------------------------------- #


def test_unknown_dataset_or_version_is_404_even_with_a_garbled_body(
    client: TestClient,
) -> None:
    # No dataset at all: the path lookup wins over the malformed body.
    response = client.post(
        "/datasets/ghost/versions/1/snapshots/at/masked-view",
        content="{not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    make_dataset_with_version(client)
    # An existing dataset with an unknown version is likewise 404 first.
    response = client.post(
        "/datasets/orders/versions/9/snapshots/at/masked-view",
        json={"role": "guest", "timestamp": "2030-01-01T00:00:00+00:00"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_no_snapshot_at_or_before_timestamp_is_404_before_shape_checks(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    # A version with no snapshot at all: the missing snapshot is a 404 even
    # when the body has shape problems (blank role, extra field, query
    # parameter) that would otherwise be a 422.
    for payload in (
        {"role": "   ", "timestamp": "2030-01-01T00:00:00+00:00"},
        {"role": "guest", "timestamp": "2030-01-01T00:00:00+00:00",
         "extra": 1},
    ):
        response = client.post(MASKED_VIEW_PATH, json=payload)
        assert response.status_code == 404, payload
        assert response.json()["error"] == "not_found"
    response = client.post(
        MASKED_VIEW_PATH + "?expand=1",
        json={"role": "guest", "timestamp": "2030-01-01T00:00:00+00:00"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    snapshot = make_snapshot(client, [{"id": 1}])
    before = (
        datetime.fromisoformat(snapshot["created_at"]) - timedelta(seconds=1)
    ).isoformat()
    response = masked_view(client, "guest", before)
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    # A rejected read writes nothing.
    assert client.get(AUDIT_PATH).json() == []
    assert client.get(ACCESS_PATH).json() == []


# --------------------------------------------------------------------------- #
# 422s: body and query shape, none of which write anything
# --------------------------------------------------------------------------- #


def _prepare_version_with_snapshot(client: TestClient) -> str:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    snapshot = make_snapshot(client, [{"id": 1}])
    return (
        datetime.fromisoformat(snapshot["created_at"]) + timedelta(days=1)
    ).isoformat()


def test_missing_or_wrong_typed_fields_are_422(client: TestClient) -> None:
    timestamp = _prepare_version_with_snapshot(client)
    for payload in (
        {},
        {"role": "guest"},
        {"timestamp": timestamp},
        {"role": None, "timestamp": timestamp},
        {"role": 7, "timestamp": timestamp},
        {"role": True, "timestamp": timestamp},
        {"role": ["guest"], "timestamp": timestamp},
        {"role": "guest", "timestamp": None},
        {"role": "guest", "timestamp": 7},
        {"role": "guest", "timestamp": True},
        {"role": "guest", "timestamp": {"when": timestamp}},
    ):
        response = client.post(MASKED_VIEW_PATH, json=payload)
        assert response.status_code == 422, payload
        assert response.json()["error"] == "validation_error"
        assert "SQLite" not in response.text
    assert client.get(AUDIT_PATH).json() == []
    assert client.get(ACCESS_PATH).json() == []


def test_blank_role_is_422(client: TestClient) -> None:
    timestamp = _prepare_version_with_snapshot(client)
    for role in ("", "   ", "\t\n  "):
        response = masked_view(client, role, timestamp)
        assert response.status_code == 422, repr(role)
        assert response.json()["error"] == "validation_error"
    assert client.get(ACCESS_PATH).json() == []


def test_unparseable_or_naive_timestamp_is_422(client: TestClient) -> None:
    _prepare_version_with_snapshot(client)
    for raw in (
        "not-a-timestamp",
        "2026-13-99T00:00:00+00:00",
        "2026-01-01",
        "2026-01-01T00:00:00",
        "2026-01-01T00:00:00.000000",
    ):
        response = masked_view(client, "guest", raw)
        assert response.status_code == 422, raw
        assert response.json()["error"] == "validation_error"
    assert client.get(ACCESS_PATH).json() == []


def test_extra_fields_are_422(client: TestClient) -> None:
    timestamp = _prepare_version_with_snapshot(client)
    response = client.post(
        MASKED_VIEW_PATH,
        json={"role": "guest", "timestamp": timestamp, "rows": [], "extra": 1},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert "extra" in body["detail"]
    assert "rows" in body["detail"]
    assert client.get(ACCESS_PATH).json() == []


def test_empty_whitespace_or_non_json_body_is_422(client: TestClient) -> None:
    timestamp = _prepare_version_with_snapshot(client)
    for content in (b"", b"   ", b"\t\n", b"{not json", b"[1, 2, 3]", b'"guest"', b"null"):
        response = client.post(
            MASKED_VIEW_PATH,
            content=content,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422, content
        assert response.json()["error"] == "validation_error"
    assert client.get(ACCESS_PATH).json() == []


def test_query_parameters_are_422_and_body_is_required(client: TestClient) -> None:
    timestamp = _prepare_version_with_snapshot(client)
    response = client.post(
        MASKED_VIEW_PATH + "?expand=1",
        json={"role": "guest", "timestamp": timestamp},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get(ACCESS_PATH).json() == []


def test_get_is_not_accepted(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.get(MASKED_VIEW_PATH)
    assert response.status_code == 405


# --------------------------------------------------------------------------- #
# Trail-write tolerance: an obstructed audit write never fails the read
# --------------------------------------------------------------------------- #


def _prepare_masked_view_for_obstruction(client: TestClient) -> str:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": []},
    )
    create_policy(
        client,
        {"field": "ssn", "classification": "secret", "masking": "redact",
         "allowed_roles": []},
    )
    snapshot = make_snapshot(client, [
        {"id": 1, "email": "alice@example.com", "ssn": "111-22-3333"},
        {"id": 2, "email": "bob@example.com", "ssn": "222-33-4444"},
    ])
    return (
        datetime.fromisoformat(snapshot["created_at"]) + timedelta(days=1)
    ).isoformat()


def test_read_succeeds_when_the_whole_trail_write_is_obstructed(
    client: TestClient, monkeypatch
) -> None:
    timestamp = _prepare_masked_view_for_obstruction(client)

    # Every trail write fails: the optimistic append on the request
    # connection loses every bounded retry with a cross-process collision,
    # and the serialized fallback then fails with a database error. The read
    # must still return 200 with its masked rows; the trail obstruction is
    # swallowed rather than surfaced. A flag (rather than monkeypatch.undo,
    # which would also revert the isolated database) lifts the obstruction.
    obstructed = {"on": True}
    real_trail = repository._append_privacy_view_trail
    real_serialized = repository._record_privacy_view_trail_serialized

    def colliding_trail(conn, version_id, role, row_count, hits):
        if obstructed["on"]:
            raise sqlite3.IntegrityError("simulated cross-process collision")
        return real_trail(conn, version_id, role, row_count, hits)

    def obstructed_serialized(version_id, role, row_count, hits):
        if obstructed["on"]:
            raise sqlite3.OperationalError("simulated write obstruction")
        return real_serialized(version_id, role, row_count, hits)

    monkeypatch.setattr(
        repository, "_append_privacy_view_trail", colliding_trail
    )
    monkeypatch.setattr(
        repository, "_record_privacy_view_trail_serialized", obstructed_serialized
    )

    response = masked_view(client, "guest", timestamp)
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == [
        {"id": 1, "email": "aom", "ssn": "***"},
        {"id": 2, "email": "bom", "ssn": "***"},
    ]

    # Nothing of the obstructed trail survived: neither the hit records nor
    # the access record (every failed attempt rolled back, never leaving half
    # a batch behind).
    assert client.get(AUDIT_PATH).json() == []
    assert client.get(ACCESS_PATH).json() == []

    # After the obstruction is lifted, a later read writes its complete trail
    # with fresh, continuous sequences and one shared write time.
    obstructed["on"] = False
    again = masked_view(client, "guest", timestamp)
    assert again.status_code == 200
    hits = client.get(AUDIT_PATH).json()
    assert [hit["sequence"] for hit in hits] == [1, 2, 3, 4]
    access = client.get(ACCESS_PATH).json()
    assert len(access) == 1
    assert access[0]["masked_count"] == 4
    assert len({hit["created_at"] for hit in hits} | {access[0]["created_at"]}) == 1


def test_failed_append_never_keeps_a_partial_trail(
    client: TestClient, monkeypatch
) -> None:
    timestamp = _prepare_masked_view_for_obstruction(client)

    obstructed = {"on": True}
    real_batch = repository._append_privacy_view_audit_batch
    real_trail = repository._append_privacy_view_trail

    def batch_then_block(conn, version_id, role, row_count, hits):
        if not obstructed["on"]:
            return real_trail(conn, version_id, role, row_count, hits)
        # The hit batch lands inside the same transaction, then the rest of
        # the same read fails with a non-collision database error (not
        # retried): the best-effort wrapper rolls the whole transaction back.
        created_at = repository.utc_now_iso()
        real_batch(conn, version_id, role, hits, created_at)
        raise sqlite3.OperationalError("simulated access-record obstruction")

    monkeypatch.setattr(
        repository, "_append_privacy_view_trail", batch_then_block
    )

    response = masked_view(client, "guest", timestamp)
    assert response.status_code == 200, response.text
    assert client.get(AUDIT_PATH).json() == []
    assert client.get(ACCESS_PATH).json() == []

    # A read after recovery continues both runs from sequence 1: the rolled
    # back attempt consumed no sequence numbers.
    obstructed["on"] = False
    recovered = masked_view(client, "guest", timestamp)
    assert recovered.status_code == 200
    hits = client.get(AUDIT_PATH).json()
    assert [hit["sequence"] for hit in hits] == [1, 2, 3, 4]
    access = client.get(ACCESS_PATH).json()
    assert [record["sequence"] for record in access] == [1]
    assert access[0]["masked_count"] == 4


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


CREATE_SCRIPT = """
import json
import time
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response, code=(200, 201)):
    assert response.status_code in code, response.text

ok(client.post("/datasets", json={"name": "orders"}))
ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "email", "type": "string", "nullable": True},
    ]},
))
ok(client.post(
    "/datasets/orders/versions/1/privacy-policies",
    json={"field": "email", "classification": "PII", "masking": "partial",
          "allowed_roles": []},
))
snapshot = client.post(
    "/datasets/orders/versions/1/snapshots",
    json={"rows": [
        {"id": 1, "email": "alice@example.com"},
        {"id": 2, "email": "bob@example.com"},
    ]},
)
assert snapshot.status_code == 201, snapshot.text
snapshot_id = snapshot.json()["id"]
future = (
    datetime.fromisoformat(snapshot.json()["created_at"]) + timedelta(days=1)
).isoformat()
view = client.post(
    "/datasets/orders/versions/1/snapshots/at/masked-view",
    json={"role": "guest", "timestamp": future},
)
assert view.status_code == 200, view.text
assert view.json()["rows"] == [
    {"id": 1, "email": "aom"}, {"id": 2, "email": "bom"}
]
print(json.dumps({"snapshot_id": snapshot_id, "timestamp": future}))
"""

VERIFY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
state = json.loads(input())

hits = client.get(
    "/datasets/orders/versions/1/privacy-policies/view/audit-records"
)
assert hits.status_code == 200, hits.text
records = hits.json()
assert len(records) == 2
assert [r["sequence"] for r in records] == [1, 2]
assert [r["field"] for r in records] == ["email", "email"]
assert [r["masking"] for r in records] == ["partial", "partial"]

access = client.get(
    "/datasets/orders/versions/1/privacy-policies/view/access-records"
)
assert access.status_code == 200, access.text
entries = access.json()
assert len(entries) == 1
assert entries[0]["row_count"] == 2
assert entries[0]["masked_count"] == 2

# A fresh read after the restart keeps selecting the same snapshot, masks the
# same values and continues both sequence runs without reusing numbers.
view = client.post(
    "/datasets/orders/versions/1/snapshots/at/masked-view",
    json={"role": "guest", "timestamp": state["timestamp"]},
)
assert view.status_code == 200, view.text
body = view.json()
assert body["snapshot_id"] == state["snapshot_id"]
assert body["rows"] == [{"id": 1, "email": "aom"}, {"id": 2, "email": "bom"}]

hits_after = client.get(
    "/datasets/orders/versions/1/privacy-policies/view/audit-records"
).json()
assert [r["sequence"] for r in hits_after] == [1, 2, 3, 4]
access_after = client.get(
    "/datasets/orders/versions/1/privacy-policies/view/access-records"
).json()
assert [r["sequence"] for r in access_after] == [1, 2]

# The stored snapshot itself is still the raw rows.
stored = client.get(
    f"/datasets/orders/versions/1/snapshots/{state['snapshot_id']}"
)
assert stored.json()["rows"] == [
    {"id": 1, "email": "alice@example.com"},
    {"id": 2, "email": "bob@example.com"},
]
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


def test_masked_view_state_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "masked-view-lineage.db"
    state = _run(db_path, CREATE_SCRIPT)
    output = _run(db_path, VERIFY_SCRIPT, stdin=state)
    assert output == "verified"
