"""Tests for the read-only quality rule evaluation history trend."""

from __future__ import annotations

import json
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


def get_trend(client: TestClient, *path: object) -> dict:
    url = trend_path(*path) if path else trend_path()  # type: ignore[arg-type]
    response = client.get(url)
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Empty history
# --------------------------------------------------------------------------- #


def test_trend_with_empty_history_is_an_explicit_empty_result(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)

    body = get_trend(client)
    assert body == {
        "dataset": "orders",
        "version": 1,
        "evaluations": [],
        "rules": [],
        "totals": {
            "evaluation_count": 0,
            "violation_row_count": 0,
            "rule_count": 0,
        },
    }


def test_trend_empty_history_does_not_list_registered_but_never_run_rules(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )

    # No recorded evaluation means no rule result to aggregate, so the rule
    # collection is empty even though a rule is registered.
    body = get_trend(client)
    assert body["evaluations"] == []
    assert body["rules"] == []
    assert body["totals"] == {
        "evaluation_count": 0,
        "violation_row_count": 0,
        "rule_count": 0,
    }


def test_trend_lists_a_rule_that_ran_but_always_passed_with_zero_counts(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    # The rule runs in recorded evaluations but never violates.
    evaluate(client, [{"id": 1}])
    evaluate(client, [])

    body = get_trend(client)
    assert body["rules"] == [
        {
            "rule_id": rule["id"],
            "name": "r",
            "violation_row_count": 0,
            "evaluation_count": 0,
            "first_violation_sequence": None,
            "last_violation_sequence": None,
        }
    ]
    assert body["totals"]["rule_count"] == 0


# --------------------------------------------------------------------------- #
# Evaluation series and per-evaluation deltas
# --------------------------------------------------------------------------- #


def test_trend_lists_every_evaluation_in_sequence_order_with_history_keys(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": None}, {"id": None}, {"id": None}])
    evaluate(client, [{"id": 1}])

    body = get_trend(client)
    assert [entry["sequence"] for entry in body["evaluations"]] == [1, 2, 3]
    assert [entry["row_count"] for entry in body["evaluations"]] == [1, 3, 1]
    assert [entry["violation_row_count"] for entry in body["evaluations"]] == [
        1,
        3,
        0,
    ]
    # First delta is null (the key is kept), then the changes against the
    # previous evaluation (positive and negative).
    assert [entry["violation_row_count_delta"] for entry in body["evaluations"]] == [
        None,
        2,
        -3,
    ]
    for entry in body["evaluations"]:
        assert set(entry) == {
            "sequence",
            "created_at",
            "row_count",
            "violation_row_count",
            "violation_row_count_delta",
        }
        datetime.fromisoformat(entry["created_at"])


def test_trend_delta_is_zero_for_equal_consecutive_violation_counts(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}, {"id": 1}])
    evaluate(client, [{"id": None}, {"id": 2}])

    deltas = [
        entry["violation_row_count_delta"] for entry in get_trend(client)["evaluations"]
    ]
    assert deltas == [None, 0]


def test_trend_zero_violation_evaluations_take_part_in_the_series(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [])  # recorded with zero violations
    evaluate(client, [{"id": 1}])

    entries = get_trend(client)["evaluations"]
    assert [entry["row_count"] for entry in entries] == [0, 1]
    assert [entry["violation_row_count"] for entry in entries] == [0, 0]
    assert [entry["violation_row_count_delta"] for entry in entries] == [None, 0]


# --------------------------------------------------------------------------- #
# Per-rule aggregates
# --------------------------------------------------------------------------- #


def test_trend_aggregates_cumulative_violations_counts_and_sequences_per_rule(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    id_rule = create_rule(
        client, {"name": "id required", "kind": "not_null", "params": {"field": "id"}}
    )
    amount_rule = create_rule(
        client,
        {"name": "amount set", "kind": "not_null", "params": {"field": "amount"}},
    )

    # Seq 1: id misses on row 1, amount misses on rows 0 and 1.
    evaluate(client, [{"id": 1}, {}, {"id": 3, "amount": 3}])
    # Seq 2: id passes everywhere; amount misses on row 0 (1 violation).
    evaluate(client, [{"id": 1}, {"id": 2, "amount": 2}])
    # Seq 3: both pass.
    evaluate(client, [{"id": 1, "amount": 1}])

    rules = {rule["rule_id"]: rule for rule in get_trend(client)["rules"]}

    assert rules[id_rule["id"]] == {
        "rule_id": id_rule["id"],
        "name": "id required",
        "violation_row_count": 1,
        "evaluation_count": 1,
        "first_violation_sequence": 1,
        "last_violation_sequence": 1,
    }
    assert rules[amount_rule["id"]] == {
        "rule_id": amount_rule["id"],
        "name": "amount set",
        # 2 violating rows at seq 1 plus 1 at seq 2: cumulative, not unioned.
        "violation_row_count": 3,
        "evaluation_count": 2,
        "first_violation_sequence": 1,
        "last_violation_sequence": 2,
    }
    assert [rule["rule_id"] for rule in get_trend(client)["rules"]] == sorted(
        rule["rule_id"] for rule in get_trend(client)["rules"]
    )


def test_trend_includes_a_rule_that_never_violated_with_null_sequences(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    bad = create_rule(
        client, {"name": "bad", "kind": "not_null", "params": {"field": "id"}}
    )
    clean = create_rule(
        client,
        {"name": "clean", "kind": "not_null", "params": {"field": "amount"}},
    )

    # id (bad) violates; amount is always present so clean never does.
    evaluate(client, [{"id": None, "amount": 1}, {"id": None, "amount": 2}])
    evaluate(client, [{"id": None, "amount": 3}])

    rules = {rule["rule_id"]: rule for rule in get_trend(client)["rules"]}
    assert rules[bad["id"]]["violation_row_count"] == 3
    assert rules[bad["id"]]["evaluation_count"] == 2
    assert rules[clean["id"]] == {
        "rule_id": clean["id"],
        "name": "clean",
        "violation_row_count": 0,
        "evaluation_count": 0,
        "first_violation_sequence": None,
        "last_violation_sequence": None,
    }


def test_trend_keeps_a_rule_disabled_after_violating_and_counts_its_history(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    paused = create_rule(
        client, {"name": "paused", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])
    assert (
        client.patch(f"{rules_path()}/{paused['id']}", json={"enabled": False}).status_code
        == 200
    )
    # The disabled rule no longer runs, so it is absent from later results.
    evaluate(client, [{"id": None}])

    rule = {rule["rule_id"]: rule for rule in get_trend(client)["rules"]}[paused["id"]]
    assert rule["name"] == "paused"
    assert rule["violation_row_count"] == 1
    assert rule["evaluation_count"] == 1
    assert rule["first_violation_sequence"] == 1
    assert rule["last_violation_sequence"] == 1


def test_trend_omits_a_rule_created_after_the_last_evaluation(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    early = create_rule(
        client, {"name": "early", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])
    late = create_rule(
        client, {"name": "late", "kind": "not_null", "params": {"field": "amount"}}
    )

    # Only 'early' has results in recorded evaluations; 'late' never ran.
    trend = get_trend(client)
    assert [rule["rule_id"] for rule in trend["rules"]] == [early["id"]]
    assert late["id"] not in {rule["rule_id"] for rule in trend["rules"]}


def test_trend_tracks_a_rule_that_starts_violating_only_later(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": 1}])
    evaluate(client, [{"id": 1}])
    evaluate(client, [{"id": None}])

    aggregate = get_trend(client)["rules"][0]
    assert aggregate["rule_id"] == rule["id"]
    assert aggregate["violation_row_count"] == 1
    assert aggregate["evaluation_count"] == 1
    assert aggregate["first_violation_sequence"] == 3
    assert aggregate["last_violation_sequence"] == 3


def test_trend_omits_a_rule_disabled_before_it_ever_ran(client: TestClient) -> None:
    make_dataset_with_version(client)
    runner = create_rule(
        client, {"name": "runner", "kind": "not_null", "params": {"field": "id"}}
    )
    dormant = create_rule(
        client, {"name": "dormant", "kind": "not_null", "params": {"field": "amount"}}
    )
    assert (
        client.patch(
            f"{rules_path()}/{dormant['id']}", json={"enabled": False}
        ).status_code
        == 200
    )
    # Only the enabled rule has results; the disabled rule never ran even once.
    evaluate(client, [{"id": None}, {"id": 2}])

    trend = get_trend(client)
    assert [rule["rule_id"] for rule in trend["rules"]] == [runner["id"]]
    assert dormant["id"] not in {rule["rule_id"] for rule in trend["rules"]}


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #


def test_trend_totals_count_evaluations_summed_violation_rows_and_involved_rules(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "a", "kind": "not_null", "params": {"field": "id"}}
    )
    create_rule(
        client, {"name": "b", "kind": "not_null", "params": {"field": "amount"}}
    )
    create_rule(
        client, {"name": "clean", "kind": "not_null", "params": {"field": "region"}}
    )

    # Seq 1: two rows each missing both id and amount, so the distinct
    # violating rows of the evaluation are 2.
    evaluate(client, [{"region": "eu"}, {"region": "us"}])
    # Seq 2: 1 violating row (id misses); the clean rule never contributes.
    evaluate(client, [{"id": None, "amount": 1, "region": "eu"}])

    totals = get_trend(client)["totals"]
    assert totals == {
        "evaluation_count": 2,
        # Sum of the per-evaluation violation row counts: 2 + 1.
        "violation_row_count": 3,
        # Only rules with at least one violation; 'clean' is not involved.
        "rule_count": 2,
    }


# --------------------------------------------------------------------------- #
# Determinism, scoping and read-only behavior
# --------------------------------------------------------------------------- #


def test_trend_document_is_compact_with_fixed_key_order_and_one_newline(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])

    response = client.get(trend_path())
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    raw = response.content
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")
    # Compact whitespace: no spaces after the JSON separators.
    assert b", " not in raw
    assert b": " not in raw

    document = json.loads(raw)
    assert list(document) == ["dataset", "version", "evaluations", "rules", "totals"]
    assert list(document["totals"]) == [
        "evaluation_count",
        "violation_row_count",
        "rule_count",
    ]
    assert list(document["evaluations"][0]) == [
        "sequence",
        "created_at",
        "row_count",
        "violation_row_count",
        "violation_row_count_delta",
    ]
    assert list(document["rules"][0]) == [
        "rule_id",
        "name",
        "violation_row_count",
        "evaluation_count",
        "first_violation_sequence",
        "last_violation_sequence",
    ]
    assert document["rules"][0]["rule_id"] == rule["id"]


def test_trend_repeated_reads_are_identical_and_write_nothing(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])

    first = client.get(trend_path())
    for _ in range(3):
        again = client.get(trend_path())
        assert again.content == first.content

    assert len(client.get(history_path()).json()) == 1


def test_trend_is_scoped_to_its_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])
    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "id", "type": "bigint", "nullable": False}]},
        ).status_code
        == 201
    )

    other = get_trend(client, "orders", 2)
    assert other["evaluations"] == []
    assert other["rules"] == []
    assert other["totals"]["evaluation_count"] == 0

    this = get_trend(client, "orders", 1)
    assert len(this["evaluations"]) == 1


# --------------------------------------------------------------------------- #
# Method, path and request shape
# --------------------------------------------------------------------------- #


def test_trend_accepts_get_only(client: TestClient) -> None:
    make_dataset_with_version(client)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, trend_path(), json={})
        assert response.status_code == 405, method


def test_trend_unknown_dataset_or_version_returns_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    for url in (trend_path("ghost"), trend_path("orders", 9)):
        response = client.get(url)
        assert response.status_code == 404
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "not_found"


def test_trend_rejects_body_bytes_and_query_params_with_422(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])

    cases = [
        client.request("GET", trend_path(), content=b"{}"),
        client.request("GET", trend_path(), content=b" "),
        client.request("GET", trend_path(), content=b"\n\t "),
        client.get(trend_path(), params={"limit": 1}),
        client.get(trend_path(), params={"full": "true", "x": "y"}),
    ]
    for response in cases:
        assert response.status_code == 422, response.text
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]

    # The rejected reads wrote nothing.
    assert len(client.get(history_path()).json()) == 1


def test_trend_404_takes_precedence_over_body_and_query_shape(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    assert (
        client.request("GET", trend_path("ghost"), content=b"{}").status_code == 404
    )
    assert (
        client.request("GET", trend_path("orders", 9), content=b" ").status_code
        == 404
    )
    assert client.get(trend_path("ghost"), params={"x": "1"}).status_code == 404


# --------------------------------------------------------------------------- #
# Pairwise diff also rejects body bytes (whitespace-only included)
# --------------------------------------------------------------------------- #


def test_pairwise_diff_rejects_any_body_bytes(client: TestClient) -> None:
    make_dataset_with_version(client)
    evaluate(client, [])
    evaluate(client, [])
    diff_url = history_path() + "/diff"

    assert client.request("GET", diff_url, content=b"{}").status_code == 422
    assert client.request("GET", diff_url, content=b" ").status_code == 422
    assert client.request("GET", diff_url, content=b"\n").status_code == 422
    assert client.get(diff_url).status_code == 200
    assert client.get(diff_url, params={"x": "1"}).status_code == 422
    # 404 still precedes the shape checks.
    assert (
        client.request(
            "GET", history_path("ghost") + "/diff", content=b" "
        ).status_code
        == 404
    )


def test_point_in_time_diff_rejects_whitespace_body_bytes(client: TestClient) -> None:
    make_dataset_with_version(client)
    evaluate(client, [])
    url = history_path() + "/diff/at"

    assert (
        client.request("GET", url, params={"timestamp": "2026-01-01T00:00:00Z"},
                       content=b" ").status_code
        == 422
    )
    assert (
        client.get(url, params={"timestamp": "2026-01-01T00:00:00Z", "x": "1"}).status_code
        == 422
    )


# --------------------------------------------------------------------------- #
# Persistence across process restarts
# --------------------------------------------------------------------------- #


_CREATE_SCRIPT = """
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
first = client.post(
    "/datasets/orders/versions/1/quality-rules",
    json={"name": "id required", "kind": "not_null", "params": {"field": "id"}},
)
assert first.status_code == 201, first.text
second = client.post(
    "/datasets/orders/versions/1/quality-rules",
    json={"name": "amount set", "kind": "not_null", "params": {"field": "amount"}},
)
assert second.status_code == 201, second.text

def evaluate(rows):
    response = client.post(
        "/datasets/orders/versions/1/quality-rules/evaluate",
        json={"rows": rows},
    )
    assert response.status_code == 200, response.text

evaluate([{"id": None, "amount": 1}, {"id": 2, "amount": 2}])
evaluate([{"id": 1, "amount": None}, {"id": 2, "amount": 2}])
evaluate([{"id": 1, "amount": 1}, {"id": 2, "amount": 2}])
print("created")
"""

_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

response = client.get(
    "/datasets/orders/versions/1/quality-rules/evaluations/trend"
)
assert response.status_code == 200, response.text
raw = response.content
assert raw.endswith(b"\\n") and not raw.endswith(b"\\n\\n")
body = response.json()
assert list(body) == ["dataset", "version", "evaluations", "rules", "totals"]
assert body["dataset"] == "orders"
assert body["version"] == 1

entries = body["evaluations"]
assert [entry["sequence"] for entry in entries] == [1, 2, 3]
assert [entry["row_count"] for entry in entries] == [2, 2, 2]
assert [entry["violation_row_count"] for entry in entries] == [1, 1, 0]
assert [entry["violation_row_count_delta"] for entry in entries] == [None, 0, -1]

rules = body["rules"]
assert [rule["name"] for rule in rules] == ["id required", "amount set"]
id_rule = next(rule for rule in rules if rule["name"] == "id required")
amount_rule = next(rule for rule in rules if rule["name"] == "amount set")
assert id_rule["violation_row_count"] == 1
assert id_rule["evaluation_count"] == 1
assert (id_rule["first_violation_sequence"],
        id_rule["last_violation_sequence"]) == (1, 1)
assert amount_rule["violation_row_count"] == 1
assert amount_rule["evaluation_count"] == 1
assert (amount_rule["first_violation_sequence"],
        amount_rule["last_violation_sequence"]) == (2, 2)

assert body["totals"] == {
    "evaluation_count": 3,
    "violation_row_count": 2,
    "rule_count": 2,
}

# Repeated reads in the same process are byte-identical and write nothing.
again = client.get(
    "/datasets/orders/versions/1/quality-rules/evaluations/trend"
)
assert again.content == raw
assert len(client.get(
    "/datasets/orders/versions/1/quality-rules/evaluations"
).json()) == 3
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


def test_trend_is_byte_identical_across_a_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "quality-trend.db"
    assert _run_script(db_path, _CREATE_SCRIPT) == "created"
    assert _run_script(db_path, _VERIFY_SCRIPT) == "verified"
