"""Tests for the quality rule evaluation history and the read-only diff."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.db import db_session


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


def diff_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{history_path(dataset, version)}/diff"


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


# --------------------------------------------------------------------------- #
# History recording
# --------------------------------------------------------------------------- #


def test_successful_evaluation_appends_a_history_record(client: TestClient) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "id required", "kind": "not_null", "params": {"field": "id"}}
    )

    evaluate(client, [{"id": 1}, {"id": None}, {"amount": 3}])

    history = client.get(history_path())
    assert history.status_code == 200, history.text
    records = history.json()
    assert len(records) == 1
    record = records[0]
    assert set(record) == {
        "sequence",
        "dataset",
        "version",
        "row_count",
        "violation_row_count",
        "results",
        "created_at",
    }
    assert record["sequence"] == 1
    assert record["dataset"] == "orders"
    assert record["version"] == 1
    assert record["row_count"] == 3
    # Rows 1 and 2 violate the only rule; the count is over distinct rows.
    assert record["violation_row_count"] == 2
    assert record["results"] == [
        {
            "rule_id": rule["id"],
            "name": "id required",
            "passed": False,
            "violations": [1, 2],
        }
    ]
    datetime.fromisoformat(record["created_at"])


def test_history_is_ordered_by_occurrence_and_sequences_are_contiguous(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": 1}])
    evaluate(client, [{"id": None}, {"id": 2}])
    evaluate(client, [])

    records = client.get(history_path()).json()
    assert [r["sequence"] for r in records] == [1, 2, 3]
    assert [r["row_count"] for r in records] == [1, 2, 0]
    assert [r["violation_row_count"] for r in records] == [0, 1, 0]


def test_empty_row_set_is_recorded_with_zero_violations(client: TestClient) -> None:
    make_dataset_with_version(client)
    first = create_rule(
        client, {"name": "a", "kind": "not_null", "params": {"field": "id"}}
    )
    second = create_rule(
        client, {"name": "b", "kind": "unique", "params": {"fields": ["id"]}}
    )

    evaluate(client, [])

    records = client.get(history_path()).json()
    assert len(records) == 1
    record = records[0]
    assert record["row_count"] == 0
    assert record["violation_row_count"] == 0
    assert record["results"] == [
        {"rule_id": first["id"], "name": "a", "passed": True, "violations": []},
        {"rule_id": second["id"], "name": "b", "passed": True, "violations": []},
    ]


def test_single_row_and_heavily_duplicated_rows_are_counted_accurately(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "u", "kind": "unique", "params": {"fields": ["id"]}}
    )

    evaluate(client, [{"id": 7}])
    evaluate(client, [{"id": 1}] * 50)

    records = client.get(history_path()).json()
    assert [r["row_count"] for r in records] == [1, 50]
    assert [r["violation_row_count"] for r in records] == [0, 50]
    assert records[1]["results"][0]["violations"] == list(range(50))


def test_violation_row_count_unions_distinct_rows_across_rules(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "a", "kind": "not_null", "params": {"field": "id"}}
    )
    create_rule(
        client, {"name": "b", "kind": "not_null", "params": {"field": "amount"}}
    )

    # Row 0 violates both rules, row 1 violates only the second.
    evaluate(client, [{"region": "eu"}, {"id": 1}])

    record = client.get(history_path()).json()[0]
    assert record["violation_row_count"] == 2


def test_rejected_or_failed_evaluation_leaves_no_record(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )

    # Structurally invalid payloads (422) and unknown paths (404).
    assert client.post(rules_path() + "/evaluate", json={}).status_code == 422
    assert (
        client.post(rules_path() + "/evaluate", json={"rows": {"id": 1}}).status_code
        == 422
    )
    assert (
        client.post(rules_path("ghost") + "/evaluate", json={"rows": []}).status_code
        == 404
    )
    assert (
        client.post(rules_path("orders", 9) + "/evaluate", json={"rows": []}).status_code
        == 404
    )

    assert client.get(history_path()).json() == []


def test_history_does_not_change_the_evaluate_response(client: TestClient) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )

    response = client.post(rules_path() + "/evaluate", json={"rows": [{"id": None}]})

    assert response.status_code == 200
    assert response.json() == {
        "dataset": "orders",
        "version": 1,
        "results": [
            {
                "rule_id": rule["id"],
                "name": "r",
                "passed": False,
                "violations": [0],
            }
        ],
    }


def test_history_survives_a_restart(client: TestClient, tmp_path) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])

    # A brand-new client (fresh app instance against the same database file)
    # sees the same history and diff.
    from app.main import app

    restarted = TestClient(app)
    records = restarted.get(history_path()).json()
    assert len(records) == 1
    assert records[0]["violation_row_count"] == 1

    evaluate(restarted, [{"id": 1}])
    diff = restarted.get(diff_path()).json()
    assert diff["from_sequence"] == 1
    assert diff["to_sequence"] == 2
    assert diff["removed_violation_rows"] == [0]


def test_history_records_are_immutable(client: TestClient) -> None:
    make_dataset_with_version(client)
    evaluate(client, [])

    with db_session() as conn:
        with pytest.raises(sqlite3.Error):
            conn.execute("UPDATE quality_rule_evaluations SET row_count = 5")
        conn.rollback()
        with pytest.raises(sqlite3.Error):
            conn.execute("DELETE FROM quality_rule_evaluations")
        conn.rollback()

    assert len(client.get(history_path()).json()) == 1


def test_history_is_scoped_to_its_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    evaluate(client, [{"id": 1}])
    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "id", "type": "bigint", "nullable": False}]},
        ).status_code
        == 201
    )

    assert client.get(history_path("orders", 2)).json() == []
    evaluate(client, [{"id": 1}, {"id": 2}], dataset="orders", version=2)
    assert len(client.get(history_path("orders", 2)).json()) == 1
    # The other version's history is untouched.
    assert len(client.get(history_path()).json()) == 1


def test_history_unknown_dataset_or_version_returns_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert client.get(history_path("ghost")).status_code == 404
    assert client.get(history_path("orders", 9)).status_code == 404
    assert client.get(diff_path("ghost")).status_code == 404
    assert client.get(diff_path("orders", 9)).status_code == 404


def test_history_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset_with_version(client)
    evaluate(client, [])

    with_body = client.request("GET", history_path(), content=b"{}")
    with_query = client.get(history_path(), params={"limit": 1})
    assert with_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, with_query):
        assert response.json()["error"] == "validation_error"

    # 404 takes precedence over the shape checks.
    assert (
        client.request("GET", history_path("ghost"), content=b"{}").status_code
        == 404
    )
    assert client.get(history_path("ghost"), params={"x": "1"}).status_code == 404


def test_diff_rejects_body_and_query_params_without_writing(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    evaluate(client, [])
    evaluate(client, [])

    assert client.request("GET", diff_path(), content=b"{}").status_code == 422
    assert client.get(diff_path(), params={"full": "1"}).status_code == 422
    assert (
        client.request("GET", diff_path("ghost"), content=b"{}").status_code == 404
    )

    # Nothing was written by the rejected reads.
    assert len(client.get(history_path()).json()) == 2


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #


def test_diff_with_empty_history_is_an_explicit_empty_result(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    response = client.get(diff_path())
    assert response.status_code == 200
    assert response.json() == {
        "dataset": "orders",
        "version": 1,
        "from_sequence": None,
        "to_sequence": None,
        "added_violation_rows": [],
        "removed_violation_rows": [],
        "rules": [],
    }


def test_diff_with_a_single_record_is_an_explicit_empty_result(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    evaluate(client, [{"id": 1}])

    diff = client.get(diff_path()).json()
    assert diff["from_sequence"] is None
    assert diff["to_sequence"] is None
    assert diff["added_violation_rows"] == []
    assert diff["removed_violation_rows"] == []
    assert diff["rules"] == []


def test_diff_reports_new_and_disappeared_violating_rows_and_count_delta(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )

    evaluate(client, [{"id": None}, {"id": 1}, {"id": None}])  # violations 0, 2
    evaluate(client, [{"id": 1}, {"id": None}, {"id": None}, {"id": 2}])  # 1, 2

    diff = client.get(diff_path()).json()
    assert diff["from_sequence"] == 1
    assert diff["to_sequence"] == 2
    assert diff["added_violation_rows"] == [1]
    assert diff["removed_violation_rows"] == [0]
    assert diff["rules"] == [
        {
            "rule_id": rule["id"],
            "name": "r",
            "before": {"violation_count": 2, "violations": [0, 2]},
            "after": {"violation_count": 2, "violations": [1, 2]},
            "added_violations": [1],
            "removed_violations": [0],
            "violation_count_delta": 0,
        }
    ]


def test_diff_observes_increase_decrease_and_no_change(client: TestClient) -> None:
    make_dataset_with_version(client)
    up = create_rule(
        client, {"name": "up", "kind": "not_null", "params": {"field": "id"}}
    )
    down = create_rule(
        client, {"name": "down", "kind": "not_null", "params": {"field": "amount"}}
    )
    flat = create_rule(
        client, {"name": "flat", "kind": "not_null", "params": {"field": "region"}}
    )

    evaluate(
        client,
        [
            {"id": 1, "amount": None, "region": None},  # 0: down, flat
            {"id": 1, "amount": 1, "region": "eu"},  # 1: none
        ],
    )
    evaluate(
        client,
        [
            {"id": None, "amount": 1, "region": None},  # 0: up, flat
            {"id": None, "amount": 1, "region": "eu"},  # 1: up
        ],
    )

    diff = client.get(diff_path()).json()
    by_name = {rule["name"]: rule for rule in diff["rules"]}
    assert by_name["up"]["violation_count_delta"] == 2
    assert by_name["up"]["before"]["violation_count"] == 0
    assert by_name["up"]["after"]["violation_count"] == 2
    assert by_name["down"]["violation_count_delta"] == -1
    assert by_name["flat"]["violation_count_delta"] == 0
    assert by_name["flat"]["added_violations"] == []
    assert by_name["flat"]["removed_violations"] == []
    assert [r["rule_id"] for r in diff["rules"]] == sorted(
        r["rule_id"] for r in diff["rules"]
    )
    assert {up["id"], down["id"], flat["id"]} == {
        r["rule_id"] for r in diff["rules"]
    }


def test_diff_compares_the_latest_two_evaluations_only(client: TestClient) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )

    evaluate(client, [{"id": None}, {"id": None}, {"id": None}])
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": None}, {"id": 1}])

    diff = client.get(diff_path()).json()
    assert (diff["from_sequence"], diff["to_sequence"]) == (2, 3)
    assert diff["rules"][0]["rule_id"] == rule["id"]
    assert diff["rules"][0]["before"] == {
        "violation_count": 1,
        "violations": [0],
    }
    assert diff["rules"][0]["after"] == {
        "violation_count": 1,
        "violations": [0],
    }


def test_diff_handles_a_rule_disabled_between_evaluations(client: TestClient) -> None:
    make_dataset_with_version(client)
    kept = create_rule(
        client, {"name": "kept", "kind": "not_null", "params": {"field": "id"}}
    )
    paused = create_rule(
        client, {"name": "paused", "kind": "not_null", "params": {"field": "amount"}}
    )

    evaluate(client, [{"id": None, "amount": None}])
    assert (
        client.patch(
            f"{rules_path()}/{paused['id']}", json={"enabled": False}
        ).status_code
        == 200
    )
    evaluate(client, [{"id": None, "amount": None}])

    diff = client.get(diff_path()).json()
    by_name = {rule["name"]: rule for rule in diff["rules"]}
    missing = by_name["paused"]
    assert missing["rule_id"] == paused["id"]
    assert missing["before"] == {
        "violation_count": 1,
        "violations": [0],
    }
    assert missing["after"] is None
    assert missing["added_violations"] is None
    assert missing["removed_violations"] is None
    assert missing["violation_count_delta"] is None
    # The still-enabled rule is compared normally.
    assert by_name["kept"]["rule_id"] == kept["id"]
    assert by_name["kept"]["violation_count_delta"] == 0
    # The disabled rule's old violations do not count as disappeared rows of
    # the latest evaluation's actual results.
    assert diff["removed_violation_rows"] == []
    assert diff["added_violation_rows"] == []


def test_diff_handles_a_rule_created_between_evaluations(client: TestClient) -> None:
    make_dataset_with_version(client)
    evaluate(client, [{"id": None}])
    created = create_rule(
        client, {"name": "late", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])

    diff = client.get(diff_path()).json()
    assert diff["rules"] == [
        {
            "rule_id": created["id"],
            "name": "late",
            "before": None,
            "after": {"violation_count": 1, "violations": [0]},
            "added_violations": None,
            "removed_violations": None,
            "violation_count_delta": None,
        }
    ]
    # The latest evaluation's actual results drive the row-level sets.
    assert diff["added_violation_rows"] == [0]
    assert diff["removed_violation_rows"] == []


def test_diff_with_different_row_sets_uses_each_evaluations_own_indices(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )

    evaluate(client, [{"id": None}] * 3)  # violations 0, 1, 2
    evaluate(client, [{"id": None}])  # violation 0 only

    diff = client.get(diff_path()).json()
    assert diff["removed_violation_rows"] == [1, 2]
    assert diff["added_violation_rows"] == []
    assert diff["rules"][0]["violation_count_delta"] == -2


def test_diff_is_consistent_across_restarts(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": 1}])

    from app.main import app

    first = client.get(diff_path()).json()
    second = TestClient(app).get(diff_path()).json()
    assert first == second
    assert first["removed_violation_rows"] == [0]


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_evaluations_have_unique_contiguous_sequences(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )

    failures: list[object] = []

    def submit(rows: list[dict]) -> None:
        try:
            response = client.post(rules_path() + "/evaluate", json={"rows": rows})
            assert response.status_code == 200, response.text
        except BaseException as exc:  # pragma: no cover - failure reporting
            failures.append(exc)

    threads = [
        threading.Thread(target=submit, args=([{"id": index}],))
        for index in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures
    records = client.get(history_path()).json()
    assert len(records) == 12
    assert [r["sequence"] for r in records] == list(range(1, 13))
