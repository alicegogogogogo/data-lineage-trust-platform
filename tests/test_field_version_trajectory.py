"""Tests for the read-only per-field cross-version trajectory."""

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


def trajectory(
    client: TestClient, dataset: str = "orders", field: str = "amount"
):
    response = client.get(f"/datasets/{dataset}/fields/{field}/trajectory")
    assert response.status_code == 200, response.text
    return response


def trajectory_body(
    client: TestClient, dataset: str = "orders", field: str = "amount"
) -> dict:
    return trajectory(client, dataset, field).json()


def setup_orders_three_versions(client: TestClient) -> None:
    """amount: v1 integer/nullable -> v2 string/nullable (type change)
    -> v3 string/not nullable (tightening); note is removed in v2."""
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
    add_version(
        client,
        "orders",
        [F("id", "integer", False), F("amount", "string", False)],
    )
    create_dataset(client, "dm_orders")
    add_version(client, "dm_orders", [F("amt"), F("extra")])
    create_dataset(client, "mart")
    add_version(client, "mart", [F("amt2")])
    # Direct and indirect downstream of the base field.
    add_link(client, ("orders", 1, "amount"), ("dm_orders", 1, "amt"))
    add_link(client, ("dm_orders", 1, "amt"), ("mart", 1, "amt2"))
    # Downstream known only to the target version's same-named field.
    add_link(client, ("orders", 2, "amount"), ("dm_orders", 1, "extra"))


# --------------------------------------------------------------------------- #
# Trajectory entries
# --------------------------------------------------------------------------- #


def test_trajectory_lists_every_version_in_order(client: TestClient) -> None:
    setup_orders_three_versions(client)

    response = trajectory(client)
    body = response.json()
    assert list(body) == ["dataset", "field", "trajectory", "changes"]
    assert body["dataset"] == "orders"
    assert body["field"] == "amount"
    assert [entry["version"] for entry in body["trajectory"]] == [1, 2, 3]
    for entry in body["trajectory"]:
        assert list(entry) == ["version", "definition"]
    assert body["trajectory"][0]["definition"] == {
        "type": "integer",
        "nullable": True,
    }
    assert body["trajectory"][1]["definition"] == {
        "type": "string",
        "nullable": True,
    }
    assert body["trajectory"][2]["definition"] == {
        "type": "string",
        "nullable": False,
    }
    # The document ends with exactly one trailing newline.
    assert response.text.endswith("\n")
    assert not response.text.endswith("\n\n")


def test_definition_is_null_where_field_is_absent(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("id", "integer", False)])
    add_version(
        client, "orders", [F("id", "integer", False), F("amount", "integer")]
    )
    add_version(client, "orders", [F("id", "integer", False)])

    body = trajectory_body(client)
    assert [entry["definition"] for entry in body["trajectory"]] == [
        None,
        {"type": "integer", "nullable": True},
        None,
    ]
    # The null definition key is present, not omitted.
    assert all("definition" in entry for entry in body["trajectory"])
    assert [change["status"] for change in body["changes"]] == [
        "added",
        "removed",
    ]


def test_single_version_has_empty_changes(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", False)])

    body = trajectory_body(client)
    assert body["trajectory"] == [
        {"version": 1, "definition": {"type": "integer", "nullable": False}}
    ]
    assert body["changes"] == []


# --------------------------------------------------------------------------- #
# Status changes and impact
# --------------------------------------------------------------------------- #


def test_status_literals_cover_every_transition(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("id", "integer", False)])
    add_version(client, "orders", [F("id", "integer", False), F("f")])
    add_version(client, "orders", [F("id", "integer", False), F("f", "int")])
    add_version(client, "orders", [F("id", "integer", False), F("f", "int", False)])
    add_version(client, "orders", [F("id", "integer", False), F("f", "int", True)])
    add_version(client, "orders", [F("id", "integer", False), F("f", "int", True)])
    add_version(client, "orders", [F("id", "integer", False)])

    body = trajectory_body(client, field="f")
    assert [change["status"] for change in body["changes"]] == [
        "added",
        "type_changed",
        "nullable_tightened",
        "nullable_loosened",
        "unchanged",
        "removed",
    ]
    for change in body["changes"]:
        assert list(change) == [
            "base_version",
            "target_version",
            "status",
            "impacted",
            "impacted_datasets",
        ]
    assert [change["base_version"] for change in body["changes"]] == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]
    assert [change["target_version"] for change in body["changes"]] == [
        2,
        3,
        4,
        5,
        6,
        7,
    ]


def test_type_change_and_tightening_collapse_into_type_change(
    client: TestClient,
) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", True)])
    add_version(client, "orders", [F("amount", "string", False)])

    changes = trajectory_body(client)["changes"]
    assert [change["status"] for change in changes] == ["type_changed"]


def test_change_impact_matches_compatibility_impact_start_rule(
    client: TestClient,
) -> None:
    setup_orders_three_versions(client)

    body = trajectory_body(client)
    first, second = body["changes"]
    # v1 -> v2: traversal starts from the v1 and v2 same-named fields, so the
    # merged downstream set is dm_orders.amt, dm_orders.extra and mart.amt2,
    # sorted by dataset, version and field.
    assert first["status"] == "type_changed"
    assert first["impacted"] == [
        {"dataset": "dm_orders", "version": 1, "field": "amt"},
        {"dataset": "dm_orders", "version": 1, "field": "extra"},
        {"dataset": "mart", "version": 1, "field": "amt2"},
    ]
    assert first["impacted_datasets"] == ["dm_orders", "mart"]
    # v2 -> v3: only the v2/v3 fields start the traversal.
    assert second["status"] == "nullable_tightened"
    assert second["impacted"] == [
        {"dataset": "dm_orders", "version": 1, "field": "extra"}
    ]
    assert second["impacted_datasets"] == ["dm_orders"]


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

    change = trajectory_body(client)["changes"][0]
    # The cycle terminates and the start fields never count as impacted.
    assert change["impacted"] == [
        {"dataset": "dm_a", "version": 1, "field": "f"},
        {"dataset": "dm_b", "version": 1, "field": "g"},
    ]
    assert change["impacted_datasets"] == ["dm_a", "dm_b"]


def test_no_downstream_yields_empty_impact(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", True)])
    add_version(client, "orders", [F("amount", "string", True)])

    change = trajectory_body(client)["changes"][0]
    assert change["impacted"] == []
    assert change["impacted_datasets"] == []


# --------------------------------------------------------------------------- #
# Errors: 404 / 422
# --------------------------------------------------------------------------- #


def test_unknown_dataset_is_404(client: TestClient) -> None:
    response = client.get("/datasets/ghost/fields/amount/trajectory")
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "not_found"


def test_field_absent_from_every_version_is_404(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("id", "integer", False)])
    add_version(client, "orders", [F("id", "integer", False), F("note")])

    response = client.get("/datasets/orders/fields/ghost/trajectory")
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "not_found"


def test_404_takes_precedence_over_query_or_body(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount")])

    assert (
        client.get("/datasets/ghost/fields/amount/trajectory?bogus=1").status_code
        == 404
    )
    assert (
        client.get("/datasets/orders/fields/ghost/trajectory?bogus=1").status_code
        == 404
    )
    response = client.request(
        "GET",
        "/datasets/orders/fields/ghost/trajectory",
        content=b'{"bogus": 1}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404


def test_unexpected_query_parameter_is_422(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", False)])

    for suffix in ("?x=1", "?x=", "?x=1&y=2", "?="):
        response = client.get(f"/datasets/orders/fields/amount/trajectory{suffix}")
        assert response.status_code == 422, suffix
        assert response.json()["error"] == "validation_error"
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["detail"]


def test_non_empty_body_is_422(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", False)])

    for kwargs in (
        {"content": b'{"x": 1}', "headers": {"content-type": "application/json"}},
        {"content": b"plain text"},
        {"content": b"["},
        {"content": b" "},
        {"content": b"  \n\t "},
    ):
        response = client.request(
            "GET", "/datasets/orders/fields/amount/trajectory", **kwargs
        )
        assert response.status_code == 422, kwargs
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"


# --------------------------------------------------------------------------- #
# Read-only guarantees
# --------------------------------------------------------------------------- #


def test_rejected_request_writes_nothing(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", False)])
    before = trajectory_body(client)

    client.get("/datasets/orders/fields/amount/trajectory?bogus=1")
    client.request(
        "GET",
        "/datasets/orders/fields/amount/trajectory",
        content=b'{"bogus": 1}',
        headers={"content-type": "application/json"},
    )

    assert trajectory_body(client) == before
    versions = client.get("/datasets/orders/versions").json()
    assert len(versions) == 1


def test_endpoint_never_touches_state(
    client: TestClient, isolated_database: Path
) -> None:
    setup_orders_three_versions(client)
    first = trajectory(client)

    # Repeated reads are byte-identical.
    again = trajectory(client)
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
    assert link_rows == 3
    assert version_rows == 5

    # The pairwise compatibility impact response is unchanged.
    impact = client.get("/datasets/orders/versions/1/compatibility/2/impact")
    assert impact.status_code == 200, impact.text
    assert list(impact.json()) == [
        "base_version",
        "target_version",
        "breaking_changes",
        "breaking_change_count",
    ]


def test_quality_and_privacy_state_play_no_role(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", True)])
    add_version(client, "orders", [F("amount", "string", True)])
    before = trajectory_body(client)

    response = client.post(
        "/datasets/orders/versions/2/quality-rules",
        json={
            "name": "amount required",
            "kind": "not_null",
            "params": {"field": "amount"},
        },
    )
    assert response.status_code == 201, response.text
    response = client.post(
        "/datasets/orders/versions/2/privacy-policies",
        json={
            "field": "amount",
            "classification": "PII",
            "masking": "partial",
            "allowed_roles": [],
        },
    )
    assert response.status_code == 201, response.text

    assert trajectory_body(client) == before


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
path = "/datasets/orders/fields/amount/trajectory"
response = client.get(path)
assert response.status_code == 200, response.text
assert response.text.endswith("\\n")
body = response.json()
assert list(body) == ["dataset", "field", "trajectory", "changes"]
assert body == {
    "dataset": "orders",
    "field": "amount",
    "trajectory": [
        {"version": 1, "definition": {"type": "integer", "nullable": True}},
        {"version": 2, "definition": {"type": "string", "nullable": False}},
    ],
    "changes": [
        {
            "base_version": 1,
            "target_version": 2,
            "status": "type_changed",
            "impacted": [
                {"dataset": "dm_orders", "version": 1, "field": "amt"},
            ],
            "impacted_datasets": ["dm_orders"],
        }
    ],
}

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


def test_trajectory_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-field-trajectory.db"
    assert _run_script(db_path, CREATE_SCRIPT) == "created"
    assert db_path.exists()
    assert _run_script(db_path, VERIFY_SCRIPT) == "verified"
