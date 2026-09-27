"""Tests for the maintained snapshot diff cache, its audit and its trail."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

AUDIT_PATH = "/datasets/{dataset}/versions/{version}/snapshots/cache-audit"
TRAIL_PATH = "/datasets/{dataset}/versions/{version}/snapshots/cache-trail"
SNAPSHOTS_PATH = "/datasets/{dataset}/versions/{version}/snapshots"


def create_dataset(client: TestClient, name: str = "orders") -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201


def create_version(
    client: TestClient,
    fields: list[dict] | None = None,
    dataset: str = "orders",
) -> int:
    fields = fields or [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "name", "type": "string", "nullable": True},
    ]
    response = client.post(
        f"/datasets/{dataset}/versions", json={"fields": fields}
    )
    assert response.status_code == 201, response.text
    return response.json()["version"]


def make_snapshot(
    client: TestClient, rows: list, dataset: str = "orders", version: int = 1
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots", json={"rows": rows}
    )
    assert response.status_code == 201, response.text
    return response.json()


def cache_dump(db_path: Path) -> dict[int, tuple[list, list]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT snapshot_id, row_canons, field_names FROM snapshot_diff_cache"
        ).fetchall()
    finally:
        conn.close()
    return {
        snapshot_id: (json.loads(row_canons), json.loads(field_names))
        for snapshot_id, row_canons, field_names in rows
    }


def trail_dump(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT sequence, cause, snapshot_id, created_at "
            "FROM snapshot_diff_cache_trail ORDER BY sequence"
        ).fetchall()
    finally:
        conn.close()
    return [tuple(row) for row in rows]


def drop_cache(db_path: Path, snapshot_id: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "DELETE FROM snapshot_diff_cache WHERE snapshot_id = ?", (snapshot_id,)
        )
        conn.commit()
    finally:
        conn.close()


def corrupt_cache(db_path: Path, snapshot_id: int, row_canons: list[str]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE snapshot_diff_cache SET row_canons = ? WHERE snapshot_id = ?",
            (json.dumps(row_canons), snapshot_id),
        )
        conn.commit()
    finally:
        conn.close()


def replace_stored_rows(db_path: Path, snapshot_id: int, rows: list) -> None:
    """Rewrite a snapshot's stored rows without touching the cache or hash."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE snapshots SET rows = ? WHERE id = ?",
            (json.dumps(rows), snapshot_id),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Cache maintenance with snapshot creation and deletion
# --------------------------------------------------------------------------- #


def test_snapshot_creation_writes_cache_record_and_created_trail(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset(client)
    create_version(client)
    snapshot = make_snapshot(client, [{"name": "a", "id": 1}, {"id": 2}])

    records = cache_dump(isolated_database)
    assert set(records) == {snapshot["id"]}
    row_canons, field_names = records[snapshot["id"]]
    assert row_canons == ['{"id":1,"name":"a"}', '{"id":2}']
    assert field_names == ["id", "name"]

    rows = trail_dump(isolated_database)
    assert len(rows) == 1
    sequence, cause, snap_id, created_at = rows[0]
    assert (sequence, cause, snap_id) == (1, "created", snapshot["id"])
    # The trail record shares the snapshot creation transaction/time.
    assert created_at == snapshot["created_at"]
    assert datetime.fromisoformat(created_at).tzinfo is not None


def test_empty_snapshot_is_cached_normally(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset(client)
    create_version(client)
    snapshot = make_snapshot(client, [])

    row_canons, field_names = cache_dump(isolated_database)[snapshot["id"]]
    assert row_canons == []
    assert field_names == []


def test_cache_write_is_atomic_with_the_snapshot(
    client: TestClient, isolated_database: Path
) -> None:
    # A rejected snapshot creation (malformed rows through the API) writes
    # neither a snapshot nor a cache record nor a trail record.
    create_dataset(client)
    create_version(client)
    response = client.post(
        SNAPSHOTS_PATH.format(dataset="orders", version=1), json={"rows": [1, 2]}
    )
    assert response.status_code == 422
    assert cache_dump(isolated_database) == {}
    assert trail_dump(isolated_database) == []


def test_confirmed_deletion_invalidates_cache_and_appends_deleted_trail(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset(client)
    create_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    client.post(
        "/datasets/orders/versions/1/retention-policies",
        json={"retention_days": 0},
    )
    request = client.post(
        f"{SNAPSHOTS_PATH.format(dataset='orders', version=1)}"
        f"/{snapshot['id']}/deletion-requests",
        json={"reason": "done"},
    ).json()
    confirm = client.post(
        f"{SNAPSHOTS_PATH.format(dataset='orders', version=1)}"
        f"/{snapshot['id']}/deletion-requests/{request['id']}/confirm"
    )
    assert confirm.status_code == 200, confirm.text

    assert snapshot["id"] not in cache_dump(isolated_database)
    records = trail_dump(isolated_database)
    assert [(row[1], row[2]) for row in records] == [
        ("created", snapshot["id"]),
        ("deleted", snapshot["id"]),
    ]
    assert [row[0] for row in records] == [1, 2]
    # The deleted record shares the confirmation commit time.
    assert records[1][3] == confirm.json()["confirmed_at"]


def test_failed_confirmation_writes_no_deleted_trail(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset(client)
    create_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    # A 10-day retention policy makes the immediate confirmation a 409.
    client.post(
        "/datasets/orders/versions/1/retention-policies",
        json={"retention_days": 10},
    )
    request = client.post(
        f"{SNAPSHOTS_PATH.format(dataset='orders', version=1)}"
        f"/{snapshot['id']}/deletion-requests",
        json={"reason": "done"},
    ).json()
    confirm = client.post(
        f"{SNAPSHOTS_PATH.format(dataset='orders', version=1)}"
        f"/{snapshot['id']}/deletion-requests/{request['id']}/confirm"
    )
    assert confirm.status_code == 409

    assert snapshot["id"] in cache_dump(isolated_database)
    causes = [row[1] for row in trail_dump(isolated_database)]
    assert causes == ["created"]


# --------------------------------------------------------------------------- #
# Cached vs fresh diff equivalence
# --------------------------------------------------------------------------- #


def test_diff_uses_the_cache_and_falls_back_to_fresh_computation(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset(client)
    create_version(client)
    first = make_snapshot(client, [{"id": 1}, {"id": 2}])
    second = make_snapshot(client, [{"id": 2}, {"id": 3}])
    path = (
        f"{SNAPSHOTS_PATH.format(dataset='orders', version=1)}"
        f"/{first['id']}/diff/{second['id']}"
    )

    cached_text = client.get(path).text

    # The cache is preferred: rewriting the stored rows (but not the cache,
    # whose document still matches the unchanged stored hash) leaves the diff
    # exactly as cached.
    replace_stored_rows(isolated_database, first["id"], [{"id": 999}])
    assert client.get(path).text == cached_text

    # With the cache record gone the same comparison is computed fresh from
    # the current rows and reflects the tampered content.
    drop_cache(isolated_database, first["id"])
    fresh = client.get(path)
    assert fresh.status_code == 200
    assert fresh.json()["removed"] == [{"row": {"id": 999}, "count": 1}]


def test_corrupt_cache_record_falls_back_and_result_matches_fresh_path(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset(client)
    create_version(client)
    first = make_snapshot(client, [{"id": 1}, {"id": 2}])
    second = make_snapshot(client, [{"id": 2}, {"id": 3}])
    path = (
        f"{SNAPSHOTS_PATH.format(dataset='orders', version=1)}"
        f"/{first['id']}/diff/{second['id']}"
    )

    expected = client.get(path).text

    corrupt_cache(isolated_database, first["id"], ['{"id":1}'])
    corrupt_cache(isolated_database, second["id"], ['{"id":2}'])
    # A stale cache record disagrees with the current rows, so the diff is
    # recomputed fresh — byte-identical to the all-cached document.
    assert client.get(path).text == expected

    drop_cache(isolated_database, first["id"])
    drop_cache(isolated_database, second["id"])
    assert client.get(path).text == expected


def test_time_diff_is_byte_identical_from_cache_and_fresh(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset(client)
    create_version(client)
    first = make_snapshot(client, [{"id": 1, "name": "a"}])
    second = make_snapshot(client, [{"id": 2, "name": "b"}])
    params = {"from": first["created_at"], "to": second["created_at"]}
    at_diff = f"{SNAPSHOTS_PATH.format(dataset='orders', version=1)}/at/diff"

    cached = client.get(at_diff, params=params).text
    drop_cache(isolated_database, first["id"])
    drop_cache(isolated_database, second["id"])
    assert client.get(at_diff, params=params).text == cached


def test_cross_version_diff_is_byte_identical_from_cache_and_fresh(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset(client)
    v1 = create_version(
        client, [{"name": "id", "type": "integer", "nullable": False}]
    )
    v2 = create_version(
        client,
        [
            {"name": "id", "type": "integer", "nullable": False},
            {"name": "name", "type": "string", "nullable": True},
        ],
    )
    base = make_snapshot(client, [{"id": 1}], version=v1)
    target = make_snapshot(client, [{"id": 1}, {"id": 2, "name": "x"}], version=v2)
    path = f"/datasets/orders/snapshots/{base['id']}/diff/{target['id']}"

    cached = client.get(path).text
    drop_cache(isolated_database, base["id"])
    drop_cache(isolated_database, target["id"])
    assert client.get(path).text == cached


def test_masked_time_diff_is_byte_identical_from_cache_and_fresh(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset(client)
    create_version(client)
    client.post(
        "/datasets/orders/versions/1/privacy-policies",
        json={
            "field": "name",
            "classification": "PII",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    first = make_snapshot(client, [{"id": 1, "name": "alice"}])
    second = make_snapshot(client, [{"id": 2, "name": "bob"}])
    masked = f"{SNAPSHOTS_PATH.format(dataset='orders', version=1)}/at/diff/masked"
    body = {"role": "guest", "from": first["created_at"], "to": second["created_at"]}

    cached = client.post(masked, json=body).text
    drop_cache(isolated_database, first["id"])
    drop_cache(isolated_database, second["id"])
    assert client.post(masked, json=body).text == cached


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


def test_audit_reports_all_three_states_with_counts(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset(client)
    create_version(client)
    cached_snap = make_snapshot(client, [{"id": 1}])
    missing_snap = make_snapshot(client, [{"id": 2}])
    mismatch_snap = make_snapshot(client, [{"id": 3}])
    drop_cache(isolated_database, missing_snap["id"])
    corrupt_cache(isolated_database, mismatch_snap["id"], ['{"id":999}'])

    response = client.get(AUDIT_PATH.format(dataset="orders", version=1))
    assert response.status_code == 200
    payload = response.json()
    assert payload["entries"] == [
        {"snapshot_id": cached_snap["id"], "status": "cached"},
        {"snapshot_id": missing_snap["id"], "status": "missing"},
        {"snapshot_id": mismatch_snap["id"], "status": "mismatch"},
    ]
    assert payload["counts"] == {
        "cached_count": 1,
        "missing_count": 1,
        "mismatch_count": 1,
    }


def test_audit_empty_version_has_empty_entries_and_zero_counts(
    client: TestClient,
) -> None:
    create_dataset(client)
    create_version(client)
    payload = client.get(
        AUDIT_PATH.format(dataset="orders", version=1)
    ).json()
    assert payload["entries"] == []
    assert payload["counts"] == {
        "cached_count": 0,
        "missing_count": 0,
        "mismatch_count": 0,
    }


def test_audit_read_never_writes_or_repairs(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset(client)
    create_version(client)
    missing = make_snapshot(client, [{"id": 1}])
    mismatch = make_snapshot(client, [{"id": 2}])
    drop_cache(isolated_database, missing["id"])
    corrupt_cache(isolated_database, mismatch["id"], ['{"id":999}'])
    before = cache_dump(isolated_database)
    trail_before = trail_dump(isolated_database)

    for _ in range(3):
        response = client.get(AUDIT_PATH.format(dataset="orders", version=1))
        assert response.status_code == 200
        statuses = {e["snapshot_id"]: e["status"] for e in response.json()["entries"]}
        assert statuses[missing["id"]] == "missing"
        assert statuses[mismatch["id"]] == "mismatch"

    assert cache_dump(isolated_database) == before
    assert trail_dump(isolated_database) == trail_before


def test_audit_document_is_deterministic_compact_json_with_newline(
    client: TestClient,
) -> None:
    create_dataset(client)
    create_version(client)
    make_snapshot(client, [{"id": 1}])

    response = client.get(AUDIT_PATH.format(dataset="orders", version=1))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.text.endswith("}\n")
    assert not response.text.endswith("}\n\n")
    payload = response.json()
    assert list(payload) == ["dataset", "version", "entries", "counts"]
    assert list(payload["entries"][0]) == ["snapshot_id", "status"]
    assert list(payload["counts"]) == [
        "cached_count",
        "missing_count",
        "mismatch_count",
    ]
    assert response.text == (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    )
    # Repeated reads are byte-identical.
    again = client.get(AUDIT_PATH.format(dataset="orders", version=1)).text
    assert again == response.text


# --------------------------------------------------------------------------- #
# Trail
# --------------------------------------------------------------------------- #


def test_trail_empty_version_returns_empty_entries(client: TestClient) -> None:
    create_dataset(client)
    create_version(client)
    payload = client.get(
        TRAIL_PATH.format(dataset="orders", version=1)
    ).json()
    assert payload == {"dataset": "orders", "version": 1, "entries": []}


def test_trail_orders_by_sequence_and_survives_restart(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset(client)
    create_version(client)
    snapshots = [make_snapshot(client, [{"id": i}]) for i in range(3)]

    text_before = client.get(
        TRAIL_PATH.format(dataset="orders", version=1)
    ).text
    payload = json.loads(text_before)
    assert [entry["sequence"] for entry in payload["entries"]] == [1, 2, 3]
    assert [entry["cause"] for entry in payload["entries"]] == ["created"] * 3
    assert [entry["snapshot_id"] for entry in payload["entries"]] == [
        snapshot["id"] for snapshot in snapshots
    ]
    for entry in payload["entries"]:
        assert list(entry) == ["sequence", "cause", "snapshot_id", "created_at"]
        assert datetime.fromisoformat(entry["created_at"]).tzinfo is not None

    restarted = TestClient(client.app)
    assert (
        restarted.get(TRAIL_PATH.format(dataset="orders", version=1)).text
        == text_before
    )


def test_trail_document_is_deterministic_compact_json_with_newline(
    client: TestClient,
) -> None:
    create_dataset(client)
    create_version(client)
    make_snapshot(client, [{"id": 1}])

    response = client.get(TRAIL_PATH.format(dataset="orders", version=1))
    assert response.text.endswith("}\n")
    assert not response.text.endswith("}\n\n")
    payload = response.json()
    assert list(payload) == ["dataset", "version", "entries"]
    assert response.text == (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    )


def test_trail_read_never_writes(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset(client)
    create_version(client)
    make_snapshot(client, [{"id": 1}])
    before = cache_dump(isolated_database)
    trail_before = trail_dump(isolated_database)
    for _ in range(2):
        assert (
            client.get(TRAIL_PATH.format(dataset="orders", version=1)).status_code
            == 200
        )
    assert cache_dump(isolated_database) == before
    assert trail_dump(isolated_database) == trail_before


def test_trail_is_isolated_per_version(
    client: TestClient,
) -> None:
    create_dataset(client)
    create_version(
        client,
        [{"name": "id", "type": "integer", "nullable": False}],
    )
    create_version(
        client,
        [{"name": "id", "type": "integer", "nullable": False}],
    )
    make_snapshot(client, [{"id": 1}], version=1)
    make_snapshot(client, [{"id": 1}], version=2)
    make_snapshot(client, [{"id": 2}], version=2)

    v1 = client.get(TRAIL_PATH.format(dataset="orders", version=1)).json()
    v2 = client.get(TRAIL_PATH.format(dataset="orders", version=2)).json()
    assert [e["sequence"] for e in v1["entries"]] == [1]
    assert [e["sequence"] for e in v2["entries"]] == [1, 2]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def test_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    create_dataset(client)
    create_version(client)
    for path in (AUDIT_PATH, TRAIL_PATH):
        assert client.get(path.format(dataset="ghost", version=1)).status_code == 404
        assert client.get(path.format(dataset="orders", version=9)).status_code == 404


def test_body_bytes_and_query_parameters_are_422(client: TestClient) -> None:
    create_dataset(client)
    create_version(client)
    for path in (AUDIT_PATH, TRAIL_PATH):
        url = path.format(dataset="orders", version=1)
        for raw in (b"{}", b" ", b"   ", b" \t\n"):
            response = client.request("GET", url, content=raw)
            assert response.status_code == 422, raw
            assert set(response.json()) == {"error", "detail"}
        for params in ({"x": "1"}, {"snapshot_id": "1"}):
            response = client.get(url, params=params)
            assert response.status_code == 422, params
            assert response.json()["error"] == "validation_error"


def test_404_takes_precedence_over_422(client: TestClient) -> None:
    create_dataset(client)
    create_version(client)
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
            path.format(dataset="orders", version=9),
            params={"x": "1"},
            content=b"{}",
        )
        assert response.status_code == 404


def test_only_get_is_accepted(client: TestClient) -> None:
    create_dataset(client)
    create_version(client)
    for path in (AUDIT_PATH, TRAIL_PATH):
        url = path.format(dataset="orders", version=1)
        for method in ("post", "put", "delete", "patch"):
            assert client.request(method, url, content=b"{}").status_code == 405


def test_rejections_write_nothing(
    client: TestClient, isolated_database: Path
) -> None:
    create_dataset(client)
    create_version(client)
    make_snapshot(client, [{"id": 1}])
    cache_before = cache_dump(isolated_database)
    trail_before = trail_dump(isolated_database)

    client.request(
        "GET", AUDIT_PATH.format(dataset="ghost", version=1), content=b"{}"
    )
    client.request(
        "GET", TRAIL_PATH.format(dataset="orders", version=9), content=b"  "
    )
    client.get(AUDIT_PATH.format(dataset="orders", version=1), params={"x": "1"})

    assert cache_dump(isolated_database) == cache_before
    assert trail_dump(isolated_database) == trail_before


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_creations_keep_trail_sequences_continuous(
    client: TestClient,
) -> None:
    count = 8
    create_dataset(client)
    create_version(
        client, [{"name": "id", "type": "integer", "nullable": False}]
    )

    failures: list[Exception] = []
    barrier = threading.Barrier(count)

    def worker(index: int) -> None:
        local = TestClient(client.app)
        barrier.wait()
        try:
            response = local.post(
                SNAPSHOTS_PATH.format(dataset="orders", version=1),
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

    payload = client.get(
        TRAIL_PATH.format(dataset="orders", version=1)
    ).json()
    entries = payload["entries"]
    assert [entry["sequence"] for entry in entries] == list(range(1, count + 1))
    assert len({entry["snapshot_id"] for entry in entries}) == count
    assert all(entry["cause"] == "created" for entry in entries)

    audit = client.get(AUDIT_PATH.format(dataset="orders", version=1)).json()
    assert audit["counts"]["cached_count"] == count
    assert audit["counts"]["missing_count"] == 0


def test_concurrent_confirmations_keep_trail_sequences_continuous(
    client: TestClient,
) -> None:
    count = 6
    create_dataset(client)
    create_version(
        client, [{"name": "id", "type": "integer", "nullable": False}]
    )
    client.post(
        "/datasets/orders/versions/1/retention-policies",
        json={"retention_days": 0},
    )
    snapshots = [
        make_snapshot(client, [{"id": i}]) for i in range(count)
    ]
    requests = []
    for snapshot in snapshots:
        response = client.post(
            f"{SNAPSHOTS_PATH.format(dataset='orders', version=1)}"
            f"/{snapshot['id']}/deletion-requests",
            json={"reason": "done"},
        )
        assert response.status_code == 201, response.text
        requests.append(response.json())

    failures: list[Exception] = []
    barrier = threading.Barrier(count)

    def worker(index: int) -> None:
        local = TestClient(client.app)
        snapshot = snapshots[index]
        request_id = requests[index]["id"]
        barrier.wait()
        try:
            response = local.post(
                f"{SNAPSHOTS_PATH.format(dataset='orders', version=1)}"
                f"/{snapshot['id']}/deletion-requests/{request_id}/confirm"
            )
            assert response.status_code == 200, response.text
        except Exception as exc:  # pragma: no cover - reported below
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures, failures

    payload = client.get(
        TRAIL_PATH.format(dataset="orders", version=1)
    ).json()
    entries = payload["entries"]
    # count 'created' records followed by count 'deleted' records, sequences
    # running continuously from 1 with no gap or repeat.
    assert [entry["sequence"] for entry in entries] == list(
        range(1, 2 * count + 1)
    )
    assert [entry["cause"] for entry in entries[:count]] == ["created"] * count
    deleted = entries[count:]
    assert sorted(entry["cause"] for entry in deleted) == ["deleted"] * count
    assert sorted(entry["snapshot_id"] for entry in deleted) == sorted(
        snapshot["id"] for snapshot in snapshots
    )
    assert client.get(
        AUDIT_PATH.format(dataset="orders", version=1)
    ).json()["entries"] == []
