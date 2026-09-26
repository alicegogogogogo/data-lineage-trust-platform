"""Tests for the cross-version snapshot comparison endpoint.

The endpoint is ``GET /datasets/{dataset}/snapshots/{base}/diff/{target}``:
it compares two snapshots of two different schema versions of one dataset.
The response gives the field-definition changes first and then the row
multisets after projection onto the field names both versions define.
These tests cover field-change vocabulary and collapsing, projection,
multiset semantics, direction, deterministic serialization, 404/422
precedence, read-only behavior and persistence across restarts.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def compare(
    client: TestClient,
    base_id: int,
    target_id: int,
    *,
    dataset: str = "orders",
    **kwargs,
):
    return client.request(
        "GET",
        f"/datasets/{dataset}/snapshots/{base_id}/diff/{target_id}",
        **kwargs,
    )


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
# Field-definition changes
# --------------------------------------------------------------------------- #


def test_field_changes_list_added_removed_type_and_nullability_kinds(
    client: TestClient,
) -> None:
    _v1, _v2, base, target = _two_versions_with_snapshots(client)

    response = compare(client, base["id"], target["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert [change["field"] for change in body["field_changes"]] == [
        "age", "code", "name", "v1_only", "v2_only"
    ]
    by_field = {change["field"]: change for change in body["field_changes"]}
    assert by_field["age"]["kind"] == "type_changed"
    assert by_field["code"]["kind"] == "nullable_loosened"
    assert by_field["name"]["kind"] == "nullable_tightened"
    assert by_field["v1_only"]["kind"] == "removed"
    assert by_field["v2_only"]["kind"] == "added"

    # The unchanged shared field never appears.
    assert "id" not in by_field

    # Each entry carries both definition sides, null (never omitted) on the
    # side where the field is missing.
    assert by_field["v1_only"]["before"] == {"type": "string", "nullable": True}
    assert by_field["v1_only"]["after"] is None
    assert by_field["v2_only"]["before"] is None
    assert by_field["v2_only"]["after"] == {"type": "string", "nullable": True}
    assert by_field["age"]["before"] == {"type": "integer", "nullable": True}
    assert by_field["age"]["after"] == {"type": "string", "nullable": True}
    assert by_field["name"]["before"] == {"type": "string", "nullable": True}
    assert by_field["name"]["after"] == {"type": "string", "nullable": False}
    for change in body["field_changes"]:
        assert set(change) == {"field", "kind", "before", "after"}


def test_type_change_and_nullability_tightening_collapse_into_type_changed(
    client: TestClient,
) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "x", "type": "integer", "nullable": True},
    ])
    v2 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "x", "type": "string", "nullable": False},
    ])
    base = create_snapshot(client, v1, [])
    target = create_snapshot(client, v2, [])

    body = compare(client, base["id"], target["id"]).json()
    assert body["field_changes"] == [
        {
            "field": "x",
            "kind": "type_changed",
            "before": {"type": "integer", "nullable": True},
            "after": {"type": "string", "nullable": False},
        }
    ]


def test_field_changes_have_no_entries_when_definitions_match(
    client: TestClient,
) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    v2 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    base = create_snapshot(client, v1, [{"id": 1}])
    target = create_snapshot(client, v2, [{"id": 1}])

    body = compare(client, base["id"], target["id"]).json()
    assert body["field_changes"] == []


# --------------------------------------------------------------------------- #
# Row projection and multiset comparison
# --------------------------------------------------------------------------- #


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
    # The rows carry extra keys not defined by either version; those, plus the
    # version-only fields, must not participate. Only {id, name} does.
    base = create_snapshot(client, v1, [
        {"id": 1, "name": "a", "old": "x", "rogue": True},
    ])
    target = create_snapshot(client, v2, [
        {"id": 1, "name": "a", "new": "y", "rogue": False},
    ])

    body = compare(client, base["id"], target["id"]).json()
    assert body["added"] == []
    assert body["removed"] == []


def test_field_missing_from_a_row_does_not_participate_as_null(
    client: TestClient,
) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "name", "type": "string", "nullable": True},
    ])
    v2 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "name", "type": "string", "nullable": True},
    ])
    # A field missing from the row is not filled with null: the projection
    # simply omits the key, so {"id": 1} and {"id": 1, "name": null} are
    # different projected rows rather than equal ones.
    base = create_snapshot(client, v1, [{"id": 1}])
    target = create_snapshot(client, v2, [{"id": 1, "name": None}])

    body = compare(client, base["id"], target["id"]).json()
    assert body["added"] == [{"row": {"id": 1, "name": None}, "count": 1}]
    assert body["removed"] == [{"row": {"id": 1}, "count": 1}]

    # Rows missing the same field project identically and cancel.
    base2 = create_snapshot(client, v1, [{"id": 2}])
    target2 = create_snapshot(client, v2, [{"id": 2}])
    body = compare(client, base2["id"], target2["id"]).json()
    assert body["added"] == []
    assert body["removed"] == []


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

    body = compare(client, base["id"], target["id"]).json()
    # id=1,a appears twice on the baseline and once on the target: removed 1.
    # id=2,b only on the baseline: removed 1. id=4,d twice only on the target:
    # added 2. id=3,c cancels.
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
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "name", "type": "string", "nullable": True},
        {"name": "flag", "type": "boolean", "nullable": True},
    ])
    v2 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "name", "type": "string", "nullable": True},
        {"name": "flag", "type": "boolean", "nullable": True},
    ])
    base = create_snapshot(client, v1, [
        {"name": "a", "id": 1, "flag": True},
        {"id": 2, "name": "2", "flag": None},
        {"id": 3, "name": "x", "flag": None},
    ])
    target = create_snapshot(client, v2, [
        {"id": 1, "name": "a", "flag": True},
        {"id": 2, "name": 2, "flag": None},
        {"id": 4, "name": "x", "flag": None},
    ])

    body = compare(client, base["id"], target["id"]).json()
    # First rows are equal despite reversed object key order. "2" vs 2 differs
    # in type, so both sides appear; {id:3} vs {id:4} differ likewise.
    assert body["removed"] == [
        {"row": {"id": 2, "name": "2", "flag": None}, "count": 1},
        {"row": {"id": 3, "name": "x", "flag": None}, "count": 1},
    ]
    assert body["added"] == [
        {"row": {"id": 2, "name": 2, "flag": None}, "count": 1},
        {"row": {"id": 4, "name": "x", "flag": None}, "count": 1},
    ]


def test_no_common_fields_compares_empty_projections(client: TestClient) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "a", "type": "string", "nullable": True},
    ])
    v2 = create_version(client, [
        {"name": "b", "type": "string", "nullable": True},
    ])
    base = create_snapshot(client, v1, [{"a": "x"}, {"a": "y"}])
    target = create_snapshot(client, v2, [{"b": "z"}])

    body = compare(client, base["id"], target["id"]).json()
    # Every row projects to {}: the multiset is {} twice on the baseline vs
    # {} once on the target.
    assert body["removed"] == [{"row": {}, "count": 1}]
    assert body["added"] == []


# --------------------------------------------------------------------------- #
# Top-level document, direction and deterministic serialization
# --------------------------------------------------------------------------- #


def test_document_identifies_both_snapshots_and_versions(
    client: TestClient,
) -> None:
    v1, v2, base, target = _two_versions_with_snapshots(client)
    body = compare(client, base["id"], target["id"]).json()
    assert body["base_snapshot_id"] == base["id"]
    assert body["base_version"] == v1
    assert body["target_snapshot_id"] == target["id"]
    assert body["target_version"] == v2
    assert set(body) == {
        "base_snapshot_id",
        "base_version",
        "target_snapshot_id",
        "target_version",
        "field_changes",
        "added",
        "removed",
    }


def test_comparison_is_directional_and_accepts_either_version_order(
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

    forward = compare(client, base["id"], target["id"]).json()
    assert forward["base_version"] == v1
    assert forward["target_version"] == v2
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

    # The reverse direction (target version number lower than the base's) is
    # valid and inverts sides, field changes and row sets.
    reverse = compare(client, target["id"], base["id"]).json()
    assert reverse["base_snapshot_id"] == target["id"]
    assert reverse["base_version"] == v2
    assert reverse["target_snapshot_id"] == base["id"]
    assert reverse["target_version"] == v1
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


def test_document_is_compact_with_fixed_key_order_and_one_newline(
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
    target = create_snapshot(client, v2, [{"id": 1, "label": "a"}])

    response = compare(client, base["id"], target["id"])
    assert response.status_code == 200
    raw = response.content
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")
    assert b": " not in raw
    assert b", " not in raw
    # Top-level key order is fixed regardless of set iteration order (json
    # loads preserves document insertion order).
    body = response.json()
    assert list(body) == [
        "base_snapshot_id",
        "base_version",
        "target_snapshot_id",
        "target_version",
        "field_changes",
        "added",
        "removed",
    ]
    # Field-change entry key order.
    assert list(body["field_changes"][0]) == ["field", "kind", "before", "after"]


# --------------------------------------------------------------------------- #
# 404s and 422s
# --------------------------------------------------------------------------- #


def test_same_schema_version_pair_is_422(client: TestClient) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    base = create_snapshot(client, v1, [{"id": 1}])
    target = create_snapshot(client, v1, [{"id": 2}])

    response = compare(client, base["id"], target["id"])
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert "SQLite" not in response.text

    # The same pair remains fully supported by the same-version endpoint.
    same_version = client.get(
        f"/datasets/orders/versions/{v1}/snapshots/"
        f"{base['id']}/diff/{target['id']}"
    )
    assert same_version.status_code == 200
    assert same_version.json()["added"] == [{"row": {"id": 2}, "count": 1}]


def test_snapshot_from_another_dataset_is_422(client: TestClient) -> None:
    create_dataset(client, "orders")
    create_dataset(client, "billing")
    orders_v = create_version(
        client, [{"name": "id", "type": "integer", "nullable": False}],
        dataset="orders",
    )
    billing_v = create_version(
        client, [{"name": "id", "type": "integer", "nullable": False}],
        dataset="billing",
    )
    orders_snapshot = create_snapshot(client, orders_v, [{"id": 1}], dataset="orders")
    billing_snapshot = create_snapshot(
        client, billing_v, [{"id": 1}], dataset="billing"
    )

    response = compare(client, orders_snapshot["id"], billing_snapshot["id"])
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"

    # The target position is checked as well, and addressing the pair through
    # the other dataset makes that dataset own the baseline instead.
    response = compare(
        client, billing_snapshot["id"], orders_snapshot["id"], dataset="billing"
    )
    assert response.status_code == 422
    response = compare(
        client, orders_snapshot["id"], billing_snapshot["id"], dataset="billing"
    )
    assert response.status_code == 422


def test_unknown_dataset_or_snapshot_is_404(client: TestClient) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    v2 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    base = create_snapshot(client, v1, [])
    target = create_snapshot(client, v2, [])

    assert compare(client, base["id"], target["id"], dataset="ghost").status_code == 404
    assert compare(client, 9999, target["id"]).status_code == 404
    assert compare(client, base["id"], 9999).status_code == 404


def test_404_takes_precedence_over_body_and_query_shape(client: TestClient) -> None:
    # Unknown dataset, with both body bytes and a query parameter: 404 wins.
    response = client.request(
        "GET",
        "/datasets/ghost/snapshots/1/diff/2",
        params={"expand": 1},
        content=b"   ",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"

    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    v2 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    base = create_snapshot(client, v1, [])
    # Unknown target snapshot with a body and a query parameter: still 404.
    response = client.request(
        "GET",
        f"/datasets/orders/snapshots/{base['id']}/diff/9999",
        params={"expand": 1},
        content=b"{}",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_request_body_and_query_parameters_are_422(client: TestClient) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    v2 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    base = create_snapshot(client, v1, [])
    target = create_snapshot(client, v2, [])
    path = f"/datasets/orders/snapshots/{base['id']}/diff/{target['id']}"

    for content in (b"{}", b"   ", b"\t\n", b"not json"):
        response = client.request(
            "GET",
            path,
            content=content,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422, content
        assert response.json()["error"] == "validation_error"
    response = client.request("GET", path, params={"expand": 1})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert "SQLite" not in response.text


def test_post_is_not_accepted(client: TestClient) -> None:
    create_dataset(client)
    v1 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    v2 = create_version(client, [
        {"name": "id", "type": "integer", "nullable": False},
    ])
    base = create_snapshot(client, v1, [])
    target = create_snapshot(client, v2, [])
    response = client.post(
        f"/datasets/orders/snapshots/{base['id']}/diff/{target['id']}"
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

    first = compare(client, base["id"], target["id"])
    assert first.status_code == 200
    second = compare(client, base["id"], target["id"])
    assert second.content == first.content

    # Snapshots and their rows are untouched.
    assert client.get(
        f"/datasets/orders/versions/{v1}/snapshots/{base['id']}"
    ).json()["rows"] == base_rows
    assert client.get(
        f"/datasets/orders/versions/{v2}/snapshots/{target['id']}"
    ).json()["rows"] == target_rows
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
    f"/datasets/orders/snapshots/{base['id']}/diff/{target['id']}"
)
assert response.status_code == 200, response.text
print(json.dumps({
    "base_id": base["id"],
    "target_id": target["id"],
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
    f"/datasets/orders/snapshots/{state['base_id']}/diff/{state['target_id']}"
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
    db_path = tmp_path / "cross-version-snapshot.db"
    state = _run(db_path, CREATE_SCRIPT)
    assert _run(db_path, VERIFY_SCRIPT, stdin=state) == "verified"
