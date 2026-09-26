"""Tests for the as-of (point-in-time) quality gate verdict."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

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


def gate_at_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{rules_path(dataset, version)}/gate/at"


def config_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{rules_path(dataset, version)}/anomaly-detection"


def create_rule(client: TestClient, payload: dict) -> dict:
    response = client.post(rules_path(), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def evaluate(client: TestClient, rows: list[dict]) -> dict:
    response = client.post(rules_path() + "/evaluate", json={"rows": rows})
    assert response.status_code == 200, response.text
    return response.json()


def register_config(client: TestClient, row_limit: int = 1000) -> dict:
    response = client.post(
        config_path(),
        json={
            "consecutive_worsening_steps": 2,
            "violation_row_limit": row_limit,
            "rule_violation_limit": 1000,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def scan(client: TestClient) -> list[dict]:
    response = client.post(config_path() + "/scan")
    assert response.status_code == 200, response.text
    return response.json()


def evaluation_times(client: TestClient) -> list[datetime]:
    history = client.get(rules_path() + "/evaluations").json()
    return [datetime.fromisoformat(record["created_at"]) for record in history]


def anomaly_times(client: TestClient) -> list[datetime]:
    records = client.get(config_path() + "/anomalies").json()
    return [datetime.fromisoformat(record["created_at"]) for record in records]


def iso(moment: datetime) -> str:
    return moment.isoformat()


def gate_at(client: TestClient, timestamp: str, **path: object) -> dict:
    response = client.get(
        gate_at_path(**path),  # type: ignore[arg-type]
        params={"timestamp": timestamp},
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Windowed verdicts
# --------------------------------------------------------------------------- #


def test_gate_at_is_undetermined_when_the_window_has_no_evaluation(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})

    gate = gate_at(client, iso(datetime.now(timezone.utc) + timedelta(days=1)))
    assert gate["verdict"] == "undetermined"
    assert gate["reasons"] == []
    assert gate["counts"] == {"evaluations": 0, "anomalies": 0, "reasons": 0}


def test_gate_at_before_any_evaluation_is_undetermined_not_an_error(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    (first_at,) = evaluation_times(client)

    gate = gate_at(client, iso(first_at - timedelta(seconds=1)))
    assert gate["verdict"] == "undetermined"
    assert gate["reasons"] == []
    assert gate["counts"] == {"evaluations": 0, "anomalies": 0, "reasons": 0}


def test_gate_at_selects_the_latest_evaluation_within_the_window(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])  # sequence 1: a violation
    evaluate(client, [{"id": 1}])  # sequence 2: clean
    first_at, second_at = evaluation_times(client)

    after_first = gate_at(client, iso(first_at + (second_at - first_at) / 2))
    assert after_first["verdict"] == "fail"
    assert after_first["reasons"] == [
        {
            "kind": "violation",
            "sequence": 1,
            "rule_id": rule["id"],
            "violation_count": 1,
        }
    ]
    assert after_first["counts"] == {"evaluations": 1, "anomalies": 0, "reasons": 1}

    after_second = gate_at(client, iso(second_at + timedelta(milliseconds=1)))
    assert after_second["verdict"] == "pass"
    assert after_second["reasons"] == []
    assert after_second["counts"] == {"evaluations": 2, "anomalies": 0, "reasons": 0}


def test_gate_at_counts_anomalies_written_within_the_window(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, row_limit=0)
    evaluate(client, [{"id": None}, {"id": None}])  # sequence 1: over the limit
    created = scan(client)
    assert len(created) == 1
    evaluate(client, [{"id": 1}])  # sequence 2: clean
    first_eval_at, second_eval_at = evaluation_times(client)
    (anomaly_at,) = anomaly_times(client)
    assert first_eval_at <= anomaly_at <= second_eval_at

    # Between the first evaluation and the anomaly record: the window holds
    # only the violating evaluation.
    before_anomaly = gate_at(
        client, iso(first_eval_at + (anomaly_at - first_eval_at) / 2)
    )
    assert before_anomaly["verdict"] == "fail"  # latest in window: sequence 1
    assert before_anomaly["counts"] == {
        "evaluations": 1,
        "anomalies": 0,
        "reasons": 1,
    }

    # After everything: the latest evaluation is clean, but the persisted
    # anomaly record still fails the gate.
    after_anomaly = gate_at(client, iso(second_eval_at + timedelta(days=1)))
    assert after_anomaly["verdict"] == "fail"
    assert after_anomaly["reasons"] == [
        {
            "kind": "row_limit",
            "sequence": 1,
            "rule_id": None,
            "violation_count": 2,
        }
    ]
    assert after_anomaly["counts"] == {
        "evaluations": 2,
        "anomalies": 1,
        "reasons": 1,
    }


def test_gate_at_boundary_instant_includes_the_record(client: TestClient) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])
    (first_at,) = evaluation_times(client)

    gate = gate_at(client, iso(first_at))
    assert gate["verdict"] == "fail"
    assert gate["reasons"] == [
        {
            "kind": "violation",
            "sequence": 1,
            "rule_id": rule["id"],
            "violation_count": 1,
        }
    ]


def test_gate_at_accepts_any_timezone_offset(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": 1}])
    (first_at,) = evaluation_times(client)

    shifted = first_at.astimezone(timezone(timedelta(hours=5, minutes=30)))
    gate = gate_at(client, shifted.isoformat())
    assert gate["verdict"] == "pass"
    assert gate["counts"] == {"evaluations": 1, "anomalies": 0, "reasons": 0}


def test_gate_at_is_scoped_to_its_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "id", "type": "bigint", "nullable": False}]},
        ).status_code
        == 201
    )
    later = iso(datetime.now(timezone.utc) + timedelta(days=1))

    assert gate_at(client, later)["verdict"] == "fail"
    other = gate_at(client, later, dataset="orders", version=2)
    assert other["verdict"] == "undetermined"
    assert other["version"] == 2


# --------------------------------------------------------------------------- #
# Request shape and 404 precedence
# --------------------------------------------------------------------------- #


def test_gate_at_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    timestamp = iso(datetime.now(timezone.utc))
    for path in (gate_at_path("ghost"), gate_at_path("orders", 9)):
        response = client.get(path, params={"timestamp": timestamp})
        assert response.status_code == 404
        body = response.json()
        assert body["error"] == "not_found"
        assert "detail" in body


def test_gate_at_404_precedes_timestamp_and_shape_checks(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    ghost = gate_at_path("ghost")
    assert client.get(ghost).status_code == 404  # missing timestamp
    assert client.get(ghost, params={"timestamp": "not-a-time"}).status_code == 404
    assert client.get(ghost, params={"x": "1"}).status_code == 404
    assert client.request("GET", ghost, content=b"{}").status_code == 404


def test_gate_at_rejects_missing_repeated_and_bad_timestamps(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    path = gate_at_path()
    good = iso(datetime.now(timezone.utc))

    responses = [
        client.get(path),  # missing
        client.get(path, params={"timestamp": ""}),  # empty
        client.get(path, params={"timestamp": "not-a-time"}),  # unparseable
        client.get(path, params={"timestamp": "2026-01-01"}),  # date only
        client.get(  # naive date-time: no timezone
            path, params={"timestamp": "2026-01-01T00:00:00"}
        ),
        client.get(  # repeated
            path, params=[("timestamp", good), ("timestamp", good)]
        ),
        client.get(path, params={"timestamp": good, "x": "1"}),  # extra param
        client.get(path, params={"x": "1"}),  # only extra param
    ]
    for response in responses:
        assert response.status_code == 422, response.text
        body = response.json()
        assert body["error"] == "validation_error"
        assert "detail" in body


def test_gate_at_rejects_any_request_body(client: TestClient) -> None:
    make_dataset_with_version(client)
    timestamp = iso(datetime.now(timezone.utc))
    for content in (b"{}", b" ", b" \t\n"):
        response = client.request(
            "GET", gate_at_path(), params={"timestamp": timestamp}, content=content
        )
        assert response.status_code == 422, response.text
        body = response.json()
        assert body["error"] == "validation_error"
        assert "detail" in body


def test_gate_at_only_accepts_get(client: TestClient) -> None:
    make_dataset_with_version(client)
    timestamp = iso(datetime.now(timezone.utc))
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        response = client.request(
            method, gate_at_path(), params={"timestamp": timestamp}
        )
        assert response.status_code == 405, response.text


# --------------------------------------------------------------------------- #
# Determinism and read-only behaviour
# --------------------------------------------------------------------------- #


def test_gate_at_response_is_byte_identical_across_calls_and_a_restart(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, row_limit=0)
    evaluate(client, [{"id": None}])
    scan(client)
    timestamp = iso(datetime.now(timezone.utc) + timedelta(days=1))

    first = client.get(gate_at_path(), params={"timestamp": timestamp})
    assert first.status_code == 200
    assert first.text.endswith("}\n")
    assert client.get(gate_at_path(), params={"timestamp": timestamp}).text == (
        first.text
    )
    assert first.text == json.dumps(
        first.json(), separators=(",", ":"), ensure_ascii=False
    ) + "\n"
    assert list(first.json()) == ["dataset", "version", "verdict", "reasons", "counts"]

    from app.main import app

    restarted = TestClient(app)
    assert restarted.get(gate_at_path(), params={"timestamp": timestamp}).text == (
        first.text
    )


def test_gate_at_writes_nothing(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, row_limit=0)
    evaluate(client, [{"id": None}])
    scan(client)
    timestamp = iso(datetime.now(timezone.utc) + timedelta(days=1))

    history_before = client.get(rules_path() + "/evaluations").json()
    anomalies_before = client.get(config_path() + "/anomalies").json()
    rules_before = client.get(rules_path()).json()

    client.get(gate_at_path(), params={"timestamp": timestamp})
    # Rejections write nothing either.
    client.get(gate_at_path())
    client.request(
        "GET", gate_at_path(), params={"timestamp": timestamp}, content=b"{}"
    )

    assert client.get(rules_path() + "/evaluations").json() == history_before
    assert client.get(config_path() + "/anomalies").json() == anomalies_before
    assert client.get(rules_path()).json() == rules_before
