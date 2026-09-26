"""Tests for the read-only preview of the batch retention sweep."""

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
    return f"/datasets/{dataset}/versions/{version}/snapshots/retention-sweep/preview"


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


def preview(client: TestClient, dataset: str = "raw", version: int = 1) -> dict:
    response = client.get(preview_path(dataset, version))
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Preview behaviour
# --------------------------------------------------------------------------- #


def test_preview_classifies_every_snapshot_exactly_once(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=7)
    expired = make_snapshot(client, rows=[{"id": 1}])
    protected = make_snapshot(client, rows=[{"id": 2}])
    young = make_snapshot(client, rows=[{"id": 3}])
    backdate_snapshot(expired["id"], days=8)
    backdate_snapshot(protected["id"], days=9)
    open_request = client.post(
        requests_path("raw", 1, protected["id"]), json={"reason": "keep me"}
    )
    assert open_request.status_code == 201, open_request.text

    body = preview(client)

    assert list(body) == [
        "dataset",
        "version",
        "would_create",
        "skipped",
        "not_due",
        "counts",
    ]
    assert body["dataset"] == "raw"
    assert body["version"] == 1
    assert body["would_create"] == [
        {"snapshot_id": expired["id"], "status": "pending"}
    ]
    assert body["skipped"] == [
        {
            "snapshot_id": protected["id"],
            "request_id": open_request.json()["id"],
            "status": "pending",
        }
    ]
    assert body["not_due"] == [{"snapshot_id": young["id"]}]
    assert body["counts"] == {
        "would_create_count": 1,
        "skipped_count": 1,
        "not_due_count": 1,
    }
    # Every snapshot appears in exactly one collection.
    seen = [
        entry["snapshot_id"]
        for collection in ("would_create", "skipped", "not_due")
        for entry in body[collection]
    ]
    assert sorted(seen) == [expired["id"], protected["id"], young["id"]]
    assert sum(body["counts"].values()) == 3


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


def test_preview_marks_would_create_blocked_when_downstream_exists(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    body = preview(client)

    assert body["would_create"] == [
        {"snapshot_id": snapshot["id"], "status": "blocked"}
    ]


def test_preview_skips_not_due_snapshot_with_open_request(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=7)
    protected = make_snapshot(client)
    open_request = client.post(
        requests_path("raw", 1, protected["id"]), json={"reason": "early"}
    )
    assert open_request.status_code == 201, open_request.text

    body = preview(client)

    # Not due but carrying an open request: skipped, exactly as the sweep
    # classifies it.
    assert body["skipped"] == [
        {
            "snapshot_id": protected["id"],
            "request_id": open_request.json()["id"],
            "status": "pending",
        }
    ]
    assert body["not_due"] == []
    assert body["counts"] == {
        "would_create_count": 0,
        "skipped_count": 1,
        "not_due_count": 0,
    }


def test_preview_echoes_blocked_open_request(client: TestClient) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)
    open_request = client.post(
        requests_path("raw", 1, snapshot["id"]), json={"reason": "held"}
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


def test_preview_writes_nothing(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    first = preview(client)
    second = preview(client)

    assert first == second
    assert first["counts"]["would_create_count"] == 1
    # No deletion request was opened by the previews.
    assert client.get(requests_path("raw", 1, snapshot["id"])).json() == []


def test_preview_matches_a_subsequent_real_sweep(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=7)
    expired = make_snapshot(client, rows=[{"id": 1}])
    protected = make_snapshot(client, rows=[{"id": 2}])
    young = make_snapshot(client, rows=[{"id": 3}])
    backdate_snapshot(expired["id"], days=8)
    backdate_snapshot(protected["id"], days=9)
    open_request = client.post(
        requests_path("raw", 1, protected["id"]), json={"reason": "original"}
    )
    assert open_request.status_code == 201, open_request.text

    before = preview(client)

    swept = client.post(sweep_path(), json={"reason": "retention reached"})
    assert swept.status_code == 200, swept.text
    swept = swept.json()

    # The preview's classification and statuses are exactly what the sweep
    # then created.
    assert [
        (entry["snapshot_id"], entry["status"]) for entry in before["would_create"]
    ] == [(entry["snapshot_id"], entry["status"]) for entry in swept["created"]]
    assert before["skipped"] == [
        {
            "snapshot_id": entry["snapshot_id"],
            "request_id": entry["request_id"],
            "status": entry["status"],
        }
        for entry in swept["skipped"]
    ]
    assert [entry["snapshot_id"] for entry in before["not_due"]] == [young["id"]]
    assert swept["counts"] == {
        "created_count": before["counts"]["would_create_count"],
        "skipped_count": before["counts"]["skipped_count"],
        "not_due_count": before["counts"]["not_due_count"],
    }

    after = preview(client)
    assert after["would_create"] == []
    assert [entry["snapshot_id"] for entry in after["skipped"]] == [
        expired["id"],
        protected["id"],
    ]
    assert [entry["snapshot_id"] for entry in after["not_due"]] == [young["id"]]


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
    # An unknown dataset/version is a 404 even when the request shape is
    # invalid.
    assert client.request("GET", preview_path("ghost"), content=b"x").status_code == 404
    assert client.get(preview_path("ghost"), params={"x": "1"}).status_code == 404
    make_dataset(client, "raw", ["id"])
    assert client.request("GET", preview_path("raw", 9), content=b"x").status_code == 404
    # A missing policy is likewise a 404 ahead of request-shape errors.
    assert client.request("GET", preview_path(), content=b"x").status_code == 404
    assert client.get(preview_path(), params={"x": "1"}).status_code == 404


def test_preview_rejects_any_request_body_and_writes_nothing(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    for content in (b"x", b"   ", b" \t\n", b"{}", b"not json", b'{"reason": "x"}'):
        response = client.request("GET", preview_path(), content=content)
        assert response.status_code == 422, content
        assert response.json()["error"] == "validation_error"

    # Nothing was written: the snapshot still has no deletion request.
    assert client.get(requests_path("raw", 1, snapshot["id"])).json() == []


def test_preview_rejects_query_parameters_and_writes_nothing(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)

    response = client.get(preview_path(), params={"reason": "x"})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get(requests_path("raw", 1, snapshot["id"])).json() == []


def test_preview_error_shape_has_error_and_detail(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)

    response = client.request("GET", preview_path(), content=b"x")
    assert response.status_code == 422
    assert set(response.json()) == {"error", "detail"}

    response = client.get(preview_path("ghost"))
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
