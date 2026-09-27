"""Tests for the time-travel cross-version snapshot comparison endpoint.

The endpoint is ``GET /datasets/{dataset}/snapshots/at/cross-diff``: each of
the ``from``/``to`` sides names a schema version (``from_version``/
``to_version``) and a timestamp, selects the newest snapshot of that version
created not later than it, and the two selected snapshots are compared with
the exact field-change and row-multiset semantics of the snapshot-id
cross-version comparison. These tests cover snapshot selection, direction,
field changes, projection and multiset semantics, deterministic
serialization, 404/422 precedence, read-only behavior and persistence across
restarts.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
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


def compare(client: TestClient, *, dataset: str = "orders", **kwargs):
    return client.request(
        "GET",
        f"/datasets/{dataset}/snapshots/at/cross-diff",
        **kwargs,
    )


def compare_params(
    client: TestClient,
    from_version: int,
    from_timestamp: str,
    to_version: int,
    to_timestamp: str,
    *,
    dataset: str = "orders",
):
    return compare(
        client,
        dataset=dataset,
        params={
            "from_version": from_version,
            "from": from_timestamp,
            "to_version": to_version,
            "to": to_timestamp,
        },
    )


def _future(iso: str, days: int = 1) -> str:
    return (datetime.fromisoformat(iso) + timedelta(days=days)).isoformat()


def _past(iso: str, days: int = 1) -> str:
    return (datetime.fromisoformat(iso) - timedelta(days=days)).isoformat()


def _two_versions_with_snapshots(client: TestClient):
    """Version 1 -> version 2 covering every field-change kind.

    v1 fields: id (integer, not null), name (string, nullable), age
    (integer, nullable), code (string, not null), v1_only (string, nullable)
    v2 fields: id, name (now not null), age (now string, nullable), code
    (string, nullable -> loosened), v2_only (string, nullable)
    """
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "name", "type": "string", "nullable": True},
        {"name": "age", "type": "integer", "nullable": True},
        {"name": "code", "type": "string", "nullable": False},
        {"name": "v1_only", "type": "string", "nullable": True},
    ])
    v2 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "name", "type": "string", "nullable": False},
        {"name": "age", "type": "string", "nullable": True},
        {"name": "code", "type": "string", "nullable": True},
        {"name": "v2_only", "type": "string", "nullable": True},
    ])
    base = create_snapshot(client, v1, [{"id": 1}])
    target = create_snapshot(client, v2, [{"id": 1}])
    return v1, v2, base, target


# --------------------------------------------------------------------------- #
# Snapshot selection and direction
# --------------------------------------------------------------------------- #


def test_each_side_selects_the_newest_snapshot_at_or_before_its_time(
    client: TestClient,
) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    v2 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    first_v1 = create_snapshot(client, v1, [{"id": 1}])
    time.sleep(0.01)
    second_v1 = create_snapshot(client, v1, [{"id": 2}])
    first_v2 = create_snapshot(client, v2, [{"id": 3}])
    time.sleep(0.01)
    second_v2 = create_snapshot(client, v2, [{"id": 4}])

    # Each timestamp lands between the two snapshots of its version, so each
    # side selects the earlier one.
    middle_v1 = (
        datetime.fromisoformat(first_v1["created_at"]) + timedelta(milliseconds=1)
    ).isoformat()
    assert middle_v1 < second_v1["created_at"]
    middle_v2 = (
        datetime.fromisoformat(first_v2["created_at"]) + timedelta(milliseconds=1)
    ).isoformat()
    assert middle_v2 < second_v2["created_at"]

    body = compare_params(
        client, v1, middle_v1, v2, middle_v2
    ).json()
    assert body["from_snapshot_id"] == first_v1["id"]
    assert body["to_snapshot_id"] == first_v2["id"]
    assert body["added"] == [{"row": {"id": 3}, "count": 1}]
    assert body["removed"] == [{"row": {"id": 1}, "count": 1}]

    # A timestamp at or after the second snapshot selects it instead.
    body = compare_params(
        client, v1, second_v1["created_at"], v2, _future(second_v2["created_at"])
    ).json()
    assert body["from_snapshot_id"] == second_v1["id"]
    assert body["to_snapshot_id"] == second_v2["id"]


def test_to_time_may_be_earlier_than_from_time(client: TestClient) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    v2 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    base = create_snapshot(client, v1, [{"id": 1}])
    time.sleep(0.01)
    target = create_snapshot(client, v2, [{"id": 2}])

    # The to timestamp is earlier than the from timestamp; both sides still
    # resolve independently.
    response = compare_params(
        client, v1, _future(base["created_at"]), v2, target["created_at"]
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["from_snapshot_id"] == base["id"]
    assert body["to_snapshot_id"] == target["id"]


def test_either_version_order_is_accepted_and_direction_is_kept(
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
    base = create_snapshot(client, v1, [{"id": 1, "name": "a"}])
    target = create_snapshot(client, v2, [{"id": 2, "label": "b"}])

    forward = compare_params(
        client, v1, base["created_at"], v2, target["created_at"]
    ).json()
    assert forward["from_version"] == v1
    assert forward["to_version"] == v2
    assert forward["field_changes"] == [
        {
            "field": "label",
            "kind": "added",
            "before": None,
            "after": {"type": "string", "nullable": True},
        },
        {
            "field": "name",
            "kind": "removed",
            "before": {"type": "string", "nullable": True},
            "after": None,
        },
    ]
    assert forward["added"] == [{"row": {"id": 2}, "count": 1}]
    assert forward["removed"] == [{"row": {"id": 1}, "count": 1}]

    # The higher version number may be the baseline; sides, field changes and
    # row sets invert.
    reverse = compare_params(
        client, v2, target["created_at"], v1, base["created_at"]
    ).json()
    assert reverse["from_version"] == v2
    assert reverse["to_version"] == v1
    assert reverse["from_snapshot_id"] == target["id"]
    assert reverse["to_snapshot_id"] == base["id"]
    assert reverse["added"] == forward["removed"]
    assert reverse["removed"] == forward["added"]
    assert reverse["field_changes"] == [
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


# --------------------------------------------------------------------------- #
# Field-definition changes and row comparison
# --------------------------------------------------------------------------- #


def test_field_changes_match_the_snapshot_id_cross_comparison(
    client: TestClient,
) -> None:
    v1, v2, base, target = _two_versions_with_snapshots(client)

    body = compare_params(
        client, v1, base["created_at"], v2, target["created_at"]
    ).json()
    assert [change["field"] for change in body["field_changes"]] == [
        "age", "code", "name", "v1_only", "v2_only"
    ]
    by_field = {change["field"]: change for change in body["field_changes"]}
    assert by_field["age"]["kind"] == "type_changed"
    assert by_field["code"]["kind"] == "nullable_loosened"
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


def test_type_change_and_nullability_tightening_collapse_into_type_changed(
    client: TestClient,
) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "x", "type": "integer", "nullable": True},
    ])
    v2 = create_version(client, [
        {"name": "x", "type": "string", "nullable": False},
    ])
    base = create_snapshot(client, v1, [])
    target = create_snapshot(client, v2, [])

    body = compare_params(
        client, v1, base["created_at"], v2, target["created_at"]
    ).json()
    assert body["field_changes"] == [
        {
            "field": "x",
            "kind": "type_changed",
            "before": {"type": "integer", "nullable": True},
            "after": {"type": "string", "nullable": False},
        }
    ]


def test_rows_are_projected_onto_common_fields_before_comparison(
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
    # Extra keys not defined by either version, plus the version-only fields,
    # never participate; a field missing from a row is not filled with null.
    base = create_snapshot(client, v1, [
        {"id": 1, "name": "a", "old": "x", "rogue": True},
        {"id": 2},
    ])
    target = create_snapshot(client, v2, [
        {"id": 1, "name": "a", "new": "y", "rogue": False},
        {"id": 2, "name": None},
    ])

    body = compare_params(
        client, v1, base["created_at"], v2, target["created_at"]
    ).json()
    assert body["added"] == [{"row": {"id": 2, "name": None}, "count": 1}]
    assert body["removed"] == [{"row": {"id": 2}, "count": 1}]


def test_projected_rows_compare_as_a_multiset(client: TestClient) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "name", "type": "string", "nullable": True},
        {"name": "v1_note", "type": "string", "nullable": True},
    ])
    v2 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "name", "type": "string", "nullable": True},
        {"name": "v2_note", "type": "string", "nullable": True},
    ])
    base = create_snapshot(client, v1, [
        {"id": 1, "name": "a"},
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b", "v1_note": "gone"},
        {"id": 3, "name": "c"},
    ])
    target = create_snapshot(client, v2, [
        {"id": 1, "name": "a", "v2_note": "new"},
        {"id": 3, "name": "c"},
        {"id": 4, "name": "d"},
        {"id": 4, "name": "d"},
    ])

    body = compare_params(
        client, v1, base["created_at"], v2, target["created_at"]
    ).json()
    assert body["removed"] == [
        {"row": {"id": 1, "name": "a"}, "count": 1},
        {"row": {"id": 2, "name": "b"}, "count": 1},
    ]
    assert body["added"] == [
        {"row": {"id": 4, "name": "d"}, "count": 2},
    ]


def test_projected_row_equality_ignores_key_order_but_keeps_types(
    client: TestClient,
) -> None:
    create_dataset(client)
    fields = [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "name", "type": "string", "nullable": True},
    ]
    v1 = create_version(client, fields)
    v2 = create_version(client, fields)
    base = create_snapshot(client, v1, [
        {"name": "a", "id": 1},
        {"id": 2, "name": "2"},
    ])
    target = create_snapshot(client, v2, [
        {"id": 1, "name": "a"},
        {"id": 2, "name": 2},
    ])

    body = compare_params(
        client, v1, base["created_at"], v2, target["created_at"]
    ).json()
    # First rows are equal despite reversed key order; "2" vs 2 differs.
    assert body["removed"] == [{"row": {"id": 2, "name": "2"}, "count": 1}]
    assert body["added"] == [{"row": {"id": 2, "name": 2}, "count": 1}]


# --------------------------------------------------------------------------- #
# Top-level document and deterministic serialization
# --------------------------------------------------------------------------- #


def test_document_is_compact_with_fixed_key_order_and_one_newline(
    client: TestClient,
) -> None:
    v1, v2, base, target = _two_versions_with_snapshots(client)
    from_ts = _future(base["created_at"])
    to_ts = _future(target["created_at"])

    response = compare_params(client, v1, from_ts, v2, to_ts)
    assert response.status_code == 200, response.text
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
    # The timestamps echo the submitted values verbatim.
    assert body["from_timestamp"] == from_ts
    assert body["to_timestamp"] == to_ts
    assert body["from_snapshot_id"] == base["id"]
    assert body["to_snapshot_id"] == target["id"]
    assert body["from_version"] == v1
    assert body["to_version"] == v2
    assert list(body["field_changes"][0]) == ["field", "kind", "before", "after"]


# --------------------------------------------------------------------------- #
# 404s and 422s
# --------------------------------------------------------------------------- #


def test_same_version_on_both_sides_is_422(client: TestClient) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    base = create_snapshot(client, v1, [{"id": 1}])
    target = create_snapshot(client, v1, [{"id": 2}])

    response = compare_params(
        client, v1, base["created_at"], v1, target["created_at"]
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert "SQLite" not in response.text


def test_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    v1, v2, base, target = _two_versions_with_snapshots(client)

    response = compare_params(
        client, v1, base["created_at"], v2, target["created_at"],
        dataset="ghost",
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    assert compare_params(
        client, 99, base["created_at"], v2, target["created_at"]
    ).status_code == 404
    assert compare_params(
        client, v1, base["created_at"], 99, target["created_at"]
    ).status_code == 404


def test_unknown_version_404_precedes_parameter_shape_checks(
    client: TestClient,
) -> None:
    v1, _v2, base, _target = _two_versions_with_snapshots(client)

    # Unknown from_version together with a body, an unknown parameter, a
    # duplicated to_version and a naive timestamp: still 404.
    response = compare(
        client,
        params=[
            ("from_version", "99"),
            ("from", base["created_at"]),
            ("to_version", "1"),
            ("to_version", "1"),
            ("to", "not-a-time"),
            ("expand", "1"),
        ],
        content=b"   ",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    # Same version numbers (a 422) also win over the shape checks.
    response = compare(
        client,
        params={"from_version": v1, "to_version": v1, "expand": 1},
        content=b"{}",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_side_without_a_snapshot_at_its_time_is_404(client: TestClient) -> None:
    v1, v2, base, target = _two_versions_with_snapshots(client)
    before_everything = _past(base["created_at"])

    assert compare_params(
        client, v1, before_everything, v2, target["created_at"]
    ).status_code == 404
    assert compare_params(
        client, v1, base["created_at"], v2, before_everything
    ).status_code == 404

    # A version with no snapshots at all never resolves.
    v3 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    response = compare_params(
        client, v1, base["created_at"], v3, _future(target["created_at"])
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    # Nothing was written by the failed reads: the snapshot lists are
    # unchanged and no privacy trail exists.
    for version in (v1, v2):
        snapshots = client.get(
            f"/datasets/orders/versions/{version}/snapshots"
        ).json()
        assert [s["id"] for s in snapshots] == [
            s["id"] for s in (base, target) if s["version"] == version
        ]
        assert client.get(
            f"/datasets/orders/versions/{version}"
            "/privacy-policies/view/access-records"
        ).json() == []


def test_missing_duplicate_and_invalid_parameters_are_422(
    client: TestClient,
) -> None:
    v1, v2, base, target = _two_versions_with_snapshots(client)
    good = {
        "from_version": str(v1),
        "from": base["created_at"],
        "to_version": str(v2),
        "to": target["created_at"],
    }

    # Each parameter is required.
    for key in ("from_version", "from", "to_version", "to"):
        params = {k: v for k, v in good.items() if k != key}
        response = compare(client, params=params)
        assert response.status_code == 422, key
        assert response.json()["error"] == "validation_error"

    # Blank values are rejected.
    for key in ("from_version", "from", "to_version", "to"):
        response = compare(client, params={**good, key: ""})
        assert response.status_code == 422, key

    # Each parameter must appear exactly once.
    for key in ("from_version", "from", "to_version", "to"):
        pairs = list(good.items()) + [(key, good[key])]
        response = compare(client, params=pairs)
        assert response.status_code == 422, key

    # Version numbers must be positive integers.
    for bad in ("abc", "1.5", "0", "-1", "1e2", "+1", "01", " 1"):
        response = compare(client, params={**good, "from_version": bad})
        assert response.status_code == 422, bad
        response = compare(client, params={**good, "to_version": bad})
        assert response.status_code == 422, bad

    # Timestamps must be ISO-8601 date-times carrying a timezone.
    for bad in ("not-a-time", "2024-01-01", "2024-01-01T00:00:00"):
        response = compare(client, params={**good, "from": bad})
        assert response.status_code == 422, bad
        response = compare(client, params={**good, "to": bad})
        assert response.status_code == 422, bad

    # Any other query parameter is rejected.
    response = compare(client, params={**good, "expand": "1"})
    assert response.status_code == 422
    assert "SQLite" not in response.text


def test_request_body_is_422_including_pure_whitespace(client: TestClient) -> None:
    v1, v2, base, target = _two_versions_with_snapshots(client)
    params = {
        "from_version": v1,
        "from": base["created_at"],
        "to_version": v2,
        "to": target["created_at"],
    }
    for content in (b"{}", b"   ", b"\t\n", b"not json"):
        response = compare(
            client,
            params=params,
            content=content,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422, content
        assert response.json()["error"] == "validation_error"


def test_post_is_not_accepted(client: TestClient) -> None:
    v1, v2, base, target = _two_versions_with_snapshots(client)
    response = client.post(
        CROSS_DIFF_PATH,
        params={
            "from_version": v1,
            "from": base["created_at"],
            "to_version": v2,
            "to": target["created_at"],
        },
    )
    assert response.status_code == 405


# --------------------------------------------------------------------------- #
# Read-only behavior and persistence across restarts
# --------------------------------------------------------------------------- #


def test_comparison_is_read_only(client: TestClient) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "name", "type": "string", "nullable": True},
    ])
    v2 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "label", "type": "string", "nullable": True},
    ])
    base_rows = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    target_rows = [{"id": 1, "label": "a"}, {"id": 3, "label": "c"}]
    base = create_snapshot(client, v1, base_rows)
    target = create_snapshot(client, v2, target_rows)
    params = {
        "from_version": v1,
        "from": base["created_at"],
        "to_version": v2,
        "to": target["created_at"],
    }

    first = compare(client, params=params)
    assert first.status_code == 200
    second = compare(client, params=params)
    assert second.content == first.content

    # Snapshots and their rows are untouched.
    assert client.get(
        f"/datasets/orders/versions/{v1}/snapshots/{base['id']}"
    ).json()["rows"] == base_rows
    assert client.get(
        f"/datasets/orders/versions/{v2}/snapshots/{target['id']}"
    ).json()["rows"] == target_rows
    # No privacy trail of any kind is written.
    for version in (v1, v2):
        assert client.get(
            f"/datasets/orders/versions/{version}"
            "/privacy-policies/view/access-records"
        ).json() == []
        assert client.get(
            f"/datasets/orders/versions/{version}"
            "/privacy-policies/view/audit-records"
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
params = {
    "from_version": v1,
    "from": base["created_at"],
    "to_version": v2,
    "to": target["created_at"],
}
response = client.get("/datasets/orders/snapshots/at/cross-diff", params=params)
assert response.status_code == 200, response.text
print(json.dumps({"params": params, "document": response.text}))
"""

VERIFY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
state = json.loads(input())
response = client.get(
    "/datasets/orders/snapshots/at/cross-diff", params=state["params"]
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
