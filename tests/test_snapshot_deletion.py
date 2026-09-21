"""Tests for schema-version retention policies and lineage-aware snapshot deletion."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
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


def make_snapshot(
    client: TestClient, rows: list | None = None, dataset: str = "raw", version: int = 1
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots",
        json={"rows": rows if rows is not None else []},
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_policy(
    client: TestClient, retention_days: int, dataset: str = "raw", version: int = 1
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/retention-policies",
        json={"retention_days": retention_days},
    )
    assert response.status_code == 201, response.text
    return response.json()


def ref(dataset: str, version: int, field: str) -> dict:
    return {"dataset": dataset, "version": version, "field": field}


def requests_path(dataset: str = "raw", version: int = 1, snapshot_id: int | None = None) -> str:
    return f"/datasets/{dataset}/versions/{version}/snapshots/{snapshot_id}/deletion-requests"


def backdate_snapshot(snapshot_id: int, days: int) -> None:
    """Rewrite a snapshot's created_at directly in the test database."""
    conn = sqlite3.connect(os.environ["DATA_LINEAGE_DB"])
    try:
        created_at = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).isoformat()
        conn.execute(
            "UPDATE snapshots SET created_at = ? WHERE id = ?",
            (created_at, snapshot_id),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Retention policies
# --------------------------------------------------------------------------- #


def test_create_retention_policy_returns_record(client: TestClient) -> None:
    make_dataset(client, "raw", ["f"])
    body = make_policy(client, 30)

    assert set(body) == {"id", "dataset", "version", "retention_days", "created_at"}
    assert isinstance(body["id"], int)
    assert body["dataset"] == "raw"
    assert body["version"] == 1
    assert body["retention_days"] == 30
    datetime.fromisoformat(body["created_at"])


def test_retention_zero_days_is_allowed(client: TestClient) -> None:
    make_dataset(client, "raw", ["f"])
    assert make_policy(client, 0)["retention_days"] == 0


def test_retention_policy_validation_errors(client: TestClient) -> None:
    make_dataset(client, "raw", ["f"])
    path = "/datasets/raw/versions/1/retention-policies"
    for payload in (
        {"retention_days": -1},
        {"retention_days": "7"},
        {"retention_days": 3.5},
        {"retention_days": True},
        {"retention_days": None},
        {},
        {"retention_days": 7, "extra": 1},
    ):
        response = client.post(path, json=payload)
        assert response.status_code == 422, payload
        assert response.json()["error"] == "validation_error"

    malformed = client.post(
        path, content="{not json", headers={"content-type": "application/json"}
    )
    assert malformed.status_code == 422


def test_retention_policy_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    path = "/datasets/ghost/versions/1/retention-policies"
    assert client.post(path, json={"retention_days": 7}).status_code == 404
    make_dataset(client, "raw", ["f"])
    assert client.post(
        "/datasets/raw/versions/9/retention-policies", json={"retention_days": 7}
    ).status_code == 404


def test_only_one_retention_policy_per_version(client: TestClient) -> None:
    make_dataset(client, "raw", ["f"])
    make_policy(client, 7)
    second = client.post(
        "/datasets/raw/versions/1/retention-policies",
        json={"retention_days": 14},
    )
    assert second.status_code == 409
    assert second.json()["error"] == "conflict"

    # A policy on another version of the same dataset is fine.
    response = client.post(
        "/datasets/raw/versions",
        json={"fields": [{"name": "f", "type": "string", "nullable": True}]},
    )
    assert response.status_code == 201
    other = client.post(
        "/datasets/raw/versions/2/retention-policies",
        json={"retention_days": 1},
    )
    assert other.status_code == 201


# --------------------------------------------------------------------------- #
# Deletion request creation
# --------------------------------------------------------------------------- #


def test_deletion_request_without_downstream_is_pending(client: TestClient) -> None:
    make_dataset(client, "raw", ["f1", "f2"])
    make_policy(client, 7)
    snapshot = make_snapshot(client, [{"f1": "a"}])

    response = client.post(requests_path(snapshot_id=snapshot["id"]), json={"reason": "gdpr"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {
        "id", "snapshot_id", "policy_id", "reason", "status", "impacted", "created_at"
    }
    assert body["snapshot_id"] == snapshot["id"]
    assert isinstance(body["policy_id"], int)
    assert body["reason"] == "gdpr"
    assert body["status"] == "pending"
    assert body["impacted"] == []
    datetime.fromisoformat(body["created_at"])


def test_deletion_request_with_direct_downstream_is_blocked(client: TestClient) -> None:
    make_dataset(client, "raw", ["f"])
    make_dataset(client, "mid", ["m"])
    add_link(client, ("raw", 1, "f"), ("mid", 1, "m"))
    make_policy(client, 7)
    snapshot = make_snapshot(client)

    response = client.post(requests_path(snapshot_id=snapshot["id"]), json={"reason": "x"})
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "blocked"
    assert body["impacted"] == [ref("mid", 1, "m")]


def test_blocked_impact_covers_indirect_downstream_dedup_and_sort(
    client: TestClient,
) -> None:
    # raw.f -> mid.a -> mart.z
    #        -> mid.b -> mart.z   (diamond: mart.z reachable on two paths)
    make_dataset(client, "raw", ["f", "g"])
    make_dataset(client, "mid", ["a", "b"])
    make_dataset(client, "mart", ["z"])
    add_link(client, ("raw", 1, "f"), ("mid", 1, "a"))
    add_link(client, ("raw", 1, "f"), ("mid", 1, "b"))
    add_link(client, ("mid", 1, "a"), ("mart", 1, "z"))
    add_link(client, ("mid", 1, "b"), ("mart", 1, "z"))
    make_policy(client, 7)
    snapshot = make_snapshot(client)

    body = client.post(
        requests_path(snapshot_id=snapshot["id"]), json={"reason": "x"}
    ).json()
    assert body["status"] == "blocked"
    # Deduplicated (mart.z once) and sorted by dataset, version, field:
    # "mart" < "mid".
    assert body["impacted"] == [
        ref("mart", 1, "z"),
        ref("mid", 1, "a"),
        ref("mid", 1, "b"),
    ]


def test_impact_is_the_union_over_all_version_fields(client: TestClient) -> None:
    make_dataset(client, "raw", ["f1", "f2"])
    make_dataset(client, "mid", ["a", "b"])
    add_link(client, ("raw", 1, "f1"), ("mid", 1, "a"))
    add_link(client, ("raw", 1, "f2"), ("mid", 1, "b"))
    make_policy(client, 7)
    snapshot = make_snapshot(client)

    body = client.post(
        requests_path(snapshot_id=snapshot["id"]), json={"reason": "x"}
    ).json()
    assert body["impacted"] == [ref("mid", 1, "a"), ref("mid", 1, "b")]


def test_deletion_request_reason_validation(client: TestClient) -> None:
    make_dataset(client, "raw", ["f"])
    make_policy(client, 7)
    snapshot = make_snapshot(client)
    path = requests_path(snapshot_id=snapshot["id"])

    for payload in (
        {"reason": "   "},
        {"reason": ""},
        {"reason": 123},
        {"reason": True},
        {"reason": None},
        {},
        {"reason": "ok", "extra": 1},
    ):
        response = client.post(path, json=payload)
        assert response.status_code == 422, payload
        assert response.json()["error"] == "validation_error"

    malformed = client.post(
        path, content="{", headers={"content-type": "application/json"}
    )
    assert malformed.status_code == 422
    # Nothing was written.
    assert client.get(path).json() == []


def test_deletion_request_unknown_resources_are_404(client: TestClient) -> None:
    make_dataset(client, "raw", ["f"])
    make_policy(client, 7)
    snapshot = make_snapshot(client)

    assert client.post(
        requests_path(dataset="ghost", snapshot_id=1), json={"reason": "x"}
    ).status_code == 404
    assert client.post(
        "/datasets/raw/versions/9/snapshots/1/deletion-requests",
        json={"reason": "x"},
    ).status_code == 404
    assert client.post(
        requests_path(snapshot_id=snapshot["id"] + 999), json={"reason": "x"}
    ).status_code == 404

    # A version without a policy behaves as if the resource were unknown.
    response = client.post(
        "/datasets/raw/versions",
        json={"fields": [{"name": "f", "type": "string", "nullable": True}]},
    )
    assert response.status_code == 201
    other = make_snapshot(client, dataset="raw", version=2)
    assert client.post(
        requests_path(dataset="raw", version=2, snapshot_id=other["id"]),
        json={"reason": "x"},
    ).status_code == 404


def test_deletion_request_for_foreign_snapshot_is_404(client: TestClient) -> None:
    make_dataset(client, "raw", ["f"])
    make_dataset(client, "other", ["f"])
    make_policy(client, 7, dataset="raw")
    make_policy(client, 7, dataset="other")
    foreign = make_snapshot(client, dataset="other")

    # Naming another version's snapshot through the raw path is a 404, not a
    # request bound to the wrong version.
    response = client.post(
        requests_path(dataset="raw", snapshot_id=foreign["id"]),
        json={"reason": "x"},
    )
    assert response.status_code == 404
    assert client.get(requests_path(dataset="raw", snapshot_id=foreign["id"])).status_code == 404


def test_duplicate_open_request_conflicts(client: TestClient) -> None:
    make_dataset(client, "raw", ["f"])
    make_policy(client, 7)
    snapshot = make_snapshot(client)
    path = requests_path(snapshot_id=snapshot["id"])

    first = client.post(path, json={"reason": "one"})
    assert first.status_code == 201
    duplicate = client.post(path, json={"reason": "two"})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"] == "conflict"
    # The duplicate did not overwrite or add a record.
    listed = client.get(path).json()
    assert [item["reason"] for item in listed] == ["one"]


def test_duplicate_blocked_request_conflicts(client: TestClient) -> None:
    make_dataset(client, "raw", ["f"])
    make_dataset(client, "mid", ["m"])
    add_link(client, ("raw", 1, "f"), ("mid", 1, "m"))
    make_policy(client, 7)
    snapshot = make_snapshot(client)
    path = requests_path(snapshot_id=snapshot["id"])

    assert client.post(path, json={"reason": "one"}).json()["status"] == "blocked"
    assert client.post(path, json={"reason": "two"}).status_code == 409


def test_requests_for_different_snapshots_are_independent(client: TestClient) -> None:
    make_dataset(client, "raw", ["f"])
    make_policy(client, 7)
    first = make_snapshot(client)
    second = make_snapshot(client)

    r1 = client.post(
        requests_path(snapshot_id=first["id"]), json={"reason": "a"})
    r2 = client.post(
        requests_path(snapshot_id=second["id"]), json={"reason": "b"})
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]
    assert r1.json()["policy_id"] == r2.json()["policy_id"]


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


def test_list_deletion_requests_returns_the_request_for_a_snapshot(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["f"])
    make_policy(client, 7)
    snapshot = make_snapshot(client)
    path = requests_path(snapshot_id=snapshot["id"])

    assert client.get(path).json() == []
    created = client.post(path, json={"reason": "listed"}).json()
    listed = client.get(path)
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0] == created
    assert set(rows[0]) == {
        "id", "snapshot_id", "policy_id", "reason", "status", "impacted",
        "created_at",
    }


def test_list_requests_are_scoped_per_snapshot(client: TestClient) -> None:
    make_dataset(client, "raw", ["f"])
    make_policy(client, 7)
    first = make_snapshot(client)
    second = make_snapshot(client)
    client.post(
        requests_path(snapshot_id=first["id"]), json={"reason": "a"}
    )
    client.post(
        requests_path(snapshot_id=second["id"]), json={"reason": "b"}
    )

    first_rows = client.get(requests_path(snapshot_id=first["id"])).json()
    second_rows = client.get(requests_path(snapshot_id=second["id"])).json()
    assert [r["reason"] for r in first_rows] == ["a"]
    assert [r["reason"] for r in second_rows] == ["b"]
    # Ids are globally increasing, hence ascending within either list.
    assert first_rows[0]["id"] < second_rows[0]["id"]


def test_list_without_policy_is_empty_but_unknown_snapshot_is_404(
    client: TestClient,
) -> None:
    make_dataset(client, "raw", ["f"])
    snapshot = make_snapshot(client)
    # Listing does not require a policy; a policy-less version just has none.
    no_policy = client.get(requests_path(snapshot_id=snapshot["id"]))
    assert no_policy.status_code == 200
    assert no_policy.json() == []
    make_policy(client, 7)
    assert client.get(requests_path(snapshot_id=snapshot["id"])).status_code == 200
    assert client.get(requests_path(snapshot_id=999)).status_code == 404


# --------------------------------------------------------------------------- #
# Confirmation and atomic deletion
# --------------------------------------------------------------------------- #


def test_confirm_pending_with_zero_retention_deletes_snapshot(client: TestClient) -> None:
    make_dataset(client, "raw", ["f1", "f2"])
    make_policy(client, 0)
    snapshot = make_snapshot(client, [{"f1": "keep?"}])
    path = requests_path(snapshot_id=snapshot["id"])
    request = client.post(path, json={"reason": "expired"}).json()
    assert request["status"] == "pending"

    response = client.post(f"{path}/{request['id']}/confirm")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "id", "snapshot_id", "policy_id", "reason", "status", "impacted",
        "created_at", "confirmed_at",
    }
    assert body["status"] == "confirmed"
    assert body["impacted"] == []
    assert isinstance(body["confirmed_at"], str)
    datetime.fromisoformat(body["confirmed_at"])
    assert body["id"] == request["id"]

    # Snapshot read, list, at and diff all stop returning it.
    snapshot_path = f"/datasets/raw/versions/1/snapshots"
    assert client.get(f"{snapshot_path}/{snapshot['id']}").status_code == 404
    assert client.get(snapshot_path).json() == []
    at = client.get(
        f"{snapshot_path}/at",
        params={"timestamp": "2099-01-01T00:00:00+00:00"},
    )
    assert at.status_code == 404
    diff = client.get(f"{snapshot_path}/{snapshot['id']}/diff/{snapshot['id']}")
    assert diff.status_code == 404


def test_confirm_before_retention_age_is_409_and_keeps_snapshot(client: TestClient) -> None:
    make_dataset(client, "raw", ["f"])
    make_policy(client, 30)
    snapshot = make_snapshot(client, [{"f": "young"}])
    path = requests_path(snapshot_id=snapshot["id"])
    request = client.post(path, json={"reason": "early"}).json()

    response = client.post(f"{path}/{request['id']}/confirm")
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"

    # Nothing was deleted or mutated.
    read = client.get(f"/datasets/raw/versions/1/snapshots/{snapshot['id']}")
    assert read.status_code == 200
    assert read.json()["rows"] == [{"f": "young"}]
    listed = client.get(path).json()
    assert listed[0]["status"] == "pending"
    assert "confirmed_at" not in listed[0]


def test_confirm_succeeds_once_snapshot_reaches_retention_age(client: TestClient) -> None:
    make_dataset(client, "raw", ["f"])
    make_policy(client, 7)
    snapshot = make_snapshot(client)
    path = requests_path(snapshot_id=snapshot["id"])
    request = client.post(path, json={"reason": "aged"}).json()

    # Too young at first.
    assert client.post(f"{path}/{request['id']}/confirm").status_code == 409

    # Backdate the snapshot beyond the retention window, then confirm succeeds.
    backdate_snapshot(snapshot["id"], days=8)
    response = client.post(f"{path}/{request['id']}/confirm")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    assert client.get(
        f"/datasets/raw/versions/1/snapshots/{snapshot['id']}"
    ).status_code == 404


def test_confirm_blocked_request_is_409_and_keeps_snapshot(client: TestClient) -> None:
    make_dataset(client, "raw", ["f"])
    make_dataset(client, "mid", ["m"])
    add_link(client, ("raw", 1, "f"), ("mid", 1, "m"))
    make_policy(client, 0)
    snapshot = make_snapshot(client)
    path = requests_path(snapshot_id=snapshot["id"])
    request = client.post(path, json={"reason": "still used"}).json()
    assert request["status"] == "blocked"

    response = client.post(f"{path}/{request['id']}/confirm")
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    assert client.get(
        f"/datasets/raw/versions/1/snapshots/{snapshot['id']}"
    ).status_code == 200


def test_confirm_unknown_request_or_wrong_snapshot_is_404(client: TestClient) -> None:
    make_dataset(client, "raw", ["f"])
    make_policy(client, 0)
    snapshot = make_snapshot(client)
    other = make_snapshot(client)
    path = requests_path(snapshot_id=snapshot["id"])
    request = client.post(path, json={"reason": "x"}).json()

    assert client.post(f"{path}/9999/confirm").status_code == 404

    # A request of another snapshot cannot be confirmed through this path.
    wrong = client.post(
        f"/datasets/raw/versions/1/snapshots/{other['id']}/deletion-requests/"
        f"{request['id']}/confirm"
    )
    assert wrong.status_code == 404


def test_confirm_unknown_dataset_version_snapshot_is_404(client: TestClient) -> None:
    make_dataset(client, "raw", ["f"])
    make_policy(client, 0)
    snapshot = make_snapshot(client)
    request = client.post(
        requests_path(snapshot_id=snapshot["id"]), json={"reason": "x"}
    ).json()

    assert client.post(
        f"/datasets/ghost/versions/1/snapshots/{snapshot['id']}/deletion-requests/"
        f"{request['id']}/confirm"
    ).status_code == 404
    assert client.post(
        f"/datasets/raw/versions/9/snapshots/{snapshot['id']}/deletion-requests/"
        f"{request['id']}/confirm"
    ).status_code == 404
    assert client.post(
        f"/datasets/raw/versions/1/snapshots/999/deletion-requests/"
        f"{request['id']}/confirm"
    ).status_code == 404


def test_confirm_takes_no_body(client: TestClient) -> None:
    make_dataset(client, "raw", ["f"])
    make_policy(client, 0)
    snapshot = make_snapshot(client)
    request = client.post(
        requests_path(snapshot_id=snapshot["id"]), json={"reason": "x"}
    ).json()
    # A body (even an unusual one) is simply ignored; confirmation still works.
    response = client.post(
        f"/datasets/raw/versions/1/snapshots/{snapshot['id']}/deletion-requests/"
        f"{request['id']}/confirm",
        json={"unexpected": True},
    )
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response):
    assert response.status_code in (200, 201), response.text

ok(client.post("/datasets", json={"name": "raw"}))
ok(client.post("/datasets", json={"name": "mid"}))
ok(client.post(
    "/datasets/raw/versions",
    json={"fields": [{"name": "f", "type": "string", "nullable": True}]},
))
ok(client.post(
    "/datasets/mid/versions",
    json={"fields": [{"name": "m", "type": "string", "nullable": True}]},
))
ok(client.post(
    "/datasets/mid/versions/1/lineage",
    json={
        "target_dataset": "mid", "target_version": 1, "target_field": "m",
        "source_dataset": "raw", "source_version": 1, "source_field": "f",
    },
))
policy = client.post(
    "/datasets/raw/versions/1/retention-policies",
    json={"retention_days": 7},
)
assert policy.status_code == 201, policy.text
blocked_snapshot = client.post(
    "/datasets/raw/versions/1/snapshots", json={"rows": []})
assert blocked_snapshot.status_code == 201, blocked_snapshot.text
blocked = client.post(
    f"/datasets/raw/versions/1/snapshots/{blocked_snapshot.json()['id']}/deletion-requests",
    json={"reason": "blocked one"},
)
assert blocked.status_code == 201, blocked.text

ok(client.post("/datasets", json={"name": "clean"}))
ok(client.post(
    "/datasets/clean/versions",
    json={"fields": [{"name": "c", "type": "string", "nullable": True}]},
))
ok(client.post(
    "/datasets/clean/versions/1/retention-policies",
    json={"retention_days": 3},
))
pending_snapshot = client.post(
    "/datasets/clean/versions/1/snapshots", json={"rows": []})
assert pending_snapshot.status_code == 201
pending = client.post(
    f"/datasets/clean/versions/1/snapshots/{pending_snapshot.json()['id']}/deletion-requests",
    json={"reason": "pending one"},
)
assert pending.status_code == 201, pending.text
import json
print(json.dumps({
    "raw_snapshot": blocked_snapshot.json()["id"],
    "clean_snapshot": pending_snapshot.json()["id"],
    "blocked": blocked.json()["id"],
    "pending": pending.json()["id"],
}))
"""

VERIFY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
ids = json.loads(input())

# Policies survive and still enforce one-per-version.
dup = client.post(
    "/datasets/raw/versions/1/retention-policies",
    json={"retention_days": 99},
)
assert dup.status_code == 409, dup.text

raw_path = (
    f"/datasets/raw/versions/1/snapshots/{ids['raw_snapshot']}/deletion-requests"
)
blocked_list = client.get(raw_path)
assert blocked_list.status_code == 200, blocked_list.text
blocked_rows = blocked_list.json()
assert len(blocked_rows) == 1
row = blocked_rows[0]
assert row["id"] == ids["blocked"]
assert row["status"] == "blocked"
assert row["reason"] == "blocked one"
assert row["impacted"] == [{"dataset": "mid", "version": 1, "field": "m"}]
assert "confirmed_at" not in row

clean_path = (
    f"/datasets/clean/versions/1/snapshots/{ids['clean_snapshot']}/deletion-requests"
)
pending_rows = client.get(clean_path).json()
assert [r["id"] for r in pending_rows] == [ids["pending"]]
assert pending_rows[0]["status"] == "pending"
assert pending_rows[0]["impacted"] == []

# A young pending request still cannot be confirmed after the restart.
too_early = client.post(f"{clean_path}/{ids['pending']}/confirm")
assert too_early.status_code == 409, too_early.text
print("verified")
"""


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


def test_policies_and_requests_survive_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "deletion-lineage.db"
    created = _run(db_path, CREATE_SCRIPT)
    ids = json.loads(created)
    assert _run(db_path, VERIFY_SCRIPT, stdin=json.dumps(ids)) == "verified"
