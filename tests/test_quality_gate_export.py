"""Tests for the read-only cross-version quality gate export."""

from __future__ import annotations

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
    "failed_count",
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


def rules_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/quality-rules"


def gate_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{rules_path(dataset, version)}/gate"


def create_rule(
    client: TestClient, dataset: str = "orders", version: int = 1
) -> dict:
    response = client.post(
        rules_path(dataset, version),
        json={"name": "r", "kind": "not_null", "params": {"field": "id"}},
    )
    assert response.status_code == 201, response.text
    return response.json()


def evaluate(
    client: TestClient, rows: list[dict], dataset: str = "orders", version: int = 1
) -> dict:
    response = client.post(
        f"{rules_path(dataset, version)}/evaluate", json={"rows": rows}
    )
    assert response.status_code == 200, response.text
    return response.json()


def scan_anomalies(
    client: TestClient,
    dataset: str = "orders",
    version: int = 1,
    row_limit: int = 0,
) -> list[dict]:
    config_path = f"{rules_path(dataset, version)}/anomaly-detection"
    response = client.post(
        config_path,
        json={
            "consecutive_worsening_steps": 2,
            "violation_row_limit": row_limit,
            "rule_violation_limit": 1000,
        },
    )
    assert response.status_code == 201, response.text
    response = client.post(f"{config_path}/scan")
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
    body = get_export(client)
    assert list(body) == TOP_LEVEL_KEYS
    assert body["dataset"] == "orders"
    assert body["versions"] == []
    assert body["totals"] == {
        "version_count": 0,
        "failed_count": 0,
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
    create_rule(client)
    evaluate(client, [{"id": None, "amount": 1}, {"id": 2, "amount": 3}])
    add_version(client)  # never evaluated

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
    # Lower-case booleans and null, no scientific notation for the counters.
    assert "True" not in text
    assert "False" not in text
    assert "None" not in text
    assert "e+" not in text.lower()

    # Fixed key order at every level.
    body = response.json()
    assert list(body) == TOP_LEVEL_KEYS
    assert list(body["totals"]) == TOTAL_KEYS
    for entry in body["versions"]:
        assert list(entry) == VERSION_KEYS

    # The whole document is exactly this byte sequence.
    assert text == (
        '{"dataset":"orders","versions":['
        '{"version":1,"verdict":"fail","reason_count":1,'
        '"violation_row_count":1},'
        '{"version":2,"verdict":"undetermined","reason_count":0,'
        '"violation_row_count":null}],'
        '"totals":{"version_count":2,"failed_count":1,"reason_count":1,'
        '"violation_row_count":1}}\n'
    )


def test_unevaluated_version_keeps_the_null_violation_count_key(
    client: TestClient,
) -> None:
    make_dataset(client)
    add_version(client)
    create_rule(client)  # a rule without any evaluation is still unevaluated

    body = get_export(client)
    entry = body["versions"][0]
    assert entry["verdict"] == "undetermined"
    assert entry["reason_count"] == 0
    assert entry["violation_row_count"] is None
    # The key is present (as null), never omitted.
    assert '"violation_row_count":null' in export_response(client).text
    assert body["totals"] == {
        "version_count": 1,
        "failed_count": 0,
        "reason_count": 0,
        "violation_row_count": 0,
    }


# --------------------------------------------------------------------------- #
# Versions, verdicts and counts
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
        assert entry["violation_row_count"] is None


def test_export_matches_the_per_version_gate(client: TestClient) -> None:
    make_dataset(client)

    # v1: passing latest evaluation, no anomalies.
    add_version(client)
    create_rule(client, version=1)
    evaluate(client, [{"id": 1, "amount": 2}], version=1)

    # v2: latest evaluation still has violating rows.
    add_version(client)
    create_rule(client, version=2)
    evaluate(client, [{"id": None, "amount": 1}], version=2)
    evaluate(
        client,
        [{"id": None, "amount": 1}, {"id": None, "amount": 2}],
        version=2,
    )

    # v3: violations plus a persisted anomaly record.
    add_version(client)
    create_rule(client, version=3)
    evaluate(client, [{"id": None, "amount": 1}], version=3)
    anomalies = scan_anomalies(client, version=3, row_limit=0)
    assert len(anomalies) == 1

    # v4: never evaluated.
    add_version(client)

    body = get_export(client)
    assert [entry["version"] for entry in body["versions"]] == [1, 2, 3, 4]
    for entry in body["versions"]:
        gate = client.get(gate_path(version=entry["version"])).json()
        assert entry["verdict"] == gate["verdict"]
        assert entry["reason_count"] == len(gate["reasons"])
        assert entry["reason_count"] == gate["counts"]["reasons"]
        evaluations = client.get(
            f"{rules_path(version=entry['version'])}/evaluations"
        ).json()
        if evaluations:
            assert (
                entry["violation_row_count"]
                == evaluations[-1]["violation_row_count"]
            )
        else:
            assert entry["violation_row_count"] is None

    entries = {entry["version"]: entry for entry in body["versions"]}
    assert entries[1]["verdict"] == "pass"
    assert entries[1]["reason_count"] == 0
    assert entries[1]["violation_row_count"] == 0
    assert entries[2]["verdict"] == "fail"
    assert entries[2]["reason_count"] == 1
    assert entries[2]["violation_row_count"] == 2
    assert entries[3]["verdict"] == "fail"
    # One violation reason plus one row_limit anomaly reason.
    assert entries[3]["reason_count"] == 2
    assert entries[3]["violation_row_count"] == 1
    assert entries[4]["verdict"] == "undetermined"
    assert entries[4]["violation_row_count"] is None


def test_export_is_scoped_to_the_named_dataset(client: TestClient) -> None:
    make_dataset(client, "orders")
    make_dataset(client, "other")
    add_version(client, "orders")
    add_version(client, "other")
    create_rule(client, dataset="other")
    evaluate(client, [{"id": None, "amount": 1}], dataset="other")

    orders = get_export(client, "orders")
    other = get_export(client, "other")
    assert orders["dataset"] == "orders"
    assert other["dataset"] == "other"
    assert orders["versions"][0]["verdict"] == "undetermined"
    assert other["versions"][0]["verdict"] == "fail"
    assert orders["totals"] == {
        "version_count": 1,
        "failed_count": 0,
        "reason_count": 0,
        "violation_row_count": 0,
    }
    assert other["totals"] == {
        "version_count": 1,
        "failed_count": 1,
        "reason_count": 1,
        "violation_row_count": 1,
    }


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #


def test_export_totals_equal_the_sum_of_the_version_entries(
    client: TestClient,
) -> None:
    make_dataset(client)

    # v1: fail, one violation reason, two violating rows in the latest
    # evaluation.
    add_version(client)
    create_rule(client, version=1)
    evaluate(
        client,
        [{"id": None, "amount": 1}, {"id": None, "amount": 2}],
        version=1,
    )

    # v2: pass with a zero-violation evaluation.
    add_version(client)
    create_rule(client, version=2)
    evaluate(client, [{"id": 1, "amount": 2}], version=2)

    # v3: fail through a persisted anomaly as well (two reasons).
    add_version(client)
    create_rule(client, version=3)
    evaluate(client, [{"id": None, "amount": 1}], version=3)
    scan_anomalies(client, version=3, row_limit=0)

    # v4: undetermined, null violation row count.
    add_version(client)

    body = get_export(client)
    versions = body["versions"]
    totals = body["totals"]
    assert [entry["version"] for entry in versions] == [1, 2, 3, 4]

    assert totals["version_count"] == len(versions) == 4
    assert totals["failed_count"] == sum(
        1 for entry in versions if entry["verdict"] == "fail"
    ) == 2
    assert totals["reason_count"] == sum(
        entry["reason_count"] for entry in versions
    ) == 3
    # Null violation row counts count as zero in the sum.
    assert totals["violation_row_count"] == sum(
        entry["violation_row_count"] or 0 for entry in versions
    ) == 3
    assert totals == {
        "version_count": 4,
        "failed_count": 2,
        "reason_count": 3,
        "violation_row_count": 3,
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
    with_query = client.get(EXPORT_PATH, params={"limit": 1})
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
    make_dataset(client)
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
            "GET",
            "/datasets/ghost/quality-gate-export",
            content=b" ",
        ).status_code
        == 404
    )


def test_export_is_strictly_read_only(client: TestClient) -> None:
    make_dataset(client)
    add_version(client)
    create_rule(client)
    evaluate(client, [{"id": None, "amount": 1}])
    scan_anomalies(client, row_limit=0)

    gate_before = client.get(gate_path()).json()
    evaluations_before = client.get(
        f"{rules_path()}/evaluations"
    ).json()
    anomalies_before = client.get(
        f"{rules_path()}/anomaly-detection/anomalies"
    ).json()
    first_text = export_response(client).text

    for _ in range(3):
        response = export_response(client)
        assert response.text == first_text
    assert client.get(gate_path()).json() == gate_before
    assert client.get(f"{rules_path()}/evaluations").json() == evaluations_before
    assert (
        client.get(f"{rules_path()}/anomaly-detection/anomalies").json()
        == anomalies_before
    )


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


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
rules = "/datasets/orders/versions/1/quality-rules"
ok(client.post(
    rules,
    json={"name": "r", "kind": "not_null", "params": {"field": "id"}},
))
ok(client.post(
    f"{rules}/evaluate",
    json={"rows": [{"id": None, "amount": 1}, {"id": 2, "amount": 3}]},
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
assert [entry["version"] for entry in body["versions"]] == [1, 2]

first = body["versions"][0]
assert list(first) == [
    "version", "verdict", "reason_count", "violation_row_count",
]
assert first == {
    "version": 1,
    "verdict": "fail",
    "reason_count": 1,
    "violation_row_count": 1,
}

second = body["versions"][1]
assert second == {
    "version": 2,
    "verdict": "undetermined",
    "reason_count": 0,
    "violation_row_count": None,
}

assert body["totals"] == {
    "version_count": 2,
    "failed_count": 1,
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
