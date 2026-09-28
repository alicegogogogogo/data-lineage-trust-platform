"""Tests for the as-of (point-in-time) single evaluation look-back."""

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


def at_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{rules_path(dataset, version)}/evaluations/at"


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


def evaluation_at(client: TestClient, timestamp: str, **path: object) -> dict:
    response = client.get(
        at_path(**path),  # type: ignore[arg-type]
        params={"timestamp": timestamp},
    )
    assert response.status_code == 200, response.text
    return response.json()


EMPTY_RESULT = {
    "sequence": None,
    "dataset": "orders",
    "version": 1,
    "row_count": None,
    "violation_row_count": None,
    "results": [],
    "created_at": None,
}

RECORD_KEYS = [
    "sequence",
    "dataset",
    "version",
    "row_count",
    "violation_row_count",
    "results",
    "created_at",
]


# --------------------------------------------------------------------------- #
# Windowed selection
# --------------------------------------------------------------------------- #


def test_at_with_no_evaluation_in_window_is_an_explicit_empty_result(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})

    assert evaluation_at(
        client, iso(datetime.now(timezone.utc) + timedelta(days=1))
    ) == EMPTY_RESULT


def test_at_before_any_evaluation_is_an_explicit_empty_result(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    evaluate_at = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    assert evaluation_at(client, iso(evaluate_at - timedelta(days=1))) == EMPTY_RESULT

    # A future evaluation only enters the window once the timestamp passes it.
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    (first_at,) = evaluation_times(client)
    assert evaluation_at(client, iso(first_at - timedelta(seconds=1))) == EMPTY_RESULT


def test_at_returns_the_highest_sequence_evaluation_in_the_window(
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

    after_first = evaluation_at(
        client, iso(first_at + (second_at - first_at) / 2)
    )
    assert after_first["sequence"] == 1
    assert after_first["row_count"] == 3
    assert after_first["violation_row_count"] == 3
    assert after_first["results"] == [
        {"rule_id": rule["id"], "name": "r", "passed": False, "violations": [0, 1, 2]}
    ]
    assert after_first["created_at"] == first_at.isoformat()

    after_second = evaluation_at(
        client, iso(second_at + (third_at - second_at) / 2)
    )
    assert after_second["sequence"] == 2
    assert after_second["row_count"] == 1
    assert after_second["violation_row_count"] == 1

    after_third = evaluation_at(client, iso(third_at + timedelta(milliseconds=1)))
    assert after_third["sequence"] == 3
    assert after_third["row_count"] == 2
    assert after_third["violation_row_count"] == 1


def test_at_boundary_instant_includes_the_evaluation(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": 1}])
    first_at, second_at = evaluation_times(client)

    assert evaluation_at(client, iso(first_at))["sequence"] == 1
    assert evaluation_at(client, iso(second_at))["sequence"] == 2


def test_at_window_grows_as_evaluations_are_persisted(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": None}])
    times = evaluation_times(client)
    future = iso(times[-1] + timedelta(days=1))

    assert evaluation_at(client, future)["sequence"] == 2

    # A newly persisted evaluation enters the window of the same timestamp.
    evaluate(client, [{"id": 1}])
    result = evaluation_at(client, future)
    assert result["sequence"] == 3
    assert result["violation_row_count"] == 0


def test_at_accepts_any_timezone_offset(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": 1}])
    _, second_at = evaluation_times(client)

    shifted = second_at.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert evaluation_at(client, shifted.isoformat())["sequence"] == 2


def test_at_is_scoped_to_its_version(client: TestClient) -> None:
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

    assert evaluation_at(client, later)["sequence"] == 1
    assert evaluation_at(client, later, version=2) == {
        **EMPTY_RESULT,
        "version": 2,
    }


# --------------------------------------------------------------------------- #
# Parity with the persisted history record and read-only semantics
# --------------------------------------------------------------------------- #


def test_at_matches_the_persisted_history_record_byte_for_byte_fields(
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

    first = evaluation_at(
        client, iso(times[0] + (times[1] - times[0]) / 2)
    )
    latest = evaluation_at(client, iso(times[-1] + timedelta(days=1)))
    history = client.get(history_path()).json()
    assert first == history[0]
    assert latest == history[-1]
    for record in (first, latest):
        assert list(record) == RECORD_KEYS


def test_at_reads_recorded_results_without_rerunning_rules(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": None}])
    # Disabling the rule after both evaluations must not change the look-back:
    # the persisted summary, not the current rule state, is returned.
    assert (
        client.patch(f"{rules_path()}/{rule['id']}", json={"enabled": False}).status_code
        == 200
    )
    times = evaluation_times(client)

    result = evaluation_at(client, iso(times[-1] + timedelta(days=1)))
    assert result["results"] == [
        {"rule_id": rule["id"], "name": "r", "passed": False, "violations": [0]}
    ]


# --------------------------------------------------------------------------- #
# Request shape and 404 precedence
# --------------------------------------------------------------------------- #


def test_at_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    timestamp = iso(datetime.now(timezone.utc))
    for path in (at_path("ghost"), at_path("orders", 9)):
        response = client.get(path, params={"timestamp": timestamp})
        assert response.status_code == 404
        body = response.json()
        assert body["error"] == "not_found"
        assert "detail" in body


def test_at_404_precedes_timestamp_and_shape_checks(client: TestClient) -> None:
    make_dataset_with_version(client)
    ghost = at_path("ghost")
    assert client.get(ghost).status_code == 404  # missing timestamp
    assert client.get(ghost, params={"timestamp": "not-a-time"}).status_code == 404
    assert client.get(ghost, params={"x": "1"}).status_code == 404
    assert client.request("GET", ghost, content=b"{}").status_code == 404


def test_at_rejects_missing_repeated_and_bad_timestamps(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    path = at_path()
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


def test_at_rejects_any_request_body(client: TestClient) -> None:
    make_dataset_with_version(client)
    timestamp = iso(datetime.now(timezone.utc))
    for content in (b"{}", b" ", b" \t\n"):
        response = client.request(
            "GET", at_path(), params={"timestamp": timestamp}, content=content
        )
        assert response.status_code == 422, response.text
        body = response.json()
        assert body["error"] == "validation_error"
        assert "detail" in body


def test_at_only_accepts_get(client: TestClient) -> None:
    make_dataset_with_version(client)
    timestamp = iso(datetime.now(timezone.utc))
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        response = client.request(
            method, at_path(), params={"timestamp": timestamp}
        )
        assert response.status_code == 405, response.text


def test_at_rejections_keep_the_two_key_error_shape(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.get(at_path())
    assert response.status_code == 422
    assert list(response.json()) == ["error", "detail"]


# --------------------------------------------------------------------------- #
# Determinism and read-only behaviour
# --------------------------------------------------------------------------- #


def test_at_response_is_byte_identical_across_calls_and_a_restart(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": 1}])
    times = evaluation_times(client)
    timestamp = iso(times[-1] + timedelta(days=1))

    first = client.get(at_path(), params={"timestamp": timestamp})
    assert first.status_code == 200
    assert first.text.endswith("}\n")
    assert client.get(at_path(), params={"timestamp": timestamp}).text == first.text
    assert first.text == json.dumps(
        first.json(), separators=(",", ":"), ensure_ascii=False
    ) + "\n"
    assert list(first.json()) == RECORD_KEYS
    assert list(first.json()["results"][0]) == [
        "rule_id",
        "name",
        "passed",
        "violations",
    ]

    from app.main import app

    restarted = TestClient(app)
    assert restarted.get(
        at_path(), params={"timestamp": timestamp}
    ).text == first.text


def test_at_empty_result_is_byte_deterministic(client: TestClient) -> None:
    make_dataset_with_version(client)
    timestamp = iso(datetime.now(timezone.utc) + timedelta(days=1))
    first = client.get(at_path(), params={"timestamp": timestamp})
    assert first.status_code == 200
    assert first.text == (
        '{"sequence":null,"dataset":"orders","version":1,"row_count":null,'
        '"violation_row_count":null,"results":[],"created_at":null}\n'
    )
    assert client.get(at_path(), params={"timestamp": timestamp}).text == first.text


def test_at_writes_nothing_and_leaves_other_reads_unchanged(
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

    client.get(at_path(), params={"timestamp": timestamp})
    client.get(at_path(), params={"timestamp": times[0].isoformat()})
    # Rejections write nothing either.
    client.get(at_path())
    client.request(
        "GET", at_path(), params={"timestamp": timestamp}, content=b"{}"
    )
    client.get(at_path(), params={"timestamp": "bad"})

    assert client.get(history_path()).json() == history_before
    assert client.get(rules_path()).json() == rules_before
