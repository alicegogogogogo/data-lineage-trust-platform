"""Tests for the read-only per-dataset adjacent-version evolution summary."""

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


def summary(client: TestClient, dataset: str = "orders"):
    response = client.get(f"/datasets/{dataset}/evolution-summary")
    assert response.status_code == 200, response.text
    return response


def summary_body(client: TestClient, dataset: str = "orders") -> dict:
    return summary(client, dataset).json()


def setup_orders_three_versions(client: TestClient) -> None:
    """orders v1 -> v2 breaks ``amount`` (type) and ``note`` (removed);
    v2 -> v3 only loosens nullability and adds a field (no breaking)."""
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
        [F("id", "integer", True), F("amount", "string", True), F("extra")],
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
# Empty / few-version cases
# --------------------------------------------------------------------------- #


def test_dataset_without_versions_has_empty_pairs(client: TestClient) -> None:
    create_dataset(client, "orders")

    response = summary(client)
    body = response.json()
    assert list(body) == ["dataset", "pairs", "totals"]
    assert body == {
        "dataset": "orders",
        "pairs": [],
        "totals": {"pair_count": 0, "breaking_count": 0, "impacted_count": 0},
    }
    # The document ends with exactly one trailing newline.
    assert response.text.endswith("\n")
    assert not response.text.endswith("\n\n")


def test_single_version_has_empty_pairs(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("id", "integer", False)])

    body = summary_body(client)
    assert body["pairs"] == []
    assert body["totals"] == {
        "pair_count": 0,
        "breaking_count": 0,
        "impacted_count": 0,
    }


def test_pair_without_breaking_changes_is_listed(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", False)])
    add_version(
        client,
        "orders",
        [F("amount", "integer", True), F("note", "string", True)],
    )

    body = summary_body(client)
    assert body["pairs"] == [
        {
            "base_version": 1,
            "target_version": 2,
            "breaking_count": 0,
            "impacted_count": 0,
            "impacted_datasets": [],
        }
    ]
    assert body["totals"] == {
        "pair_count": 1,
        "breaking_count": 0,
        "impacted_count": 0,
    }


# --------------------------------------------------------------------------- #
# Pair entries and totals
# --------------------------------------------------------------------------- #


def test_pairs_cover_every_adjacent_step_in_order(client: TestClient) -> None:
    setup_orders_three_versions(client)

    response = summary(client)
    body = response.json()
    assert list(body) == ["dataset", "pairs", "totals"]
    assert [pair["base_version"] for pair in body["pairs"]] == [1, 2]
    assert [pair["target_version"] for pair in body["pairs"]] == [2, 3]
    for pair in body["pairs"]:
        assert list(pair) == [
            "base_version",
            "target_version",
            "breaking_count",
            "impacted_count",
            "impacted_datasets",
        ]
    assert list(body["totals"]) == [
        "pair_count",
        "breaking_count",
        "impacted_count",
    ]

    first, second = body["pairs"]
    # v1 -> v2: two breaking entries; the impacted sets of both broken fields
    # merge and deduplicate (dm_orders.amt, dm_orders.extra, dm_orders.note_copy,
    # mart.amt2).
    assert first["breaking_count"] == 2
    assert first["impacted_count"] == 4
    assert first["impacted_datasets"] == ["dm_orders", "mart"]
    # v2 -> v3: no breaking change, still listed.
    assert second["breaking_count"] == 0
    assert second["impacted_count"] == 0
    assert second["impacted_datasets"] == []

    assert body["totals"] == {
        "pair_count": 2,
        "breaking_count": 2,
        "impacted_count": 4,
    }


def test_pair_impact_matches_pairwise_impact_response(
    client: TestClient,
) -> None:
    setup_orders_three_versions(client)

    impact = client.get("/datasets/orders/versions/1/compatibility/2/impact")
    assert impact.status_code == 200, impact.text
    changes = impact.json()["breaking_changes"]
    pairwise_refs = {
        (item["dataset"], item["version"], item["field"])
        for change in changes
        for item in change["impacted"]
    }

    pair = summary_body(client)["pairs"][0]
    assert pair["breaking_count"] == impact.json()["breaking_change_count"]
    assert pair["impacted_count"] == len(pairwise_refs)
    assert pair["impacted_datasets"] == sorted(
        {ref[0] for ref in pairwise_refs}
    )


def test_multiple_changes_of_one_field_count_once(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", True)])
    add_version(client, "orders", [F("amount", "string", False)])

    pair = summary_body(client)["pairs"][0]
    # Type change and nullable tightening on the same field merge into one
    # breaking entry (the kind literal stays the pairwise response's).
    assert pair["breaking_count"] == 1


def test_nullable_tightening_counts_as_breaking(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", True)])
    add_version(client, "orders", [F("amount", "integer", False)])
    create_dataset(client, "dm_orders")
    add_version(client, "dm_orders", [F("amt")])
    add_link(client, ("orders", 2, "amount"), ("dm_orders", 1, "amt"))

    pair = summary_body(client)["pairs"][0]
    assert pair["breaking_count"] == 1
    assert pair["impacted_count"] == 1
    assert pair["impacted_datasets"] == ["dm_orders"]


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

    pair = summary_body(client)["pairs"][0]
    assert pair["breaking_count"] == 1
    # The cycle terminates and the start fields never count as impacted.
    assert pair["impacted_count"] == 2
    assert pair["impacted_datasets"] == ["dm_a", "dm_b"]


def test_impacted_datasets_deduplicated_and_sorted(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("a"), F("b")])
    add_version(client, "orders", [F("c")])
    create_dataset(client, "zz_mart")
    add_version(client, "zz_mart", [F("x"), F("y")])
    create_dataset(client, "aa_mart")
    add_version(client, "aa_mart", [F("w")])
    add_link(client, ("orders", 1, "a"), ("zz_mart", 1, "x"))
    add_link(client, ("orders", 1, "b"), ("zz_mart", 1, "y"))
    add_link(client, ("orders", 1, "b"), ("aa_mart", 1, "w"))

    pair = summary_body(client)["pairs"][0]
    assert pair["breaking_count"] == 2
    assert pair["impacted_count"] == 3
    # Sorted ascending, each dataset listed once.
    assert pair["impacted_datasets"] == ["aa_mart", "zz_mart"]


# --------------------------------------------------------------------------- #
# Errors: 404 / 422
# --------------------------------------------------------------------------- #


def test_unknown_dataset_is_404(client: TestClient) -> None:
    response = client.get("/datasets/ghost/evolution-summary")
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "not_found"


def test_404_takes_precedence_over_query_or_body(client: TestClient) -> None:
    create_dataset(client, "orders")

    assert (
        client.get("/datasets/ghost/evolution-summary?bogus=1").status_code
        == 404
    )
    response = client.request(
        "GET",
        "/datasets/ghost/evolution-summary",
        content=b'{"bogus": 1}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404


def test_unexpected_query_parameter_is_422(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("id", "integer", False)])

    for suffix in ("?x=1", "?x=", "?x=1&y=2", "?="):
        response = client.get(f"/datasets/orders/evolution-summary{suffix}")
        assert response.status_code == 422, suffix
        assert response.json()["error"] == "validation_error"
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["detail"]


def test_non_empty_body_is_422(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("id", "integer", False)])

    for kwargs in (
        {"content": b'{"x": 1}', "headers": {"content-type": "application/json"}},
        {"content": b"plain text"},
        {"content": b"["},
        {"content": b" "},
        {"content": b"  \n\t "},
    ):
        response = client.request(
            "GET", "/datasets/orders/evolution-summary", **kwargs
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
    before = summary_body(client)

    client.get("/datasets/orders/evolution-summary?bogus=1")
    client.request(
        "GET",
        "/datasets/orders/evolution-summary",
        content=b'{"bogus": 1}',
        headers={"content-type": "application/json"},
    )

    assert summary_body(client) == before
    versions = client.get("/datasets/orders/versions").json()
    assert len(versions) == 1


def test_endpoint_never_touches_state(
    client: TestClient, isolated_database: Path
) -> None:
    setup_orders_three_versions(client)
    first = summary(client)

    # Repeated reads are byte-identical.
    again = summary(client)
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


def test_quality_and_privacy_state_play_no_role(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", True)])
    add_version(client, "orders", [F("amount", "string", True)])
    before = summary_body(client)

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

    assert summary_body(client) == before


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
path = "/datasets/orders/evolution-summary"
response = client.get(path)
assert response.status_code == 200, response.text
assert response.text.endswith("\\n")
body = response.json()
assert list(body) == ["dataset", "pairs", "totals"]
assert body == {
    "dataset": "orders",
    "pairs": [
        {
            "base_version": 1,
            "target_version": 2,
            "breaking_count": 2,
            "impacted_count": 1,
            "impacted_datasets": ["dm_orders"],
        }
    ],
    "totals": {"pair_count": 1, "breaking_count": 2, "impacted_count": 1},
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


def test_evolution_summary_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-evolution-summary.db"
    assert _run_script(db_path, CREATE_SCRIPT) == "created"
    assert db_path.exists()
    assert _run_script(db_path, VERIFY_SCRIPT) == "verified"
