"""Tests for the dataset-level cross-version privacy compliance export."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "email", "type": "string", "nullable": True},
    {"name": "ssn", "type": "string", "nullable": True},
]

FUTURE = "2099-01-01T00:00:00+00:00"


def export_path(dataset: str = "orders") -> str:
    return f"/datasets/{dataset}/privacy-compliance-export"


def policies_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/privacy-policies"


def audit_path(dataset: str = "orders", version: int = 1) -> str:
    return policies_path(dataset, version) + "/view/audit-records"


def make_dataset(client: TestClient, name: str = "orders") -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201


def make_version(client: TestClient, dataset: str = "orders") -> int:
    response = client.post(
        f"/datasets/{dataset}/versions", json={"fields": BASE_FIELDS}
    )
    assert response.status_code == 201, response.text
    return response.json()["version"]


def create_policy(client: TestClient, payload: dict, **path: object) -> dict:
    response = client.post(policies_path(**path), json=payload)  # type: ignore[arg-type]
    assert response.status_code == 201, response.text
    return response.json()


def post_view(client: TestClient, role: str, rows: list[dict], **path: object) -> None:
    response = client.post(
        policies_path(**path) + "/view",  # type: ignore[arg-type]
        json={"role": role, "rows": rows},
    )
    assert response.status_code == 200, response.text


def make_cleanup_request(
    client: TestClient, *, reason: str = "legal hold", before: str = FUTURE, **path: object
) -> dict:
    response = client.post(
        audit_path(**path) + "/cleanup-requests",  # type: ignore[arg-type]
        json={"reason": reason, "before": before},
    )
    assert response.status_code == 201, response.text
    return response.json()


def confirm_cleanup(client: TestClient, request_id: int, **path: object) -> dict:
    response = client.post(
        f"{audit_path(**path)}/cleanup-requests/{request_id}/confirm"  # type: ignore[arg-type]
    )
    assert response.status_code == 200, response.text
    return response.json()


def get_export(client: TestClient, dataset: str = "orders") -> dict:
    response = client.get(export_path(dataset))
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Empty states
# --------------------------------------------------------------------------- #


def test_dataset_without_versions_exports_empty_result(client: TestClient) -> None:
    make_dataset(client)

    response = client.get(export_path())

    assert response.status_code == 200, response.text
    assert response.json() == {
        "dataset": "orders",
        "versions": [],
        "totals": {
            "policy_count": 0,
            "hit_count": 0,
            "masked_count": 0,
            "view_count": 0,
            "cleanup_request_count": 0,
        },
    }


def test_version_without_any_records_exports_zero_counts(client: TestClient) -> None:
    make_dataset(client)
    make_version(client)

    body = get_export(client)

    assert body["versions"] == [
        {
            "version": 1,
            "policies": [],
            "hit_count": 0,
            "masked_count": 0,
            "view_count": 0,
            "cleanup_requests": [],
        }
    ]
    assert body["totals"] == {
        "policy_count": 0,
        "hit_count": 0,
        "masked_count": 0,
        "view_count": 0,
        "cleanup_request_count": 0,
    }


# --------------------------------------------------------------------------- #
# Content and ordering
# --------------------------------------------------------------------------- #


def seed_dataset(client: TestClient) -> None:
    """Two versions with policies, views and cleanup requests.

    Version 1: two policies, two views (three masked values over both views),
    one confirmed cleanup request removing the first view's two hits and one
    pending request on top of the survivor.
    Version 2: one disabled policy, one view masking nothing.
    """
    make_dataset(client)
    make_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    create_policy(
        client,
        {"field": "ssn", "classification": "SECRET", "masking": "partial",
         "allowed_roles": ["analyst"]},
    )
    post_view(client, "guest", [{"email": "a@b.c", "ssn": "123456", "id": 1}])
    first = make_cleanup_request(client, reason="first sweep")
    confirm_cleanup(client, first["id"])
    post_view(client, "analyst", [{"email": "b@b.c", "ssn": "654321", "id": 2}])
    make_cleanup_request(client, reason="second sweep")

    make_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": []},
        version=2,
    )
    # Disable the version-2 policy so its view masks nothing.
    policies = client.get(policies_path(version=2)).json()
    assert client.patch(
        f"{policies_path(version=2)}/{policies[0]['id']}", json={"enabled": False}
    ).status_code == 200
    post_view(client, "guest", [{"email": "c@b.c", "id": 3}], version=2)


def test_export_covers_every_version_in_ascending_order(client: TestClient) -> None:
    seed_dataset(client)

    body = get_export(client)

    assert body["dataset"] == "orders"
    assert [entry["version"] for entry in body["versions"]] == [1, 2]


def test_version_entries_carry_policies_counts_and_cleanup_requests(
    client: TestClient,
) -> None:
    seed_dataset(client)

    body = get_export(client)
    first, second = body["versions"]

    assert first["policies"] == [
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": [], "enabled": True},
        {"field": "ssn", "classification": "SECRET", "masking": "partial",
         "allowed_roles": ["analyst"], "enabled": True},
    ]
    # The confirmed cleanup removed the first view's two hits; the second
    # view's single hit (email; the analyst role is allowed on ssn) survives.
    assert first["hit_count"] == 1
    assert first["masked_count"] == 3
    assert first["view_count"] == 2
    assert [request["status"] for request in first["cleanup_requests"]] == [
        "confirmed",
        "pending",
    ]
    assert [request["reason"] for request in first["cleanup_requests"]] == [
        "first sweep",
        "second sweep",
    ]
    assert [request["id"] for request in first["cleanup_requests"]] == sorted(
        request["id"] for request in first["cleanup_requests"]
    )
    for request in first["cleanup_requests"]:
        assert set(request) == {"id", "reason", "status", "created_at"}

    assert second["policies"] == [
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": [], "enabled": False}
    ]
    assert second["hit_count"] == 0
    assert second["masked_count"] == 0
    assert second["view_count"] == 1
    assert second["cleanup_requests"] == []


def test_totals_sum_the_version_entries(client: TestClient) -> None:
    seed_dataset(client)

    body = get_export(client)

    assert body["totals"] == {
        "policy_count": 3,
        "hit_count": 1,
        "masked_count": 3,
        "view_count": 3,
        "cleanup_request_count": 2,
    }
    assert body["totals"]["policy_count"] == sum(
        len(entry["policies"]) for entry in body["versions"]
    )
    for key in ("hit_count", "masked_count", "view_count"):
        assert body["totals"][key] == sum(
            entry[key] for entry in body["versions"]
        )
    assert body["totals"]["cleanup_request_count"] == sum(
        len(entry["cleanup_requests"]) for entry in body["versions"]
    )


def test_hit_count_tracks_surviving_records_after_cleanup(
    client: TestClient,
) -> None:
    make_dataset(client)
    make_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    post_view(client, "guest", [{"email": "a@b.c", "id": 1}])
    post_view(client, "guest", [{"email": "b@b.c", "id": 2}])

    before = get_export(client)
    assert before["versions"][0]["hit_count"] == 2
    assert before["versions"][0]["masked_count"] == 2
    assert before["versions"][0]["view_count"] == 2

    request = make_cleanup_request(client)
    confirm_cleanup(client, request["id"])

    after = get_export(client)
    # The hits are gone; the access trail is never cleaned up.
    assert after["versions"][0]["hit_count"] == 0
    assert after["versions"][0]["masked_count"] == 2
    assert after["versions"][0]["view_count"] == 2
    assert after["totals"]["hit_count"] == 0
    assert after["totals"]["cleanup_request_count"] == 1


def test_export_is_scoped_to_the_named_dataset(client: TestClient) -> None:
    seed_dataset(client)
    make_dataset(client, "other")
    make_version(client, "other")

    other = get_export(client, "other")

    assert other["dataset"] == "other"
    assert [entry["version"] for entry in other["versions"]] == [1]
    assert other["versions"][0]["policies"] == []
    assert other["totals"] == {
        "policy_count": 0,
        "hit_count": 0,
        "masked_count": 0,
        "view_count": 0,
        "cleanup_request_count": 0,
    }


# --------------------------------------------------------------------------- #
# Deterministic wire format
# --------------------------------------------------------------------------- #


def test_response_is_compact_json_with_trailing_newline(client: TestClient) -> None:
    seed_dataset(client)

    response = client.get(export_path())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    text = response.text
    assert text.endswith("\n")
    assert not text[:-1].endswith("\n")
    # Compact separators: no insignificant whitespace anywhere.
    assert ", " not in text and ": " not in text
    # Booleans serialize lowercase.
    assert '"enabled":true' in text
    assert '"enabled":false' in text
    # The payload parses back to exactly the same document.
    assert json.loads(text) == response.json()


def test_top_level_and_nested_key_order_is_stable(client: TestClient) -> None:
    seed_dataset(client)

    text = client.get(export_path()).text

    assert text.startswith('{"dataset":"orders","versions":[')
    assert text.index('"versions"') < text.index('"totals"')
    first_entry = text[text.index('"versions":[') :]
    assert first_entry.index('"version"') < first_entry.index('"policies"')
    assert first_entry.index('"policies"') < first_entry.index('"hit_count"')
    assert first_entry.index('"hit_count"') < first_entry.index('"masked_count"')
    assert first_entry.index('"masked_count"') < first_entry.index('"view_count"')
    assert first_entry.index('"view_count"') < first_entry.index(
        '"cleanup_requests"'
    )
    totals = text[text.index('"totals"') :]
    assert totals.index('"policy_count"') < totals.index('"hit_count"')
    assert totals.index('"hit_count"') < totals.index('"masked_count"')
    assert totals.index('"masked_count"') < totals.index('"view_count"')
    assert totals.index('"view_count"') < totals.index('"cleanup_request_count"')


def test_repeated_reads_are_byte_identical(client: TestClient) -> None:
    seed_dataset(client)

    first = client.get(export_path()).text
    second = client.get(export_path()).text

    assert first == second


# --------------------------------------------------------------------------- #
# Read-only behaviour
# --------------------------------------------------------------------------- #


def test_export_does_not_change_any_privacy_state(client: TestClient) -> None:
    seed_dataset(client)

    audit_before = client.get(audit_path()).json()
    access_before = client.get(policies_path() + "/view/access-records").json()
    cleanup_before = client.get(audit_path() + "/cleanup-requests").json()
    policies_before = client.get(policies_path()).json()

    get_export(client)
    get_export(client)

    assert client.get(audit_path()).json() == audit_before
    assert client.get(policies_path() + "/view/access-records").json() == access_before
    assert client.get(audit_path() + "/cleanup-requests").json() == cleanup_before
    assert client.get(policies_path()).json() == policies_before


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


def test_unknown_dataset_returns_404(client: TestClient) -> None:
    response = client.get(export_path("ghost"))

    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}


def test_body_and_query_parameters_are_422(client: TestClient) -> None:
    seed_dataset(client)

    with_body = client.request("GET", export_path(), content=b"{}")
    with_query = client.get(export_path(), params={"version": 1})

    for response in (with_body, with_query):
        assert response.status_code == 422, response.text
        assert response.json()["error"] == "validation_error"
        assert set(response.json()) == {"error", "detail"}


def test_shape_errors_keep_404_precedence(client: TestClient) -> None:
    assert client.get(export_path("ghost"), params={"x": "1"}).status_code == 404
    assert (
        client.request("GET", export_path("ghost"), content=b"{}").status_code
        == 404
    )


def test_rejected_export_writes_nothing(client: TestClient) -> None:
    seed_dataset(client)
    before = client.get(export_path()).text

    client.request("GET", export_path(), content=b"{}")
    client.get(export_path(), params={"x": "1"})

    assert client.get(export_path()).text == before
