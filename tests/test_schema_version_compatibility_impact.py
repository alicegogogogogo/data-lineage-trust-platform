"""Tests for the read-only breaking-change impact endpoint.

The endpoint appends ``/impact`` to the two-version compatibility check and
joins each breaking entry with the direct and indirect downstream fields of
the breaking field along lineage mappings.
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


def add_version(client: TestClient, fields: list[dict], dataset: str = "orders") -> int:
    response = client.post(
        f"/datasets/{dataset}/versions", json={"fields": fields}
    )
    assert response.status_code == 201, response.text
    return response.json()["version"]


def make_dataset(client: TestClient, name: str, fields: list[str]) -> None:
    create_dataset(client, name)
    add_version(
        client,
        [{"name": f, "type": "string", "nullable": True} for f in fields],
        dataset=name,
    )


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


def impact_check(
    client: TestClient, base_version: int, target_version: int, dataset: str = "orders"
):
    response = client.get(
        f"/datasets/{dataset}/versions/{base_version}"
        f"/compatibility/{target_version}/impact"
    )
    assert response.status_code == 200, response.text
    return response


def impact_body(
    client: TestClient, base_version: int, target_version: int, dataset: str = "orders"
) -> dict:
    return impact_check(client, base_version, target_version, dataset).json()


def F(name: str, ftype: str = "string", nullable: bool = True) -> dict:
    return {"name": name, "type": ftype, "nullable": nullable}


def ref(dataset: str, version: int, field: str) -> dict:
    return {"dataset": dataset, "version": version, "field": field}


# --------------------------------------------------------------------------- #
# Empty / identical cases
# --------------------------------------------------------------------------- #


def test_same_version_has_empty_entries_and_zero_count(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("a"), F("b", "integer", False)])

    response = impact_check(client, 1, 1)
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
    assert response.text.endswith("\n")
    assert not response.text.endswith("\n\n")


def test_identical_versions_have_no_entries(client: TestClient) -> None:
    create_dataset(client)
    fields = [F("a"), F("b", "integer", False)]
    add_version(client, fields)
    add_version(client, list(reversed(fields)))

    assert impact_body(client, 1, 2)["breaking_changes"] == []
    assert impact_body(client, 2, 1)["breaking_change_count"] == 0


def test_non_breaking_changes_have_no_entries(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False), F("amount", "integer", False)])
    add_version(
        client,
        [
            F("id", "integer", False),
            F("amount", "integer", True),
            F("note", "string", True),
        ],
    )

    body = impact_body(client, 1, 2)
    assert body["breaking_changes"] == []
    assert body["breaking_change_count"] == 0


# --------------------------------------------------------------------------- #
# Entry shape and the three breaking kinds
# --------------------------------------------------------------------------- #


def test_entries_have_five_keys_in_order(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("a"), F("b", "integer", True)])
    add_version(client, [F("b", "string", False)])

    body = impact_body(client, 1, 2)
    assert body["breaking_change_count"] == 2
    for change in body["breaking_changes"]:
        assert list(change) == ["field", "kind", "before", "after", "impacted"]


def test_removed_entry_uses_base_start_and_null_after(client: TestClient) -> None:
    make_dataset(client, "orders", ["note"])
    make_dataset(client, "down", ["d1"])
    add_link(client, ("orders", 1, "note"), ("down", 1, "d1"))
    # A version must keep at least one field; remove "note" in v2 by replacing
    # the field set with a different field.
    add_version(client, [F("other")], dataset="orders")

    body = impact_body(client, 1, 2)
    assert body["breaking_changes"] == [
        {
            "field": "note",
            "kind": "removed",
            "before": {"type": "string", "nullable": True},
            "after": None,
            "impacted": [ref("down", 1, "d1")],
        }
    ]


def test_type_change_entry_carries_both_sides(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("amount", "integer", True)])
    add_version(client, [F("amount", "string", True)])

    entry = impact_body(client, 1, 2)["breaking_changes"][0]
    assert entry["kind"] == "type_changed"
    assert entry["before"] == {"type": "integer", "nullable": True}
    assert entry["after"] == {"type": "string", "nullable": True}
    assert entry["impacted"] == []


def test_nullable_tightening_entry_carries_both_sides(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("amount", "integer", True)])
    add_version(client, [F("amount", "integer", False)])

    entry = impact_body(client, 1, 2)["breaking_changes"][0]
    assert entry["kind"] == "nullable_tightened"
    assert entry["before"] == {"type": "integer", "nullable": True}
    assert entry["after"] == {"type": "integer", "nullable": False}
    assert entry["impacted"] == []


def test_type_change_wins_over_nullable_tightening(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("amount", "integer", True)])
    add_version(client, [F("amount", "string", False)])

    entries = impact_body(client, 1, 2)["breaking_changes"]
    assert [change["kind"] for change in entries] == ["type_changed"]


# --------------------------------------------------------------------------- #
# Impact traversal
# --------------------------------------------------------------------------- #


def test_impact_merges_base_and_target_downstreams(client: TestClient) -> None:
    make_dataset(client, "orders", ["amount"])
    make_dataset(client, "base_only", ["b1"])
    make_dataset(client, "tgt_only", ["t1"])
    # orders v1.amount -> base_only.b1
    add_link(client, ("orders", 1, "amount"), ("base_only", 1, "b1"))

    # v2 keeps "amount" (tightened), so its own downstream joins the base's.
    add_version(client, [F("amount", "string", False)], dataset="orders")
    add_link(client, ("orders", 2, "amount"), ("tgt_only", 1, "t1"))

    body = impact_body(client, 1, 2)
    assert body["breaking_changes"] == [
        {
            "field": "amount",
            "kind": "nullable_tightened",
            "before": {"type": "string", "nullable": True},
            "after": {"type": "string", "nullable": False},
            "impacted": [
                ref("base_only", 1, "b1"),
                ref("tgt_only", 1, "t1"),
            ],
        }
    ]


def test_removed_field_only_traces_base_side(client: TestClient) -> None:
    make_dataset(client, "orders", ["note"])
    make_dataset(client, "base_down", ["b1"])
    add_link(client, ("orders", 1, "note"), ("base_down", 1, "b1"))
    # v2 has no "note"; give the version another field and a downstream
    # that must never appear in the removed field's impact.
    make_dataset(client, "other_down", ["o1"])
    add_version(client, [F("other")], dataset="orders")
    add_link(client, ("orders", 2, "other"), ("other_down", 1, "o1"))

    entry = impact_body(client, 1, 2)["breaking_changes"][0]
    assert entry["field"] == "note"
    assert entry["impacted"] == [ref("base_down", 1, "b1")]


def test_impact_is_direct_indirect_deduped_and_sorted(client: TestClient) -> None:
    make_dataset(client, "orders", ["amount", "kept"])
    make_dataset(client, "mid", ["m1", "m2"])
    make_dataset(client, "sink", ["s1"])
    # Diamond: amount reaches s1 two ways; m1 and m2 are both direct.
    add_link(client, ("orders", 1, "amount"), ("mid", 1, "m1"))
    add_link(client, ("orders", 1, "amount"), ("mid", 1, "m2"))
    add_link(client, ("mid", 1, "m1"), ("sink", 1, "s1"))
    add_link(client, ("mid", 1, "m2"), ("sink", 1, "s1"))

    add_version(
        client, [F("amount", "integer", True), F("kept")], dataset="orders"
    )

    entry = impact_body(client, 1, 2)["breaking_changes"][0]
    assert entry["field"] == "amount"
    assert entry["impacted"] == [
        ref("mid", 1, "m1"),
        ref("mid", 1, "m2"),
        ref("sink", 1, "s1"),
    ]


def test_impact_excludes_starts_even_across_a_cycle(client: TestClient) -> None:
    make_dataset(client, "orders", ["amount"])
    make_dataset(client, "cyc_y", ["f"])
    make_dataset(client, "cyc_z", ["f"])
    add_link(client, ("orders", 1, "amount"), ("cyc_y", 1, "f"))
    add_link(client, ("cyc_y", 1, "f"), ("cyc_z", 1, "f"))
    add_link(client, ("cyc_z", 1, "f"), ("orders", 1, "amount"))

    add_version(client, [F("amount", "integer", True)], dataset="orders")

    entry = impact_body(client, 1, 2)["breaking_changes"][0]
    assert ref("orders", 1, "amount") not in entry["impacted"]
    assert ref("orders", 2, "amount") not in entry["impacted"]
    assert entry["impacted"] == [
        ref("cyc_y", 1, "f"),
        ref("cyc_z", 1, "f"),
    ]


def test_target_start_excluded_when_it_is_reachable_from_base(
    client: TestClient,
) -> None:
    # A mapping from orders v1.amount to orders v2.amount is impossible
    # directly (same dataset), so route through one dataset: the target
    # version's field is reachable from the base start and must not appear
    # in its own impact list.
    make_dataset(client, "orders", ["amount"])
    make_dataset(client, "relay", ["r1"])
    add_link(client, ("orders", 1, "amount"), ("relay", 1, "r1"))
    add_version(client, [F("amount", "integer", False)], dataset="orders")
    add_link(client, ("relay", 1, "r1"), ("orders", 2, "amount"))

    entry = impact_body(client, 1, 2)["breaking_changes"][0]
    assert entry["impacted"] == [ref("relay", 1, "r1")]


def test_entries_stay_sorted_by_field_with_impact(client: TestClient) -> None:
    make_dataset(client, "orders", ["zeta", "removed", "tight"])
    make_dataset(client, "down", ["d1"])
    add_link(client, ("orders", 1, "zeta"), ("down", 1, "d1"))
    add_version(
        client,
        [F("tight", "string", False), F("alpha")],
        dataset="orders",
    )

    body = impact_body(client, 1, 2)
    assert [c["field"] for c in body["breaking_changes"]] == [
        "removed",
        "tight",
        "zeta",
    ]
    assert body["breaking_change_count"] == 3
    by_field = {c["field"]: c for c in body["breaking_changes"]}
    assert by_field["removed"]["impacted"] == []
    assert by_field["zeta"]["impacted"] == [ref("down", 1, "d1")]


# --------------------------------------------------------------------------- #
# Deterministic serialization
# --------------------------------------------------------------------------- #


def test_document_is_compact_with_trailing_newline(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("amount", "integer", True)])
    add_version(client, [F("amount", "integer", False)])

    response = impact_check(client, 1, 2)
    assert response.headers["content-type"] == "application/json"
    assert response.text.endswith("\n")
    assert not response.text.endswith("\n\n")
    # Compact: no whitespace outside strings.
    assert "\n" not in response.text[:-1]
    assert ": " not in response.text
    assert ", " not in response.text
    # Booleans are lowercase; the tightened entry carries both sides.
    assert '"nullable":false' in response.text
    assert '"after":null' not in response.text

    # A later version that removes the field keeps the literal null after.
    removed_setup = client.post(
        "/datasets/orders/versions", json={"fields": [F("other")]}
    )
    assert removed_setup.status_code == 201
    removed_text = impact_check(client, 1, 3).text
    assert '"after":null' in removed_text


def test_repeated_reads_are_byte_identical(client: TestClient) -> None:
    make_dataset(client, "orders", ["amount"])
    make_dataset(client, "down", ["d1"])
    add_link(client, ("orders", 1, "amount"), ("down", 1, "d1"))
    add_version(client, [F("amount", "integer", False)], dataset="orders")

    first = impact_check(client, 1, 2).text
    second = impact_check(client, 1, 2).text
    assert first == second


# --------------------------------------------------------------------------- #
# Errors: 404 / 422
# --------------------------------------------------------------------------- #


def test_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])
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
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])

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
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])

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
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])

    for path in (
        "/datasets/orders/versions/not-an-int/compatibility/1/impact",
        "/datasets/orders/versions/1/compatibility/not-an-int/impact",
        "/datasets/orders/versions/1.5/compatibility/1/impact",
    ):
        response = client.get(path)
        assert response.status_code == 422, path
        assert response.json()["error"] == "validation_error"


def test_unexpected_query_parameter_is_422(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])

    for suffix in ("?x=1", "?x=", "?x=1&y=2", "?="):
        response = client.get(
            f"/datasets/orders/versions/1/compatibility/1/impact{suffix}"
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
            "GET",
            "/datasets/orders/versions/1/compatibility/1/impact",
            **kwargs,
        )
        assert response.status_code == 422, kwargs
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"


def test_rejected_request_writes_nothing(client: TestClient) -> None:
    create_dataset(client)
    add_version(client, [F("id", "integer", False)])
    before = impact_body(client, 1, 1)

    client.get("/datasets/orders/versions/1/compatibility/1/impact?bogus=1")
    client.request(
        "GET",
        "/datasets/orders/versions/1/compatibility/1/impact",
        content=b'{"bogus": 1}',
        headers={"content-type": "application/json"},
    )
    client.get("/datasets/orders/versions/0/compatibility/1/impact")

    assert impact_body(client, 1, 1) == before
    versions = client.get("/datasets/orders/versions").json()
    assert len(versions) == 1


def test_errors_never_expose_sql_or_internals(client: TestClient) -> None:
    response = client.get(
        "/datasets/orders/versions/1/compatibility/x/impact"
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "Traceback" not in detail
    assert "sqlite" not in detail.lower()
    assert "SELECT" not in detail


# --------------------------------------------------------------------------- #
# Read-only: no impact-cache writes, existing endpoints untouched
# --------------------------------------------------------------------------- #


def test_impact_endpoint_never_populates_cache(
    client: TestClient, isolated_database: Path
) -> None:
    import sqlite3

    make_dataset(client, "orders", ["amount"])
    make_dataset(client, "down", ["d1"])
    add_link(client, ("orders", 1, "amount"), ("down", 1, "d1"))
    add_version(client, [F("amount", "integer", False)], dataset="orders")

    impact_check(client, 1, 2)
    impact_check(client, 1, 2)

    conn = sqlite3.connect(isolated_database)
    try:
        count = conn.execute("SELECT COUNT(*) FROM lineage_impact_cache").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_compatibility_and_lineage_impact_endpoints_keep_their_shape(
    client: TestClient,
) -> None:
    make_dataset(client, "orders", ["amount"])
    make_dataset(client, "down", ["d1"])
    add_link(client, ("orders", 1, "amount"), ("down", 1, "d1"))
    add_version(client, [F("amount", "integer", False)], dataset="orders")

    compat = client.get("/datasets/orders/versions/1/compatibility/2")
    assert compat.status_code == 200
    entry = compat.json()["breaking_changes"][0]
    assert list(entry) == ["field", "kind", "before", "after"]
    assert "impacted" not in entry

    single = client.get(
        "/datasets/orders/versions/1/lineage/impact", params={"field": "amount"}
    )
    assert single.status_code == 200
    assert list(single.json()) == ["source", "impacted"]


# --------------------------------------------------------------------------- #
# Stability across process restarts
# --------------------------------------------------------------------------- #


CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response):
    assert response.status_code in (200, 201), response.text

ok(client.post("/datasets", json={"name": "orders"}))
ok(client.post("/datasets", json={"name": "mid"}))
ok(client.post("/datasets", json={"name": "sink"}))
ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [
        {"name": "amount", "type": "integer", "nullable": True},
        {"name": "note", "type": "string", "nullable": True},
    ]},
))
ok(client.post(
    "/datasets/mid/versions",
    json={"fields": [{"name": "m1", "type": "string", "nullable": True}]},
))
ok(client.post(
    "/datasets/sink/versions",
    json={"fields": [{"name": "s1", "type": "string", "nullable": True}]},
))
ok(client.post("/datasets/mid/versions/1/lineage", json={
    "target_dataset": "mid", "target_version": 1, "target_field": "m1",
    "source_dataset": "orders", "source_version": 1, "source_field": "amount",
}))
ok(client.post("/datasets/sink/versions/1/lineage", json={
    "target_dataset": "sink", "target_version": 1, "target_field": "s1",
    "source_dataset": "mid", "source_version": 1, "source_field": "m1",
}))
ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [
        {"name": "amount", "type": "string", "nullable": False},
    ]},
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
            {"dataset": "mid", "version": 1, "field": "m1"},
            {"dataset": "sink", "version": 1, "field": "s1"},
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

again = client.get(path)
assert again.status_code == 200, again.text
assert again.text == response.text

# Self comparison stays empty.
same = client.get("/datasets/orders/versions/2/compatibility/2/impact")
assert same.json()["breaking_changes"] == []
assert same.json()["breaking_change_count"] == 0
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
