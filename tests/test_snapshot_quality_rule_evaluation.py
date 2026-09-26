"""Tests for evaluating quality rules over a persisted snapshot."""

from __future__ import annotations

import threading
from datetime import datetime

from fastapi.testclient import TestClient


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


def snapshots_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/snapshots"


def evaluate_path(snapshot_id: int, dataset: str = "orders", version: int = 1) -> str:
    return f"{snapshots_path(dataset, version)}/{snapshot_id}/quality-rules/evaluate"


def create_rule(client: TestClient, payload: dict, **path: object) -> dict:
    response = client.post(rules_path(**path), json=payload)  # type: ignore[arg-type]
    assert response.status_code == 201, response.text
    return response.json()


def create_snapshot(client: TestClient, rows: list[dict], **path: object) -> dict:
    response = client.post(
        snapshots_path(**path), json={"rows": rows}  # type: ignore[arg-type]
    )
    assert response.status_code == 201, response.text
    return response.json()


def evaluate_snapshot(client: TestClient, snapshot_id: int, **path: object) -> dict:
    response = client.post(evaluate_path(snapshot_id, **path))  # type: ignore[arg-type]
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Evaluation semantics
# --------------------------------------------------------------------------- #


def test_snapshot_rows_are_evaluated_with_the_same_semantics(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    not_null = create_rule(
        client, {"name": "id required", "kind": "not_null", "params": {"field": "id"}}
    )
    ranged = create_rule(
        client,
        {
            "name": "amount range",
            "kind": "numeric_range",
            "params": {"field": "amount", "min": 0, "max": 10},
        },
    )
    unique = create_rule(
        client, {"name": "id unique", "kind": "unique", "params": {"fields": ["id"]}}
    )
    snapshot = create_snapshot(
        client,
        [
            {"id": 1, "amount": 0, "region": "eu"},  # boundary min: passes
            {"id": None, "amount": 10},  # null id; boundary max: passes
            {"amount": 11},  # missing id; out of range
            {"id": None, "amount": 5},  # null id; duplicate null id with row 1
        ],
    )

    result = evaluate_snapshot(client, snapshot["id"])

    assert result["dataset"] == "orders"
    assert result["version"] == 1
    assert result["results"] == [
        {
            "rule_id": not_null["id"],
            "name": "id required",
            "passed": False,
            "violations": [1, 2, 3],
        },
        {
            "rule_id": ranged["id"],
            "name": "amount range",
            "passed": False,
            "violations": [2],
        },
        {
            "rule_id": unique["id"],
            "name": "id unique",
            "passed": False,
            # Rows 1, 2 (missing id) and 3 all count as null and duplicate.
            "violations": [1, 2, 3],
        },
    ]


def test_results_are_sorted_by_rule_id_and_match_the_row_evaluation_shape(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "a", "kind": "not_null", "params": {"field": "id"}})
    create_rule(
        client, {"name": "b", "kind": "not_null", "params": {"field": "amount"}}
    )
    rows = [{"id": None, "amount": 1}, {"id": 1}]
    snapshot = create_snapshot(client, rows)

    from_snapshot = evaluate_snapshot(client, snapshot["id"])
    from_rows = client.post(rules_path() + "/evaluate", json={"rows": rows}).json()

    assert list(from_snapshot) == ["dataset", "version", "results"]
    assert from_snapshot["results"] == from_rows["results"]
    assert [r["rule_id"] for r in from_snapshot["results"]] == sorted(
        r["rule_id"] for r in from_snapshot["results"]
    )


def test_disabled_rules_do_not_run_and_are_not_modified(client: TestClient) -> None:
    make_dataset_with_version(client)
    enabled = create_rule(
        client, {"name": "on", "kind": "not_null", "params": {"field": "id"}}
    )
    disabled = create_rule(
        client, {"name": "off", "kind": "not_null", "params": {"field": "amount"}}
    )
    assert (
        client.patch(
            f"{rules_path()}/{disabled['id']}", json={"enabled": False}
        ).status_code
        == 200
    )
    snapshot = create_snapshot(client, [{"id": None, "amount": None}])

    result = evaluate_snapshot(client, snapshot["id"])

    assert [r["rule_id"] for r in result["results"]] == [enabled["id"]]
    rules = {rule["id"]: rule for rule in client.get(rules_path()).json()}
    assert rules[disabled["id"]]["enabled"] is False
    assert rules[enabled["id"]]["enabled"] is True


def test_evaluation_does_not_modify_the_snapshot(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    rows = [{"id": None, "region": "eu"}, {"id": 1}]
    snapshot = create_snapshot(client, rows)
    before = client.get(f"{snapshots_path()}/{snapshot['id']}").json()

    evaluate_snapshot(client, snapshot["id"])

    after = client.get(f"{snapshots_path()}/{snapshot['id']}").json()
    assert after == before
    assert after["rows"] == rows
    assert client.get(snapshots_path()).json()[0]["row_count"] == 2


def test_empty_snapshot_evaluates_successfully_with_zero_violations(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    first = create_rule(
        client, {"name": "a", "kind": "not_null", "params": {"field": "id"}}
    )
    second = create_rule(
        client, {"name": "b", "kind": "unique", "params": {"fields": ["id"]}}
    )
    snapshot = create_snapshot(client, [])

    result = evaluate_snapshot(client, snapshot["id"])

    assert result["results"] == [
        {"rule_id": first["id"], "name": "a", "passed": True, "violations": []},
        {"rule_id": second["id"], "name": "b", "passed": True, "violations": []},
    ]
    records = client.get(history_path()).json()
    assert len(records) == 1
    assert records[0]["row_count"] == 0
    assert records[0]["violation_row_count"] == 0


# --------------------------------------------------------------------------- #
# History, diff and anomaly detection integration
# --------------------------------------------------------------------------- #


def test_successful_snapshot_evaluation_appends_a_history_record(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "id required", "kind": "not_null", "params": {"field": "id"}}
    )
    snapshot = create_snapshot(client, [{"id": 1}, {"id": None}, {"amount": 3}])

    evaluate_snapshot(client, snapshot["id"])

    records = client.get(history_path()).json()
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


def test_snapshot_and_row_evaluations_share_one_contiguous_sequence(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    snapshot = create_snapshot(client, [{"id": None}, {"id": 2}])

    client.post(rules_path() + "/evaluate", json={"rows": [{"id": 1}]})
    evaluate_snapshot(client, snapshot["id"])
    client.post(rules_path() + "/evaluate", json={"rows": []})

    records = client.get(history_path()).json()
    assert [r["sequence"] for r in records] == [1, 2, 3]
    assert [r["row_count"] for r in records] == [1, 2, 0]
    assert [r["violation_row_count"] for r in records] == [0, 1, 0]


def test_concurrent_snapshot_evaluations_have_unique_contiguous_sequences(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    snapshot = create_snapshot(client, [{"id": None}])

    failures: list[object] = []

    def submit() -> None:
        try:
            response = client.post(evaluate_path(snapshot["id"]))
            assert response.status_code == 200, response.text
        except BaseException as exc:  # pragma: no cover - failure reporting
            failures.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures
    records = client.get(history_path()).json()
    assert len(records) == 12
    assert [r["sequence"] for r in records] == list(range(1, 13))


def test_snapshot_evaluations_feed_the_evaluation_diff(client: TestClient) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    first = create_snapshot(client, [{"id": None}, {"id": 1}, {"id": None}])
    second = create_snapshot(client, [{"id": 1}, {"id": None}])

    evaluate_snapshot(client, first["id"])
    evaluate_snapshot(client, second["id"])

    diff = client.get(diff_path()).json()
    assert (diff["from_sequence"], diff["to_sequence"]) == (1, 2)
    assert diff["added_violation_rows"] == [1]
    assert diff["removed_violation_rows"] == [0, 2]
    assert diff["rules"] == [
        {
            "rule_id": rule["id"],
            "name": "r",
            "before": {"violation_count": 2, "violations": [0, 2]},
            "after": {"violation_count": 1, "violations": [1]},
            "added_violations": [1],
            "removed_violations": [0, 2],
            "violation_count_delta": -1,
        }
    ]


def test_snapshot_evaluations_feed_anomaly_detection(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    response = client.post(
        rules_path() + "/anomaly-detection",
        json={
            "consecutive_worsening_steps": 2,
            "violation_row_limit": 0,
            "rule_violation_limit": 1000,
        },
    )
    assert response.status_code == 201, response.text
    first = create_snapshot(client, [{"id": None}])
    second = create_snapshot(client, [{"id": None}, {"id": None}])

    evaluate_snapshot(client, first["id"])
    evaluate_snapshot(client, second["id"])

    scan = client.post(rules_path() + "/anomaly-detection/scan")
    assert scan.status_code == 200, scan.text
    anomalies = client.get(rules_path() + "/anomaly-detection/anomalies").json()
    assert len(anomalies) == len(scan.json()) > 0
    # Both snapshot evaluations are part of the scanned history.
    assert {a["sequence"] for a in anomalies} == {1, 2}


def test_snapshot_evaluation_history_survives_a_restart(client: TestClient) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    snapshot = create_snapshot(client, [{"id": None}])
    first = evaluate_snapshot(client, snapshot["id"])

    from app.main import app

    restarted = TestClient(app)
    records = restarted.get(history_path()).json()
    assert len(records) == 1
    assert records[0]["results"] == first["results"]
    assert records[0]["results"][0]["rule_id"] == rule["id"]
    # Evaluating again after the restart continues the sequence.
    evaluate_snapshot(restarted, snapshot["id"])
    assert [r["sequence"] for r in restarted.get(history_path()).json()] == [1, 2]


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


def test_unknown_dataset_version_or_snapshot_returns_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    snapshot = create_snapshot(client, [{"id": 1}])

    assert client.post(evaluate_path(snapshot["id"], dataset="ghost")).status_code == 404
    assert client.post(evaluate_path(snapshot["id"], version=9)).status_code == 404
    assert client.post(evaluate_path(9999)).status_code == 404
    for response in (
        client.post(evaluate_path(snapshot["id"], dataset="ghost")),
        client.post(evaluate_path(9999)),
    ):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "not_found"

    assert client.get(history_path()).json() == []


def test_snapshot_owned_by_another_version_is_422_and_writes_nothing(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    snapshot = create_snapshot(client, [{"id": None}])
    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "id", "type": "bigint", "nullable": False}]},
        ).status_code
        == 201
    )

    response = client.post(evaluate_path(snapshot["id"], version=2))

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get(history_path("orders", 2)).json() == []
    assert client.get(history_path()).json() == []


def test_body_and_query_params_are_422_and_write_nothing(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    snapshot = create_snapshot(client, [{"id": None}])

    with_body = client.post(evaluate_path(snapshot["id"]), content=b"{}")
    with_json = client.post(evaluate_path(snapshot["id"]), json={"rows": []})
    with_query = client.post(evaluate_path(snapshot["id"]), params={"full": "1"})
    assert with_body.status_code == 422
    assert with_json.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, with_json, with_query):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"

    assert client.get(history_path()).json() == []


def test_404_takes_precedence_over_shape_checks(client: TestClient) -> None:
    make_dataset_with_version(client)
    snapshot = create_snapshot(client, [{"id": 1}])

    assert (
        client.post(evaluate_path(snapshot["id"], dataset="ghost"), content=b"{}").status_code
        == 404
    )
    assert (
        client.post(evaluate_path(9999), params={"x": "1"}).status_code == 404
    )
    assert (
        client.post(evaluate_path(snapshot["id"], version=9), content=b"{}").status_code
        == 404
    )
