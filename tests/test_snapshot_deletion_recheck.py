"""Tests for the snapshot deletion-request recheck endpoint.

The impacted baseline of a deletion request is computed once at creation; the
recheck segment recomputes the direct and indirect downstream set against the
current lineage graph and rewrites ``impacted`` and ``status`` while an open
request is still pending/blocked.
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

from fastapi.testclient import TestClient

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
            "target_version": target_version,
            "target_field": target_field,
            "source_dataset": source_dataset,
            "source_version": source_version,
            "source_field": source_field,
        },
    )
    assert response.status_code == 201, response.text


def delete_link(
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


def recheck_path(
    dataset: str, version: int, snapshot_id: int, request_id: int
) -> str:
    return f"{requests_path(dataset, version, snapshot_id)}/{request_id}/recheck"


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
    dataset: str = "raw",
    version: int = 1,
    **kwargs,
):
    return client.post(
        recheck_path(dataset, version, snapshot_id, request_id), **kwargs
    )


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
# Recomputation: blocked <-> pending
# --------------------------------------------------------------------------- #


def test_recheck_added_downstream_moves_pending_to_blocked(client: TestClient) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    assert request["status"] == "pending"
    assert request["impacted"] == []

    # Lineage gains a downstream after the request was opened.
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))

    response = recheck(client, snapshot["id"], request["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "blocked"
    assert body["impacted"] == [{"dataset": "dm", "version": 1, "field": "id"}]
    # Only status and impacted move; every other field is carried over.
    for key in ("id", "snapshot_id", "policy_id", "reason", "created_at"):
        assert body[key] == request[key]
    assert set(body) == {
        "id",
        "snapshot_id",
        "policy_id",
        "reason",
        "status",
        "impacted",
        "created_at",
    }


def test_recheck_removed_downstream_moves_blocked_to_pending(client: TestClient) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    assert request["status"] == "blocked"

    # The dependency disappears from the lineage graph.
    delete_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))

    response = recheck(client, snapshot["id"], request["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["impacted"] == []
    for key in ("id", "snapshot_id", "policy_id", "reason", "created_at"):
        assert body[key] == request[key]


def test_recheck_blocked_stays_blocked_when_graph_unchanged(client: TestClient) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])

    response = recheck(client, snapshot["id"], request["id"])
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "blocked"
    assert body["impacted"] == request["impacted"]


def test_recheck_pending_stays_pending_when_graph_unchanged(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])

    response = recheck(client, snapshot["id"], request["id"])
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["impacted"] == []


def test_recheck_indirect_downstream_is_deduplicated_sorted_and_cycle_safe(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    # A direct downstream exists at creation; the request opens blocked.
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])

    # An indirect downstream and a cycle are registered only afterwards.
    make_dataset(client, "bi", ["report_id"])
    add_link(client, ("dm", 1, "id"), ("bi", 1, "report_id"))
    add_link(client, ("bi", 1, "report_id"), ("dm", 1, "id"))

    body = recheck(client, snapshot["id"], request["id"]).json()
    assert body["status"] == "blocked"
    assert body["impacted"] == [
        {"dataset": "bi", "version": 1, "field": "report_id"},
        {"dataset": "dm", "version": 1, "field": "id"},
    ]
    # The seeded version's own start field never appears in the result.
    assert all(
        not (item["dataset"] == "raw" and item["field"] == "order_id")
        for item in body["impacted"]
    )


def test_recheck_result_is_visible_in_the_list(client: TestClient) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])

    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    assert recheck(client, snapshot["id"], request["id"]).status_code == 200

    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert [item["id"] for item in listed] == [request["id"]]
    assert listed[0]["status"] == "blocked"
    assert listed[0]["impacted"] == [
        {"dataset": "dm", "version": 1, "field": "id"}
    ]
    assert "confirmed_at" not in listed[0]


# --------------------------------------------------------------------------- #
# No side effects
# --------------------------------------------------------------------------- #


def test_recheck_does_not_delete_snapshot_or_create_request(client: TestClient) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))

    assert recheck(client, snapshot["id"], request["id"]).status_code == 200

    # The snapshot is untouched and no new request was opened.
    assert (
        client.get(f"/datasets/raw/versions/1/snapshots/{snapshot['id']}").status_code
        == 200
    )
    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert [item["id"] for item in listed] == [request["id"]]


def test_recheck_does_not_change_lineage_or_policy(client: TestClient) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    policy = make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])

    delete_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))
    assert recheck(client, snapshot["id"], request["id"]).status_code == 200

    # Lineage is exactly the post-deletion graph and the policy is unchanged.
    lineage = client.get("/datasets/dm/versions/1/lineage").json()
    assert lineage["fields"][0]["sources"] == []
    policy_read = client.get("/datasets/raw/versions/1")  # version still readable
    assert policy_read.status_code == 200
    policies = client.post(
        "/datasets/raw/versions/1/retention-policies",
        json={"retention_days": 99},
    )
    assert policies.status_code == 409  # the one policy is still in place
    assert policy["retention_days"] == 7


# --------------------------------------------------------------------------- #
# Confirmed requests
# --------------------------------------------------------------------------- #


def test_recheck_confirmed_request_is_409_even_after_snapshot_deleted(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    confirm_path = f"{requests_path('raw', 1, snapshot['id'])}/{request['id']}/confirm"
    assert client.post(confirm_path).status_code == 200
    # The snapshot is gone, but the confirmed request record remains.
    assert (
        client.get(f"/datasets/raw/versions/1/snapshots/{snapshot['id']}").status_code
        == 404
    )

    response = recheck(client, snapshot["id"], request["id"])
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"

    # No field changed; the list still shows the confirmed request as stored.
    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert len(listed) == 1
    assert listed[0]["status"] == "confirmed"
    assert listed[0]["impacted"] == []


# --------------------------------------------------------------------------- #
# Resolution and request shape
# --------------------------------------------------------------------------- #


def test_recheck_accepts_post_only(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    assert (
        client.get(recheck_path("raw", 1, snapshot["id"], request["id"])).status_code
        == 405
    )


def test_recheck_unknown_dataset_version_or_request_is_404(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])

    assert recheck(client, snapshot["id"], request["id"], dataset="ghost").status_code == 404
    assert (
        recheck(client, snapshot["id"], request["id"], version=9).status_code == 404
    )
    assert recheck(client, snapshot["id"], 999).status_code == 404


def test_recheck_404_precedes_body_and_query_shape_checks(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    # Unknown dataset with body bytes and a query parameter is still a 404.
    response = client.post(
        recheck_path("ghost", 1, 1, 1) + "?x=1",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_recheck_request_under_another_snapshot_is_422(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    first = make_snapshot(client, rows=[{"id": 1}])
    second = make_snapshot(client, rows=[{"id": 2}])
    request = create_request(client, first["id"])

    # The request exists but the path names a different snapshot of the same
    # version: a 422 that writes nothing.
    response = recheck(client, second["id"], request["id"])
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"

    # The real request is unchanged.
    listed = client.get(requests_path("raw", 1, first["id"])).json()
    assert listed[0]["status"] == "pending"
    assert client.get(requests_path("raw", 1, second["id"])).json() == []


def test_recheck_rejects_any_body_bytes_and_query_parameters(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    url = recheck_path("raw", 1, snapshot["id"], request["id"])

    bodies = [b" ", b"\n\t ", b"{", b"{bad json", b"{}", b"null", b"[]"]
    for raw in bodies:
        response = client.post(
            url, content=raw, headers={"content-type": "application/json"}
        )
        assert response.status_code == 422, raw
        assert response.json()["error"] == "validation_error"

    assert client.post(url, params={"x": "1"}).status_code == 422
    assert client.post(url + "?x=").status_code == 422

    # Nothing was written: the request is still its original pending record.
    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert listed[0]["status"] == "pending"
    assert listed[0]["impacted"] == []


def test_recheck_rejection_error_shape_never_leaks_internals(client: TestClient) -> None:
    response = client.post(recheck_path("ghost", 1, 1, 1))
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    make_dataset(client, "raw", ["id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    bad = client.post(
        recheck_path("raw", 1, snapshot["id"], request["id"]),
        content=b"{}",
        headers={"content-type": "application/json"},
    )
    assert set(bad.json()) == {"error", "detail"}


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_rechecks_have_a_single_winner(client: TestClient) -> None:
    make_dataset(client, "raw", ["order_id"])
    make_policy(client)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    # Both rechecks observe a freshly added downstream.
    make_dataset(client, "dm", ["id"])
    add_link(client, ("raw", 1, "order_id"), ("dm", 1, "id"))

    url = recheck_path("raw", 1, snapshot["id"], request["id"])
    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        statuses.append(thread_client.post(url).status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [200, 409], statuses
    # The winner's recomputation is the only committed state.
    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert listed[0]["status"] == "blocked"
    assert listed[0]["impacted"] == [
        {"dataset": "dm", "version": 1, "field": "id"}
    ]


def test_concurrent_recheck_and_confirm_have_a_single_winner(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    # The request is confirmable by age; the recheck recomputes it as pending.
    backdate_snapshot(snapshot["id"], days=1)

    r_url = recheck_path("raw", 1, snapshot["id"], request["id"])
    c_url = (
        f"{requests_path('raw', 1, snapshot['id'])}/{request['id']}/confirm"
    )
    barrier = threading.Barrier(2)
    results: dict[str, int] = {}

    def recheck_worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        results["recheck"] = thread_client.post(r_url).status_code

    def confirm_worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        results["confirm"] = thread_client.post(c_url).status_code

    threads = [
        threading.Thread(target=recheck_worker),
        threading.Thread(target=confirm_worker),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results.values()) == [200, 409], results
    # State is internally consistent with exactly one winner.
    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    snapshot_status = client.get(
        f"/datasets/raw/versions/1/snapshots/{snapshot['id']}"
    ).status_code
    if results["confirm"] == 200:
        assert results["recheck"] == 409
        assert listed[0]["status"] == "confirmed"
        assert snapshot_status == 404
    else:
        assert results["recheck"] == 200
        assert listed[0]["status"] == "pending"
        assert snapshot_status == 200


def test_concurrent_recheck_and_sweep_have_a_single_winner(client: TestClient) -> None:
    make_dataset(client, "raw", ["id"])
    make_policy(client, retention_days=0)
    snapshot = make_snapshot(client)
    request = create_request(client, snapshot["id"])
    backdate_snapshot(snapshot["id"], days=1)

    r_url = recheck_path("raw", 1, snapshot["id"], request["id"])
    sweep_url = "/datasets/raw/versions/1/snapshots/retention-sweep"
    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def recheck_worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        statuses.append(thread_client.post(r_url).status_code)

    def sweep_worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        statuses.append(
            thread_client.post(sweep_url, json={"reason": "sweep"}).status_code
        )

    threads = [
        threading.Thread(target=recheck_worker),
        threading.Thread(target=sweep_worker),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # The sweep is the single-writer when it wins (200); the recheck either
    # wins or loses, but the two write transactions never both commit.
    assert sorted(statuses) == [200, 409], statuses
    listed = client.get(requests_path("raw", 1, snapshot["id"])).json()
    assert [item["id"] for item in listed] == [request["id"]]


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


OPEN_PENDING_SCRIPT = """
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
ok(client.post(
    "/datasets/raw/versions/1/retention-policies",
    json={"retention_days": 7},
))
snapshot = client.post(
    "/datasets/raw/versions/1/snapshots", json={"rows": [{"id": 1}]}
)
ok(snapshot)
request = client.post(
    f"/datasets/raw/versions/1/snapshots/{snapshot.json()['id']}/deletion-requests",
    json={"reason": "gdpr request"},
)
ok(request)
assert request.json()["status"] == "pending"
print(snapshot.json()["id"], request.json()["id"])
"""

RECHECK_BLOCKED_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
snapshot_id, request_id = json.loads(input())

def ok(response, code=(200, 201)):
    assert response.status_code in code, response.text

ok(client.post("/datasets", json={"name": "dm"}))
ok(client.post(
    "/datasets/dm/versions",
    json={"fields": [{"name": "id", "type": "string", "nullable": True}]},
))
ok(client.post(
    "/datasets/dm/versions/1/lineage",
    json={
        "target_dataset": "dm",
        "target_version": 1,
        "target_field": "id",
        "source_dataset": "raw",
        "source_version": 1,
        "source_field": "order_id",
    },
))
response = client.post(
    f"/datasets/raw/versions/1/snapshots/{snapshot_id}/deletion-requests/"
    f"{request_id}/recheck"
)
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
listed = client.get(
    f"/datasets/raw/versions/1/snapshots/{snapshot_id}/deletion-requests"
)
assert listed.status_code == 200, listed.text
items = listed.json()
assert [item["id"] for item in items] == [request_id]
assert items[0]["status"] == "blocked"
assert items[0]["impacted"] == [{"dataset": "dm", "version": 1, "field": "id"}]
assert items[0]["reason"] == "gdpr request"
print("persisted")
"""


def test_recomputed_status_and_impacted_survive_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "recheck-lineage.db"

    out = _run(db_path, OPEN_PENDING_SCRIPT)
    snapshot_id, request_id = out.split()

    payload = json.dumps([int(snapshot_id), int(request_id)])
    assert _run(db_path, RECHECK_BLOCKED_SCRIPT, stdin=payload) == "rechecked"
    assert _run(db_path, VERIFY_SCRIPT, stdin=payload) == "persisted"
