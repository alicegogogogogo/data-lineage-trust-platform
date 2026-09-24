"""Tests for persisted quality-rule evaluation history and comparison."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from tests.test_persistence import _run  # reuse the subprocess runner

BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "amount", "type": "decimal", "nullable": True},
    {"name": "region", "type": "string", "nullable": True},
]


def make_dataset_with_version(client: TestClient, name: str = "orders") -> int:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions", json={"fields": BASE_FIELDS}
    )
    assert response.status_code == 201, response.text
    return response.json()["version"]


def rules_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/quality-rules"


def history_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{rules_path(dataset, version)}/history"


def compare_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{history_path(dataset, version)}/compare"


def evaluate(client: TestClient, rows: list, dataset: str = "orders", version: int = 1):
    response = client.post(
        f"{rules_path(dataset, version)}/evaluate", json={"rows": rows}
    )
    assert response.status_code == 200, response.text
    return response.json()


def create_rule(client: TestClient, payload: dict) -> dict:
    response = client.post(rules_path(), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# History persistence
# --------------------------------------------------------------------------- #


def test_successful_evaluation_is_recorded_with_full_summary(client: TestClient) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "id required", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": 1}, {"amount": 2}, {"id": None}])

    history = client.get(history_path())
    assert history.status_code == 200, history.text
    body = history.json()
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert len(body["evaluations"]) == 1
    entry = body["evaluations"][0]
    assert set(entry) == {
        "sequence",
        "dataset",
        "version",
        "row_count",
        "violation_count",
        "results",
        "created_at",
    }
    assert entry["sequence"] == 1
    assert entry["dataset"] == "orders"
    assert entry["version"] == 1
    assert entry["row_count"] == 3
    # Distinct violating rows across all rules: rows 1 and 2, counted once.
    assert entry["violation_count"] == 2
    assert entry["results"] == [
        {
            "rule_id": rule["id"],
            "name": "id required",
            "passed": False,
            "violations": [1, 2],
        }
    ]


def test_history_is_ordered_by_occurrence_with_continuous_sequences(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "id required", "kind": "not_null", "params": {"field": "id"}}
    )
    for rows in ([{"id": None}], [{"id": 1}], [{"id": None}, {"id": None}]):
        evaluate(client, rows)

    entries = client.get(history_path()).json()["evaluations"]
    assert [entry["sequence"] for entry in entries] == [1, 2, 3]
    assert [entry["row_count"] for entry in entries] == [1, 1, 2]
    assert [entry["violation_count"] for entry in entries] == [1, 0, 2]
    # Occurrence order is monotonic in created_at as well.
    assert [entry["created_at"] for entry in entries] == sorted(
        entry["created_at"] for entry in entries
    )


def test_empty_row_set_is_recorded_with_zero_counts(client: TestClient) -> None:
    make_dataset_with_version(client)
    first = create_rule(
        client, {"name": "a", "kind": "not_null", "params": {"field": "id"}}
    )
    second = create_rule(
        client,
        {
            "name": "b",
            "kind": "numeric_range",
            "params": {"field": "amount", "min": 0, "max": 1},
        },
    )
    evaluate(client, [])

    entry = client.get(history_path()).json()["evaluations"][0]
    assert entry["row_count"] == 0
    assert entry["violation_count"] == 0
    assert [result["rule_id"] for result in entry["results"]] == [
        first["id"],
        second["id"],
    ]
    assert all(result["passed"] is True for result in entry["results"])
    assert all(result["violations"] == [] for result in entry["results"])


def test_evaluation_without_rules_still_records(client: TestClient) -> None:
    make_dataset_with_version(client)
    evaluate(client, [{"id": 1}, {"id": 2}])
    entry = client.get(history_path()).json()["evaluations"][0]
    assert entry["results"] == []
    assert entry["row_count"] == 2
    assert entry["violation_count"] == 0


def test_single_row_and_many_duplicate_rows_are_counted_accurately(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "u", "kind": "unique", "params": {"fields": ["id"]}}
    )
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": 1}] * 50)

    entries = client.get(history_path()).json()["evaluations"]
    assert entries[0]["row_count"] == 1
    assert entries[0]["violation_count"] == 0
    assert entries[1]["row_count"] == 50
    # Every member of the duplicate group is a violation: all 50 identical rows.
    assert entries[1]["violation_count"] == 50
    assert entries[1]["results"][0]["violations"] == list(range(50))


def test_violation_count_counts_each_row_once_even_with_several_rules(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "id required", "kind": "not_null", "params": {"field": "id"}}
    )
    create_rule(
        client,
        {
            "name": "amount bounded",
            "kind": "numeric_range",
            "params": {"field": "amount", "min": 0, "max": 10},
        },
    )
    # Row 0 violates both rules at once but is a single violating row.
    evaluate(client, [{}, {"id": 1, "amount": 5}])

    entry = client.get(history_path()).json()["evaluations"][0]
    assert entry["violation_count"] == 1
    assert [result["violations"] for result in entry["results"]] == [[0], [0]]


def test_rejected_or_failed_evaluation_leaves_no_record(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )

    # Malformed request shapes never reach the persistence step.
    assert client.post(
        f"{rules_path()}/evaluate", json={"rows": "not-a-list"}
    ).status_code == 422
    assert client.post(f"{rules_path()}/evaluate", json={}).status_code == 422
    assert client.post(
        f"{rules_path('ghost')}/evaluate", json={"rows": []}
    ).status_code == 404
    assert client.post(
        f"{rules_path('orders', 9)}/evaluate", json={"rows": []}
    ).status_code == 404

    assert client.get(history_path()).json()["evaluations"] == []

    # A successful evaluation afterwards is still sequence 1.
    evaluate(client, [{"id": 1}])
    assert client.get(history_path()).json()["evaluations"][0]["sequence"] == 1


def test_evaluate_response_shape_is_unchanged(client: TestClient) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    body = evaluate(client, [{"id": None}])
    assert set(body) == {"dataset", "version", "results"}
    assert set(body["results"][0]) == {"rule_id", "name", "passed", "violations"}
    assert body["results"][0]["rule_id"] == rule["id"]


def test_history_is_scoped_per_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": BASE_FIELDS},
        ).status_code
        == 201
    )
    evaluate(client, [{"id": None}], version=1)
    evaluate(client, [{"id": 1}], version=2)

    v1 = client.get(history_path(version=1)).json()["evaluations"]
    v2 = client.get(history_path(version=2)).json()["evaluations"]
    assert [entry["sequence"] for entry in v1] == [1]
    assert [entry["sequence"] for entry in v2] == [1]
    assert v1[0]["violation_count"] == 1
    assert v2[0]["violation_count"] == 0


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #


def test_compare_reports_new_and_disappeared_violations_and_count_deltas(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": 1}, {"id": None}, {"id": None}])  # violations 1, 2
    evaluate(client, [{"id": None}, {"id": 1}, {"id": 1}])  # violation 0

    body = client.get(compare_path())
    assert body.status_code == 200, body.text
    diff = body.json()
    assert diff["dataset"] == "orders"
    assert diff["version"] == 1
    assert diff["from_evaluation"]["sequence"] == 1
    assert diff["to_evaluation"]["sequence"] == 2
    assert diff["from_evaluation"]["row_count"] == 3
    assert diff["to_evaluation"]["violation_count"] == 1
    assert diff["new_violations"] == [{"rule_id": 1, "row_index": 0}]
    assert diff["disappeared_violations"] == [
        {"rule_id": 1, "row_index": 1},
        {"rule_id": 1, "row_index": 2},
    ]
    assert diff["rule_changes"] == [
        {
            "rule_id": 1,
            "name": "r",
            "previous_violation_count": 2,
            "latest_violation_count": 1,
            "delta": -1,
        }
    ]


def test_compare_observes_increase_decrease_and_unchanged_counts(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "inc", "kind": "not_null", "params": {"field": "id"}}
    )
    create_rule(
        client, {"name": "dec", "kind": "not_null", "params": {"field": "amount"}}
    )
    create_rule(
        client, {"name": "flat", "kind": "unique", "params": {"fields": ["region"]}}
    )

    # First evaluation: inc 1 (row 0), dec 2 (rows 0, 1), flat 0 (distinct
    # regions, with a single missing id allowed).
    evaluate(
        client,
        [
            {"amount": None, "region": "a"},
            {"id": 2, "amount": None, "region": "b"},
            {"id": 3, "amount": 30, "region": "c"},
        ],
    )
    # Second evaluation: inc 2 (rows 0, 1), dec 0, flat 0.
    evaluate(
        client,
        [
            {"amount": 1, "region": "d"},
            {"amount": 2, "region": "e"},
            {"id": 3, "amount": 3, "region": "f"},
            {"id": 4, "amount": 4, "region": "g"},
        ],
    )

    changes = {
        change["name"]: change
        for change in client.get(compare_path()).json()["rule_changes"]
    }
    assert changes["inc"]["delta"] == 1
    assert changes["dec"]["delta"] == -2
    assert changes["flat"]["delta"] == 0
    assert changes["flat"]["previous_violation_count"] == 0
    assert changes["flat"]["latest_violation_count"] == 0


def test_compare_always_uses_the_latest_two_evaluations(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": 1}])
    evaluate(client, [{"id": None}, {"id": None}])

    diff = client.get(compare_path()).json()
    assert diff["from_evaluation"]["sequence"] == 2
    assert diff["to_evaluation"]["sequence"] == 3
    assert diff["disappeared_violations"] == []
    assert [v["row_index"] for v in diff["new_violations"]] == [0, 1]
    assert diff["rule_changes"][0]["delta"] == 2


def test_compare_with_disabled_rule_marks_missing_side_null(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])
    assert (
        client.patch(f"{rules_path()}/{rule['id']}", json={"enabled": False}).status_code
        == 200
    )
    evaluate(client, [{"id": None}, {"id": None}])

    diff = client.get(compare_path()).json()
    (change,) = diff["rule_changes"]
    assert change["rule_id"] == rule["id"]
    assert change["name"] == "r"
    assert change["previous_violation_count"] == 1
    assert change["latest_violation_count"] is None
    assert change["delta"] is None
    # The rule's violations disappeared from the latest result; no "new" side
    # can exist for a rule that did not run.
    assert diff["new_violations"] == []
    assert diff["disappeared_violations"] == [{"rule_id": rule["id"], "row_index": 0}]


def test_compare_with_newly_enabled_rule_marks_previous_side_null(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    assert (
        client.patch(f"{rules_path()}/{rule['id']}", json={"enabled": False}).status_code
        == 200
    )
    evaluate(client, [{"id": None}])  # rule absent
    assert (
        client.patch(f"{rules_path()}/{rule['id']}", json={"enabled": True}).status_code
        == 200
    )
    evaluate(client, [{"id": None}])  # rule present with one violation

    diff = client.get(compare_path()).json()
    (change,) = diff["rule_changes"]
    assert change["previous_violation_count"] is None
    assert change["latest_violation_count"] == 1
    assert change["delta"] is None
    assert diff["disappeared_violations"] == []
    assert diff["new_violations"] == [{"rule_id": rule["id"], "row_index": 0}]


def test_compare_with_different_row_set_sizes_uses_recorded_indices(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}] * 4)  # violations 0..3
    evaluate(client, [{"id": None}])  # violation 0

    diff = client.get(compare_path()).json()
    assert diff["from_evaluation"]["row_count"] == 4
    assert diff["to_evaluation"]["row_count"] == 1
    assert diff["new_violations"] == []
    assert [v["row_index"] for v in diff["disappeared_violations"]] == [1, 2, 3]
    assert diff["rule_changes"][0]["delta"] == -3


def test_compare_empty_history_returns_explicit_empty_result(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    diff = client.get(compare_path())
    assert diff.status_code == 200, diff.text
    assert diff.json() == {
        "dataset": "orders",
        "version": 1,
        "from_evaluation": None,
        "to_evaluation": None,
        "new_violations": [],
        "disappeared_violations": [],
        "rule_changes": [],
    }


def test_compare_single_evaluation_returns_explicit_empty_result(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])
    diff = client.get(compare_path()).json()
    assert diff["from_evaluation"] is None
    assert diff["to_evaluation"] is None
    assert diff["new_violations"] == []
    assert diff["disappeared_violations"] == []
    assert diff["rule_changes"] == []


def test_compare_keeps_rule_results_independent(client: TestClient) -> None:
    make_dataset_with_version(client)
    r1 = create_rule(
        client, {"name": "a", "kind": "not_null", "params": {"field": "id"}}
    )
    r2 = create_rule(
        client, {"name": "b", "kind": "not_null", "params": {"field": "amount"}}
    )
    # Evaluation 1: r1 violates row 1, r2 violates row 0.
    evaluate(client, [{"id": 1, "amount": None}, {"id": None, "amount": 2}])
    # Evaluation 2: the same rows arrive in the opposite order.
    evaluate(client, [{"id": None, "amount": 2}, {"id": 1, "amount": None}])

    diff = client.get(compare_path()).json()
    # Index 0 moved from r2 to r1; index 1 moved from r1 to r2.
    assert diff["new_violations"] == [
        {"rule_id": r1["id"], "row_index": 0},
        {"rule_id": r2["id"], "row_index": 1},
    ]
    assert diff["disappeared_violations"] == [
        {"rule_id": r1["id"], "row_index": 1},
        {"rule_id": r2["id"], "row_index": 0},
    ]
    # Both keep one violation overall: equal counts, zero delta.
    by_rule = {change["rule_id"]: change for change in diff["rule_changes"]}
    assert by_rule[r1["id"]]["delta"] == 0
    assert by_rule[r2["id"]]["delta"] == 0


# --------------------------------------------------------------------------- #
# Errors and request shape
# --------------------------------------------------------------------------- #


def test_history_and_compare_unknown_dataset_or_version_return_404(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    for url in (
        history_path("ghost"),
        history_path("orders", 7),
        compare_path("ghost"),
        compare_path("orders", 7),
    ):
        response = client.get(url)
        assert response.status_code == 404, url
        assert response.json()["error"] == "not_found"


def test_history_rejects_body_and_query_parameters_with_422(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    evaluate(client, [])

    with_body = client.request("GET", history_path(), content=b'{"x": 1}')
    with_query = client.get(f"{history_path()}?bogus=1")
    for response in (with_body, with_query):
        assert response.status_code == 422
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"

    # The rejections wrote nothing and did not advance the sequence.
    entries = client.get(history_path()).json()["evaluations"]
    assert [entry["sequence"] for entry in entries] == [1]


def test_compare_rejects_body_and_query_parameters_with_422_and_no_write(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    evaluate(client, [{"id": 1}])
    evaluate(client, [{"id": 1}])

    with_body = client.request("GET", compare_path(), content=b'{"x": 1}')
    with_query = client.get(f"{compare_path()}?bogus=1")
    for response in (with_body, with_query):
        assert response.status_code == 422
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"

    # Exactly the two evaluations exist: no write was triggered.
    entries = client.get(history_path()).json()["evaluations"]
    assert [entry["sequence"] for entry in entries] == [1, 2]


def test_body_on_history_for_unknown_dataset_stays_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    # Path resolution takes precedence over the body check.
    response = client.request(
        "GET", history_path("ghost"), content=b'{"x": 1}'
    )
    assert response.status_code == 404
    response = client.get(f"{history_path('ghost')}?bogus=1")
    assert response.status_code == 404


def test_error_responses_do_not_leak_internals(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.request("GET", compare_path(), content=b"not json")
    text = response.text.lower()
    assert "traceback" not in text
    assert "sqlite" not in text
    assert "select" not in text


# --------------------------------------------------------------------------- #
# Concurrency and restart
# --------------------------------------------------------------------------- #


def test_concurrent_evaluations_never_reuse_or_skip_a_sequence(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )

    thread_count = 8
    per_thread = 25
    barrier = threading.Barrier(thread_count)
    errors: list[str] = []

    def worker() -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        for index in range(per_thread):
            rows = [{"id": None}] if index % 2 else [{"id": 1}]
            response = thread_client.post(
                f"{rules_path()}/evaluate", json={"rows": rows}
            )
            if response.status_code != 200:  # pragma: no cover - failure path
                errors.append(response.text)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    entries = client.get(history_path()).json()["evaluations"]
    assert len(entries) == thread_count * per_thread
    assert [entry["sequence"] for entry in entries] == list(
        range(1, thread_count * per_thread + 1)
    )
    assert len({entry["created_at"] + str(entry["sequence"]) for entry in entries}) == len(
        entries
    )


CREATE_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
client.post("/datasets", json={"name": "orders"})
client.post(
    "/datasets/orders/versions",
    json={"fields": [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "amount", "type": "decimal", "nullable": True},
    ]},
)
rule = client.post(
    "/datasets/orders/versions/1/quality-rules",
    json={"name": "r", "kind": "not_null", "params": {"field": "id"}},
).json()
client.post(
    "/datasets/orders/versions/1/quality-rules/evaluate",
    json={"rows": [{"id": 1}, {"id": None}]},
)
client.post(
    "/datasets/orders/versions/1/quality-rules/evaluate",
    json={"rows": [{"id": None}, {"id": 1}, {"id": None}]},
)
print("created")
"""

VERIFY_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
history = client.get("/datasets/orders/versions/1/quality-rules/history")
assert history.status_code == 200, history.text
entries = history.json()["evaluations"]
assert [e["sequence"] for e in entries] == [1, 2]
assert entries[0]["row_count"] == 2 and entries[0]["violation_count"] == 1
assert entries[1]["row_count"] == 3 and entries[1]["violation_count"] == 2
assert entries[0]["results"][0]["violations"] == [1]
assert entries[1]["results"][0]["violations"] == [0, 2]

compare = client.get("/datasets/orders/versions/1/quality-rules/history/compare")
assert compare.status_code == 200, compare.text
diff = compare.json()
assert diff["from_evaluation"]["sequence"] == 1
assert diff["to_evaluation"]["sequence"] == 2
assert diff["new_violations"] == [
    {"rule_id": 1, "row_index": 0},
    {"rule_id": 1, "row_index": 2},
]
assert diff["disappeared_violations"] == [{"rule_id": 1, "row_index": 1}]
assert diff["rule_changes"][0]["delta"] == 1
print(json.dumps({"sequences": [e["sequence"] for e in entries]}))
"""


def test_history_and_compare_survive_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "quality-history.db"
    assert _run(db_path, CREATE_SCRIPT) == "created"
    output = json.loads(_run(db_path, VERIFY_SCRIPT))
    assert output["sequences"] == [1, 2]
