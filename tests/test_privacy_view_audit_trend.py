"""Tests for the read-only privacy view masking-hit trend by policy."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import db_session

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def trend_path(dataset: str = "orders", version: int = 1) -> str:
    return audit_path(dataset, version) + "/trend"


def create_policy(client: TestClient, payload: dict, **path: object) -> dict:
    response = client.post(policies_path(**path), json=payload)  # type: ignore[arg-type]
    assert response.status_code == 201, response.text
    return response.json()


def post_view(client: TestClient, role: str, rows: list[dict], **path: object):
    return client.post(
        policies_path(**path) + "/view",  # type: ignore[arg-type]
        json={"role": role, "rows": rows},
    )


def get_trend(client: TestClient, *path_args: object) -> dict:
    target = trend_path(*path_args) if path_args else trend_path()  # type: ignore[arg-type]
    response = client.get(target)
    assert response.status_code == 200, response.text
    return response.json()


def _version_id() -> int:
    with db_session() as conn:
        return conn.execute(
            "SELECT id FROM schema_versions ORDER BY version LIMIT 1"
        ).fetchone()["id"]


def insert_hit_records(client: TestClient, rows: list[tuple]) -> None:
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


POLICY_KEYS = {"policy_id", "field", "classification", "masking", "total_hits", "days"}
DAY_KEYS = {"day", "hit_count", "hit_count_delta", "trend"}


# --------------------------------------------------------------------------- #
# Empty state and response shape
# --------------------------------------------------------------------------- #


def test_trend_without_records_is_empty_not_an_error(client: TestClient) -> None:
    make_dataset_with_version(client)
    body = get_trend(client)
    assert set(body) == {"dataset", "version", "policies", "totals"}
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["policies"] == []
    assert body["totals"] == {
        "total_hits": 0,
        "policy_count": 0,
        "day_count": 0,
    }


def test_trend_is_get_only(client: TestClient) -> None:
    make_dataset_with_version(client)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, trend_path())
        assert response.status_code == 405, method


def test_policy_without_hits_never_appears(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    body = get_trend(client)
    assert body["policies"] == []
    assert body["totals"]["policy_count"] == 0


# --------------------------------------------------------------------------- #
# Aggregation by policy across roles, registration fields
# --------------------------------------------------------------------------- #


def test_trend_merges_roles_per_policy_and_uses_registration_fields(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    email_policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    ssn_policy = create_policy(
        client,
        {"field": "ssn", "classification": "SECRET", "masking": "partial",
         "allowed_roles": []},
    )

    assert post_view(client, "guest", [{"email": "a@b.c", "ssn": "123456"}]).status_code == 200
    assert post_view(client, "auditor", [{"email": "d@e.f"}]).status_code == 200
    assert post_view(client, "guest", [{"ssn": "654321"}]).status_code == 200

    body = get_trend(client)
    policies = body["policies"]
    assert len(policies) == 2
    for policy in policies:
        assert set(policy) == POLICY_KEYS

    by_id = {policy["policy_id"]: policy for policy in policies}
    email = by_id[email_policy["id"]]
    assert email["field"] == "email"
    assert email["classification"] == "PII"
    assert email["masking"] == "redact"
    assert email["total_hits"] == 2
    ssn = by_id[ssn_policy["id"]]
    assert ssn["field"] == "ssn"
    assert ssn["classification"] == "SECRET"
    assert ssn["masking"] == "partial"
    assert ssn["total_hits"] == 2

    # One listed day per policy (all views land on the same UTC day), and the
    # two roles merged into a single hit count.
    assert len(email["days"]) == 1
    assert email["days"][0]["hit_count"] == 2
    assert len(ssn["days"]) == 1
    assert ssn["days"][0]["hit_count"] == 2

    assert body["totals"] == {
        "total_hits": 4,
        "policy_count": 2,
        "day_count": 1,
    }


def test_disabled_policy_historical_hits_still_count(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    assert (
        client.patch(
            f"{policies_path()}/{policy['id']}", json={"enabled": False}
        ).status_code
        == 200
    )
    # A later view masks nothing for the disabled policy and adds no hit.
    assert post_view(client, "guest", [{"email": "d@e.f"}]).status_code == 200

    body = get_trend(client)
    assert len(body["policies"]) == 1
    row = body["policies"][0]
    assert row["policy_id"] == policy["id"]
    assert row["total_hits"] == 1
    assert body["totals"]["total_hits"] == 1


def test_trend_uses_policy_registration_field_and_masking(
    client: TestClient,
) -> None:
    # The row's field/classification/masking come from the policy registration,
    # not from the (denormalized) values stored on each hit record.
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    insert_hit_records(
        client,
        [
            # Hit carries a stale field name and a different masking mode.
            (1, "old_email", policy["id"], "guest", "partial",
             "2026-03-01T08:00:00+00:00"),
        ],
    )
    row = get_trend(client)["policies"][0]
    assert row["policy_id"] == policy["id"]
    assert row["field"] == "email"
    assert row["classification"] == "PII"
    assert row["masking"] == "redact"


def test_hit_count_counts_records_not_rows_or_fields(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    # Three masked values in one view write three records on one day.
    assert post_view(
        client, "guest",
        [{"email": "a@b.c"}, {"email": "d@e.f"}, {"email": "g@h.i"}],
    ).status_code == 200

    row = get_trend(client)["policies"][0]
    assert row["total_hits"] == 3
    assert row["days"][0]["hit_count"] == 3


# --------------------------------------------------------------------------- #
# Days, deltas and trend directions
# --------------------------------------------------------------------------- #


def test_days_list_only_hit_days_ascending_with_delta_and_trend(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    insert_hit_records(
        client,
        [
            (1, "email", policy["id"], "guest", "redact",
             "2026-03-01T08:00:00+00:00"),
            (2, "email", policy["id"], "guest", "redact",
             "2026-03-01T20:00:00+00:00"),
            (3, "email", policy["id"], "guest", "redact",
             "2026-03-01T23:00:00+00:00"),
            # A gap day (2026-03-02) with no hits must not appear.
            (4, "email", policy["id"], "auditor", "redact",
             "2026-03-03T00:30:00+00:00"),
            (5, "email", policy["id"], "guest", "redact",
             "2026-03-03T09:00:00+00:00"),
            (6, "email", policy["id"], "guest", "redact",
             "2026-03-04T09:00:00+00:00"),
            (7, "email", policy["id"], "guest", "redact",
             "2026-03-04T10:00:00+00:00"),
        ],
    )

    days = get_trend(client)["policies"][0]["days"]
    assert [set(day) for day in days] == [DAY_KEYS] * 3
    assert [day["day"] for day in days] == ["2026-03-01", "2026-03-03", "2026-03-04"]
    assert [day["hit_count"] for day in days] == [3, 2, 2]
    assert days[0]["hit_count_delta"] is None
    assert days[0]["trend"] == "none"
    assert days[1]["hit_count_delta"] == -1
    assert days[1]["trend"] == "down"
    assert days[2]["hit_count_delta"] == 0
    assert days[2]["trend"] == "flat"


def test_trend_directions_up_down_flat_and_none(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    counts = (3, 5, 2, 2)
    sequence = 1
    records = []
    for index, count in enumerate(counts):
        for _ in range(count):
            records.append(
                (
                    sequence,
                    "email",
                    policy["id"],
                    "guest",
                    "redact",
                    f"2026-03-0{index + 1}T08:00:00+00:00",
                )
            )
            sequence += 1
    insert_hit_records(client, records)

    days = get_trend(client)["policies"][0]["days"]
    assert [day["trend"] for day in days] == ["none", "up", "down", "flat"]
    assert [day["hit_count_delta"] for day in days] == [None, 2, -3, 0]


def test_days_bucket_by_utc_calendar_day_with_offset(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    insert_hit_records(
        client,
        [
            # 00:30 at +01:00 is still 2026-02-28 in UTC.
            (1, "email", policy["id"], "guest", "redact",
             "2026-03-01T00:30:00+01:00"),
            # 23:30 at +05:00 is 18:30 UTC on 2026-03-01.
            (2, "email", policy["id"], "guest", "redact",
             "2026-03-01T23:30:00+05:00"),
            # 02:00 at -05:00 is 07:00 UTC on 2026-03-01.
            (3, "email", policy["id"], "guest", "redact",
             "2026-03-01T02:00:00-05:00"),
            # 23:30 UTC crosses into the next UTC calendar day.
            (4, "email", policy["id"], "guest", "redact",
             "2026-03-01T23:30:00+00:00"),
        ],
    )

    days = get_trend(client)["policies"][0]["days"]
    assert [day["day"] for day in days] == ["2026-02-28", "2026-03-01"]
    assert days[0]["hit_count"] == 1
    assert days[1]["hit_count"] == 3
    assert days[1]["hit_count_delta"] == 2
    assert days[1]["trend"] == "up"


# --------------------------------------------------------------------------- #
# Ordering and totals
# --------------------------------------------------------------------------- #


def test_policies_sort_by_policy_id_independent_of_database_order(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    first = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    second = create_policy(
        client,
        {"field": "ssn", "classification": "SECRET", "masking": "partial",
         "allowed_roles": []},
    )
    third_id = second["id"] + 1
    # Insert hits in reverse policy-id order, including a dangling id that has
    # no policy row (such hits can never occur in normal flow and must not
    # fabricate a trend row).
    insert_hit_records(
        client,
        [
            (1, "ssn", second["id"], "guest", "partial",
             "2026-03-02T08:00:00+00:00"),
            (2, "email", first["id"], "guest", "redact",
             "2026-03-01T08:00:00+00:00"),
            (3, "ghost", third_id, "guest", "redact",
             "2026-03-01T09:00:00+00:00"),
        ],
    )

    body = get_trend(client)
    assert [policy["policy_id"] for policy in body["policies"]] == [
        first["id"],
        second["id"],
    ]


def test_totals_day_count_is_the_union_of_policy_days(client: TestClient) -> None:
    make_dataset_with_version(client)
    email = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    ssn = create_policy(
        client,
        {"field": "ssn", "classification": "SECRET", "masking": "partial",
         "allowed_roles": []},
    )
    insert_hit_records(
        client,
        [
            (1, "email", email["id"], "guest", "redact",
             "2026-03-01T08:00:00+00:00"),
            (2, "email", email["id"], "guest", "redact",
             "2026-03-02T08:00:00+00:00"),
            (3, "ssn", ssn["id"], "guest", "partial",
             "2026-03-02T09:00:00+00:00"),
            (4, "ssn", ssn["id"], "guest", "partial",
             "2026-03-03T09:00:00+00:00"),
        ],
    )

    body = get_trend(client)
    rows = {policy["policy_id"]: policy for policy in body["policies"]}
    assert [day["day"] for day in rows[email["id"]]["days"]] == [
        "2026-03-01",
        "2026-03-02",
    ]
    assert [day["day"] for day in rows[ssn["id"]]["days"]] == [
        "2026-03-02",
        "2026-03-03",
    ]
    # The shared 2026-03-02 counts once in the version-wide day union.
    totals = body["totals"]
    assert totals["total_hits"] == 4
    assert totals["policy_count"] == 2
    assert totals["day_count"] == 3
    assert totals["total_hits"] == sum(
        policy["total_hits"] for policy in body["policies"]
    )
    assert totals["day_count"] == len(
        {day["day"] for policy in body["policies"] for day in policy["days"]}
    )


def test_trend_is_scoped_to_its_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200

    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "email", "type": "string", "nullable": True}]},
        ).status_code
        == 201
    )
    assert get_trend(client, "orders", 2)["policies"] == []
    assert len(get_trend(client)["policies"]) == 1
    assert get_trend(client)["policies"][0]["policy_id"] == policy["id"]


# --------------------------------------------------------------------------- #
# Errors: 404 / 422, no writes
# --------------------------------------------------------------------------- #


def test_trend_unknown_dataset_or_version_returns_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    unknown_dataset = client.get(trend_path("ghost"))
    unknown_version = client.get(trend_path("orders", 9))
    assert unknown_dataset.status_code == 404
    assert unknown_version.status_code == 404
    for response in (unknown_dataset, unknown_version):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "not_found"


def test_trend_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    before = get_trend(client)

    with_body = client.request("GET", trend_path(), content=b"{}")
    with_query = client.get(trend_path(), params={"limit": 1})
    assert with_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, with_query):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]

    # The rejections wrote nothing and the trend still reads identically.
    assert get_trend(client) == before
    assert len(client.get(audit_path()).json()) == 1


def test_trend_shape_errors_keep_404_precedence(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert client.get(trend_path("ghost"), params={"x": "1"}).status_code == 404
    assert (
        client.request("GET", trend_path("orders", 9), content=b"{}").status_code
        == 404
    )


def test_trend_does_not_modify_records(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    records_before = client.get(audit_path()).json()

    for _ in range(3):
        assert get_trend(client)["policies"][0]["total_hits"] == 1
    assert client.get(audit_path()).json() == records_before


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


_CREATE_AND_VIEW_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

assert client.post("/datasets", json={"name": "orders"}).status_code == 201
assert client.post(
    "/datasets/orders/versions",
    json={"fields": [
        {"name": "email", "type": "string", "nullable": True},
        {"name": "ssn", "type": "string", "nullable": True},
    ]},
).status_code == 201
email = client.post(
    "/datasets/orders/versions/1/privacy-policies",
    json={
        "field": "email",
        "classification": "PII",
        "masking": "redact",
        "allowed_roles": [],
    },
)
assert email.status_code == 201, email.text
ssn = client.post(
    "/datasets/orders/versions/1/privacy-policies",
    json={
        "field": "ssn",
        "classification": "SECRET",
        "masking": "partial",
        "allowed_roles": [],
    },
)
assert ssn.status_code == 201, ssn.text
for role in ("guest", "auditor"):
    viewed = client.post(
        "/datasets/orders/versions/1/privacy-policies/view",
        json={"role": role, "rows": [{"email": "a@b.c", "ssn": "123456"}]},
    )
    assert viewed.status_code == 200, viewed.text
print("created")
"""

_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

trend = client.get(
    "/datasets/orders/versions/1/privacy-policies/view/audit-records/trend"
)
assert trend.status_code == 200, trend.text
body = trend.json()
assert set(body) == {"dataset", "version", "policies", "totals"}
assert body["dataset"] == "orders"
assert body["version"] == 1
policies = body["policies"]
assert len(policies) == 2
for policy in policies:
    assert set(policy) == {
        "policy_id", "field", "classification", "masking",
        "total_hits", "days",
    }
    assert policy["total_hits"] == 2
    assert len(policy["days"]) == 1
    day = policy["days"][0]
    assert set(day) == {"day", "hit_count", "hit_count_delta", "trend"}
    assert day["hit_count"] == 2
    assert day["hit_count_delta"] is None
    assert day["trend"] == "none"
assert body["totals"] == {
    "total_hits": 4,
    "policy_count": 2,
    "day_count": 1,
}

# A second read in the same process returns an identical result.
again = client.get(
    "/datasets/orders/versions/1/privacy-policies/view/audit-records/trend"
)
assert again.status_code == 200, again.text
assert again.json() == body
print("verified")
"""


def _run_script(db_path: Path, script: str) -> str:
    env = os.environ.copy()
    env["DATA_LINEAGE_DB"] = str(db_path)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_trend_survives_a_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "privacy-view-trend.db"
    assert _run_script(db_path, _CREATE_AND_VIEW_SCRIPT) == "created"

    # New interpreter: the trend is recomputed from the persisted records.
    assert _run_script(db_path, _VERIFY_SCRIPT) == "verified"
