"""Tests for per-version quality rules and row evaluation."""

from __future__ import annotations

import math
from datetime import datetime

from fastapi.testclient import TestClient

from app import repository
from app.db import db_session


BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "amount", "type": "decimal", "nullable": True},
    {"name": "region", "type": "string", "nullable": True},
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


def rules_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/quality-rules"


def create_rule(client: TestClient, payload: dict, **path: object) -> dict:
    response = client.post(rules_path(**path), json=payload)  # type: ignore[arg-type]
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #


def test_create_not_null_rule_returns_stable_payload(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.post(
        rules_path(),
        json={"name": "id required", "kind": "not_null", "params": {"field": "id"}},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"id", "name", "kind", "params", "enabled", "created_at"}
    assert isinstance(body["id"], int)
    assert body["name"] == "id required"
    assert body["kind"] == "not_null"
    assert body["params"] == {"field": "id"}
    assert body["enabled"] is True
    datetime.fromisoformat(body["created_at"])


def test_create_numeric_range_and_unique_rules(client: TestClient) -> None:
    make_dataset_with_version(client)

    ranged = client.post(
        rules_path(),
        json={
            "name": "amount bounded",
            "kind": "numeric_range",
            "params": {"field": "amount", "min": 0, "max": 1000},
        },
    )
    combo = client.post(
        rules_path(),
        json={
            "name": "region per id",
            "kind": "unique",
            "params": {"fields": ["id", "region"]},
        },
    )

    assert ranged.status_code == 201, ranged.text
    assert combo.status_code == 201, combo.text
    assert ranged.json()["params"] == {"field": "amount", "min": 0, "max": 1000}
    assert combo.json()["params"] == {"fields": ["id", "region"]}


def test_params_default_to_empty_object(client: TestClient) -> None:
    make_dataset_with_version(client)
    # Missing params is accepted structurally; semantic validation then rejects
    # it because not_null requires a field.
    response = client.post(
        rules_path(), json={"name": "broken", "kind": "not_null"}
    )
    assert response.status_code == 422
    assert client.get(rules_path()).json() == []


def test_unknown_kind_is_rejected(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.post(
        rules_path(),
        json={"name": "x", "kind": "positive", "params": {"field": "id"}},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get(rules_path()).json() == []


def test_empty_rule_name_is_rejected(client: TestClient) -> None:
    make_dataset_with_version(client)
    for raw_name in ("", "   "):
        response = client.post(
            rules_path(),
            json={"name": raw_name, "kind": "not_null", "params": {"field": "id"}},
        )
        assert response.status_code == 422
        assert response.json()["error"] == "validation_error"
    assert client.get(rules_path()).json() == []


def test_missing_name_or_kind_is_rejected(client: TestClient) -> None:
    make_dataset_with_version(client)
    missing_name = client.post(rules_path(), json={"kind": "not_null"})
    missing_kind = client.post(rules_path(), json={"name": "r"})

    for response in (missing_name, missing_kind):
        assert response.status_code == 422
        assert response.json()["error"] == "validation_error"
    assert client.get(rules_path()).json() == []


def test_extra_body_fields_are_rejected(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.post(
        rules_path(),
        json={
            "name": "r",
            "kind": "not_null",
            "params": {"field": "id"},
            "enabled": False,
        },
    )
    assert response.status_code == 422
    assert client.get(rules_path()).json() == []


def test_unknown_dataset_version_and_field_return_404(client: TestClient) -> None:
    make_dataset_with_version(client)

    unknown_dataset = client.post(
        rules_path("ghost"),
        json={"name": "r", "kind": "not_null", "params": {"field": "id"}},
    )
    unknown_version = client.post(
        rules_path("orders", 9),
        json={"name": "r", "kind": "not_null", "params": {"field": "id"}},
    )
    unknown_field = client.post(
        rules_path(),
        json={"name": "r", "kind": "not_null", "params": {"field": "ghost"}},
    )

    assert unknown_dataset.status_code == 404
    assert unknown_version.status_code == 404
    assert unknown_field.status_code == 404
    for response in (unknown_dataset, unknown_version, unknown_field):
        assert response.json()["error"] == "not_found"
    assert client.get(rules_path()).json() == []


def test_numeric_range_param_validation(client: TestClient) -> None:
    make_dataset_with_version(client)

    def post(params: dict) -> int:
        return client.post(
            rules_path(),
            json={"name": f"range-{params}", "kind": "numeric_range", "params": params},
        ).status_code

    base = {"field": "amount"}
    assert post({**base, "min": 0}) == 422  # max missing
    assert post({**base, "max": 10}) == 422  # min missing
    assert post({**base, "min": "0", "max": 10}) == 422  # non-number
    assert post({**base, "min": 0, "max": True}) == 422  # bool, not a number
    assert post({**base, "min": None, "max": 10}) == 422
    assert post({**base, "min": 10, "max": 5}) == 422  # min > max
    assert post({"field": "ghost", "min": 0, "max": 10}) == 404
    assert client.get(rules_path()).json() == []


def test_infinite_numeric_bounds_are_rejected_at_repository_level(
    client: TestClient,
) -> None:
    # Non-finite floats cannot be expressed in strict JSON, so the finite check
    # is exercised directly against the repository.
    from app.errors import RequestInvalidError

    make_dataset_with_version(client)

    for value in (math.inf, -math.inf, math.nan):
        try:
            with db_session() as conn:
                repository.create_quality_rule(
                    conn,
                    "orders",
                    1,
                    f"r-{value}",
                    "numeric_range",
                    {"field": "amount", "min": value, "max": 10},
                )
        except RequestInvalidError:
            pass
        else:  # pragma: no cover
            raise AssertionError("non-finite bound was accepted")

    # All failed attempts rolled back; a valid rule can still be created.
    assert client.get(rules_path()).json() == []
    response = client.post(
        rules_path(),
        json={
            "name": "ok",
            "kind": "numeric_range",
            "params": {"field": "amount", "min": 0, "max": 10},
        },
    )
    assert response.status_code == 201


def test_unique_param_validation(client: TestClient) -> None:
    make_dataset_with_version(client)

    def post(params: dict) -> int:
        return client.post(
            rules_path(),
            json={"name": f"unique-{params}", "kind": "unique", "params": params},
        ).status_code

    assert post({"fields": []}) == 422
    assert post({}) == 422
    assert post({"fields": "id"}) == 422
    assert post({"fields": ["id", "id"]}) == 422  # duplicate field names
    assert post({"fields": ["id", ""]}) == 422
    assert post({"fields": ["id", 42]}) == 422
    assert post({"fields": ["id", "ghost"]}) == 404
    assert client.get(rules_path()).json() == []


def test_duplicate_rule_name_in_same_version_conflicts(client: TestClient) -> None:
    make_dataset_with_version(client)
    payload = {"name": "same", "kind": "not_null", "params": {"field": "id"}}
    first = client.post(rules_path(), json=payload)
    # Even a same-named rule of a different kind conflicts.
    second = client.post(
        rules_path(),
        json={
            "name": "same",
            "kind": "numeric_range",
            "params": {"field": "amount", "min": 0, "max": 9},
        },
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"] == "conflict"
    assert [r["name"] for r in client.get(rules_path()).json()] == ["same"]


def test_rule_name_can_repeat_across_versions_and_datasets(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    # Second version of the same dataset, single field.
    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "id", "type": "bigint", "nullable": False}]},
        ).status_code
        == 201
    )
    make_dataset_with_version(
        client,
        "customers",
        [{"name": "id", "type": "integer", "nullable": False}],
    )

    payload = {"name": "same", "kind": "not_null", "params": {"field": "id"}}
    assert client.post(rules_path("orders", 1), json=payload).status_code == 201
    assert client.post(rules_path("orders", 2), json=payload).status_code == 201
    assert client.post(rules_path("customers", 1), json=payload).status_code == 201


def test_invalid_rule_is_not_written(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.post(
        rules_path(),
        json={"name": "bad", "kind": "not_null", "params": {"field": "ghost"}},
    )
    assert response.status_code == 404
    assert client.get(rules_path()).json() == []


# --------------------------------------------------------------------------- #
# Listing and enable/disable
# --------------------------------------------------------------------------- #


def test_rules_are_listed_sorted_by_id(client: TestClient) -> None:
    make_dataset_with_version(client)
    for name in ("zeta", "alpha", "mid"):
        created = create_rule(
            client, {"name": name, "kind": "not_null", "params": {"field": "id"}}
        )
        assert created["name"] == name

    listed = client.get(rules_path()).json()
    assert [r["name"] for r in listed] == ["zeta", "alpha", "mid"]
    assert [r["id"] for r in listed] == sorted(r["id"] for r in listed)


def test_listing_rules_for_unknown_dataset_or_version_returns_404(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    assert client.get(rules_path("ghost")).status_code == 404
    assert client.get(rules_path("orders", 7)).status_code == 404


def test_patch_enables_and_disables_rule(client: TestClient) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )

    disabled = client.patch(f"{rules_path()}/{rule['id']}", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    body = disabled.json()
    assert body["id"] == rule["id"]
    assert body["enabled"] is False
    assert body["name"] == "r"
    assert body["kind"] == "not_null"

    assert client.get(rules_path()).json()[0]["enabled"] is False

    re_enabled = client.patch(f"{rules_path()}/{rule['id']}", json={"enabled": True})
    assert re_enabled.status_code == 200
    assert re_enabled.json()["enabled"] is True


def test_patch_rejects_anything_but_boolean_enabled(client: TestClient) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    url = f"{rules_path()}/{rule['id']}"

    assert client.patch(url, json={"enabled": "false"}).status_code == 422
    assert client.patch(url, json={"enabled": 0}).status_code == 422
    assert client.patch(url, json={"enabled": None}).status_code == 422
    assert client.patch(url, json={}).status_code == 422
    assert client.patch(url, json={"enabled": True, "name": "x"}).status_code == 422
    # State is untouched.
    assert client.get(rules_path()).json()[0]["enabled"] is True


def test_patch_unknown_rule_dataset_or_version_returns_404(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )

    assert (
        client.patch(
            f"{rules_path()}/{rule['id'] + 100}", json={"enabled": False}
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"{rules_path('orders', 9)}/{rule['id']}", json={"enabled": False}
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"{rules_path('ghost')}/{rule['id']}", json={"enabled": False}
        ).status_code
        == 404
    )
    assert client.get(rules_path()).json()[0]["enabled"] is True


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def test_evaluate_not_null_rule(client: TestClient) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client, {"name": "id required", "kind": "not_null", "params": {"field": "id"}}
    )

    response = client.post(
        f"{rules_path()}/evaluate",
        json={
            "rows": [
                {"id": 1},  # 0 passes
                {"id": None},  # 1 fails
                {"amount": 5},  # 2 fails: missing
                {"id": 0},  # 3 passes: zero is not null
                {"id": ""},  # 4 passes: empty string is not null
                {"id": False},  # 5 passes
            ]
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["results"] == [
        {
            "rule_id": rule["id"],
            "name": "id required",
            "passed": False,
            "violations": [1, 2],
        }
    ]


def test_evaluate_numeric_range_rule(client: TestClient) -> None:
    make_dataset_with_version(client)
    rule = create_rule(
        client,
        {
            "name": "amount bounded",
            "kind": "numeric_range",
            "params": {"field": "amount", "min": 0, "max": 10},
        },
    )

    response = client.post(
        f"{rules_path()}/evaluate",
        json={
            "rows": [
                {"amount": 0},  # 0 boundary passes
                {"amount": 10},  # 1 boundary passes
                {"amount": 5.5},  # 2 passes
                {"amount": -0.1},  # 3 below
                {"amount": 11},  # 4 above
                {"amount": None},  # 5 null
                {"amount": "4"},  # 6 non-number
                {"amount": True},  # 7 boolean
                {"region": "eu"},  # 8 missing
            ]
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["results"] == [
        {
            "rule_id": rule["id"],
            "name": "amount bounded",
            "passed": False,
            "violations": [3, 4, 5, 6, 7, 8],
        }
    ]


def test_evaluate_unique_rule_uses_field_combos_and_treats_missing_as_null(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    single = create_rule(
        client, {"name": "id unique", "kind": "unique", "params": {"fields": ["id"]}}
    )
    combo = create_rule(
        client,
        {
            "name": "id+region unique",
            "kind": "unique",
            "params": {"fields": ["id", "region"]},
        },
    )

    response = client.post(
        f"{rules_path()}/evaluate",
        json={
            "rows": [
                {"id": 1, "region": "eu"},  # 0 unique
                {"id": 1, "region": "us"},  # 1 differs by region
                {"id": 1, "region": "eu"},  # 2 duplicates 0
                {"id": 2, "region": None},  # 3
                {"region": "eu"},  # 4 missing id -> null combo
                {"id": None, "region": "eu"},  # 5 same null-id combo as 4
                {"id": 2, "region": None},  # 6 duplicates 3
            ]
        },
    )

    assert response.status_code == 200, response.text
    results = response.json()["results"]
    # Results are sorted by rule_id ascending.
    assert [r["rule_id"] for r in results] == [single["id"], combo["id"]]

    # For the single-field rule all rows sharing id 1 repeat, as do the rows
    # with id 2 and the rows whose id is missing/null; every member of each
    # duplicate group is reported.
    assert results[0] == {
        "rule_id": single["id"],
        "name": "id unique",
        "passed": False,
        "violations": [0, 1, 2, 3, 4, 5, 6],
    }
    assert results[1] == {
        "rule_id": combo["id"],
        "name": "id+region unique",
        "passed": False,
        "violations": [0, 2, 3, 4, 5, 6],
    }


def test_evaluate_types_do_not_collide(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "u", "kind": "unique", "params": {"fields": ["region"]}}
    )
    response = client.post(
        f"{rules_path()}/evaluate",
        json={"rows": [{"region": "1"}, {"region": 1}, {"region": True}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["passed"] is True
    assert response.json()["results"][0]["violations"] == []


def test_evaluate_only_executes_enabled_rules(client: TestClient) -> None:
    make_dataset_with_version(client)
    active = create_rule(
        client,
        {"name": "active", "kind": "not_null", "params": {"field": "amount"}},
    )
    paused = create_rule(
        client, {"name": "paused", "kind": "not_null", "params": {"field": "id"}}
    )
    assert (
        client.patch(f"{rules_path()}/{paused['id']}", json={"enabled": False}).status_code
        == 200
    )

    response = client.post(
        f"{rules_path()}/evaluate", json={"rows": [{"id": None, "amount": None}]}
    )

    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert [r["rule_id"] for r in results] == [active["id"]]
    assert results[0]["violations"] == [0]


def test_evaluate_empty_rows_passes_every_rule(client: TestClient) -> None:
    make_dataset_with_version(client)
    first = create_rule(
        client, {"name": "a", "kind": "not_null", "params": {"field": "id"}}
    )
    second = create_rule(
        client,
        {
            "name": "b",
            "kind": "numeric_range",
            "params": {"field": "amount", "min": 0, "max": 1},
        },
    )
    third = create_rule(
        client, {"name": "c", "kind": "unique", "params": {"fields": ["id"]}}
    )

    response = client.post(f"{rules_path()}/evaluate", json={"rows": []})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "dataset": "orders",
        "version": 1,
        "results": [
            {"rule_id": first["id"], "name": "a", "passed": True, "violations": []},
            {"rule_id": second["id"], "name": "b", "passed": True, "violations": []},
            {"rule_id": third["id"], "name": "c", "passed": True, "violations": []},
        ],
    }


def test_evaluate_without_rules_returns_empty_results(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.post(f"{rules_path()}/evaluate", json={"rows": [{"id": 1}]})
    assert response.status_code == 200
    assert response.json() == {"dataset": "orders", "version": 1, "results": []}


def test_evaluate_unknown_dataset_or_version_returns_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert (
        client.post(rules_path("ghost") + "/evaluate", json={"rows": []}).status_code
        == 404
    )
    assert (
        client.post(
            rules_path("orders", 42) + "/evaluate", json={"rows": []}
        ).status_code
        == 404
    )


def test_evaluate_requires_rows_list(client: TestClient) -> None:
    make_dataset_with_version(client)
    assert client.post(f"{rules_path()}/evaluate", json={}).status_code == 422
    assert client.post(
        f"{rules_path()}/evaluate", json={"rows": {"id": 1}}
    ).status_code == 422


def test_rules_are_scoped_to_their_version(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_rule(
        client, {"name": "r", "kind": "not_null", "params": {"field": "id"}}
    )
    assert (
        client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "id", "type": "bigint", "nullable": False}]},
        ).status_code
        == 201
    )

    assert client.get(rules_path("orders", 2)).json() == []
    evaluated = client.post(
        rules_path("orders", 2) + "/evaluate", json={"rows": [{}]}
    )
    assert evaluated.status_code == 200
    assert evaluated.json()["results"] == []


def test_error_responses_do_not_leak_internals(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = client.post(
        rules_path(),
        json={"name": "r", "kind": "numeric_range", "params": {"min": 9, "max": 1}},
    )
    assert response.status_code == 422
    text = response.text.lower()
    assert "traceback" not in text
    assert "sqlite" not in text
    assert "select" not in text
    assert "insert" not in text
