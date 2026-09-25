"""Tests for the read-only privacy view masking-hit audit day-over-day diff."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import db_session

BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "email", "type": "string", "nullable": True},
    {"name": "ssn", "type": "string", "nullable": True},
]


def make_dataset_with_version(
    client: TestClient, name: str = "orders", fields: list[dict] | None = None
) -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions",
        json={"fields": fields if fields is not None else BASE_FIELDS},
    )
    assert response.status_code == 201, response.text


def policies_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/privacy-policies"


def audit_path(dataset: str = "orders", version: int = 1) -> str:
    return policies_path(dataset, version) + "/view/audit-records"


def diff_path(dataset: str = "orders", version: int = 1) -> str:
    return audit_path(dataset, version) + "/diff"


def create_policy(client: TestClient, payload: dict, **path: object) -> dict:
    response = client.post(policies_path(**path), json=payload)  # type: ignore[arg-type]
    assert response.status_code == 201, response.text
    return response.json()


def post_view(client: TestClient, role: str, rows: list[dict], **path: object):
    return client.post(
        policies_path(**path) + "/view",  # type: ignore[arg-type]
        json={"role": role, "rows": rows},
    )


def get_diff(client: TestClient, *path_args: object) -> dict:
    target = diff_path(*path_args) if path_args else diff_path()  # type: ignore[arg-type]
    response = client.get(target)
    assert response.status_code == 200, response.text
    return response.json()


def insert_records(rows: list[tuple[int, str, int, str, str, str]]) -> None:
    """Insert (sequence, field, policy_id, role, masking, created_at) records."""
    with db_session() as conn:
        version_id = conn.execute(
            "SELECT id FROM schema_versions ORDER BY version LIMIT 1"
        ).fetchone()["id"]
        for sequence, field, policy_id, role, masking, created_at in rows:
            conn.execute(
                "INSERT INTO privacy_view_audit_records ("
                "version_id, sequence, field, policy_id, role, masking, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (version_id, sequence, field, policy_id, role, masking, created_at),
            )


GROUP_FIELDS = {
    "field",
    "policy_id",
    "role",
    "masking",
    "kind",
    "before",
    "after",
    "hit_count_delta",
}

SIDE_FIELDS = {"hit_count", "first_hit_at", "last_hit_at"}


# --------------------------------------------------------------------------- #
# Empty state and response shape
# --------------------------------------------------------------------------- #


def test_diff_without_records_is_empty_not_an_error(client: TestClient) -> None:
    make_dataset_with_version(client)
    body = get_diff(client)
    assert set(body) == {"from_period", "to_period", "groups"}
    assert body["from_period"] is None
    assert body["to_period"] is None
    assert body["groups"] == []


def test_diff_with_a_single_day_is_empty(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_records(
        [
            (1, "email", 1, "guest", "redact", "2026-01-02T08:00:00+00:00"),
            (2, "email", 1, "guest", "redact", "2026-01-02T23:59:59+00:00"),
        ]
    )
    body = get_diff(client)
    assert body["from_period"] is None
    assert body["to_period"] is None
    assert body["groups"] == []


def test_diff_is_get_only(client: TestClient) -> None:
    make_dataset_with_version(client)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, diff_path())
        assert response.status_code == 405, method


# --------------------------------------------------------------------------- #
# Grouping, kinds and deltas
# --------------------------------------------------------------------------- #


def test_diff_reports_added_removed_and_changed_groups(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_records(
        [
            # Baseline day 2026-01-02.
            (1, "email", 1, "guest", "redact", "2026-01-02T08:00:00+00:00"),
            (2, "email", 1, "guest", "redact", "2026-01-02T09:00:00+00:00"),
            (3, "ssn", 2, "guest", "partial", "2026-01-02T10:00:00+00:00"),
            # Target day 2026-01-03.
            (4, "email", 1, "guest", "redact", "2026-01-03T08:00:00+00:00"),
            (5, "email", 1, "auditor", "redact", "2026-01-03T11:00:00+00:00"),
        ]
    )
    body = get_diff(client)
    assert body["from_period"] == "2026-01-02"
    assert body["to_period"] == "2026-01-03"

    groups = body["groups"]
    assert len(groups) == 3
    for group in groups:
        assert set(group) == GROUP_FIELDS

    keyed = {(g["field"], g["policy_id"], g["role"], g["masking"]): g for g in groups}

    changed = keyed[("email", 1, "guest", "redact")]
    assert changed["kind"] == "changed"
    assert changed["before"] == {
        "hit_count": 2,
        "first_hit_at": "2026-01-02T08:00:00+00:00",
        "last_hit_at": "2026-01-02T09:00:00+00:00",
    }
    assert changed["after"] == {
        "hit_count": 1,
        "first_hit_at": "2026-01-03T08:00:00+00:00",
        "last_hit_at": "2026-01-03T08:00:00+00:00",
    }
    assert changed["hit_count_delta"] == -1

    removed = keyed[("ssn", 2, "guest", "partial")]
    assert removed["kind"] == "removed"
    assert removed["before"] is not None
    assert removed["before"]["hit_count"] == 1
    assert removed["after"] is None
    assert removed["hit_count_delta"] == -1

    added = keyed[("email", 1, "auditor", "redact")]
    assert added["kind"] == "added"
    assert added["before"] is None
    assert added["after"] is not None
    assert added["after"]["hit_count"] == 1
    assert added["hit_count_delta"] == 1

    # Groups sort by field, policy id, role and masking, all ascending.
    assert [
        (g["field"], g["policy_id"], g["role"], g["masking"]) for g in groups
    ] == sorted(
        (g["field"], g["policy_id"], g["role"], g["masking"]) for g in groups
    )


def test_diff_compares_only_the_two_most_recent_days(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_records(
        [
            (1, "ssn", 2, "guest", "partial", "2026-01-01T08:00:00+00:00"),
            (2, "email", 1, "guest", "redact", "2026-01-03T08:00:00+00:00"),
            (3, "email", 1, "guest", "redact", "2026-01-05T08:00:00+00:00"),
            (4, "email", 1, "guest", "redact", "2026-01-05T09:00:00+00:00"),
        ]
    )
    body = get_diff(client)
    assert body["from_period"] == "2026-01-03"
    assert body["to_period"] == "2026-01-05"
    assert len(body["groups"]) == 1
    group = body["groups"][0]
    assert group["kind"] == "changed"
    assert group["before"]["hit_count"] == 1
    assert group["after"]["hit_count"] == 2
    assert group["hit_count_delta"] == 1


def test_diff_buckets_records_by_utc_calendar_day(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_records(
        [
            # 2026-01-02 01:30 at +05:00 is still 2026-01-01 in UTC.
            (1, "email", 1, "guest", "redact", "2026-01-02T01:30:00+05:00"),
            (2, "email", 1, "auditor", "redact", "2026-01-02T12:00:00+00:00"),
        ]
    )
    body = get_diff(client)
    assert body["from_period"] == "2026-01-01"
    assert body["to_period"] == "2026-01-02"
    assert len(body["groups"]) == 2
    kinds = {(g["kind"]) for g in body["groups"]}
    assert kinds == {"added", "removed"}


def test_diff_is_scoped_to_its_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_records(
        [
            (1, "email", 1, "guest", "redact", "2026-01-02T08:00:00+00:00"),
            (2, "email", 1, "guest", "redact", "2026-01-03T08:00:00+00:00"),
        ]
    )
    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "email", "type": "string", "nullable": True}]},
        ).status_code
        == 201
    )
    assert get_diff(client, "orders", 2)["groups"] == []
    assert get_diff(client, "orders", 2)["from_period"] is None
    assert len(get_diff(client)["groups"]) == 1


def test_same_batch_records_share_one_write_time(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    create_policy(
        client,
        {"field": "ssn", "classification": "secret", "masking": "partial",
         "allowed_roles": []},
    )
    assert post_view(
        client, "guest", [{"email": "a@b.c", "ssn": "123"}]
    ).status_code == 200

    records = client.get(audit_path()).json()
    assert len(records) == 2
    assert records[0]["created_at"] == records[1]["created_at"]


# --------------------------------------------------------------------------- #
# Errors: 404 / 422, no writes
# --------------------------------------------------------------------------- #


def test_diff_unknown_dataset_or_version_returns_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    unknown_dataset = client.get(diff_path("ghost"))
    unknown_version = client.get(diff_path("orders", 9))
    assert unknown_dataset.status_code == 404
    assert unknown_version.status_code == 404
    for response in (unknown_dataset, unknown_version):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "not_found"


def test_diff_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_records(
        [
            (1, "email", 1, "guest", "redact", "2026-01-02T08:00:00+00:00"),
            (2, "email", 1, "guest", "redact", "2026-01-03T08:00:00+00:00"),
        ]
    )
    before = get_diff(client)

    with_body = client.request("GET", diff_path(), content=b"{}")
    with_query = client.get(diff_path(), params={"limit": 1})
    assert with_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, with_query):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]

    # The rejections wrote nothing and the diff still reads identically.
    assert get_diff(client) == before
    assert len(client.get(audit_path()).json()) == 2


def test_diff_shape_errors_keep_404_precedence(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert client.get(diff_path("ghost"), params={"x": "1"}).status_code == 404
    assert (
        client.request("GET", diff_path("orders", 9), content=b"{}").status_code
        == 404
    )


def test_diff_does_not_modify_records(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    records_before = client.get(audit_path()).json()

    for _ in range(3):
        get_diff(client)
    assert client.get(audit_path()).json() == records_before
