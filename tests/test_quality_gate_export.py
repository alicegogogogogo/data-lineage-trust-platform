"""Tests for the read-only cross-version quality gate export."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXPORT_PATH = "/datasets/orders/quality-gate-export"

TOP_LEVEL_KEYS = ["dataset", "versions", "totals"]
VERSION_KEYS = ["version", "verdict", "reason_count", "violation_row_count"]
TOTAL_KEYS = [
    "version_count",
    "failed_version_count",
    "reason_count",
    "violation_row_count",
]

BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "amount", "type": "decimal", "nullable": True},
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_dataset(client: TestClient, name: str = "orders") -> None:
    response = client.post("/datasets", json={"name": name})
    assert response.status_code == 201, response.text


def add_version(client: TestClient, dataset: str = "orders") -> int:
    response = client.post(
        f"/datasets/{dataset}/versions", json={"fields": BASE_FIELDS}
    )
    assert response.status_code == 201, response.text
    return response.json()["version"]


def rules_path(version: int, dataset: str = "orders") -> str:
    return f"/datasets/{dataset}/versions/{version}/quality-rules"


def gate_path(version: int, dataset: str = "orders") -> str:
    return f"{rules_path(version, dataset)}/gate"


def config_path(version: int, dataset: str = "orders") -> str:
    return f"{rules_path(version, dataset)}/anomaly-detection"


def create_rule(client: TestClient, version: int, payload: dict) -> dict:
    response = client.post(rules_path(version), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def evaluate(client: TestClient, version: int, rows: list[dict]) -> dict:
    response = client.post(rules_path(version) + "/evaluate", json={"rows": rows})
    assert response.status_code == 200, response.text
    return response.json()


def register_config(
    client: TestClient,
    version: int,
    *,
    steps: int = 2,
    row_limit: int = 1000,
    rule_limit: int = 1000,
) -> dict:
    response = client.post(
        config_path(version),
        json={
            "consecutive_worsening_steps": steps,
            "violation_row_limit": row_limit,
            "rule_violation_limit": rule_limit,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def scan(client: TestClient, version: int) -> list[dict]:
    response = client.post(config_path(version) + "/scan")
    assert response.status_code == 200, response.text
    return response.json()


def get_export(client: TestClient, dataset: str = "orders") -> dict:
    response = client.get(f"/datasets/{dataset}/quality-gate-export")
    assert response.status_code == 200, response.text
    return response.json()


def export_response(client: TestClient, dataset: str = "orders"):
    response = client.get(f"/datasets/{dataset}/quality-gate-export")
    assert response.status_code == 200, response.text
    return response


# --------------------------------------------------------------------------- #
# Empty state and wire format
# --------------------------------------------------------------------------- #


def test_export_without_versions_is_empty_not_an_error(client: TestClient) -> None:
    make_dataset(client)
    response = export_response(client)
    assert response.text == (
        '{"dataset":"orders","versions":[],'
        '"totals":{"version_count":0,"failed_version_count":0,'
        '"reason_count":0,"violation_row_count":0}}\n'
    )
    body = response.json()
    assert list(body) == TOP_LEVEL_KEYS
    assert body["dataset"] == "orders"
    assert body["versions"] == []
    assert body["totals"] == {
        "version_count": 0,
        "failed_version_count": 0,
        "reason_count": 0,
        "violation_row_count": 0,
    }


def test_export_is_get_only(client: TestClient) -> None:
    make_dataset(client)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, EXPORT_PATH)
        assert response.status_code == 405, method


def test_export_wire_format_is_compact_with_fixed_key_order_and_newline(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    create_rule(client, 1, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, 1, [{"id": None, "amount": 1}])

    response = export_response(client)
    assert response.headers["content-type"].startswith("application/json")
    text = response.text
    # Compact whitespace: no separator spaces, and the only line break is the
    # single trailing newline.
    assert ": " not in text
    assert ", " not in text
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    assert "\n" not in text[:-1]
    # Lower-case booleans, no scientific notation for the integer counters.
    assert "True" not in text
    assert "False" not in text
    assert "e+" not in text.lower()

    body = response.json()
    assert list(body) == TOP_LEVEL_KEYS
    assert list(body["totals"]) == TOTAL_KEYS
    entry = body["versions"][0]
    assert list(entry) == VERSION_KEYS
    # The body is the compact serialization of the parsed document.
    assert text == json.dumps(
        body, separators=(",", ":"), ensure_ascii=False
    ) + "\n"


# --------------------------------------------------------------------------- #
# Version entries: verdicts, reason counts, violation row counts
# --------------------------------------------------------------------------- #


def test_export_versions_sort_ascending_and_cover_every_version(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    add_version(client)
    add_version(client)

    body = get_export(client)
    assert [entry["version"] for entry in body["versions"]] == [1, 2, 3]
    for entry in body["versions"]:
        assert list(entry) == VERSION_KEYS
        assert entry["verdict"] == "undetermined"
        assert entry["reason_count"] == 0
        # Never evaluated: the key survives with a null value.
        assert "violation_row_count" in entry
        assert entry["violation_row_count"] is None


def test_export_matches_the_per_version_gate_for_every_status(
    client: TestClient,
) -> None:
    make_dataset(client)

    # v1: fail — latest evaluation still violates, plus an anomaly reason.
    add_version(client)
    create_rule(
        client, 1, {"name": "a", "kind": "not_null", "params": {"field": "id"}}
    )
    create_rule(
        client, 1, {"name": "b", "kind": "not_null", "params": {"field": "amount"}}
    )
    register_config(client, 1, row_limit=0)
    # Two distinct violating rows across the two rules (2 violation reasons).
    evaluate(client, 1, [{"id": None, "amount": None}, {"id": 1}])
    scan(client, 1)  # one row_limit anomaly

    # v2: pass — a clean latest evaluation wipes a prior failure.
    add_version(client)
    create_rule(
        client, 2, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, 2, [{"id": None}])
    evaluate(client, 2, [{"id": 1}])

    # v3: undetermined — schema only, never evaluated.
    add_version(client)

    body = get_export(client)
    entries = {entry["version"]: entry for entry in body["versions"]}
    assert sorted(entries) == [1, 2, 3]

    for version in (1, 2, 3):
        gate = client.get(gate_path(version)).json()
        entry = entries[version]
        assert entry["verdict"] == gate["verdict"]
        assert entry["reason_count"] == gate["counts"]["reasons"] == len(
            gate["reasons"]
        )

    assert entries[1]["verdict"] == "fail"
    assert entries[1]["reason_count"] == 3  # two violation reasons + one anomaly
    assert entries[1]["violation_row_count"] == 2

    assert entries[2]["verdict"] == "pass"
    assert entries[2]["reason_count"] == 0
    # The latest (clean) evaluation's count, not the earlier failing one's.
    assert entries[2]["violation_row_count"] == 0

    assert entries[3]["verdict"] == "undetermined"
    assert entries[3]["reason_count"] == 0
    assert entries[3]["violation_row_count"] is None


def test_export_violation_count_uses_only_the_latest_evaluation(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    create_rule(
        client, 1, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, 1, [{"id": None}, {"id": None}])  # sequence 1: 2 rows
    evaluate(client, 1, [{"id": None}, {"id": 1}, {"id": 2}])  # seq 2: 1 row
    evaluate(client, 1, [{"id": 1}])  # sequence 3: clean

    entry = get_export(client)["versions"][0]
    assert entry["verdict"] == "pass"
    assert entry["violation_row_count"] == 0


def test_export_anomaly_only_fail_carries_the_clean_latest_count(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    create_rule(
        client, 1, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    register_config(client, 1, row_limit=0)
    evaluate(client, 1, [{"id": None}, {"id": None}])
    scan(client, 1)
    evaluate(client, 1, [{"id": 1}])  # clean latest, anomaly persists

    entry = get_export(client)["versions"][0]
    assert entry["verdict"] == "fail"
    assert entry["reason_count"] == 1
    assert entry["violation_row_count"] == 0


def test_export_is_scoped_to_the_named_dataset(client: TestClient) -> None:
    make_dataset(client, "orders")
    make_dataset(client, "other")
    add_version(client, "orders")
    add_version(client, "other")
    create_rule(
        client, 1, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, 1, [{"id": None}])
    response = client.post(
        rules_path(1, "other"),
        json={"name": "r", "kind": "not_null", "params": {"field": "id"}},
    )
    assert response.status_code == 201, response.text

    orders = get_export(client, "orders")
    other = get_export(client, "other")
    assert orders["dataset"] == "orders"
    assert other["dataset"] == "other"
    assert orders["versions"][0]["verdict"] == "fail"
    assert orders["versions"][0]["violation_row_count"] == 1
    assert other["versions"][0]["verdict"] == "undetermined"
    assert other["versions"][0]["violation_row_count"] is None


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #


def test_export_totals_equal_the_sum_of_the_version_entries(
    client: TestClient,
) -> None:
    make_dataset(client)

    # v1: fail, 3 reasons, latest violation row count 2.
    add_version(client)
    create_rule(
        client, 1, {"name": "a", "kind": "not_null", "params": {"field": "id"}}
    )
    create_rule(
        client, 1, {"name": "b", "kind": "not_null", "params": {"field": "amount"}}
    )
    register_config(client, 1, row_limit=0)
    evaluate(client, 1, [{"id": None, "amount": None}, {"id": 1}])
    scan(client, 1)

    # v2: pass with a clean zero-violation latest evaluation.
    add_version(client)
    create_rule(
        client, 2, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, 2, [{"id": 1}])

    # v3: undetermined (never evaluated).
    add_version(client)

    body = get_export(client)
    versions = body["versions"]
    totals = body["totals"]
    assert [entry["version"] for entry in versions] == [1, 2, 3]

    assert totals["version_count"] == len(versions) == 3
    assert totals["failed_version_count"] == 1
    assert (
        totals["reason_count"]
        == sum(entry["reason_count"] for entry in versions)
        == 3
    )
    # Null counts contribute zero.
    assert (
        totals["violation_row_count"]
        == sum((entry["violation_row_count"] or 0) for entry in versions)
        == 2
    )
    assert totals == {
        "version_count": 3,
        "failed_version_count": 1,
        "reason_count": 3,
        "violation_row_count": 2,
    }
    assert list(totals) == TOTAL_KEYS


def test_export_empty_dataset_totals_are_all_zero(client: TestClient) -> None:
    make_dataset(client)
    totals = get_export(client)["totals"]
    assert totals == {
        "version_count": 0,
        "failed_version_count": 0,
        "reason_count": 0,
        "violation_row_count": 0,
    }


# --------------------------------------------------------------------------- #
# Errors: 404 / 422, no writes
# --------------------------------------------------------------------------- #


def test_export_unknown_dataset_returns_404(client: TestClient) -> None:
    response = client.get("/datasets/ghost/quality-gate-export")
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "not_found"
    assert "sqlite" not in response.text.lower()
    assert "traceback" not in response.text.lower()


def test_export_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset(client)
    before = export_response(client).text

    with_body = client.request("GET", EXPORT_PATH, content=b"{}")
    whitespace_body = client.request("GET", EXPORT_PATH, content=b"   ")
    single_space_body = client.request("GET", EXPORT_PATH, content=b" ")
    with_query = client.get(EXPORT_PATH, params={"x": "1"})
    assert with_body.status_code == 422
    assert whitespace_body.status_code == 422
    assert single_space_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, whitespace_body, single_space_body, with_query):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]
        assert "sqlite" not in response.text.lower()
        assert "traceback" not in response.text.lower()

    # The rejections wrote nothing.
    assert export_response(client).text == before


def test_export_shape_errors_keep_404_precedence(client: TestClient) -> None:
    assert client.get(
        "/datasets/ghost/quality-gate-export", params={"x": "1"}
    ).status_code == 404
    assert (
        client.request(
            "GET", "/datasets/ghost/quality-gate-export", content=b"{}"
        ).status_code
        == 404
    )
    assert (
        client.request(
            "GET", "/datasets/ghost/quality-gate-export", content=b" "
        ).status_code
        == 404
    )


# --------------------------------------------------------------------------- #
# Read-only behaviour and persistence across restarts
# --------------------------------------------------------------------------- #


def test_export_is_strictly_read_only(client: TestClient) -> None:
    make_dataset(client)
    add_version(client)
    create_rule(
        client, 1, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    register_config(client, 1, row_limit=0)
    evaluate(client, 1, [{"id": None}])
    scan(client, 1)

    history_before = client.get(rules_path(1) + "/evaluations").json()
    anomalies_before = client.get(config_path(1) + "/anomalies").json()
    gate_before = client.get(gate_path(1)).text
    first_text = export_response(client).text

    for _ in range(3):
        assert export_response(client).text == first_text
    assert client.get(rules_path(1) + "/evaluations").json() == history_before
    assert client.get(config_path(1) + "/anomalies").json() == anomalies_before
    assert client.get(gate_path(1)).text == gate_before


_CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def ok(response):
    assert response.status_code in (200, 201), response.text
    return response.json()


ok(client.post("/datasets", json={"name": "orders"}))
fields = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "amount", "type": "decimal", "nullable": True},
]
ok(client.post("/datasets/orders/versions", json={"fields": fields}))
base = "/datasets/orders/versions/1/quality-rules"
ok(client.post(base, json={
    "name": "r", "kind": "not_null", "params": {"field": "id"},
}))
ok(client.post(base + "/evaluate", json={"rows": [{"id": None}, {"id": 1}]}))
ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
))
ok(client.post(
    "/datasets/orders/versions/2/quality-rules",
    json={"name": "r", "kind": "not_null", "params": {"field": "id"}},
))
ok(client.post(
    "/datasets/orders/versions/2/quality-rules/evaluate",
    json={"rows": [{"id": 1}]},
))
ok(client.post("/datasets/orders/versions", json={"fields": fields}))
print("created")
"""

_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

path = "/datasets/orders/quality-gate-export"
response = client.get(path)
assert response.status_code == 200, response.text
assert response.text.endswith("\\n")
body = response.json()
assert list(body) == ["dataset", "versions", "totals"]
assert body["dataset"] == "orders"
assert [entry["version"] for entry in body["versions"]] == [1, 2, 3]

first = body["versions"][0]
assert list(first) == ["version", "verdict", "reason_count", "violation_row_count"]
assert first == {
    "version": 1,
    "verdict": "fail",
    "reason_count": 1,
    "violation_row_count": 1,
}
second = body["versions"][1]
assert second == {
    "version": 2,
    "verdict": "pass",
    "reason_count": 0,
    "violation_row_count": 0,
}
third = body["versions"][2]
assert third == {
    "version": 3,
    "verdict": "undetermined",
    "reason_count": 0,
    "violation_row_count": None,
}

assert body["totals"] == {
    "version_count": 3,
    "failed_version_count": 1,
    "reason_count": 1,
    "violation_row_count": 1,
}

# Repeated reads and a fresh request return byte-identical documents.
again = client.get(path)
assert again.status_code == 200, again.text
assert again.text == response.text
print("verified")
"""


def _run_script(db_path: Path, script: str) -> str:
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


def test_export_survives_a_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "quality-gate-export.db"
    assert _run_script(db_path, _CREATE_SCRIPT) == "created"
    assert _run_script(db_path, _VERIFY_SCRIPT) == "verified"
