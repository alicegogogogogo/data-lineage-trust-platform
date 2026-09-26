"""Tests for the read-only retention sweep preview of a version's snapshots."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_dataset(client: TestClient, name: str, fields: list[str]) -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions",
        json={
            "fields": [
                {"name": field, "type": "string", "nullable": True}
                for field in fields
            ]
        },
    )
    assert response.status_code == 201, response.text


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


def make_snapshot(
    client: TestClient, dataset: str = "raw", version: int = 1, rows: list | None = None
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots",
        json={"rows": rows if rows is not None else [{"id": 1}]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_policy(
    client: TestClient, dataset: str = "raw", version: int = 1, retention_days: int = 7
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/retention-policies",
        json={"retention_days": retention_days},
    )
    assert response.status_code == 201, response.text
    return response.json()


def preview_path(dataset: str = "raw", version: int = 1) -> str:
    return (
        f"/datasets/{dataset}/versions/{version}/snapshots/retention-sweep/preview"
    )


def sweep_path(dataset: str = "raw", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/snapshots/retention-sweep"


def requests_path(dataset: str, version: int, snapshot_id: int) -> str:
    return (
        f"/datasets/{dataset}/versions/{version}/snapshots/"
        f"{snapshot_id}/deletion-requests"
    )


def backdate_snapshot(snapshot_id: int, days: int) -> None:
    """Age an existing snapshot directly in the database (bypassing the API)."""
    db_path = os.environ["DATA_LINEAGE_DB"]
    old = (datetime.now(timezone.utc) - timedelta(days=days, seconds=1)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE snapshots SET created_at = ? WHERE id = ?",
            (old, snapshot_id),
        )


def preview(client: TestClient) -> dict:
    response = client.get(preview_path())
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


def test_preview_classifies_every_snapshot_exactly_once(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=7)
    expired_open = make_snapshot(client, rows=[{"id": 1}])
    expired_free = make_snapshot(client, rows=[{"id": 2}])
    young_open = make_snapshot(client, rows=[{"id": 3}])
    young_free = make_snapshot(client, rows=[{"id": 4}])
    backdate_snapshot(expired_open["id"], days=8)
    backdate_snapshot(expired_free["id"], days=9)

    first_request = client.post(
        requests_path("raw", 1, expired_open["id"]),
        json={"reason": "open on expired"},
    )
    assert first_request.status_code == 201, first_request.text
    second_request = client.post(
        requests_path("raw", 1, young_open["id"]),
        json={"reason": "open on young"},
    )
    assert second_request.status_code == 201, second_request.text

    body = preview(client)

    assert set(body) == {
        "dataset",
        "version",
        "would_create",
        "skipped",
        "not_due",
        "counts",
    }
    assert body["dataset"] == "raw"
    assert body["version"] == 1

    assert body["would_create"] == [
        {"snapshot_id": expired_free["id"], "status": "pending"}
    ]
    assert body["skipped"] == [
        {
            "snapshot_id": expired_open["id"],
            "request_id": first_request.json()["id"],
            "status": "pending",
        },
        {
            "snapshot_id": young_open["id"],
            "request_id": second_request.json()["id"],
            "status": "pending",
        },
    ]
    # The not-due snapshot carrying an open request is skipped, so only the
    # young request-free snapshot is not due.
    assert body["not_due"] == [young_free["id"]]
    assert body["counts"] == {
        "would_create_count": 1,
        "skipped_count": 2,
        "not_due_count": 1,
    }
    assert (
        body["counts"]["would_create_count"]
        + body["counts"]["skipped_count"]
        + body["counts"]["not_due_count"]
        == 4
    )

    # Each snapshot appears in exactly one collection, and every collection is
    # itself ordered by snapshot id ascending.
    would_create_ids = [entry["snapshot_id"] for entry in body["would_create"]]
    skipped_ids = [entry["snapshot_id"] for entry in body["skipped"]]
    listed = would_create_ids + skipped_ids + body["not_due"]
    assert would_create_ids == sorted(would_create_ids)
    assert skipped_ids == sorted(skipped_ids)
    assert body["not_due"] == sorted(body["not_due"])
    assert len(listed) == len(set(listed)) == 4


def test_preview_status_is_blocked_when_downstream_exists(client: TestClient) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    body = preview(client)

    assert body["would_create"] == [
        {"snapshot_id": snapshot["id"], "status": "blocked"}
    ]
    assert body["skipped"] == []
    assert body["not_due"] == []
    assert body["counts"] == {
        "would_create_count": 1,
        "skipped_count": 0,
        "not_due_count": 0,
    }


def test_preview_echoes_a_blocked_open_request(client: TestClient) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    make_policy(client, retention_days=7)
    snapshot = make_snapshot(client)
    open_request = client.post(
        requests_path("raw", 1, snapshot["id"]), json={"reason": "blocked one"}
    )
    assert open_request.status_code == 201, open_request.text
    assert open_request.json()["status"] == "blocked"

    body = preview(client)

    assert body["would_create"] == []
    assert body["skipped"] == [
        {
            "snapshot_id": snapshot["id"],
            "request_id": open_request.json()["id"],
            "status": "blocked",
        }
    ]
    assert body["not_due"] == []


def test_preview_collections_are_sorted_by_snapshot_id(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=7)
    snapshots = [make_snapshot(client, rows=[{"id": i}]) for i in range(5)]
    # Every snapshot is due; alternate ones already carry open requests.
    for index, snapshot in enumerate(snapshots):
        backdate_snapshot(snapshot["id"], days=8)
        if index % 2 == 0:
            response = client.post(
                requests_path("raw", 1, snapshot["id"]),
                json={"reason": f"open {index}"},
            )
            assert response.status_code == 201, response.text

    body = preview(client)

    open_ids = [snapshots[i]["id"] for i in (0, 2, 4)]
    free_ids = [snapshots[i]["id"] for i in (1, 3)]
    assert [entry["snapshot_id"] for entry in body["would_create"]] == free_ids
    assert [entry["snapshot_id"] for entry in body["skipped"]] == open_ids
    assert body["not_due"] == []


def test_preview_of_version_without_snapshots_succeeds(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)

    assert preview(client) == {
        "dataset": "raw",
        "version": 1,
        "would_create": [],
        "skipped": [],
        "not_due": [],
        "counts": {
            "would_create_count": 0,
            "skipped_count": 0,
            "not_due_count": 0,
        },
    }


# --------------------------------------------------------------------------- #
# Deterministic document
# --------------------------------------------------------------------------- #


def test_preview_response_is_a_deterministic_document(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    response = client.get(preview_path())

    assert response.status_code == 200, response.text
    payload = response.json()
    assert list(payload) == [
        "dataset",
        "version",
        "would_create",
        "skipped",
        "not_due",
        "counts",
    ]
    assert list(payload["counts"]) == [
        "would_create_count",
        "skipped_count",
        "not_due_count",
    ]
    assert list(payload["would_create"][0]) == ["snapshot_id", "status"]
    # Compact whitespace, fixed key order, exactly one trailing newline.
    assert response.text == (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    )
    assert response.text == (
        '{"dataset":"raw","version":1,"would_create":[{"snapshot_id":'
        f'{snapshot["id"]},"status":"pending"}}],"skipped":[],"not_due":[],'
        '"counts":{"would_create_count":1,"skipped_count":0,'
        '"not_due_count":0}}\n'
    )


# --------------------------------------------------------------------------- #
# Read-only behaviour and parity with the real sweep
# --------------------------------------------------------------------------- #


def test_preview_writes_nothing_and_is_stable(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    first = client.get(preview_path())
    second = client.get(preview_path())
    assert first.status_code == second.status_code == 200
    assert first.text == second.text

    # The preview opened no deletion request and changed no snapshot.
    assert client.get(requests_path("raw", 1, snapshot["id"])).json() == []
    assert (
        client.get(f"/datasets/raw/versions/1/snapshots/{snapshot['id']}").status_code
        == 200
    )


def test_preview_matches_the_following_real_sweep(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=7)
    expired_open = make_snapshot(client, rows=[{"id": 1}])
    expired_free_a = make_snapshot(client, rows=[{"id": 2}])
    young_open = make_snapshot(client, rows=[{"id": 3}])
    expired_free_b = make_snapshot(client, rows=[{"id": 4}])
    young_free = make_snapshot(client, rows=[{"id": 5}])
    for snapshot in (expired_open, expired_free_a, expired_free_b):
        backdate_snapshot(snapshot["id"], days=8)

    open_a = client.post(
        requests_path("raw", 1, expired_open["id"]), json={"reason": "old open"}
    )
    assert open_a.status_code == 201, open_a.text
    open_b = client.post(
        requests_path("raw", 1, young_open["id"]), json={"reason": "young open"}
    )
    assert open_b.status_code == 201, open_b.text

    before = preview(client)

    sweep_response = client.post(sweep_path(), json={"reason": "retention reached"})
    assert sweep_response.status_code == 200, sweep_response.text
    swept = sweep_response.json()

    # The would-create preview entries match the requests the sweep opened,
    # snapshot id and status alike and in the same order.
    assert [entry["snapshot_id"] for entry in before["would_create"]] == [
        entry["snapshot_id"] for entry in swept["created"]
    ]
    preview_status = {
        entry["snapshot_id"]: entry["status"]
        for entry in before["would_create"]
    }
    for entry in swept["created"]:
        assert entry["status"] == preview_status[entry["snapshot_id"]]

    # The skipped preview entries match the sweep's echoes (id and status).
    assert before["skipped"] == [
        {
            "snapshot_id": entry["snapshot_id"],
            "request_id": entry["request_id"],
            "status": entry["status"],
        }
        for entry in swept["skipped"]
    ]

    # The not-due collection matches the sweep's not-due count.
    assert before["counts"]["not_due_count"] == swept["counts"]["not_due_count"]
    assert before["counts"] == {
        "would_create_count": swept["counts"]["created_count"],
        "skipped_count": swept["counts"]["skipped_count"],
        "not_due_count": swept["counts"]["not_due_count"],
    }
    assert before["not_due"] == [young_free["id"]]

    # After the sweep the preview is all skips: the newly opened requests are
    # echoed with their real ids, nothing moves to would-create.
    request_by_snapshot = {
        entry["snapshot_id"]: entry for entry in swept["created"]
    }
    after = preview(client)
    assert after["would_create"] == []
    assert after["not_due"] == [young_free["id"]]
    assert {entry["snapshot_id"] for entry in after["skipped"]} == {
        expired_open["id"],
        expired_free_a["id"],
        young_open["id"],
        expired_free_b["id"],
    }
    for entry in after["skipped"]:
        if entry["snapshot_id"] in request_by_snapshot:
            assert (
                entry["request_id"]
                == request_by_snapshot[entry["snapshot_id"]]["request_id"]
            )
            assert (
                entry["status"]
                == request_by_snapshot[entry["snapshot_id"]]["status"]
            )


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def test_preview_without_retention_policy_is_404(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_snapshot(client)

    response = client.get(preview_path())
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_preview_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    assert client.get(preview_path("ghost")).status_code == 404
    make_dataset(client, "raw", ["id"])
    assert client.get(preview_path("raw", 9)).status_code == 404


def test_preview_404_takes_precedence_over_request_shape(client: TestClient) -> None:
    # An unknown dataset/version is a 404 even with a body or query parameter.
    assert client.request("GET", preview_path("ghost"), content=b" ").status_code == 404
    assert (
        client.get(preview_path("ghost"), params={"x": "1"}).status_code == 404
    )
    make_dataset(client, "raw", ["id"])
    assert (
        client.request("GET", preview_path("raw", 9), content=b" ").status_code == 404
    )
    # A missing policy is likewise a 404 ahead of shape errors.
    assert client.request("GET", preview_path(), content=b" ").status_code == 404
    assert client.get(preview_path(), params={"x": "1"}).status_code == 404


def test_preview_rejects_body_and_query_params_and_writes_nothing(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    # An empty body is the legitimate parameterless GET; any present bytes,
    # whitespace-only included, are a shape error.
    for content in (b" ", b"   ", b" \t\n", b"not json", b"{}", b'["x"]'):
        response = client.request("GET", preview_path(), content=content)
        assert response.status_code == 422, content
        assert response.json()["error"] == "validation_error"
    assert client.request("GET", preview_path(), content=b"").status_code == 200

    response = client.get(preview_path(), params={"x": "1"})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"

    # Nothing was written: the snapshot still has no deletion request and the
    # well-formed preview still classifies it as would-create.
    assert client.get(requests_path("raw", 1, snapshot["id"])).json() == []
    assert [entry["snapshot_id"] for entry in preview(client)["would_create"]] == [
        snapshot["id"]
    ]


def test_preview_only_accepts_get(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        response = client.request(method, preview_path())
        assert response.status_code == 405, (method, response.status_code)
