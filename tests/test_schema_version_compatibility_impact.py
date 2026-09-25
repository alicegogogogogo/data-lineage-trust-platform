"""Tests for the read-only compatibility check joined with lineage impact."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def F(name: str, ftype: str = "string", nullable: bool = True) -> dict:
    return {"name": name, "type": ftype, "nullable": nullable}


def create_dataset(client: TestClient, name: str) -> None:
    response = client.post("/datasets", json={"name": name})
    assert response.status_code == 201, response.text


def add_version(client: TestClient, dataset: str, fields: list[dict]) -> int:
    response = client.post(
        f"/datasets/{dataset}/versions", json={"fields": fields}
    )
    assert response.status_code == 201, response.text
    return response.json()["version"]


def add_link(
    client: TestClient,
    source: tuple[str, int, str],
    target: tuple[str, int, str],
) -> None:
    source_dataset, source_version, source_field = source
    target_dataset, target_version, target_field = target
    response = client.post(
        f"/datasets/{target_dataset}/versions/{target_version}/lineage",
        json={
            "target_dataset": target_dataset,
            "target_version": target_version,
            "target_field": target_field,
            "source_dataset": source_dataset,
            "source_version": source_version,
            "source_field": source_field,
        },
    )
    assert response.status_code == 201, response.text


def check(
    client: TestClient,
    base_version: int,
    target_version: int,
    dataset: str = "orders",
):
    response = client.get(
        f"/datasets/{dataset}/versions/{base_version}"
        f"/compatibility/{target_version}/impact"
    )
    assert response.status_code == 200, response.text
    return response


def check_body(
    client: TestClient,
    base_version: int,
    target_version: int,
    dataset: str = "orders",
) -> dict:
    return check(client, base_version, target_version, dataset).json()


def ref(dataset: str, version: int, field: str) -> dict:
    return {"dataset": dataset, "version": version, "field": field}


def setup_orders_with_lineage(client: TestClient) -> None:
    """orders v1 -> v2 breaks ``amount`` (type) and ``note`` (removed)."""
    create_dataset(client, "orders")
    add_version(
        client,
        "orders",
        [
            F("id", "integer", False),
            F("amount", "integer", True),
            F("note", "string", True),
        ],
    )
    add_version(
        client,
        "orders",
        [F("id", "integer", False), F("amount", "string", True)],
    )
    create_dataset(client, "dm_orders")
    add_version(client, "dm_orders", [F("amt"), F("extra"), F("note_copy")])
    create_dataset(client, "mart")
    add_version(client, "mart", [F("amt2")])
    # Direct and indirect downstream of the base field.
    add_link(client, ("orders", 1, "amount"), ("dm_orders", 1, "amt"))
    add_link(client, ("dm_orders", 1, "amt"), ("mart", 1, "amt2"))
    # Downstream of the removed field.
    add_link(client, ("orders", 1, "note"), ("dm_orders", 1, "note_copy"))
    # Downstream known only to the target version's same-named field.
    add_link(client, ("orders", 2, "amount"), ("dm_orders", 1, "extra"))


# --------------------------------------------------------------------------- #
# Empty / identical cases
# --------------------------------------------------------------------------- #


def test_same_version_has_no_breaking_changes(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("a"), F("b", "integer", False)])

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


def test_non_breaking_changes_have_no_entries(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", False)])
    add_version(
        client,
        "orders",
        [F("amount", "integer", True), F("note", "string", True)],
    )

    body = check_body(client, 1, 2)
    assert body["breaking_changes"] == []
    assert body["breaking_change_count"] == 0


# --------------------------------------------------------------------------- #
# Entries carry the downstream impact of the broken field
# --------------------------------------------------------------------------- #


def test_entries_merge_base_and_target_downstream(client: TestClient) -> None:
    setup_orders_with_lineage(client)

    body = check_body(client, 1, 2)
    assert body["breaking_change_count"] == 2
    assert [change["field"] for change in body["breaking_changes"]] == [
        "amount",
        "note",
    ]
    for change in body["breaking_changes"]:
        assert list(change) == ["field", "kind", "before", "after", "impacted"]

    amount, note = body["breaking_changes"]
    assert amount["kind"] == "type_changed"
    assert amount["before"] == {"type": "integer", "nullable": True}
    assert amount["after"] == {"type": "string", "nullable": True}
    # Direct + indirect downstream of the base field, merged with the target
    # field's own downstream, deduplicated and sorted.
    assert amount["impacted"] == [
        ref("dm_orders", 1, "amt"),
        ref("dm_orders", 1, "extra"),
        ref("mart", 1, "amt2"),
    ]

    assert note["kind"] == "removed"
    assert note["before"] == {"type": "string", "nullable": True}
    assert note["after"] is None
    assert note["impacted"] == [ref("dm_orders", 1, "note_copy")]


def test_nullable_tightening_carries_impact(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", True)])
    add_version(client, "orders", [F("amount", "integer", False)])
    create_dataset(client, "dm_orders")
    add_version(client, "dm_orders", [F("amt")])
    add_link(client, ("orders", 2, "amount"), ("dm_orders", 1, "amt"))

    body = check_body(client, 1, 2)
    assert body["breaking_changes"] == [
        {
            "field": "amount",
            "kind": "nullable_tightened",
            "before": {"type": "integer", "nullable": True},
            "after": {"type": "integer", "nullable": False},
            "impacted": [ref("dm_orders", 1, "amt")],
        }
    ]


def test_type_change_wins_over_nullable_tightening(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", True)])
    add_version(client, "orders", [F("amount", "string", False)])

    body = check_body(client, 1, 2)
    assert [change["kind"] for change in body["breaking_changes"]] == [
        "type_changed"
    ]
    assert body["breaking_changes"][0]["impacted"] == []


def test_field_without_downstream_has_empty_impacted(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("note", "string", True)])
    add_version(client, "orders", [F("other", "string", True)])

    body = check_body(client, 1, 2)
    assert body["breaking_change_count"] == 1
    assert body["breaking_changes"][0]["kind"] == "removed"
    assert body["breaking_changes"][0]["impacted"] == []


def test_impacted_excludes_start_and_survives_cycles(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", True)])
    add_version(client, "orders", [F("amount", "string", True)])
    create_dataset(client, "dm_a")
    add_version(client, "dm_a", [F("f")])
    create_dataset(client, "dm_b")
    add_version(client, "dm_b", [F("g")])
    # A downstream cycle: amount -> dm_a.f -> dm_b.g -> dm_a.f.
    add_link(client, ("orders", 1, "amount"), ("dm_a", 1, "f"))
    add_link(client, ("dm_a", 1, "f"), ("dm_b", 1, "g"))
    add_link(client, ("dm_b", 1, "g"), ("dm_a", 1, "f"))

    body = check_body(client, 1, 2)
    assert body["breaking_change_count"] == 1
    change = body["breaking_changes"][0]
    assert change["impacted"] == [ref("dm_a", 1, "f"), ref("dm_b", 1, "g")]
    # Neither start field ever appears in its own impacted set.
    assert ref("orders", 1, "amount") not in change["impacted"]
    assert ref("orders", 2, "amount") not in change["impacted"]


# --------------------------------------------------------------------------- #
# Errors: 404 / 422
# --------------------------------------------------------------------------- #


def test_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("id", "integer", False)])
    for path in (
        "/datasets/ghost/versions/1/compatibility/1/impact",
        "/datasets/orders/versions/9/compatibility/1/impact",
        "/datasets/orders/versions/1/compatibility/9/impact",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "not_found"


def test_404_takes_precedence_over_query_or_body(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("id", "integer", False)])

    assert (
        client.get(
            "/datasets/ghost/versions/1/compatibility/1/impact?bogus=1"
        ).status_code
        == 404
    )
    response = client.request(
        "GET",
        "/datasets/ghost/versions/1/compatibility/1/impact",
        content=b'{"bogus": 1}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404
    assert (
        client.get(
            "/datasets/orders/versions/9/compatibility/1/impact?bogus=1"
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/datasets/orders/versions/1/compatibility/9/impact?bogus=1"
        ).status_code
        == 404
    )


def test_non_positive_version_numbers_are_422(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("id", "integer", False)])

    for path in (
        "/datasets/orders/versions/0/compatibility/1/impact",
        "/datasets/orders/versions/1/compatibility/0/impact",
        "/datasets/orders/versions/-1/compatibility/1/impact",
        "/datasets/orders/versions/1/compatibility/-2/impact",
    ):
        response = client.get(path)
        assert response.status_code == 422, path
        assert response.json()["error"] == "validation_error"
        assert set(response.json()) == {"error", "detail"}


def test_non_integer_path_segments_are_422(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("id", "integer", False)])

    for path in (
        "/datasets/orders/versions/not-an-int/compatibility/1/impact",
        "/datasets/orders/versions/1/compatibility/not-an-int/impact",
        "/datasets/orders/versions/1.5/compatibility/1/impact",
    ):
        response = client.get(path)
        assert response.status_code == 422, path
        assert response.json()["error"] == "validation_error"


def test_unexpected_query_parameter_is_422(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("id", "integer", False)])

    for suffix in ("?x=1", "?x=", "?x=1&y=2", "?="):
        response = client.get(
            f"/datasets/orders/versions/1/compatibility/1/impact{suffix}"
        )
        assert response.status_code == 422, suffix
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]


def test_non_empty_body_is_422(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("id", "integer", False)])

    for kwargs in (
        {"content": b'{"x": 1}', "headers": {"content-type": "application/json"}},
        {"content": b"plain text"},
        {"content": b"["},
        {"content": b"  \n\t "},
    ):
        response = client.request(
            "GET",
            "/datasets/orders/versions/1/compatibility/1/impact",
            **kwargs,
        )
        assert response.status_code == 422, kwargs
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"


# --------------------------------------------------------------------------- #
# Read-only guarantees
# --------------------------------------------------------------------------- #


def test_rejected_request_writes_nothing(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("id", "integer", False)])
    before = check_body(client, 1, 1)

    client.get("/datasets/orders/versions/1/compatibility/1/impact?bogus=1")
    client.request(
        "GET",
        "/datasets/orders/versions/1/compatibility/1/impact",
        content=b'{"bogus": 1}',
        headers={"content-type": "application/json"},
    )
    client.get("/datasets/orders/versions/0/compatibility/1/impact")

    assert check_body(client, 1, 1) == before
    versions = client.get("/datasets/orders/versions").json()
    assert len(versions) == 1


def test_endpoint_never_touches_state(
    client: TestClient, isolated_database: Path
) -> None:
    setup_orders_with_lineage(client)
    first = check(client, 1, 2)

    # Repeated reads are byte-identical.
    again = check(client, 1, 2)
    assert again.text == first.text

    # The impact cache is never populated by this endpoint, and no lineage
    # mapping or version definition changed.
    conn = sqlite3.connect(isolated_database)
    try:
        cache_rows = conn.execute(
            "SELECT COUNT(*) FROM lineage_impact_cache"
        ).fetchone()[0]
        link_rows = conn.execute(
            "SELECT COUNT(*) FROM lineage_links"
        ).fetchone()[0]
        version_rows = conn.execute(
            "SELECT COUNT(*) FROM schema_versions"
        ).fetchone()[0]
    finally:
        conn.close()
    assert cache_rows == 0
    assert link_rows == 4
    assert version_rows == 4

    # The single-field impact query still computes the same downstream.
    response = client.get(
        "/datasets/orders/versions/1/lineage/impact",
        params={"field": "amount"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["impacted"] == [
        ref("dm_orders", 1, "amt"),
        ref("mart", 1, "amt2"),
    ]


def test_compatibility_check_response_is_unchanged(client: TestClient) -> None:
    setup_orders_with_lineage(client)

    response = client.get("/datasets/orders/versions/1/compatibility/2")
    assert response.status_code == 200, response.text
    body = response.json()
    assert list(body) == [
        "base_version",
        "target_version",
        "breaking_changes",
        "breaking_change_count",
    ]
    for change in body["breaking_changes"]:
        assert list(change) == ["field", "kind", "before", "after"]


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
    ]},
))
ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [
        {"name": "amount", "type": "string", "nullable": False},
    ]},
))
ok(client.post("/datasets", json={"name": "dm_orders"}))
ok(client.post(
    "/datasets/dm_orders/versions",
    json={"fields": [{"name": "amt", "type": "string", "nullable": True}]},
))
ok(client.post(
    "/datasets/dm_orders/versions/1/lineage",
    json={
        "target_dataset": "dm_orders",
        "target_version": 1,
        "target_field": "amt",
        "source_dataset": "orders",
        "source_version": 1,
        "source_field": "amount",
    },
))
print("created")
"""

VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
path = "/datasets/orders/versions/1/compatibility/2/impact"
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
        "impacted": [
            {"dataset": "dm_orders", "version": 1, "field": "amt"},
        ],
    },
    {
        "field": "note",
        "kind": "removed",
        "before": {"type": "string", "nullable": True},
        "after": None,
        "impacted": [],
    },
]
assert body["breaking_change_count"] == 2

# Repeated reads are identical and read-only.
again = client.get(path)
assert again.status_code == 200, again.text
assert again.text == response.text
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


def test_compatibility_impact_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-compatibility-impact.db"
    assert _run_script(db_path, CREATE_SCRIPT) == "created"
    assert db_path.exists()
    assert _run_script(db_path, VERIFY_SCRIPT) == "verified"
