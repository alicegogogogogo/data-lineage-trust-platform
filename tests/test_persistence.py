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
        {"name": "ssn", "type": "string", "nullable": True},
        {"name": "email", "type": "string", "nullable": True},
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
    json={"field": "ssn", "classification": "pii", "masking": "redact",
          "allowed_roles": ["auditor"]},
))
disabled_policy = client.post(
    "/datasets/target_ds/versions/1/privacy-policies",
    json={"field": "email", "classification": "pii", "masking": "partial",
          "allowed_roles": []},
)
assert disabled_policy.status_code == 201, disabled_policy.text
ok(client.patch(
    "/datasets/target_ds/versions/1/privacy-policies/"
    f"{disabled_policy.json()['id']}",
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
lineage_by_field = {entry["target_field"]: entry["sources"] for entry in body["fields"]}
assert set(lineage_by_field) == {"tid", "ssn", "email"}
assert lineage_by_field["tid"] == [
    {"dataset": "source_ds", "version": 1, "field": "sid"}
]
assert lineage_by_field["ssn"] == []
assert lineage_by_field["email"] == []

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

policies = client.get("/datasets/target_ds/versions/1/privacy-policies")
assert policies.status_code == 200, policies.text
policy_rows = policies.json()
assert [p["field"] for p in policy_rows] == ["ssn", "email"]
policies_by_field = {p["field"]: p for p in policy_rows}
assert policies_by_field["ssn"]["classification"] == "pii"
assert policies_by_field["ssn"]["masking"] == "redact"
assert policies_by_field["ssn"]["allowed_roles"] == ["auditor"]
assert policies_by_field["ssn"]["enabled"] is True
assert policies_by_field["email"]["masking"] == "partial"
assert policies_by_field["email"]["allowed_roles"] == []
assert policies_by_field["email"]["enabled"] is False

# Masking behaviour and the persisted enabled state hold after the restart.
view_rows = [{"tid": 1, "ssn": "123-45-6789", "email": "alice@example.com"}]
masked = client.post(
    "/datasets/target_ds/versions/1/privacy-policies/view",
    json={"role": "guest", "rows": view_rows},
)
assert masked.status_code == 200, masked.text
masked_rows = masked.json()["rows"]
assert masked_rows[0]["ssn"] == "***"            # enabled redact, role not allowed
assert masked_rows[0]["email"] == "alice@example.com"  # disabled policy

visible = client.post(
    "/datasets/target_ds/versions/1/privacy-policies/view",
    json={"role": "auditor", "rows": view_rows},
)
assert visible.status_code == 200, visible.text
assert visible.json()["rows"][0]["ssn"] == "123-45-6789"  # allowed role
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
