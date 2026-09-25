"""Tests for the read-only masking-hit record filter endpoint."""

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


def make_dataset_with_version(client: TestClient, name: str = "orders") -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions", json={"fields": BASE_FIELDS}
    )
    assert response.status_code == 201, response.text


def policies_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/privacy-policies"


def filter_path(dataset: str = "orders", version: int = 1) -> str:
    return policies_path(dataset, version) + "/view/audit-records/filter"


def audit_path(dataset: str = "orders", version: int = 1) -> str:
    return policies_path(dataset, version) + "/view/audit-records"


def create_policy(client: TestClient, payload: dict) -> dict:
    response = client.post(policies_path(), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def post_view(client: TestClient, role: str, rows: list[dict]) -> None:
    response = client.post(
        policies_path() + "/view", json={"role": role, "rows": rows}
    )
    assert response.status_code == 200, response.text


def insert_records(
    role: str, field: str, policy_id: int, masking: str, times: list[str]
) -> None:
    """Append hit records with fixed write times for deterministic windows."""
    with db_session() as conn:
        version_id = conn.execute(
            "SELECT sv.id AS version_id FROM schema_versions sv JOIN datasets d "
            "ON d.id = sv.dataset_id WHERE d.name = 'orders' AND sv.version = 1"
        ).fetchone()["version_id"]
        tail = conn.execute(
            "SELECT MAX(sequence) AS m FROM privacy_view_audit_records "
            "WHERE version_id = ?",
            (version_id,),
        ).fetchone()["m"]
        next_sequence = (tail or 0) + 1
        for created_at in times:
            conn.execute(
                "INSERT INTO privacy_view_audit_records ("
                "version_id, sequence, field, policy_id, role, masking, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    version_id,
                    next_sequence,
                    field,
                    policy_id,
                    role,
                    masking,
                    created_at,
                ),
            )
            next_sequence += 1


def test_no_filters_matches_the_full_list_in_shape_and_order(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {
            "field": "email",
            "classification": "PII",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    post_view(client, "guest", [{"email": "a@b.c"}, {"email": "d@e.f"}])

    filtered = client.get(filter_path()).json()
    assert filtered == client.get(audit_path()).json()
    for record in filtered:
        assert set(record) == {
            "sequence",
            "field",
            "policy_id",
            "role",
            "masking",
            "created_at",
        }
    assert [record["sequence"] for record in filtered] == [1, 2]


def test_role_filter_is_exact_apart_from_case(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {
            "field": "email",
            "classification": "PII",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    post_view(client, "guest", [{"email": "a@b.c"}])
    post_view(client, "analyst", [{"email": "d@e.f"}])

    assert [r["role"] for r in client.get(filter_path(), params={"role": "guest"}).json()] == ["guest"]
    # Case-insensitive exact match; the stored value is echoed unchanged.
    upper = client.get(filter_path(), params={"role": "GUEST"}).json()
    assert [r["role"] for r in upper] == ["guest"]
    # No substring or other fuzzy matching.
    assert client.get(filter_path(), params={"role": "gues"}).json() == []
    assert client.get(filter_path(), params={"role": "guest-x"}).json() == []


def test_field_filter_is_exact_apart_from_case(client: TestClient) -> None:
    make_dataset_with_version(client)
    email_policy = create_policy(
        client,
        {
            "field": "email",
            "classification": "PII",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    create_policy(
        client,
        {
            "field": "ssn",
            "classification": "secret",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    post_view(client, "guest", [{"email": "a@b.c", "ssn": "1"}])

    assert [r["field"] for r in client.get(filter_path(), params={"field": "ssn"}).json()] == ["ssn"]
    upper = client.get(filter_path(), params={"field": "EMAIL"}).json()
    assert [r["field"] for r in upper] == ["email"]
    assert upper[0]["policy_id"] == email_policy["id"]
    assert client.get(filter_path(), params={"field": "emai"}).json() == []
    assert client.get(filter_path(), params={"field": "email2"}).json() == []


def test_time_window_is_closed_and_each_bound_works_alone(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {
            "field": "email",
            "classification": "PII",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    times = [
        "2024-01-01T00:00:00+00:00",
        "2024-02-15T12:30:00+00:00",
        "2024-03-31T23:59:59+00:00",
    ]
    insert_records("guest", "email", policy["id"], "redact", times)

    # Closed interval: records exactly on either bound are included.
    both = client.get(
        filter_path(),
        params={
            "start_at": "2024-01-01T00:00:00Z",
            "end_at": "2024-03-31T23:59:59Z",
        },
    ).json()
    assert [r["created_at"] for r in both] == times

    middle = client.get(
        filter_path(),
        params={
            "start_at": "2024-02-01T00:00:00Z",
            "end_at": "2024-02-28T00:00:00Z",
        },
    ).json()
    assert [r["created_at"] for r in middle] == ["2024-02-15T12:30:00+00:00"]

    only_start = client.get(
        filter_path(), params={"start_at": "2024-02-15T12:30:00Z"}
    ).json()
    assert [r["created_at"] for r in only_start] == [times[1], times[2]]

    only_end = client.get(
        filter_path(), params={"end_at": "2024-02-15T12:30:00Z"}
    ).json()
    assert [r["created_at"] for r in only_end] == [times[0], times[1]]


def test_time_window_normalizes_other_offsets(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {
            "field": "email",
            "classification": "PII",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    # 09:00+09:00 is exactly 00:00Z: a closed window ending on that instant
    # keeps the record written at midnight UTC.
    insert_records(
        "guest", "email", policy["id"], "redact", ["2024-01-01T00:00:00+00:00"]
    )
    response = client.get(
        filter_path(), params={"end_at": "2024-01-01T09:00:00+09:00"}
    )
    assert response.status_code == 200, response.text
    assert len(response.json()) == 1


def test_filters_combine_with_and_and_empty_match_is_an_empty_array(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {
            "field": "email",
            "classification": "PII",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    insert_records(
        "guest",
        "email",
        policy["id"],
        "redact",
        ["2024-01-01T00:00:00+00:00", "2024-06-01T00:00:00+00:00"],
    )

    combined = client.get(
        filter_path(),
        params={
            "role": "GUEST",
            "field": "Email",
            "start_at": "2024-05-01T00:00:00Z",
            "end_at": "2024-07-01T00:00:00Z",
        },
    ).json()
    assert [r["created_at"] for r in combined] == ["2024-06-01T00:00:00+00:00"]

    # Criteria that each match something but no record satisfies together.
    no_match = client.get(
        filter_path(),
        params={"role": "guest", "start_at": "2030-01-01T00:00:00Z"},
    )
    assert no_match.status_code == 200
    assert no_match.json() == []


def test_filter_results_ordered_by_sequence_ascending(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {
            "field": "email",
            "classification": "PII",
            "masking": "partial",
            "allowed_roles": [],
        },
    )
    insert_records(
        "guest",
        "email",
        policy["id"],
        "partial",
        [
            "2024-03-01T00:00:00+00:00",
            "2024-01-01T00:00:00+00:00",
            "2024-02-01T00:00:00+00:00",
        ],
    )
    records = client.get(
        filter_path(), params={"role": "guest"}
    ).json()
    # Write (sequence) order, not timestamp order.
    assert [r["created_at"] for r in records] == [
        "2024-03-01T00:00:00+00:00",
        "2024-01-01T00:00:00+00:00",
        "2024-02-01T00:00:00+00:00",
    ]
    assert [r["sequence"] for r in records] == [1, 2, 3]


def test_filter_is_read_only(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {
            "field": "email",
            "classification": "PII",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    post_view(client, "guest", [{"email": "a@b.c"}])
    before = client.get(audit_path()).json()

    for params in (
        {"role": "nobody"},
        {"field": "missing"},
        {"start_at": "2030-01-01T00:00:00Z"},
        {"start_at": "bad"},
        {"role": "  "},
        {"unknown": "1"},
    ):
        client.get(filter_path(), params=params)
    client.request("GET", filter_path(), content=b"{}")

    assert client.get(audit_path()).json() == before
    # The summary and diff responses keep their existing shape.
    summary = client.get(audit_path() + "/summary")
    assert summary.status_code == 200
    assert summary.json()["groups"][0]["hit_count"] == 1


def test_filter_validation_errors(client: TestClient) -> None:
    make_dataset_with_version(client)

    cases = [
        {"start_at": "not-a-timestamp"},
        {"end_at": "2024-13-01T00:00:00Z"},
        {"start_at": "2024-01-01T00:00:00"},  # missing timezone
        {"end_at": "2024-01-01"},              # a bare date has no timezone
        {"role": "   "},
        {"field": ""},
        {"start_at": "2024-02-01T00:00:00Z", "end_at": "2024-01-01T00:00:00Z"},
        {"masking": "redact"},  # not an accepted filter
    ]
    for params in cases:
        response = client.get(filter_path(), params=params)
        assert response.status_code == 422, params
        body = response.json()
        assert set(body) == {"error", "detail"}
        assert body["error"] == "validation_error"

    assert client.request("GET", filter_path(), content=b"{}").status_code == 422


def test_filter_unknown_dataset_or_version_returns_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert client.get(filter_path("ghost")).status_code == 404
    assert client.get(filter_path("orders", 9)).status_code == 404


def test_filter_404_takes_precedence_over_422(client: TestClient) -> None:
    make_dataset_with_version(client)
    # Same precedence as the full record read: the path resolves before the
    # query parameters, body and window are validated.
    assert (
        client.get(filter_path("ghost"), params={"start_at": "bad"}).status_code
        == 404
    )
    assert (
        client.get(
            filter_path("orders", 9),
            params={
                "start_at": "2024-02-01T00:00:00Z",
                "end_at": "2024-01-01T00:00:00Z",
            },
        ).status_code
        == 404
    )
    assert (
        client.request(
            "GET",
            filter_path("ghost"),
            params={"unknown": "1"},
            content=b"{}",
        ).status_code
        == 404
    )


_CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

assert client.post("/datasets", json={"name": "orders"}).status_code == 201
assert client.post(
    "/datasets/orders/versions",
    json={"fields": [{"name": "email", "type": "string", "nullable": True}]},
).status_code == 201
policy = client.post(
    "/datasets/orders/versions/1/privacy-policies",
    json={
        "field": "email",
        "classification": "PII",
        "masking": "redact",
        "allowed_roles": [],
    },
)
assert policy.status_code == 201, policy.text
viewed = client.post(
    "/datasets/orders/versions/1/privacy-policies/view",
    json={"role": "guest", "rows": [{"email": "a@b.c"}]},
)
assert viewed.status_code == 200, viewed.text
"""


_FILTER_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

base = "/datasets/orders/versions/1/privacy-policies/view/audit-records"
full = client.get(base)
assert full.status_code == 200, full.text
created_at = full.json()[0]["created_at"]

filtered = client.get(base + "/filter", params={"role": "GUEST", "start_at": created_at, "end_at": created_at})
assert filtered.status_code == 200, filtered.text
assert filtered.json() == full.json()
assert client.get(base + "/filter", params={"role": "other"}).json() == []
"""


def _run_script(db_path: Path, script: str) -> None:
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


def test_filter_results_survive_a_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "privacy-view-audit-filter.db"
    _run_script(db_path, _CREATE_SCRIPT)
    _run_script(db_path, _FILTER_SCRIPT)
