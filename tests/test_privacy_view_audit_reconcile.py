"""Tests for the read-only privacy view per-day reconcile endpoint."""

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


def access_path(dataset: str = "orders", version: int = 1) -> str:
    return policies_path(dataset, version) + "/view/access-records"


def reconcile_path(dataset: str = "orders", version: int = 1) -> str:
    return audit_path(dataset, version) + "/reconcile"


def get_reconcile(client: TestClient, *path_args: object) -> dict:
    target = (
        reconcile_path(*path_args) if path_args else reconcile_path()  # type: ignore[arg-type]
    )
    response = client.get(target)
    assert response.status_code == 200, response.text
    return response.json()


def _version_id() -> int:
    with db_session() as conn:
        return conn.execute(
            "SELECT id FROM schema_versions ORDER BY version LIMIT 1"
        ).fetchone()["id"]


def insert_hits(rows: list[tuple]) -> None:
    """Insert (sequence, field, policy_id, role, masking, created_at) rows."""
    version_id = _version_id()
    with db_session() as conn:
        for sequence, field, policy_id, role, masking, created_at in rows:
            conn.execute(
                "INSERT INTO privacy_view_audit_records ("
                "version_id, sequence, field, policy_id, role, masking, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (version_id, sequence, field, policy_id, role, masking, created_at),
            )


def insert_access(rows: list[tuple]) -> None:
    """Insert (sequence, role, row_count, masked_count, created_at) rows."""
    version_id = _version_id()
    with db_session() as conn:
        for sequence, role, row_count, masked_count, created_at in rows:
            conn.execute(
                "INSERT INTO privacy_view_access_records ("
                "version_id, sequence, role, row_count, masked_count, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (version_id, sequence, role, row_count, masked_count, created_at),
            )


DAY_KEYS = ["day", "view_count", "masked_count", "hit_count", "consistent"]
TOTALS_KEYS = ["view_count", "masked_count", "hit_count"]


# --------------------------------------------------------------------------- #
# Empty states and response shape
# --------------------------------------------------------------------------- #


def test_reconcile_without_records_is_empty_not_an_error(client: TestClient) -> None:
    make_dataset_with_version(client)
    body = get_reconcile(client)
    assert list(body) == ["dataset", "version", "days", "totals"]
    assert body == {
        "dataset": "orders",
        "version": 1,
        "days": [],
        "totals": {"view_count": 0, "masked_count": 0, "hit_count": 0},
    }
    assert list(body["totals"]) == TOTALS_KEYS


def test_reconcile_is_get_only(client: TestClient) -> None:
    make_dataset_with_version(client)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, reconcile_path())
        assert response.status_code == 405, method


# --------------------------------------------------------------------------- #
# Per-day reconciliation
# --------------------------------------------------------------------------- #


def test_reconcile_matches_masked_sum_against_hit_count(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_access(
        [
            (1, "guest", 3, 2, "2026-03-01T08:00:00+00:00"),
            (2, "admin", 1, 1, "2026-03-01T20:00:00+00:00"),
        ]
    )
    insert_hits(
        [
            (1, "email", 1, "guest", "redact", "2026-03-01T08:00:00+00:00"),
            (2, "ssn", 2, "guest", "redact", "2026-03-01T08:00:00+00:00"),
            (3, "email", 1, "admin", "partial", "2026-03-01T20:00:00+00:00"),
        ]
    )
    body = get_reconcile(client)
    assert body["days"] == [
        {
            "day": "2026-03-01",
            "view_count": 2,
            "masked_count": 3,
            "hit_count": 3,
            "consistent": True,
        }
    ]
    assert list(body["days"][0]) == DAY_KEYS
    assert body["totals"] == {"view_count": 2, "masked_count": 3, "hit_count": 3}


def test_reconcile_flags_a_day_whose_counts_disagree(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_access([(1, "guest", 2, 2, "2026-03-01T08:00:00+00:00")])
    insert_hits(
        [
            (1, "email", 1, "guest", "redact", "2026-03-01T08:00:00+00:00"),
            (2, "email", 1, "guest", "redact", "2026-03-01T08:00:01+00:00"),
            (3, "ssn", 2, "guest", "redact", "2026-03-01T08:00:02+00:00"),
        ]
    )
    day = get_reconcile(client)["days"][0]
    assert day["masked_count"] == 2
    assert day["hit_count"] == 3
    assert day["consistent"] is False


def test_reconcile_counts_zero_for_a_side_without_records(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    # Day 1 has only access records (a view that masked nothing); day 3 has
    # only hit records. The missing side counts as zero on each day.
    insert_access(
        [
            (1, "guest", 2, 0, "2026-03-01T08:00:00+00:00"),
            (2, "guest", 1, 0, "2026-03-01T09:00:00+00:00"),
        ]
    )
    insert_hits(
        [(1, "email", 1, "guest", "redact", "2026-03-03T08:00:00+00:00")]
    )
    body = get_reconcile(client)
    assert body["days"] == [
        {
            "day": "2026-03-01",
            "view_count": 2,
            "masked_count": 0,
            "hit_count": 0,
            "consistent": True,
        },
        {
            "day": "2026-03-03",
            "view_count": 0,
            "masked_count": 0,
            "hit_count": 1,
            "consistent": False,
        },
    ]
    assert body["totals"] == {"view_count": 2, "masked_count": 0, "hit_count": 1}


def test_reconcile_days_sort_ascending_and_skip_recordless_days(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    # Insert out of calendar order; 2026-03-02 has no records at all and must
    # not appear (no zero-filled gap days).
    insert_access([(1, "guest", 1, 1, "2026-03-03T08:00:00+00:00")])
    insert_hits(
        [
            (1, "email", 1, "guest", "redact", "2026-03-03T08:00:00+00:00"),
            (2, "email", 1, "guest", "redact", "2026-03-01T08:00:00+00:00"),
        ]
    )
    insert_access([(2, "guest", 1, 1, "2026-03-01T08:00:00+00:00")])
    days = get_reconcile(client)["days"]
    assert [day["day"] for day in days] == ["2026-03-01", "2026-03-03"]


def test_reconcile_uses_utc_calendar_day_boundaries(client: TestClient) -> None:
    make_dataset_with_version(client)
    # 23:30 UTC and 00:30 UTC are different UTC calendar days.
    insert_access(
        [
            (1, "guest", 1, 1, "2026-03-01T23:30:00+00:00"),
            (2, "guest", 1, 1, "2026-03-02T00:30:00+00:00"),
        ]
    )
    insert_hits(
        [
            (1, "email", 1, "guest", "redact", "2026-03-01T23:30:00+00:00"),
            (2, "email", 1, "guest", "redact", "2026-03-02T00:30:00+00:00"),
        ]
    )
    days = get_reconcile(client)["days"]
    assert [day["day"] for day in days] == ["2026-03-01", "2026-03-02"]
    assert all(day["consistent"] for day in days)


def test_reconcile_totals_equal_the_sum_of_the_days(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_access(
        [
            (1, "guest", 2, 2, "2026-03-01T08:00:00+00:00"),
            (2, "admin", 1, 0, "2026-03-02T08:00:00+00:00"),
            (3, "guest", 4, 3, "2026-03-03T08:00:00+00:00"),
        ]
    )
    insert_hits(
        [
            (1, "email", 1, "guest", "redact", "2026-03-01T08:00:00+00:00"),
            (2, "ssn", 2, "guest", "redact", "2026-03-01T08:00:01+00:00"),
            (3, "email", 1, "guest", "redact", "2026-03-03T08:00:00+00:00"),
        ]
    )
    body = get_reconcile(client)
    assert len(body["days"]) == 3
    for key in TOTALS_KEYS:
        assert body["totals"][key] == sum(day[key] for day in body["days"])


def test_reconcile_is_scoped_to_its_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_access([(1, "guest", 1, 1, "2026-03-01T08:00:00+00:00")])
    insert_hits([(1, "email", 1, "guest", "redact", "2026-03-01T08:00:00+00:00")])
    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "email", "type": "string", "nullable": True}]},
        ).status_code
        == 201
    )
    assert get_reconcile(client, "orders", 2) == {
        "dataset": "orders",
        "version": 2,
        "days": [],
        "totals": {"view_count": 0, "masked_count": 0, "hit_count": 0},
    }
    assert len(get_reconcile(client)["days"]) == 1


def test_reconcile_reflects_real_views_as_consistent(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert (
        client.post(
            policies_path(),
            json={
                "field": "email",
                "classification": "pii",
                "masking": "redact",
                "allowed_roles": [],
            },
        ).status_code
        == 201
    )
    view = policies_path() + "/view"
    assert (
        client.post(
            view,
            json={"role": "guest", "rows": [{"id": 1, "email": "a@x.io"}]},
        ).status_code
        == 200
    )
    assert (
        client.post(view, json={"role": "guest", "rows": []}).status_code == 200
    )
    body = get_reconcile(client)
    assert len(body["days"]) == 1
    day = body["days"][0]
    assert day["view_count"] == 2
    assert day["masked_count"] == 1
    assert day["hit_count"] == 1
    assert day["consistent"] is True


# --------------------------------------------------------------------------- #
# Errors: 404 / 422, no writes
# --------------------------------------------------------------------------- #


def test_reconcile_unknown_dataset_or_version_returns_404(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    unknown_dataset = client.get(reconcile_path("ghost"))
    unknown_version = client.get(reconcile_path("orders", 9))
    assert unknown_dataset.status_code == 404
    assert unknown_version.status_code == 404
    for response in (unknown_dataset, unknown_version):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "not_found"


def test_reconcile_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_access([(1, "guest", 1, 1, "2026-03-01T08:00:00+00:00")])
    insert_hits([(1, "email", 1, "guest", "redact", "2026-03-01T08:00:00+00:00")])
    before = get_reconcile(client)

    with_body = client.request("GET", reconcile_path(), content=b"{}")
    with_query = client.get(reconcile_path(), params={"limit": 1})
    assert with_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, with_query):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]

    # The rejections wrote nothing and the reconcile still reads identically.
    assert get_reconcile(client) == before
    assert len(client.get(audit_path()).json()) == 1
    assert len(client.get(access_path()).json()) == 1


def test_reconcile_shape_errors_keep_404_precedence(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert client.get(reconcile_path("ghost"), params={"x": "1"}).status_code == 404
    assert (
        client.request("GET", reconcile_path("orders", 9), content=b"{}").status_code
        == 404
    )


def test_reconcile_does_not_modify_records(client: TestClient) -> None:
    make_dataset_with_version(client)
    insert_access([(1, "guest", 1, 1, "2026-03-01T08:00:00+00:00")])
    insert_hits([(1, "email", 1, "guest", "redact", "2026-03-01T08:00:00+00:00")])
    hits_before = client.get(audit_path()).json()
    access_before = client.get(access_path()).json()
    for _ in range(3):
        assert get_reconcile(client) == get_reconcile(client)
    assert client.get(audit_path()).json() == hits_before
    assert client.get(access_path()).json() == access_before
