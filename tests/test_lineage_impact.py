"""Tests for incremental field impact queries and their persistent cache.

Covers direct, multi-hop, branching and cyclic lineage, sorting/deduplication,
404/422 handling, persistence across restarts, cache invalidation after new
mappings and schema versions, failed-write isolation and compatibility of the
pre-existing lineage endpoints.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_dataset(client: TestClient, name: str, fields: list[dict]) -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions", json={"fields": fields}
    )
    assert response.status_code == 201, response.text


def add_version(client: TestClient, name: str, fields: list[dict]) -> int:
    response = client.post(f"/datasets/{name}/versions", json={"fields": fields})
    assert response.status_code == 201, response.text
    return response.json()["version"]


def add_link(
    client: TestClient,
    source: str,
    source_version: int,
    source_field: str,
    target: str,
    target_version: int,
    target_field: str,
) -> None:
    response = client.post(
        f"/datasets/{target}/versions/{target_version}/lineage",
        json={
            "target_dataset": target,
            "target_version": target_version,
            "target_field": target_field,
            "source_dataset": source,
            "source_version": source_version,
            "source_field": source_field,
        },
    )
    assert response.status_code == 201, response.text


def impact(
    client: TestClient, dataset: str, version: int, field: str | None = None
):
    url = f"/datasets/{dataset}/versions/{version}/lineage/impact"
    if field is not None:
        url += f"?field={field}"
    return client.get(url)


def impacted_refs(client: TestClient, dataset: str, version: int, field: str):
    response = impact(client, dataset, version, field)
    assert response.status_code == 200, response.text
    return response.json()


def _db_path() -> Path:
    return Path(os.environ["DATA_LINEAGE_DB"])


def _cache_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def field_id(dataset: str, version: int, field: str) -> int:
    with _cache_connection() as conn:
        row = conn.execute(
            """
            SELECT sf.id AS id
            FROM   schema_fields sf
            JOIN   schema_versions sv ON sv.id = sf.version_id
            JOIN   datasets d ON d.id = sv.dataset_id
            WHERE  d.name = ? AND sv.version = ? AND sf.name = ?
            """,
            (dataset, version, field),
        ).fetchone()
    assert row is not None
    return row["id"]


def cached_entry(dataset: str, version: int, field: str) -> dict | None:
    with _cache_connection() as conn:
        row = conn.execute(
            "SELECT impacted, revision FROM lineage_impact_cache "
            "WHERE source_field_id = ?",
            (field_id(dataset, version, field),),
        ).fetchone()
    return None if row is None else dict(row)


def graph_revision() -> int | None:
    with _cache_connection() as conn:
        row = conn.execute(
            "SELECT revision FROM lineage_graph_revision WHERE id = 1"
        ).fetchone()
    return None if row is None else row["revision"]


def cache_row_count() -> int:
    with _cache_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM lineage_impact_cache"
        ).fetchone()["n"]


# --------------------------------------------------------------------------- #
# Traversal: direct, multi-hop, branches, cycles
# --------------------------------------------------------------------------- #


def test_direct_impact(client: TestClient) -> None:
    make_dataset(
        client, "raw", [{"name": "id", "type": "integer", "nullable": False}]
    )
    make_dataset(
        client, "dm", [{"name": "id", "type": "integer", "nullable": False}]
    )
    add_link(client, "raw", 1, "id", "dm", 1, "id")

    body = impacted_refs(client, "raw", 1, "id")
    assert body == {
        "source": {"dataset": "raw", "version": 1, "field": "id"},
        "impacted": [{"dataset": "dm", "version": 1, "field": "id"}],
    }


def test_multi_hop_impact(client: TestClient) -> None:
    # raw.id -> stage1.k -> stage2.m -> dm.out
    for name, field in (
        ("raw", "id"),
        ("stage1", "k"),
        ("stage2", "m"),
        ("dm", "out"),
    ):
        make_dataset(
            client, name, [{"name": field, "type": "integer", "nullable": False}]
        )
    add_link(client, "raw", 1, "id", "stage1", 1, "k")
    add_link(client, "stage1", 1, "k", "stage2", 1, "m")
    add_link(client, "stage2", 1, "m", "dm", 1, "out")

    assert impacted_refs(client, "raw", 1, "id")["impacted"] == [
        {"dataset": "dm", "version": 1, "field": "out"},
        {"dataset": "stage1", "version": 1, "field": "k"},
        {"dataset": "stage2", "version": 1, "field": "m"},
    ]
    assert impacted_refs(client, "stage1", 1, "k")["impacted"] == [
        {"dataset": "dm", "version": 1, "field": "out"},
        {"dataset": "stage2", "version": 1, "field": "m"},
    ]
    assert impacted_refs(client, "stage2", 1, "m")["impacted"] == [
        {"dataset": "dm", "version": 1, "field": "out"}
    ]
    assert impacted_refs(client, "dm", 1, "out")["impacted"] == []


def test_branching_impact(client: TestClient) -> None:
    # One source fans out and two branches converge on the same downstream
    # field, which must be reported only once.
    make_dataset(client, "raw", [{"name": "id", "type": "integer", "nullable": False}])
    make_dataset(
        client,
        "mid1",
        [{"name": "f1", "type": "integer", "nullable": False}],
    )
    make_dataset(
        client,
        "mid2",
        [{"name": "f2", "type": "integer", "nullable": False}],
    )
    make_dataset(
        client, "dm", [{"name": "out", "type": "integer", "nullable": False}]
    )
    add_link(client, "raw", 1, "id", "mid1", 1, "f1")
    add_link(client, "raw", 1, "id", "mid2", 1, "f2")
    add_link(client, "mid1", 1, "f1", "dm", 1, "out")
    add_link(client, "mid2", 1, "f2", "dm", 1, "out")

    body = impacted_refs(client, "raw", 1, "id")
    assert body["source"] == {"dataset": "raw", "version": 1, "field": "id"}
    assert body["impacted"] == [
        {"dataset": "dm", "version": 1, "field": "out"},
        {"dataset": "mid1", "version": 1, "field": "f1"},
        {"dataset": "mid2", "version": 1, "field": "f2"},
    ]


def test_cycle_terminates_and_never_includes_source(client: TestClient) -> None:
    # a.x -> b.y -> c.q -> a.x closes a loop back onto the source itself.
    make_dataset(client, "a", [{"name": "x", "type": "integer", "nullable": False}])
    make_dataset(client, "b", [{"name": "y", "type": "integer", "nullable": False}])
    make_dataset(client, "c", [{"name": "q", "type": "integer", "nullable": False}])
    add_link(client, "a", 1, "x", "b", 1, "y")
    add_link(client, "b", 1, "y", "c", 1, "q")
    add_link(client, "c", 1, "q", "a", 1, "x")

    body = impacted_refs(client, "a", 1, "x")
    assert body["impacted"] == [
        {"dataset": "b", "version": 1, "field": "y"},
        {"dataset": "c", "version": 1, "field": "q"},
    ]
    assert all(item["field"] != "x" or item["dataset"] != "a"
               for item in body["impacted"])

    # The cycle must also resolve from a node inside it without hanging.
    assert impacted_refs(client, "b", 1, "y")["impacted"] == [
        {"dataset": "a", "version": 1, "field": "x"},
        {"dataset": "c", "version": 1, "field": "q"},
    ]


def test_results_sorted_by_dataset_version_field(client: TestClient) -> None:
    make_dataset(
        client, "raw", [{"name": "id", "type": "integer", "nullable": False}]
    )
    make_dataset(
        client,
        "zeta",
        [{"name": "z1", "type": "integer", "nullable": False}],
    )
    make_dataset(
        client,
        "alpha",
        [{"name": "a1", "type": "integer", "nullable": False}],
    )
    add_version(
        client,
        "alpha",
        [{"name": "a2", "type": "bigint", "nullable": False}],
    )

    # Insert in an order deliberately different from the required sort order.
    add_link(client, "raw", 1, "id", "zeta", 1, "z1")
    add_link(client, "raw", 1, "id", "alpha", 2, "a2")
    add_link(client, "raw", 1, "id", "alpha", 1, "a1")

    assert impacted_refs(client, "raw", 1, "id")["impacted"] == [
        {"dataset": "alpha", "version": 1, "field": "a1"},
        {"dataset": "alpha", "version": 2, "field": "a2"},
        {"dataset": "zeta", "version": 1, "field": "z1"},
    ]


def test_repeated_queries_return_stable_cached_result(client: TestClient) -> None:
    make_dataset(client, "raw", [{"name": "id", "type": "integer", "nullable": False}])
    make_dataset(client, "dm", [{"name": "id", "type": "integer", "nullable": False}])
    add_link(client, "raw", 1, "id", "dm", 1, "id")

    first = impacted_refs(client, "raw", 1, "id")
    assert cached_entry("raw", 1, "id") is not None
    second = impacted_refs(client, "raw", 1, "id")
    assert first == second


def test_empty_impact_is_cached_as_empty_list(client: TestClient) -> None:
    make_dataset(client, "raw", [{"name": "id", "type": "integer", "nullable": False}])
    body = impacted_refs(client, "raw", 1, "id")
    assert body["impacted"] == []
    entry = cached_entry("raw", 1, "id")
    assert entry is not None
    assert entry["impacted"] == "[]"


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


def test_unknown_dataset_version_field_return_404(client: TestClient) -> None:
    make_dataset(
        client, "raw", [{"name": "id", "type": "integer", "nullable": False}]
    )
    for url in (
        "/datasets/ghost/versions/1/lineage/impact?field=id",
        "/datasets/raw/versions/9/lineage/impact?field=id",
        "/datasets/raw/versions/1/lineage/impact?field=missing",
    ):
        response = client.get(url)
        assert response.status_code == 404, url
        body = response.json()
        assert body["error"] == "not_found"
        assert isinstance(body["detail"], str) and body["detail"]


def test_missing_or_blank_field_returns_422(client: TestClient) -> None:
    make_dataset(
        client, "raw", [{"name": "id", "type": "integer", "nullable": False}]
    )
    for suffix in ("", "?field=", "?field=%20%20%09"):
        response = client.get(f"/datasets/raw/versions/1/lineage/impact{suffix}")
        assert response.status_code == 422, suffix
        assert response.json() == {
            "error": "validation_error",
            "detail": (
                "Query parameter 'field' is required and must name an "
                "existing field"
            ),
        }


def test_validation_precedence_unknown_dataset_with_blank_field(client: TestClient) -> None:
    # A blank field is structurally invalid regardless of the path target.
    response = client.get("/datasets/ghost/versions/1/lineage/impact?field=")
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_failed_impact_queries_write_no_cache(client: TestClient) -> None:
    make_dataset(
        client, "raw", [{"name": "id", "type": "integer", "nullable": False}]
    )
    assert cache_row_count() == 0

    client.get("/datasets/raw/versions/1/lineage/impact")
    client.get("/datasets/raw/versions/1/lineage/impact?field=")
    client.get("/datasets/raw/versions/1/lineage/impact?field=ghost")
    client.get("/datasets/ghost/versions/1/lineage/impact?field=id")

    assert cache_row_count() == 0


# --------------------------------------------------------------------------- #
# Cache invalidation
# --------------------------------------------------------------------------- #


def test_new_mapping_invalidates_source_and_all_upstream_cache(
    client: TestClient,
) -> None:
    for name, field in (
        ("a", "x"),
        ("b", "y"),
        ("c", "w"),
        ("d", "q"),
    ):
        make_dataset(
            client, name, [{"name": field, "type": "integer", "nullable": False}]
        )
    # An unrelated chain whose cache must survive.
    make_dataset(
        client, "u", [{"name": "u1", "type": "integer", "nullable": False}]
    )
    make_dataset(
        client, "v", [{"name": "v1", "type": "integer", "nullable": False}]
    )
    add_link(client, "a", 1, "x", "b", 1, "y")
    add_link(client, "b", 1, "y", "c", 1, "w")
    add_link(client, "u", 1, "u1", "v", 1, "v1")

    # Populate caches along the chain and for the unrelated source.
    assert impacted_refs(client, "a", 1, "x")["impacted"] == [
        {"dataset": "b", "version": 1, "field": "y"},
        {"dataset": "c", "version": 1, "field": "w"},
    ]
    assert impacted_refs(client, "b", 1, "y")["impacted"] == [
        {"dataset": "c", "version": 1, "field": "w"}
    ]
    assert impacted_refs(client, "c", 1, "w")["impacted"] == []
    unrelated_before = impacted_refs(client, "u", 1, "u1")

    revision_before = graph_revision()
    add_link(client, "c", 1, "w", "d", 1, "q")
    assert graph_revision() == revision_before + 1

    # Every cached upstream result reflects the newly committed edge.
    assert impacted_refs(client, "a", 1, "x")["impacted"] == [
        {"dataset": "b", "version": 1, "field": "y"},
        {"dataset": "c", "version": 1, "field": "w"},
        {"dataset": "d", "version": 1, "field": "q"},
    ]
    assert impacted_refs(client, "b", 1, "y")["impacted"] == [
        {"dataset": "c", "version": 1, "field": "w"},
        {"dataset": "d", "version": 1, "field": "q"},
    ]
    assert impacted_refs(client, "c", 1, "w")["impacted"] == [
        {"dataset": "d", "version": 1, "field": "q"}
    ]

    # Unrelated cache is retained in storage and still served correctly.
    assert cached_entry("u", 1, "u1") is not None
    assert impacted_refs(client, "u", 1, "u1") == unrelated_before


def test_new_schema_version_invalidates_dataset_cache(client: TestClient) -> None:
    make_dataset(client, "a", [{"name": "x", "type": "integer", "nullable": False}])
    make_dataset(client, "b", [{"name": "y", "type": "integer", "nullable": False}])
    add_link(client, "a", 1, "x", "b", 1, "y")
    impacted_refs(client, "a", 1, "x")
    impacted_refs(client, "b", 1, "y")
    assert cached_entry("a", 1, "x") is not None
    assert cached_entry("b", 1, "y") is not None

    revision_before = graph_revision()
    add_version(
        client, "a", [{"name": "x2", "type": "string", "nullable": True}]
    )

    # Entries keyed on the dataset's fields are gone; the other dataset's entry
    # is untouched. Version creation does not itself rewrite lineage edges.
    assert cached_entry("a", 1, "x") is None
    assert cached_entry("b", 1, "y") is not None
    assert graph_revision() == revision_before

    # Recomputing on the next read yields the same graph result.
    assert impacted_refs(client, "a", 1, "x")["impacted"] == [
        {"dataset": "b", "version": 1, "field": "y"}
    ]


def test_failed_schema_version_creation_keeps_cache(client: TestClient) -> None:
    make_dataset(client, "a", [{"name": "x", "type": "integer", "nullable": False}])
    make_dataset(client, "b", [{"name": "y", "type": "integer", "nullable": False}])
    add_link(client, "a", 1, "x", "b", 1, "y")
    impacted_refs(client, "a", 1, "x")
    entry_before = cached_entry("a", 1, "x")
    assert entry_before is not None

    # 422: empty field list writes nothing and must not evict the cache.
    invalid = client.post("/datasets/a/versions", json={"fields": []})
    assert invalid.status_code == 422
    # 404: unknown dataset.
    missing = client.post(
        "/datasets/ghost/versions",
        json={"fields": [{"name": "z", "type": "integer", "nullable": False}]},
    )
    assert missing.status_code == 404

    assert cached_entry("a", 1, "x") == entry_before
    assert impacted_refs(client, "a", 1, "x")["impacted"] == [
        {"dataset": "b", "version": 1, "field": "y"}
    ]


def test_failed_lineage_writes_do_not_touch_cache_or_data(
    client: TestClient,
) -> None:
    make_dataset(client, "a", [{"name": "x", "type": "integer", "nullable": False}])
    make_dataset(client, "b", [{"name": "y", "type": "integer", "nullable": False}])
    make_dataset(client, "c", [{"name": "z", "type": "integer", "nullable": False}])
    add_link(client, "a", 1, "x", "b", 1, "y")
    impacted_refs(client, "a", 1, "x")

    revision_before = graph_revision()
    entry_before = cached_entry("a", 1, "x")
    assert entry_before is not None

    # 409: submitting the same mapping again must not invalidate anything.
    duplicate = client.post(
        "/datasets/b/versions/1/lineage",
        json={
            "target_dataset": "b",
            "target_version": 1,
            "target_field": "y",
            "source_dataset": "a",
            "source_version": 1,
            "source_field": "x",
        },
    )
    assert duplicate.status_code == 409

    # 422: source and target datasets must differ.
    same_dataset = client.post(
        "/datasets/a/versions/1/lineage",
        json={
            "target_dataset": "a",
            "target_version": 1,
            "target_field": "x",
            "source_dataset": "a",
            "source_version": 1,
            "source_field": "x",
        },
    )
    assert same_dataset.status_code == 422

    # 404: unknown source field.
    unknown_source = client.post(
        "/datasets/b/versions/1/lineage",
        json={
            "target_dataset": "b",
            "target_version": 1,
            "target_field": "y",
            "source_dataset": "c",
            "source_version": 1,
            "source_field": "ghost",
        },
    )
    assert unknown_source.status_code == 404

    assert graph_revision() == revision_before
    assert cached_entry("a", 1, "x") == entry_before
    # Existing data is unchanged: the target still has exactly one source.
    lineage = client.get("/datasets/b/versions/1/lineage").json()
    y_field = next(f for f in lineage["fields"] if f["target_field"] == "y")
    assert y_field["sources"] == [
        {"dataset": "a", "version": 1, "field": "x"}
    ]


# --------------------------------------------------------------------------- #
# Persistence across a process restart
# --------------------------------------------------------------------------- #


RESTART_CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response):
    assert response.status_code in (200, 201), response.text

for name, field in (("raw", "id"), ("stage", "k"), ("dm", "out"), ("rpt", "r")):
    ok(client.post("/datasets", json={"name": name}))
    ok(client.post(
        f"/datasets/{name}/versions",
        json={"fields": [{"name": field, "type": "integer", "nullable": False}]},
    ))

def link(s, sv, sf, t, tv, tf):
    ok(client.post(
        f"/datasets/{t}/versions/{tv}/lineage",
        json={
            "target_dataset": t, "target_version": tv, "target_field": tf,
            "source_dataset": s, "source_version": sv, "source_field": sf,
        },
    ))

link("raw", 1, "id", "stage", 1, "k")
link("stage", 1, "k", "dm", 1, "out")

impact = client.get("/datasets/raw/versions/1/lineage/impact?field=id")
assert impact.status_code == 200, impact.text
assert impact.json()["impacted"] == [
    {"dataset": "dm", "version": 1, "field": "out"},
    {"dataset": "stage", "version": 1, "field": "k"},
]
print("created")
"""


RESTART_VERIFY_SCRIPT = """
import os
import sqlite3
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def cache_row():
    conn = sqlite3.connect(os.environ["DATA_LINEAGE_DB"])
    return conn.execute(
        '''
        SELECT cic.impacted AS impacted
        FROM   lineage_impact_cache cic
        JOIN   schema_fields sf ON sf.id = cic.source_field_id
        JOIN   schema_versions sv ON sv.id = sf.version_id
        JOIN   datasets d ON d.id = sv.dataset_id
        WHERE  d.name = 'raw' AND sv.version = 1 AND sf.name = 'id'
        '''
    ).fetchone()

def raw_impact():
    response = client.get("/datasets/raw/versions/1/lineage/impact?field=id")
    assert response.status_code == 200, response.text
    return response.json()

# The impact result is served after the restart.
assert raw_impact() == {
    "source": {"dataset": "raw", "version": 1, "field": "id"},
    "impacted": [
        {"dataset": "dm", "version": 1, "field": "out"},
        {"dataset": "stage", "version": 1, "field": "k"},
    ],
}

# The cache itself lives in the database file, not in process memory.
row = cache_row()
assert row is not None, "impact cache row did not survive the restart"
assert '"dataset": "dm"' in row[0]

# An unrelated mapping (rpt.r feeds INTO dm.out; rpt cannot be reached from
# raw.id) must neither invalidate raw's cache nor change raw's impact.
unrelated = client.post(
    "/datasets/dm/versions/1/lineage",
    json={
        "target_dataset": "dm", "target_version": 1, "target_field": "out",
        "source_dataset": "rpt", "source_version": 1, "source_field": "r",
    },
)
assert unrelated.status_code == 201, unrelated.text
assert cache_row() is not None, "unrelated mapping must not evict raw's cache"
assert raw_impact()["impacted"] == [
    {"dataset": "dm", "version": 1, "field": "out"},
    {"dataset": "stage", "version": 1, "field": "k"},
]

# Extending the chain beyond dm.out invalidates raw's persisted cache and the
# next read reflects the newly committed graph.
created_ds = client.post("/datasets", json={"name": "down"})
assert created_ds.status_code == 201, created_ds.text
created_ver = client.post(
    "/datasets/down/versions",
    json={"fields": [{"name": "d1", "type": "integer", "nullable": False}]},
)
assert created_ver.status_code == 201, created_ver.text
linked = client.post(
    "/datasets/down/versions/1/lineage",
    json={
        "target_dataset": "down", "target_version": 1, "target_field": "d1",
        "source_dataset": "dm", "source_version": 1, "source_field": "out",
    },
)
assert linked.status_code == 201, linked.text

assert raw_impact()["impacted"] == [
    {"dataset": "dm", "version": 1, "field": "out"},
    {"dataset": "down", "version": 1, "field": "d1"},
    {"dataset": "stage", "version": 1, "field": "k"},
]
print("verified")
"""


def _run(db_path: Path, script: str) -> str:
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


def test_impact_cache_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "impact-restart.db"
    assert _run(db_path, RESTART_CREATE_SCRIPT) == "created"
    assert _run(db_path, RESTART_VERIFY_SCRIPT) == "verified"


# --------------------------------------------------------------------------- #
# Compatibility with the pre-existing lineage interfaces
# --------------------------------------------------------------------------- #


def test_existing_lineage_endpoints_remain_compatible(client: TestClient) -> None:
    make_dataset(
        client,
        "raw",
        [
            {"name": "id", "type": "integer", "nullable": False},
            {"name": "amount", "type": "decimal", "nullable": True},
        ],
    )
    make_dataset(
        client,
        "dm",
        [
            {"name": "id", "type": "integer", "nullable": False},
            {"name": "total", "type": "decimal", "nullable": True},
        ],
    )
    add_link(client, "raw", 1, "id", "dm", 1, "id")
    add_link(client, "raw", 1, "amount", "dm", 1, "total")

    # The impact route (/lineage/impact) must not shadow the lineage listing.
    lineage = client.get("/datasets/dm/versions/1/lineage")
    assert lineage.status_code == 200
    assert lineage.json() == {
        "target_dataset": "dm",
        "target_version": 1,
        "fields": [
            {
                "target_field": "id",
                "sources": [
                    {"dataset": "raw", "version": 1, "field": "id"}
                ],
            },
            {
                "target_field": "total",
                "sources": [
                    {"dataset": "raw", "version": 1, "field": "amount"}
                ],
            },
        ],
    }

    created = client.post(
        "/datasets/dm/versions/1/lineage",
        json={
            "target_dataset": "dm",
            "target_version": 1,
            "target_field": "total",
            "source_dataset": "raw",
            "source_version": 1,
            "source_field": "id",
        },
    )
    assert created.status_code == 201, created.text

    # The new mapping is visible through both interfaces; a source used twice
    # impacts both targets.
    assert impacted_refs(client, "raw", 1, "id")["impacted"] == [
        {"dataset": "dm", "version": 1, "field": "id"},
        {"dataset": "dm", "version": 1, "field": "total"},
    ]
    total_field = next(
        f
        for f in client.get("/datasets/dm/versions/1/lineage").json()["fields"]
        if f["target_field"] == "total"
    )
    assert len(total_field["sources"]) == 2
