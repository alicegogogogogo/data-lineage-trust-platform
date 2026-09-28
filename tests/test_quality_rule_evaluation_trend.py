"""Tests for the read-only quality evaluation trend summary."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]


BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "amount", "type": "decimal", "nullable": True},
    {"name": "region", "type": "string", "nullable": True},
]


def make_dataset_with_version(client: TestClient, name: str = "orders") -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions", json={"fields": BASE_FIELDS}
    )
    assert response.status_code == 201, response.text


def rules_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/quality-rules"


def history_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{rules_path(dataset, version)}/evaluations"


def trend_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{history_path(dataset, version)}/trend"


def create_rule(client: TestClient, payload: dict) -> dict:
    response = client.post(rules_path(), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def evaluate(client: TestClient, rows: list[dict], **path: object) -> dict:
    response = client.post(
        rules_path(**path) + "/evaluate",  # type: ignore[arg-type]
        json={"rows": rows},
    )
    assert response.status_code == 200, response.text
    return response.json()


def get_trend(client: TestClient, *path_args: object):
    target = trend_path(*path_args) if path_args else trend_path()  # type: ignore[arg-type]
    response = client.get(target)
    assert response.status_code == 200, response.text
    return response


EVALUATION_KEYS = {
    "created_at",
    "row_count",
    "violation_row_count",
    "violation_row_count_delta",
}
RULE_KEYS = {
    "rule_id",
    "name",
    "violation_row_count",
    "violating_evaluation_count",
    "first_violation_sequence",
    "last_violation_sequence",
}
TOTALS_KEYS = {"evaluation_count", "violation_row_count", "rule_count"}


# --------------------------------------------------------------------------- #
# Empty state and response shape
# --------------------------------------------------------------------------- #


def test_trend_without_history_is_an_explicit_empty_result(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    response = get_trend(client)
    assert list(response.json()) == ["dataset", "version", "evaluations", "rules", "totals"]
    body = response.json()
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["evaluations"] == []
    assert body["rules"] == []
    assert body["totals"] == {
        "evaluation_count": 0,
        "violation_row_count": 0,
        "rule_count": 0,
    }
    # The empty document still ends with exactly one newline.
    assert response.text.endswith("\n")
    assert not response.text.endswith("\n\n")


def test_trend_is_get_only(client: TestClient) -> None:
    make_dataset_with_version(client)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, trend_path())
        assert response.status_code == 405, method


# --------------------------------------------------------------------------- #
# Per-evaluation series
# --------------------------------------------------------------------------- #


def test_evaluations_series_uses_history_keys_with_delta(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )

    evaluate(client, [{"id": None}, {"id": None}, {"id": 1}])  # 2 violating rows
    evaluate(client, [{"id": None}])  # 1
    evaluate(client, [])  # 0

    history = client.get(history_path()).json()
    body = get_trend(client).json()
    entries = body["evaluations"]
    assert len(entries) == 3
    for entry in entries:
        assert set(entry) == EVALUATION_KEYS

    # Keys and values reuse the history summary.
    for entry, record in zip(entries, history):
        assert entry["created_at"] == record["created_at"]
        assert entry["row_count"] == record["row_count"]
        assert entry["violation_row_count"] == record["violation_row_count"]
        datetime.fromisoformat(entry["created_at"])

    assert [entry["row_count"] for entry in entries] == [3, 1, 0]
    assert [entry["violation_row_count"] for entry in entries] == [2, 1, 0]
    # The first delta is null (key present); the rest compare to the previous
    # evaluation in sequence order.
    assert entries[0]["violation_row_count_delta"] is None
    assert [entry["violation_row_count_delta"] for entry in entries[1:]] == [-1, -1]


def test_delta_handles_increase_and_no_change_and_zero_first(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )

    evaluate(client, [])  # 0
    evaluate(client, [{"id": 1}, {"id": None}])  # 1 -> +1
    evaluate(client, [{"id": None}])  # 1 -> 0
    # Row 1 omits id and row 2 carries null: two distinct violating rows.
    evaluate(client, [{"id": 1}, {"amount": 1}, {"id": None}])  # 2 -> +1

    deltas = [
        entry["violation_row_count_delta"]
        for entry in get_trend(client).json()["evaluations"]
    ]
    assert deltas == [None, 1, 0, 1]


# --------------------------------------------------------------------------- #
# Per-rule aggregation
# --------------------------------------------------------------------------- #


def test_rule_rows_aggregate_counts_and_violation_sequences(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    id_rule = create_rule(
        client, {"name": "id", "kind": "not_null", "params": {"field": "id"}}
    )
    amount_rule = create_rule(
        client,
        {"name": "amount", "kind": "not_null", "params": {"field": "amount"}},
    )

    # seq 1: id rule violates rows 0 (missing) and 1 (null); amount rule
    # violates row 0 (missing).
    evaluate(client, [{"region": "eu"}, {"id": None, "amount": 9}, {"id": 3, "amount": 5}])
    # seq 2: both rules pass (id present on every row, amount always given).
    evaluate(client, [{"id": 1, "amount": 1}, {"id": 2, "amount": 2}])
    # seq 3: only amount rule violates, one row.
    evaluate(client, [{"id": 1}, {"id": 2, "amount": 1}])

    rules = get_trend(client).json()["rules"]
    assert [set(row) for row in rules] == [RULE_KEYS, RULE_KEYS]
    by_id = {row["rule_id"]: row for row in rules}

    id_row = by_id[id_rule["id"]]
    assert id_row["name"] == "id"
    assert id_row["violation_row_count"] == 2
    assert id_row["violating_evaluation_count"] == 1
    assert id_row["first_violation_sequence"] == 1
    assert id_row["last_violation_sequence"] == 1

    amount_row = by_id[amount_rule["id"]]
    assert amount_row["name"] == "amount"
    # One violating row in seq 1 and one in seq 3.
    assert amount_row["violation_row_count"] == 2
    assert amount_row["violating_evaluation_count"] == 2
    assert amount_row["first_violation_sequence"] == 1
    assert amount_row["last_violation_sequence"] == 3


def test_disabled_rule_keeps_its_aggregation_row(client: TestClient) -> None:
    make_dataset_with_version(client)
    paused = create_rule(
        client, {"name": "paused", "kind": "not_null", "params": {"field": "id"}}
    )
    kept = create_rule(
        client, {"name": "kept", "kind": "not_null", "params": {"field": "amount"}}
    )

    evaluate(client, [{"id": None, "amount": None}])  # seq 1: both violate
    assert (
        client.patch(
            f"{rules_path()}/{paused['id']}", json={"enabled": False}
        ).status_code
        == 200
    )
    evaluate(client, [{"id": 1, "amount": None}])  # seq 2: only kept violates

    by_id = {row["rule_id"]: row for row in get_trend(client).json()["rules"]}
    assert set(by_id) == {paused["id"], kept["id"]}
    paused_row = by_id[paused["id"]]
    assert paused_row["name"] == "paused"
    assert paused_row["violation_row_count"] == 1
    assert paused_row["violating_evaluation_count"] == 1
    assert paused_row["first_violation_sequence"] == 1
    assert paused_row["last_violation_sequence"] == 1
    assert by_id[kept["id"]]["violating_evaluation_count"] == 2


def test_rule_that_never_violated_appears_with_zero_counts_and_null_sequences(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    clean = create_rule(
        client, {"name": "clean", "kind": "not_null", "params": {"field": "id"}}
    )

    evaluate(client, [{"id": 1}])
    evaluate(client, [{"id": 2}, {"id": 3}])

    rules = get_trend(client).json()["rules"]
    assert len(rules) == 1
    row = rules[0]
    assert row["rule_id"] == clean["id"]
    assert row["name"] == "clean"
    assert row["violation_row_count"] == 0
    assert row["violating_evaluation_count"] == 0
    assert row["first_violation_sequence"] is None
    assert row["last_violation_sequence"] is None


def test_rules_sort_by_rule_id_independent_of_database_order(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    first = create_rule(
        client, {"name": "first", "kind": "not_null", "params": {"field": "region"}}
    )
    second = create_rule(
        client, {"name": "second", "kind": "not_null", "params": {"field": "amount"}}
    )
    third = create_rule(
        client, {"name": "third", "kind": "not_null", "params": {"field": "id"}}
    )

    # Submit in reverse rule-id order so the highest id is the first to
    # violate in stored history order.
    evaluate(client, [{"amount": 1, "region": "eu"}])  # third (id) violates
    evaluate(client, [{"id": 1, "region": "eu"}])  # second (amount) violates
    evaluate(client, [{"id": 1, "amount": 1}])  # first (region) violates

    rows = get_trend(client).json()["rules"]
    assert [row["rule_id"] for row in rows] == [
        first["id"],
        second["id"],
        third["id"],
    ]
    assert [row["first_violation_sequence"] for row in rows] == [3, 2, 1]


def test_rule_counts_sum_per_rule_violations_not_distinct_rows(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "id", "kind": "not_null", "params": {"field": "id"}}
    )
    create_rule(
        client, {"name": "amount", "kind": "not_null", "params": {"field": "amount"}}
    )

    # One row violates both rules in one evaluation: it counts once for each
    # rule's cumulative total.
    evaluate(client, [{"region": "eu"}])

    rules = get_trend(client).json()["rules"]
    assert [row["violation_row_count"] for row in rules] == [1, 1]


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #


def test_totals_count_evaluations_rows_and_involved_rules(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "id", "kind": "not_null", "params": {"field": "id"}}
    )
    create_rule(
        client, {"name": "amount", "kind": "not_null", "params": {"field": "amount"}}
    )

    evaluate(client, [{"amount": 1}, {"id": 2}])  # seq 1: 2 distinct violating rows
    evaluate(client, [{"id": 1, "amount": 1}])  # seq 2: 0
    evaluate(client, [{"id": None}])  # seq 3: 1

    totals = get_trend(client).json()["totals"]
    assert set(totals) == TOTALS_KEYS
    assert totals["evaluation_count"] == 3
    assert totals["violation_row_count"] == 3
    # Both rules appear in the history even though only one violated in the
    # last evaluation; the other is still an involved rule.
    assert totals["rule_count"] == 2


def test_never_violated_rule_counts_toward_rule_count(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "clean", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": 1}])

    totals = get_trend(client).json()["totals"]
    assert totals == {
        "evaluation_count": 1,
        "violation_row_count": 0,
        "rule_count": 1,
    }


def test_trend_is_scoped_to_its_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])
    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": BASE_FIELDS},
        ).status_code
        == 201
    )

    other = get_trend(client, "orders", 2).json()
    assert other["evaluations"] == []
    assert other["rules"] == []
    assert other["totals"]["evaluation_count"] == 0
    assert len(get_trend(client).json()["evaluations"]) == 1


# --------------------------------------------------------------------------- #
# Errors: 404 / 422, no writes
# --------------------------------------------------------------------------- #


def test_trend_unknown_dataset_or_version_returns_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    for path in (trend_path("ghost"), trend_path("orders", 9)):
        response = client.get(path)
        assert response.status_code == 404
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "not_found"
        assert response.json()["detail"]


def test_trend_rejects_any_body_bytes_and_query_params(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])

    for content in (b"{}", b" ", b" \t\n", b"\x00"):
        response = client.request("GET", trend_path(), content=content)
        assert response.status_code == 422, content
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]

    for params in ({"limit": 1}, {"x": "1", "y": "2"}):
        response = client.get(trend_path(), params=params)
        assert response.status_code == 422, params
        assert response.json()["error"] == "validation_error"


def test_trend_404_precedes_body_and_query_shape_checks(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert (
        client.request("GET", trend_path("ghost"), content=b"{}").status_code == 404
    )
    assert (
        client.request("GET", trend_path("orders", 9), content=b" ").status_code
        == 404
    )
    assert client.get(trend_path("ghost"), params={"x": "1"}).status_code == 404


def test_trend_reads_write_nothing(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])
    history_before = client.get(history_path()).json()
    snapshot = get_trend(client).text

    for _ in range(3):
        assert get_trend(client).text == snapshot
    assert client.get(history_path()).json() == history_before
    # The rejected shape requests write nothing either.
    client.request("GET", trend_path(), content=b"{}")
    client.get(trend_path(), params={"x": 1})
    assert client.get(history_path()).json() == history_before


# --------------------------------------------------------------------------- #
# Deterministic JSON document and persistence across restarts
# --------------------------------------------------------------------------- #


def test_trend_document_is_compact_with_fixed_key_order_and_a_newline(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])

    text = get_trend(client).text
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    # Compact whitespace: no spaces after the JSON separators.
    assert ": " not in text
    assert ", " not in text
    # Null renders lowercase.
    assert '"violation_row_count_delta":null' in text
    assert "None" not in text
    # Fixed top-level and nested key order.
    assert list(get_trend(client).json()) == [
        "dataset",
        "version",
        "evaluations",
        "rules",
        "totals",
    ]


def test_trend_is_identical_across_clients_and_restarts(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}, {"id": 1}])
    evaluate(client, [{"id": 1}])

    from app.main import app

    first = get_trend(client).text
    second = TestClient(app).get(trend_path()).text
    assert first == second


_CREATE_AND_EVALUATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

assert client.post("/datasets", json={"name": "orders"}).status_code == 201
assert client.post(
    "/datasets/orders/versions",
    json={"fields": [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "amount", "type": "decimal", "nullable": True},
    ]},
).status_code == 201
base = "/datasets/orders/versions/1/quality-rules"
first = client.post(
    base, json={"name": "id", "kind": "not_null", "params": {"field": "id"}}
)
assert first.status_code == 201, first.text
second = client.post(
    base,
    json={"name": "amount", "kind": "not_null", "params": {"field": "amount"}},
)
assert second.status_code == 201, second.text
assert client.post(
    base + "/evaluate", json={"rows": [{"amount": 1}, {"id": 2}]}
).status_code == 200
assert client.post(
    base + "/evaluate", json={"rows": [{"id": 1, "amount": 1}]}
).status_code == 200
trend = client.get(base + "/evaluations/trend")
assert trend.status_code == 200, trend.text
print(trend.text, end="")
"""

_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
trend = client.get(
    "/datasets/orders/versions/1/quality-rules/evaluations/trend"
)
assert trend.status_code == 200, trend.text
body = trend.json()
assert list(body) == ["dataset", "version", "evaluations", "rules", "totals"]
assert body["dataset"] == "orders"
assert body["version"] == 1
assert [e["violation_row_count"] for e in body["evaluations"]] == [2, 0]
assert body["evaluations"][0]["violation_row_count_delta"] is None
assert body["evaluations"][1]["violation_row_count_delta"] == -2
assert [r["rule_id"] for r in body["rules"]] == sorted(
    r["rule_id"] for r in body["rules"]
)
assert body["totals"] == {
    "evaluation_count": 2,
    "violation_row_count": 2,
    "rule_count": 2,
}
assert trend.text.endswith("\\n")
print(trend.text, end="")
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
    return result.stdout


def test_trend_survives_a_process_restart_byte_for_byte(tmp_path: Path) -> None:
    db_path = tmp_path / "quality-evaluation-trend.db"
    created = _run_script(db_path, _CREATE_AND_EVALUATE_SCRIPT)

    # A brand-new interpreter recomputes the trend from the persisted history
    # and the documents match verbatim.
    assert _run_script(db_path, _VERIFY_SCRIPT) == created
