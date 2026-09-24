"""Tests for quality anomaly detection over the evaluation history."""

from __future__ import annotations

import threading
from datetime import datetime

import pytest
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


def anomalies_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{config_path(dataset, version)}/anomalies"


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


def evaluate_with_violations(client: TestClient, count: int, **path: object) -> None:
    """One evaluation whose violation row count is exactly ``count``."""
    evaluate(client, [{"id": None}] * count + [{"id": 1}], **path)


def register_config(
    client: TestClient,
    steps: int = 2,
    row_limit: int = 1000,
    rule_limit: int = 1000,
    **path: object,
) -> dict:
    response = client.post(
        config_path(**path),  # type: ignore[arg-type]
        json={
            "consecutive_worsening_steps": steps,
            "violation_row_limit": row_limit,
            "rule_violation_limit": rule_limit,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Config registration
# --------------------------------------------------------------------------- #


def test_register_config_returns_201_and_the_config(client: TestClient) -> None:
    make_dataset_with_version(client)

    response = client.post(
        config_path(),
        json={
            "consecutive_worsening_steps": 3,
            "violation_row_limit": 10,
            "rule_violation_limit": 4,
        },
    )

    assert response.status_code == 201, response.text
    config = response.json()
    assert set(config) == {
        "id",
        "dataset",
        "version",
        "consecutive_worsening_steps",
        "violation_row_limit",
        "rule_violation_limit",
        "created_at",
    }
    assert config["dataset"] == "orders"
    assert config["version"] == 1
    assert config["consecutive_worsening_steps"] == 3
    assert config["violation_row_limit"] == 10
    assert config["rule_violation_limit"] == 4
    datetime.fromisoformat(config["created_at"])

    # The read-only entry returns the same config.
    fetched = client.get(config_path())
    assert fetched.status_code == 200
    assert fetched.json() == config


def test_config_accepts_zero_limits_and_two_steps(client: TestClient) -> None:
    make_dataset_with_version(client)
    config = register_config(client, steps=2, row_limit=0, rule_limit=0)
    assert config["consecutive_worsening_steps"] == 2
    assert config["violation_row_limit"] == 0
    assert config["rule_violation_limit"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},  # all fields missing
        {"consecutive_worsening_steps": 2, "violation_row_limit": 0},  # one missing
        {  # extra field
            "consecutive_worsening_steps": 2,
            "violation_row_limit": 0,
            "rule_violation_limit": 0,
            "extra": 1,
        },
        {  # non-integer steps
            "consecutive_worsening_steps": "2",
            "violation_row_limit": 0,
            "rule_violation_limit": 0,
        },
        {  # non-integer limit
            "consecutive_worsening_steps": 2,
            "violation_row_limit": 1.5,
            "rule_violation_limit": 0,
        },
        {  # boolean is not an integer
            "consecutive_worsening_steps": True,
            "violation_row_limit": 0,
            "rule_violation_limit": 0,
        },
        {  # steps below 2
            "consecutive_worsening_steps": 1,
            "violation_row_limit": 0,
            "rule_violation_limit": 0,
        },
        {  # negative row limit
            "consecutive_worsening_steps": 2,
            "violation_row_limit": -1,
            "rule_violation_limit": 0,
        },
        {  # negative rule limit
            "consecutive_worsening_steps": 2,
            "violation_row_limit": 0,
            "rule_violation_limit": -1,
        },
    ],
)
def test_invalid_config_payloads_are_422_and_write_nothing(
    client: TestClient, payload: dict
) -> None:
    make_dataset_with_version(client)

    response = client.post(config_path(), json=payload)

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert "detail" in body
    # Nothing was registered.
    assert client.get(config_path()).status_code == 404


def test_register_config_unknown_dataset_or_version_is_404(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    payload = {
        "consecutive_worsening_steps": 2,
        "violation_row_limit": 0,
        "rule_violation_limit": 0,
    }
    assert client.post(config_path("ghost"), json=payload).status_code == 404
    assert client.post(config_path("orders", 9), json=payload).status_code == 404


def test_second_config_for_the_same_version_is_409(client: TestClient) -> None:
    make_dataset_with_version(client)
    register_config(client)

    response = client.post(
        config_path(),
        json={
            "consecutive_worsening_steps": 4,
            "violation_row_limit": 1,
            "rule_violation_limit": 1,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    # The first config is untouched.
    assert client.get(config_path()).json()["consecutive_worsening_steps"] == 2


def test_config_is_scoped_to_its_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    register_config(client)
    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "id", "type": "bigint", "nullable": False}]},
        ).status_code
        == 201
    )

    # The new version has no config yet; registering one there is independent.
    assert client.get(config_path("orders", 2)).status_code == 404
    register_config(client, steps=3, dataset="orders", version=2)
    assert (
        client.get(config_path("orders", 2)).json()["consecutive_worsening_steps"]
        == 3
    )
    assert client.get(config_path()).json()["consecutive_worsening_steps"] == 2


def test_get_config_without_registration_is_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert client.get(config_path()).status_code == 404
    assert client.get(config_path("ghost")).status_code == 404
    assert client.get(config_path("orders", 9)).status_code == 404


# --------------------------------------------------------------------------- #
# Scan prerequisites and shape checks
# --------------------------------------------------------------------------- #


def test_scan_without_config_is_409(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate_with_violations(client, 3)

    response = client.post(scan_path())

    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    assert client.get(anomalies_path()).json() == []


def test_scan_with_empty_history_succeeds_and_writes_nothing(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    register_config(client)

    response = client.post(scan_path())

    assert response.status_code == 200
    assert response.json() == []
    assert client.get(anomalies_path()).json() == []


def test_scan_and_read_endpoints_unknown_dataset_or_version_is_404(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    assert client.post(scan_path("ghost")).status_code == 404
    assert client.post(scan_path("orders", 9)).status_code == 404
    assert client.get(anomalies_path("ghost")).status_code == 404
    assert client.get(anomalies_path("orders", 9)).status_code == 404


def test_scan_and_read_endpoints_reject_body_and_query_params(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    register_config(client)

    responses = [
        client.request("POST", scan_path(), content=b"{}"),
        client.post(scan_path(), json={}),
        client.post(scan_path(), params={"x": "1"}),
        client.request("GET", anomalies_path(), content=b"{}"),
        client.get(anomalies_path(), params={"limit": "1"}),
        client.request("GET", config_path(), content=b"{}"),
        client.get(config_path(), params={"x": "1"}),
    ]
    for response in responses:
        assert response.status_code == 422, response.text
        body = response.json()
        assert body["error"] == "validation_error"
        assert "detail" in body

    # 404 takes precedence over the shape checks.
    assert client.request("POST", scan_path("ghost"), content=b"{}").status_code == 404
    assert client.post(scan_path("ghost"), params={"x": "1"}).status_code == 404
    assert (
        client.request("GET", anomalies_path("ghost"), content=b"{}").status_code
        == 404
    )
    assert client.get(anomalies_path("ghost"), params={"x": "1"}).status_code == 404
    assert client.get(config_path("ghost"), params={"x": "1"}).status_code == 404


# --------------------------------------------------------------------------- #
# Row-limit and rule-limit records
# --------------------------------------------------------------------------- #


def test_row_limit_records_point_to_the_history_sequence(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, row_limit=2)

    evaluate_with_violations(client, 1)  # sequence 1: below the limit
    evaluate_with_violations(client, 3)  # sequence 2: exceeds the limit
    evaluate_with_violations(client, 2)  # sequence 3: equal, not exceeding

    created = client.post(scan_path()).json()
    assert len(created) == 1
    record = created[0]
    assert set(record) == {
        "id",
        "kind",
        "sequence",
        "rule_id",
        "violation_count",
        "created_at",
    }
    assert record["kind"] == "row_limit"
    assert record["sequence"] == 2
    assert record["rule_id"] is None
    assert record["violation_count"] == 3
    datetime.fromisoformat(record["created_at"])


def test_rule_limit_records_carry_the_rule_id(client: TestClient) -> None:
    make_dataset_with_version(client)
    first = create_rule(
        client, {"name": "a", "kind": "not_null", "params": {"field": "id"}}
    )
    second = create_rule(
        client, {"name": "b", "kind": "not_null", "params": {"field": "amount"}}
    )
    register_config(client, rule_limit=1)

    # Row 0 violates both rules, row 1 violates only the second.
    evaluate(client, [{"id": None, "amount": None}, {"id": 1}])

    created = client.post(scan_path()).json()
    assert len(created) == 1
    record = created[0]
    assert record["kind"] == "rule_limit"
    assert record["sequence"] == 1
    assert record["rule_id"] == second["id"]
    assert record["violation_count"] == 2
    assert record["rule_id"] != first["id"]


def test_row_and_rule_limit_records_in_one_scan(client: TestClient) -> None:
    make_dataset_with_version(client)
    first = create_rule(
        client, {"name": "a", "kind": "not_null", "params": {"field": "id"}}
    )
    second = create_rule(
        client, {"name": "b", "kind": "not_null", "params": {"field": "amount"}}
    )
    register_config(client, steps=2, row_limit=1, rule_limit=1)

    # Two rows violating both rules, then a clean one: counts 2 then 0.
    evaluate(client, [{"id": None, "amount": None}] * 2 + [{"id": 1, "amount": 1}])
    evaluate(client, [{"id": 1, "amount": 1}])

    created = client.post(scan_path()).json()
    # Insertion order: the row-limit record, then rule-limit by rule id.
    assert [(r["kind"], r["sequence"], r["rule_id"]) for r in created] == [
        ("row_limit", 1, None),
        ("rule_limit", 1, first["id"]),
        ("rule_limit", 1, second["id"]),
    ]
    assert [r["id"] for r in created] == [1, 2, 3]
    assert all(r["violation_count"] == 2 for r in created)
    # The counts went down, so there is no trend record.
    assert client.get(anomalies_path()).json() == created


# --------------------------------------------------------------------------- #
# Trend records
# --------------------------------------------------------------------------- #


def test_trend_record_when_the_tail_reaches_the_configured_steps(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, steps=2)

    evaluate_with_violations(client, 0)
    evaluate_with_violations(client, 1)
    evaluate_with_violations(client, 2)

    created = client.post(scan_path()).json()
    assert len(created) == 1
    record = created[0]
    assert record["kind"] == "trend"
    # The record points to the last evaluation of the increasing sequence.
    assert record["sequence"] == 3
    assert record["rule_id"] is None
    assert record["violation_count"] == 2


@pytest.mark.parametrize(
    "counts",
    [
        [5],  # a single evaluation cannot form a trend
        [1, 2],  # one increase, below two steps
        [2, 1],  # decline
        [1, 1],  # plateau is not a strict increase
        [0, 1, 1],  # plateau at the tail
        [0, 1, 2, 2],  # the run ended before the tail
        [3, 2, 1],  # strictly decreasing
    ],
)
def test_no_trend_record_without_a_long_enough_strictly_increasing_tail(
    client: TestClient, counts: list[int]
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, steps=2)

    for count in counts:
        evaluate_with_violations(client, count)

    assert client.post(scan_path()).json() == []
    assert client.get(anomalies_path()).json() == []


def test_trend_steps_of_three_need_three_consecutive_increases(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, steps=3)

    for count in (0, 1, 2):
        evaluate_with_violations(client, count)
    assert client.post(scan_path()).json() == []

    evaluate_with_violations(client, 3)
    created = client.post(scan_path()).json()
    assert len(created) == 1
    assert created[0]["kind"] == "trend"
    assert created[0]["sequence"] == 4
    assert created[0]["violation_count"] == 3


def test_rescan_only_adds_newly_appearing_anomalies(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, steps=2)

    for count in (0, 1, 2):
        evaluate_with_violations(client, count)
    first = client.post(scan_path()).json()
    assert [r["sequence"] for r in first] == [3]

    # A repeated scan persists and returns nothing new.
    assert client.post(scan_path()).json() == []
    assert client.get(anomalies_path()).json() == first

    # A decline and a single increase do not reach the steps yet.
    evaluate_with_violations(client, 1)
    evaluate_with_violations(client, 2)
    assert client.post(scan_path()).json() == []

    # The tail is now 1 < 2 < 3: a new trend record appears at the last
    # evaluation; the old one is not duplicated.
    evaluate_with_violations(client, 3)
    second = client.post(scan_path()).json()
    assert len(second) == 1
    assert second[0]["kind"] == "trend"
    assert second[0]["sequence"] == 6
    assert second[0]["violation_count"] == 3

    records = client.get(anomalies_path()).json()
    assert [r["id"] for r in records] == [1, 2]
    assert [r["sequence"] for r in records] == [3, 6]


# --------------------------------------------------------------------------- #
# Listing, scoping and persistence
# --------------------------------------------------------------------------- #


def test_list_is_ordered_by_id_and_empty_without_records(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    # No config and no history: an empty result, not an error.
    assert client.get(anomalies_path()).status_code == 200
    assert client.get(anomalies_path()).json() == []

    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, steps=2, row_limit=0, rule_limit=0)
    evaluate_with_violations(client, 0)
    evaluate_with_violations(client, 1)
    evaluate_with_violations(client, 2)

    created = client.post(scan_path()).json()
    listed = client.get(anomalies_path()).json()
    assert listed == created
    assert [r["id"] for r in listed] == sorted(r["id"] for r in listed)
    # counts 0, 1 then 2: row-limit and rule-limit on the last two
    # evaluations, plus a trend record pointing at the final one.
    assert [(r["kind"], r["sequence"]) for r in listed] == [
        ("row_limit", 2),
        ("rule_limit", 2),
        ("row_limit", 3),
        ("rule_limit", 3),
        ("trend", 3),
    ]


def test_anomalies_are_scoped_to_their_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, row_limit=0)
    evaluate_with_violations(client, 2)
    assert len(client.post(scan_path()).json()) == 1

    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "id", "type": "bigint", "nullable": False}]},
        ).status_code
        == 201
    )
    # The other version has no config and no records.
    assert client.get(anomalies_path("orders", 2)).json() == []
    assert client.post(scan_path("orders", 2)).status_code == 409
    assert len(client.get(anomalies_path()).json()) == 1


def test_config_and_anomalies_survive_a_restart(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    config = register_config(client, row_limit=1)
    evaluate_with_violations(client, 3)
    created = client.post(scan_path()).json()
    assert len(created) == 1

    # A brand-new client (fresh app instance against the same database file)
    # sees the same config and records, and a rescan adds nothing.
    from app.main import app

    restarted = TestClient(app)
    assert restarted.get(config_path()).json() == config
    assert restarted.get(anomalies_path()).json() == created
    assert restarted.post(scan_path()).json() == []


def test_scan_does_not_touch_rules_or_history(client: TestClient) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    register_config(client, row_limit=0)
    evaluate_with_violations(client, 2)

    rules_before = client.get(rules_path()).json()
    history_before = client.get(rules_path() + "/evaluations").json()
    client.post(scan_path())

    assert client.get(rules_path()).json() == rules_before
    assert client.get(rules_path() + "/evaluations").json() == history_before
    assert rules_before[0]["id"] == rule["id"]


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_scans_store_each_anomaly_once(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, steps=2, row_limit=1, rule_limit=1)
    for count in (0, 1, 2, 3):
        evaluate_with_violations(client, count)

    batches: list[list[dict]] = []
    failures: list[object] = []

    def scan() -> None:
        try:
            response = client.post(scan_path())
            assert response.status_code == 200, response.text
            batches.append(response.json())
        except BaseException as exc:  # pragma: no cover - failure reporting
            failures.append(exc)

    threads = [threading.Thread(target=scan) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures
    # counts 0,1,2,3 with limits 1/1: row-limit and rule-limit records at
    # sequences 3 and 4, plus one trend record at sequence 4.
    records = client.get(anomalies_path()).json()
    assert len(records) == 5
    assert [r["id"] for r in records] == [1, 2, 3, 4, 5]
    # Across all concurrent scans each record was returned exactly once.
    returned = [record for batch in batches for record in batch]
    assert sorted(record["id"] for record in returned) == [1, 2, 3, 4, 5]
