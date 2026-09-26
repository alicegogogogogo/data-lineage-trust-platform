"""Tests for the point-in-time quality gate (``.../quality-rules/gate/at``)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import repository


BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": True},
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


def gate_at_path(dataset: str = "orders", version: int = 1) -> str:
    return f"{gate_path(dataset, version)}/at"


def config_path() -> str:
    return rules_path() + "/anomaly-detection"


ACCESS_PATH = "/datasets/orders/versions/1/privacy-policies/view/access-records"
AUDIT_PATH = "/datasets/orders/versions/1/privacy-policies/view/audit-records"


def create_rule(client: TestClient, payload: dict) -> dict:
    response = client.post(rules_path(), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def evaluate(client: TestClient, rows: list[dict]) -> dict:
    response = client.post(rules_path() + "/evaluate", json={"rows": rows})
    assert response.status_code == 200, response.text
    return response.json()


def evaluations(client: TestClient) -> list[dict]:
    response = client.get(rules_path() + "/evaluations")
    assert response.status_code == 200
    return response.json()


def anomalies(client: TestClient) -> list[dict]:
    response = client.get(config_path() + "/anomalies")
    assert response.status_code == 200
    return response.json()


def register_config(
    client: TestClient,
    steps: int = 2,
    row_limit: int = 1000,
    rule_limit: int = 1000,
) -> None:
    response = client.post(
        config_path(),
        json={
            "consecutive_worsening_steps": steps,
            "violation_row_limit": row_limit,
            "rule_violation_limit": rule_limit,
        },
    )
    assert response.status_code == 201, response.text


def scan(client: TestClient) -> list[dict]:
    response = client.post(config_path() + "/scan")
    assert response.status_code == 200, response.text
    return response.json()


def iso_after(created_at: str, **delta: object) -> str:
    return (datetime.fromisoformat(created_at) + timedelta(**delta)).isoformat()


def iso_before(created_at: str, **delta: object) -> str:
    return (datetime.fromisoformat(created_at) - timedelta(**delta)).isoformat()


def get_gate_at(
    client: TestClient, timestamp: str, *, dataset: str = "orders", version: int = 1
) -> object:
    return client.get(
        gate_at_path(dataset, version), params={"timestamp": timestamp}
    )


# --------------------------------------------------------------------------- #
# Point-in-time verdicts
# --------------------------------------------------------------------------- #


def test_gate_at_before_the_first_evaluation_is_undetermined(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    first = evaluations(client)[0]["created_at"]

    response = get_gate_at(client, iso_before(first, seconds=1))

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

    # A successful read leaves its access record even when the window holds no
    # evaluation, and writes no hits.
    access = client.get(ACCESS_PATH).json()
    assert len(access) == 1
    assert access[0]["role"] == "quality_gate_at"
    assert client.get(AUDIT_PATH).json() == []


def test_gate_at_boundary_timestamp_includes_the_record_written_at_it(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    first = evaluations(client)[0]["created_at"]

    # created_at <= timestamp: the exact write instant is inside the window.
    at = get_gate_at(client, first).json()
    assert at["verdict"] == "fail"
    assert at["counts"] == {"evaluations": 1, "anomalies": 0, "reasons": 1}
    assert at["reasons"] == [
        {"kind": "violation", "sequence": 1, "rule_id": 1, "violation_count": 1}
    ]


def test_gate_at_only_considers_evaluations_in_the_window(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])  # sequence 1: a violation
    evaluate(client, [{"id": 1}])  # sequence 2: clean
    first, second = [record["created_at"] for record in evaluations(client)]

    middle = (
        datetime.fromisoformat(first)
        + (datetime.fromisoformat(second) - datetime.fromisoformat(first)) / 2
    ).isoformat()

    before_second = get_gate_at(client, middle).json()
    # Sequence 2 is still in the future: the latest in-window evaluation is the
    # violating sequence 1.
    assert before_second["verdict"] == "fail"
    assert before_second["counts"]["evaluations"] == 1
    assert before_second["reasons"] == [
        {"kind": "violation", "sequence": 1, "rule_id": 1, "violation_count": 1}
    ]

    after_second = get_gate_at(client, iso_after(second, seconds=1)).json()
    assert after_second["verdict"] == "pass"
    assert after_second["reasons"] == []
    assert after_second["counts"] == {"evaluations": 2, "anomalies": 0, "reasons": 0}


def test_gate_at_anomaly_record_enters_only_after_its_write_time(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, row_limit=0)
    evaluate(client, [{"id": None}])  # sequence 1: over the limit, violating
    scan(client)
    evaluate(client, [{"id": 1}])  # sequence 2: clean
    first, second = [record["created_at"] for record in evaluations(client)]
    anomaly_at = anomalies(client)[0]["created_at"]
    assert datetime.fromisoformat(first) < datetime.fromisoformat(anomaly_at)
    assert datetime.fromisoformat(anomaly_at) < datetime.fromisoformat(second)

    # Before the anomaly was written: only the violating sequence 1 is visible.
    before_anomaly = (
        datetime.fromisoformat(first)
        + (
            datetime.fromisoformat(anomaly_at) - datetime.fromisoformat(first)
        ) / 2
    ).isoformat()
    early = get_gate_at(client, before_anomaly).json()
    assert early["verdict"] == "fail"
    assert early["counts"] == {"evaluations": 1, "anomalies": 0, "reasons": 1}
    assert early["reasons"][0]["kind"] == "violation"

    # After the anomaly but before the clean evaluation: the anomaly reason is
    # visible and the latest in-window evaluation still violates, so both
    # reasons are returned (never merged).
    between = (
        datetime.fromisoformat(anomaly_at)
        + (
            datetime.fromisoformat(second) - datetime.fromisoformat(anomaly_at)
        ) / 2
    ).isoformat()
    middle = get_gate_at(client, between).json()
    assert middle["counts"] == {"evaluations": 1, "anomalies": 1, "reasons": 2}
    assert [reason["kind"] for reason in middle["reasons"]] == [
        "row_limit",
        "violation",
    ]

    # After the clean sequence 2: it passes, but the anomaly keeps the verdict
    # a fail on its own.
    late = get_gate_at(client, iso_after(second, seconds=1)).json()
    assert late["verdict"] == "fail"
    assert late["counts"] == {"evaluations": 2, "anomalies": 1, "reasons": 1}
    assert late["reasons"] == [
        {"kind": "row_limit", "sequence": 1, "rule_id": None, "violation_count": 1}
    ]


def test_gate_at_normalizes_offsets(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    first = datetime.fromisoformat(evaluations(client)[0]["created_at"])

    # A timestamp one second before the write, expressed in a far-western
    # offset, names the same UTC instant and keeps the evaluation out of the
    # window; the write instant expressed with a 'Z' designator brings it in.
    before_instant = first - timedelta(seconds=1)
    before_western = before_instant.astimezone(
        timezone(timedelta(hours=-8))
    ).isoformat()
    at_zulu = first.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    before = get_gate_at(client, before_western).json()
    at = get_gate_at(client, at_zulu).json()
    assert before["counts"]["evaluations"] == 0
    assert before["verdict"] == "undetermined"
    assert at["verdict"] == "fail"
    assert at["counts"]["evaluations"] == 1


def test_gate_at_in_the_future_matches_the_current_gate_byte_for_byte(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "a", "kind": "not_null", "params": {"field": "id"}}
    )
    create_rule(
        client, {"name": "b", "kind": "not_null", "params": {"field": "amount"}}
    )
    register_config(client, row_limit=0, rule_limit=0)
    evaluate(client, [{"id": None, "amount": None}])
    scan(client)

    future = get_gate_at(client, "2099-01-01T00:00:00+00:00")
    current = client.get(gate_path())
    assert future.status_code == 200
    assert future.text == current.text
    assert future.text.endswith("}\n")


# --------------------------------------------------------------------------- #
# 404 precedence and 422 request shape
# --------------------------------------------------------------------------- #


def test_gate_at_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    for path in (gate_at_path("ghost"), gate_at_path("orders", 9)):
        response = client.get(path, params={"timestamp": "2030-01-01T00:00:00+00:00"})
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"
        assert "detail" in response.json()


def test_gate_at_resolves_the_path_before_validating_the_timestamp(
    client: TestClient,
) -> None:
    for params in (
        {"timestamp": "not-a-timestamp"},
        {"timestamp": "2026-01-01T00:00:00"},  # naive
        {},  # missing
        {"timestamp": "2030-01-01T00:00:00+00:00", "extra": "1"},
    ):
        response = client.get(gate_at_path("ghost"), params=params)
        assert response.status_code == 404, params
    assert (
        client.request(
            "GET",
            gate_at_path("ghost"),
            params={"timestamp": "2030-01-01T00:00:00+00:00"},
            content=b"{}",
        ).status_code
        == 404
    )


def test_gate_at_rejects_missing_blank_unparseable_or_naive_timestamps(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    valid = "2030-01-01T00:00:00+00:00"
    requests = [
        client.get(gate_at_path()),  # missing entirely
        client.get(gate_at_path(), params={"timestamp": ""}),  # blank
        client.get(gate_at_path(), params={"timestamp": "   "}),  # whitespace
        client.get(gate_at_path(), params={"timestamp": "not-a-timestamp"}),
        client.get(gate_at_path(), params={"timestamp": "2026-13-99T00:00:00+00:00"}),
        client.get(gate_at_path(), params={"timestamp": "2026-01-01"}),  # date only
        client.get(gate_at_path(), params={"timestamp": "2026-01-01T00:00:00"}),
        client.get(gate_at_path(), params={"timestamp": valid + "junk"}),
        client.get(gate_at_path(), params={"other": valid}),  # wrong name
    ]
    for response in requests:
        assert response.status_code == 422, response.text
        body = response.json()
        assert body["error"] == "validation_error"
        assert set(body) == {"error", "detail"}
        assert "SQLite" not in response.text
        assert "Traceback" not in response.text
    # Valid shape sanity check.
    assert client.get(gate_at_path(), params={"timestamp": valid}).status_code == 200


def test_gate_at_rejects_duplicate_timestamp_and_unknown_parameters(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    response = client.get(
        gate_at_path(),
        params=[
            ("timestamp", "2030-01-01T00:00:00+00:00"),
            ("timestamp", "2030-02-01T00:00:00+00:00"),
        ],
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"

    response = client.get(
        gate_at_path(),
        params={"timestamp": "2030-01-01T00:00:00+00:00", "expand": "1"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert "expand" in body["detail"]


def test_gate_at_rejects_any_request_body_including_whitespace(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    valid = {"timestamp": "2030-01-01T00:00:00+00:00"}
    for content in (b"{}", b" ", b" \t\n", b"not json", b"\x00"):
        response = client.request(
            "GET", gate_at_path(), params=valid, content=content
        )
        assert response.status_code == 422, content
        assert response.json()["error"] == "validation_error"

    # No trail is written by a rejected read.
    assert client.get(ACCESS_PATH).json() == []


def test_gate_at_only_accepts_get(client: TestClient) -> None:
    make_dataset_with_version(client)
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        response = client.request(method, gate_at_path())
        assert response.status_code == 405, response.text


# --------------------------------------------------------------------------- #
# Read-only behaviour of the verdict and its masking-read trail
# --------------------------------------------------------------------------- #


def test_gate_at_does_not_modify_evaluations_anomalies_or_rules(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, row_limit=0)
    evaluate(client, [{"id": None}])
    scan(client)

    history_before = client.get(rules_path() + "/evaluations").json()
    anomalies_before = client.get(config_path() + "/anomalies").json()
    rules_before = client.get(rules_path()).json()

    get_gate_at(client, "2099-01-01T00:00:00+00:00")
    get_gate_at(client, "2000-01-01T00:00:00+00:00")

    assert client.get(rules_path() + "/evaluations").json() == history_before
    assert client.get(config_path() + "/anomalies").json() == anomalies_before
    assert client.get(rules_path()).json() == rules_before


def test_gate_at_answer_is_stable_across_repeated_reads_and_a_restart(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    register_config(client, row_limit=0)
    evaluate(client, [{"id": None}])
    scan(client)
    target = iso_after(anomalies(client)[0]["created_at"], seconds=1)

    first = get_gate_at(client, target)
    assert first.status_code == 200
    assert first.text.endswith("}\n")
    # The response document itself is byte-identical regardless of the trail
    # appends each read performs.
    assert get_gate_at(client, target).text == first.text

    from app.main import app

    restarted = TestClient(app)
    assert (
        restarted.get(
            gate_at_path(), params={"timestamp": target}
        ).text
        == first.text
    )


def test_successful_gate_at_reads_leave_one_access_record_without_hits(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": 1}])
    target = iso_after(evaluations(client)[0]["created_at"], seconds=1)

    assert get_gate_at(client, target).status_code == 200
    assert get_gate_at(client, target).status_code == 200

    hits = client.get(AUDIT_PATH).json()
    assert hits == []  # the gate masks no values

    access = client.get(ACCESS_PATH).json()
    assert [record["sequence"] for record in access] == [1, 2]
    for record in access:
        assert set(record) == {
            "sequence", "role", "row_count", "masked_count", "created_at"
        }
        assert record["role"] == "quality_gate_at"
        assert record["row_count"] == 0
        assert record["masked_count"] == 0
        datetime.fromisoformat(record["created_at"])


def test_gate_at_trail_enters_the_per_day_reconciliation(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    evaluate(client, [])

    assert get_gate_at(client, "2099-01-01T00:00:00+00:00").status_code == 200

    reconcile = client.get(
        "/datasets/orders/versions/1/privacy-policies/view/audit-records/reconcile"
    )
    assert reconcile.status_code == 200
    day = reconcile.json()["days"][0]
    assert day["view_count"] == 1
    assert day["masked_count"] == 0
    assert day["hit_count"] == 0
    assert day["consistent"] is True
    totals = reconcile.json()["totals"]
    assert totals["view_count"] == 1
    assert totals["masked_count"] == 0
    assert totals["hit_count"] == 0

    export = client.get("/datasets/orders/privacy-compliance-export")
    assert export.status_code == 200
    version_state = export.json()["versions"][0]
    assert version_state["view_count"] == 1
    assert version_state["masked_count"] == 0
    assert version_state["hit_count"] == 0


def test_gate_at_trail_continues_the_sequence_run_of_regular_views(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    policy_path = "/datasets/orders/versions/1/privacy-policies"
    response = client.post(
        policy_path,
        json={
            "field": "id",
            "classification": "PII",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    assert response.status_code == 201
    # A regular view writes one hit plus one access record.
    assert client.post(
        policy_path + "/view", json={"role": "guest", "rows": [{"id": 7}]}
    ).status_code == 200
    assert get_gate_at(client, "2099-01-01T00:00:00+00:00").status_code == 200

    hits = client.get(AUDIT_PATH).json()
    assert [hit["sequence"] for hit in hits] == [1]
    access = client.get(ACCESS_PATH).json()
    assert [record["sequence"] for record in access] == [1, 2]
    assert [record["role"] for record in access] == ["guest", "quality_gate_at"]

    # The day still reconciles: one masked hit against one masked access value,
    # plus the gate read's zero-mask view.
    day = client.get(policy_path + "/view/audit-records/reconcile").json()["days"][0]
    assert day["view_count"] == 2
    assert day["masked_count"] == 1
    assert day["hit_count"] == 1
    assert day["consistent"] is True


def test_gate_at_still_answers_when_the_trail_write_fails(
    client: TestClient, monkeypatch
) -> None:
    make_dataset_with_version(client)
    create_rule(client, {"name": "r", "kind": "not_null", "params": {"field": "id"}})
    evaluate(client, [{"id": None}])
    target = iso_after(evaluations(client)[0]["created_at"], seconds=1)

    def failing_trail(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("simulated trail write failure")

    monkeypatch.setattr(repository, "_record_privacy_view_trail", failing_trail)

    response = get_gate_at(client, target)
    assert response.status_code == 200, response.text
    assert response.json()["verdict"] == "fail"
    assert response.json()["counts"] == {"evaluations": 1, "anomalies": 0, "reasons": 1}

    # Nothing about the failed trail surfaces and no access record lands.
    assert client.get(ACCESS_PATH).json() == []


def test_regular_view_writes_its_hits_and_access_record_at_one_time(
    client: TestClient,
) -> None:
    # The shared-write-time guarantee the gate-at trail relies on: for a read
    # that does mask values, the hit batch and the access record carry the very
    # same write timestamp.
    make_dataset_with_version(client)
    policy_path = "/datasets/orders/versions/1/privacy-policies"
    assert client.post(
        policy_path,
        json={
            "field": "id",
            "classification": "PII",
            "masking": "redact",
            "allowed_roles": [],
        },
    ).status_code == 201
    response = client.post(
        policy_path + "/view",
        json={"role": "guest", "rows": [{"id": 1}, {"id": 2}, {"id": 3}]},
    )
    assert response.status_code == 200

    hits = client.get(AUDIT_PATH).json()
    access = client.get(ACCESS_PATH).json()
    assert len(hits) == 3
    assert len(access) == 1
    assert {hit["created_at"] for hit in hits} == {access[0]["created_at"]}
