"""Tests for the quality gate verdict of a schema version."""

from __future__ import annotations

import json

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


def config_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{rules_path(dataset, version)}/anomaly-detection"


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
    response = client.post(config_path() + "/scan")
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Verdicts
# --------------------------------------------------------------------------- #


def test_gate_is_undetermined_without_any_evaluation(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})

    response = client.get(gate_path())

    assert response.status_code == 200, response.text
    # Deterministic body: fixed key order, compact whitespace, one newline.
    assert response.text == (
        '{"dataset":"orders","version":1,"verdict":"undetermined",'
        '"reasons":[],'
        '"counts":{"evaluations":0,"anomalies":0,"reasons":0}}\n'
    )
    assert response.json() == {
        "dataset": "orders",
        "version": 1,
        "verdict": "undetermined",
        "reasons": [],
        "counts": {"evaluations": 0, "anomalies": 0, "reasons": 0},
    }


def test_gate_passes_when_the_latest_evaluation_has_no_violations(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": 1, "amount": 2}])

    gate = client.get(gate_path()).json()
    assert gate["verdict"] == "pass"
    assert gate["reasons"] == []
    assert gate["counts"] == {"evaluations": 1, "anomalies": 0, "reasons": 0}


def test_gate_accepts_an_empty_rows_evaluation_as_valid_basis(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [])  # recorded with zero violations

    gate = client.get(gate_path()).json()
    assert gate["verdict"] == "pass"
    assert gate["counts"] == {"evaluations": 1, "anomalies": 0, "reasons": 0}


def test_gate_fails_on_violations_of_the_latest_evaluation(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    first = create_rule(
        client, {"name": "a", "kind": "not_null", "params": {"field": "id"}}
    )
    second = create_rule(
        client, {"name": "b", "kind": "not_null", "params": {"field": "amount"}}
    )
    # Row 0 violates both rules, row 1 violates only the second.
    evaluate(client, [{"id": None, "amount": None}, {"id": 1}])

    gate = client.get(gate_path()).json()
    assert gate["verdict"] == "fail"
    assert gate["reasons"] == [
        {
            "kind": "violation",
            "sequence": 1,
            "rule_id": first["id"],
            "violation_count": 1,
        },
        {
            "kind": "violation",
            "sequence": 1,
            "rule_id": second["id"],
            "violation_count": 2,
        },
    ]
    assert gate["counts"] == {"evaluations": 1, "anomalies": 0, "reasons": 2}


def test_gate_only_considers_the_latest_evaluation(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])  # sequence 1: a violation
    evaluate(client, [{"id": 1}])  # sequence 2: clean

    gate = client.get(gate_path()).json()
    assert gate["verdict"] == "pass"
    assert gate["reasons"] == []
    assert gate["counts"] == {"evaluations": 2, "anomalies": 0, "reasons": 0}


def test_gate_fails_on_any_anomaly_record_even_with_a_clean_latest_evaluation(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    register_config(client, row_limit=0)
    evaluate(client, [{"id": None}, {"id": None}])  # sequence 1: over the limit
    created = scan(client)
    assert len(created) == 1
    evaluate(client, [{"id": 1}])  # sequence 2: clean, but the record persists

    gate = client.get(gate_path()).json()
    assert gate["verdict"] == "fail"
    assert gate["reasons"] == [
        {
            "kind": "row_limit",
            "sequence": 1,
            "rule_id": None,
            "violation_count": 2,
        }
    ]
    assert gate["counts"] == {"evaluations": 2, "anomalies": 1, "reasons": 1}
    assert rule["id"]  # the rule itself is untouched


def test_gate_combines_evaluation_and_anomaly_reasons_without_merging(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    first = create_rule(
        client, {"name": "a", "kind": "not_null", "params": {"field": "id"}}
    )
    second = create_rule(
        client, {"name": "b", "kind": "not_null", "params": {"field": "amount"}}
    )
    register_config(client, row_limit=0, rule_limit=0)
    evaluate(client, [{"id": None, "amount": None}])  # both rules violated
    created = scan(client)
    assert [record["kind"] for record in created] == [
        "row_limit",
        "rule_limit",
        "rule_limit",
    ]

    gate = client.get(gate_path()).json()
    assert gate["verdict"] == "fail"
    # Sorted by kind, sequence and rule id (null first); the same rule appears
    # once as a rule_limit anomaly and once as a latest-evaluation violation.
    assert gate["reasons"] == [
        {"kind": "row_limit", "sequence": 1, "rule_id": None, "violation_count": 1},
        {
            "kind": "rule_limit",
            "sequence": 1,
            "rule_id": first["id"],
            "violation_count": 1,
        },
        {
            "kind": "rule_limit",
            "sequence": 1,
            "rule_id": second["id"],
            "violation_count": 1,
        },
        {
            "kind": "violation",
            "sequence": 1,
            "rule_id": first["id"],
            "violation_count": 1,
        },
        {
            "kind": "violation",
            "sequence": 1,
            "rule_id": second["id"],
            "violation_count": 1,
        },
    ]
    assert gate["counts"] == {"evaluations": 1, "anomalies": 3, "reasons": 5}


def test_gate_counts_violations_of_rules_disabled_after_the_evaluation(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])
    response = client.patch(
        rules_path() + f"/{rule['id']}", json={"enabled": False}
    )
    assert response.status_code == 200

    gate = client.get(gate_path()).json()
    # The persisted history is taken as-is; disabling the rule changes nothing.
    assert gate["verdict"] == "fail"
    assert gate["reasons"] == [
        {
            "kind": "violation",
            "sequence": 1,
            "rule_id": rule["id"],
            "violation_count": 1,
        }
    ]


def test_gate_is_scoped_to_its_version(client: TestClient) -> None:
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

    assert client.get(gate_path()).json()["verdict"] == "fail"
    other = client.get(gate_path("orders", 2)).json()
    assert other["verdict"] == "undetermined"
    assert other["version"] == 2
    assert other["counts"] == {"evaluations": 0, "anomalies": 0, "reasons": 0}


# --------------------------------------------------------------------------- #
# Request shape and 404 precedence
# --------------------------------------------------------------------------- #


def test_gate_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    for path in (gate_path("ghost"), gate_path("orders", 9)):
        response = client.get(path)
        assert response.status_code == 404
        body = response.json()
        assert body["error"] == "not_found"
        assert "detail" in body


def test_gate_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset_with_version(client)

    responses = [
        client.request("GET", gate_path(), content=b"{}"),
        client.request("GET", gate_path(), content=b" "),  # whitespace-only
        client.request("GET", gate_path(), content=b" \t\n"),
        client.get(gate_path(), params={"x": "1"}),
    ]
    for response in responses:
        assert response.status_code == 422, response.text
        body = response.json()
        assert body["error"] == "validation_error"
        assert "detail" in body

    # 404 takes precedence over the shape checks.
    assert client.request("GET", gate_path("ghost"), content=b"{}").status_code == 404
    assert client.get(gate_path("ghost"), params={"x": "1"}).status_code == 404
    # Nothing was written by the rejections.
    assert client.get(gate_path()).json()["verdict"] == "undetermined"


def test_gate_only_accepts_get(client: TestClient) -> None:
    make_dataset_with_version(client)
    for method in ("POST", "PUT", "DELETE"):
        response = client.request(method, gate_path())
        assert response.status_code == 405, response.text
    # PATCH resolves to the existing /quality-rules/{rule_id} route with a
    # non-integer rule id, exactly like any other non-numeric trailing segment.
    response = client.request("PATCH", gate_path(), json={"enabled": True})
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Determinism and read-only behaviour
# --------------------------------------------------------------------------- #


def test_gate_response_is_byte_identical_across_calls_and_a_restart(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, row_limit=0)
    evaluate(client, [{"id": None}])
    scan(client)

    first = client.get(gate_path())
    assert first.status_code == 200
    assert first.text.endswith("}\n")
    assert client.get(gate_path()).text == first.text
    # The body is the compact serialization of the parsed document.
    assert first.text == json.dumps(
        first.json(), separators=(",", ":"), ensure_ascii=False
    ) + "\n"

    # A brand-new client (fresh app instance against the same database file)
    # computes the very same verdict.
    from app.main import app

    restarted = TestClient(app)
    assert restarted.get(gate_path()).text == first.text


def test_gate_writes_nothing(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, row_limit=0)
    evaluate(client, [{"id": None}])
    scan(client)

    history_before = client.get(rules_path() + "/evaluations").json()
    anomalies_before = client.get(config_path() + "/anomalies").json()
    rules_before = client.get(rules_path()).json()

    client.get(gate_path())

    assert client.get(rules_path() + "/evaluations").json() == history_before
    assert client.get(config_path() + "/anomalies").json() == anomalies_before
    assert client.get(rules_path()).json() == rules_before
