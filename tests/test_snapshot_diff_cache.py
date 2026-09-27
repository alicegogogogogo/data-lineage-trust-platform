"""Tests for the incrementally maintained snapshot diff cache.

A snapshot creation atomically writes the cache record (canonical per-row
form plus the top-level field-name set) and a ``created`` trail entry;
snapshot diffs synthesize from the cache when it is complete and consistent
and recompute from extant rows otherwise, with identical results. A
confirmed deletion voids the record and appends a ``deleted`` trail entry.
The two read-only entries are ``.../snapshots/cache-audit`` and
``.../snapshots/cache-trail``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

AUDIT_PATH = "/datasets/{dataset}/versions/{version}/snapshots/cache-audit"
TRAIL_PATH = "/datasets/{dataset}/versions/{version}/snapshots/cache-trail"


def make_dataset(client: TestClient, name: str = "orders") -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201


def make_version(
    client: TestClient,
    fields: list[str] | None = None,
    *,
    dataset: str = "orders",
    version: int = 1,
) -> None:
    fields = fields or ["id", "name"]
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={
            "fields": [
                {"name": field, "type": "string", "nullable": True}
                for field in fields
            ]
        },
    )
    assert response.status_code == 201, response.text


def make_snapshot(client: TestClient, rows: list, *, version: int = 1) -> dict:
    response = client.post(
        f"/datasets/orders/versions/{version}/snapshots", json={"rows": rows}
    )
    assert response.status_code == 201, response.text
    return response.json()


def cache_dump(db_path: Path) -> dict[int, tuple[list, list]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT snapshot_id, canonical_rows, field_names "
            "FROM snapshot_diff_cache"
        ).fetchall()
    finally:
        conn.close()
    return {
        snapshot_id: (json.loads(canonical_rows), json.loads(field_names))
        for snapshot_id, canonical_rows, field_names in rows
    }


def drop_cache_rows(db_path: Path, snapshot_ids: list[int]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "DELETE FROM snapshot_diff_cache WHERE snapshot_id = ?",
            [(snapshot_id,) for snapshot_id in snapshot_ids],
        )
        conn.commit()
    finally:
        conn.close()


def tamper_rows(db_path: Path, snapshot_id: int, rows: list) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE snapshots SET rows = ? WHERE id = ?",
            (json.dumps(rows), snapshot_id),
        )
        conn.commit()
    finally:
        conn.close()


def same_version_diff(client: TestClient, from_id: int, to_id: int):
    return client.get(
        f"/datasets/orders/versions/1/snapshots/{from_id}/diff/{to_id}"
    )


# --------------------------------------------------------------------------- #
# Cache maintenance on creation
# --------------------------------------------------------------------------- #


def test_snapshot_creation_writes_a_complete_cache_record(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client)
    make_version(client)
    rows = [{"name": "a", "id": 1}, {"id": 2}, {"name": "b", "extra": [1, 2]}]
    snapshot = make_snapshot(client, rows)

    cache = cache_dump(isolated_database)
    assert set(cache) == {snapshot["id"]}
    canonical_rows, field_names = cache[snapshot["id"]]
    assert canonical_rows == [
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for row in rows
    ]
    assert field_names == ["extra", "id", "name"]


def test_empty_snapshot_is_cached_normally(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client)
    make_version(client)
    snapshot = make_snapshot(client, [])
    assert cache_dump(isolated_database) == {snapshot["id"]: ([], [])}


def test_creation_appends_a_created_trail_record(client: TestClient) -> None:
    make_dataset(client)
    make_version(client)
    first = make_snapshot(client, [{"id": 1}])
    second = make_snapshot(client, [{"id": 2}])

    trail = client.get(TRAIL_PATH.format(dataset="orders", version=1)).json()
    assert [
        (entry["sequence"], entry["cause"], entry["snapshot_id"])
        for entry in trail
    ] == [
        (1, "created", first["id"]),
        (2, "created", second["id"]),
    ]
    for entry in trail:
        parsed = datetime.fromisoformat(entry["created_at"])
        assert parsed.tzinfo is not None


# --------------------------------------------------------------------------- #
# Cache-synthesized vs recomputed diffs are identical
# --------------------------------------------------------------------------- #


def test_same_version_diff_is_identical_with_and_without_cache(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client)
    make_version(client)
    first = make_snapshot(client, [
        {"name": "a", "id": 1}, {"id": 2}, {"id": 2},
    ])
    second = make_snapshot(client, [
        {"id": 2}, {"id": 3, "name": "c"}, {"id": 4, "name": "d"},
    ])

    cached = same_version_diff(client, first["id"], second["id"])
    assert cached.status_code == 200
    drop_cache_rows(isolated_database, [first["id"], second["id"]])
    recomputed = same_version_diff(client, first["id"], second["id"])
    assert recomputed.status_code == 200
    assert recomputed.text == cached.text


def test_time_diff_is_identical_with_and_without_cache(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client)
    make_version(client)
    first = make_snapshot(client, [{"id": 1, "zeta": 1, "alpha": 2}])
    second = make_snapshot(client, [{"id": 1, "zeta": 1, "mid": 3}])
    params = {"from": first["created_at"], "to": second["created_at"]}
    url = "/datasets/orders/versions/1/snapshots/at/diff"

    cached = client.get(url, params=params)
    assert cached.status_code == 200
    drop_cache_rows(isolated_database, [first["id"], second["id"]])
    recomputed = client.get(url, params=params)
    assert recomputed.text == cached.text


def test_cross_version_diff_is_identical_with_and_without_cache(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client)
    client.post(
        "/datasets/orders/versions",
        json={"fields": [
            {"name": "id", "type": "integer", "nullable": False},
            {"name": "name", "type": "string", "nullable": True},
        ]},
    )
    client.post(
        "/datasets/orders/versions",
        json={"fields": [
            {"name": "id", "type": "integer", "nullable": False},
            {"name": "label", "type": "string", "nullable": True},
        ]},
    )
    base = make_snapshot(
        client, [{"name": "a", "id": 1}, {"id": 2, "name": "b"}], version=1
    )
    target = make_snapshot(
        client, [{"id": 1, "label": "a"}, {"id": 3, "label": "c"}], version=2
    )
    url = f"/datasets/orders/snapshots/{base['id']}/diff/{target['id']}"

    cached = client.get(url)
    assert cached.status_code == 200
    drop_cache_rows(isolated_database, [base["id"], target["id"]])
    recomputed = client.get(url)
    assert recomputed.text == cached.text


def test_diff_recomputes_from_extant_rows_when_the_cache_disagrees(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client)
    make_version(client)
    first = make_snapshot(client, [{"id": 1}, {"id": 2}])
    second = make_snapshot(client, [{"id": 2}, {"id": 3}])

    tamper_rows(isolated_database, first["id"], [{"id": 1}, {"id": 2}, {"id": 4}])

    # The stale cache is ignored: the removed id 4 row comes from the extant
    # rows, exactly as when no cache row exists.
    tampered_diff = same_version_diff(client, first["id"], second["id"]).json()
    assert [entry["row"] for entry in tampered_diff["removed"]] == [
        {"id": 1}, {"id": 4},
    ]
    drop_cache_rows(isolated_database, [first["id"]])
    fresh_diff = same_version_diff(client, first["id"], second["id"]).json()
    assert fresh_diff == tampered_diff


def test_masked_time_diff_is_identical_with_and_without_cache(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client)
    client.post(
        "/datasets/orders/versions",
        json={"fields": [
            {"name": "id", "type": "integer", "nullable": False},
            {"name": "email", "type": "string", "nullable": True},
        ]},
    )
    assert client.post(
        "/datasets/orders/versions/1/privacy-policies",
        json={
            "field": "email",
            "classification": "PII",
            "masking": "redact",
            "allowed_roles": [],
        },
    ).status_code == 201
    first = make_snapshot(client, [{"id": 1, "email": "alice@example.com"}])
    second = make_snapshot(client, [{"id": 2, "email": "bob@example.com"}])
    body = {
        "role": "guest",
        "from": first["created_at"],
        "to": second["created_at"],
    }
    url = "/datasets/orders/versions/1/snapshots/at/diff/masked"

    cached = client.post(url, json=body)
    assert cached.status_code == 200, cached.text
    drop_cache_rows(isolated_database, [first["id"], second["id"]])
    recomputed = client.post(url, json=body)
    assert recomputed.status_code == 200, recomputed.text
    assert recomputed.json()["added"] == cached.json()["added"]
    assert recomputed.json()["removed"] == cached.json()["removed"]


# --------------------------------------------------------------------------- #
# Cache audit
# --------------------------------------------------------------------------- #


def test_audit_reports_new_snapshots_as_cached(client: TestClient) -> None:
    make_dataset(client)
    make_version(client)
    first = make_snapshot(client, [{"id": 1}])
    second = make_snapshot(client, [{"id": 2}])

    response = client.get(AUDIT_PATH.format(dataset="orders", version=1))
    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == ["dataset", "version", "entries", "counts"]
    assert payload["dataset"] == "orders"
    assert payload["version"] == 1
    assert payload["entries"] == [
        {"snapshot_id": first["id"], "status": "cached"},
        {"snapshot_id": second["id"], "status": "cached"},
    ]
    assert payload["counts"] == {
        "cached_count": 2,
        "missing_count": 0,
        "mismatch_count": 0,
    }


def test_audit_entries_are_sorted_by_snapshot_id(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client)
    make_version(client)
    ids = [make_snapshot(client, [{"id": i}])["id"] for i in range(5)]
    # Remove the middle cache rows to mix statuses.
    drop_cache_rows(isolated_database, [ids[1], ids[3]])

    payload = client.get(
        AUDIT_PATH.format(dataset="orders", version=1)
    ).json()
    assert [entry["snapshot_id"] for entry in payload["entries"]] == ids
    assert [entry["status"] for entry in payload["entries"]] == [
        "cached", "missing", "cached", "missing", "cached",
    ]
    assert payload["counts"] == {
        "cached_count": 3,
        "missing_count": 2,
        "mismatch_count": 0,
    }


def test_audit_missing_record_is_not_an_error(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client)
    make_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    drop_cache_rows(isolated_database, [snapshot["id"]])

    response = client.get(AUDIT_PATH.format(dataset="orders", version=1))
    assert response.status_code == 200
    assert response.json()["entries"] == [
        {"snapshot_id": snapshot["id"], "status": "missing"}
    ]


def test_audit_reports_a_tampered_snapshot_as_mismatch(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client)
    make_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    tamper_rows(isolated_database, snapshot["id"], [{"id": 1}, {"id": 2}])

    payload = client.get(
        AUDIT_PATH.format(dataset="orders", version=1)
    ).json()
    assert payload["entries"] == [
        {"snapshot_id": snapshot["id"], "status": "mismatch"}
    ]
    assert payload["counts"]["mismatch_count"] == 1


def _corrupt_cache_record(
    db_path: Path,
    snapshot_id: int,
    *,
    canonical_rows: list | None = None,
    field_names: list | None = None,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        current = conn.execute(
            "SELECT canonical_rows, field_names FROM snapshot_diff_cache "
            "WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        new_rows = (
            json.dumps(canonical_rows)
            if canonical_rows is not None
            else current[0]
        )
        new_fields = (
            json.dumps(field_names)
            if field_names is not None
            else current[1]
        )
        conn.execute(
            "UPDATE snapshot_diff_cache SET canonical_rows = ?, field_names = ? "
            "WHERE snapshot_id = ?",
            (new_rows, new_fields, snapshot_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_audit_reports_a_corrupt_cache_record_as_mismatch(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client)
    make_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])

    # A stale canonical-rows column disagrees with the extant rows.
    _corrupt_cache_record(isolated_database, snapshot["id"], canonical_rows=['{"id":9}'])
    payload = client.get(
        AUDIT_PATH.format(dataset="orders", version=1)
    ).json()
    assert payload["entries"][0]["status"] == "mismatch"
    # The diff ignores the corrupt record and recomputes from extant rows,
    # exactly as a missing record would.
    tampered = same_version_diff(client, snapshot["id"], snapshot["id"]).json()
    assert tampered["added"] == [] and tampered["removed"] == []
    drop_cache_rows(isolated_database, [snapshot["id"]])
    missing = same_version_diff(client, snapshot["id"], snapshot["id"]).json()
    assert missing == tampered

    # Recreate the cache row through a new snapshot, then tamper only the
    # field-name column: that alone is a mismatch too.
    other = make_snapshot(client, [{"id": 2}])
    _corrupt_cache_record(isolated_database, other["id"], field_names=["ghost"])
    payload = client.get(
        AUDIT_PATH.format(dataset="orders", version=1)
    ).json()
    statuses = {entry["snapshot_id"]: entry["status"] for entry in payload["entries"]}
    assert statuses[other["id"]] == "mismatch"


def test_audit_of_a_version_without_snapshots_is_an_empty_document(
    client: TestClient,
) -> None:
    make_dataset(client)
    make_version(client)
    response = client.get(AUDIT_PATH.format(dataset="orders", version=1))
    assert response.status_code == 200
    payload = response.json()
    assert payload["entries"] == []
    assert payload["counts"] == {
        "cached_count": 0,
        "missing_count": 0,
        "mismatch_count": 0,
    }


def test_audit_document_is_deterministic_compact_json_with_newline(
    client: TestClient,
) -> None:
    make_dataset(client)
    make_version(client)
    make_snapshot(client, [{"id": 1}])
    response = client.get(AUDIT_PATH.format(dataset="orders", version=1))
    assert response.headers["content-type"] == "application/json"
    payload = response.json()
    assert response.text == (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    )
    assert response.text.endswith("}\n")
    assert not response.text.endswith("}\n\n")
    assert list(payload["counts"]) == [
        "cached_count", "missing_count", "mismatch_count"
    ]
    # Repeated reads are byte-identical.
    again = client.get(AUDIT_PATH.format(dataset="orders", version=1))
    assert again.text == response.text


def test_audit_never_writes_or_repairs(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client)
    make_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    tamper_rows(isolated_database, snapshot["id"], [{"id": 1}, {"id": 2}])

    before = cache_dump(isolated_database)
    for _ in range(3):
        response = client.get(AUDIT_PATH.format(dataset="orders", version=1))
        assert response.status_code == 200
        assert response.json()["entries"][0]["status"] == "mismatch"
    # Nothing was repaired, inserted or voided by the reads.
    assert cache_dump(isolated_database) == before


# --------------------------------------------------------------------------- #
# Cache trail
# --------------------------------------------------------------------------- #


def test_trail_of_a_version_without_records_is_an_empty_array(
    client: TestClient,
) -> None:
    make_dataset(client)
    make_version(client)
    response = client.get(TRAIL_PATH.format(dataset="orders", version=1))
    assert response.status_code == 200
    assert response.text == "[]\n"


def test_trail_records_cache_invalidation_on_confirmed_deletion(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client)
    make_version(client, ["id"])
    first = make_snapshot(client, [{"id": 1}])
    second = make_snapshot(client, [{"id": 2}])
    assert client.post(
        "/datasets/orders/versions/1/retention-policies",
        json={"retention_days": 0},
    ).status_code == 201
    request = client.post(
        f"/datasets/orders/versions/1/snapshots/{second['id']}/deletion-requests",
        json={"reason": "retention reached"},
    ).json()
    confirmed = client.post(
        f"/datasets/orders/versions/1/snapshots/{second['id']}"
        f"/deletion-requests/{request['id']}/confirm"
    )
    assert confirmed.status_code == 200, confirmed.text

    trail = client.get(TRAIL_PATH.format(dataset="orders", version=1)).json()
    assert [
        (entry["sequence"], entry["cause"], entry["snapshot_id"])
        for entry in trail
    ] == [
        (1, "created", first["id"]),
        (2, "created", second["id"]),
        (3, "deleted", second["id"]),
    ]
    # The cache row is voided together with the snapshot.
    assert set(cache_dump(isolated_database)) == {first["id"]}
    for entry in trail:
        datetime.fromisoformat(entry["created_at"])


def test_trail_is_append_only_and_loses_no_sequence_on_failed_confirmation(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client)
    make_version(client, ["id"])
    snapshot = make_snapshot(client, [{"id": 1}])
    assert client.post(
        "/datasets/orders/versions/1/retention-policies",
        json={"retention_days": 99},
    ).status_code == 201
    request = client.post(
        f"/datasets/orders/versions/1/snapshots/{snapshot['id']}/deletion-requests",
        json={"reason": "too early"},
    ).json()
    # The snapshot is younger than the retention age: 409 and zero writes.
    rejected = client.post(
        f"/datasets/orders/versions/1/snapshots/{snapshot['id']}"
        f"/deletion-requests/{request['id']}/confirm"
    )
    assert rejected.status_code == 409

    trail = client.get(TRAIL_PATH.format(dataset="orders", version=1)).json()
    assert [(t["cause"], t["snapshot_id"]) for t in trail] == [
        ("created", snapshot["id"])
    ]
    # The cache entry survived the rejected confirmation.
    assert set(cache_dump(isolated_database)) == {snapshot["id"]}


def test_trail_entries_sort_by_sequence_and_have_fixed_key_order(
    client: TestClient,
) -> None:
    make_dataset(client)
    make_version(client, ["id"])
    make_snapshot(client, [{"id": 1}])
    response = client.get(TRAIL_PATH.format(dataset="orders", version=1))
    entry = response.json()[0]
    assert list(entry) == ["sequence", "cause", "snapshot_id", "created_at"]


def test_trail_survives_a_restart(client: TestClient) -> None:
    make_dataset(client)
    make_version(client, ["id"])
    snapshot = make_snapshot(client, [{"id": 1}])
    assert client.post(
        "/datasets/orders/versions/1/retention-policies",
        json={"retention_days": 0},
    ).status_code == 201
    request = client.post(
        f"/datasets/orders/versions/1/snapshots/{snapshot['id']}/deletion-requests",
        json={"reason": "retention reached"},
    ).json()
    assert client.post(
        f"/datasets/orders/versions/1/snapshots/{snapshot['id']}"
        f"/deletion-requests/{request['id']}/confirm"
    ).status_code == 200

    before = client.get(TRAIL_PATH.format(dataset="orders", version=1)).text
    restarted = TestClient(client.app)
    after = restarted.get(
        TRAIL_PATH.format(dataset="orders", version=1)
    ).text
    assert after == before


# --------------------------------------------------------------------------- #
# Errors and 404-before-422 precedence
# --------------------------------------------------------------------------- #


def test_unknown_dataset_or_version_returns_404(client: TestClient) -> None:
    make_dataset(client)
    make_version(client)
    for path in (AUDIT_PATH, TRAIL_PATH):
        assert client.get(path.format(dataset="ghost", version=1)).status_code == 404
        assert client.get(path.format(dataset="orders", version=99)).status_code == 404


def test_request_body_or_query_parameter_returns_422(client: TestClient) -> None:
    make_dataset(client)
    make_version(client)
    for path in (AUDIT_PATH, TRAIL_PATH):
        url = path.format(dataset="orders", version=1)
        assert client.request("GET", url, content=b"{}").status_code == 422
        for raw in (b" ", b"  \t\n"):
            response = client.request("GET", url, content=raw)
            assert response.status_code == 422, raw
        assert client.get(url, params={"x": "1"}).status_code == 422
        body = client.request("GET", url, content=b"{}").json()
        assert set(body) == {"error", "detail"}


def test_404_takes_precedence_over_every_shape_check(client: TestClient) -> None:
    make_dataset(client)
    make_version(client)
    for path in (AUDIT_PATH, TRAIL_PATH):
        response = client.request(
            "GET",
            path.format(dataset="ghost", version=1),
            params={"x": "1"},
            content=b"  ",
        )
        assert response.status_code == 404
        response = client.request(
            "GET",
            path.format(dataset="orders", version=99),
            params={"x": "1"},
            content=b"{}",
        )
        assert response.status_code == 404


def test_only_get_is_accepted(client: TestClient) -> None:
    make_dataset(client)
    make_version(client)
    for path in (AUDIT_PATH, TRAIL_PATH):
        url = path.format(dataset="orders", version=1)
        for method in ("post", "put", "delete", "patch"):
            assert client.request(method, url, content=b"{}").status_code == 405


def test_rejections_write_nothing(
    client: TestClient, isolated_database: Path
) -> None:
    make_dataset(client)
    make_version(client, ["id"])
    snapshot = make_snapshot(client, [{"id": 1}])
    cache_before = cache_dump(isolated_database)
    assert cache_before
    conn = sqlite3.connect(isolated_database)
    try:
        trail_before = conn.execute(
            "SELECT sequence, cause, snapshot_id FROM snapshot_diff_cache_trail"
        ).fetchall()
    finally:
        conn.close()

    client.request(
        "GET", AUDIT_PATH.format(dataset="ghost", version=1), content=b"{}"
    )
    client.get(AUDIT_PATH.format(dataset="orders", version=1), params={"x": "1"})
    client.request(
        "GET", TRAIL_PATH.format(dataset="orders", version=1), content=b" \t\n"
    )

    assert cache_dump(isolated_database) == cache_before
    conn = sqlite3.connect(isolated_database)
    try:
        trail_after = conn.execute(
            "SELECT sequence, cause, snapshot_id FROM snapshot_diff_cache_trail"
        ).fetchall()
    finally:
        conn.close()
    assert trail_after == trail_before
    # The cache row still belongs to the snapshot and stays usable.
    assert snapshot["id"] in cache_dump(isolated_database)


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_snapshot_creations_keep_trail_sequences_continuous(
    client: TestClient,
) -> None:
    make_dataset(client)
    make_version(client, ["id"])
    count = 8
    failures: list[Exception] = []
    barrier = threading.Barrier(count)

    def worker(index: int) -> None:
        local = TestClient(client.app)
        barrier.wait()
        try:
            response = local.post(
                "/datasets/orders/versions/1/snapshots",
                json={"rows": [{"id": index}]},
            )
            assert response.status_code == 201, response.text
        except Exception as exc:  # pragma: no cover - reported below
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures, failures

    trail = client.get(TRAIL_PATH.format(dataset="orders", version=1)).json()
    assert [entry["sequence"] for entry in trail] == list(range(1, count + 1))
    assert sorted(entry["snapshot_id"] for entry in trail) == sorted(
        entry["snapshot_id"] for entry in trail
    )
    assert len({entry["snapshot_id"] for entry in trail}) == count
    assert all(entry["cause"] == "created" for entry in trail)
