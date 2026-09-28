"""Tests for the as-of (point-in-time) quality rule evaluation diff."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

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


def diff_at_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{rules_path(dataset, version)}/evaluations/diff/at"


def diff_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{rules_path(dataset, version)}/evaluations/diff"


def history_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{rules_path(dataset, version)}/evaluations"


def create_rule(client: TestClient, payload: dict) -> dict:
    response = client.post(rules_path(), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def evaluate(client: TestClient, rows: list[dict], **path: object) -> None:
    response = client.post(
        rules_path(**path) + "/evaluate",  # type: ignore[arg-type]
        json={"rows": rows},
    )
    assert response.status_code == 200, response.text


def evaluation_times(client: TestClient) -> list[datetime]:
    history = client.get(history_path()).json()
    return [datetime.fromisoformat(record["created_at"]) for record in history]


def iso(moment: datetime) -> str:
    return moment.isoformat()


def diff_at(client: TestClient, timestamp: str, **path: object) -> dict:
    response = client.get(
        diff_at_path(**path),  # type: ignore[arg-type]
        params={"timestamp": timestamp},
    )
    assert response.status_code == 200, response.text
    return response.json()


EMPTY_RESULT = {
    "dataset": "orders",
    "version": 1,
    "from_sequence": None,
    "to_sequence": None,
    "added_violation_rows": [],
    "removed_violation_rows": [],
    "rules": [],
}


# --------------------------------------------------------------------------- #
# Windowed pair selection
# --------------------------------------------------------------------------- #


def test_diff_at_with_no_evaluation_in_window_is_an_explicit_empty_result(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})

    diff = diff_at(client, iso(datetime.now(timezone.utc) + timedelta(days=1)))
    assert diff == EMPTY_RESULT


def test_diff_at_with_one_evaluation_in_window_is_an_explicit_empty_result(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    (first_at,) = evaluation_times(client)

    diff = diff_at(client, iso(first_at))
    assert diff["from_sequence"] is None
    assert diff["to_sequence"] is None
    assert diff["added_violation_rows"] == []
    assert diff["removed_violation_rows"] == []
    assert diff["rules"] == []


def test_diff_at_before_any_evaluation_is_an_explicit_empty_result(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    evaluate_at = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    diff = diff_at(client, iso(evaluate_at - timedelta(days=1)))
    assert diff == EMPTY_RESULT

    # Future evaluations only enter the window once the timestamp passes them.
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    (first_at,) = evaluation_times(client)
    assert diff_at(client, iso(first_at - timedelta(seconds=1))) == EMPTY_RESULT


def test_diff_at_compares_the_two_highest_sequence_evaluations_in_the_window(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )

    evaluate(client, [{"id": None}, {"id": None}, {"id": None}])  # sequence 1
    evaluate(client, [{"id": None}])  # sequence 2
    evaluate(client, [{"id": None}, {"id": 1}])  # sequence 3
    first_at, second_at, third_at = evaluation_times(client)

    after_first = diff_at(client, iso(first_at + timedelta(milliseconds=1)))
    assert after_first["from_sequence"] is None
    assert after_first["to_sequence"] is None

    after_second = diff_at(
        client, iso(second_at + (third_at - second_at) / 2)
    )
    assert (
        after_second["from_sequence"],
        after_second["to_sequence"],
    ) == (1, 2)
    assert after_second["rules"][0]["rule_id"] == rule["id"]
    assert after_second["rules"][0]["before"] == {
        "violation_count": 3,
        "violations": [0, 1, 2],
    }
    assert after_second["rules"][0]["after"] == {
        "violation_count": 1,
        "violations": [0],
    }
    assert after_second["removed_violation_rows"] == [1, 2]

    after_third = diff_at(client, iso(third_at + timedelta(milliseconds=1)))
    assert (after_third["from_sequence"], after_third["to_sequence"]) == (2, 3)
    assert after_third["rules"][0]["after"] == {
        "violation_count": 1,
        "violations": [0],
    }


def test_diff_at_boundary_instant_includes_the_evaluation(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": 1}])
    _, second_at = evaluation_times(client)

    diff = diff_at(client, iso(second_at))
    assert (diff["from_sequence"], diff["to_sequence"]) == (1, 2)


def test_diff_at_window_grows_as_evaluations_are_persisted(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": None}])
    times = evaluation_times(client)
    future = iso(times[-1] + timedelta(days=1))

    assert (
        diff_at(client, future)["from_sequence"],
        diff_at(client, future)["to_sequence"],
    ) == (1, 2)

    # A newly persisted evaluation enters the window of the same timestamp.
    evaluate(client, [{"id": 1}])
    diff = diff_at(client, future)
    assert (diff["from_sequence"], diff["to_sequence"]) == (2, 3)
    assert diff["removed_violation_rows"] == [0]


def test_diff_at_accepts_any_timezone_offset(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": 1}])
    _, second_at = evaluation_times(client)

    shifted = second_at.astimezone(timezone(timedelta(hours=5, minutes=30)))
    diff = diff_at(client, shifted.isoformat())
    assert (diff["from_sequence"], diff["to_sequence"]) == (1, 2)


def test_diff_at_is_scoped_to_its_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": 1}])
    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "id", "type": "bigint", "nullable": False}]},
        ).status_code
        == 201
    )
    later = iso(datetime.now(timezone.utc) + timedelta(days=1))

    assert (
        diff_at(client, later)["from_sequence"],
        diff_at(client, later)["to_sequence"],
    ) == (1, 2)
    other = diff_at(client, later, dataset="orders", version=2)
    assert other == {**EMPTY_RESULT, "version": 2}


# --------------------------------------------------------------------------- #
# Comparison semantics (identical to the pairwise diff)
# --------------------------------------------------------------------------- #


def test_diff_at_reports_new_and_disappeared_rows_and_count_delta(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )

    evaluate(client, [{"id": None}, {"id": 1}, {"id": None}])  # violations 0, 2
    evaluate(client, [{"id": 1}, {"id": None}, {"id": None}, {"id": 2}])  # 1, 2
    times = evaluation_times(client)

    diff = diff_at(client, iso(times[-1] + timedelta(milliseconds=1)))
    assert (diff["from_sequence"], diff["to_sequence"]) == (1, 2)
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


def test_diff_at_handles_a_rule_disabled_between_the_windowed_evaluations(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    kept = create_rule(
        client, {"name": "kept", "kind": "not_null", "params": {"field": "id"}}
    )
    paused = create_rule(
        client, {"name": "paused", "kind": "not_null", "params": {"field": "amount"}}
    )

    evaluate(client, [{"id": None, "amount": None}])
    assert (
        client.patch(f"{rules_path()}/{paused['id']}", json={"enabled": False}).status_code
        == 200
    )
    evaluate(client, [{"id": None, "amount": None}])
    times = evaluation_times(client)

    diff = diff_at(client, iso(times[-1] + timedelta(milliseconds=1)))
    by_name = {entry["name"]: entry for entry in diff["rules"]}
    missing = by_name["paused"]
    assert missing["rule_id"] == paused["id"]
    assert missing["before"] == {"violation_count": 1, "violations": [0]}
    assert missing["after"] is None
    assert missing["added_violations"] is None
    assert missing["removed_violations"] is None
    assert missing["violation_count_delta"] is None
    assert by_name["kept"]["rule_id"] == kept["id"]
    assert by_name["kept"]["violation_count_delta"] == 0
    assert diff["added_violation_rows"] == []
    assert diff["removed_violation_rows"] == []


def test_diff_at_handles_a_rule_created_between_the_windowed_evaluations(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    evaluate(client, [{"id": None}])
    created = create_rule(
        client, {"name": "late", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])
    times = evaluation_times(client)

    diff = diff_at(client, iso(times[-1] + timedelta(milliseconds=1)))
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
    assert diff["added_violation_rows"] == [0]
    assert diff["removed_violation_rows"] == []


def test_diff_at_reads_recorded_results_without_rerunning_rules(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": None}])
    # Disabling the rule after both evaluations must not change the windowed
    # diff: the persisted summaries, not the current rule state, are compared.
    assert (
        client.patch(f"{rules_path()}/{rule['id']}", json={"enabled": False}).status_code
        == 200
    )
    times = evaluation_times(client)

    diff = diff_at(client, iso(times[-1] + timedelta(days=1)))
    assert diff["rules"][0]["rule_id"] == rule["id"]
    assert diff["rules"][0]["before"] is not None
    assert diff["rules"][0]["after"] is not None


def test_diff_at_window_with_more_than_two_evaluations_ignores_older_pairs(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    for rows in (
        [{"id": None}],
        [{"id": 1}],
        [{"id": None}, {"id": 1}, {"id": None}],
        [{"id": 1}, {"id": 1}],
    ):
        evaluate(client, rows)
    times = evaluation_times(client)

    # A timestamp after sequence 3 compares only sequences 2 and 3; sequence
    # 1 never enters the comparison.
    diff = diff_at(client, iso(times[2] + timedelta(milliseconds=1)))
    assert (diff["from_sequence"], diff["to_sequence"]) == (2, 3)
    assert diff["added_violation_rows"] == [0, 2]
    assert diff["removed_violation_rows"] == []


# --------------------------------------------------------------------------- #
# Request shape and 404 precedence
# --------------------------------------------------------------------------- #


def test_diff_at_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    timestamp = iso(datetime.now(timezone.utc))
    for path in (diff_at_path("ghost"), diff_at_path("orders", 9)):
        response = client.get(path, params={"timestamp": timestamp})
        assert response.status_code == 404
        body = response.json()
        assert body["error"] == "not_found"
        assert "detail" in body


def test_diff_at_404_precedes_timestamp_and_shape_checks(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    ghost = diff_at_path("ghost")
    assert client.get(ghost).status_code == 404  # missing timestamp
    assert client.get(ghost, params={"timestamp": "not-a-time"}).status_code == 404
    assert client.get(ghost, params={"x": "1"}).status_code == 404
    assert client.request("GET", ghost, content=b"{}").status_code == 404


def test_diff_at_rejects_missing_repeated_and_bad_timestamps(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    path = diff_at_path()
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
        assert set(body) == {"error", "detail"}


def test_diff_at_rejects_any_request_body(client: TestClient) -> None:
    make_dataset_with_version(client)
    timestamp = iso(datetime.now(timezone.utc))
    for content in (b"{}", b" ", b" \t\n"):
        response = client.request(
            "GET", diff_at_path(), params={"timestamp": timestamp}, content=content
        )
        assert response.status_code == 422, response.text
        body = response.json()
        assert body["error"] == "validation_error"
        assert "detail" in body


def test_diff_at_only_accepts_get(client: TestClient) -> None:
    make_dataset_with_version(client)
    timestamp = iso(datetime.now(timezone.utc))
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        response = client.request(
            method, diff_at_path(), params={"timestamp": timestamp}
        )
        assert response.status_code == 405, response.text


def test_diff_at_rejections_keep_the_two_key_error_shape(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.get(diff_at_path())
    assert response.status_code == 422
    assert list(response.json()) == ["error", "detail"]


# --------------------------------------------------------------------------- #
# Determinism, parity with the pairwise diff and read-only behaviour
# --------------------------------------------------------------------------- #


def test_diff_at_matches_the_pairwise_diff_at_the_latest_window(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "a", "kind": "not_null", "params": {"field": "id"}})
    create_rule(
        client, {"name": "b", "kind": "not_null", "params": {"field": "amount"}}
    )
    evaluate(client, [{"id": None, "amount": None}])
    evaluate(client, [{"id": 1, "amount": None}, {"id": None, "amount": 1}])
    times = evaluation_times(client)
    future = iso(times[-1] + timedelta(days=1))

    windowed = diff_at(client, future)
    current = client.get(diff_path()).json()
    assert windowed == current


def test_diff_at_response_is_byte_identical_across_calls_and_a_restart(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": 1}])
    times = evaluation_times(client)
    timestamp = iso(times[-1] + timedelta(days=1))

    first = client.get(diff_at_path(), params={"timestamp": timestamp})
    assert first.status_code == 200
    assert first.text.endswith("}\n")
    assert client.get(diff_at_path(), params={"timestamp": timestamp}).text == (
        first.text
    )
    assert first.text == json.dumps(
        first.json(), separators=(",", ":"), ensure_ascii=False
    ) + "\n"
    assert list(first.json()) == [
        "dataset",
        "version",
        "from_sequence",
        "to_sequence",
        "added_violation_rows",
        "removed_violation_rows",
        "rules",
    ]
    rule_keys = list(first.json()["rules"][0])
    assert rule_keys == [
        "rule_id",
        "name",
        "before",
        "after",
        "added_violations",
        "removed_violations",
        "violation_count_delta",
    ]

    from app.main import app

    restarted = TestClient(app)
    assert restarted.get(
        diff_at_path(), params={"timestamp": timestamp}
    ).text == first.text


def test_diff_at_empty_result_is_byte_deterministic(client: TestClient) -> None:
    make_dataset_with_version(client)
    timestamp = iso(datetime.now(timezone.utc) + timedelta(days=1))
    first = client.get(diff_at_path(), params={"timestamp": timestamp})
    assert first.status_code == 200
    assert first.text == (
        '{"dataset":"orders","version":1,"from_sequence":null,'
        '"to_sequence":null,"added_violation_rows":[],'
        '"removed_violation_rows":[],"rules":[]}\n'
    )
    assert client.get(diff_at_path(), params={"timestamp": timestamp}).text == (
        first.text
    )


def test_diff_at_writes_nothing_and_leaves_other_reads_unchanged(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": 1}])
    times = evaluation_times(client)
    timestamp = iso(times[-1] + timedelta(days=1))

    history_before = client.get(history_path()).json()
    rules_before = client.get(rules_path()).json()
    current_diff_before = client.get(diff_path()).json()

    client.get(diff_at_path(), params={"timestamp": timestamp})
    client.get(diff_at_path(), params={"timestamp": times[0].isoformat()})
    # Rejections write nothing either.
    client.get(diff_at_path())
    client.request(
        "GET", diff_at_path(), params={"timestamp": timestamp}, content=b"{}"
    )
    client.get(diff_at_path(), params={"timestamp": "bad"})

    assert client.get(history_path()).json() == history_before
    assert client.get(rules_path()).json() == rules_before
    assert client.get(diff_path()).json() == current_diff_before
