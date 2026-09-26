"""Tests for evaluating quality rules against persisted snapshot rows."""

from __future__ import annotations

from fastapi.testclient import TestClient


FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "amount", "type": "decimal", "nullable": True},
    {"name": "region", "type": "string", "nullable": True},
]

ROWS = [
    {"id": 1, "amount": 10.5, "region": "eu"},
    {"id": 2, "amount": None, "region": "us"},
    {"id": 2, "amount": 250.0},
    {"amount": 5.0, "region": "eu"},
]


def create_dataset_and_version(client: TestClient, dataset: str = "orders") -> None:
    response = client.post("/datasets", json={"name": dataset})
    assert response.status_code == 201, response.text
    response = client.post(
        f"/datasets/{dataset}/versions", json={"fields": FIELDS}
    )
    assert response.status_code == 201, response.text


def create_rule(client: TestClient, payload: dict, dataset: str = "orders") -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/1/quality-rules", json=payload
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_snapshot(
    client: TestClient, rows: list, dataset: str = "orders", version: int = 1
) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots",
        json={"rows": rows},
    )
    assert response.status_code == 201, response.text
    return response.json()


def evaluate_path(snapshot_id: int, dataset: str = "orders", version: int = 1) -> str:
    return (
        f"/datasets/{dataset}/versions/{version}"
        f"/snapshots/{snapshot_id}/quality-rules/evaluate"
    )


def history_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/quality-rules/evaluations"


def setup_with_rules(client: TestClient) -> None:
    create_dataset_and_version(client)
    create_rule(client, {"name": "id required", "kind": "not_null", "params": {"field": "id"}})
    create_rule(
        client,
        {
            "name": "amount in range",
            "kind": "numeric_range",
            "params": {"field": "amount", "min": 0, "max": 100},
        },
    )
    create_rule(
        client,
        {"name": "unique ids", "kind": "unique", "params": {"fields": ["id"]}},
    )


# --------------------------------------------------------------------------- #
# Evaluation semantics
# --------------------------------------------------------------------------- #


def test_snapshot_evaluation_matches_row_evaluation(client: TestClient) -> None:
    setup_with_rules(client)
    snapshot = make_snapshot(client, ROWS)

    response = client.post(evaluate_path(snapshot["id"]))
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == {"dataset", "version", "results"}
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert [result["name"] for result in body["results"]] == [
        "id required",
        "amount in range",
        "unique ids",
    ]
    by_name = {result["name"]: result for result in body["results"]}
    # Missing id counts as a violation (row 3).
    assert by_name["id required"] == {
        "rule_id": by_name["id required"]["rule_id"],
        "name": "id required",
        "passed": False,
        "violations": [3],
    }
    # None and out-of-range amounts violate; the boundary value is accepted.
    assert by_name["amount in range"]["violations"] == [1, 2]
    assert by_name["amount in range"]["passed"] is False
    # Both rows of the duplicated id are flagged.
    assert by_name["unique ids"]["violations"] == [1, 2]


def test_snapshot_evaluation_matches_direct_row_evaluation(
    client: TestClient,
) -> None:
    setup_with_rules(client)
    snapshot = make_snapshot(client, ROWS)

    direct = client.post(
        "/datasets/orders/versions/1/quality-rules/evaluate", json={"rows": ROWS}
    )
    assert direct.status_code == 200, direct.text
    via_snapshot = client.post(evaluate_path(snapshot["id"]))
    assert via_snapshot.status_code == 200, via_snapshot.text
    assert via_snapshot.json() == direct.json()


def test_disabled_rules_are_not_executed(client: TestClient) -> None:
    setup_with_rules(client)
    rules = client.get("/datasets/orders/versions/1/quality-rules").json()
    disabled = rules[0]
    response = client.patch(
        f"/datasets/orders/versions/1/quality-rules/{disabled['id']}",
        json={"enabled": False},
    )
    assert response.status_code == 200, response.text

    snapshot = make_snapshot(client, ROWS)
    body = client.post(evaluate_path(snapshot["id"])).json()
    assert [result["name"] for result in body["results"]] == [
        "amount in range",
        "unique ids",
    ]
    # The disabled rule keeps its stored state.
    rules_after = client.get("/datasets/orders/versions/1/quality-rules").json()
    assert rules_after[0]["enabled"] is False


def test_snapshot_is_not_modified_by_evaluation(client: TestClient) -> None:
    setup_with_rules(client)
    snapshot = make_snapshot(client, ROWS)

    assert client.post(evaluate_path(snapshot["id"])).status_code == 200

    stored = client.get(f"/datasets/orders/versions/1/snapshots/{snapshot['id']}")
    assert stored.status_code == 200, stored.text
    assert stored.json()["rows"] == ROWS
    assert stored.json()["row_count"] == len(ROWS)


def test_empty_snapshot_evaluates_successfully(client: TestClient) -> None:
    setup_with_rules(client)
    snapshot = make_snapshot(client, [])

    response = client.post(evaluate_path(snapshot["id"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert all(result["passed"] for result in body["results"])
    assert all(result["violations"] == [] for result in body["results"])

    history = client.get(history_path()).json()
    assert len(history) == 1
    assert history[0]["row_count"] == 0
    assert history[0]["violation_row_count"] == 0


# --------------------------------------------------------------------------- #
# History recording
# --------------------------------------------------------------------------- #


def test_snapshot_evaluation_appends_history(client: TestClient) -> None:
    setup_with_rules(client)
    snapshot = make_snapshot(client, ROWS)

    assert client.post(evaluate_path(snapshot["id"])).status_code == 200
    assert client.post(evaluate_path(snapshot["id"])).status_code == 200

    history = client.get(history_path()).json()
    assert [record["sequence"] for record in history] == [1, 2]
    for record in history:
        assert record["dataset"] == "orders"
        assert record["version"] == 1
        assert record["row_count"] == len(ROWS)
        # Rows 1, 2 and 3 violate at least one rule.
        assert record["violation_row_count"] == 3
        assert [r["name"] for r in record["results"]] == [
            "id required",
            "amount in range",
            "unique ids",
        ]


def test_snapshot_evaluation_feeds_diff(client: TestClient) -> None:
    setup_with_rules(client)
    first = make_snapshot(client, ROWS)
    second = make_snapshot(client, [{"id": 1, "amount": 1.0, "region": "eu"}])

    assert client.post(evaluate_path(first["id"])).status_code == 200
    assert client.post(evaluate_path(second["id"])).status_code == 200

    diff = client.get(f"{history_path()}/diff")
    assert diff.status_code == 200, diff.text
    body = diff.json()
    assert body["from_sequence"] == 1
    assert body["to_sequence"] == 2
    assert body["removed_violation_rows"] == [1, 2, 3]
    assert body["added_violation_rows"] == []


# --------------------------------------------------------------------------- #
# Path resolution and request shape
# --------------------------------------------------------------------------- #


def test_unknown_dataset_version_or_snapshot_is_404(client: TestClient) -> None:
    setup_with_rules(client)
    snapshot = make_snapshot(client, ROWS)

    response = client.post(evaluate_path(snapshot["id"], dataset="nope"))
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}

    response = client.post(evaluate_path(snapshot["id"], version=9))
    assert response.status_code == 404

    response = client.post(evaluate_path(9999))
    assert response.status_code == 404


def test_404_takes_precedence_over_request_shape(client: TestClient) -> None:
    response = client.post(
        evaluate_path(1, dataset="nope"),
        content=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}


def test_snapshot_of_another_version_is_422_and_writes_nothing(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    # A second version with its own snapshot.
    response = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )
    assert response.status_code == 201, response.text
    other_snapshot = make_snapshot(client, [{"id": 1}], version=2)

    response = client.post(evaluate_path(other_snapshot["id"], version=1))
    assert response.status_code == 422
    assert set(response.json()) == {"error", "detail"}

    # A snapshot of another dataset is likewise a 422.
    create_dataset_and_version(client, dataset="customers")
    foreign = make_snapshot(client, [{"id": 1}], dataset="customers")
    response = client.post(evaluate_path(foreign["id"]))
    assert response.status_code == 422

    assert client.get(history_path()).json() == []


def test_request_body_is_422_and_writes_nothing(client: TestClient) -> None:
    setup_with_rules(client)
    snapshot = make_snapshot(client, ROWS)

    response = client.post(
        evaluate_path(snapshot["id"]),
        content=b'{"rows": []}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert set(response.json()) == {"error", "detail"}
    assert client.get(history_path()).json() == []


def test_query_parameters_are_422_and_write_nothing(client: TestClient) -> None:
    setup_with_rules(client)
    snapshot = make_snapshot(client, ROWS)

    response = client.post(evaluate_path(snapshot["id"]) + "?dry_run=true")
    assert response.status_code == 422
    assert set(response.json()) == {"error", "detail"}
    assert client.get(history_path()).json() == []
