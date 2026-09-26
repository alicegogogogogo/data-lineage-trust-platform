"""Tests for the role-masked time-travel diff of row snapshots.

The endpoint is ``POST .../snapshots/at/diff/masked``: each body timestamp
selects the latest snapshot created at or before it (the bare-row time diff's
selection), the diff is computed on the unmasked rows with the bare diff's
multiset semantics, and masking only rewrites each entry's row. Every read
leaves the masked view's hit/access trail. These tests cover selection, the
masked diff output, the audit trail, 404/422 precedence, snapshot
immutability and persistence across restarts.
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

PROJECT_ROOT = Path(__file__).resolve().parents[1]

MASKED_DIFF_PATH = "/datasets/orders/versions/1/snapshots/at/diff/masked"
AT_DIFF_PATH = "/datasets/orders/versions/1/snapshots/at/diff"
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


def after(snapshot: dict, **delta) -> str:
    return (
        datetime.fromisoformat(snapshot["created_at"]) + timedelta(**delta)
    ).isoformat()


def masked_diff(
    client: TestClient,
    role: str,
    from_ts: str,
    to_ts: str,
    *,
    path: str = MASKED_DIFF_PATH,
):
    return client.post(path, json={"role": role, "from": from_ts, "to": to_ts})


# --------------------------------------------------------------------------- #
# Selection and masked diff output
# --------------------------------------------------------------------------- #


def test_masked_diff_compares_raw_rows_and_masks_entry_rows(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
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
        {"id": 2, "email": None, "ssn": None},
    ])
    time.sleep(0.01)
    second = make_snapshot(client, [
        {"id": 1, "email": "alice@example.com", "ssn": "111-22-3333"},
        {"id": 3, "email": "bob@example.com", "ssn": "222-33-4444", "age": 40},
    ])

    response = masked_diff(
        client, "guest", after(first), after(second, seconds=1)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # The bare time diff's document shape and key order, echoed timestamps.
    assert list(body) == [
        "from_timestamp", "to_timestamp", "from_snapshot_id",
        "to_snapshot_id", "added", "removed", "fields_added", "fields_removed",
    ]
    assert body["from_snapshot_id"] == first["id"]
    assert body["to_snapshot_id"] == second["id"]
    # The comparison ran on the raw rows; masking rewrote only the entry rows
    # (partial email, redacted ssn, nulls and uncovered fields untouched).
    assert body["added"] == [
        {"row": {"id": 3, "email": "bom", "ssn": "***", "age": 40},
         "count": 1}
    ]
    assert body["removed"] == [
        {"row": {"id": 2, "email": None, "ssn": None}, "count": 1}
    ]
    assert body["fields_added"] == ["age"]
    assert body["fields_removed"] == []


def test_masked_diff_counts_duplicates_without_merging_entries(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    first = make_snapshot(client, [
        {"id": 1, "email": "alice@example.com"},
    ])
    time.sleep(0.01)
    second = make_snapshot(client, [
        {"id": 1, "email": "alice@example.com"},
        {"id": 1, "email": "alice@example.com"},
        {"id": 1, "email": "alice@example.com"},
    ])

    response = masked_diff(client, "guest", after(first), after(second))
    assert response.status_code == 200, response.text
    body = response.json()
    # Two extra occurrences of the same raw row: one entry, count preserved.
    assert body["added"] == [
        {"row": {"id": 1, "email": "***"}, "count": 2}
    ]
    assert body["removed"] == []


def test_masked_diff_entries_sort_by_raw_row_text(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    first = make_snapshot(client, [])
    time.sleep(0.01)
    second = make_snapshot(client, [
        {"id": 2, "email": "zoe@example.com"},
        {"id": 1, "email": "amy@example.com"},
    ])

    response = masked_diff(client, "guest", after(first), after(second))
    assert response.status_code == 200, response.text
    # Masked to the same "***" value, the entries would look alike; the order
    # still follows the canonical text of the raw rows (id 1 before id 2).
    assert [entry["row"]["id"] for entry in response.json()["added"]] == [1, 2]


def test_masked_diff_response_is_compact_with_one_trailing_newline(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    snapshot = make_snapshot(client, [{"id": 1, "email": "a@b.c"}])

    response = masked_diff(client, "guest", after(snapshot), after(snapshot))
    assert response.status_code == 200, response.text
    text = response.text
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert text[:-1] == json.dumps(
        json.loads(text), separators=(",", ":"), ensure_ascii=False
    )


def test_masked_diff_same_snapshot_yields_empty_diff(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    snapshot = make_snapshot(client, [{"id": 1, "email": "alice@example.com"}])

    response = masked_diff(client, "guest", after(snapshot), after(snapshot))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["from_snapshot_id"] == snapshot["id"]
    assert body["to_snapshot_id"] == snapshot["id"]
    assert body["added"] == [] and body["removed"] == []
    assert body["fields_added"] == [] and body["fields_removed"] == []


def test_masked_diff_allowed_role_sees_unmasked_rows(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": ["analyst"]},
    )
    first = make_snapshot(client, [])
    time.sleep(0.01)
    second = make_snapshot(client, [{"id": 1, "email": "alice@example.com"}])

    response = masked_diff(client, "analyst", after(first), after(second))
    assert response.status_code == 200, response.text
    assert response.json()["added"] == [
        {"row": {"id": 1, "email": "alice@example.com"}, "count": 1}
    ]


def test_masked_diff_matches_bare_diff_before_masking(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    first = make_snapshot(client, [
        {"id": 1, "email": "alice@example.com"}, {"id": 2, "email": None},
    ])
    time.sleep(0.01)
    second = make_snapshot(client, [
        {"id": 2, "email": None}, {"id": 3, "email": "bob@example.com"},
    ])
    from_ts, to_ts = after(first), after(second)

    bare = client.get(AT_DIFF_PATH, params={"from": from_ts, "to": to_ts})
    assert bare.status_code == 200, bare.text
    masked = masked_diff(client, "guest", from_ts, to_ts)
    assert masked.status_code == 200, masked.text
    bare_body, masked_body = bare.json(), masked.json()
    # Same snapshots, same counts and same field sets; only the entry rows
    # differ (masked), and only where a policy covers a non-null value.
    for key in (
        "from_snapshot_id", "to_snapshot_id", "fields_added", "fields_removed"
    ):
        assert masked_body[key] == bare_body[key]
    for side in ("added", "removed"):
        assert [e["count"] for e in masked_body[side]] == [
            e["count"] for e in bare_body[side]
        ]
    assert masked_body["added"][0]["row"] == {"id": 3, "email": "***"}
    assert masked_body["removed"][0]["row"] == {"id": 1, "email": "***"}


# --------------------------------------------------------------------------- #
# Audit trail
# --------------------------------------------------------------------------- #


def test_masked_diff_writes_hits_per_occurrence_across_both_sides(
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
        {"id": 2, "email": None, "ssn": None},
    ])
    time.sleep(0.01)
    second = make_snapshot(client, [
        {"id": 1, "email": "alice@example.com", "ssn": "111"},
        {"id": 3, "email": "bob@example.com"},
    ])

    response = masked_diff(client, "guest", after(first), after(second))
    assert response.status_code == 200, response.text

    hits = client.get(AUDIT_PATH).json()
    # Every masked value hits once per occurrence across both snapshots:
    # baseline (email + ssn, nulls never hit) then target (email + ssn +
    # email), in row order.
    assert [(hit["field"], hit["masking"]) for hit in hits] == [
        ("email", "partial"), ("ssn", "redact"),
        ("email", "partial"), ("ssn", "redact"), ("email", "partial"),
    ]
    assert [hit["sequence"] for hit in hits] == [1, 2, 3, 4, 5]
    assert [hit["policy_id"] for hit in hits] == [
        email_policy["id"], ssn_policy["id"],
        email_policy["id"], ssn_policy["id"], email_policy["id"],
    ]
    assert {hit["role"] for hit in hits} == {"guest"}
    assert len({hit["created_at"] for hit in hits}) == 1
    for hit in hits:
        assert set(hit) == {
            "sequence", "field", "policy_id", "role", "masking", "created_at"
        }

    access = client.get(ACCESS_PATH).json()
    assert len(access) == 1
    record = access[0]
    assert record["role"] == "guest"
    # The row count is the two sides' combined row count; the masked count
    # equals the number of hit records.
    assert record["row_count"] == 2 + 2
    assert record["masked_count"] == 5
    assert record["created_at"] == hits[0]["created_at"]


def test_masked_diff_without_hits_leaves_only_an_access_record(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": ["analyst"]},
    )
    first = make_snapshot(client, [{"id": 1, "email": "alice@example.com"}])
    time.sleep(0.01)
    second = make_snapshot(client, [{"id": 2, "email": None}])

    response = masked_diff(client, "analyst", after(first), after(second))
    assert response.status_code == 200, response.text
    assert client.get(AUDIT_PATH).json() == []
    access = client.get(ACCESS_PATH).json()
    assert len(access) == 1
    assert access[0]["row_count"] == 2
    assert access[0]["masked_count"] == 0


def test_masked_diff_trail_continues_the_existing_sequence_runs(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    snapshot = make_snapshot(client, [{"id": 1, "email": "alice@example.com"}])
    view = client.post(
        "/datasets/orders/versions/1/privacy-policies/view",
        json={"role": "guest", "rows": [{"email": "carol@example.com"}]},
    )
    assert view.status_code == 200, view.text

    response = masked_diff(client, "guest", after(snapshot), after(snapshot))
    assert response.status_code == 200, response.text

    hits = client.get(AUDIT_PATH).json()
    # One hit from the regular view, then two from the diff (one per side).
    assert [hit["sequence"] for hit in hits] == [1, 2, 3]
    access = client.get(ACCESS_PATH).json()
    assert [record["sequence"] for record in access] == [1, 2]
    assert access[1]["row_count"] == 2
    assert access[1]["masked_count"] == 2


# --------------------------------------------------------------------------- #
# 404/422 precedence and request shape
# --------------------------------------------------------------------------- #


def test_unknown_dataset_or_version_is_404_before_shape_checks(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    for path in (
        "/datasets/unknown/versions/1/snapshots/at/diff/masked",
        "/datasets/orders/versions/9/snapshots/at/diff/masked",
    ):
        response = client.post(path, content=b"not json")
        assert response.status_code == 404, response.text
        assert set(response.json()) == {"error", "detail"}


def test_missing_snapshot_on_either_side_is_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    before = (
        datetime.fromisoformat(snapshot["created_at"]) - timedelta(days=1)
    ).isoformat()
    future = after(snapshot, days=1)

    for from_ts, to_ts in ((before, future), (future, before), (before, before)):
        response = masked_diff(client, "guest", from_ts, to_ts)
        assert response.status_code == 404, response.text
    assert client.get(AUDIT_PATH).json() == []
    assert client.get(ACCESS_PATH).json() == []


def test_missing_snapshot_404_precedes_remaining_shape_checks(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    before = (
        datetime.fromisoformat(snapshot["created_at"]) - timedelta(days=1)
    ).isoformat()
    future = after(snapshot, days=1)

    # A usable 'from' with no snapshot wins over a blank role, an extra field
    # and a query parameter.
    response = client.post(
        MASKED_DIFF_PATH + "?x=1",
        json={"role": " ", "from": before, "to": future, "extra": 1},
    )
    assert response.status_code == 404, response.text
    # Same for a usable 'to'.
    response = client.post(
        MASKED_DIFF_PATH,
        json={"role": " ", "from": future, "to": before},
    )
    assert response.status_code == 404, response.text


def test_shape_problems_are_422_and_write_nothing(client: TestClient) -> None:
    make_dataset_with_version(client)
    snapshot = make_snapshot(client, [{"id": 1, "email": "a@b.c"}])
    future = after(snapshot, days=1)

    bad_bodies = [
        b"",  # empty
        b"   \n\t ",  # whitespace only
        b"not json",  # invalid JSON
        b"[1, 2]",  # non-object
        json.dumps({"from": future, "to": future}).encode(),  # missing role
        json.dumps({"role": "guest", "to": future}).encode(),  # missing from
        json.dumps({"role": "guest", "from": future}).encode(),  # missing to
        json.dumps({"role": 1, "from": future, "to": future}).encode(),
        json.dumps({"role": "  ", "from": future, "to": future}).encode(),
        json.dumps({"role": "guest", "from": 1, "to": future}).encode(),
        json.dumps({"role": "guest", "from": "soon", "to": future}).encode(),
        json.dumps({"role": "guest", "from": future, "to": "soon"}).encode(),
        # timezone-less timestamps
        json.dumps({"role": "guest", "from": future[:19], "to": future}).encode(),
        json.dumps({"role": "guest", "from": future, "to": future[:19]}).encode(),
        # extra field
        json.dumps(
            {"role": "guest", "from": future, "to": future, "x": 1}
        ).encode(),
    ]
    for body in bad_bodies:
        response = client.post(MASKED_DIFF_PATH, content=body)
        assert response.status_code == 422, (body, response.text)
        assert set(response.json()) == {"error", "detail"}

    # Any query parameter is a 422 too.
    response = client.post(
        MASKED_DIFF_PATH + "?from=" + future,
        json={"role": "guest", "from": future, "to": future},
    )
    assert response.status_code == 422, response.text

    assert client.get(AUDIT_PATH).json() == []
    assert client.get(ACCESS_PATH).json() == []


def test_masked_diff_accepts_z_and_offset_timestamps(client: TestClient) -> None:
    make_dataset_with_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    future = datetime.fromisoformat(snapshot["created_at"]) + timedelta(hours=1)

    z_value = future.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    offset_value = future.astimezone(
        timezone(timedelta(hours=5, minutes=30))
    ).isoformat()
    response = masked_diff(client, "guest", z_value, offset_value)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["from_timestamp"] == z_value
    assert body["to_timestamp"] == offset_value
    assert body["from_snapshot_id"] == snapshot["id"]
    assert body["to_snapshot_id"] == snapshot["id"]


# --------------------------------------------------------------------------- #
# Immutability and persistence
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

    response = masked_diff(client, "guest", after(snapshot), after(snapshot))
    assert response.status_code == 200, response.text

    stored = client.get(f"{SNAPSHOTS_PATH}/{snapshot['id']}").json()
    assert stored["rows"] == rows
    policies = client.get(POLICIES_PATH).json()
    assert len(policies) == 1 and policies[0]["id"] == policy["id"]
    assert policies[0]["enabled"] is True


def test_masked_diff_result_and_trail_survive_a_restart(
    client: TestClient, tmp_path: Path
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": []},
    )
    first = make_snapshot(client, [{"id": 1, "email": "alice@example.com"}])
    time.sleep(0.01)
    second = make_snapshot(
        client, [{"id": 1, "email": "alice@example.com"}, {"id": 2}]
    )
    from_ts, to_ts = after(first), after(second)

    response = masked_diff(client, "guest", from_ts, to_ts)
    assert response.status_code == 200, response.text
    expected_body = response.text
    expected_hits = client.get(AUDIT_PATH).json()
    expected_access = client.get(ACCESS_PATH).json()

    db_path = os.environ["DATA_LINEAGE_DB"]
    script = (
        "import json, sys\n"
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "client = TestClient(app)\n"
        "path = sys.argv[1]\n"
        "resp = client.post(path, json={'role': 'guest', "
        "'from': sys.argv[2], 'to': sys.argv[3]})\n"
        "print(resp.status_code)\n"
        "print(resp.text, end='')\n"
        "print(json.dumps(client.get("
        "'/datasets/orders/versions/1/privacy-policies/view/audit-records'"
        ").json()))\n"
        "print(json.dumps(client.get("
        "'/datasets/orders/versions/1/privacy-policies/view/access-records'"
        ").json()))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, MASKED_DIFF_PATH, from_ts, to_ts],
        capture_output=True,
        text=True,
        env={**os.environ, "DATA_LINEAGE_DB": db_path},
        cwd=PROJECT_ROOT,
    )
    assert result.returncode == 0, result.stderr
    status_line, body, hits, access = result.stdout.splitlines()
    assert status_line == "200"
    # The diff is recomputed identically and the trail reads back as written,
    # with the restart's own read appended on top, sequences continuous.
    assert body + "\n" == expected_body
    assert json.loads(hits)[:2] == expected_hits
    assert json.loads(access)[:1] == expected_access
    assert [hit["sequence"] for hit in json.loads(hits)] == [1, 2, 3, 4]
    assert [record["sequence"] for record in json.loads(access)] == [1, 2]

    # The database file itself holds the records (no in-memory state).
    conn = sqlite3.connect(db_path)
    try:
        hit_count = conn.execute(
            "SELECT COUNT(*) FROM privacy_view_audit_records"
        ).fetchone()[0]
        access_count = conn.execute(
            "SELECT COUNT(*) FROM privacy_view_access_records"
        ).fetchone()[0]
    finally:
        conn.close()
    assert hit_count == 4
    assert access_count == 2
