"""Tests for the read-only schema version compatibility check endpoint."""

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


def check(
    client: TestClient, base_version: int, target_version: int, dataset: str = "orders"
):
    response = client.get(
        f"/datasets/{dataset}/versions/{base_version}"
        f"/compatibility/{target_version}"
    )
    assert response.status_code == 200, response.text
    return response


def check_body(
    client: TestClient, base_version: int, target_version: int, dataset: str = "orders"
) -> dict:
    return check(client, base_version, target_version, dataset).json()


def F(name: str, ftype: str = "string", nullable: bool = True) -> dict:
    return {"name": name, "type": ftype, "nullable": nullable}


# --------------------------------------------------------------------------- #
# Empty / identical cases
# --------------------------------------------------------------------------- #


def test_same_version_has_no_breaking_changes(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("a"), F("b", "integer", False)])

    response = check(client, 1, 1)
    body = response.json()
    assert list(body) == [
        "base_version",
        "target_version",
        "breaking_changes",
        "breaking_change_count",
    ]
    assert body == {
        "base_version": 1,
        "target_version": 1,
        "breaking_changes": [],
        "breaking_change_count": 0,
    }
    # The document ends with exactly one trailing newline.
    assert response.text.endswith("\n")
    assert not response.text.endswith("\n\n")


def test_identical_field_sets_have_no_breaking_changes(client: TestClient) -> None:
    create_dataset(client)
    fields = [F("a"), F("b", "integer", False)]
    add_version(client, fields)
    add_version(client, list(reversed(fields)))

    # Field reordering alone is not a change, in either direction.
    assert check_body(client, 1, 2)["breaking_changes"] == []
    assert check_body(client, 1, 2)["breaking_change_count"] == 0
    assert check_body(client, 2, 1)["breaking_changes"] == []
    assert check_body(client, 2, 1)["breaking_change_count"] == 0


# --------------------------------------------------------------------------- #
# Non-breaking changes
# --------------------------------------------------------------------------- #


def test_added_field_is_not_breaking(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])
    add_version(client, [F("id", "integer", False), F("note", "string", True)])

    body = check_body(client, 1, 2)
    assert body["breaking_changes"] == []
    assert body["breaking_change_count"] == 0


def test_nullable_loosening_is_not_breaking(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("amount", "integer", False)])
    add_version(client, [F("amount", "integer", True)])

    body = check_body(client, 1, 2)
    assert body["breaking_changes"] == []
    assert body["breaking_change_count"] == 0


# --------------------------------------------------------------------------- #
# Breaking changes: removal, type change, nullable tightening
# --------------------------------------------------------------------------- #


def test_removed_field_is_breaking(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False), F("note", "string", True)])
    add_version(client, [F("id", "integer", False)])

    body = check_body(client, 1, 2)
    assert body["breaking_change_count"] == 1
    assert body["breaking_changes"] == [
        {
            "field": "note",
            "kind": "removed",
            "before": {"type": "string", "nullable": True},
            "after": None,
        }
    ]

    # The reverse comparison reports the same field as an addition: no break.
    reverse = check_body(client, 2, 1)
    assert reverse["breaking_changes"] == []
    assert reverse["breaking_change_count"] == 0


def test_type_change_is_breaking(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("amount", "integer", True)])
    add_version(client, [F("amount", "string", True)])

    body = check_body(client, 1, 2)
    assert body["breaking_change_count"] == 1
    assert body["breaking_changes"] == [
        {
            "field": "amount",
            "kind": "type_changed",
            "before": {"type": "integer", "nullable": True},
            "after": {"type": "string", "nullable": True},
        }
    ]


def test_nullable_tightening_is_breaking(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("amount", "integer", True)])
    add_version(client, [F("amount", "integer", False)])

    body = check_body(client, 1, 2)
    assert body["breaking_change_count"] == 1
    assert body["breaking_changes"] == [
        {
            "field": "amount",
            "kind": "nullable_tightened",
            "before": {"type": "integer", "nullable": True},
            "after": {"type": "integer", "nullable": False},
        }
    ]


def test_type_change_wins_over_nullable_tightening(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("amount", "integer", True)])
    add_version(client, [F("amount", "string", False)])

    body = check_body(client, 1, 2)
    assert [change["kind"] for change in body["breaking_changes"]] == [
        "type_changed"
    ]


def test_breaking_changes_sorted_by_field_name(client: TestClient) -> None:
    create_dataset(client)
    add_version(
        client,
        [
            F("zeta", "integer", False),
            F("removed", "string", True),
            F("kept", "string", True),
            F("tightened", "string", True),
        ],
    )
    add_version(
        client,
        [
            F("kept", "string", True),
            F("alpha", "string", True),
            F("zeta", "string", False),
            F("tightened", "string", False),
        ],
    )

    body = check_body(client, 1, 2)
    assert [change["field"] for change in body["breaking_changes"]] == [
        "removed",
        "tightened",
        "zeta",
    ]
    kinds = {change["field"]: change["kind"] for change in body["breaking_changes"]}
    assert kinds == {
        "removed": "removed",
        "tightened": "nullable_tightened",
        "zeta": "type_changed",
    }
    # The added field "alpha" never appears; the count equals the entries.
    assert body["breaking_change_count"] == len(body["breaking_changes"]) == 3


def test_entry_shape_always_has_before_and_after_keys(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("a"), F("b", "integer", True)])
    add_version(client, [F("b", "string", False)])

    body = check_body(client, 1, 2)
    assert body["breaking_change_count"] == 2
    for change in body["breaking_changes"]:
        assert list(change) == ["field", "kind", "before", "after"]
    by_field = {change["field"]: change for change in body["breaking_changes"]}
    assert by_field["a"]["kind"] == "removed"
    assert by_field["a"]["before"] == {"type": "string", "nullable": True}
    assert by_field["a"]["after"] is None
    assert by_field["b"]["kind"] == "type_changed"
    assert by_field["b"]["before"] == {"type": "integer", "nullable": True}
    assert by_field["b"]["after"] == {"type": "string", "nullable": False}


# --------------------------------------------------------------------------- #
# Errors: 404 / 422
# --------------------------------------------------------------------------- #


def test_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])
    for path in (
        "/datasets/ghost/versions/1/compatibility/1",
        "/datasets/orders/versions/9/compatibility/1",
        "/datasets/orders/versions/1/compatibility/9",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "not_found"


def test_404_takes_precedence_over_query_or_body(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])

    assert (
        client.get("/datasets/ghost/versions/1/compatibility/1?bogus=1").status_code
        == 404
    )
    response = client.request(
        "GET",
        "/datasets/ghost/versions/1/compatibility/1",
        content=b'{"bogus": 1}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404
    assert (
        client.get("/datasets/orders/versions/9/compatibility/1?bogus=1").status_code
        == 404
    )
    assert (
        client.get("/datasets/orders/versions/1/compatibility/9?bogus=1").status_code
        == 404
    )


def test_non_positive_version_numbers_are_422(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])

    for path in (
        "/datasets/orders/versions/0/compatibility/1",
        "/datasets/orders/versions/1/compatibility/0",
        "/datasets/orders/versions/-1/compatibility/1",
        "/datasets/orders/versions/1/compatibility/-2",
    ):
        response = client.get(path)
        assert response.status_code == 422, path
        assert response.json()["error"] == "validation_error"
        assert set(response.json()) == {"error", "detail"}


def test_non_integer_path_segments_are_422(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])

    for path in (
        "/datasets/orders/versions/not-an-int/compatibility/1",
        "/datasets/orders/versions/1/compatibility/not-an-int",
        "/datasets/orders/versions/1.5/compatibility/1",
    ):
        response = client.get(path)
        assert response.status_code == 422, path
        assert response.json()["error"] == "validation_error"


def test_unexpected_query_parameter_is_422(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])
    add_version(client, [F("id", "integer", False), F("note", "string", True)])

    for suffix in ("?x=1", "?x=", "?x=1&y=2", "?="):
        response = client.get(
            f"/datasets/orders/versions/1/compatibility/2{suffix}"
        )
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
        {"content": b"  \n\t "},
    ):
        response = client.request(
            "GET", "/datasets/orders/versions/1/compatibility/1", **kwargs
        )
        assert response.status_code == 422, kwargs
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"


def test_rejected_request_writes_nothing(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])
    before = check_body(client, 1, 1)

    client.get("/datasets/orders/versions/1/compatibility/1?bogus=1")
    client.request(
        "GET",
        "/datasets/orders/versions/1/compatibility/1",
        content=b'{"bogus": 1}',
        headers={"content-type": "application/json"},
    )
    client.get("/datasets/orders/versions/0/compatibility/1")

    after = check_body(client, 1, 1)
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
path = "/datasets/orders/versions/1/compatibility/2"
response = client.get(path)
assert response.status_code == 200, response.text
assert response.text.endswith("\\n")
body = response.json()
assert list(body) == [
    "base_version",
    "target_version",
    "breaking_changes",
    "breaking_change_count",
]
assert body["base_version"] == 1
assert body["target_version"] == 2
assert body["breaking_changes"] == [
    {
        "field": "amount",
        "kind": "type_changed",
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
assert body["breaking_change_count"] == 2

# Repeated reads are identical and read-only.
again = client.get(path)
assert again.status_code == 200, again.text
assert again.text == response.text

# The reverse check recomputes independently from persisted definitions:
# alpha is removed and amount still changes type, so it breaks too.
reverse = client.get("/datasets/orders/versions/2/compatibility/1")
assert reverse.status_code == 200, reverse.text
reverse_body = reverse.json()
assert reverse_body["breaking_change_count"] == 2
reverse_kinds = {c["field"]: c["kind"] for c in reverse_body["breaking_changes"]}
assert reverse_kinds == {"alpha": "removed", "amount": "type_changed"}
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


def test_compatibility_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-compatibility.db"
    assert _run_script(db_path, CREATE_SCRIPT) == "created"
    assert db_path.exists()
    assert _run_script(db_path, VERIFY_SCRIPT) == "verified"
