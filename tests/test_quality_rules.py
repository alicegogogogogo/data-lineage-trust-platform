"""Tests for version-scoped data-quality rules."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

RULES = "/datasets/orders/versions/1/quality-rules"


def make_version(
    client: TestClient,
    dataset: str = "orders",
    fields: list[dict] | None = None,
) -> None:
    assert client.post("/datasets", json={"name": dataset}).status_code == 201
    fields = fields or [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "amount", "type": "decimal", "nullable": True},
        {"name": "region", "type": "string", "nullable": True},
    ]
    response = client.post(f"/datasets/{dataset}/versions", json={"fields": fields})
    assert response.status_code == 201, response.text


def create_rule(client: TestClient, body: dict, path: str = RULES) -> dict:
    response = client.post(path, json=body)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #


def test_create_not_null_rule_shape(client: TestClient) -> None:
    make_version(client)
    body = create_rule(
        client, {"name": "id-present", "kind": "not_null", "parameters": {"field": "id"}}
    )

    assert set(body) == {"id", "name", "kind", "parameters", "enabled", "created_at"}
    assert isinstance(body["id"], int)
    assert body["name"] == "id-present"
    assert body["kind"] == "not_null"
    assert body["parameters"] == {"field": "id"}
    assert body["enabled"] is True
    datetime.fromisoformat(body["created_at"])


def test_enabled_defaults_to_true_for_every_kind(client: TestClient) -> None:
    make_version(client)
    for body in (
        {"name": "r1", "kind": "numeric_range", "parameters": {"field": "amount", "min": 0, "max": 100}},
        {"name": "r2", "kind": "unique", "parameters": {"fields": ["id", "region"]}},
    ):
        assert create_rule(client, body)["enabled"] is True


def test_rule_name_may_repeat_across_versions(client: TestClient) -> None:
    make_version(client)
    client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "bigint", "nullable": False}]},
    )
    body = {"name": "same-name", "kind": "not_null", "parameters": {"field": "id"}}

    first = client.post(RULES, json=body)
    second = client.post("/datasets/orders/versions/2/quality-rules", json=body)

    assert first.status_code == 201
    assert second.status_code == 201


def test_duplicate_rule_name_within_version_is_conflict(client: TestClient) -> None:
    make_version(client)
    body = {"name": "id-present", "kind": "not_null", "parameters": {"field": "id"}}
    assert client.post(RULES, json=body).status_code == 201

    duplicate = client.post(
        RULES,
        json={"name": "id-present", "kind": "unique", "parameters": {"fields": ["id"]}},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"] == "conflict"

    rules = client.get(RULES).json()
    assert len(rules) == 1


def test_empty_rule_name_is_rejected(client: TestClient) -> None:
    make_version(client)
    response = client.post(
        RULES,
        json={"name": "  ", "kind": "not_null", "parameters": {"field": "id"}},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get(RULES).json() == []


def test_unknown_kind_is_rejected(client: TestClient) -> None:
    make_version(client)
    response = client.post(
        RULES,
        json={"name": "bad", "kind": "regex", "parameters": {}},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get(RULES).json() == []


def test_missing_required_body_fields_are_rejected(client: TestClient) -> None:
    make_version(client)
    response = client.post(RULES, json={"kind": "not_null"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert "name" in body["detail"]


# --------------------------------------------------------------------------- #
# Parameter validation and 404s
# --------------------------------------------------------------------------- #


def test_not_null_rule_requires_existing_field(client: TestClient) -> None:
    make_version(client)
    response = client.post(
        RULES,
        json={"name": "ghost-field", "kind": "not_null", "parameters": {"field": "ghost"}},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert client.get(RULES).json() == []


def test_numeric_range_requires_existing_field(client: TestClient) -> None:
    make_version(client)
    response = client.post(
        RULES,
        json={
            "name": "r",
            "kind": "numeric_range",
            "parameters": {"field": "ghost", "min": 0, "max": 100},
        },
    )
    assert response.status_code == 404
    assert client.get(RULES).json() == []


def test_numeric_range_min_max_validation(client: TestClient) -> None:
    make_version(client)

    def post(parameters: dict) -> int:
        return client.post(
            RULES,
            json={"name": "range-" + str(parameters), "kind": "numeric_range", "parameters": parameters},
        ).status_code

    assert post({"field": "amount", "min": 100, "max": 0}) == 422
    assert post({"field": "amount", "min": "0", "max": 100}) == 422
    assert post({"field": "amount", "min": True, "max": 100}) == 422
    # Equal bounds are allowed (min <= max).
    ok = client.post(
        RULES,
        json={
            "name": "exact",
            "kind": "numeric_range",
            "parameters": {"field": "amount", "min": 5, "max": 5},
        },
    )
    assert ok.status_code == 201, ok.text
    # Valid, unequal bounds are accepted.
    valid = client.post(
        RULES,
        json={
            "name": "wide",
            "kind": "numeric_range",
            "parameters": {"field": "amount", "min": 0, "max": 100},
        },
    )
    assert valid.status_code == 201, valid.text
    # Missing min/max.
    missing = client.post(
        RULES,
        json={"name": "missing-bounds", "kind": "numeric_range", "parameters": {"field": "amount"}},
    )
    assert missing.status_code == 422


def test_numeric_range_rejects_non_finite_bounds(client: TestClient) -> None:
    make_version(client)
    # Python's JSON stack emits/accepts NaN and Infinity tokens; either must be 422.
    for raw_body in (
        '{"name":"nan","kind":"numeric_range","parameters":{"field":"amount","min":0,"max":NaN}}',
        '{"name":"inf","kind":"numeric_range","parameters":{"field":"amount","min":-Infinity,"max":1}}',
    ):
        response = client.post(
            RULES, content=raw_body, headers={"content-type": "application/json"}
        )
        assert response.status_code == 422, response.text
    assert client.get(RULES).json() == []


def test_not_null_without_field_parameter_is_422(client: TestClient) -> None:
    make_version(client)
    response = client.post(
        RULES, json={"name": "r", "kind": "not_null", "parameters": {}}
    )
    assert response.status_code == 422
    assert client.get(RULES).json() == []


def test_unique_requires_non_empty_distinct_existing_fields(client: TestClient) -> None:
    make_version(client)
    counter = 0

    def post(parameters: dict, *, valid: bool = False) -> int:
        nonlocal counter
        counter += 1
        return client.post(
            RULES,
            json={"name": f"u-{counter}" if valid else "u", "kind": "unique", "parameters": parameters},
        ).status_code

    assert post({"fields": []}) == 422
    assert post({}) == 422
    assert post({"fields": ["id", "id"]}) == 422
    assert post({"fields": ["id", "ghost"]}) == 404
    assert post({"fields": ["id"]}, valid=True) == 201
    assert post({"fields": ["id", "region"]}, valid=True) == 201


def test_unknown_dataset_and_version_return_404(client: TestClient) -> None:
    body = {"name": "r", "kind": "not_null", "parameters": {"field": "id"}}

    assert client.post(
        "/datasets/ghost/versions/1/quality-rules", json=body
    ).status_code == 404
    assert client.get(
        "/datasets/ghost/versions/1/quality-rules"
    ).status_code == 404
    assert client.post(
        "/datasets/ghost/versions/1/quality-rules/evaluate", json={"rows": []}
    ).status_code == 404

    make_version(client)
    assert client.get(
        "/datasets/orders/versions/99/quality-rules"
    ).status_code == 404
    assert client.post(
        "/datasets/orders/versions/99/quality-rules", json=body
    ).status_code == 404
    assert client.post(
        "/datasets/orders/versions/99/quality-rules/evaluate", json={"rows": []}
    ).status_code == 404


# --------------------------------------------------------------------------- #
# Listing and PATCH
# --------------------------------------------------------------------------- #


def test_list_rules_sorted_by_id(client: TestClient) -> None:
    make_version(client)
    bodies = [
        {"name": "zeta", "kind": "not_null", "parameters": {"field": "id"}},
        {"name": "alpha", "kind": "not_null", "parameters": {"field": "amount"}},
        {"name": "mid", "kind": "unique", "parameters": {"fields": ["region"]}},
    ]
    for body in bodies:
        create_rule(client, body)

    rules = client.get(RULES).json()
    assert [r["name"] for r in rules] == ["zeta", "alpha", "mid"]
    assert [r["id"] for r in rules] == sorted(r["id"] for r in rules)


def test_patch_enables_and_disables_rule(client: TestClient) -> None:
    make_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "parameters": {"field": "id"}}
    )
    path = f"{RULES}/{rule['id']}"

    disabled = client.patch(path, json={"enabled": False})
    assert disabled.status_code == 200
    body = disabled.json()
    assert body["enabled"] is False
    assert body["id"] == rule["id"]
    assert body["name"] == "r"

    assert client.patch(path, json={"enabled": True}).json()["enabled"] is True
    # State persists across requests.
    assert client.get(RULES).json()[0]["enabled"] is True


def test_patch_unknown_rule_returns_404(client: TestClient) -> None:
    make_version(client)
    assert client.patch(
        f"{RULES}/999", json={"enabled": False}
    ).status_code == 404


def test_rule_id_is_scoped_to_its_version(client: TestClient) -> None:
    make_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "parameters": {"field": "id"}}
    )
    client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "bigint", "nullable": False}]},
    )
    # The rule belongs to version 1 and must not be addressable via version 2.
    other_version = f"/datasets/orders/versions/2/quality-rules/{rule['id']}"
    assert client.patch(other_version, json={"enabled": False}).status_code == 404
    # ... and version 1's rule remains enabled.
    assert client.get(RULES).json()[0]["enabled"] is True


def test_patch_requires_boolean_enabled(client: TestClient) -> None:
    make_version(client)
    rule = create_rule(
        client, {"name": "r", "kind": "not_null", "parameters": {"field": "id"}}
    )
    path = f"{RULES}/{rule['id']}"

    assert client.patch(path, json={"enabled": "yes"}).status_code == 422
    assert client.patch(path, json={}).status_code == 422
    # Rule untouched.
    assert client.get(RULES).json()[0]["enabled"] is True


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def evaluate(client: TestClient, rows: list[dict]) -> dict:
    response = client.post(f"{RULES}/evaluate", json={"rows": rows})
    assert response.status_code == 200, response.text
    return response.json()


def test_evaluate_not_null(client: TestClient) -> None:
    make_version(client)
    create_rule(
        client, {"name": "id-present", "kind": "not_null", "parameters": {"field": "id"}}
    )

    body = evaluate(
        client,
        [
            {"id": 1},
            {"id": None},
            {"amount": 5},          # missing id
            {"id": 4, "region": "eu"},
        ],
    )
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["results"] == [
        {"rule_id": body["results"][0]["rule_id"], "name": "id-present", "passed": False, "violations": [1, 2]}
    ]


def test_evaluate_numeric_range(client: TestClient) -> None:
    make_version(client)
    create_rule(
        client,
        {
            "name": "amount-range",
            "kind": "numeric_range",
            "parameters": {"field": "amount", "min": 0, "max": 100},
        },
    )

    body = evaluate(
        client,
        [
            {"amount": 0},          # 0: in range
            {"amount": 100},        # 1: in range (inclusive)
            {"amount": 100.5},      # 2: above max
            {"amount": -1},         # 3: below min
            {"amount": None},       # 4: null
            {},                     # 5: missing
            {"amount": "50"},       # 6: non-numeric string
            {"amount": True},       # 7: boolean, not a number
            {"amount": 50},         # 8: in range
        ],
    )
    assert body["results"][0]["passed"] is False
    assert body["results"][0]["violations"] == [2, 3, 4, 5, 6, 7]


def test_evaluate_unique_on_field_combination(client: TestClient) -> None:
    make_version(client)
    create_rule(
        client,
        {"name": "uniq", "kind": "unique", "parameters": {"fields": ["id", "region"]}},
    )

    body = evaluate(
        client,
        [
            {"id": 1, "region": "eu"},   # 0: first
            {"id": 1, "region": "us"},   # 1: distinct combination
            {"id": 1, "region": "eu"},   # 2: duplicate of 0
            {"region": "eu"},            # 3: missing id -> null
            {"id": None, "region": "eu"},  # 4: null id == missing id
            {"id": 2, "region": "eu"},   # 5: unique
        ],
    )
    assert body["results"][0]["violations"] == [2, 4]


def test_evaluate_single_field_unique(client: TestClient) -> None:
    make_version(client)
    create_rule(
        client, {"name": "uniq-id", "kind": "unique", "parameters": {"fields": ["id"]}}
    )
    body = evaluate(
        client,
        [{"id": 1}, {"id": 2}, {"id": 1}, {"id": 2}],
    )
    assert body["results"][0]["violations"] == [2, 3]


def test_empty_rows_pass_every_rule(client: TestClient) -> None:
    make_version(client)
    create_rule(
        client, {"name": "nn", "kind": "not_null", "parameters": {"field": "id"}}
    )
    create_rule(
        client,
        {"name": "nr", "kind": "numeric_range", "parameters": {"field": "amount", "min": 0, "max": 1}},
    )
    create_rule(
        client, {"name": "uq", "kind": "unique", "parameters": {"fields": ["id"]}}
    )

    body = evaluate(client, [])
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert [r["rule_id"] for r in body["results"]] == sorted(
        r["rule_id"] for r in body["results"]
    )
    assert all(r["passed"] is True for r in body["results"])
    assert all(r["violations"] == [] for r in body["results"])


def test_only_enabled_rules_are_evaluated(client: TestClient) -> None:
    make_version(client)
    strict = create_rule(
        client, {"name": "strict", "kind": "not_null", "parameters": {"field": "id"}}
    )
    create_rule(
        client, {"name": "lenient", "kind": "not_null", "parameters": {"field": "amount"}}
    )
    client.patch(f"{RULES}/{strict['id']}", json={"enabled": False})

    body = evaluate(client, [{"amount": 1}])  # id missing, amount present
    assert [r["name"] for r in body["results"]] == ["lenient"]
    assert body["results"][0]["passed"] is True

    # Re-enable: both run, ordered by rule_id.
    client.patch(f"{RULES}/{strict['id']}", json={"enabled": True})
    body = evaluate(client, [{"amount": 1}])
    assert [r["name"] for r in body["results"]] == ["strict", "lenient"]
    by_name = {r["name"]: r for r in body["results"]}
    assert by_name["strict"]["passed"] is False
    assert by_name["strict"]["violations"] == [0]


def test_evaluate_requires_rows_field(client: TestClient) -> None:
    make_version(client)
    response = client.post(f"{RULES}/evaluate", json={})
    assert response.status_code == 422
    assert "rows" in response.json()["detail"]


def test_errors_do_not_leak_internals(client: TestClient) -> None:
    make_version(client)
    response = client.post(
        RULES,
        json={"name": "r", "kind": "not_null", "parameters": {"field": "ghost"}},
    )
    assert response.status_code == 404
    assert "traceback" not in response.text.lower()
    assert "sqlite" not in response.text.lower()


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

assert client.post("/datasets", json={"name": "orders"}).status_code == 201
assert client.post(
    "/datasets/orders/versions",
    json={"fields": [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "amount", "type": "decimal", "nullable": True},
    ]},
).status_code == 201

assert client.post(
    "/datasets/orders/versions/1/quality-rules",
    json={"name": "id-present", "kind": "not_null", "parameters": {"field": "id"}},
).status_code == 201
range_response = client.post(
    "/datasets/orders/versions/1/quality-rules",
    json={"name": "amount-range", "kind": "numeric_range",
          "parameters": {"field": "amount", "min": 0, "max": 100}},
)
assert range_response.status_code == 201
rule_id = range_response.json()["id"]
assert client.patch(
    f"/datasets/orders/versions/1/quality-rules/{rule_id}",
    json={"enabled": False},
).status_code == 200
print("created")
"""

VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

rules = client.get("/datasets/orders/versions/1/quality-rules")
assert rules.status_code == 200, rules.text
by_name = {r["name"]: r for r in rules.json()}
assert set(by_name) == {"id-present", "amount-range"}
assert by_name["id-present"]["enabled"] is True
assert by_name["amount-range"]["enabled"] is False
assert by_name["amount-range"]["parameters"] == {"field": "amount", "min": 0, "max": 100}

# Only the enabled not_null rule runs; the disabled range rule stays silent.
evaluated = client.post(
    "/datasets/orders/versions/1/quality-rules/evaluate",
    json={"rows": [{"id": 1, "amount": 999}, {"amount": 5}]},
)
assert evaluated.status_code == 200, evaluated.text
body = evaluated.json()
assert [r["name"] for r in body["results"]] == ["id-present"]
assert body["results"][0]["violations"] == [1]
print("verified")
"""


def _run(db_path: Path, script: str) -> str:
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


def test_rules_and_enabled_state_survive_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "quality-rules.db"
    assert _run(db_path, CREATE_SCRIPT) == "created"
    assert _run(db_path, VERIFY_SCRIPT) == "verified"
