"""Tests for the read-only filtered search of privacy view masking hits."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

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


def audit_path(dataset: str = "orders", version: int = 1) -> str:
    return policies_path(dataset, version) + "/view/audit-records"


def search_path(dataset: str = "orders", version: int = 1) -> str:
    return audit_path(dataset, version) + "/search"


def create_policy(client: TestClient, payload: dict) -> dict:
    response = client.post(policies_path(), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def post_view(
    client: TestClient, role: str, rows: list[dict], at: str | None = None
) -> None:
    # Pin the batch write time when interval filtering needs fixed bounds.
    real_now = None
    if at is not None:
        from app import repository

        real_now = repository.utc_now_iso
        repository.utc_now_iso = lambda: at  # type: ignore[assignment]
    try:
        response = client.post(
            policies_path() + "/view", json={"role": role, "rows": rows}
        )
        assert response.status_code == 200, response.text
    finally:
        if real_now is not None:
            from app import repository

            repository.utc_now_iso = real_now  # type: ignore[assignment]


def seed_records(client: TestClient) -> list[dict]:
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
    post_view(
        client, "guest",
        [{"email": "a@b.c", "ssn": "1"}, {"email": "d@e.f"}],
        at="2026-01-01T00:00:00+00:00",
    )
    post_view(
        client, "Analyst",
        [{"ssn": "2"}],
        at="2026-02-15T12:30:00+00:00",
    )
    post_view(
        client, "guest",
        [{"email": "g@h.i"}],
        at="2026-03-31T23:59:59+09:00",
    )
    records = client.get(audit_path()).json()
    assert [record["sequence"] for record in records] == [1, 2, 3, 4, 5]
    return records


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #


def test_no_filters_returns_every_record_like_the_full_list(
    client: TestClient,
) -> None:
    all_records = seed_records(client)
    response = client.get(search_path())
    assert response.status_code == 200, response.text
    assert response.json() == all_records


def test_filter_by_role_is_case_insensitive_exact(client: TestClient) -> None:
    seed_records(client)
    for value in ("guest", "GUEST", "GuEsT"):
        records = client.get(search_path(), params={"role": value}).json()
        assert [record["sequence"] for record in records] == [1, 2, 3, 5]
        assert {record["role"] for record in records} == {"guest"}

    records = client.get(search_path(), params={"role": "analyst"}).json()
    assert [record["sequence"] for record in records] == [4]
    assert records[0]["role"] == "Analyst"


def test_role_matches_exactly_without_substring_or_trim_fuzziness(
    client: TestClient,
) -> None:
    seed_records(client)
    assert client.get(search_path(), params={"role": "gues"}).json() == []
    assert client.get(search_path(), params={"role": " guest "}).json() == []
    assert client.get(search_path(), params={"role": "guest,analyst"}).json() == []


def test_filter_by_field_is_case_insensitive_exact(client: TestClient) -> None:
    seed_records(client)
    for value in ("email", "EMAIL", "Email"):
        records = client.get(search_path(), params={"field": value}).json()
        assert [record["sequence"] for record in records] == [1, 3, 5]
        assert {record["field"] for record in records} == {"email"}

    records = client.get(search_path(), params={"field": "SSN"}).json()
    assert [record["sequence"] for record in records] == [2, 4]

    assert client.get(search_path(), params={"field": "ss"}).json() == []
    assert client.get(search_path(), params={"field": " email "}).json() == []


def test_filters_combine_with_and(client: TestClient) -> None:
    seed_records(client)
    records = client.get(
        search_path(), params={"role": "guest", "field": "ssn"}
    ).json()
    assert [record["sequence"] for record in records] == [2]


def test_start_only_closed_interval_matches_boundary(client: TestClient) -> None:
    seed_records(client)
    # Sequence 4 was written at exactly 12:30:00Z; closed interval includes it.
    records = client.get(
        search_path(), params={"start": "2026-02-15T12:30:00Z"}
    ).json()
    assert [record["sequence"] for record in records] == [4, 5]


def test_end_only_closed_interval_matches_boundary(client: TestClient) -> None:
    seed_records(client)
    records = client.get(
        search_path(), params={"end": "2026-01-01T00:00:00+00:00"}
    ).json()
    assert [record["sequence"] for record in records] == [1, 2, 3]


def test_interval_respects_supplied_timezone_offsets(client: TestClient) -> None:
    seed_records(client)
    # Sequence 5 is 2026-03-31T23:59:59+09:00 == 2026-03-31T14:59:59Z.
    records = client.get(
        search_path(),
        params={
            "start": "2026-03-31T23:00:00+09:00",
            "end": "2026-04-01T00:00:00+09:00",
        },
    ).json()
    assert [record["sequence"] for record in records] == [5]

    # The same instant expressed in UTC, with both endpoints touching it.
    records = client.get(
        search_path(),
        params={
            "start": "2026-03-31T14:59:59+00:00",
            "end": "2026-03-31T14:59:59Z",
        },
    ).json()
    assert [record["sequence"] for record in records] == [5]


def test_role_field_and_interval_together(client: TestClient) -> None:
    seed_records(client)
    records = client.get(
        search_path(),
        params={
            "role": "GUEST",
            "field": "email",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-02T00:00:00Z",
        },
    ).json()
    assert [record["sequence"] for record in records] == [1, 3]


def test_no_match_returns_empty_array_not_error(client: TestClient) -> None:
    seed_records(client)
    response = client.get(search_path(), params={"role": "nobody"})
    assert response.status_code == 200
    assert response.json() == []


def test_empty_version_search_returns_empty_array(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.get(search_path(), params={"role": "guest"})
    assert response.status_code == 200
    assert response.json() == []


def test_matches_keep_full_record_shape_and_sequence_order(
    client: TestClient,
) -> None:
    all_records = seed_records(client)
    records = client.get(search_path(), params={"field": "email"}).json()
    assert records == [
        record for record in all_records if record["field"] == "email"
    ]
    for record in records:
        assert set(record) == {
            "sequence", "field", "policy_id", "role", "masking", "created_at",
        }
        datetime.fromisoformat(record["created_at"])


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_search_unknown_dataset_or_version_returns_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    unknown_dataset = client.get(search_path("ghost"), params={"role": "x"})
    unknown_version = client.get(search_path("orders", 9), params={"role": "x"})
    assert unknown_dataset.status_code == 404
    assert unknown_version.status_code == 404
    for response in (unknown_dataset, unknown_version):
        assert response.json()["error"] == "not_found"


def test_search_rejects_body(client: TestClient) -> None:
    seed_records(client)
    response = client.request(
        "GET", search_path(), params={"role": "guest"}, content=b"{}"
    )
    assert response.status_code == 422
    assert set(response.json()) == {"error", "detail"}


def test_search_rejects_unknown_query_parameters(client: TestClient) -> None:
    seed_records(client)
    response = client.get(search_path(), params={"masking": "redact"})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert response.json()["detail"]
    # No SQL or internals leak into the detail.
    assert "select" not in response.json()["detail"].lower()


def test_search_rejects_unparseable_timestamps(client: TestClient) -> None:
    seed_records(client)
    for param, value in (
        ("start", "not-a-time"),
        ("end", "2026-13-99T00:00:00Z"),
        ("start", "2026-01-01"),  # date without time
    ):
        response = client.get(search_path(), params={param: value})
        assert response.status_code == 422, (param, response.text)
        assert set(response.json()) == {"error", "detail"}


def test_search_rejects_naive_timestamps_without_timezone(
    client: TestClient,
) -> None:
    seed_records(client)
    for param in ("start", "end"):
        response = client.get(
            search_path(), params={param: "2026-01-01T00:00:00"}
        )
        assert response.status_code == 422
        assert response.json()["error"] == "validation_error"


def test_search_start_later_than_end_returns_422(client: TestClient) -> None:
    seed_records(client)
    response = client.get(
        search_path(),
        params={
            "start": "2026-03-01T00:00:00Z",
            "end": "2026-02-01T00:00:00Z",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_search_equal_bounds_are_valid(client: TestClient) -> None:
    seed_records(client)
    response = client.get(
        search_path(),
        params={
            "start": "2026-02-15T12:30:00Z",
            "end": "2026-02-15T12:30:00Z",
        },
    )
    assert response.status_code == 200
    assert [record["sequence"] for record in response.json()] == [4]


def test_search_shape_errors_keep_404_precedence(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert (
        client.get(search_path("ghost"), params={"bogus": "1"}).status_code
        == 404
    )
    assert (
        client.request(
            "GET", search_path("orders", 9), content=b"{}"
        ).status_code
        == 404
    )
    assert (
        client.get(
            search_path("ghost"),
            params={"start": "2026-01-01T00:00:00",
                    "end": "2020-01-01T00:00:00Z"},
        ).status_code
        == 404
    )


def test_search_is_read_only(client: TestClient) -> None:
    seed_records(client)
    before = client.get(audit_path()).json()

    rejected = [
        {"masking": "redact"},
        {"start": "nonsense"},
        {"end": "2026-01-01T00:00:00"},
        {"start": "2026-03-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"},
    ]
    for params in rejected:
        response = client.get(search_path(), params=params)
        assert response.status_code == 422, params
    # Valid searches likewise change nothing, including the summary.
    client.get(search_path(), params={"role": "guest"})
    client.get(search_path(), params={"start": "2026-02-01T00:00:00Z"})

    assert client.get(audit_path()).json() == before
    summary = client.get(audit_path() + "/summary").json()
    assert sum(group["hit_count"] for group in summary["groups"]) == len(before)


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


_CREATE_AND_SEARCH_SCRIPT = """
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
policy = client.post(
    "/datasets/orders/versions/1/privacy-policies",
    json={
        "field": "email", "classification": "PII",
        "masking": "redact", "allowed_roles": [],
    },
)
assert policy.status_code == 201, policy.text
from app import repository
repository.utc_now_iso = lambda: "2026-05-01T08:00:00+00:00"
viewed = client.post(
    "/datasets/orders/versions/1/privacy-policies/view",
    json={"role": "guest", "rows": [{"email": "a@b.c"}]},
)
assert viewed.status_code == 200, viewed.text
print("created")
"""

_VERIFY_SEARCH_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

path = "/datasets/orders/versions/1/privacy-policies/view/audit-records/search"
inside = client.get(path, params={
    "role": "GUEST", "field": "email",
    "start": "2026-05-01T00:00:00Z", "end": "2026-05-02T00:00:00Z",
})
assert inside.status_code == 200, inside.text
assert [r["sequence"] for r in inside.json()] == [1]
outside = client.get(path, params={"start": "2026-06-01T00:00:00Z"})
assert outside.status_code == 200, outside.text
assert outside.json() == []
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


def test_search_results_survive_a_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "privacy-view-search.db"
    assert _run_script(db_path, _CREATE_AND_SEARCH_SCRIPT) == "created"
    assert _run_script(db_path, _VERIFY_SEARCH_SCRIPT) == "verified"
