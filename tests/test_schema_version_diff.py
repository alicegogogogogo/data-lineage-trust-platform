"""Tests for the read-only schema version diff endpoint."""

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


def add_version(client: TestClient, fields: list[dict], dataset: str = "orders") -> int:
    response = client.post(
        f"/datasets/{dataset}/versions", json={"fields": fields}
    )
    assert response.status_code == 201, response.text
    return response.json()["version"]


def diff(
    client: TestClient, from_version: int, to_version: int, dataset: str = "orders"
):
    response = client.get(
        f"/datasets/{dataset}/versions/{from_version}/diff/{to_version}"
    )
    assert response.status_code == 200, response.text
    return response.json()


def F(name: str, ftype: str = "string", nullable: bool = True) -> dict:
    return {"name": name, "type": ftype, "nullable": nullable}


# --------------------------------------------------------------------------- #
# Empty / identical cases
# --------------------------------------------------------------------------- #


def test_same_version_is_empty_and_compatible(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("a"), F("b", "integer", False)])

    body = diff(client, 1, 1)
    assert set(body) == {"from_version", "to_version", "compatible", "changes"}
    assert body == {
        "from_version": 1,
        "to_version": 1,
        "compatible": True,
        "changes": [],
    }


def test_identical_field_sets_diff_is_empty(client: TestClient) -> None:
    create_dataset(client)
    fields = [F("a"), F("b", "integer", False)]
    add_version(client, fields)
    add_version(client, list(reversed(fields)))

    # Field reordering alone is not a difference and does not break
    # compatibility, regardless of the comparison direction.
    assert diff(client, 1, 2)["changes"] == []
    assert diff(client, 1, 2)["compatible"] is True
    assert diff(client, 2, 1)["changes"] == []
    assert diff(client, 2, 1)["compatible"] is True


# --------------------------------------------------------------------------- #
# Added / removed fields
# --------------------------------------------------------------------------- #


def test_added_field_is_compatible(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])
    add_version(client, [F("id", "integer", False), F("note", "string", True)])

    body = diff(client, 1, 2)
    assert body["compatible"] is True
    assert body["changes"] == [
        {
            "field": "note",
            "kind": "added",
            "before": None,
            "after": {"type": "string", "nullable": True},
        }
    ]


def test_removed_field_is_incompatible(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False), F("note", "string", True)])
    add_version(client, [F("id", "integer", False)])

    body = diff(client, 1, 2)
    assert body["compatible"] is False
    assert body["changes"] == [
        {
            "field": "note",
            "kind": "removed",
            "before": {"type": "string", "nullable": True},
            "after": None,
        }
    ]

    # The reverse comparison then reports the same field as an addition and is
    # compatible.
    reverse = diff(client, 2, 1)
    assert reverse["compatible"] is True
    assert reverse["changes"][0]["kind"] == "added"
    assert reverse["changes"][0]["before"] is None
    assert reverse["changes"][0]["after"] == {"type": "string", "nullable": True}


# --------------------------------------------------------------------------- #
# Changed definitions: type and nullability
# --------------------------------------------------------------------------- #


def test_type_change_is_changed_and_incompatible(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("amount", "integer", True)])
    add_version(client, [F("amount", "string", True)])

    body = diff(client, 1, 2)
    assert body["compatible"] is False
    assert body["changes"] == [
        {
            "field": "amount",
            "kind": "changed",
            "before": {"type": "integer", "nullable": True},
            "after": {"type": "string", "nullable": True},
        }
    ]


def test_nullable_tightening_is_incompatible(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("amount", "integer", True)])
    add_version(client, [F("amount", "integer", False)])

    body = diff(client, 1, 2)
    assert body["compatible"] is False
    change = body["changes"][0]
    assert change["kind"] == "changed"
    assert change["before"] == {"type": "integer", "nullable": True}
    assert change["after"] == {"type": "integer", "nullable": False}


def test_nullable_loosening_is_compatible(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("amount", "integer", False)])
    add_version(client, [F("amount", "integer", True)])

    body = diff(client, 1, 2)
    assert body["compatible"] is True
    assert body["changes"] == [
        {
            "field": "amount",
            "kind": "changed",
            "before": {"type": "integer", "nullable": False},
            "after": {"type": "integer", "nullable": True},
        }
    ]


def test_type_and_nullability_change_together(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("amount", "integer", False)])
    # Type changes even though nullability is loosened: a type change is still
    # incompatible.
    add_version(client, [F("amount", "string", True)])

    body = diff(client, 1, 2)
    assert body["compatible"] is False
    assert body["changes"][0]["kind"] == "changed"


def test_changes_sorted_by_field_name_with_mixed_kinds(client: TestClient) -> None:
    create_dataset(client)
    add_version(
        client,
        [
            F("zeta", "integer", False),
            F("removed", "string", True),
            F("kept", "string", True),
        ],
    )
    add_version(
        client,
        [
            F("kept", "string", True),
            F("alpha", "string", True),
            F("zeta", "string", False),
        ],
    )

    body = diff(client, 1, 2)
    assert [change["field"] for change in body["changes"]] == [
        "alpha",
        "removed",
        "zeta",
    ]
    kinds = {change["field"]: change["kind"] for change in body["changes"]}
    assert kinds == {"alpha": "added", "removed": "removed", "zeta": "changed"}
    # The removed field makes the whole comparison incompatible even though
    # zeta only changed type and alpha was added.
    assert body["compatible"] is False


def test_change_entry_shape_always_has_before_and_after_keys(
    client: TestClient,
) -> None:
    create_dataset(client)
    add_version(client, [F("a")])
    add_version(client, [F("b")])

    body = diff(client, 1, 2)
    for change in body["changes"]:
        assert set(change) == {"field", "kind", "before", "after"}
    by_field = {change["field"]: change for change in body["changes"]}
    assert by_field["a"]["kind"] == "removed"
    assert by_field["a"]["before"] == {"type": "string", "nullable": True}
    assert by_field["a"]["after"] is None
    assert by_field["b"]["kind"] == "added"
    assert by_field["b"]["before"] is None
    assert by_field["b"]["after"] == {"type": "string", "nullable": True}


# --------------------------------------------------------------------------- #
# Duplicate fields stay rejected at creation (no versions are written)
# --------------------------------------------------------------------------- #


def test_duplicate_field_creation_rejected_and_diff_unaffected(
    client: TestClient,
) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])

    response = client.post(
        "/datasets/orders/versions",
        json={"fields": [F("id", "integer", False), F("id", "string", True)]},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"

    # Only version 1 exists; the rejected version was never numbered.
    versions = client.get("/datasets/orders/versions").json()
    assert [v["version"] for v in versions] == [1]
    body = diff(client, 1, 1)
    assert body["changes"] == []
    assert body["compatible"] is True


# --------------------------------------------------------------------------- #
# Errors: 404 / 422 and stable JSON
# --------------------------------------------------------------------------- #


def test_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])
    for path in (
        "/datasets/ghost/versions/1/diff/1",
        "/datasets/orders/versions/9/diff/1",
        "/datasets/orders/versions/1/diff/9",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "not_found"


def test_404_takes_precedence_over_query_or_body(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])

    assert client.get("/datasets/ghost/versions/1/diff/1?bogus=1").status_code == 404
    response = client.request(
        "GET",
        "/datasets/ghost/versions/1/diff/1",
        content=b'{"bogus": 1}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404
    assert client.get("/datasets/orders/versions/9/diff/1?bogus=1").status_code == 404
    assert client.get("/datasets/orders/versions/1/diff/9?bogus=1").status_code == 404


def test_non_positive_version_numbers_are_422(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])

    for path in (
        "/datasets/orders/versions/0/diff/1",
        "/datasets/orders/versions/1/diff/0",
        "/datasets/orders/versions/-1/diff/1",
        "/datasets/orders/versions/1/diff/-2",
    ):
        response = client.get(path)
        assert response.status_code == 422, path
        assert response.json()["error"] == "validation_error"
        assert set(response.json()) == {"error", "detail"}


def test_non_integer_path_segments_are_422(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])

    for path in (
        "/datasets/orders/versions/not-an-int/diff/1",
        "/datasets/orders/versions/1/diff/not-an-int",
        "/datasets/orders/versions/1.5/diff/1",
    ):
        response = client.get(path)
        assert response.status_code == 422, path
        assert response.json()["error"] == "validation_error"


def test_unexpected_query_parameter_is_422(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])
    add_version(client, [F("id", "integer", False), F("note", "string", True)])

    for suffix in ("?x=1", "?x=", "?x=1&y=2", "?="):
        response = client.get(f"/datasets/orders/versions/1/diff/2{suffix}")
        assert response.status_code == 422, suffix
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]


def test_non_empty_body_is_422(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])

    for kwargs in (
        {"content": b'{"x": 1}', "headers": {"content-type": "application/json"}},
        {"content": b"plain text"},
        {"content": b"["},
    ):
        response = client.request(
            "GET", "/datasets/orders/versions/1/diff/1", **kwargs
        )
        assert response.status_code == 422, kwargs
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"


def test_rejected_request_writes_nothing(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])
    before = diff(client, 1, 1)

    client.get("/datasets/orders/versions/1/diff/1?bogus=1")
    client.request(
        "GET",
        "/datasets/orders/versions/1/diff/1",
        content=b'{"bogus": 1}',
        headers={"content-type": "application/json"},
    )
    client.get("/datasets/orders/versions/0/diff/1")

    after = diff(client, 1, 1)
    assert after == before
    versions = client.get("/datasets/orders/versions").json()
    assert len(versions) == 1


# --------------------------------------------------------------------------- #
# Stability across process restarts
# --------------------------------------------------------------------------- #


CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response):
    assert response.status_code in (200, 201), response.text
    return response.json()

ok(client.post("/datasets", json={"name": "orders"}))
ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [
        {"name": "amount", "type": "integer", "nullable": True},
        {"name": "note", "type": "string", "nullable": True},
        {"name": "zeta", "type": "string", "nullable": False},
    ]},
))
ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [
        {"name": "zeta", "type": "string", "nullable": False},
        {"name": "amount", "type": "string", "nullable": False},
        {"name": "alpha", "type": "string", "nullable": True},
    ]},
))
print("created")
"""

VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
path = "/datasets/orders/versions/1/diff/2"
response = client.get(path)
assert response.status_code == 200, response.text
body = response.json()
assert set(body) == {"from_version", "to_version", "compatible", "changes"}
assert body["from_version"] == 1
assert body["to_version"] == 2
assert body["compatible"] is False
assert body["changes"] == [
    {
        "field": "alpha",
        "kind": "added",
        "before": None,
        "after": {"type": "string", "nullable": True},
    },
    {
        "field": "amount",
        "kind": "changed",
        "before": {"type": "integer", "nullable": True},
        "after": {"type": "string", "nullable": False},
    },
    {
        "field": "note",
        "kind": "removed",
        "before": {"type": "string", "nullable": True},
        "after": None,
    },
]
# Field order (zeta/note positions swapped relative to each other is not the
# point here; zeta is identical in both) never appears in the changes.
assert [c["field"] for c in body["changes"]] == sorted(
    c["field"] for c in body["changes"]
)

# Repeated reads are identical and read-only.
again = client.get(path)
assert again.status_code == 200, again.text
assert again.json() == body

# The reverse diff recomputes independently from persisted definitions:
# alpha is removed and amount still changes type, so it is incompatible too.
reverse = client.get("/datasets/orders/versions/2/diff/1")
assert reverse.status_code == 200, reverse.text
assert reverse.json()["compatible"] is False
reverse_changes = {c["field"]: c["kind"] for c in reverse.json()["changes"]}
assert reverse_changes == {"alpha": "removed", "amount": "changed", "note": "added"}
print("verified")
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


def test_diff_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-diff.db"
    assert _run_script(db_path, CREATE_SCRIPT) == "created"
    assert db_path.exists()
    assert _run_script(db_path, VERIFY_SCRIPT) == "verified"
