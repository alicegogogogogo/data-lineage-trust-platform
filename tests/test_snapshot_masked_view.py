"""Tests for the snapshot masked view (role-scoped time-travel read)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "email", "type": "string", "nullable": True},
    {"name": "ssn", "type": "string", "nullable": True},
    {"name": "age", "type": "integer", "nullable": True},
]

ROWS = [
    {"id": 1, "email": "alice@example.com", "ssn": "123-45-6789", "age": 34},
    {"id": 2, "email": "bob@example.com", "ssn": None, "age": 41},
]

FAR_FUTURE = "2099-01-01T00:00:00Z"
FAR_PAST = "2000-01-01T00:00:00Z"


def make_dataset_with_version(client: TestClient, name: str = "orders") -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions", json={"fields": BASE_FIELDS}
    )
    assert response.status_code == 201, response.text


def policies_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/privacy-policies"


def snapshots_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/snapshots"


def masked_view_path(dataset: str = "orders", version: int = 1) -> str:
    return snapshots_path(dataset, version) + "/masked-view"


def audit_path(dataset: str = "orders", version: int = 1) -> str:
    return policies_path(dataset, version) + "/view/audit-records"


def access_path(dataset: str = "orders", version: int = 1) -> str:
    return policies_path(dataset, version) + "/view/access-records"


def create_policy(client: TestClient, payload: dict) -> dict:
    response = client.post(policies_path(), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def create_snapshot(client: TestClient, rows: list[dict]) -> dict:
    response = client.post(snapshots_path(), json={"rows": rows})
    assert response.status_code == 201, response.text
    return response.json()


def post_masked_view(
    client: TestClient,
    role: str = "guest",
    timestamp: str = FAR_FUTURE,
    dataset: str = "orders",
    version: int = 1,
):
    return client.post(
        masked_view_path(dataset, version),
        json={"role": role, "timestamp": timestamp},
    )


def audit_records(client: TestClient) -> list[dict]:
    response = client.get(audit_path())
    assert response.status_code == 200, response.text
    return response.json()


def access_records(client: TestClient) -> list[dict]:
    response = client.get(access_path())
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Successful masked reads
# --------------------------------------------------------------------------- #


def test_masked_view_masks_snapshot_rows_like_the_privacy_view(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": []},
    )
    create_policy(
        client,
        {"field": "ssn", "classification": "PII", "masking": "redact",
         "allowed_roles": ["analyst"]},
    )
    snapshot = create_snapshot(client, ROWS)

    response = post_masked_view(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["snapshot_id"] == snapshot["id"]
    assert body["created_at"] == snapshot["created_at"]
    # Row order is preserved; masking matches the privacy view exactly:
    # partial keeps the first character and the last two, redact replaces
    # every non-null value, null values stay null.
    assert body["rows"] == [
        {"id": 1, "email": "aom", "ssn": "***", "age": 34},
        {"id": 2, "email": "bom", "ssn": None, "age": 41},
    ]


def test_masked_view_selects_the_newest_snapshot_at_or_before_the_timestamp(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    first = create_snapshot(client, [{"id": 1, "email": "a@b.co"}])
    second = create_snapshot(client, [{"id": 2, "email": "c@d.co"}])
    assert first["id"] != second["id"]

    # A timestamp between the two creation instants selects the first one.
    first_at = datetime.fromisoformat(first["created_at"])
    second_at = datetime.fromisoformat(second["created_at"])
    midpoint = (first_at + (second_at - first_at) / 2).isoformat()

    response = post_masked_view(client, timestamp=midpoint)
    assert response.status_code == 200, response.text
    assert response.json()["snapshot_id"] == first["id"]

    # The second snapshot's own creation instant and the far future both
    # select the second one.
    response = post_masked_view(client, timestamp=second["created_at"])
    assert response.json()["snapshot_id"] == second["id"]
    response = post_masked_view(client, timestamp=FAR_FUTURE)
    assert response.json()["snapshot_id"] == second["id"]


def test_masked_view_allowed_role_sees_raw_values(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": ["analyst"]},
    )
    create_snapshot(client, ROWS)

    response = post_masked_view(client, role="analyst")

    assert response.status_code == 200, response.text
    assert response.json()["rows"] == ROWS
    assert audit_records(client) == []
    # The allowed-role read still leaves exactly one access record.
    records = access_records(client)
    assert len(records) == 1
    assert records[0]["role"] == "analyst"
    assert records[0]["row_count"] == 2
    assert records[0]["masked_count"] == 0


def test_masked_view_disabled_policy_does_not_mask(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert client.patch(
        policies_path() + f"/{policy['id']}", json={"enabled": False}
    ).status_code == 200
    create_snapshot(client, ROWS)

    response = post_masked_view(client)

    assert response.status_code == 200, response.text
    assert response.json()["rows"] == ROWS


def test_masked_view_empty_snapshot_succeeds(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    snapshot = create_snapshot(client, [])

    response = post_masked_view(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["snapshot_id"] == snapshot["id"]
    assert body["rows"] == []
    assert audit_records(client) == []
    records = access_records(client)
    assert len(records) == 1
    assert records[0]["row_count"] == 0
    assert records[0]["masked_count"] == 0


def test_masked_view_all_null_values_hit_nothing(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    create_snapshot(client, [{"id": 1, "email": None}, {"id": 2, "email": None}])

    response = post_masked_view(client)

    assert response.status_code == 200, response.text
    assert response.json()["rows"] == [
        {"id": 1, "email": None},
        {"id": 2, "email": None},
    ]
    assert audit_records(client) == []
    assert len(access_records(client)) == 1


def test_masked_view_version_without_policies_returns_rows_verbatim(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_snapshot(client, ROWS)

    response = post_masked_view(client)

    assert response.status_code == 200, response.text
    assert response.json()["rows"] == ROWS
    assert audit_records(client) == []
    assert len(access_records(client)) == 1


# --------------------------------------------------------------------------- #
# Audit trail: hit records and the access record
# --------------------------------------------------------------------------- #


def test_masked_view_writes_hit_records_and_one_access_record(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    email_policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": []},
    )
    ssn_policy = create_policy(
        client,
        {"field": "ssn", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    create_snapshot(client, ROWS)

    response = post_masked_view(client, role="guest")
    assert response.status_code == 200, response.text

    # One hit per masked value, in row order (fields of a row in policy id
    # order): email of row 1, ssn of row 1, email of row 2 (its ssn is null).
    hits = audit_records(client)
    assert [(h["field"], h["policy_id"], h["masking"]) for h in hits] == [
        ("email", email_policy["id"], "partial"),
        ("ssn", ssn_policy["id"], "redact"),
        ("email", email_policy["id"], "partial"),
    ]
    assert [h["sequence"] for h in hits] == [1, 2, 3]
    assert all(h["role"] == "guest" for h in hits)
    # All records of one read share a single write timestamp.
    assert len({h["created_at"] for h in hits}) == 1

    records = access_records(client)
    assert len(records) == 1
    assert records[0]["sequence"] == 1
    assert records[0]["role"] == "guest"
    assert records[0]["row_count"] == 2
    assert records[0]["masked_count"] == 3


def test_masked_view_trail_shares_sequences_with_the_privacy_view(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    create_snapshot(client, ROWS)

    # A privacy view first, then a masked-view read: both trails feed the
    # same per-version sequence runs without gaps or reuse.
    view_response = client.post(
        policies_path() + "/view",
        json={"role": "guest", "rows": [{"id": 9, "email": "x@y.zzzzz"}]},
    )
    assert view_response.status_code == 200, view_response.text
    assert post_masked_view(client).status_code == 200

    hits = audit_records(client)
    assert [h["sequence"] for h in hits] == [1, 2, 3]
    records = access_records(client)
    assert [r["sequence"] for r in records] == [1, 2]
    assert [r["masked_count"] for r in records] == [1, 2]


def test_masked_view_records_survive_a_restart(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    create_snapshot(client, ROWS)
    assert post_masked_view(client).status_code == 200

    # A fresh app instance over the same database file sees the same trail.
    restarted = TestClient(app)
    assert len(restarted.get(audit_path()).json()) == 2
    assert len(restarted.get(access_path()).json()) == 1
    response = restarted.post(
        masked_view_path(), json={"role": "guest", "timestamp": FAR_FUTURE}
    )
    assert response.status_code == 200, response.text
    hits = restarted.get(audit_path()).json()
    assert [h["sequence"] for h in hits] == [1, 2, 3, 4]


# --------------------------------------------------------------------------- #
# The read never modifies snapshots, rows, policies or identifications
# --------------------------------------------------------------------------- #


def test_masked_view_does_not_modify_the_snapshot_or_policies(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    snapshot = create_snapshot(client, ROWS)

    assert post_masked_view(client).status_code == 200

    stored = client.get(snapshots_path() + f"/{snapshot['id']}").json()
    assert stored["rows"] == ROWS
    assert client.get(snapshots_path()).json() == [
        {
            "id": snapshot["id"],
            "dataset": "orders",
            "version": 1,
            "created_at": snapshot["created_at"],
            "row_count": 2,
        }
    ]
    policies = client.get(policies_path()).json()
    assert policies == [policy]


# --------------------------------------------------------------------------- #
# 404s, checked before the request-shape rules
# --------------------------------------------------------------------------- #


def test_masked_view_unknown_dataset_is_404_before_shape_checks(
    client: TestClient,
) -> None:
    response = client.post(
        masked_view_path("nope"), json={"role": "guest", "timestamp": FAR_FUTURE}
    )
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}

    # Even a malformed body does not turn an unknown dataset into a 422.
    response = client.post(masked_view_path("nope"), content=b"not json")
    assert response.status_code == 404


def test_masked_view_unknown_version_is_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.post(
        masked_view_path(version=9),
        json={"role": "guest", "timestamp": FAR_FUTURE},
    )
    assert response.status_code == 404


def test_masked_view_without_eligible_snapshot_is_404_before_shape_checks(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_snapshot(client, ROWS)

    # Every snapshot is newer than the requested timestamp.
    response = post_masked_view(client, timestamp=FAR_PAST)
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}

    # The 404 keeps precedence over a blank role and an extra field.
    response = client.post(
        masked_view_path(),
        json={"role": "  ", "timestamp": FAR_PAST, "extra": 1},
    )
    assert response.status_code == 404

    # A version with no snapshot at all answers the same way.
    response = post_masked_view(client)
    assert response.status_code == 200  # the far-future default still matches
    other = "empty"
    make_dataset_with_version(client, other)
    response = client.post(
        masked_view_path(other),
        json={"role": "guest", "timestamp": FAR_FUTURE},
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# 422 request-shape rules; nothing is written on rejection
# --------------------------------------------------------------------------- #


def _assert_422_and_nothing_written(client: TestClient, response) -> None:
    assert response.status_code == 422, response.text
    assert set(response.json()) == {"error", "detail"}
    assert audit_records(client) == []
    assert access_records(client) == []


def test_masked_view_missing_fields_are_422(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_snapshot(client, ROWS)

    _assert_422_and_nothing_written(
        client, client.post(masked_view_path(), json={"timestamp": FAR_FUTURE})
    )
    _assert_422_and_nothing_written(
        client, client.post(masked_view_path(), json={"role": "guest"})
    )
    _assert_422_and_nothing_written(
        client, client.post(masked_view_path(), json={})
    )


def test_masked_view_wrongly_typed_fields_are_422(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_snapshot(client, ROWS)

    for payload in (
        {"role": 7, "timestamp": FAR_FUTURE},
        {"role": None, "timestamp": FAR_FUTURE},
        {"role": "guest", "timestamp": 7},
        {"role": "guest", "timestamp": None},
    ):
        _assert_422_and_nothing_written(
            client, client.post(masked_view_path(), json=payload)
        )


def test_masked_view_blank_role_is_422(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_snapshot(client, ROWS)

    _assert_422_and_nothing_written(
        client,
        client.post(masked_view_path(), json={"role": "   ", "timestamp": FAR_FUTURE}),
    )


def test_masked_view_bad_timestamp_is_422(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_snapshot(client, ROWS)

    for timestamp in ("not-a-date", "2026-01-01", "2026-01-01T00:00:00"):
        _assert_422_and_nothing_written(
            client,
            client.post(
                masked_view_path(),
                json={"role": "guest", "timestamp": timestamp},
            )
        )


def test_masked_view_extra_field_is_422(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_snapshot(client, ROWS)

    _assert_422_and_nothing_written(
        client,
        client.post(
            masked_view_path(),
            json={"role": "guest", "timestamp": FAR_FUTURE, "rows": []},
        ),
    )


def test_masked_view_empty_whitespace_and_invalid_json_bodies_are_422(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_snapshot(client, ROWS)

    _assert_422_and_nothing_written(client, client.post(masked_view_path()))
    _assert_422_and_nothing_written(
        client, client.post(masked_view_path(), content=b"   \n\t ")
    )
    _assert_422_and_nothing_written(
        client, client.post(masked_view_path(), content=b"{not json")
    )
    _assert_422_and_nothing_written(
        client, client.post(masked_view_path(), content=b"[1, 2]")
    )


def test_masked_view_query_parameters_are_422(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_snapshot(client, ROWS)

    _assert_422_and_nothing_written(
        client,
        client.post(
            masked_view_path() + "?role=guest",
            json={"role": "guest", "timestamp": FAR_FUTURE},
        ),
    )


def test_masked_view_rejections_leave_the_snapshot_untouched(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    snapshot = create_snapshot(client, ROWS)

    assert client.post(masked_view_path(), json={}).status_code == 422
    assert client.post(masked_view_path(), content=b"junk").status_code == 422

    stored = client.get(snapshots_path() + f"/{snapshot['id']}").json()
    assert stored["rows"] == ROWS
