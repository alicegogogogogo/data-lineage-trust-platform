"""Tests for the read-only per-dataset adjacent-version evolution summary."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SUMMARY_PATH = "/datasets/orders/evolution-summary"


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


def summary(
    client: TestClient, dataset: str = "orders", **kwargs
) -> object:
    response = client.get(f"/datasets/{dataset}/evolution-summary", **kwargs)
    assert response.status_code == 200, response.text
    return response


def summary_body(client: TestClient, dataset: str = "orders") -> dict:
    return summary(client, dataset).json()


def setup_versions_and_lineage(client: TestClient) -> None:
    """orders v1 -> v2 breaks two fields; v2 -> v3 breaks nothing.

    Pair (1, 2) downstream: dm_orders (two broken fields merge there) and
    mart (transitive only from ``amount``).
    """
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
        [
            F("id", "integer", False),
            F("amount", "string", True),
            F("extra", "string", True),
        ],
    )
    add_version(
        client,
        "orders",
        [
            F("id", "integer", False),
            F("amount", "string", True),
            F("extra", "string", True),
        ],
    )
    create_dataset(client, "dm_orders")
    add_version(client, "dm_orders", [F("amt"), F("extra_copy"), F("note_copy")])
    create_dataset(client, "mart")
    add_version(client, "mart", [F("amt2")])
    add_link(client, ("orders", 1, "amount"), ("dm_orders", 1, "amt"))
    add_link(client, ("dm_orders", 1, "amt"), ("mart", 1, "amt2"))
    add_link(client, ("orders", 1, "note"), ("dm_orders", 1, "note_copy"))
    add_link(client, ("orders", 2, "amount"), ("dm_orders", 1, "extra_copy"))


# --------------------------------------------------------------------------- #
# Shape and ordering
# --------------------------------------------------------------------------- #


def test_document_is_deterministic_compact_json(client: TestClient) -> None:
    setup_versions_and_lineage(client)

    response = summary(client)
    raw = response.text
    assert raw.endswith("\n")
    assert not raw.endswith("\n\n")
    # Compact whitespace: no insignificant spaces anywhere.
    assert '": ' not in raw
    assert ", " not in raw

    body = response.json()
    assert list(body) == ["dataset", "pairs", "totals"]
    assert body["dataset"] == "orders"
    assert len(body["pairs"]) == 2
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


def test_pairs_are_adjacent_and_include_non_breaking_ones(client: TestClient) -> None:
    setup_versions_and_lineage(client)

    body = summary_body(client)
    assert [
        (pair["base_version"], pair["target_version"]) for pair in body["pairs"]
    ] == [(1, 2), (2, 3)]

    first, second = body["pairs"]
    # amount type-changed and note removed; downstream merges to two dataset
    # names (dm_orders hosts both, mart is transitive from amount).
    assert first["breaking_count"] == 2
    assert first["impacted_datasets"] == ["dm_orders", "mart"]
    assert first["impacted_count"] == 2
    # No break between v2 and v3, but the pair is still listed.
    assert second == {
        "base_version": 2,
        "target_version": 3,
        "breaking_count": 0,
        "impacted_count": 0,
        "impacted_datasets": [],
    }

    assert body["totals"] == {
        "pair_count": 2,
        "breaking_count": 2,
        "impacted_count": 2,
    }


def test_fewer_than_two_versions_is_empty_success(client: TestClient) -> None:
    create_dataset(client, "orders")

    body = summary_body(client)
    assert body == {
        "dataset": "orders",
        "pairs": [],
        "totals": {
            "pair_count": 0,
            "breaking_count": 0,
            "impacted_count": 0,
        },
    }
    assert summary(client).text.endswith("\n")

    add_version(client, "orders", [F("id", "integer", False)])
    body = summary_body(client)
    assert body["pairs"] == []
    assert body["totals"] == {
        "pair_count": 0,
        "breaking_count": 0,
        "impacted_count": 0,
    }


def test_nullable_tightening_and_merged_changes_count_once(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", True)])
    # Type change plus nullable tightening on the same field: one entry.
    add_version(client, "orders", [F("amount", "string", False)])
    add_version(client, "orders", [F("amount", "string", True)])
    # Nullable loosening alone is not breaking.

    body = summary_body(client)
    assert [
        (pair["base_version"], pair["target_version"]) for pair in body["pairs"]
    ] == [(1, 2), (2, 3)]
    assert body["pairs"][0]["breaking_count"] == 1
    assert body["pairs"][0]["impacted_count"] == 0
    assert body["pairs"][0]["impacted_datasets"] == []
    assert body["pairs"][1]["breaking_count"] == 0
    assert body["totals"]["breaking_count"] == 1


def test_impacted_datasets_dedupe_per_pair_but_sum_across_pairs(
    client: TestClient,
) -> None:
    # The same downstream dataset is impacted independently by both pairs:
    # it counts once per pair, hence twice in the totals.
    create_dataset(client, "orders")
    add_version(client, "orders", [F("amount", "integer", True), F("email")])
    add_version(client, "orders", [F("amount", "string", True), F("email")])
    add_version(client, "orders", [F("amount", "string", True), F("email", "integer")])
    create_dataset(client, "dm_orders")
    add_version(client, "dm_orders", [F("amt"), F("mail")])
    add_link(client, ("orders", 2, "amount"), ("dm_orders", 1, "amt"))
    add_link(client, ("orders", 3, "email"), ("dm_orders", 1, "mail"))

    body = summary_body(client)
    assert body["pairs"][0]["impacted_datasets"] == ["dm_orders"]
    assert body["pairs"][0]["impacted_count"] == 1
    assert body["pairs"][1]["impacted_datasets"] == ["dm_orders"]
    assert body["pairs"][1]["impacted_count"] == 1
    assert body["totals"] == {
        "pair_count": 2,
        "breaking_count": 2,
        "impacted_count": 2,
    }


def test_cycle_terminates_and_start_fields_never_count(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(
        client,
        "orders",
        [F("amount", "integer", True), F("loopback", "string", True)],
    )
    add_version(
        client,
        "orders",
        [F("amount", "string", True), F("loopback", "string", True)],
    )
    create_dataset(client, "dm_a")
    add_version(client, "dm_a", [F("f"), F("g")])
    # amount -> dm_a.f -> orders v2 loopback -> dm_a.f (cycle); dm_a also
    # reached a second time via dm_a.g, which must not duplicate the name.
    add_link(client, ("orders", 1, "amount"), ("dm_a", 1, "f"))
    add_link(client, ("dm_a", 1, "f"), ("orders", 2, "loopback"))
    add_link(client, ("orders", 2, "loopback"), ("dm_a", 1, "f"))
    add_link(client, ("orders", 2, "amount"), ("dm_a", 1, "g"))

    body = summary_body(client)
    pair = body["pairs"][0]
    assert pair["breaking_count"] == 1
    # dm_a is reached through several fields but named once; the traversal
    # re-enters the start dataset at a different field (loopback), while the
    # start fields themselves (orders 1/2 amount) never appear.
    assert pair["impacted_datasets"] == ["dm_a", "orders"]
    assert pair["impacted_count"] == 2


# --------------------------------------------------------------------------- #
# Errors: 404 / 422
# --------------------------------------------------------------------------- #


def test_unknown_dataset_is_404(client: TestClient) -> None:
    response = client.get("/datasets/ghost/evolution-summary")
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "not_found"
    assert response.json()["detail"]
    # No SQL or stack trace leaks into the error document.
    assert "Traceback" not in response.text
    assert "SELECT" not in response.text


def test_404_takes_precedence_over_query_or_body(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("id", "integer", False)])

    assert (
        client.get("/datasets/ghost/evolution-summary?bogus=1").status_code == 404
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
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]


def test_any_body_is_422_including_whitespace(client: TestClient) -> None:
    create_dataset(client, "orders")
    add_version(client, "orders", [F("id", "integer", False)])

    for kwargs in (
        {"content": b'{"x": 1}', "headers": {"content-type": "application/json"}},
        {"content": b"plain text"},
        {"content": b"["},
        {"content": b"  \n\t "},
        {"content": b" "},
    ):
        response = client.request(
            "GET", "/datasets/orders/evolution-summary", **kwargs
        )
        assert response.status_code == 422, kwargs
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert "Traceback" not in response.text


# --------------------------------------------------------------------------- #
# Read-only guarantees
# --------------------------------------------------------------------------- #


def test_repeated_reads_are_byte_identical_and_write_nothing(
    client: TestClient, isolated_database: Path
) -> None:
    setup_versions_and_lineage(client)
    first = summary(client)
    again = summary(client)
    assert again.text == first.text

    conn = sqlite3.connect(isolated_database)
    try:
        cache_rows = conn.execute(
            "SELECT COUNT(*) FROM lineage_impact_cache"
        ).fetchone()[0]
        link_rows = conn.execute("SELECT COUNT(*) FROM lineage_links").fetchone()[0]
        version_rows = conn.execute(
            "SELECT COUNT(*) FROM schema_versions"
        ).fetchone()[0]
    finally:
        conn.close()
    assert cache_rows == 0
    assert link_rows == 4
    assert version_rows == 5


def test_rejected_request_writes_nothing(client: TestClient) -> None:
    setup_versions_and_lineage(client)
    before = summary(client).text

    client.get("/datasets/orders/evolution-summary?bogus=1")
    client.request(
        "GET",
        "/datasets/orders/evolution-summary",
        content=b" ",
    )
    client.get("/datasets/ghost/evolution-summary")

    assert summary(client).text == before


def test_quality_and_privacy_state_do_not_affect_summary(client: TestClient) -> None:
    setup_versions_and_lineage(client)
    before = summary(client).text

    rule = client.post(
        "/datasets/orders/versions/1/quality-rules",
        json={"name": "id required", "kind": "not_null", "params": {"field": "id"}},
    )
    assert rule.status_code == 201, rule.text
    policy = client.post(
        "/datasets/orders/versions/1/privacy-policies",
        json={
            "field": "note",
            "classification": "PII",
            "masking": "partial",
            "allowed_roles": [],
        },
    )
    assert policy.status_code == 201, policy.text

    assert summary(client).text == before


def test_pairwise_endpoints_are_unchanged(client: TestClient) -> None:
    setup_versions_and_lineage(client)

    compatibility = client.get(
        "/datasets/orders/versions/1/compatibility/2"
    ).json()
    assert list(compatibility) == [
        "base_version",
        "target_version",
        "breaking_changes",
        "breaking_change_count",
    ]
    assert compatibility["breaking_change_count"] == 2
    impact = client.get(
        "/datasets/orders/versions/1/compatibility/2/impact"
    ).json()
    assert impact["breaking_change_count"] == 2
    assert [change["field"] for change in impact["breaking_changes"]] == [
        "amount",
        "note",
    ]
    # The single-field lineage impact response and cache behave as before.
    single = client.get(
        "/datasets/orders/versions/1/lineage/impact", params={"field": "amount"}
    )
    assert single.status_code == 200
    assert [item["dataset"] for item in single.json()["impacted"]] == [
        "dm_orders",
        "mart",
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
assert not response.text.endswith("\\n\\n")
assert response.json() == {
    "dataset": "orders",
    "pairs": [
        {
            "base_version": 1,
            "target_version": 2,
            "breaking_count": 2,
            "impacted_count": 1,
            "impacted_datasets": ["dm_orders"],
        },
        {
            "base_version": 2,
            "target_version": 3,
            "breaking_count": 0,
            "impacted_count": 0,
            "impacted_datasets": [],
        },
    ],
    "totals": {
        "pair_count": 2,
        "breaking_count": 2,
        "impacted_count": 1,
    },
}
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
