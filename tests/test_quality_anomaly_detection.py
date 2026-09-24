"""Tests for quality anomaly detection over the evaluation history."""

from __future__ import annotations

import threading
from datetime import datetime

from fastapi.testclient import TestClient


BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "amount", "type": "decimal", "nullable": True},
]


def make_dataset_with_version(client: TestClient, name: str = "orders") -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions", json={"fields": BASE_FIELDS}
    )
    assert response.status_code == 201, response.text


def rules_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/quality-rules"


def config_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{rules_path(dataset, version)}/anomaly-detection"


def scan_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{config_path(dataset, version)}/scan"


def records_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{config_path(dataset, version)}/records"


def create_rule(client: TestClient, payload: dict, **path: object) -> dict:
    response = client.post(rules_path(**path), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def evaluate(client: TestClient, rows: list[dict], **path: object) -> dict:
    response = client.post(
        rules_path(**path) + "/evaluate",  # type: ignore[arg-type]
        json={"rows": rows},
    )
    assert response.status_code == 200, response.text
    return response.json()


def register_config(
    client: TestClient,
    *,
    steps: int = 2,
    row_limit: int = 100,
    rule_limit: int = 100,
    dataset: str = "orders",
    version: int = 1,
) -> dict:
    response = client.post(
        config_path(dataset, version),
        json={
            "consecutive_worsening_steps": steps,
            "violation_row_limit": row_limit,
            "rule_violation_limit": rule_limit,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def scan(client: TestClient, **path: object) -> list[dict]:
    response = client.post(scan_path(**path))
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Config registration
# --------------------------------------------------------------------------- #


def test_register_config_returns_the_config_content(client: TestClient) -> None:
    make_dataset_with_version(client)

    body = register_config(client, steps=3, row_limit=10, rule_limit=4)

    assert set(body) == {
        "id",
        "dataset",
        "version",
        "consecutive_worsening_steps",
        "violation_row_limit",
        "rule_violation_limit",
        "created_at",
    }
    assert isinstance(body["id"], int)
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["consecutive_worsening_steps"] == 3
    assert body["violation_row_limit"] == 10
    assert body["rule_violation_limit"] == 4
    datetime.fromisoformat(body["created_at"])

    fetched = client.get(config_path())
    assert fetched.status_code == 200
    assert fetched.json() == body


def test_register_config_twice_conflicts(client: TestClient) -> None:
    make_dataset_with_version(client)
    register_config(client, steps=2, row_limit=1, rule_limit=1)

    again = client.post(
        config_path(),
        json={
            "consecutive_worsening_steps": 4,
            "violation_row_limit": 9,
            "rule_violation_limit": 9,
        },
    )
    assert again.status_code == 409
    assert again.json()["error"] == "conflict"

    # The first config is untouched.
    assert client.get(config_path()).json()["consecutive_worsening_steps"] == 2


def test_config_is_scoped_to_its_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    register_config(client, steps=2)
    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "id", "type": "bigint", "nullable": False}]},
        ).status_code
        == 201
    )

    assert client.get(config_path("orders", 2)).status_code == 404
    assert client.post(scan_path("orders", 2)).status_code == 409
    register_config(client, steps=5, dataset="orders", version=2)
    assert (
        client.get(config_path("orders", 2)).json()["consecutive_worsening_steps"]
        == 5
    )
    assert client.get(config_path()).json()["consecutive_worsening_steps"] == 2


def test_get_config_without_registration_is_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert client.get(config_path()).status_code == 404


def test_config_unknown_dataset_or_version_returns_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    payload = {
        "consecutive_worsening_steps": 2,
        "violation_row_limit": 1,
        "rule_violation_limit": 1,
    }
    assert client.post(config_path("ghost"), json=payload).status_code == 404
    assert client.post(config_path("orders", 9), json=payload).status_code == 404
    assert client.get(config_path("ghost")).status_code == 404
    assert client.get(config_path("orders", 9)).status_code == 404
    assert client.post(scan_path("ghost")).status_code == 404
    assert client.post(scan_path("orders", 9)).status_code == 404
    assert client.get(records_path("ghost")).status_code == 404
    assert client.get(records_path("orders", 9)).status_code == 404


def test_config_rejects_invalid_payloads_without_writing(client: TestClient) -> None:
    make_dataset_with_version(client)
    valid = {
        "consecutive_worsening_steps": 2,
        "violation_row_limit": 1,
        "rule_violation_limit": 1,
    }

    invalid_payloads = [
        {},  # all fields missing
        {k: v for k, v in valid.items() if k != "violation_row_limit"},
        {**valid, "extra": 1},  # unknown additional field
        {**valid, "consecutive_worsening_steps": "2"},  # not an integer
        {**valid, "violation_row_limit": 1.5},
        {**valid, "rule_violation_limit": True},  # a boolean is not an integer
        {**valid, "consecutive_worsening_steps": 1},  # steps below 2
        {**valid, "consecutive_worsening_steps": 0},
        {**valid, "violation_row_limit": -1},  # negative limit
        {**valid, "rule_violation_limit": -2},
    ]
    for payload in invalid_payloads:
        response = client.post(config_path(), json=payload)
        assert response.status_code == 422, payload
        assert response.json()["error"] == "validation_error"

    # Nothing was registered by the rejected payloads.
    assert client.get(config_path()).status_code == 404

    # Zero limits and a step count of exactly 2 are accepted.
    register_config(client, steps=2, row_limit=0, rule_limit=0)


def test_scan_and_query_endpoints_reject_body_and_query_params(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    register_config(client)

    assert client.post(scan_path(), content=b"{}").status_code == 422
    assert client.post(scan_path(), params={"full": "1"}).status_code == 422
    assert client.request("GET", records_path(), content=b"{}").status_code == 422
    assert client.get(records_path(), params={"kind": "trend"}).status_code == 422
    assert client.request("GET", config_path(), content=b"{}").status_code == 422
    assert client.get(config_path(), params={"x": "1"}).status_code == 422

    # 404 takes precedence over the shape checks.
    assert client.post(scan_path("ghost"), content=b"{}").status_code == 404
    assert client.get(records_path("ghost"), params={"x": "1"}).status_code == 404


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #


def test_scan_without_config_conflicts(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])

    response = client.post(scan_path())
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    assert client.get(records_path()).json() == []


def test_scan_with_empty_history_succeeds_without_writing(client: TestClient) -> None:
    make_dataset_with_version(client)
    register_config(client, row_limit=0, rule_limit=0)

    assert scan(client) == []
    assert client.get(records_path()).json() == []


def test_records_are_empty_without_any_detection(client: TestClient) -> None:
    make_dataset_with_version(client)
    # No config registered yet: the read-only list is simply empty.
    assert client.get(records_path()).json() == []

    register_config(client)
    evaluate(client, [{"id": 1}])
    assert scan(client) == []
    assert client.get(records_path()).json() == []


def test_row_limit_exceeded_evaluations_are_flagged(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, row_limit=1)

    evaluate(client, [{"id": None}, {"id": None}])  # 2 violating rows > 1
    evaluate(client, [{"id": None}])  # 1 violating row: at the limit, not over
    evaluate(client, [{"id": None}] * 3)  # 3 > 1

    created = scan(client)
    assert [(r["kind"], r["evaluation_sequence"]) for r in created] == [
        ("row_limit_exceeded", 1),
        ("row_limit_exceeded", 3),
    ]
    for record, count in zip(created, (2, 3)):
        assert record["rule_id"] is None
        assert record["violation_count"] == count
        assert record["dataset"] == "orders"
        assert record["version"] == 1
        datetime.fromisoformat(record["created_at"])
    assert [r["sequence"] for r in created] == [1, 2]

    # The read-only list returns the same records by sequence ascending.
    assert client.get(records_path()).json() == created


def test_rule_limit_exceeded_rules_carry_the_rule_id(client: TestClient) -> None:
    make_dataset_with_version(client)
    first = create_rule(
        client, {"name": "a", "kind": "not_null", "params": {"field": "id"}}
    )
    second = create_rule(
        client, {"name": "b", "kind": "not_null", "params": {"field": "amount"}}
    )
    register_config(client, rule_limit=1)

    # Evaluation 1: rule a has 2 violations (> 1), rule b has 1 (at the limit).
    evaluate(client, [{"amount": 1}, {"amount": 1}, {"id": 1, "amount": None}])
    # Evaluation 2: both rules exceed the limit.
    evaluate(client, [{}, {}, {}])

    created = scan(client)
    assert [
        (r["kind"], r["evaluation_sequence"], r["rule_id"], r["violation_count"])
        for r in created
    ] == [
        ("rule_limit_exceeded", 1, first["id"], 2),
        ("rule_limit_exceeded", 2, first["id"], 3),
        ("rule_limit_exceeded", 2, second["id"], 3),
    ]


def test_trend_requires_strictly_increasing_steps(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, steps=2)

    # Violation row counts: 0, 1, 2 -> two strictly increasing steps.
    evaluate(client, [])
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": None}, {"id": None}])

    created = scan(client)
    assert len(created) == 1
    record = created[0]
    assert record["kind"] == "trend"
    # The trend record points at the last evaluation of the increasing run.
    assert record["evaluation_sequence"] == 3
    assert record["rule_id"] is None
    assert record["violation_count"] == 2


def test_trend_not_reached_or_broken_by_a_decline_produces_nothing(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, steps=3)

    # 0, 1, 2: only two steps, below the configured three.
    evaluate(client, [])
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": None}] * 2)
    # A decline (2 -> 1) resets the run; 1, 2 is a single step.
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": None}] * 2)
    # A plateau is not a strict increase either.
    evaluate(client, [{"id": None}] * 2)

    assert scan(client) == []
    assert client.get(records_path()).json() == []


def test_trend_continuing_past_the_threshold_flags_each_evaluation(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, steps=2)

    for count in range(4):  # violation row counts 0, 1, 2, 3
        evaluate(client, [{"id": None}] * count)

    created = scan(client)
    assert [(r["kind"], r["evaluation_sequence"]) for r in created] == [
        ("trend", 3),
        ("trend", 4),
    ]


def test_scan_combines_all_kinds_in_evaluation_order(client: TestClient) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    register_config(client, steps=2, row_limit=1, rule_limit=0)

    evaluate(client, [])  # 0 violating rows
    evaluate(client, [{"id": None}, {"id": None}])  # 2 > row limit and rule limit
    evaluate(client, [{"id": None}] * 3)  # 3, worsening continues

    created = scan(client)
    assert [
        (r["kind"], r["evaluation_sequence"], r["rule_id"])
        for r in created
    ] == [
        ("row_limit_exceeded", 2, None),
        ("rule_limit_exceeded", 2, rule["id"]),
        ("row_limit_exceeded", 3, None),
        ("rule_limit_exceeded", 3, rule["id"]),
        ("trend", 3, None),
    ]
    assert [r["sequence"] for r in created] == [1, 2, 3, 4, 5]


def test_repeated_scans_only_add_new_anomalies(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, steps=2, row_limit=0)

    evaluate(client, [{"id": None}])
    first = scan(client)
    assert [(r["kind"], r["evaluation_sequence"]) for r in first] == [
        ("row_limit_exceeded", 1)
    ]

    # A scan over unchanged history detects nothing new.
    assert scan(client) == []
    assert client.get(records_path()).json() == first

    # New evaluations make the worsening trend detectable; only the new
    # anomalies are appended, with contiguous sequences.
    evaluate(client, [{"id": None}] * 2)
    evaluate(client, [{"id": None}] * 3)
    second = scan(client)
    assert [(r["kind"], r["evaluation_sequence"]) for r in second] == [
        ("row_limit_exceeded", 2),
        ("row_limit_exceeded", 3),
        ("trend", 3),
    ]
    assert [r["sequence"] for r in second] == [2, 3, 4]

    everything = client.get(records_path()).json()
    assert [r["sequence"] for r in everything] == [1, 2, 3, 4]
    assert everything == first + second


def test_scan_does_not_change_history_rules_or_config(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    config = register_config(client, row_limit=0)
    evaluate(client, [{"id": None}])

    scan(client)

    history = client.get(rules_path() + "/evaluations").json()
    assert len(history) == 1
    assert client.get(rules_path()).json()[0]["enabled"] is True
    assert client.get(config_path()).json() == config


def test_records_survive_a_restart(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, row_limit=0)
    evaluate(client, [{"id": None}])
    created = scan(client)

    from app.main import app

    restarted = TestClient(app)
    assert restarted.get(records_path()).json() == created
    assert restarted.get(config_path()).json()["violation_row_limit"] == 0
    # A rescan after the restart still finds nothing new.
    assert restarted.post(scan_path()).json() == []


def test_records_are_scoped_to_their_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, row_limit=0)
    evaluate(client, [{"id": None}])
    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "id", "type": "bigint", "nullable": False}]},
        ).status_code
        == 201
    )
    register_config(client, row_limit=0, dataset="orders", version=2)

    assert scan(client) != []
    # The other version has its own empty record list and scan.
    assert client.get(records_path("orders", 2)).json() == []
    assert scan(client, dataset="orders", version=2) == []
    assert len(client.get(records_path()).json()) == 1


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_scans_record_each_anomaly_once(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, steps=2, row_limit=0, rule_limit=0)
    for count in range(1, 4):
        evaluate(client, [{"id": None}] * count)

    responses: list[list[dict]] = []
    failures: list[object] = []

    def run_scan() -> None:
        try:
            response = client.post(scan_path())
            assert response.status_code == 200, response.text
            responses.append(response.json())
        except BaseException as exc:  # pragma: no cover - failure reporting
            failures.append(exc)

    threads = [threading.Thread(target=run_scan) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures
    records = client.get(records_path()).json()
    # Each anomaly was persisted exactly once, with contiguous sequences.
    keys = [
        (r["kind"], r["evaluation_sequence"], r["rule_id"]) for r in records
    ]
    assert len(keys) == len(set(keys))
    assert [r["sequence"] for r in records] == list(range(1, len(records) + 1))
    # The union of what the racing scans returned is exactly the stored set.
    returned = [
        (r["kind"], r["evaluation_sequence"], r["rule_id"])
        for response in responses
        for r in response
    ]
    assert sorted(returned) == sorted(keys)
