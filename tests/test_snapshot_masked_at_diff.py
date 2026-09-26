"""Tests for the role-masked time-travel snapshot diff.

The endpoint is ``POST .../snapshots/at/diff/masked``: each of ``from`` and
``to`` selects the latest snapshot created at or before it, added/removed
entries are judged and counted on the raw rows with the bare-row diff's
multiset semantics, and only the emitted entry rows are masked for the role.
The read leaves the same masking-hit and access records as the masked view.
These tests cover diff/masking semantics, multiplicity and ordering, the
audit trail, 404/422 precedence, read-only behaviour and persistence across
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

MASKED_DIFF_PATH = "/datasets/orders/versions/1/snapshots/at/diff/masked"
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


def masked_diff(client: TestClient, payload: dict, *, path: str = MASKED_DIFF_PATH):
    return client.post(path, json=payload)


def _future(iso: str, days: int = 1) -> str:
    return (datetime.fromisoformat(iso) + timedelta(days=days)).isoformat()


# --------------------------------------------------------------------------- #
# Diff and masking semantics
# --------------------------------------------------------------------------- #


def test_masked_diff_judges_entries_on_raw_rows_and_masks_the_output(
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

    first = make_snapshot(client, [
        {"id": 1, "email": "alice@example.com", "ssn": "111-22-3333"},
        {"id": 2, "email": "bob@example.com"},
    ])
    time.sleep(0.01)
    second = make_snapshot(client, [
        {"id": 2, "email": "bob@example.com"},
        {"id": 3, "email": "carol@example.com", "ssn": "333-44-5555"},
    ])

    response = masked_diff(client, {
        "role": "guest",
        "from": first["created_at"],
        "to": _future(second["created_at"]),
    })
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
    assert body["to_timestamp"] == _future(second["created_at"])
    assert body["from_snapshot_id"] == first["id"]
    assert body["to_snapshot_id"] == second["id"]
    # The shared row (id 2) is in neither entry; only the raw-added and
    # raw-removed rows appear, each masked as in the privacy view.
    assert body["added"] == [
        {"row": {"id": 3, "email": "com", "ssn": "***"}, "count": 1}
    ]
    assert body["removed"] == [
        {"row": {"id": 1, "email": "aom", "ssn": "***"}, "count": 1}
    ]
    # The field-name sets carry no values and are unaffected by masking.
    assert body["fields_added"] == []
    assert body["fields_removed"] == []

    # An allowed role keeps the email values but ssn stays masked.
    allowed = masked_diff(client, {
        "role": "analyst",
        "from": first["created_at"],
        "to": _future(second["created_at"]),
    })
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["added"] == [
        {"row": {"id": 3, "email": "carol@example.com", "ssn": "***"},
         "count": 1}
    ]
    assert allowed.json()["removed"] == [
        {"row": {"id": 1, "email": "alice@example.com", "ssn": "***"},
         "count": 1}
    ]

    # Hits use the policy that performed the masking; the guest read masked
    # email (partial) and ssn (redact) once on each side.
    guest_hits = [
        record for record in client.get(AUDIT_PATH).json()
        if record["role"] == "guest"
    ]
    assert [(hit["field"], hit["masking"]) for hit in guest_hits] == [
        ("email", "partial"),
        ("ssn", "redact"),
        ("email", "partial"),
        ("ssn", "redact"),
    ]
    assert all(
        hit["policy_id"] == email_policy["id"]
        for hit in guest_hits if hit["field"] == "email"
    )


def test_masking_never_changes_membership_counts_or_order(client: TestClient) -> None:
    # Two rows whose masked representations coincide are still distinct
    # entries because the raw rows differ; both sides carry duplicates, which
    # survive masking as one entry with a multiplicity.
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )

    first = make_snapshot(client, [
        {"id": 1, "email": "alice@example.com"},
        {"id": 1, "email": "alice@example.com"},
    ])
    time.sleep(0.01)
    second = make_snapshot(client, [
        {"id": 2, "email": "bob@example.com"},
        {"id": 3, "email": "carol@example.com"},
    ])

    response = masked_diff(client, {
        "role": "guest",
        "from": first["created_at"],
        "to": _future(second["created_at"]),
    })
    assert response.status_code == 200, response.text
    body = response.json()
    # The two added raw rows both mask to email "***" but stay separate
    # entries (id differs), sorted by canonical RAW row text: id 2 before
    # id 3. The removed entry keeps its multiplicity of 2.
    assert body["added"] == [
        {"row": {"id": 2, "email": "***"}, "count": 1},
        {"row": {"id": 3, "email": "***"}, "count": 1},
    ]
    assert body["removed"] == [
        {"row": {"id": 1, "email": "***"}, "count": 2}
    ]

    # One hit per occurrence: two added emails plus two removed emails.
    hits = client.get(AUDIT_PATH).json()
    assert len(hits) == 4
    access = client.get(ACCESS_PATH).json()
    assert len(access) == 1
    # row_count expands the entries on both sides: 2 added + 2 removed.
    assert access[0]["row_count"] == 4
    assert access[0]["masked_count"] == 4


def test_masked_diff_sorts_entries_by_canonical_raw_text(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )

    first = make_snapshot(client, [{"id": 1}])
    time.sleep(0.01)
    # The raw emails sort "aaa..." < "zzz..."; masking turns both into "***",
    # so ordering by masked text (then id) would give id 1 first while the
    # raw order puts the aaa row (id 2) first.
    second = make_snapshot(client, [
        {"id": 1, "email": "zzz@example.com"},
        {"id": 2, "email": "aaa@example.com"},
    ])

    body = masked_diff(client, {
        "role": "guest",
        "from": first["created_at"],
        "to": _future(second["created_at"]),
    }).json()
    # Raw canonical texts: {"email":"aaa...","id":2} < {"email":"zzz...","id":1}
    assert [entry["row"]["id"] for entry in body["added"]] == [2, 1]
    # Masked alone would place both at "***" (then id order 1, 2) — the
    # observed order proves ordering uses the raw rows.
    assert [entry["row"]["email"] for entry in body["added"]] == ["***", "***"]


def test_null_and_uncovered_values_pass_through_but_still_diff(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": []},
    )

    first = make_snapshot(client, [{"id": 1, "email": None}])
    time.sleep(0.01)
    second = make_snapshot(client, [{"id": 2, "email": "dave@example.com"}])

    body = masked_diff(client, {
        "role": "guest",
        "from": first["created_at"],
        "to": _future(second["created_at"]),
    }).json()
    assert body["added"] == [
        {"row": {"id": 2, "email": "dom"}, "count": 1}
    ]
    # A null covered value is unchanged and never a masking hit.
    assert body["removed"] == [
        {"row": {"id": 1, "email": None}, "count": 1}
    ]
    hits = client.get(AUDIT_PATH).json()
    assert [hit["field"] for hit in hits] == ["email"]
    assert client.get(ACCESS_PATH).json()[0]["masked_count"] == 1
    assert client.get(ACCESS_PATH).json()[0]["row_count"] == 2


def test_same_snapshot_on_both_sides_is_empty_but_traced(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    snapshot = make_snapshot(client, [{"id": 1, "email": "a@b.co"}])
    future = _future(snapshot["created_at"])

    body = masked_diff(client, {"role": "guest", "from": future, "to": future}).json()
    assert body["from_snapshot_id"] == body["to_snapshot_id"] == snapshot["id"]
    assert body["added"] == []
    assert body["removed"] == []
    assert body["fields_added"] == []
    assert body["fields_removed"] == []
    # No values are masked (there are no diff entries), but the read still
    # leaves exactly one access record.
    assert client.get(AUDIT_PATH).json() == []
    access = client.get(ACCESS_PATH).json()
    assert len(access) == 1
    assert access[0]["row_count"] == 0
    assert access[0]["masked_count"] == 0


def test_field_name_sets_reflect_the_raw_snapshots(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    first = make_snapshot(client, [{"id": 1, "email": "a@b.co", "old": True}])
    time.sleep(0.01)
    second = make_snapshot(client, [{"id": 2, "email": "c@d.co", "fresh": True}])

    body = masked_diff(client, {
        "role": "guest",
        "from": first["created_at"],
        "to": _future(second["created_at"]),
    }).json()
    assert body["fields_added"] == ["fresh"]
    assert body["fields_removed"] == ["old"]


def test_document_is_compact_with_fixed_key_order_and_one_newline(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    first = make_snapshot(client, [{"id": 1, "email": "old@example.com"}])
    time.sleep(0.01)
    second = make_snapshot(client, [{"id": 2, "email": "new@example.com"}])

    response = masked_diff(client, {
        "role": "guest",
        "from": first["created_at"],
        "to": _future(second["created_at"]),
    })
    assert response.status_code == 200
    expected = json.dumps(
        response.json(), separators=(",", ":"), ensure_ascii=False
    ) + "\n"
    assert response.text == expected
    assert response.text.endswith("\n")
    assert not response.text.endswith("\n\n")
    assert response.text.index("from_timestamp") < response.text.index("to_timestamp")
    assert response.text.index("to_snapshot_id") < response.text.index("added")
    assert response.text.index("removed") < response.text.index("fields_added")
    assert response.text.index("fields_added") < response.text.index(
        "fields_removed"
    )


# --------------------------------------------------------------------------- #
# Audit trail
# --------------------------------------------------------------------------- #


def test_successful_read_writes_hit_and_access_records_with_one_write_time(
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
    first = make_snapshot(client, [
        {"id": 1, "email": "alice@example.com", "ssn": "111"},
    ])
    time.sleep(0.01)
    second = make_snapshot(client, [
        {"id": 2, "email": "bob@example.com", "ssn": "222"},
        {"id": 3, "email": None, "ssn": None},
    ])

    response = masked_diff(client, {
        "role": "guest",
        "from": first["created_at"],
        "to": _future(second["created_at"]),
    })
    assert response.status_code == 200

    hits = client.get(AUDIT_PATH).json()
    # Added-side entries first (id 2 masks email and ssn; id 3 masks
    # nothing), then the removed-side entry (id 1).
    assert [(hit["field"], hit["masking"]) for hit in hits] == [
        ("email", "partial"),
        ("ssn", "redact"),
        ("email", "partial"),
        ("ssn", "redact"),
    ]
    assert [hit["sequence"] for hit in hits] == [1, 2, 3, 4]
    assert [hit["policy_id"] for hit in hits] == [
        email_policy["id"], ssn_policy["id"],
        email_policy["id"], ssn_policy["id"],
    ]
    assert {hit["role"] for hit in hits} == {"guest"}
    assert len({hit["created_at"] for hit in hits}) == 1

    access = client.get(ACCESS_PATH).json()
    assert len(access) == 1
    # Expanded entry rows: two added (counts 1+1) + one removed.
    assert access[0]["row_count"] == 3
    assert access[0]["masked_count"] == 4
    assert access[0]["created_at"] == hits[0]["created_at"]

    # A second read continues both runs without reusing sequences.
    assert masked_diff(client, {
        "role": "guest",
        "from": first["created_at"],
        "to": _future(second["created_at"]),
    }).status_code == 200
    assert [hit["sequence"] for hit in client.get(AUDIT_PATH).json()] == [
        1, 2, 3, 4, 5, 6, 7, 8
    ]
    assert [a["sequence"] for a in client.get(ACCESS_PATH).json()] == [1, 2]


def test_diff_trail_is_continuous_with_the_other_masked_reads(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    snapshot = make_snapshot(client, [{"id": 1, "email": "a@b.co"}])
    # An instant strictly between the populated snapshot and the later empty one
    # selects the populated snapshot; a future instant selects the empty one.
    time.sleep(0.01)
    empty = make_snapshot(client, [])
    future = _future(empty["created_at"])
    middle = (
        datetime.fromisoformat(snapshot["created_at"])
        + (
            datetime.fromisoformat(empty["created_at"])
            - datetime.fromisoformat(snapshot["created_at"])
        )
        / 2
    ).isoformat()

    # A regular row-submission view and the at-time masked view both append to
    # the same sequence runs before the masked diff does; the diff then adds
    # the removed row's hit, so all three reads contribute one hit each.
    assert client.post(
        f"{POLICIES_PATH}/view",
        json={"role": "guest", "rows": [{"email": "submitted@example.com"}]},
    ).status_code == 200
    assert client.post(
        "/datasets/orders/versions/1/snapshots/at/masked-view",
        json={"role": "guest", "timestamp": middle},
    ).status_code == 200

    assert masked_diff(client, {
        "role": "guest",
        "from": middle,
        "to": future,
    }).status_code == 200

    hits = client.get(AUDIT_PATH).json()
    assert [hit["sequence"] for hit in hits] == [1, 2, 3]
    access = client.get(ACCESS_PATH).json()
    assert [a["sequence"] for a in access] == [1, 2, 3]
    assert [a["row_count"] for a in access] == [1, 1, 1]
    assert [a["masked_count"] for a in access] == [1, 1, 1]


def test_diff_trail_enters_the_existing_aggregates(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    first = make_snapshot(client, [])
    time.sleep(0.01)
    second = make_snapshot(client, [
        {"id": 1, "email": "alice@example.com"},
        {"id": 1, "email": "alice@example.com"},
    ])
    assert masked_diff(client, {
        "role": "guest",
        "from": first["created_at"],
        "to": _future(second["created_at"]),
    }).status_code == 200

    base = POLICIES_PATH + "/view"

    search = client.get(
        base + "/audit-records/search",
        params={"role": "GUEST", "field": "email"},
    )
    assert search.status_code == 200
    assert len(search.json()) == 2

    summary = client.get(base + "/audit-records/summary")
    assert summary.status_code == 200
    assert summary.json()["groups"][0]["hit_count"] == 2

    trend = client.get(base + "/audit-records/trend")
    assert trend.status_code == 200
    assert trend.json()["totals"]["total_hits"] == 2

    reconcile = client.get(base + "/audit-records/reconcile")
    assert reconcile.status_code == 200
    day = reconcile.json()["days"][0]
    assert day["hit_count"] == 2
    assert day["masked_count"] == 2
    assert day["view_count"] == 1
    assert day["consistent"] is True

    preview = client.post(
        base + "/audit-records/cleanup-requests",
        json={"reason": "hold expired",
              "before": "2099-01-01T00:00:00+00:00"},
    )
    assert preview.status_code == 201, preview.text
    assert preview.json()["preview"]["hit_count"] == 2

    export = client.get("/datasets/orders/privacy-compliance-export")
    version_state = export.json()["versions"][0]
    assert version_state["hit_count"] == 2
    assert version_state["masked_count"] == 2
    assert version_state["view_count"] == 1


# --------------------------------------------------------------------------- #
# Read-only behaviour
# --------------------------------------------------------------------------- #


def test_masked_diff_does_not_modify_snapshots_or_policies(
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
    future = _future(snapshot["created_at"])
    payload = {"role": "guest", "from": future, "to": future}

    assert masked_diff(client, payload).status_code == 200
    assert masked_diff(client, payload).status_code == 200

    stored = client.get(f"{SNAPSHOTS_PATH}/{snapshot['id']}")
    assert stored.json()["rows"] == rows
    assert client.get(POLICIES_PATH).json() == [policy]


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
    first = make_snapshot(client, [])
    time.sleep(0.01)
    second = make_snapshot(client, [{"id": 1, "email": "alice@example.com"}])

    body = masked_diff(client, {
        "role": "guest",
        "from": first["created_at"],
        "to": _future(second["created_at"]),
    }).json()
    assert body["added"] == [
        {"row": {"id": 1, "email": "alice@example.com"}, "count": 1}
    ]
    assert client.get(AUDIT_PATH).json() == []
    access = client.get(ACCESS_PATH).json()
    assert access[0]["masked_count"] == 0
    assert access[0]["row_count"] == 1


# --------------------------------------------------------------------------- #
# 404s, checked ahead of every shape check
# --------------------------------------------------------------------------- #


def test_unknown_dataset_or_version_is_404_even_with_a_garbled_body(
    client: TestClient,
) -> None:
    response = client.post(
        "/datasets/ghost/versions/1/snapshots/at/diff/masked",
        content="{not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    make_dataset_with_version(client)
    response = client.post(
        "/datasets/orders/versions/9/snapshots/at/diff/masked",
        json={"role": "guest", "from": "2030-01-01T00:00:00Z",
              "to": "2031-01-01T00:00:00Z"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_no_snapshot_on_either_side_is_404_before_shape_checks(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    valid = "2030-01-01T00:00:00+00:00"
    # A version without any snapshot: the missing-snapshot 404 wins over a
    # blank role, an extra field and a query parameter.
    for payload in (
        {"role": "   ", "from": valid, "to": valid},
        {"role": "guest", "from": valid, "to": valid, "extra": 1},
    ):
        response = masked_diff(client, payload)
        assert response.status_code == 404, payload
        assert response.json()["error"] == "not_found"
    response = client.post(
        MASKED_DIFF_PATH + "?expand=1",
        json={"role": "guest", "from": valid, "to": valid},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    snapshot = make_snapshot(client, [{"id": 1}])
    before = (
        datetime.fromisoformat(snapshot["created_at"]) - timedelta(seconds=1)
    ).isoformat()
    future = _future(snapshot["created_at"])
    assert masked_diff(
        client, {"role": "guest", "from": before, "to": future}
    ).status_code == 404
    assert masked_diff(
        client, {"role": "guest", "from": future, "to": before}
    ).status_code == 404

    # A rejected read writes nothing.
    assert client.get(AUDIT_PATH).json() == []
    assert client.get(ACCESS_PATH).json() == []


def test_missing_snapshot_404_precedes_shape_errors_on_the_other_side(
    client: TestClient,
) -> None:
    # Each side is probed independently, so one usable timestamp with no
    # snapshot at or before it is a 404 even when the other side is missing,
    # unparseable, naive, wrong-typed or carried by a malformed surrounding
    # body (extra field, query parameter).
    make_dataset_with_version(client)
    usable = "2030-01-01T00:00:00+00:00"
    for payload in (
        {"role": "guest", "from": usable},
        {"role": "guest", "from": usable, "to": "not-a-time"},
        {"role": "guest", "from": usable, "to": "2026-01-01T00:00:00"},
        {"role": "guest", "from": usable, "to": 7},
        {"role": "   ", "from": usable, "to": usable},
        {"role": "guest", "from": usable, "to": usable, "extra": 1},
    ):
        response = masked_diff(client, payload)
        assert response.status_code == 404, payload
        assert response.json()["error"] == "not_found"

    response = client.post(
        MASKED_DIFF_PATH + "?expand=1",
        json={"role": "guest", "from": usable, "to": "not-a-time"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    # The to side is probed independently as well: a missing to snapshot wins
    # over a shape problem on the from side.
    snapshot = make_snapshot(client, [{"id": 1}])
    before = (
        datetime.fromisoformat(snapshot["created_at"]) - timedelta(seconds=1)
    ).isoformat()
    response = masked_diff(
        client,
        {"role": "guest", "from": "not-a-time", "to": before},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    # A rejected read writes nothing.
    assert client.get(AUDIT_PATH).json() == []
    assert client.get(ACCESS_PATH).json() == []


# --------------------------------------------------------------------------- #
# 422s: body and query shape, none of which write anything
# --------------------------------------------------------------------------- #


def _prepare_version_with_snapshot(client: TestClient) -> dict:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    snapshot = make_snapshot(client, [{"id": 1}])
    future = _future(snapshot["created_at"])
    return {"role": "guest", "from": future, "to": future}


def test_missing_or_wrong_typed_fields_are_422(client: TestClient) -> None:
    valid = _prepare_version_with_snapshot(client)
    future = valid["from"]
    for payload in (
        {},
        {"role": "guest"},
        {"from": future, "to": future},
        {"role": "guest", "from": future},
        {"role": "guest", "to": future},
        {"role": None, "from": future, "to": future},
        {"role": 7, "from": future, "to": future},
        {"role": True, "from": future, "to": future},
        {"role": ["guest"], "from": future, "to": future},
        {"role": "guest", "from": None, "to": future},
        {"role": "guest", "from": 7, "to": future},
        {"role": "guest", "from": True, "to": future},
        {"role": "guest", "from": future, "to": {"when": future}},
    ):
        response = masked_diff(client, payload)
        assert response.status_code == 422, payload
        assert response.json()["error"] == "validation_error"
        assert set(response.json()) == {"error", "detail"}
        assert "SQLite" not in response.text
    assert client.get(AUDIT_PATH).json() == []
    assert client.get(ACCESS_PATH).json() == []


def test_blank_role_is_422(client: TestClient) -> None:
    valid = _prepare_version_with_snapshot(client)
    for role in ("", "   ", "\t\n  "):
        response = masked_diff(client, {**valid, "role": role})
        assert response.status_code == 422, repr(role)
        assert response.json()["error"] == "validation_error"
    assert client.get(ACCESS_PATH).json() == []


def test_unparseable_or_naive_timestamps_are_422(client: TestClient) -> None:
    valid = _prepare_version_with_snapshot(client)
    good = valid["from"]
    for raw in (
        "not-a-timestamp",
        "2026-13-99T00:00:00+00:00",
        "2026-01-01",
        "2026-01-01T00:00:00",
        "2026-01-01T00:00:00.000000",
    ):
        bad_from = masked_diff(client, {"role": "guest", "from": raw, "to": good})
        assert bad_from.status_code == 422, raw
        bad_to = masked_diff(client, {"role": "guest", "from": good, "to": raw})
        assert bad_to.status_code == 422, raw
    assert client.get(ACCESS_PATH).json() == []


def test_extra_fields_are_422(client: TestClient) -> None:
    valid = _prepare_version_with_snapshot(client)
    response = masked_diff(client, {**valid, "timestamp": valid["from"], "x": 1})
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert "timestamp" in body["detail"]
    assert "x" in body["detail"]
    assert client.get(ACCESS_PATH).json() == []


def test_empty_whitespace_or_non_json_body_is_422(client: TestClient) -> None:
    _prepare_version_with_snapshot(client)
    for content in (
        b"", b"   ", b"\t\n", b"{not json", b"[1, 2, 3]", b'"guest"', b"null"
    ):
        response = client.post(
            MASKED_DIFF_PATH,
            content=content,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422, content
        assert response.json()["error"] == "validation_error"
    assert client.get(ACCESS_PATH).json() == []


def test_query_parameters_are_422(client: TestClient) -> None:
    valid = _prepare_version_with_snapshot(client)
    response = client.post(
        MASKED_DIFF_PATH + "?expand=1",
        json=valid,
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get(ACCESS_PATH).json() == []


def test_only_post_is_accepted(client: TestClient) -> None:
    make_dataset_with_version(client)
    for method in ("get", "put", "patch", "delete"):
        response = getattr(client, method)(MASKED_DIFF_PATH)
        assert response.status_code == 405, method
        assert response.headers.get("allow") == "POST"


def test_timestamps_accept_z_and_offsets_and_are_echoed_verbatim(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    stored = datetime.fromisoformat(snapshot["created_at"])
    future = stored + timedelta(hours=1)

    z_value = future.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    z_response = masked_diff(
        client, {"role": "guest", "from": z_value, "to": z_value}
    )
    assert z_response.status_code == 200, z_response.text
    assert z_response.json()["from_timestamp"] == z_value
    assert z_response.json()["to_timestamp"] == z_value

    offset_value = future.astimezone(
        timezone(timedelta(hours=5, minutes=30))
    ).isoformat()
    offset_response = masked_diff(
        client, {"role": "guest", "from": offset_value, "to": offset_value}
    )
    assert offset_response.status_code == 200, offset_response.text
    assert offset_response.json()["from_snapshot_id"] == snapshot["id"]


# --------------------------------------------------------------------------- #
# Trail-write tolerance
# --------------------------------------------------------------------------- #


def _prepare_diff_for_obstruction(client: TestClient) -> dict:
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
    first = make_snapshot(client, [
        {"id": 1, "email": "alice@example.com", "ssn": "111-22-3333"},
    ])
    time.sleep(0.01)
    second = make_snapshot(client, [
        {"id": 2, "email": "bob@example.com", "ssn": "222-33-4444"},
    ])
    return {
        "role": "guest",
        "from": first["created_at"],
        "to": _future(second["created_at"]),
    }


def test_diff_read_succeeds_when_the_whole_trail_write_is_obstructed(
    client: TestClient, monkeypatch
) -> None:
    payload = _prepare_diff_for_obstruction(client)

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

    monkeypatch.setattr(repository, "_append_privacy_view_trail", colliding_trail)
    monkeypatch.setattr(
        repository, "_record_privacy_view_trail_serialized", obstructed_serialized
    )

    response = masked_diff(client, payload)
    assert response.status_code == 200, response.text
    assert response.json()["added"] == [
        {"row": {"id": 2, "email": "bom", "ssn": "***"}, "count": 1}
    ]
    assert response.json()["removed"] == [
        {"row": {"id": 1, "email": "aom", "ssn": "***"}, "count": 1}
    ]
    assert client.get(AUDIT_PATH).json() == []
    assert client.get(ACCESS_PATH).json() == []

    obstructed["on"] = False
    again = masked_diff(client, payload)
    assert again.status_code == 200
    hits = client.get(AUDIT_PATH).json()
    assert [hit["sequence"] for hit in hits] == [1, 2, 3, 4]
    access = client.get(ACCESS_PATH).json()
    assert len(access) == 1
    assert access[0]["masked_count"] == 4
    assert access[0]["row_count"] == 2
    assert len({hit["created_at"] for hit in hits} | {access[0]["created_at"]}) == 1


def test_diff_failed_append_never_keeps_a_partial_trail(
    client: TestClient, monkeypatch
) -> None:
    payload = _prepare_diff_for_obstruction(client)

    obstructed = {"on": True}
    real_batch = repository._append_privacy_view_audit_batch
    real_trail = repository._append_privacy_view_trail

    def batch_then_block(conn, version_id, role, row_count, hits):
        if not obstructed["on"]:
            return real_trail(conn, version_id, role, row_count, hits)
        created_at = repository.utc_now_iso()
        real_batch(conn, version_id, role, hits, created_at)
        raise sqlite3.OperationalError("simulated access-record obstruction")

    monkeypatch.setattr(repository, "_append_privacy_view_trail", batch_then_block)

    response = masked_diff(client, payload)
    assert response.status_code == 200, response.text
    assert client.get(AUDIT_PATH).json() == []
    assert client.get(ACCESS_PATH).json() == []

    obstructed["on"] = False
    assert masked_diff(client, payload).status_code == 200
    hits = client.get(AUDIT_PATH).json()
    assert [hit["sequence"] for hit in hits] == [1, 2, 3, 4]
    access = client.get(ACCESS_PATH).json()
    assert access[0]["masked_count"] == 4


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


CREATE_SCRIPT = """
import json
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
first = client.post(
    "/datasets/orders/versions/1/snapshots",
    json={"rows": [{"id": 1, "email": "alice@example.com"}]},
)
assert first.status_code == 201, first.text
import time
time.sleep(0.01)
second = client.post(
    "/datasets/orders/versions/1/snapshots",
    json={"rows": [{"id": 2, "email": "bob@example.com"}]},
)
assert second.status_code == 201, second.text
to_value = (
    datetime.fromisoformat(second.json()["created_at"]) + timedelta(days=1)
).isoformat()
diff = client.post(
    "/datasets/orders/versions/1/snapshots/at/diff/masked",
    json={"role": "guest",
          "from": first.json()["created_at"],
          "to": to_value},
)
assert diff.status_code == 200, diff.text
assert diff.json()["added"] == [{"row": {"id": 2, "email": "bom"}, "count": 1}]
assert diff.json()["removed"] == [{"row": {"id": 1, "email": "aom"}, "count": 1}]
print(json.dumps({
    "from_value": first.json()["created_at"],
    "to_value": to_value,
    "first_id": first.json()["id"],
    "second_id": second.json()["id"],
}))
"""

VERIFY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
state = json.loads(input())

hits = client.get(
    "/datasets/orders/versions/1/privacy-policies/view/audit-records"
).json()
assert [r["sequence"] for r in hits] == [1, 2]
assert [r["field"] for r in hits] == ["email", "email"]

access = client.get(
    "/datasets/orders/versions/1/privacy-policies/view/access-records"
).json()
assert len(access) == 1
assert access[0]["row_count"] == 2
assert access[0]["masked_count"] == 2

# A fresh read after the restart returns the identical comparison and
# continues both sequence runs without reusing numbers.
diff = client.post(
    "/datasets/orders/versions/1/snapshots/at/diff/masked",
    json={"role": "guest",
          "from": state["from_value"],
          "to": state["to_value"]},
)
assert diff.status_code == 200, diff.text
body = diff.json()
assert body["from_snapshot_id"] == state["first_id"]
assert body["to_snapshot_id"] == state["second_id"]
assert body["added"] == [{"row": {"id": 2, "email": "bom"}, "count": 1}]
assert body["removed"] == [{"row": {"id": 1, "email": "aom"}, "count": 1}]

hits_after = client.get(
    "/datasets/orders/versions/1/privacy-policies/view/audit-records"
).json()
assert [r["sequence"] for r in hits_after] == [1, 2, 3, 4]
access_after = client.get(
    "/datasets/orders/versions/1/privacy-policies/view/access-records"
).json()
assert [r["sequence"] for r in access_after] == [1, 2]

# The stored snapshots still carry the raw rows.
for snapshot_id, email in (
    (state["first_id"], "alice@example.com"),
    (state["second_id"], "bob@example.com"),
):
    stored = client.get(
        f"/datasets/orders/versions/1/snapshots/{snapshot_id}"
    ).json()
    assert stored["rows"] == [{"id": stored["rows"][0]["id"], "email": email}]
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


def test_masked_diff_state_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "masked-diff-lineage.db"
    state = _run(db_path, CREATE_SCRIPT)
    assert _run(db_path, VERIFY_SCRIPT, stdin=state) == "verified"
