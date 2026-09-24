"""Tests for the read-only schema version diff endpoint."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DIFF_PATH = "/datasets/orders/versions/1/diff/2"


def create_dataset(client: TestClient, name: str = "orders") -> None:
    response = client.post("/datasets", json={"name": name})
    assert response.status_code == 201, response.text


def create_version(client: TestClient, fields: list[dict], dataset: str = "orders") -> int:
    response = client.post(
        f"/datasets/{dataset}/versions", json={"fields": fields}
    )
    assert response.status_code == 201, response.text
    return response.json()["version"]


def field(name: str, type_: str = "string", nullable: bool = True) -> dict:
    return {"name": name, "type": type_, "nullable": nullable}


def get_diff(
    client: TestClient, from_version: int = 1, to_version: int = 2
) -> dict:
    response = client.get(
        f"/datasets/orders/versions/{from_version}/diff/{to_version}"
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Response shape and change kinds
# --------------------------------------------------------------------------- #


def test_diff_reports_added_removed_and_changed_sorted_by_field(
    client: TestClient,
) -> None:
    create_dataset(client)
    create_version(
        client,
        [
            field("removed_field", "integer", False),
            field("changed_field", "integer", False),
            field("kept_field", "string", True),
        ],
    )
    create_version(
        client,
        [
            field("changed_field", "string", False),
            field("kept_field", "string", True),
            field("added_field", "decimal(10,2)", True),
        ],
    )

    body = get_diff(client)
    assert set(body) == {"from_version", "to_version", "compatible", "changes"}
    assert body["from_version"] == 1
    assert body["to_version"] == 2
    assert body["compatible"] is False
    assert [change["field"] for change in body["changes"]] == [
        "added_field",
        "changed_field",
        "removed_field",
    ]
    added, changed, removed = body["changes"]
    assert added == {
        "field": "added_field",
        "kind": "added",
        "before": None,
        "after": {"type": "decimal(10,2)", "nullable": True},
    }
    assert changed == {
        "field": "changed_field",
        "kind": "changed",
        "before": {"type": "integer", "nullable": False},
        "after": {"type": "string", "nullable": False},
    }
    assert removed == {
        "field": "removed_field",
        "kind": "removed",
        "before": {"type": "integer", "nullable": False},
        "after": None,
    }


def test_identical_versions_have_empty_changes_and_are_compatible(
    client: TestClient,
) -> None:
    create_dataset(client)
    create_version(client, [field("id", "integer", False)])
    create_version(client, [field("id", "integer", False)])

    body = get_diff(client)
    assert body["changes"] == []
    assert body["compatible"] is True


def test_diff_of_a_version_with_itself_is_empty(client: TestClient) -> None:
    create_dataset(client)
    create_version(client, [field("id", "integer", False), field("note")])

    body = get_diff(client, from_version=1, to_version=1)
    assert body == {
        "from_version": 1,
        "to_version": 1,
        "compatible": True,
        "changes": [],
    }


def test_field_reordering_is_not_a_difference(client: TestClient) -> None:
    create_dataset(client)
    create_version(
        client,
        [field("zeta", "string", True), field("alpha", "integer", False)],
    )
    create_version(
        client,
        [field("alpha", "integer", False), field("zeta", "string", True)],
    )

    body = get_diff(client)
    assert body["changes"] == []
    assert body["compatible"] is True


# --------------------------------------------------------------------------- #
# Compatibility semantics
# --------------------------------------------------------------------------- #


def test_added_fields_keep_compatibility(client: TestClient) -> None:
    create_dataset(client)
    create_version(client, [field("id", "integer", False)])
    create_version(
        client, [field("id", "integer", False), field("extra", "string", False)]
    )

    body = get_diff(client)
    assert body["compatible"] is True
    assert [change["kind"] for change in body["changes"]] == ["added"]


def test_removed_field_breaks_compatibility(client: TestClient) -> None:
    create_dataset(client)
    create_version(client, [field("id", "integer", False), field("old")])
    create_version(client, [field("id", "integer", False)])

    body = get_diff(client)
    assert body["compatible"] is False
    assert [change["kind"] for change in body["changes"]] == ["removed"]


def test_type_change_breaks_compatibility(client: TestClient) -> None:
    create_dataset(client)
    create_version(client, [field("amount", "integer", True)])
    create_version(client, [field("amount", "decimal(10,2)", True)])

    body = get_diff(client)
    assert body["compatible"] is False
    change = body["changes"][0]
    assert change["kind"] == "changed"
    assert change["before"] == {"type": "integer", "nullable": True}
    assert change["after"] == {"type": "decimal(10,2)", "nullable": True}


def test_nullable_tightening_breaks_compatibility(client: TestClient) -> None:
    create_dataset(client)
    create_version(client, [field("note", "string", True)])
    create_version(client, [field("note", "string", False)])

    body = get_diff(client)
    assert body["compatible"] is False
    change = body["changes"][0]
    assert change["kind"] == "changed"
    assert change["before"] == {"type": "string", "nullable": True}
    assert change["after"] == {"type": "string", "nullable": False}


def test_nullable_loosening_keeps_compatibility(client: TestClient) -> None:
    create_dataset(client)
    create_version(client, [field("note", "string", False)])
    create_version(client, [field("note", "string", True)])

    body = get_diff(client)
    assert body["compatible"] is True
    change = body["changes"][0]
    assert change["kind"] == "changed"
    assert change["before"] == {"type": "string", "nullable": False}
    assert change["after"] == {"type": "string", "nullable": True}


def test_diff_direction_matters(client: TestClient) -> None:
    create_dataset(client)
    create_version(client, [field("id", "integer", False)])
    create_version(
        client, [field("id", "integer", False), field("extra", "string", True)]
    )

    forward = get_diff(client, from_version=1, to_version=2)
    backward = get_diff(client, from_version=2, to_version=1)
    assert forward["compatible"] is True
    assert [change["kind"] for change in forward["changes"]] == ["added"]
    assert backward["compatible"] is False
    assert [change["kind"] for change in backward["changes"]] == ["removed"]


# --------------------------------------------------------------------------- #
# Error semantics
# --------------------------------------------------------------------------- #


def test_unknown_dataset_returns_404(client: TestClient) -> None:
    response = client.get("/datasets/ghost/versions/1/diff/2")
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "not_found"


def test_unknown_version_returns_404(client: TestClient) -> None:
    create_dataset(client)
    create_version(client, [field("id", "integer", False)])

    for path in (
        "/datasets/orders/versions/1/diff/2",
        "/datasets/orders/versions/2/diff/1",
        "/datasets/orders/versions/99/diff/100",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"] == "not_found"


def test_non_positive_and_non_integer_versions_return_422(
    client: TestClient,
) -> None:
    create_dataset(client)
    create_version(client, [field("id", "integer", False)])

    for path in (
        "/datasets/orders/versions/0/diff/1",
        "/datasets/orders/versions/1/diff/0",
        "/datasets/orders/versions/-1/diff/1",
        "/datasets/orders/versions/1/diff/-2",
        "/datasets/orders/versions/abc/diff/1",
        "/datasets/orders/versions/1/diff/1.5",
    ):
        response = client.get(path)
        assert response.status_code == 422, path
        assert response.json()["error"] == "validation_error"


def test_request_body_is_rejected(client: TestClient) -> None:
    create_dataset(client)
    create_version(client, [field("id", "integer", False)])
    create_version(client, [field("id", "integer", False)])

    for content in (b'{"extra": 1}', b"x"):
        response = client.request(
            "GET",
            DIFF_PATH,
            content=content,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422, content
        assert response.json()["error"] == "validation_error"


def test_query_parameters_are_rejected(client: TestClient) -> None:
    create_dataset(client)
    create_version(client, [field("id", "integer", False)])
    create_version(client, [field("id", "integer", False)])

    response = client.get(DIFF_PATH, params={"include": "fields"})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_unknown_path_wins_over_body_and_query(client: TestClient) -> None:
    # 404 precedence: an unresolved path stays 404 even with an invalid shape.
    response = client.request(
        "GET",
        "/datasets/ghost/versions/1/diff/2",
        content=b"{}",
        headers={"Content-Type": "application/json"},
        params={"x": "1"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


# --------------------------------------------------------------------------- #
# Read-only behaviour
# --------------------------------------------------------------------------- #


def test_diff_does_not_create_or_modify_data(client: TestClient) -> None:
    create_dataset(client)
    create_version(client, [field("id", "integer", False)])
    create_version(
        client, [field("id", "integer", False), field("note", "string", True)]
    )
    before = client.get("/datasets/orders/versions").json()
    datasets_before = client.get("/datasets").json()

    first = get_diff(client)
    second = get_diff(client)
    assert first == second
    # Rejected shapes also leave every stored version untouched.
    client.get(DIFF_PATH, params={"x": "1"})
    assert client.get("/datasets/orders/versions").json() == before
    assert client.get("/datasets").json() == datasets_before


def test_diff_ignores_other_version_scoped_state(client: TestClient) -> None:
    create_dataset(client)
    create_version(client, [field("id", "integer", False)])
    create_version(client, [field("id", "integer", False)])
    # Quality rules, privacy policies, snapshots and tasks of either version
    # must not leak into the field-definition diff.
    client.post(
        "/datasets/orders/versions/1/quality-rules",
        json={"name": "id required", "kind": "not_null", "params": {"field": "id"}},
    )
    client.post(
        "/datasets/orders/versions/2/privacy-policies",
        json={
            "field": "id",
            "classification": "internal",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    client.post(
        "/datasets/orders/versions/1/snapshots", json={"rows": [{"id": 1}]}
    )
    client.post(
        "/datasets/orders/versions/2/processing-tasks", json={"name": "extract"}
    )

    body = get_diff(client)
    assert body["changes"] == []
    assert body["compatible"] is True


def test_duplicate_field_creation_still_rejected_and_diff_unaffected(
    client: TestClient,
) -> None:
    create_dataset(client)
    create_version(client, [field("id", "integer", False)])
    rejected = client.post(
        "/datasets/orders/versions",
        json={"fields": [field("id"), field("id", "string")]},
    )
    assert rejected.status_code == 422

    # The failed creation left no version behind; diffing v1 against the
    # missing v2 is a 404.
    assert client.get(DIFF_PATH).status_code == 404
    assert len(client.get("/datasets/orders/versions").json()) == 1


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #

_CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
assert client.post("/datasets", json={"name": "orders"}).status_code == 201
assert client.post(
    "/datasets/orders/versions",
    json={"fields": [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "legacy", "type": "string", "nullable": True},
    ]},
).status_code == 201
assert client.post(
    "/datasets/orders/versions",
    json={"fields": [
        {"name": "id", "type": "string", "nullable": False},
        {"name": "extra", "type": "string", "nullable": True},
    ]},
).status_code == 201
print("created")
"""

_VERIFY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
first = client.get("/datasets/orders/versions/1/diff/2")
assert first.status_code == 200, first.text
assert first.json() == {
    "from_version": 1,
    "to_version": 2,
    "compatible": False,
    "changes": [
        {
            "field": "extra",
            "kind": "added",
            "before": None,
            "after": {"type": "string", "nullable": True},
        },
        {
            "field": "id",
            "kind": "changed",
            "before": {"type": "integer", "nullable": False},
            "after": {"type": "string", "nullable": False},
        },
        {
            "field": "legacy",
            "kind": "removed",
            "before": {"type": "string", "nullable": True},
            "after": None,
        },
    ],
}
# The answer is stable across repeated reads after the restart.
again = client.get("/datasets/orders/versions/1/diff/2")
assert again.json() == first.json()
print(json.dumps({"changes": len(first.json()["changes"])}))
"""


def _run_script(db_path: Path, script: str) -> str:
    env = os.environ.copy()
    env["DATA_LINEAGE_DB"] = str(db_path)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_diff_is_stable_across_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "diff-persistence.db"
    assert _run_script(db_path, _CREATE_SCRIPT) == "created"

    # New interpreter: the diff is recomputed from the persisted definitions.
    output = _run_script(db_path, _VERIFY_SCRIPT)
    assert json.loads(output)["changes"] == 3


def test_diff_result_does_not_depend_on_database_row_order(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset(client)
    create_version(
        client,
        [field("b_keep"), field("a_change", "integer", True), field("c_drop")],
    )
    create_version(
        client,
        [field("b_keep"), field("a_change", "string", True), field("d_new")],
    )

    expected = get_diff(client)
    assert [change["field"] for change in expected["changes"]] == [
        "a_change",
        "c_drop",
        "d_new",
    ]

    # Rewriting the field rows in a different physical order must not change
    # the answer: names, change order and booleans come from explicit sorting.
    conn = sqlite3.connect(isolated_database)
    try:
        rows = conn.execute(
            "SELECT version_id, name, type, nullable, position FROM schema_fields"
        ).fetchall()
        conn.execute("DELETE FROM schema_fields")
        conn.executemany(
            "INSERT INTO schema_fields (version_id, name, type, nullable, position) "
            "VALUES (?, ?, ?, ?, ?)",
            list(reversed(rows)),
        )
        conn.commit()
    finally:
        conn.close()

    assert get_diff(client) == expected
