"""Tests for the read-only pre-release quality gate verdict."""

from __future__ import annotations

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


def gate_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{rules_path(dataset, version)}/gate"


def history_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{rules_path(dataset, version)}/evaluations"


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


def evaluate(client: TestClient, rows: list[dict]) -> dict:
    response = client.post(rules_path() + "/evaluate", json={"rows": rows})
    assert response.status_code == 200, response.text
    return response.json()


def register_config(
    client: TestClient,
    steps: int = 2,
    row_limit: int = 1000,
    rule_limit: int = 1000,
) -> dict:
    response = client.post(
        config_path(),
        json={
            "consecutive_worsening_steps": steps,
            "violation_row_limit": row_limit,
            "rule_violation_limit": rule_limit,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def scan(client: TestClient) -> list[dict]:
    response = client.post(scan_path())
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Verdict: undetermined
# --------------------------------------------------------------------------- #


def test_no_evaluations_is_undetermined_with_empty_reasons(client: TestClient) -> None:
    make_dataset_with_version(client)

    response = client.get(gate_path())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verdict"] == "undetermined"
    assert body["reasons"] == []
    assert body["checks"] == {
        "evaluation_count": 0,
        "anomaly_count": 0,
        "reason_count": 0,
    }


def test_a_config_without_evaluations_is_still_undetermined(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    register_config(client)

    body = client.get(gate_path()).json()

    assert body["verdict"] == "undetermined"
    assert body["reasons"] == []
    assert body["checks"]["evaluation_count"] == 0


# --------------------------------------------------------------------------- #
# Verdict: passed
# --------------------------------------------------------------------------- #


def test_latest_evaluation_clean_and_no_anomalies_is_passed(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    # An earlier violating evaluation does not matter once the latest passes.
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": 1}])

    body = client.get(gate_path()).json()

    assert body["verdict"] == "passed"
    assert body["reasons"] == []
    assert body["checks"] == {
        "evaluation_count": 2,
        "anomaly_count": 0,
        "reason_count": 0,
    }


def test_zero_row_evaluation_with_zero_violations_is_a_valid_passing_basis(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [])

    body = client.get(gate_path()).json()

    assert body["verdict"] == "passed"
    assert body["reasons"] == []
    assert body["checks"]["evaluation_count"] == 1


# --------------------------------------------------------------------------- #
# Verdict: failed through the latest evaluation
# --------------------------------------------------------------------------- #


def test_latest_evaluation_with_violations_is_failed(client: TestClient) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": 1}])
    evaluate(client, [{"id": None}, {"id": 2, "amount": 1}, {"id": None}])

    body = client.get(gate_path()).json()

    assert body["verdict"] == "failed"
    assert body["reasons"] == [
        {
            "type": "evaluation",
            "sequence": 2,
            "rule_id": rule["id"],
            "violation_count": 2,
        }
    ]
    assert body["checks"] == {
        "evaluation_count": 2,
        "anomaly_count": 0,
        "reason_count": 1,
    }


def test_one_evaluation_reason_per_violating_rule_sorted_by_rule_id(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    first = create_rule(
        client, {"name": "a", "kind": "not_null", "params": {"field": "id"}}
    )
    second = create_rule(
        client, {"name": "b", "kind": "not_null", "params": {"field": "amount"}}
    )
    # Row 0 violates both rules; row 1 violates only the second rule.
    evaluate(client, [{"region": "eu"}, {"id": 1}])

    body = client.get(gate_path()).json()

    assert body["verdict"] == "failed"
    assert [(r["type"], r["sequence"], r["rule_id"]) for r in body["reasons"]] == [
        ("evaluation", 1, first["id"]),
        ("evaluation", 1, second["id"]),
    ]
    assert [r["violation_count"] for r in body["reasons"]] == [1, 2]


def test_rule_disabled_after_the_latest_evaluation_still_counts_from_history(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])
    # Disable the rule after the violating evaluation was recorded; the gate
    # never re-runs rules, so the persisted violation still blocks release.
    response = client.patch(
        f"{rules_path()}/{rule['id']}", json={"enabled": False}
    )
    assert response.status_code == 200

    body = client.get(gate_path()).json()

    assert body["verdict"] == "failed"
    assert body["reasons"] == [
        {
            "type": "evaluation",
            "sequence": 1,
            "rule_id": rule["id"],
            "violation_count": 1,
        }
    ]


# --------------------------------------------------------------------------- #
# Verdict: failed through persisted anomaly records
# --------------------------------------------------------------------------- #


def test_any_persisted_anomaly_fails_even_when_the_latest_evaluation_passes(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    register_config(client, row_limit=0)
    evaluate(client, [{"id": None}])
    assert len(scan(client)) == 1

    # The latest evaluation is clean, but the persisted anomaly stays.
    evaluate(client, [{"id": 1}])

    body = client.get(gate_path()).json()

    assert body["verdict"] == "failed"
    assert body["reasons"] == [
        {
            "type": "row_limit",
            "sequence": 1,
            "rule_id": None,
            "violation_count": 1,
        }
    ]
    assert body["checks"] == {
        "evaluation_count": 2,
        "anomaly_count": 1,
        "reason_count": 1,
    }
    assert rule["id"]  # fixture sanity


def test_each_anomaly_kind_becomes_a_reason_using_its_stored_values(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    # Counts 1, 2, 3 with zero limits: row- and rule-limit records at every
    # sequence and a trend record pointing at the final one.
    register_config(client, steps=2, row_limit=0, rule_limit=0)
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": None}, {"id": None}])
    evaluate(client, [{"id": None}, {"id": None}, {"id": None}])

    records = scan(client)
    kinds = {(r["kind"], r["sequence"], r["rule_id"]) for r in records}
    assert ("row_limit", 1, None) in kinds
    assert ("rule_limit", 1, rule["id"]) in kinds
    assert ("trend", 3, None) in kinds

    body = client.get(gate_path()).json()
    assert body["verdict"] == "failed"
    reasons = body["reasons"]
    by_key = {(r["type"], r["sequence"], r["rule_id"]): r for r in reasons}
    assert by_key[("row_limit", 1, None)]["violation_count"] == 1
    assert by_key[("rule_limit", 1, rule["id"])]["violation_count"] == 1
    assert by_key[("row_limit", 3, None)]["violation_count"] == 3
    assert by_key[("rule_limit", 3, rule["id"])]["violation_count"] == 3
    assert by_key[("trend", 3, None)]["violation_count"] == 3


def test_evaluation_and_anomaly_reasons_for_one_rule_are_not_merged(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    register_config(client, row_limit=10, rule_limit=0)
    # The latest (and only) evaluation violates the rule and produces an
    # anomaly record for the same rule.
    evaluate(client, [{"id": None}, {"id": None}])
    created = scan(client)
    assert [(r["kind"], r["rule_id"]) for r in created] == [
        ("rule_limit", rule["id"])
    ]

    body = client.get(gate_path()).json()

    assert body["verdict"] == "failed"
    assert [(r["type"], r["sequence"], r["rule_id"]) for r in body["reasons"]] == [
        ("evaluation", 1, rule["id"]),
        ("rule_limit", 1, rule["id"]),
    ]
    assert [r["violation_count"] for r in body["reasons"]] == [2, 2]
    assert body["checks"]["reason_count"] == 2


def test_reasons_sort_by_type_then_sequence_then_rule_id(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    # Counts 2, 3, 4 with limits 1/1 and two worsening steps: row- and
    # rule-limit records at every sequence, a trend record pointing at the
    # final one, plus an evaluation reason for the latest violation.
    register_config(client, steps=2, row_limit=1, rule_limit=1)
    evaluate(client, [{"id": None}] * 2)
    evaluate(client, [{"id": None}] * 3)
    evaluate(client, [{"id": None}] * 4)
    scan(client)

    reasons = client.get(gate_path()).json()["reasons"]

    assert [(r["type"], r["sequence"], r["rule_id"]) for r in reasons] == [
        ("evaluation", 3, rule["id"]),
        ("row_limit", 1, None),
        ("row_limit", 2, None),
        ("row_limit", 3, None),
        ("rule_limit", 1, rule["id"]),
        ("rule_limit", 2, rule["id"]),
        ("rule_limit", 3, rule["id"]),
        ("trend", 3, None),
    ]
    # The null rule id is still present as a key, never omitted.
    assert all(set(r) == {"type", "sequence", "rule_id", "violation_count"}
               for r in reasons)
    assert reasons[1]["rule_id"] is None


# --------------------------------------------------------------------------- #
# Shape, precedence and side-effect rules
# --------------------------------------------------------------------------- #


def test_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert client.get(gate_path("ghost")).status_code == 404
    assert client.get(gate_path("orders", 9)).status_code == 404


def test_body_and_query_parameters_are_422(client: TestClient) -> None:
    make_dataset_with_version(client)

    responses = [
        client.request("GET", gate_path(), content=b"{}"),
        client.request("GET", gate_path(), content=b" "),
        client.request("GET", gate_path(), content=b"\t\n"),
        client.get(gate_path(), params={"x": "1"}),
        client.get(gate_path(), params={"limit": "0"}),
    ]
    for response in responses:
        assert response.status_code == 422, response.text
        body = response.json()
        assert set(body) == {"error", "detail"}
        assert body["error"] == "validation_error"
        assert isinstance(body["detail"], str) and body["detail"]


def test_404_takes_precedence_over_request_shape_errors(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    assert (
        client.request("GET", gate_path("ghost"), content=b" ").status_code
        == 404
    )
    assert (
        client.get(gate_path("ghost"), params={"x": "1"}).status_code == 404
    )
    assert (
        client.request("GET", gate_path("orders", 9), content=b"{}").status_code
        == 404
    )
    assert client.get(gate_path("orders", 9), params={"x": "1"}).status_code == 404


def test_only_get_is_accepted(client: TestClient) -> None:
    make_dataset_with_version(client)
    # POST/PUT/DELETE match no route shape and are refused with 405; PATCH
    # falls through to the /quality-rules/{rule_id} route like the other
    # literal quality-rules segments ("evaluate", "anomaly-detection").
    for method in ("POST", "PUT", "DELETE"):
        response = client.request(method, gate_path())
        assert response.status_code == 405, (method, response.status_code)
    assert client.request("PATCH", gate_path()).status_code == 422
    assert client.get(gate_path()).status_code == 200


def test_gate_reads_write_nothing(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    register_config(client, row_limit=0)
    evaluate(client, [{"id": None}])
    scan(client)

    history_before = client.get(history_path()).json()
    anomalies_before = client.get(anomalies_path()).json()

    for _ in range(3):
        assert client.get(gate_path()).status_code == 200
    assert client.request("GET", gate_path(), content=b"?").status_code == 422
    assert client.get(gate_path(), params={"x": "1"}).status_code == 422

    assert client.get(history_path()).json() == history_before
    assert client.get(anomalies_path()).json() == anomalies_before


# --------------------------------------------------------------------------- #
# Deterministic document shape
# --------------------------------------------------------------------------- #


def test_document_is_compact_with_fixed_key_order_and_one_newline(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    register_config(client)
    evaluate(client, [])

    response = client.get(gate_path())

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.content.endswith(b"\n")
    assert not response.content.endswith(b"\n\n")
    text = response.text[:-1]
    # Compact whitespace: no padding after separators.
    assert ", " not in text
    assert ": " not in text
    assert text == '{"dataset":"orders","version":1,"verdict":"passed",' \
        '"reasons":[],"checks":{"evaluation_count":1,"anomaly_count":0,' \
        '"reason_count":0}}'


def test_top_level_and_reason_key_order_is_fixed(client: TestClient) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])

    body = client.get(gate_path()).json()

    assert list(body) == ["dataset", "version", "verdict", "reasons", "checks"]
    assert list(body["checks"]) == [
        "evaluation_count",
        "anomaly_count",
        "reason_count",
    ]
    assert list(body["reasons"][0]) == [
        "type",
        "sequence",
        "rule_id",
        "violation_count",
    ]
    assert rule["id"]


def test_verdict_is_identical_across_restarts(client: TestClient) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    register_config(client, steps=2, row_limit=1, rule_limit=1)
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": None}, {"id": None}])
    scan(client)

    first = client.get(gate_path()).content

    from app.main import app

    restarted = TestClient(app)
    assert restarted.get(gate_path()).content == first
    # A second read on the same process is byte-identical too.
    assert client.get(gate_path()).content == first
    assert rule["id"]
