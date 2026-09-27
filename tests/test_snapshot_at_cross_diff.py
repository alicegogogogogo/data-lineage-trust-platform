"""Tests for the cross-version snapshot time diff endpoint.

The endpoint is ``GET /datasets/{dataset}/snapshots/at/cross-diff`` with
query parameters ``from_version``, ``from``, ``to_version`` and ``to``: each
side names a schema version and a timestamp, and the newest snapshot of that
version created not later than the timestamp is compared. Field-definition
changes and the projected row multiset reuse the cross-version snapshot-id
comparison exactly. These tests cover selection, direction, field-change and
multiset semantics, deterministic serialization, 404/422 precedence,
read-only behavior and persistence across restarts.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

CROSS_DIFF_PATH = "/datasets/orders/snapshots/at/cross-diff"


def create_dataset(client: TestClient, name: str = "orders") -> None:
    response = client.post("/datasets", json={"name": name})
    assert response.status_code == 201, response.text


def create_version(client: TestClient, fields: list[dict], dataset: str = "orders") -> int:
    response = client.post(
        f"/datasets/{dataset}/versions", json={"fields": fields}
    )
    assert response.status_code == 201, response.text
    return response.json()["version"]


def create_snapshot(
    client: TestClient, version: int, rows: list, dataset: str = "orders"
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots",
        json={"rows": rows},
    )
    assert response.status_code == 201, response.text
    return response.json()


def cross_diff(client: TestClient, *, dataset: str = "orders", **kwargs):
    return client.request(
        "GET",
        f"/datasets/{dataset}/snapshots/at/cross-diff",
        **kwargs,
    )


def _shift(iso: str, **delta) -> str:
    return (datetime.fromisoformat(iso) + timedelta(**delta)).isoformat()


def _two_versions_with_snapshots(client: TestClient):
    """Version 1 -> version 2 covering every field-change kind.

    v1 fields: id (integer, not null), name (string, nullable), age
    (integer, nullable), v1_only (string, nullable)
    v2 fields: id, name (now not null), age (now string, nullable),
    v2_only (string, nullable)
    """
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "name", "type": "string", "nullable": True},
        {"name": "age", "type": "integer", "nullable": True},
        {"name": "v1_only", "type": "string", "nullable": True},
    ])
    v2 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "name", "type": "string", "nullable": False},
        {"name": "age", "type": "string", "nullable": True},
        {"name": "v2_only", "type": "string", "nullable": True},
    ])
    base = create_snapshot(client, v1, [{"id": 1, "name": "a", "age": 3}])
    target = create_snapshot(client, v2, [{"id": 1, "name": "a", "age": "3"}])
    return v1, v2, base, target


def _query(base: dict, target: dict, v1: int, v2: int) -> dict:
    return {
        "from_version": v1,
        "from": base["created_at"],
        "to_version": v2,
        "to": target["created_at"],
    }


# --------------------------------------------------------------------------- #
# Snapshot selection and direction
# --------------------------------------------------------------------------- #


def test_each_side_selects_the_newest_snapshot_of_its_version_at_its_time(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    v2 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "label", "type": "string", "nullable": True},
    ])
    v1_first = create_snapshot(client, v1, [{"id": 1}])
    v1_second = create_snapshot(client, v1, [{"id": 2}])
    v2_first = create_snapshot(client, v2, [{"id": 1, "label": "a"}])
    v2_second = create_snapshot(client, v2, [{"id": 2, "label": "b"}])

    # Pin the creation times so the selection windows are deterministic.
    stamps = {
        v1_first["id"]: "2024-01-01T00:00:00+00:00",
        v1_second["id"]: "2024-01-03T00:00:00+00:00",
        v2_first["id"]: "2024-01-02T00:00:00+00:00",
        v2_second["id"]: "2024-01-04T00:00:00+00:00",
    }
    conn = sqlite3.connect(str(isolated_database))
    try:
        for snapshot_id, created_at in stamps.items():
            conn.execute(
                "UPDATE snapshots SET created_at = ? WHERE id = ?",
                (created_at, snapshot_id),
            )
        conn.commit()
    finally:
        conn.close()

    # The from time falls between the two v1 snapshots: the older one is
    # selected even though a newer v1 snapshot and newer v2 snapshots exist.
    response = cross_diff(client, params={
        "from_version": v1,
        "from": "2024-01-02T12:00:00+00:00",
        "to_version": v2,
        "to": "2024-01-05T00:00:00+00:00",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["from_snapshot_id"] == v1_first["id"]
    assert body["to_snapshot_id"] == v2_second["id"]

    # A boundary instant selects the snapshot created exactly at it, and the
    # other version's snapshots never leak into this side's selection.
    response = cross_diff(client, params={
        "from_version": v1,
        "from": "2024-01-03T00:00:00+00:00",
        "to_version": v2,
        "to": "2024-01-02T00:00:00+00:00",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["from_snapshot_id"] == v1_second["id"]
    assert body["to_snapshot_id"] == v2_first["id"]


def test_target_time_may_precede_base_time_and_either_version_order_works(
    client: TestClient,
) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "name", "type": "string", "nullable": True},
    ])
    v2 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "label", "type": "string", "nullable": True},
    ])
    snap_v1 = create_snapshot(client, v1, [{"id": 1, "name": "a"}])
    snap_v2 = create_snapshot(client, v2, [{"id": 2, "label": "b"}])

    # The higher version is the baseline and its (later) timestamp is the
    # from side; the target time is earlier than the base time.
    response = cross_diff(client, params={
        "from_version": v2,
        "from": snap_v2["created_at"],
        "to_version": v1,
        "to": snap_v1["created_at"],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["from_version"] == v2
    assert body["to_version"] == v1
    assert body["from_snapshot_id"] == snap_v2["id"]
    assert body["to_snapshot_id"] == snap_v1["id"]
    assert body["from_timestamp"] == snap_v2["created_at"]
    assert body["to_timestamp"] == snap_v1["created_at"]
    assert body["field_changes"] == [
        {
            "field": "label",
            "kind": "removed",
            "before": {"type": "string", "nullable": True},
            "after": None,
        },
        {
            "field": "name",
            "kind": "added",
            "before": None,
            "after": {"type": "string", "nullable": True},
        },
    ]
    assert body["added"] == [{"row": {"id": 1}, "count": 1}]
    assert body["removed"] == [{"row": {"id": 2}, "count": 1}]


# --------------------------------------------------------------------------- #
# Field changes and row multiset semantics
# --------------------------------------------------------------------------- #


def test_field_changes_reuse_the_cross_version_vocabulary(
    client: TestClient,
) -> None:
    v1, v2, base, target = _two_versions_with_snapshots(client)

    response = cross_diff(client, params=_query(base, target, v1, v2))
    assert response.status_code == 200, response.text
    body = response.json()
    by_field = {change["field"]: change for change in body["field_changes"]}
    assert [change["field"] for change in body["field_changes"]] == [
        "age", "name", "v1_only", "v2_only"
    ]
    assert by_field["age"]["kind"] == "type_changed"
    assert by_field["name"]["kind"] == "nullable_tightened"
    assert by_field["v1_only"]["kind"] == "removed"
    assert by_field["v2_only"]["kind"] == "added"
    assert "id" not in by_field
    assert by_field["v1_only"]["before"] == {"type": "string", "nullable": True}
    assert by_field["v1_only"]["after"] is None
    assert by_field["v2_only"]["before"] is None
    assert by_field["v2_only"]["after"] == {"type": "string", "nullable": True}
    for change in body["field_changes"]:
        assert set(change) == {"field", "kind", "before", "after"}


def test_rows_project_onto_common_fields_and_compare_as_a_multiset(
    client: TestClient,
) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "name", "type": "string", "nullable": True},
        {"name": "old", "type": "string", "nullable": True},
    ])
    v2 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "name", "type": "string", "nullable": True},
        {"name": "new", "type": "string", "nullable": True},
    ])
    base = create_snapshot(client, v1, [
        {"id": 1, "name": "a", "old": "x", "rogue": True},
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b"},
    ])
    target = create_snapshot(client, v2, [
        {"name": "a", "id": 1, "new": "y", "rogue": False},
        {"id": 3, "name": "c"},
        {"id": 3, "name": "c"},
    ])

    body = cross_diff(client, params=_query(base, target, v1, v2)).json()
    # Only {id, name} participates; key order is irrelevant and duplicates
    # count. {id:1,name:a} is twice on the baseline, once on the target.
    assert body["removed"] == [
        {"row": {"id": 1, "name": "a"}, "count": 1},
        {"row": {"id": 2, "name": "b"}, "count": 1},
    ]
    assert body["added"] == [{"row": {"id": 3, "name": "c"}, "count": 2}]


def test_added_and_removed_sort_by_canonical_row_text(client: TestClient) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    v2 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "extra", "type": "string", "nullable": True},
    ])
    base = create_snapshot(client, v1, [{"id": 10}, {"id": 2}, {"id": 1}])
    target = create_snapshot(client, v2, [{"id": 3}, {"id": 20}])

    body = cross_diff(client, params=_query(base, target, v1, v2)).json()
    # Canonical text sort: {"id":10} < {"id":1} < {"id":2} as JSON text
    # ("0" sorts before "}").
    assert [entry["row"] for entry in body["removed"]] == [
        {"id": 10}, {"id": 1}, {"id": 2}
    ]
    assert [entry["row"] for entry in body["added"]] == [{"id": 20}, {"id": 3}]


# --------------------------------------------------------------------------- #
# Top-level document and deterministic serialization
# --------------------------------------------------------------------------- #


def test_document_is_compact_with_fixed_key_order_and_one_newline(
    client: TestClient,
) -> None:
    v1, v2, base, target = _two_versions_with_snapshots(client)

    response = cross_diff(client, params=_query(base, target, v1, v2))
    assert response.status_code == 200
    raw = response.content
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")
    assert b": " not in raw
    assert b", " not in raw
    body = response.json()
    assert list(body) == [
        "from_timestamp",
        "to_timestamp",
        "from_snapshot_id",
        "to_snapshot_id",
        "from_version",
        "to_version",
        "field_changes",
        "added",
        "removed",
    ]
    assert list(body["field_changes"][0]) == ["field", "kind", "before", "after"]
    # The timestamps echo the submitted query values verbatim.
    assert body["from_timestamp"] == base["created_at"]
    assert body["to_timestamp"] == target["created_at"]


# --------------------------------------------------------------------------- #
# 404s and 422s
# --------------------------------------------------------------------------- #


def test_same_version_on_both_sides_is_422(client: TestClient) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    snapshot = create_snapshot(client, v1, [{"id": 1}])

    response = cross_diff(client, params={
        "from_version": v1,
        "from": snapshot["created_at"],
        "to_version": v1,
        "to": snapshot["created_at"],
    })
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert "SQLite" not in response.text


def test_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    v1, v2, base, target = _two_versions_with_snapshots(client)

    assert cross_diff(
        client, dataset="ghost", params=_query(base, target, v1, v2)
    ).status_code == 404
    unknown = v2 + 100
    response = cross_diff(client, params={
        "from_version": unknown,
        "from": base["created_at"],
        "to_version": v2,
        "to": target["created_at"],
    })
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    response = cross_diff(client, params={
        "from_version": v1,
        "from": base["created_at"],
        "to_version": unknown,
        "to": target["created_at"],
    })
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_404_precedes_parameter_shape_checks(client: TestClient) -> None:
    # Unknown dataset with every parameter missing and a body: 404 wins.
    response = client.request(
        "GET",
        "/datasets/ghost/snapshots/at/cross-diff",
        content=b"{}",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    # Unknown version with an extra query parameter and a body: still 404.
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    snapshot = create_snapshot(client, v1, [{"id": 1}])
    response = client.request(
        "GET",
        CROSS_DIFF_PATH,
        params={
            "from_version": v1,
            "from": snapshot["created_at"],
            "to_version": v1 + 100,
            "to": snapshot["created_at"],
            "expand": 1,
        },
        content=b"   ",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_missing_repeated_and_invalid_parameters_are_422(client: TestClient) -> None:
    v1, v2, base, target = _two_versions_with_snapshots(client)
    good = _query(base, target, v1, v2)

    # Each parameter is required.
    for key in ("from_version", "from", "to_version", "to"):
        params = {k: v for k, v in good.items() if k != key}
        response = cross_diff(client, params=params)
        assert response.status_code == 422, key
        assert response.json()["error"] == "validation_error"

    # Blank values are missing values.
    for key in good:
        response = cross_diff(client, params={**good, key: ""})
        assert response.status_code == 422, key

    # Repetition of any parameter is a shape error.
    for key in good:
        response = cross_diff(
            client,
            params=[*good.items(), (key, good[key])],
        )
        assert response.status_code == 422, key

    # Version numbers must be positive integers.
    for bad in ("abc", "1.5", "1e2", "+1", " 1", "0", "-1"):
        response = cross_diff(client, params={**good, "from_version": bad})
        assert response.status_code == 422, bad
        response = cross_diff(client, params={**good, "to_version": bad})
        assert response.status_code == 422, bad

    # Timestamps must be ISO-8601 date-times carrying a timezone.
    for bad in ("not-a-time", "2024-01-01", "2024-01-01T00:00:00"):
        response = cross_diff(client, params={**good, "from": bad})
        assert response.status_code == 422, bad
        response = cross_diff(client, params={**good, "to": bad})
        assert response.status_code == 422, bad

    # Any other query parameter is a shape error.
    response = cross_diff(client, params={**good, "expand": 1})
    assert response.status_code == 422
    assert "SQLite" not in response.text


def test_any_request_body_is_422(client: TestClient) -> None:
    v1, v2, base, target = _two_versions_with_snapshots(client)

    for content in (b"{}", b"   ", b"\t\n", b"not json"):
        response = client.request(
            "GET",
            CROSS_DIFF_PATH,
            params=_query(base, target, v1, v2),
            content=content,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422, content
        assert response.json()["error"] == "validation_error"


def test_side_without_a_snapshot_at_its_time_is_404(client: TestClient) -> None:
    v1, v2, base, target = _two_versions_with_snapshots(client)

    before_base = _shift(base["created_at"], days=-1)
    response = cross_diff(client, params={
        "from_version": v1,
        "from": before_base,
        "to_version": v2,
        "to": target["created_at"],
    })
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    before_target = _shift(target["created_at"], days=-1)
    response = cross_diff(client, params={
        "from_version": v1,
        "from": base["created_at"],
        "to_version": v2,
        "to": before_target,
    })
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_post_is_not_accepted(client: TestClient) -> None:
    v1, v2, base, target = _two_versions_with_snapshots(client)
    response = client.post(CROSS_DIFF_PATH, params=_query(base, target, v1, v2))
    assert response.status_code == 405


# --------------------------------------------------------------------------- #
# Read-only behavior and persistence across restarts
# --------------------------------------------------------------------------- #


def test_comparison_is_read_only(client: TestClient) -> None:
    v1, v2, base, target = _two_versions_with_snapshots(client)

    first = cross_diff(client, params=_query(base, target, v1, v2))
    assert first.status_code == 200
    second = cross_diff(client, params=_query(base, target, v1, v2))
    assert second.content == first.content

    # Snapshots and their rows are untouched.
    assert client.get(
        f"/datasets/orders/versions/{v1}/snapshots/{base['id']}"
    ).json()["rows"] == [{"id": 1, "name": "a", "age": 3}]
    assert client.get(
        f"/datasets/orders/versions/{v2}/snapshots/{target['id']}"
    ).json()["rows"] == [{"id": 1, "name": "a", "age": "3"}]
    # No privacy trail of any kind is written.
    assert client.get(
        f"/datasets/orders/versions/{v1}/privacy-policies/view/access-records"
    ).json() == []
    assert client.get(
        f"/datasets/orders/versions/{v2}/privacy-policies/view/audit-records"
    ).json() == []


CREATE_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

assert client.post("/datasets", json={"name": "orders"}).status_code == 201
v1 = client.post("/datasets/orders/versions", json={"fields": [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "name", "type": "string", "nullable": True},
]}).json()["version"]
v2 = client.post("/datasets/orders/versions", json={"fields": [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "label", "type": "string", "nullable": False},
]}).json()["version"]
base = client.post(
    f"/datasets/orders/versions/{v1}/snapshots",
    json={"rows": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]},
).json()
target = client.post(
    f"/datasets/orders/versions/{v2}/snapshots",
    json={"rows": [{"id": 1, "label": "a"}, {"id": 3, "label": "c"}]},
).json()
response = client.get(
    "/datasets/orders/snapshots/at/cross-diff",
    params={
        "from_version": v1,
        "from": base["created_at"],
        "to_version": v2,
        "to": target["created_at"],
    },
)
assert response.status_code == 200, response.text
print(json.dumps({
    "v1": v1,
    "v2": v2,
    "base_created_at": base["created_at"],
    "target_created_at": target["created_at"],
    "document": response.text,
}))
"""

VERIFY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
state = json.loads(input())
response = client.get(
    "/datasets/orders/snapshots/at/cross-diff",
    params={
        "from_version": state["v1"],
        "from": state["base_created_at"],
        "to_version": state["v2"],
        "to": state["target_created_at"],
    },
)
assert response.status_code == 200, response.text
# The comparison recomputed after the restart is byte-identical to the one
# computed when the snapshots were first written.
assert response.text == state["document"], (response.text, state["document"])
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


def test_comparison_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "cross-version-at-diff.db"
    state = _run(db_path, CREATE_SCRIPT)
    assert _run(db_path, VERIFY_SCRIPT, stdin=state) == "verified"
