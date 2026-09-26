"""Tests for the controlled recheck of snapshot deletion requests.

A deletion request's blocked/pending baseline is computed once at creation;
the ``POST .../deletion-requests/{request_id}/recheck`` entry point recomputes
that state against the current lineage graph.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import errors as api_errors
from app import repository

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
            "target_version":  target_version,
            "target_field": target_field,
            "source_dataset": source_dataset,
            "source_version": source_version,
            "source_field": source_field,
        },
    )
    assert response.status_code == 201, response.text


def remove_link(
    client: TestClient,
    source: tuple[str, int, str],
    target: tuple[str, int, str],
) -> None:
    source_dataset, source_version, source_field = source
    target_dataset, target_version, target_field = target
    response = client.request(
        "DELETE",
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
    assert response.status_code == 200, response.text


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


def requests_path(dataset: str, version: int, snapshot_id: int) -> str:
    return (
        f"/datasets/{dataset}/versions/{version}/snapshots/"
        f"{snapshot_id}/deletion-requests"
    )


def create_request(
    client: TestClient,
    snapshot_id: int,
    reason: str = "no longer needed",
    dataset: str = "raw",
    version: int = 1,
) -> dict:
    response = client.post(
        requests_path(dataset, version, snapshot_id), json={"reason": reason}
    )
    assert response.status_code == 201, response.text
    return response.json()


def recheck(
    client: TestClient,
    snapshot_id: int,
    request_id: int,
    *,
    dataset: str = "raw",
    version: int = 1,
    content: bytes = b"",
    params: dict | None = None,
    method: str = "POST",
):
    return client.request(
        method,
        f"{requests_path(dataset, version, snapshot_id)}/{request_id}/recheck",
        content=content,
        params=params,
    )


def confirm(
    client: TestClient, snapshot_id: int, request_id: int
) -> int:
    return client.post(
        f"{requests_path('raw', 1, snapshot_id)}/{request_id}/confirm"
    ).status_code


def backdate_snapshot(snapshot_id: int, days: int) -> None:
    db_path = os.environ["DATA_LINEAGE_DB"]
    old = (
        datetime.now(timezone.utc) - timedelta(days=days, seconds=1)
    ).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE snapshots SET created_at = ? WHERE id = ?",
            (old, snapshot_id),
        )


# --------------------------------------------------------------------------- #
# Success behavior
# --------------------------------------------------------------------------- #


def test_recheck_pending_request_finds_new_direct_and_indirect_downstream(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    make_dataset(client, "bi", ["report_id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    assert request["status"] == "pending"
    assert request["impacted"] == []

    # A direct downstream appears first.
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    first = recheck(client, snapshot["id"], request["id"])
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["status"] == "blocked"
    assert first_body["impacted"] == [
        {"dataset": "dm", "version": 1, "field": "id"}
    ]

    # Re-running after an indirect downstream is added recomputes the union.
    add_link(client, ("dm", 1, "id"), ("bi", 1, "report_id"))
    second = recheck(client, snapshot["id"], request["id"])
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["status"] == "blocked"
    assert second_body["impacted"] == [
        {"dataset": "bi", "version": 1, "field": "report_id"},
        {"dataset": "dm", "version": 1, "field": "id"},
    ]


def test_recheck_blocked_request_returns_to_pending_when_downstream_removed(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    assert request["status"] == "blocked"

    remove_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    response = recheck(client, snapshot["id"], request["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["impacted"] == []

    # The request then confirms under the existing age rule.
    backdate_snapshot(snapshot["id"], days=7)
    assert confirm(client, snapshot["id"], request["id"]) == 200


def test_recheck_recomputes_union_deduplicates_and_sorts(client: TestClient) -> None:
    make_dataset(client, "raw", ["a", "b"])
    make_dataset(client, "dm", ["x"])
    make_dataset(client, "bi", ["y"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])

    # Two version fields feeding the same downstream field list it once; the
    # indirect field is reached through the same shared node.
    add_link(client, ("raw", 1, "a"), ("dm", 1, "x"))
    add_link(client, ("raw", 1, "b"), ("dm", 1, "x"))
    add_link(client, ("dm", 1, "x"), ("bi", 1, "y"))
    body = recheck(client, snapshot["id"], request["id"]).json()
    assert body["impacted"] == [
        {"dataset": "bi", "version": 1, "field": "y"},
        {"dataset": "dm", "version": 1, "field": "x"},
    ]


def test_recheck_walk_terminates_on_cycles_and_excludes_start_fields(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["a"])
    make_dataset(client, "dm", ["x"])
    make_dataset(client, "bi", ["y"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])

    add_link(client, ("raw", 1, "a"), ("dm", 1, "x"))
    add_link(client, ("dm", 1, "x"), ("bi", 1, "y"))
    add_link(client, ("bi", 1, "y"), ("dm", 1, "x"))
    body = recheck(client, snapshot["id"], request["id"]).json()
    assert body["impacted"] == [
        {"dataset": "bi", "version": 1, "field": "y"},
        {"dataset": "dm", "version": 1, "field": "x"},
    ]
    assert all(item["dataset"] != "raw" for item in body["impacted"])


def test_recheck_idempotent_when_graph_unchanged(client: TestClient) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])

    body = recheck(client, snapshot["id"], request["id"]).json()
    assert body["status"] == "blocked"
    assert set(body) == {
        "id",
        "snapshot_id",
        "policy_id",
        "reason",
        "status",
        "impacted",
        "created_at",
    }
    # The non-derived fields stay exactly as created.
    for field in ("id", "snapshot_id", "policy_id", "reason", "created_at"):
        assert body[field] == request[field]


def test_recheck_does_not_delete_snapshot_or_create_requests(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])

    assert recheck(client, snapshot["id"], request["id"]).status_code == 200
    # The snapshot is still readable and the collection still holds one
    # request; rechecks do not create or confirm.
    assert (
        client.get(f"/datasets/raw/versions/1/snapshots/{snapshot['id']}").status_code
        == 200
    )
    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert [item["id"] for item in listed] == [request["id"]]
    assert listed[0]["status"] == "pending"


# --------------------------------------------------------------------------- #
# 409 behavior
# --------------------------------------------------------------------------- #


def test_recheck_confirmed_request_is_409_even_after_snapshot_deleted(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    assert confirm(client, snapshot["id"], request["id"]) == 200

    # The snapshot is gone but a recheck is still refused with 409 and no
    # column changes.
    response = recheck(client, snapshot["id"], request["id"])
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert listed[0]["status"] == "confirmed"
    assert listed[0]["impacted"] == []
    assert "confirmed_at" not in listed[0]


def test_recheck_then_confirm_persists_and_lists_by_id(client: TestClient) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    assert request["status"] == "pending"

    # New downstream blocks the request; it must not confirm.
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    blocked = recheck(client, snapshot["id"], request["id"]).json()
    assert blocked["status"] == "blocked"
    assert confirm(client, snapshot["id"], request["id"]) == 409
    assert (
        client.get(f"/datasets/raw/versions/1/snapshots/{snapshot['id']}").status_code
        == 200
    )

    # Downstream removed; recheck frees it and the existing confirm flow works.
    remove_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    freed = recheck(client, snapshot["id"], request["id"]).json()
    assert freed["status"] == "pending"
    assert confirm(client, snapshot["id"], request["id"]) == 200
    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert [item["id"] for item in listed] == [request["id"]]
    assert listed[0]["status"] == "confirmed"


# --------------------------------------------------------------------------- #
# 404 / 422 precedence
# --------------------------------------------------------------------------- #


def test_recheck_unknown_dataset_version_request_is_404(client: TestClient) -> None:
    assert recheck(client, 1, 1).status_code == 404
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    assert recheck(client, snapshot["id"], 999).status_code == 404
    assert recheck(client, snapshot["id"], 1, version=9).status_code == 404


def test_recheck_request_under_another_snapshot_path_is_422(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    first = make_snapshot(client, rows=[{"id": 1}])
    second = make_snapshot(client, rows=[{"id": 2}])
    request = create_request(client, first["id"])

    response = recheck(client, second["id"], request["id"])
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    # No write: the request record is unchanged.
    listed = client.get(requests_path("raw", 1, first["id"])).json()
    assert listed[0]["status"] == "pending"
    assert listed[0]["impacted"] == []


def test_recheck_request_of_another_version_or_dataset_path_is_422(
    client: TestClient,
) -> None:
    # Two datasets each with a policy, snapshot and open request. Addressing a
    # real request through another dataset/version/snapshot path is a 422, not
    # a 404: the request exists but is out of the path's scope.
    make_dataset(client, "raw", ["id"])
    make_dataset(client, "other", ["id"])
    make_policy(client)
    make_policy(client, dataset="other")
    raw_snapshot = make_snapshot(client)
    other_snapshot = make_snapshot(client, dataset="other")
    raw_request = create_request(client, raw_snapshot["id"])
    other_request = create_request(
        client, other_snapshot["id"], dataset="other"
    )

    response = recheck(
        client,
        other_snapshot["id"],
        raw_request["id"],
        dataset="other",
        version=1,
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    # The inverse path mismatch is likewise a 422.
    response = recheck(
        client,
        raw_snapshot["id"],
        other_request["id"],
    )
    assert response.status_code == 422
    # Neither request changed.
    assert (
        client.get(requests_path("raw", 1, raw_snapshot["id"])).json()[0]["status"]
        == "pending"
    )
    assert (
        client.get(
            requests_path("other", 1, other_snapshot["id"])
        ).json()[0]["status"]
        == "pending"
    )


def test_recheck_rejects_any_body_or_query_as_422(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    path = f"{requests_path('raw', 1, snapshot['id'])}/{request['id']}/recheck"

    for content in (b" ", b"\t\n ", b"{bad json", b"{}", b'{"a": 1}', b"null"):
        response = client.post(path, content=content)
        assert response.status_code == 422, content
        assert response.json()["error"] == "validation_error"
    for params in ({"x": "1"}, {"x": ""}):
        response = client.post(path, params=params)
        assert response.status_code == 422, params
        assert response.json()["error"] == "validation_error"

    # Nothing changed; a clean recheck still succeeds.
    clean = client.post(path)
    assert clean.status_code == 200, clean.text
    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert listed[0]["status"] == "pending"


def test_recheck_404_takes_precedence_over_body_and_query(client: TestClient) -> None:
    # An unknown request with a body/query is still a 404.
    response = recheck(client, 1, 999, content=b"garbage", params={"x": "1"})
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    assert recheck(client, snapshot["id"], 999, content=b"garbage").status_code == 404


def test_recheck_wrong_method_is_405(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    for method in ("GET", "PUT", "PATCH", "DELETE"):
        response = recheck(client, snapshot["id"], request["id"], method=method)
        assert response.status_code == 405, method


def test_recheck_rejections_expose_only_error_and_detail(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    for response in (
        recheck(client, snapshot["id"], 999),
        recheck(client, snapshot["id"], request["id"], content=b"{}"),
    ):
        assert set(response.json()) == {"error", "detail"}
        assert "SELECT" not in response.text and "Traceback" not in response.text


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


def _run(db_path: Path, script: str, stdin: str = "") -> str:
    env = os.environ.copy()
    env["DATA_LINEAGE_DB"] = str(db_path)
    result = subprocess.run(
        [sys.executable, "-c", script],
        input=stdin,
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


CREATE_PENDING_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response, code=(200, 201)):
    assert response.status_code in code, response.text

ok(client.post("/datasets", json={"name": "raw"}))
ok(client.post(
    "/datasets/raw/versions",
    json={"fields": [{"name": "order_id", "type": "string", "nullable": True}]},
))
ok(client.post("/datasets", json={"name": "dm"}))
ok(client.post(
    "/datasets/dm/versions",
    json={"fields": [{"name": "id", "type": "string", "nullable": True}]},
))
ok(client.post(
    "/datasets/raw/versions/1/retention-policies",
    json={"retention_days": 7},
))
snapshot = client.post(
    "/datasets/raw/versions/1/snapshots", json={"rows": [{"order_id": "a"}]}
)
ok(snapshot)
request = client.post(
    f"/datasets/raw/versions/1/snapshots/{snapshot.json()['id']}/deletion-requests",
    json={"reason": "gdpr"},
)
ok(request)
print(snapshot.json()["id"], request.json()["id"])
"""

RECHECK_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
snapshot_id, request_id = json.loads(input())
# Register a new downstream, then recheck.
r = client.post(
    "/datasets/dm/versions/1/lineage",
    json={
        "target_dataset": "dm",
        "target_version": 1,
        "target_field": "id",
        "source_dataset": "raw",
        "source_version": 1,
        "source_field": "order_id",
    },
)
assert r.status_code == 201, r.text
path = (
    f"/datasets/raw/versions/1/snapshots/{snapshot_id}/deletion-requests/"
    f"{request_id}/recheck"
)
response = client.post(path)
assert response.status_code == 200, response.text
body = response.json()
assert body["status"] == "blocked"
assert body["impacted"] == [{"dataset": "dm", "version": 1, "field": "id"}]
print("rechecked")
"""

VERIFY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
snapshot_id, request_id = json.loads(input())
path = f"/datasets/raw/versions/1/snapshots/{snapshot_id}/deletion-requests"
items = client.get(path).json()
assert len(items) == 1
assert items[0]["id"] == request_id
assert items[0]["status"] == "blocked"
assert items[0]["impacted"] == [{"dataset": "dm", "version": 1, "field": "id"}]
print("persisted")
"""


def test_rechecked_state_survives_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "recheck-lineage.db"

    out = _run(db_path, CREATE_PENDING_SCRIPT)
    snapshot_id, request_id = (int(part) for part in out.split())
    payload = json.dumps([snapshot_id, request_id])

    assert _run(db_path, RECHECK_SCRIPT, stdin=payload) == "rechecked"
    assert _run(db_path, VERIFY_SCRIPT, stdin=payload) == "persisted"


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_rechecks_have_a_single_winner(client: TestClient) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    path = f"{requests_path('raw', 1, snapshot['id'])}/{request['id']}/recheck"

    barrier = threading.Barrier(2)
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        status = thread_client.post(path).status_code
        with statuses_lock:
            statuses.append(status)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [200, 409], statuses
    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert len(listed) == 1
    assert listed[0]["status"] == "blocked"
    assert listed[0]["impacted"] == [
        {"dataset": "dm", "version": 1, "field": "id"}
    ]


def test_concurrent_recheck_and_confirm_have_a_single_winner(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    recheck_path = (
        f"{requests_path('raw', 1, snapshot['id'])}/{request['id']}/recheck"
    )
    confirm_path = (
        f"{requests_path('raw', 1, snapshot['id'])}/{request['id']}/confirm"
    )

    results: list[tuple[str, int]] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def recheck_worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        with results_lock:
            results.append(("recheck", thread_client.post(recheck_path).status_code))

    def confirm_worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        with results_lock:
            results.append(("confirm", thread_client.post(confirm_path).status_code))

    threads = [
        threading.Thread(target=recheck_worker),
        threading.Thread(target=confirm_worker),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    outcomes = dict(results)
    assert sorted(outcomes.values()) == [200, 409], outcomes
    # Exactly one terminal action: the request is either confirmed (snapshot
    # deleted) or still pending with its snapshot intact.
    snapshot_status = client.get(
        f"/datasets/raw/versions/1/snapshots/{snapshot['id']}"
    ).status_code
    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    if outcomes["confirm"] == 200:
        assert outcomes["recheck"] == 409
        assert snapshot_status == 404
        assert listed[0]["status"] == "confirmed"
    else:
        assert outcomes["recheck"] == 200
        assert snapshot_status == 200
        assert listed[0]["status"] == "pending"
        assert listed[0]["impacted"] == []


def test_recheck_against_uncommitted_writer_is_409_and_writes_nothing(
    client: TestClient,
) -> None:
    # A writer in another connection/process already holding the database
    # write lock (the cross-process guard used by the sweep and the confirm)
    # makes the recheck fail fast with a 409 and leave every column intact.
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])

    db_path = os.environ["DATA_LINEAGE_DB"]
    holder = sqlite3.connect(db_path, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        victim = sqlite3.connect(db_path, timeout=0)
        victim.row_factory = sqlite3.Row
        try:
            with pytest.raises(api_errors.ConflictError):
                repository.recheck_snapshot_deletion_request(
                    victim, "raw", 1, snapshot["id"], request["id"]
                )
        finally:
            victim.close()
    finally:
        holder.rollback()
        holder.close()

    # Once the lock is released the recheck succeeds; the failed attempt
    # wrote nothing.
    assert recheck(client, snapshot["id"], request["id"]).status_code == 200
    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert listed[0]["status"] == "pending"
    assert listed[0]["impacted"] == []


def test_recheck_lock_also_rejects_a_concurrent_sweep(client: TestClient) -> None:
    # The symmetric contention: a held recheck write lock makes a concurrent
    # batch retention sweep of the same version lose with a 409.
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    sweep_snapshot = make_snapshot(client, rows=[{"id": 1}])
    recheck_snapshot = make_snapshot(client, rows=[{"id": 2}])
    request = create_request(client, recheck_snapshot["id"])
    backdate_snapshot(sweep_snapshot["id"], days=1)

    db_path = os.environ["DATA_LINEAGE_DB"]
    holder = sqlite3.connect(db_path, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        victim = sqlite3.connect(db_path, timeout=0)
        victim.row_factory = sqlite3.Row
        try:
            with pytest.raises(api_errors.ConflictError):
                repository.sweep_snapshot_retention(
                    victim, "raw", 1, b'{"reason": "due"}'
                )
        finally:
            victim.close()
    finally:
        holder.rollback()
        holder.close()

    # The losing sweep created no request; a clean sweep afterwards works.
    assert (
        client.get(requests_path("raw", 1, sweep_snapshot["id"])).json() == []
    )
    sweep = client.post(
        "/datasets/raw/versions/1/snapshots/retention-sweep",
        json={"reason": "due"},
    )
    assert sweep.status_code == 200, sweep.text
    assert sweep.json()["counts"]["created_count"] == 1
    # The recheck request itself was untouched by either attempt.
    listed = client.get(requests_path("raw", 1, recheck_snapshot["id"])).json()
    assert listed[0]["id"] == request["id"]
    assert listed[0]["status"] == "pending"
