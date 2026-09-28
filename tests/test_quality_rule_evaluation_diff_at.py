"""Tests for the as-of (point-in-time) quality rule evaluation diff."""

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


def history_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{rules_path(dataset, version)}/evaluations"


def diff_at_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{history_path(dataset, version)}/diff/at"


def bare_diff_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{history_path(dataset, version)}/diff"


def create_rule(client: TestClient, payload: dict) -> dict:
    response = client.post(rules_path(), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def evaluate(client: TestClient, rows: list[dict]) -> dict:
    response = client.post(rules_path() + "/evaluate", json={"rows": rows})
    assert response.status_code == 200, response.text
    return response.json()


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


# --------------------------------------------------------------------------- #
# Windowed comparison
# --------------------------------------------------------------------------- #


def test_diff_at_with_no_evaluation_in_window_is_an_explicit_empty_result(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])

    response = client.get(
        diff_at_path(),
        params={"timestamp": iso(datetime.now(timezone.utc) - timedelta(days=1))},
    )
    assert response.status_code == 200
    assert response.json() == {
        "dataset": "orders",
        "version": 1,
        "from_sequence": None,
        "to_sequence": None,
        "added_violation_rows": [],
        "removed_violation_rows": [],
        "rules": [],
    }


def test_diff_at_with_a_single_record_in_window_is_an_explicit_empty_result(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": 1}])
    first_at, second_at = evaluation_times(client)

    middle = diff_at(client, iso(first_at + (second_at - first_at) / 2))
    assert middle["from_sequence"] is None
    assert middle["to_sequence"] is None
    assert middle["added_violation_rows"] == []
    assert middle["removed_violation_rows"] == []
    assert middle["rules"] == []


def test_diff_at_compares_the_two_highest_sequences_inside_the_window(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}, {"id": None}, {"id": None}])  # seq 1
    evaluate(client, [{"id": None}])  # seq 2
    evaluate(client, [{"id": None}, {"id": 1}])  # seq 3
    times = evaluation_times(client)

    # Only seq 1 is in the window: empty.
    one = diff_at(client, iso(times[0] + (times[1] - times[0]) / 2))
    assert one["from_sequence"] is None
    assert one["to_sequence"] is None

    # Seq 1 and 2 in the window: compare them.
    two = diff_at(client, iso(times[1] + (times[2] - times[1]) / 2))
    assert two["from_sequence"] == 1
    assert two["to_sequence"] == 2
    assert two["rules"][0]["rule_id"] == rule["id"]
    assert two["rules"][0]["before"] == {
        "violation_count": 3,
        "violations": [0, 1, 2],
    }
    assert two["rules"][0]["after"] == {
        "violation_count": 1,
        "violations": [0],
    }

    # Whole history in the window: exactly the bare diff (seq 2 -> 3).
    three = diff_at(client, iso(times[2] + timedelta(days=1)))
    assert three["from_sequence"] == 2
    assert three["to_sequence"] == 3
    assert three["rules"][0]["before"] == {
        "violation_count": 1,
        "violations": [0],
    }
    assert three["rules"][0]["after"] == {
        "violation_count": 1,
        "violations": [0],
    }


def test_diff_at_matches_the_bare_diff_when_the_window_covers_everything(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "up", "kind": "not_null", "params": {"field": "id"}}
    )
    create_rule(
        client, {"name": "down", "kind": "not_null", "params": {"field": "amount"}}
    )
    evaluate(client, [{"amount": None}, {"id": 1, "amount": 1}])
    evaluate(client, [{"id": None}, {"id": None}])

    (_, second_at) = evaluation_times(client)
    windowed = diff_at(client, iso(second_at + timedelta(seconds=1)))
    assert windowed == client.get(bare_diff_path()).json()
    assert windowed["added_violation_rows"] == [1]
    assert windowed["removed_violation_rows"] == []


def test_diff_at_boundary_instant_includes_the_record(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": 1}])
    first_at, second_at = evaluation_times(client)

    # Exactly at the first record: only one record in the window.
    at_first = diff_at(client, iso(first_at))
    assert at_first["from_sequence"] is None
    assert at_first["to_sequence"] is None

    # Exactly at the second record: both participate.
    at_second = diff_at(client, iso(second_at))
    assert at_second["from_sequence"] == 1
    assert at_second["to_sequence"] == 2
    assert at_second["removed_violation_rows"] == [0]


def test_diff_at_accepts_any_timezone_offset(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": 1}])
    (_, second_at) = evaluation_times(client)

    shifted = (second_at + timedelta(seconds=1)).astimezone(
        timezone(timedelta(hours=5, minutes=30))
    )
    diff = diff_at(client, shifted.isoformat())
    assert diff["from_sequence"] == 1
    assert diff["to_sequence"] == 2


def test_diff_at_accepts_utc_z_suffix(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": 1}])

    diff = diff_at(client, "9999-01-01T00:00:00Z")
    assert diff["from_sequence"] == 1
    assert diff["to_sequence"] == 2


def test_diff_at_handles_a_rule_disabled_between_window_evaluations(
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
        client.patch(
            f"{rules_path()}/{paused['id']}", json={"enabled": False}
        ).status_code
        == 200
    )
    evaluate(client, [{"id": None, "amount": None}])
    (_, second_at) = evaluation_times(client)

    diff = diff_at(client, iso(second_at + timedelta(seconds=1)))
    by_name = {rule["name"]: rule for rule in diff["rules"]}
    assert by_name["paused"]["before"] == {
        "violation_count": 1,
        "violations": [0],
    }
    assert by_name["paused"]["after"] is None
    assert by_name["paused"]["added_violations"] is None
    assert by_name["paused"]["removed_violations"] is None
    assert by_name["paused"]["violation_count_delta"] is None
    assert by_name["kept"]["rule_id"] == kept["id"]
    assert by_name["kept"]["violation_count_delta"] == 0
    assert diff["added_violation_rows"] == []
    assert diff["removed_violation_rows"] == []


def test_diff_at_handles_a_rule_created_between_window_evaluations(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    evaluate(client, [{"id": None}])
    created = create_rule(
        client, {"name": "late", "kind": "not_null", "params": {"field": "id"}}
    )
    evaluate(client, [{"id": None}])
    (_, second_at) = evaluation_times(client)

    diff = diff_at(client, iso(second_at + timedelta(seconds=1)))
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


def test_diff_at_is_scoped_to_its_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": None}])
    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "id", "type": "bigint", "nullable": False}]},
        ).status_code
        == 201
    )
    later = iso(datetime.now(timezone.utc) + timedelta(days=1))

    assert diff_at(client, later)["to_sequence"] == 2
    other = diff_at(client, later, dataset="orders", version=2)
    assert other["version"] == 2
    assert other["from_sequence"] is None
    assert other["to_sequence"] is None


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
    evaluate(client, [])
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
        assert "detail" in body
        # Error documents never leak internals.
        assert set(body) == {"error", "detail"}


def test_diff_at_rejects_any_request_body(client: TestClient) -> None:
    make_dataset_with_version(client)
    evaluate(client, [])
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


# --------------------------------------------------------------------------- #
# Determinism and read-only behaviour
# --------------------------------------------------------------------------- #


def test_diff_at_response_is_byte_identical_across_calls_and_a_restart(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": 1}])
    timestamp = iso(datetime.now(timezone.utc) + timedelta(days=1))

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
    assert list(first.json()["rules"][0]) == [
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


def test_diff_at_writes_nothing_and_the_window_expands_with_history(
    client: TestClient,
) -> None:
    from app.db import db_session

    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": None}])
    (_, second_at) = evaluation_times(client)
    timestamp = iso(second_at + timedelta(seconds=1))

    history_before = client.get(history_path()).json()
    rules_before = client.get(rules_path()).json()

    windowed = client.get(diff_at_path(), params={"timestamp": timestamp})
    assert windowed.status_code == 200
    assert windowed.json()["to_sequence"] == 2
    # Rejections write nothing either.
    client.get(diff_at_path())
    client.request(
        "GET", diff_at_path(), params={"timestamp": timestamp}, content=b"{}"
    )
    client.get(diff_at_path(), params={"timestamp": "naive"})

    assert client.get(history_path()).json() == history_before
    assert client.get(rules_path()).json() == rules_before

    # A newly persisted evaluation strictly after the old timestamp extends the
    # window once the timestamp moves past it, without changing the as-of
    # result at the old instant. The record is appended directly (the history
    # table is append-only) so its creation instant is unambiguous.
    third_at = second_at + timedelta(minutes=1)
    history = client.get(history_path()).json()
    last = history[-1]
    with db_session() as conn:
        version_id = conn.execute(
            "SELECT id FROM schema_versions "
            "WHERE dataset_id = (SELECT id FROM datasets WHERE name = 'orders') "
            "AND version = 1"
        ).fetchone()["id"]
        clean_results = [
            {**result, "passed": True, "violations": []}
            for result in last["results"]
        ]
        conn.execute(
            "INSERT INTO quality_rule_evaluations ("
            "version_id, sequence, row_count, violation_row_count, "
            "results, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                version_id,
                3,
                1,
                0,
                json.dumps(clean_results),
                third_at.isoformat(),
            ),
        )

    assert client.get(diff_at_path(), params={"timestamp": timestamp}).json() == (
        windowed.json()
    )
    extended = client.get(
        diff_at_path(),
        params={"timestamp": iso(third_at + timedelta(seconds=1))},
    ).json()
    assert extended["from_sequence"] == 2
    assert extended["to_sequence"] == 3
    assert extended["removed_violation_rows"] == [0]


def test_bare_diff_and_history_keep_their_behaviour(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    evaluate(client, [{"id": 1}])

    bare = client.get(bare_diff_path())
    assert bare.status_code == 200
    assert bare.json()["from_sequence"] == 1
    # The bare endpoint still rejects query parameters and a body.
    assert (
        client.get(bare_diff_path(), params={"timestamp": "2026-01-01T00:00:00Z"}).status_code
        == 422
    )
    assert client.request("GET", bare_diff_path(), content=b"x").status_code == 422
    assert len(client.get(history_path()).json()) == 2
