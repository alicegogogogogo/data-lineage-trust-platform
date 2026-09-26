"""Tests for field-level lineage mapping deletion.

Deletion shares the registration entry point: a DELETE submits the same
complete mapping. These tests cover the 200/404/422 contract, 404-before-422
precedence, read-after-delete graph recomputation, impact-cache invalidation,
single-winner concurrency, restart persistence and the tightened
repeated-`field` contract of the lineage read endpoints.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

LINEAGE_PATH = "/datasets/dm_orders/versions/1/lineage"


def make_dataset(client: TestClient, name: str, fields: list[dict]) -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions", json={"fields": fields}
    )
    assert response.status_code == 201, response.text


def payload(
    *,
    target: str = "dm_orders",
    target_version: int = 1,
    target_field: str = "id",
    source: str = "raw_orders",
    source_version: int = 1,
    source_field: str = "order_id",
) -> dict:
    return {
        "target_dataset": target,
        "target_version": target_version,
        "target_field": target_field,
        "source_dataset": source,
        "source_version": source_version,
        "source_field": source_field,
    }


def setup_two_datasets(client: TestClient) -> None:
    make_dataset(
        client,
        "raw_orders",
        [
            {"name": "order_id", "type": "integer", "nullable": False},
            {"name": "amount", "type": "decimal", "nullable": True},
        ],
    )
    make_dataset(
        client,
        "dm_orders",
        [
            {"name": "id", "type": "integer", "nullable": False},
            {"name": "total", "type": "decimal", "nullable": True},
            {"name": "orphan", "type": "string", "nullable": True},
        ],
    )


def register(client: TestClient, body: dict) -> dict:
    response = client.post(LINEAGE_PATH, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def register_link(client: TestClient, body: dict) -> dict:
    response = client.post(
        f"/datasets/{body['target_dataset']}/versions/"
        f"{body['target_version']}/lineage",
        json=body,
    )
    assert response.status_code == 201, response.text
    return response.json()


def link_count(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM lineage_links").fetchone()[0]
    finally:
        conn.close()


def cache_rows(db_path: Path) -> list[tuple[str, int, str]]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT source_dataset, source_version, source_field "
            "FROM lineage_impact_cache ORDER BY 1, 2, 3"
        ).fetchall()
    finally:
        conn.close()


def impact(
    client: TestClient, dataset: str, version: int, field: str
) -> list[dict]:
    response = client.get(
        f"/datasets/{dataset}/versions/{version}/lineage/impact",
        params={"field": field},
    )
    assert response.status_code == 200, response.text
    return response.json()["impacted"]


# --------------------------------------------------------------------------- #
# Successful deletion
# --------------------------------------------------------------------------- #


def test_delete_registered_mapping_returns_200_and_the_mapping(
    client: TestClient,
) -> None:
    setup_two_datasets(client)
    body = payload()
    register(client, body)

    response = client.request("DELETE", LINEAGE_PATH, json=body)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "target_dataset": "dm_orders",
        "target_version": 1,
        "target_field": "id",
        "source": {
            "dataset": "raw_orders",
            "version": 1,
            "field": "order_id",
        },
    }


def test_delete_removes_mapping_from_target_sources(client: TestClient) -> None:
    setup_two_datasets(client)
    make_dataset(
        client,
        "raw_refunds",
        [{"name": "refund_amount", "type": "decimal", "nullable": True}],
    )
    register(client, payload(source="raw_orders", source_field="amount",
                             target_field="total"))
    other = payload(
        source="raw_refunds", source_field="refund_amount", target_field="total"
    )
    register(client, other)

    deleted = client.request(
        "DELETE",
        LINEAGE_PATH,
        json=payload(source="raw_orders", source_field="amount",
                     target_field="total"),
    )
    assert deleted.status_code == 200, deleted.text

    lineage = client.get(LINEAGE_PATH).json()
    by_field = {f["target_field"]: f["sources"] for f in lineage["fields"]}
    # The target field stays, with only the surviving source, same ordering.
    assert by_field["total"] == [
        {"dataset": "raw_refunds", "version": 1, "field": "refund_amount"}
    ]
    assert by_field["id"] == []
    assert by_field["orphan"] == []
    assert [f["target_field"] for f in lineage["fields"]] == [
        "id",
        "orphan",
        "total",
    ]


def test_deleted_edge_is_gone_from_impact_and_paths(client: TestClient) -> None:
    setup_two_datasets(client)
    make_dataset(
        client,
        "mart",
        [{"name": "m_id", "type": "integer", "nullable": False}],
    )
    # raw_orders.order_id -> dm_orders.id -> mart.m_id
    register(client, payload())
    register_link(
        client,
        payload(
            target="mart",
            target_version=1,
            target_field="m_id",
            source="dm_orders",
            source_version=1,
            source_field="id",
        ),
    )
    # Warm the persistent cache before deleting.
    assert impact(client, "raw_orders", 1, "order_id") == [
        {"dataset": "dm_orders", "version": 1, "field": "id"},
        {"dataset": "mart", "version": 1, "field": "m_id"},
    ]

    response = client.request("DELETE", LINEAGE_PATH, json=payload())
    assert response.status_code == 200

    # The impact query recomputes from the post-delete graph: no old
    # downstream of the removed edge is returned.
    assert impact(client, "raw_orders", 1, "order_id") == []
    # dm_orders.id still feeds mart.m_id through the surviving mapping.
    assert impact(client, "dm_orders", 1, "id") == [
        {"dataset": "mart", "version": 1, "field": "m_id"}
    ]

    paths = client.get(
        "/datasets/raw_orders/versions/1/lineage/impact-paths",
        params={"field": "order_id"},
    )
    assert paths.status_code == 200
    assert paths.json()["impacts"] == []

    # Source tracing up from mart.m_id no longer reaches raw_orders.order_id.
    sources = client.get(
        "/datasets/mart/versions/1/lineage/impact/source-paths",
        params={"field": "m_id"},
    )
    assert sources.status_code == 200
    origin_keys = {
        (o["dataset"], o["version"], o["field"])
        for o in sources.json()["origins"]
    }
    assert origin_keys == {("dm_orders", 1, "id")}


def test_deleting_a_mapping_that_does_not_exist_returns_404(
    client: TestClient,
) -> None:
    setup_two_datasets(client)
    # Every referenced resource exists; only the mapping itself is absent.
    response = client.request("DELETE", LINEAGE_PATH, json=payload())

    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "not_found"
    assert isinstance(body["detail"], str) and body["detail"]
    assert "traceback" not in response.text.lower()


def test_delete_then_readd_then_delete_succeeds(client: TestClient) -> None:
    setup_two_datasets(client)
    body = payload()
    assert client.request("DELETE", LINEAGE_PATH, json=body).status_code == 404

    register(client, body)
    first = client.request("DELETE", LINEAGE_PATH, json=body)
    second = client.request("DELETE", LINEAGE_PATH, json=body)
    assert first.status_code == 200
    assert second.status_code == 404

    # Re-registering a previously deleted mapping is a fresh registration.
    recreated = client.post(LINEAGE_PATH, json=body)
    assert recreated.status_code == 201


# --------------------------------------------------------------------------- #
# 404 precedence over request-shape errors
# --------------------------------------------------------------------------- #


def test_unknown_path_dataset_returns_404_even_for_empty_body(
    client: TestClient,
) -> None:
    setup_two_datasets(client)
    response = client.request(
        "DELETE", "/datasets/ghost/versions/1/lineage", content=b"{}"
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_unknown_path_version_returns_404_for_every_bad_shape(
    client: TestClient,
) -> None:
    setup_two_datasets(client)
    url = "/datasets/dm_orders/versions/99/lineage"
    for content in (b"", b"   ", b"{not json", b"[]", b"{}"):
        response = client.request(
            "DELETE", url, content=content,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 404, content


def test_unknown_source_dataset_returns_404_before_shape_errors(
    client: TestClient,
) -> None:
    setup_two_datasets(client)
    # Malformed shape (five fields missing) but the source dataset is
    # readable and missing: the 404 wins.
    response = client.request(
        "DELETE", LINEAGE_PATH, json={"source_dataset": "ghost"}
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_unknown_source_resources_return_404(client: TestClient) -> None:
    setup_two_datasets(client)
    register(client, payload())

    for change in (
        {"source_version": 2},
        {"source_field": "missing"},
    ):
        response = client.request(
            "DELETE", LINEAGE_PATH, json=payload(**change)
        )
        assert response.status_code == 404, change
        assert response.json()["error"] == "not_found"


def test_unknown_target_field_returns_404(client: TestClient) -> None:
    setup_two_datasets(client)
    response = client.request(
        "DELETE", LINEAGE_PATH, json=payload(target_field="missing")
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


# --------------------------------------------------------------------------- #
# Shape validation (422), only after every resource resolves
# --------------------------------------------------------------------------- #


def test_shape_errors_return_422_and_write_nothing(
    client: TestClient, isolated_database: Path
) -> None:
    setup_two_datasets(client)
    good = payload()
    register(client, good)
    cache_before = cache_rows(isolated_database)
    assert link_count(isolated_database) == 1

    valid = payload()
    malformed_bodies = [
        {k: v for k, v in valid.items() if k != "source_field"},  # missing
        {**valid, "unexpected": 1},                               # extra
        {**valid, "target_version": "1"},                         # wrong type
        {**valid, "source_version": True},                        # bool, not int
        {**valid, "target_field": 123},                           # non-string
        {**valid, "source_field": "   "},                         # blank name
        {**valid, "target_field": ""},                            # empty name
    ]
    for body in malformed_bodies:
        response = client.request(
            "DELETE",
            LINEAGE_PATH,
            content=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422, body
        assert response.json()["error"] == "validation_error"

    for content in (b"", b"   ", b"{", b"[]", b"null", b'"a string"', b"123"):
        response = client.request(
            "DELETE",
            LINEAGE_PATH,
            content=content,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422, content

    response = client.request("DELETE", LINEAGE_PATH + "?bogus=1", json=valid)
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"

    # No rejected request changed lineage or the impact cache.
    assert link_count(isolated_database) == 1
    assert cache_rows(isolated_database) == cache_before
    lineage = client.get(LINEAGE_PATH).json()
    assert lineage["fields"][0]["sources"] == [
        {"dataset": "raw_orders", "version": 1, "field": "order_id"}
    ]


def test_same_source_and_target_dataset_returns_422(client: TestClient) -> None:
    setup_two_datasets(client)
    response = client.request(
        "DELETE",
        LINEAGE_PATH,
        json=payload(source="dm_orders", source_version=1, source_field="total"),
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_target_mismatching_path_returns_422(client: TestClient) -> None:
    setup_two_datasets(client)
    # Target names an existing other dataset/version; the disagreement with
    # the path is a shape error, not a lookup against the body target.
    response = client.request(
        "DELETE",
        LINEAGE_PATH,
        json=payload(target="raw_orders", target_field="order_id"),
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_missing_mapping_404_does_not_touch_cache(
    client: TestClient, isolated_database: Path
) -> None:
    setup_two_datasets(client)
    impact(client, "raw_orders", 1, "order_id")
    cache_before = cache_rows(isolated_database)

    response = client.request("DELETE", LINEAGE_PATH, json=payload())
    assert response.status_code == 404
    assert cache_rows(isolated_database) == cache_before


# --------------------------------------------------------------------------- #
# Impact cache invalidation
# --------------------------------------------------------------------------- #


def test_delete_invalidates_source_and_upstream_cache(
    client: TestClient, isolated_database: Path
) -> None:
    # raw_a.a1 -> mid_b.b1 -> mart_c.c1
    #          -> mid_b.b2 -> mart_c.c1
    for name, fields in (
        ("raw_a", ["a1", "a2"]),
        ("mid_b", ["b1", "b2"]),
        ("mart_c", ["c1"]),
        ("lonely", ["l1"]),
    ):
        make_dataset(
            client,
            name,
            [{"name": f, "type": "string", "nullable": True} for f in fields],
        )
    for source, target in (
        (("raw_a", "a1"), ("mid_b", "b1")),
        (("raw_a", "a1"), ("mid_b", "b2")),
        (("mid_b", "b1"), ("mart_c", "c1")),
        (("mid_b", "b2"), ("mart_c", "c1")),
    ):
        (src_ds, src_field), (dst_ds, dst_field) = source, target
        register_link(
            client,
            payload(
                target=dst_ds,
                target_field=dst_field,
                source=src_ds,
                source_field=src_field,
            ),
        )

    impact(client, "raw_a", 1, "a1")       # upstream of the deleted edge
    impact(client, "mid_b", 1, "b1")       # the deleted edge's source
    impact(client, "mart_c", 1, "c1")     # unaffected sink
    impact(client, "lonely", 1, "l1")     # unrelated

    # Delete the edge mid_b.b1 -> mart_c.c1.
    response = client.request(
        "DELETE",
        "/datasets/mart_c/versions/1/lineage",
        content=json.dumps(
            {
                "target_dataset": "mart_c",
                "target_version": 1,
                "target_field": "c1",
                "source_dataset": "mid_b",
                "source_version": 1,
                "source_field": "b1",
            }
        ).encode(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200, response.text

    remaining = cache_rows(isolated_database)
    # Source of the removed edge (mid_b.b1) and every field reaching it
    # (raw_a.a1) are rebuilt; unrelated entries survive.
    assert ("mid_b", 1, "b1") not in remaining
    assert ("raw_a", 1, "a1") not in remaining
    assert ("mart_c", 1, "c1") in remaining
    assert ("lonely", 1, "l1") in remaining

    # Recomputed from the post-delete graph: b1 no longer points anywhere;
    # a1 still directly reaches b1 and reaches c1 only via b2 (the result
    # set happens to stay the same, but the entry was rebuilt).
    assert impact(client, "mid_b", 1, "b1") == []
    assert impact(client, "raw_a", 1, "a1") == [
        {"dataset": "mart_c", "version": 1, "field": "c1"},
        {"dataset": "mid_b", "version": 1, "field": "b1"},
        {"dataset": "mid_b", "version": 1, "field": "b2"},
    ]


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_deletes_have_single_winner(
    client: TestClient, isolated_database: Path
) -> None:
    setup_two_datasets(client)
    body = payload()
    register(client, body)
    raw = json.dumps(body).encode()

    barrier = threading.Barrier(2)
    results: list[int] = []
    results_lock = threading.Lock()

    def delete() -> None:
        barrier.wait()
        response = client.request(
            "DELETE",
            LINEAGE_PATH,
            content=raw,
            headers={"content-type": "application/json"},
        )
        with results_lock:
            results.append(response.status_code)

    threads = [threading.Thread(target=delete) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [200, 404]
    assert link_count(isolated_database) == 0
    # The loser changed nothing: a fresh delete still reports the mapping gone.
    assert client.request("DELETE", LINEAGE_PATH, json=body).status_code == 404


def test_concurrent_delete_and_register_leave_a_consistent_graph(
    client: TestClient, isolated_database: Path
) -> None:
    setup_two_datasets(client)
    body = payload()
    raw = json.dumps(body).encode()

    def register_one(barrier: threading.Barrier, outcomes: list[int]) -> None:
        barrier.wait()
        response = client.post(
            LINEAGE_PATH,
            content=raw,
            headers={"content-type": "application/json"},
        )
        outcomes.append(response.status_code)

    def delete_one(barrier: threading.Barrier, outcomes: list[int]) -> None:
        barrier.wait()
        response = client.request(
            "DELETE",
            LINEAGE_PATH,
            content=raw,
            headers={"content-type": "application/json"},
        )
        outcomes.append(response.status_code)

    # Repeated cycles: whichever operation commits first, the final graph
    # contains the mapping at most once and every subsequent read agrees with
    # that state.
    for _ in range(8):
        register(client, body)
        barrier = threading.Barrier(2)
        outcomes: list[int] = []
        threads = [
            threading.Thread(target=register_one, args=(barrier, outcomes)),
            threading.Thread(target=delete_one, args=(barrier, outcomes)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert set(outcomes) <= {200, 201, 404, 409}, outcomes
        assert link_count(isolated_database) in (0, 1)
        lineage = client.get(LINEAGE_PATH).json()
        id_sources = next(
            f["sources"] for f in lineage["fields"] if f["target_field"] == "id"
        )
        assert len(id_sources) == link_count(isolated_database)
        # Bring the graph back to empty deterministically for the next cycle.
        if link_count(isolated_database) == 1:
            cleanup = client.request(
                "DELETE",
                LINEAGE_PATH,
                content=raw,
                headers={"content-type": "application/json"},
            )
            assert cleanup.status_code == 200


# --------------------------------------------------------------------------- #
# Persistence across a process restart
# --------------------------------------------------------------------------- #


def test_deletion_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "restart-lineage.db"
    create_script = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response):
    assert response.status_code in (200, 201), response.text

ok(client.post("/datasets", json={"name": "raw_orders"}))
ok(client.post("/datasets", json={"name": "dm_orders"}))
ok(client.post(
    "/datasets/raw_orders/versions",
    json={"fields": [
        {"name": "order_id", "type": "integer", "nullable": False},
        {"name": "amount", "type": "decimal", "nullable": True},
    ]},
))
ok(client.post(
    "/datasets/dm_orders/versions",
    json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
))
body = {
    "target_dataset": "dm_orders",
    "target_version": 1,
    "target_field": "id",
    "source_dataset": "raw_orders",
    "source_version": 1,
    "source_field": "order_id",
}
ok(client.post("/datasets/dm_orders/versions/1/lineage", json=body))
# Register a second mapping that must survive the deletion.
ok(client.post(
    "/datasets/dm_orders/versions/1/lineage",
    json={
        "target_dataset": "dm_orders",
        "target_version": 1,
        "target_field": "id",
        "source_dataset": "raw_orders",
        "source_version": 1,
        "source_field": "amount",
    },
))
deleted = client.request(
    "DELETE",
    "/datasets/dm_orders/versions/1/lineage",
    content=json.dumps(body).encode(),
    headers={"content-type": "application/json"},
)
assert deleted.status_code == 200, deleted.text
"""
    env = {**os.environ, "DATA_LINEAGE_DB": str(db_path)}
    create = subprocess.run(
        [sys.executable, "-c", create_script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert create.returncode == 0, create.stderr

    verify_script = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
lineage = client.get("/datasets/dm_orders/versions/1/lineage")
assert lineage.status_code == 200, lineage.text
[id_field] = [f for f in lineage.json()["fields"] if f["target_field"] == "id"]
assert id_field["sources"] == [
    {"dataset": "raw_orders", "version": 1, "field": "amount"}
], id_field["sources"]

# The deleted mapping is still absent after the restart.
deleted_again = client.request(
    "DELETE",
    "/datasets/dm_orders/versions/1/lineage",
    content=b'''{
        "target_dataset": "dm_orders",
        "target_version": 1,
        "target_field": "id",
        "source_dataset": "raw_orders",
        "source_version": 1,
        "source_field": "order_id"
    }''',
    headers={"content-type": "application/json"},
)
assert deleted_again.status_code == 404, deleted_again.text

# Version definitions are unaffected.
versions = client.get("/datasets/dm_orders/versions")
assert versions.status_code == 200
assert [v["version"] for v in versions.json()] == [1]
"""
    verify = subprocess.run(
        [sys.executable, "-c", verify_script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert verify.returncode == 0, verify.stderr


# --------------------------------------------------------------------------- #
# Repeated 'field' parameter on the lineage read endpoints
# --------------------------------------------------------------------------- #


def _branching_graph(client: TestClient) -> None:
    for name, fields in (
        ("raw_a", ["a1", "a2"]),
        ("mid_b", ["b1", "b2"]),
        ("mart_c", ["c1"]),
    ):
        make_dataset(
            client,
            name,
            [{"name": f, "type": "string", "nullable": True} for f in fields],
        )
    for source, target in (
        (("raw_a", "a1"), ("mid_b", "b1")),
        (("raw_a", "a1"), ("mid_b", "b2")),
        (("mid_b", "b1"), ("mart_c", "c1")),
        (("mid_b", "b2"), ("mart_c", "c1")),
    ):
        (src_ds, src_field), (dst_ds, dst_field) = source, target
        register_link(
            client,
            {
                "target_dataset": dst_ds,
                "target_version": 1,
                "target_field": dst_field,
                "source_dataset": src_ds,
                "source_version": 1,
                "source_field": src_field,
            },
        )


def test_impact_repeated_field_parameter_returns_422(client: TestClient) -> None:
    _branching_graph(client)
    response = client.get(
        "/datasets/raw_a/versions/1/lineage/impact",
        params=[("field", "a1"), ("field", "a2")],
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_impact_repeated_field_uses_first_value_for_lookup(
    client: TestClient,
) -> None:
    _branching_graph(client)
    # First value exists, second does not: the missing second value must not
    # turn the repetition's 422 into a 404.
    response = client.get(
        "/datasets/raw_a/versions/1/lineage/impact",
        params=[("field", "a1"), ("field", "missing")],
    )
    assert response.status_code == 422, response.text

    # First value missing, second existing: the first value performs the
    # resource lookup and its absence is a 404.
    response = client.get(
        "/datasets/raw_a/versions/1/lineage/impact",
        params=[("field", "missing"), ("field", "a1")],
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_impact_repeated_field_unknown_resource_still_404(
    client: TestClient,
) -> None:
    _branching_graph(client)
    response = client.get(
        "/datasets/ghost/versions/1/lineage/impact",
        params=[("field", "a1"), ("field", "a2")],
    )
    assert response.status_code == 404
    response = client.get(
        "/datasets/raw_a/versions/99/lineage/impact",
        params=[("field", "a1"), ("field", "a2")],
    )
    assert response.status_code == 404


def test_impact_paths_repeated_field_uses_first_value_for_lookup(
    client: TestClient,
) -> None:
    _branching_graph(client)
    response = client.get(
        "/datasets/raw_a/versions/1/lineage/impact-paths",
        params=[("field", "a1"), ("field", "missing")],
    )
    assert response.status_code == 422, response.text

    response = client.get(
        "/datasets/raw_a/versions/1/lineage/impact-paths",
        params=[("field", "missing"), ("field", "a1")],
    )
    assert response.status_code == 404


def test_source_paths_repeated_field_uses_first_value_for_lookup(
    client: TestClient,
) -> None:
    _branching_graph(client)
    response = client.get(
        "/datasets/mart_c/versions/1/lineage/impact/source-paths",
        params=[("field", "c1"), ("field", "missing")],
    )
    assert response.status_code == 422, response.text

    response = client.get(
        "/datasets/mart_c/versions/1/lineage/impact/source-paths",
        params=[("field", "missing"), ("field", "c1")],
    )
    assert response.status_code == 404
