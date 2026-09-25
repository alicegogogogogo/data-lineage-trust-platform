"""Tests for the read-only privacy view masking-hit audit day diff."""

from __future__ import annotations

from pathlib import Path

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


def get_diff(client: TestClient, *path_args: object) -> dict:
    target = diff_path(*path_args) if path_args else diff_path()  # type: ignore[arg-type]
    response = client.get(target)
    assert response.status_code == 200, response.text
    return response.json()


def insert_records(client: TestClient, rows: list[tuple]) -> None:
    """Insert (sequence, field, policy_id, role, masking, created_at) rows."""
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


GROUP_KEYS = {"field", "policy_id", "role", "masking"}
SIDE_KEYS = {"hit_count", "first_hit_at", "last_hit_at"}


# --------------------------------------------------------------------------- #
# Empty states and response shape
# --------------------------------------------------------------------------- #


def test_diff_without_records_is_empty_not_an_error(client: TestClient) -> None:
    make_dataset_with_version(client)
    body = get_diff(client)
    assert set(body) == {"from_period", "to_period", "groups"}
    assert body == {"from_period": None, "to_period": None, "groups": []}


def test_diff_with_a_single_day_is_empty_not_an_error(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_records(
        client,
        [
            (1, "email", 1, "guest", "redact", "2026-03-01T08:00:00+00:00"),
            (2, "email", 1, "guest", "redact", "2026-03-01T20:00:00+00:00"),
        ],
    )
    body = get_diff(client)
    assert body == {"from_period": None, "to_period": None, "groups": []}


def test_diff_is_get_only(client: TestClient) -> None:
    make_dataset_with_version(client)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, diff_path())
        assert response.status_code == 405, method


# --------------------------------------------------------------------------- #
# Periods, kinds and stats
# --------------------------------------------------------------------------- #


def test_diff_compares_the_two_most_recent_calendar_days(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_records(
        client,
        [
            # Old day is ignored: only the two most recent days are compared.
            (1, "email", 1, "guest", "redact", "2026-02-27T00:00:00+00:00"),
            (2, "email", 1, "guest", "redact", "2026-03-01T08:00:00+00:00"),
            (3, "email", 1, "guest", "redact", "2026-03-01T23:00:00+00:00"),
            (4, "email", 1, "guest", "redact", "2026-03-02T06:00:00+00:00"),
            (5, "ssn", 2, "guest", "partial", "2026-03-02T18:00:00+00:00"),
        ],
    )
    body = get_diff(client)
    assert body["from_period"] == "2026-03-01"
    assert body["to_period"] == "2026-03-02"

    groups = body["groups"]
    keyed = {tuple(g[k] for k in ("field", "policy_id", "role", "masking")): g
             for g in groups}

    email = keyed[("email", 1, "guest", "redact")]
    assert email["kind"] == "changed"
    assert email["before"] == {
        "hit_count": 2,
        "first_hit_at": "2026-03-01T08:00:00+00:00",
        "last_hit_at": "2026-03-01T23:00:00+00:00",
    }
    assert email["after"] == {
        "hit_count": 1,
        "first_hit_at": "2026-03-02T06:00:00+00:00",
        "last_hit_at": "2026-03-02T06:00:00+00:00",
    }
    assert email["hit_count_delta"] == -1

    ssn = keyed[("ssn", 2, "guest", "partial")]
    assert ssn["kind"] == "added"
    assert ssn["before"] is None
    assert ssn["after"] == {
        "hit_count": 1,
        "first_hit_at": "2026-03-02T18:00:00+00:00",
        "last_hit_at": "2026-03-02T18:00:00+00:00",
    }
    assert ssn["hit_count_delta"] == 1


def test_diff_removed_group_keeps_null_after_with_key_present(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    insert_records(
        client,
        [
            (1, "email", 1, "guest", "redact", "2026-03-01T08:00:00+00:00"),
            (2, "email", 1, "guest", "redact", "2026-03-01T09:00:00+00:00"),
            (3, "ssn", 2, "guest", "partial", "2026-03-02T09:00:00+00:00"),
        ],
    )
    groups = get_diff(client)["groups"]
    by_field = {g["field"]: g for g in groups}

    removed = by_field["email"]
    assert removed["kind"] == "removed"
    assert set(removed) == GROUP_KEYS | {
        "kind", "before", "after", "hit_count_delta"
    }
    assert removed["after"] is None
    assert removed["before"]["hit_count"] == 2
    assert removed["hit_count_delta"] == 0 - 2

    added = by_field["ssn"]
    assert added["kind"] == "added"
    assert "before" in added and added["before"] is None
    assert added["after"]["hit_count"] == 1
    assert added["hit_count_delta"] == 1


def test_diff_groups_sort_by_field_policy_role_masking(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_records(
        client,
        [
            (1, "ssn", 3, "guest", "redact", "2026-03-01T00:00:00+00:00"),
            (2, "email", 2, "guest", "redact", "2026-03-01T00:00:00+00:00"),
            (3, "email", 2, "guest", "partial", "2026-03-02T00:00:00+00:00"),
            (4, "email", 2, "analyst", "redact", "2026-03-02T00:00:00+00:00"),
            (5, "email", 1, "admin", "partial", "2026-03-02T00:00:00+00:00"),
        ],
    )
    groups = get_diff(client)["groups"]
    assert [
        (g["field"], g["policy_id"], g["role"], g["masking"]) for g in groups
    ] == [
        ("email", 1, "admin", "partial"),
        ("email", 2, "analyst", "redact"),
        ("email", 2, "guest", "partial"),
        ("email", 2, "guest", "redact"),
        ("ssn", 3, "guest", "redact"),
    ]


def test_diff_sides_carry_only_the_three_stat_keys(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_records(
        client,
        [
            (1, "email", 1, "guest", "redact", "2026-03-01T08:00:00+00:00"),
            (2, "email", 1, "guest", "redact", "2026-03-02T08:00:00+00:00"),
        ],
    )
    group = get_diff(client)["groups"][0]
    assert group["kind"] == "changed"
    assert set(group["before"]) == SIDE_KEYS
    assert set(group["after"]) == SIDE_KEYS


def test_diff_delta_is_target_minus_baseline(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_records(
        client,
        [
            (1, "email", 1, "guest", "redact", "2026-03-01T00:00:00+00:00"),
            (2, "email", 1, "guest", "redact", "2026-03-01T01:00:00+00:00"),
            (3, "email", 1, "guest", "redact", "2026-03-01T02:00:00+00:00"),
            (4, "email", 1, "guest", "redact", "2026-03-02T02:00:00+00:00"),
            (5, "email", 1, "guest", "redact", "2026-03-02T03:00:00+00:00"),
        ],
    )
    group = get_diff(client)["groups"][0]
    assert group["before"]["hit_count"] == 3
    assert group["after"]["hit_count"] == 2
    assert group["hit_count_delta"] == -1


def test_diff_uses_utc_calendar_day_boundaries(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_records(
        client,
        [
            # 23:30 UTC and 00:30 UTC are different UTC calendar days even
            # though they are one hour apart.
            (1, "email", 1, "guest", "redact", "2026-03-01T23:30:00+00:00"),
            (2, "email", 1, "guest", "redact", "2026-03-02T00:30:00+00:00"),
        ],
    )
    body = get_diff(client)
    assert body["from_period"] == "2026-03-01"
    assert body["to_period"] == "2026-03-02"
    group = body["groups"][0]
    assert group["kind"] == "changed"


def test_diff_is_scoped_to_its_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_records(
        client,
        [
            (1, "email", 1, "guest", "redact", "2026-03-01T00:00:00+00:00"),
            (2, "email", 1, "guest", "redact", "2026-03-02T00:00:00+00:00"),
        ],
    )
    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "email", "type": "string", "nullable": True}]},
        ).status_code
        == 201
    )
    assert get_diff(client, "orders", 2) == {
        "from_period": None, "to_period": None, "groups": []
    }
    assert len(get_diff(client)["groups"]) == 1


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
        client,
        [
            (1, "email", 1, "guest", "redact", "2026-03-01T00:00:00+00:00"),
            (2, "email", 1, "guest", "redact", "2026-03-02T00:00:00+00:00"),
        ],
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
    insert_records(
        client,
        [
            (1, "email", 1, "guest", "redact", "2026-03-01T00:00:00+00:00"),
            (2, "email", 1, "guest", "redact", "2026-03-02T00:00:00+00:00"),
        ],
    )
    records_before = client.get(audit_path()).json()
    for _ in range(3):
        assert get_diff(client) == get_diff(client)
    assert client.get(audit_path()).json() == records_before
