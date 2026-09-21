"""Tests for per-version privacy policies and role-based masked views."""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient


BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "ssn", "type": "string", "nullable": True},
    {"name": "email", "type": "string", "nullable": True},
    {"name": "note", "type": "string", "nullable": True},
]


def make_dataset_with_version(
    client: TestClient, name: str = "users", fields: list[dict] | None = None
) -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions",
        json={"fields": fields if fields is not None else BASE_FIELDS},
    )
    assert response.status_code == 201, response.text


def policies_path(dataset: str = "users", version: int = 1) -> str:
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
            "field": "ssn",
            "classification": "pii",
            "masking": "redact",
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
    assert body["field"] == "ssn"
    assert body["classification"] == "pii"
    assert body["masking"] == "redact"
    assert body["allowed_roles"] == ["analyst", "auditor"]
    assert body["enabled"] is True
    datetime.fromisoformat(body["created_at"])


def test_create_partial_policy_with_empty_roles(client: TestClient) -> None:
    make_dataset_with_version(client)
    body = create_policy(
        client,
        {
            "field": "email",
            "classification": "confidential",
            "masking": "partial",
            "allowed_roles": [],
        },
    )
    assert body["masking"] == "partial"
    assert body["classification"] == "confidential"
    assert body["allowed_roles"] == []
    assert body["enabled"] is True


def test_duplicate_field_in_same_version_conflicts(client: TestClient) -> None:
    make_dataset_with_version(client)
    payload = {
        "field": "ssn",
        "classification": "pii",
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
    assert [p["field"] for p in client.get(policies_path()).json()] == ["ssn"]


def test_same_field_can_register_across_versions_and_datasets(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    assert (
        client.post(
            "/datasets/users/versions",
            json={"fields": [{"name": "ssn", "type": "string", "nullable": True}]},
        ).status_code
        == 201
    )
    make_dataset_with_version(
        client,
        "members",
        [{"name": "ssn", "type": "string", "nullable": True}],
    )

    payload = {
        "field": "ssn",
        "classification": "pii",
        "masking": "redact",
        "allowed_roles": [],
    }
    assert client.post(policies_path("users", 1), json=payload).status_code == 201
    assert client.post(policies_path("users", 2), json=payload).status_code == 201
    assert client.post(policies_path("members", 1), json=payload).status_code == 201


def test_unknown_dataset_version_and_field_return_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    payload = {
        "field": "ssn",
        "classification": "pii",
        "masking": "redact",
        "allowed_roles": [],
    }

    unknown_dataset = client.post(policies_path("ghost"), json=payload)
    unknown_version = client.post(policies_path("users", 9), json=payload)
    unknown_field = client.post(policies_path(), json={**payload, "field": "ghost"})

    assert unknown_dataset.status_code == 404
    assert unknown_version.status_code == 404
    assert unknown_field.status_code == 404
    for response in (unknown_dataset, unknown_version, unknown_field):
        assert response.json()["error"] == "not_found"
    assert client.get(policies_path()).json() == []


def test_invalid_payloads_are_rejected_and_not_written(client: TestClient) -> None:
    make_dataset_with_version(client)
    valid = {
        "field": "ssn",
        "classification": "pii",
        "masking": "redact",
        "allowed_roles": [],
    }

    def post(**overrides: object) -> int:
        payload = {**valid, **overrides}
        return client.post(policies_path(), json=payload).status_code

    # Missing required fields.
    for key in ("field", "classification", "masking", "allowed_roles"):
        partial = {k: v for k, v in valid.items() if k != key}
        assert client.post(policies_path(), json=partial).status_code == 422

    # Empty / whitespace classification.
    assert post(classification="") == 422
    assert post(classification="   ") == 422
    # Unsupported masking strategy.
    assert post(masking="hide") == 422
    assert post(masking=None) == 422
    # Wrong types.
    assert post(field=42) == 422
    assert post(classification=7) == 422
    assert post(allowed_roles="analyst") == 422
    assert post(allowed_roles=["analyst", 3]) == 422
    # Empty or duplicated roles.
    assert post(allowed_roles=[""]) == 422
    assert post(allowed_roles=["  "]) == 422
    assert post(allowed_roles=["a", "a"]) == 422
    # Unknown fields in the body.
    assert client.post(
        policies_path(), json={**valid, "enabled": False}
    ).status_code == 422

    assert client.get(policies_path()).json() == []


def test_malformed_json_returns_422(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.post(policies_path(), content="{not json", headers={
        "content-type": "application/json"
    })
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get(policies_path()).json() == []


# --------------------------------------------------------------------------- #
# Listing and enable/disable
# --------------------------------------------------------------------------- #


def test_policies_are_listed_sorted_by_id(client: TestClient) -> None:
    make_dataset_with_version(client)
    for field_name in ("ssn", "email", "note"):
        create_policy(
            client,
            {
                "field": field_name,
                "classification": "pii",
                "masking": "redact",
                "allowed_roles": [],
            },
        )

    listed = client.get(policies_path()).json()
    assert [p["field"] for p in listed] == ["ssn", "email", "note"]
    assert [p["id"] for p in listed] == sorted(p["id"] for p in listed)


def test_listing_policies_for_unknown_dataset_or_version_returns_404(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    assert client.get(policies_path("ghost")).status_code == 404
    assert client.get(policies_path("users", 7)).status_code == 404


def test_patch_enables_and_disables_policy(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {
            "field": "ssn",
            "classification": "pii",
            "masking": "redact",
            "allowed_roles": ["auditor"],
        },
    )

    disabled = client.patch(f"{policies_path()}/{policy['id']}", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    body = disabled.json()
    assert body["id"] == policy["id"]
    assert body["enabled"] is False
    # The rest of the policy is returned unchanged.
    assert body["field"] == "ssn"
    assert body["masking"] == "redact"
    assert body["allowed_roles"] == ["auditor"]

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
        {
            "field": "ssn",
            "classification": "pii",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    url = f"{policies_path()}/{policy['id']}"

    assert client.patch(url, json={"enabled": "false"}).status_code == 422
    assert client.patch(url, json={"enabled": 0}).status_code == 422
    assert client.patch(url, json={"enabled": None}).status_code == 422
    assert client.patch(url, json={}).status_code == 422
    assert client.patch(
        url, json={"enabled": True, "masking": "partial"}
    ).status_code == 422
    assert client.get(policies_path()).json()[0]["enabled"] is True


def test_patch_unknown_policy_dataset_or_version_returns_404(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {
            "field": "ssn",
            "classification": "pii",
            "masking": "redact",
            "allowed_roles": [],
        },
    )

    assert (
        client.patch(
            f"{policies_path()}/{policy['id'] + 100}", json={"enabled": False}
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"{policies_path('users', 9)}/{policy['id']}", json={"enabled": False}
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
# Masked view
# --------------------------------------------------------------------------- #


def view(client: TestClient, role: str, rows: list[dict], **path: object) -> dict:
    response = client.post(
        policies_path(**path) + "/view", json={"role": role, "rows": rows}  # type: ignore[arg-type]
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_redact_masks_every_non_null_value(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {
            "field": "ssn",
            "classification": "pii",
            "masking": "redact",
            "allowed_roles": [],
        },
    )

    rows = [
        {"ssn": "123-45-6789", "id": 1},
        {"ssn": 42, "id": 2},
        {"ssn": False, "id": 3},
        {"ssn": ["a"], "id": 4},
        {"ssn": None, "id": 5},
        {"id": 6},  # field absent
    ]
    body = view(client, "guest", rows)

    assert body["dataset"] == "users"
    assert body["version"] == 1
    assert [row["ssn"] if "ssn" in row else "<absent>" for row in body["rows"]] == [
        "***",
        "***",
        "***",
        "***",
        None,
        "<absent>",
    ]
    # Uncovered fields and nulls stay intact.
    assert [row["id"] for row in body["rows"]] == [1, 2, 3, 4, 5, 6]


def test_partial_masking_keeps_first_and_last_two_chars(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {
            "field": "ssn",
            "classification": "pii",
            "masking": "partial",
            "allowed_roles": [],
        },
    )

    rows = [
        {"ssn": "abcdef"},   # len 6 -> first + last 2
        {"ssn": "abcde"},    # len 5, boundary
        {"ssn": "abcd"},     # len 4 -> fully redacted
        {"ssn": "abc"},      # shorter
        {"ssn": ""},         # empty string is non-null
        {"ssn": 99},         # non-string
        {"ssn": 3.14},
        {"ssn": True},
        {"ssn": {"k": "v"}},
        {"ssn": None},
    ]
    body = view(client, "guest", rows)
    assert [row["ssn"] for row in body["rows"]] == [
        "a***ef",
        "a***de",
        "***",
        "***",
        "***",
        "***",
        "***",
        "***",
        "***",
        None,
    ]


def test_allowed_roles_see_unmasked_values(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {
            "field": "ssn",
            "classification": "pii",
            "masking": "partial",
            "allowed_roles": ["analyst", "auditor"],
        },
    )

    rows = [{"ssn": "secret-value"}, {"ssn": None}]
    assert view(client, "analyst", rows)["rows"] == rows
    assert view(client, "auditor", rows)["rows"] == rows
    # Role matching is exact; whitespace and prefixes do not grant access.
    assert view(client, " analyst", rows)["rows"][0]["ssn"] == "s***ue"
    assert view(client, "senior-analyst", rows)["rows"][0]["ssn"] == "s***ue"


def test_disabled_policy_does_not_mask(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(
        client,
        {
            "field": "ssn",
            "classification": "pii",
            "masking": "redact",
            "allowed_roles": ["analyst"],
        },
    )
    assert (
        client.patch(f"{policies_path()}/{policy['id']}", json={"enabled": False}).status_code
        == 200
    )

    rows = [{"ssn": "123-45-6789"}, {"ssn": None}]
    # Neither covered nor excluded roles are affected while the policy is off.
    assert view(client, "guest", rows)["rows"] == rows
    assert view(client, "analyst", rows)["rows"] == rows


def test_multiple_policies_apply_independently(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {
            "field": "ssn",
            "classification": "pii",
            "masking": "redact",
            "allowed_roles": ["analyst"],
        },
    )
    create_policy(
        client,
        {
            "field": "email",
            "classification": "pii",
            "masking": "partial",
            "allowed_roles": ["auditor"],
        },
    )

    rows = [
        {"id": 1, "ssn": "123456789", "email": "a@b.com", "note": "hi"},
        {"id": 2, "ssn": None, "email": None},
    ]

    analyst = view(client, "analyst", rows)["rows"]
    assert analyst[0]["ssn"] == "123456789"  # role allowed for ssn
    assert analyst[0]["email"] == "a***om"  # not allowed for email
    assert analyst[0]["note"] == "hi"  # no policy
    assert analyst[1] == {"id": 2, "ssn": None, "email": None}

    guest = view(client, "guest", rows)["rows"]
    assert guest[0]["ssn"] == "***"
    assert guest[0]["email"] == "a***om"
    assert guest[0]["note"] == "hi"


def test_view_preserves_row_order_key_order_and_does_not_mutate_input(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {
            "field": "ssn",
            "classification": "pii",
            "masking": "redact",
            "allowed_roles": [],
        },
    )

    rows = [
        {"zzz": 1, "ssn": "a", "aaa": 2},
        {"ssn": "b"},
        {"ssn": "c"},
    ]
    original = [dict(row) for row in rows]
    body = view(client, "guest", rows)

    assert [list(row) for row in body["rows"]] == [
        ["zzz", "ssn", "aaa"],
        ["ssn"],
        ["ssn"],
    ]
    assert [row["ssn"] for row in body["rows"]] == ["***", "***", "***"]
    # The caller's payload is left untouched.
    assert rows == original


def test_view_with_empty_rows(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {
            "field": "ssn",
            "classification": "pii",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    body = view(client, "guest", [])
    assert body == {"dataset": "users", "version": 1, "rows": []}


def test_view_validates_role_and_rows(client: TestClient) -> None:
    make_dataset_with_version(client)
    url = policies_path() + "/view"

    assert client.post(url, json={"rows": []}).status_code == 422  # no role
    assert client.post(url, json={"role": "guest"}).status_code == 422  # no rows
    assert client.post(url, json={"role": "", "rows": []}).status_code == 422
    assert client.post(url, json={"role": "  ", "rows": []}).status_code == 422
    assert client.post(url, json={"role": 9, "rows": []}).status_code == 422
    assert client.post(
        url, json={"role": "guest", "rows": {"ssn": "x"}}
    ).status_code == 422
    assert client.post(
        url, json={"role": "guest", "rows": [1, 2]}
    ).status_code == 422
    assert client.post(
        url, json={"role": "guest", "rows": [], "extra": 1}
    ).status_code == 422


def test_view_unknown_dataset_or_version_returns_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert (
        client.post(
            policies_path("ghost") + "/view", json={"role": "guest", "rows": []}
        ).status_code
        == 404
    )
    assert (
        client.post(
            policies_path("users", 42) + "/view",
            json={"role": "guest", "rows": []},
        ).status_code
        == 404
    )


def test_policies_are_scoped_to_their_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {
            "field": "ssn",
            "classification": "pii",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    assert (
        client.post(
            "/datasets/users/versions",
            json={"fields": [{"name": "ssn", "type": "string", "nullable": True}]},
        ).status_code
        == 201
    )

    assert client.get(policies_path("users", 2)).json() == []
    body = view(client, "guest", [{"ssn": "123456"}], dataset="users", version=2)
    assert body["rows"] == [{"ssn": "123456"}]


def test_error_responses_do_not_leak_internals(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.post(
        policies_path(),
        json={
            "field": "ssn",
            "classification": "pii",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    assert response.status_code == 201
    duplicate = client.post(
        policies_path(),
        json={
            "field": "ssn",
            "classification": "pii",
            "masking": "redact",
            "allowed_roles": [],
        },
    )
    assert duplicate.status_code == 409
    text = duplicate.text.lower()
    assert "traceback" not in text
    assert "sqlite" not in text
    assert "select" not in text
    assert "insert" not in text
