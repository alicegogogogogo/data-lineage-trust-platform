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
    json={"fields": [{"name": "tid", "type": "integer", "nullable": False}]},
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
        "target_field": "tid",
        "sources": [
            {"dataset": "source_ds", "version": 1, "field": "sid"}
        ],
    }
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
