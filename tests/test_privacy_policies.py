"""Tests for per-version privacy policies and role-based masked views."""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient


BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "email", "type": "string", "nullable": True},
    {"name": "ssn", "type": "string", "nullable": True},
    {"name": "age", "type": "integer", "nullable": True},
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


def create_policy(client: TestClient, payload: dict, **path: object) -> dict:
    response = client.post(policies_path(**path), json=payload)  # type: ignore[arg-type]
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #


def test_create_policy_returns_stable_payload(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.post(
        policies_path(),
        json={
            "field": "email",
            "classification": "PII",
            "masking": "partial",
            "allowed_roles": ["analyst", "auditor"],
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {
        "id",
        "field",
        "classification",
        "masking",
        "allowed_roles",
        "enabled",
        "created_at",
    }
    assert isinstance(body["id"], int)
    assert body["field"] == "email"
    assert body["classification"] == "PII"
    assert body["masking"] == "partial"
    assert body["allowed_roles"] == ["analyst", "auditor"]
    assert body["enabled"] is True
    datetime.fromisoformat(body["created_at"])


def test_empty_allowed_roles_is_accepted(client: TestClient) -> None:
    make_dataset_with_version(client)
    body = create_policy(
        client,
        {
            "field": "ssn",
            "classification": "secret",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    assert body["allowed_roles"] == []


def test_duplicate_field_in_same_version_conflicts(client: TestClient) -> None:
    make_dataset_with_version(client)
    payload = {
        "field": "email",
        "classification": "PII",
        "masking": "redact",
        "allowed_roles": [],
    }
    first = client.post(policies_path(), json=payload)
    second = client.post(
        policies_path(),
        json={**payload, "classification": "other", "masking": "partial"},
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"] == "conflict"
    assert [p["field"] for p in client.get(policies_path()).json()] == ["email"]


def test_same_field_can_be_registered_across_versions(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "email", "type": "string", "nullable": True}]},
        ).status_code
        == 201
    )
    payload = {
        "field": "email",
        "classification": "PII",
        "masking": "redact",
        "allowed_roles": [],
    }
    assert client.post(policies_path("orders", 1), json=payload).status_code == 201
    assert client.post(policies_path("orders", 2), json=payload).status_code == 201


def test_unknown_dataset_version_and_field_return_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    payload = {
        "field": "email",
        "classification": "PII",
        "masking": "redact",
        "allowed_roles": [],
    }

    unknown_dataset = client.post(policies_path("ghost"), json=payload)
    unknown_version = client.post(policies_path("orders", 9), json=payload)
    unknown_field = client.post(policies_path(), json={**payload, "field": "ghost"})

    assert unknown_dataset.status_code == 404
    assert unknown_version.status_code == 404
    assert unknown_field.status_code == 404
    for response in (unknown_dataset, unknown_version, unknown_field):
        assert response.json()["error"] == "not_found"
    assert client.get(policies_path()).json() == []


def test_invalid_inputs_return_422_and_write_nothing(client: TestClient) -> None:
    make_dataset_with_version(client)
    base = {
        "field": "email",
        "classification": "PII",
        "masking": "redact",
        "allowed_roles": [],
    }

    def post(overrides: dict | None = None, payload: dict | None = None) -> int:
        body = payload if payload is not None else {**base, **(overrides or {})}
        return client.post(policies_path(), json=body).status_code

    assert post({"classification": ""}) == 422
    assert post({"classification": "   "}) == 422
    assert post({"field": "   "}) == 422
    assert post({"masking": "hide"}) == 422
    assert post({"masking": "REDACT"}) == 422
    assert post({"allowed_roles": ["a", "a"]}) == 422
    assert post({"allowed_roles": ["ok", ""]}) == 422
    assert post({"allowed_roles": ["ok", "  "]}) == 422
    assert post({"allowed_roles": ["a", 1]}) == 422
    assert post({"allowed_roles": "analyst"}) == 422
    assert post({"classification": 7}) == 422
    assert post({"field": 9}) == 422
    assert post(payload={k: v for k, v in base.items() if k != "field"}) == 422
    assert post(payload={k: v for k, v in base.items() if k != "classification"}) == 422
    assert post(payload={k: v for k, v in base.items() if k != "masking"}) == 422
    assert post(payload={k: v for k, v in base.items() if k != "allowed_roles"}) == 422
    assert post({"enabled": False}) == 422  # extra field
    assert client.get(policies_path()).json() == []


def test_whitespace_around_field_is_matched_to_existing_field(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    response = client.post(
        policies_path(),
        json={
            "field": "  email ",
            "classification": "PII",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["field"] == "email"


# --------------------------------------------------------------------------- #
# Listing and enable/disable
# --------------------------------------------------------------------------- #


def test_policies_are_listed_sorted_by_id(client: TestClient) -> None:
    make_dataset_with_version(client)
    first = create_policy(
        client,
        {"field": "email", "classification": "a", "masking": "redact",
         "allowed_roles": []},
    )
    second = create_policy(
        client,
        {"field": "ssn", "classification": "b", "masking": "partial",
         "allowed_roles": []},
    )

    listed = client.get(policies_path()).json()
    assert [p["id"] for p in listed] == [first["id"], second["id"]]
    assert [p["field"] for p in listed] == ["email", "ssn"]


def test_listing_policies_for_unknown_dataset_or_version_returns_404(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    assert client.get(policies_path("ghost")).status_code == 404
    assert client.get(policies_path("orders", 7)).status_code == 404


def test_patch_enables_and_disables_policy(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )

    disabled = client.patch(f"{policies_path()}/{policy['id']}", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    body = disabled.json()
    assert body["id"] == policy["id"]
    assert body["enabled"] is False
    assert body["field"] == "email"
    assert body["masking"] == "redact"

    assert client.get(policies_path()).json()[0]["enabled"] is False

    re_enabled = client.patch(
        f"{policies_path()}/{policy['id']}", json={"enabled": True}
    )
    assert re_enabled.status_code == 200
    assert re_enabled.json()["enabled"] is True


def test_patch_rejects_anything_but_boolean_enabled(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    url = f"{policies_path()}/{policy['id']}"

    assert client.patch(url, json={"enabled": "false"}).status_code == 422
    assert client.patch(url, json={"enabled": 0}).status_code == 422
    assert client.patch(url, json={"enabled": None}).status_code == 422
    assert client.patch(url, json={}).status_code == 422
    assert client.patch(url, json={"enabled": True, "masking": "x"}).status_code == 422
    assert client.get(policies_path()).json()[0]["enabled"] is True


def test_patch_unknown_policy_dataset_or_version_returns_404(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )

    assert (
        client.patch(
            f"{policies_path()}/{policy['id'] + 100}", json={"enabled": False}
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"{policies_path('orders', 9)}/{policy['id']}", json={"enabled": False}
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"{policies_path('ghost')}/{policy['id']}", json={"enabled": False}
        ).status_code
        == 404
    )
    assert client.get(policies_path()).json()[0]["enabled"] is True


# --------------------------------------------------------------------------- #
# Role-based masked view
# --------------------------------------------------------------------------- #


def post_view(client: TestClient, role: str, rows: list[dict], **path: object):
    return client.post(
        policies_path(**path) + "/view",  # type: ignore[arg-type]
        json={"role": role, "rows": rows},
    )


def test_redact_masks_every_non_null_value(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "ssn", "classification": "secret", "masking": "redact",
         "allowed_roles": ["auditor"]},
    )
    rows = [
        {"id": 1, "ssn": "123-45-6789"},
        {"id": 2, "ssn": 42},
        {"id": 3, "ssn": None},
        {"id": 4, "ssn": True},
    ]

    response = post_view(client, "guest", rows)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert [row["ssn"] for row in body["rows"]] == ["***", "***", None, "***"]
    # Uncovered fields and null values survive untouched.
    assert [row["id"] for row in body["rows"]] == [1, 2, 3, 4]


def test_partial_masks_long_strings_and_redacts_the_rest(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": ["analyst"]},
    )
    create_policy(
        client,
        {"field": "age", "classification": "sensitive", "masking": "partial",
         "allowed_roles": ["analyst"]},
    )
    rows = [
        {"email": "alice@example.com"},   # len > 4 -> first + last 2
        {"email": "12345"},                # len 5 -> "145"
        {"email": "1234"},                 # len 4 -> ***
        {"email": "abcd"},                 # len 4 -> ***
        {"email": None},                   # null stays
        {"email": 99},                     # non-string non-null -> ***
        {"email": True},                   # boolean -> ***
        {"age": 30},                       # non-string -> ***
        {},                                # field absent -> stays absent
    ]

    response = post_view(client, "guest", rows)
    assert response.status_code == 200, response.text
    masked = [row.get("email", "<absent>") for row in response.json()["rows"][:7]]
    assert masked == ["aom", "145", "***", "***", None, "***", "***"]
    assert response.json()["rows"][7]["age"] == "***"
    assert "email" not in response.json()["rows"][8]


def test_allowed_role_sees_raw_values(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": ["analyst", "auditor"]},
    )
    rows = [{"email": "alice@example.com"}, {"email": None}]

    for role in ("analyst", "auditor"):
        response = post_view(client, role, rows)
        assert response.status_code == 200
        assert [row["email"] for row in response.json()["rows"]] == [
            "alice@example.com",
            None,
        ]


def test_empty_allowed_roles_masks_every_role(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    response = post_view(client, "anyone-at-all", [{"email": "x@y.z"}])
    assert response.status_code == 200
    assert response.json()["rows"] == [{"email": "***"}]


def test_disabled_policy_does_not_mask(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": ["analyst"]},
    )
    assert (
        client.patch(f"{policies_path()}/{policy['id']}", json={"enabled": False}).status_code
        == 200
    )
    response = post_view(client, "guest", [{"email": "alice@example.com"}])
    assert response.status_code == 200
    assert response.json()["rows"] == [{"email": "alice@example.com"}]


def test_view_preserves_row_order_keys_and_returns_copy(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "ssn", "classification": "secret", "masking": "redact",
         "allowed_roles": []},
    )
    original = [
        {"id": 2, "ssn": "aaa", "note": "x"},
        {"id": 1, "ssn": None, "note": "y"},
        {"id": 3, "note": "z"},
    ]

    response = post_view(client, "guest", original)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["id"] for row in body["rows"]] == [2, 1, 3]
    assert [list(row) for row in body["rows"]] == [list(row) for row in original]
    assert body["rows"][0] == {"id": 2, "ssn": "***", "note": "x"}
    # The request-side objects are not mutated and the response is independent.
    assert original[0]["ssn"] == "aaa"
    body["rows"][0]["ssn"] = "tampered"
    again = post_view(client, "guest", original)
    assert again.json()["rows"][0]["ssn"] == "***"


def test_view_with_empty_rows(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    response = post_view(client, "guest", [])
    assert response.status_code == 200
    assert response.json() == {"dataset": "orders", "version": 1, "rows": []}


def test_view_without_policies_changes_nothing(client: TestClient) -> None:
    make_dataset_with_version(client)
    rows = [{"id": 1, "email": "a@b.c", "extra": [1, 2]}]
    response = post_view(client, "guest", rows)
    assert response.status_code == 200
    assert response.json()["rows"] == rows


def test_view_validation_errors(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert client.post(policies_path() + "/view", json={"rows": []}).status_code == 422
    assert (
        client.post(policies_path() + "/view", json={"role": "x"}).status_code == 422
    )
    assert (
        client.post(
            policies_path() + "/view", json={"role": "", "rows": []}
        ).status_code
        == 422
    )
    assert (
        client.post(
            policies_path() + "/view", json={"role": "  ", "rows": []}
        ).status_code
        == 422
    )
    assert (
        client.post(
            policies_path() + "/view", json={"role": 7, "rows": []}
        ).status_code
        == 422
    )
    assert (
        client.post(
            policies_path() + "/view", json={"role": "x", "rows": {}}
        ).status_code
        == 422
    )
    assert (
        client.post(
            policies_path() + "/view", json={"role": "x", "rows": ["not-object"]}
        ).status_code
        == 422
    )
    assert (
        client.post(
            policies_path() + "/view",
            json={"role": "x", "rows": [], "extra": 1},
        ).status_code
        == 422
    )


def test_view_unknown_dataset_or_version_returns_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert post_view(client, "x", [], dataset="ghost").status_code == 404  # type: ignore[arg-type]
    assert post_view(client, "x", [], dataset="orders", version=9).status_code == 404  # type: ignore[arg-type]


def test_error_responses_do_not_leak_internals(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.post(
        policies_path(),
        json={
            "field": "email",
            "classification": "PII",
            "masking": "redact",
            "allowed_roles": [],
            "unexpected": True,
        },
    )
    assert response.status_code == 422
    text = response.text.lower()
    assert "traceback" not in text
    assert "sqlite" not in text
    assert "select" not in text
    assert "insert" not in text
