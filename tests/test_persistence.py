"""Persistence tests: registered data must survive a process restart.

The creation and verification phases run in separate Python interpreters pointed
at the same SQLite file, which is what a real service restart looks like.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response):
    assert response.status_code in (200, 201), response.text

ok(client.post("/datasets", json={"name": "source_ds", "description": "src"}))
ok(client.post("/datasets", json={"name": "target_ds"}))
ok(client.post(
    "/datasets/source_ds/versions",
    json={"fields": [{"name": "sid", "type": "integer", "nullable": False}]},
))
ok(client.post(
    "/datasets/target_ds/versions",
    json={"fields": [
        {"name": "tid", "type": "integer", "nullable": False},
        {"name": "label", "type": "string", "nullable": True},
    ]},
))
ok(client.post(
    "/datasets/target_ds/versions/1/lineage",
    json={
        "target_dataset": "target_ds",
        "target_version": 1,
        "target_field": "tid",
        "source_dataset": "source_ds",
        "source_version": 1,
        "source_field": "sid",
    },
))
ok(client.post(
    "/datasets/target_ds/versions/1/quality-rules",
    json={"name": "tid present", "kind": "not_null", "params": {"field": "tid"}},
))
paused = client.post(
    "/datasets/target_ds/versions/1/quality-rules",
    json={"name": "tid range", "kind": "numeric_range",
          "params": {"field": "tid", "min": 0, "max": 99}},
)
assert paused.status_code == 201, paused.text
paused_id = paused.json()["id"]
ok(client.patch(
    f"/datasets/target_ds/versions/1/quality-rules/{paused_id}",
    json={"enabled": False},
))
ok(client.post(
    "/datasets/target_ds/versions/1/privacy-policies",
    json={
        "field": "tid",
        "classification": "restricted",
        "masking": "redact",
        "allowed_roles": ["auditor"],
    },
))
paused_policy = client.post(
    "/datasets/target_ds/versions/1/privacy-policies",
    json={
        "field": "label",
        "classification": "internal",
        "masking": "partial",
        "allowed_roles": [],
    },
)
assert paused_policy.status_code == 201, paused_policy.text
ok(client.patch(
    "/datasets/target_ds/versions/1/privacy-policies/"
    f"{paused_policy.json()['id']}",
    json={"enabled": False},
))
snapshot = client.post(
    "/datasets/target_ds/versions/1/snapshots",
    json={"rows": [
        {"tid": 1, "label": "a"},
        {"tid": 2, "label": "b", "nested": [1, 2, {"k": True}]},
    ]},
)
assert snapshot.status_code == 201, snapshot.text
print("created")
"""

VERIFY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

datasets = client.get("/datasets")
assert datasets.status_code == 200, datasets.text
by_name = {d["name"]: d for d in datasets.json()}
assert set(by_name) == {"source_ds", "target_ds"}
assert by_name["source_ds"]["description"] == "src"

version = client.get("/datasets/source_ds/versions/1")
assert version.status_code == 200, version.text
assert version.json()["version"] == 1
assert version.json()["fields"] == [
    {"name": "sid", "type": "integer", "nullable": False}
]

lineage = client.get("/datasets/target_ds/versions/1/lineage")
assert lineage.status_code == 200, lineage.text
body = lineage.json()
assert body["fields"] == [
    {
        "target_field": "label",
        "sources": [],
    },
    {
        "target_field": "tid",
        "sources": [
            {"dataset": "source_ds", "version": 1, "field": "sid"}
        ],
    },
]

rules = client.get("/datasets/target_ds/versions/1/quality-rules")
assert rules.status_code == 200, rules.text
rule_rows = rules.json()
assert [r["name"] for r in rule_rows] == ["tid present", "tid range"]
rules_by_name = {r["name"]: r for r in rule_rows}
assert rules_by_name["tid present"]["kind"] == "not_null"
assert rules_by_name["tid present"]["params"] == {"field": "tid"}
assert rules_by_name["tid present"]["enabled"] is True
assert rules_by_name["tid range"]["enabled"] is False

# Only the enabled rule executes after the restart.
evaluated = client.post(
    "/datasets/target_ds/versions/1/quality-rules/evaluate",
    json={"rows": [{"tid": None}, {"tid": 5}]},
)
assert evaluated.status_code == 200, evaluated.text
evaluation = evaluated.json()
assert [r["name"] for r in evaluation["results"]] == ["tid present"]
assert evaluation["results"][0]["violations"] == [0]

# Privacy policies and their enabled states survive the restart.
policies = client.get("/datasets/target_ds/versions/1/privacy-policies")
assert policies.status_code == 200, policies.text
policy_rows = policies.json()
assert [p["field"] for p in policy_rows] == ["tid", "label"]
policies_by_field = {p["field"]: p for p in policy_rows}
assert policies_by_field["tid"] == {
    "id": policies_by_field["tid"]["id"],
    "field": "tid",
    "classification": "restricted",
    "masking": "redact",
    "allowed_roles": ["auditor"],
    "enabled": True,
    "created_at": policies_by_field["tid"]["created_at"],
}
assert policies_by_field["label"]["enabled"] is False
assert policies_by_field["label"]["masking"] == "partial"

viewed = client.post(
    "/datasets/target_ds/versions/1/privacy-policies/view",
    json={"role": "guest", "rows": [
        {"tid": 42, "label": "secret-label"},
        {"tid": None, "label": None},
    ]},
)
assert viewed.status_code == 200, viewed.text
view_body = viewed.json()
assert view_body["dataset"] == "target_ds"
assert view_body["version"] == 1
# Enabled redact policy masks for a role outside allowed_roles; null stays null;
# the disabled 'label' policy leaves values untouched.
assert view_body["rows"] == [
    {"tid": "***", "label": "secret-label"},
    {"tid": None, "label": None},
]

auditor_view = client.post(
    "/datasets/target_ds/versions/1/privacy-policies/view",
    json={"role": "auditor", "rows": [{"tid": 42, "label": "secret-label"}]},
)
assert auditor_view.status_code == 200, auditor_view.text
assert auditor_view.json()["rows"] == [{"tid": 42, "label": "secret-label"}]

# Snapshots and their rows survive the restart.
snapshots = client.get("/datasets/target_ds/versions/1/snapshots")
assert snapshots.status_code == 200, snapshots.text
snapshot_list = snapshots.json()
assert len(snapshot_list) == 1
snapshot_meta = snapshot_list[0]
assert snapshot_meta["dataset"] == "target_ds"
assert snapshot_meta["version"] == 1
assert snapshot_meta["row_count"] == 2
assert "rows" not in snapshot_meta

stored = client.get(
    f"/datasets/target_ds/versions/1/snapshots/{snapshot_meta['id']}"
)
assert stored.status_code == 200, stored.text
assert stored.json()["rows"] == [
    {"tid": 1, "label": "a"},
    {"tid": 2, "label": "b", "nested": [1, 2, {"k": True}]},
]

at = client.get(
    "/datasets/target_ds/versions/1/snapshots/at",
    params={"timestamp": "2030-01-01T00:00:00+00:00"},
)
assert at.status_code == 200, at.text
assert at.json()["id"] == snapshot_meta["id"]
assert at.json()["rows"][0]["label"] == "a"

# A second snapshot after restart and a cross-snapshot diff also work.
later = client.post(
    "/datasets/target_ds/versions/1/snapshots",
    json={"rows": [{"tid": 1, "label": "a"}, {"tid": 3, "label": "c"}]},
)
assert later.status_code == 201, later.text
diff = client.get(
    f"/datasets/target_ds/versions/1/snapshots/{snapshot_meta['id']}"
    f"/diff/{later.json()['id']}"
)
assert diff.status_code == 200, diff.text
diff_body = diff.json()
assert diff_body["from_snapshot_id"] == snapshot_meta["id"]
assert diff_body["to_snapshot_id"] == later.json()["id"]
assert diff_body["added"] == [{"row": {"tid": 3, "label": "c"}, "count": 1}]
assert diff_body["removed"] == [
    {"row": {"tid": 2, "label": "b", "nested": [1, 2, {"k": True}]}, "count": 1}
]
print(json.dumps({"dataset_id": by_name["target_ds"]["id"]}))
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


def test_data_survives_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-lineage.db"

    created = _run(db_path, CREATE_SCRIPT)
    assert created == "created"
    assert db_path.exists()

    # New interpreter: nothing is cached in memory; everything comes from disk.
    output = _run(db_path, VERIFY_SCRIPT)
    assert json.loads(output)["dataset_id"] >= 1


def test_repeated_requests_share_persistent_state(client, tmp_path) -> None:
    # The autouse fixture points DATA_LINEAGE_DB at tmp_path; every request
    # opens a fresh connection, so later requests observe earlier writes.
    create = client.post("/datasets", json={"name": "orders"})
    assert create.status_code == 201
    created_id = create.json()["id"]

    again = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert again.status_code == 201

    read = client.get("/datasets/orders/versions/1")
    assert read.status_code == 200
    assert read.json()["dataset_id"] == created_id
