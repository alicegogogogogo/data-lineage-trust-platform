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
    """orders v1 -> v2 changes ``amount``'s type and tightens its
    nullability; v2 -> v3 loosens the nullability back. ``note`` exists
    only in v1, ``extra`` only from v3."""
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
        [F("id", "integer", False), F("amount", "string", False)],
    )
    add_version(
        client,
        "orders",
        [F("id", "integer", False), F("amount", "string", True), F("extra")],
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
# Trajectory entries
# --------------------------------------------------------------------------- #


def test_entries_cover_every_version_in_order(client: TestClient) -> None:
    setup_orders_three_versions(client)

    response = trajectory(client)
    body = response.json()
    assert list(body) == ["dataset", "field", "entries", "changes"]
    assert body["dataset"] == "orders"
    assert body["field"] == "amount"
    assert [entry["version"] for entry in body["entries"]] == [1, 2, 3]
    for entry in body["entries"]:
        assert list(entry) == ["version", "definition"]
    assert body["entries"][0]["definition"] == {
        "type": "integer",
        "nullable": True,
    }
    assert body["entries"][1]["definition"] == {
        "type": "string",
        "nullable": False,
    }
    assert body["entries"][2]["definition"] == {
        "type": "string",
        "nullable": True,
    }
    # The document ends with exactly one trailing newline.
    assert response.text.endswith("\n")
    assert not response.text.endswith("\n\n")


def test_absent_field_definition_is_null_not_omitted(client: TestClient) -> None:
    setup_orders_three_versions(client)

    body = trajectory_body(client, field="note")
    assert body["entries"] == [
        {"version": 1, "definition": {"type": "string", "nullable": True}},
        {"version": 2, "definition": None},
        {"version": 3, "definition": None},
    ]
    for entry in body["entries"]:
        assert "definition" in entry

    body = trajectory_body(client, field="extra")
    assert body["entries"] == [
        {"version": 1, "definition": None},
        {"version": 2, "definition": None},
        {"version": 3, "definition": {"type": "string", "nullable": True}},
    ]


def test_definition_only_reads_field_definitions(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", True)])
    add_version(client, "orders", [F("amount", "string", True)])
    before = trajectory_body(client)

    # Quality, privacy, snapshot and task state play no role.
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
    response = client.post(
        "/datasets/orders/versions/2/snapshots",
        json={"rows": [{"amount": "1"}]},
    )
    assert response.status_code == 201, response.text
    response = client.post(
        "/datasets/orders/versions/2/processing-tasks",
        json={"name": "extract"},
    )
    assert response.status_code == 201, response.text

    assert trajectory_body(client) == before


# --------------------------------------------------------------------------- #
# Status changes
# --------------------------------------------------------------------------- #


def test_changes_cover_every_adjacent_pair_in_order(client: TestClient) -> None:
    setup_orders_three_versions(client)

    body = trajectory_body(client)
    assert [(c["base_version"], c["target_version"]) for c in body["changes"]] == [
        (1, 2),
        (2, 3),
    ]
    for change in body["changes"]:
        assert list(change) == [
            "base_version",
            "target_version",
            "status",
            "impacted",
            "impacted_datasets",
        ]
    # v1 -> v2: type change and nullable tightening collapse into one
    # status, the type change winning.
    assert body["changes"][0]["status"] == "type_changed"
    # v2 -> v3: only the nullability relaxes.
    assert body["changes"][1]["status"] == "nullable_loosened"


def test_nullable_tightening_status(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", True)])
    add_version(client, "orders", [F("amount", "integer", False)])

    assert trajectory_body(client)["changes"][0]["status"] == "nullable_tightened"


def test_added_and_removed_status(client: TestClient) -> None:
    setup_orders_three_versions(client)

    added = trajectory_body(client, field="extra")["changes"]
    assert [change["status"] for change in added] == ["unchanged", "added"]

    removed = trajectory_body(client, field="note")["changes"]
    assert [change["status"] for change in removed] == ["removed", "unchanged"]


def test_unchanged_status(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("id", "integer", False), F("amount")])
    add_version(client, "orders", [F("id", "integer", True), F("amount")])

    changes = trajectory_body(client)["changes"]
    assert changes[0]["status"] == "unchanged"
    assert changes[0]["impacted"] == []
    assert changes[0]["impacted_datasets"] == []


def test_fewer_than_two_versions_returns_empty_changes(
    client: TestClient,
) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", True)])

    body = trajectory_body(client)
    assert body["entries"] == [
        {"version": 1, "definition": {"type": "integer", "nullable": True}}
    ]
    assert body["changes"] == []


# --------------------------------------------------------------------------- #
# Downstream impact of each change
# --------------------------------------------------------------------------- #


def test_change_impact_matches_pairwise_impact_response(
    client: TestClient,
) -> None:
    setup_orders_three_versions(client)

    impact = client.get("/datasets/orders/versions/1/compatibility/2/impact")
    assert impact.status_code == 200, impact.text
    pairwise = {
        change["field"]: change["impacted"]
        for change in impact.json()["breaking_changes"]
    }

    change = trajectory_body(client)["changes"][0]
    # Same start-field rule as the pairwise impact response for the broken
    # field: base and target same-named fields both seed the traversal.
    assert change["status"] == "type_changed"
    assert change["impacted"] == pairwise["amount"]
    assert change["impacted"] == [
        {"dataset": "dm_orders", "version": 1, "field": "amt"},
        {"dataset": "dm_orders", "version": 1, "field": "extra"},
        {"dataset": "mart", "version": 1, "field": "amt2"},
    ]
    assert change["impacted_datasets"] == ["dm_orders", "mart"]


def test_removed_field_impact_starts_from_base_only(client: TestClient) -> None:
    setup_orders_three_versions(client)

    change = trajectory_body(client, field="note")["changes"][0]
    assert change["status"] == "removed"
    assert change["impacted"] == [
        {"dataset": "dm_orders", "version": 1, "field": "note_copy"}
    ]
    assert change["impacted_datasets"] == ["dm_orders"]


def test_added_field_impact_starts_from_target_only(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("id", "integer", False)])
    add_version(client, "orders", [F("id", "integer", False), F("extra")])
    create_dataset(client, "dm_orders")
    add_version(client, "dm_orders", [F("x")])
    add_link(client, ("orders", 2, "extra"), ("dm_orders", 1, "x"))

    change = trajectory_body(client, field="extra")["changes"][0]
    assert change["status"] == "added"
    assert change["impacted"] == [
        {"dataset": "dm_orders", "version": 1, "field": "x"}
    ]
    assert change["impacted_datasets"] == ["dm_orders"]


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
    assert change["status"] == "type_changed"
    # The cycle terminates and the start fields never count as impacted.
    assert change["impacted"] == [
        {"dataset": "dm_a", "version": 1, "field": "f"},
        {"dataset": "dm_b", "version": 1, "field": "g"},
    ]
    assert change["impacted_datasets"] == ["dm_a", "dm_b"]


def test_impacted_sorted_and_datasets_deduplicated(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", True)])
    add_version(client, "orders", [F("amount", "string", True)])
    create_dataset(client, "zz_mart")
    add_version(client, "zz_mart", [F("x"), F("y")])
    create_dataset(client, "aa_mart")
    add_version(client, "aa_mart", [F("w")])
    add_link(client, ("orders", 1, "amount"), ("zz_mart", 1, "y"))
    add_link(client, ("orders", 2, "amount"), ("zz_mart", 1, "x"))
    add_link(client, ("orders", 2, "amount"), ("aa_mart", 1, "w"))

    change = trajectory_body(client)["changes"][0]
    assert change["impacted"] == [
        {"dataset": "aa_mart", "version": 1, "field": "w"},
        {"dataset": "zz_mart", "version": 1, "field": "x"},
        {"dataset": "zz_mart", "version": 1, "field": "y"},
    ]
    # Sorted ascending, each dataset listed once.
    assert change["impacted_datasets"] == ["aa_mart", "zz_mart"]


def test_no_downstream_yields_empty_collections(client: TestClient) -> None:
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
    add_version(client, "orders", [F("id", "integer", False), F("amount")])

    response = client.get("/datasets/orders/fields/ghost/trajectory")
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "not_found"


def test_dataset_without_versions_rejects_any_field(client: TestClient) -> None:
    create_dataset(client, "orders")

    response = client.get("/datasets/orders/fields/amount/trajectory")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_404_takes_precedence_over_query_or_body(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount")])

    # Unknown dataset wins over the request shape checks.
    assert (
        client.get("/datasets/ghost/fields/amount/trajectory?bogus=1").status_code
        == 404
    )
    response = client.request(
        "GET",
        "/datasets/ghost/fields/amount/trajectory",
        content=b'{"bogus": 1}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404

    # A field absent from every version also wins over the shape checks.
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
    add_version(client, "orders", [F("amount", "integer", True)])

    for suffix in ("?x=1", "?x=", "?x=1&y=2", "?="):
        response = client.get(
            f"/datasets/orders/fields/amount/trajectory{suffix}"
        )
        assert response.status_code == 422, suffix
        assert response.json()["error"] == "validation_error"
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["detail"]


def test_non_empty_body_is_422(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", True)])

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
    add_version(client, "orders", [F("amount", "integer", True)])
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
    assert link_rows == 4
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
assert list(body) == ["dataset", "field", "entries", "changes"]
assert body == {
    "dataset": "orders",
    "field": "amount",
    "entries": [
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


def test_field_trajectory_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-field-trajectory.db"
    assert _run_script(db_path, CREATE_SCRIPT) == "created"
    assert db_path.exists()
    assert _run_script(db_path, VERIFY_SCRIPT) == "verified"
