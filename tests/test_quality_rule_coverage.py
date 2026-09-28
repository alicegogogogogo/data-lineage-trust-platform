"""Tests for the read-only cross-version quality rule coverage check."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

COVERAGE_PATH = "/datasets/orders/quality-rule-coverage"

BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "amount", "type": "number", "nullable": True},
    {"name": "email", "type": "string", "nullable": True},
    {"name": "ssn", "type": "string", "nullable": True},
]

TOP_LEVEL_KEYS = ["dataset", "versions", "totals"]
VERSION_KEYS = ["version", "fields"]
FIELD_KEYS = ["field", "coverage", "kinds", "rule_ids"]
TOTAL_KEYS = [
    "enabled_count",
    "disabled_count",
    "unregistered_count",
    "version_count",
    "field_count",
]


def make_dataset(
    client: TestClient, name: str = "orders", fields: list[dict] | None = None
) -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    if fields is not None:
        response = client.post(
            f"/datasets/{name}/versions", json={"fields": fields}
        )
        assert response.status_code == 201, response.text


def add_version(client: TestClient, fields: list[dict], dataset: str = "orders") -> int:
    response = client.post(
        f"/datasets/{dataset}/versions", json={"fields": fields}
    )
    assert response.status_code == 201, response.text
    return response.json()["version"]


def rules_path(version: int, dataset: str = "orders") -> str:
    return f"/datasets/{dataset}/versions/{version}/quality-rules"


def create_rule(
    client: TestClient, version: int, payload: dict, dataset: str = "orders"
) -> dict:
    response = client.post(rules_path(version, dataset), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def disable_rule(client: TestClient, version: int, rule_id: int) -> None:
    response = client.patch(
        rules_path(version) + f"/{rule_id}", json={"enabled": False}
    )
    assert response.status_code == 200, response.text


def get_coverage(client: TestClient, dataset: str = "orders") -> dict:
    response = client.get(f"/datasets/{dataset}/quality-rule-coverage")
    assert response.status_code == 200, response.text
    return response.json()


def coverage_response(client: TestClient, dataset: str = "orders"):
    response = client.get(f"/datasets/{dataset}/quality-rule-coverage")
    assert response.status_code == 200, response.text
    return response


# --------------------------------------------------------------------------- #
# Empty state and wire format
# --------------------------------------------------------------------------- #


def test_coverage_without_versions_is_empty_not_an_error(client: TestClient) -> None:
    make_dataset(client)
    body = get_coverage(client)
    assert list(body) == TOP_LEVEL_KEYS
    assert body["dataset"] == "orders"
    assert body["versions"] == []
    assert body["totals"] == {
        "enabled_count": 0,
        "disabled_count": 0,
        "unregistered_count": 0,
        "version_count": 0,
        "field_count": 0,
    }


def test_coverage_is_get_only(client: TestClient) -> None:
    make_dataset(client)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, COVERAGE_PATH)
        assert response.status_code == 405, method


def test_coverage_wire_format_is_compact_with_fixed_key_order_and_newline(
    client: TestClient,
) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    create_rule(
        client, 1,
        {"name": "id_required", "kind": "not_null", "params": {"field": "id"}},
    )

    response = coverage_response(client)
    assert response.headers["content-type"].startswith("application/json")
    text = response.text
    # Compact whitespace: no separator spaces, and the only line break is the
    # single trailing newline.
    assert ": " not in text
    assert ", " not in text
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    assert "\n" not in text[:-1]
    assert "None" not in text

    # Key order at every level.
    assert list(response.json()) == TOP_LEVEL_KEYS
    positions = [text.index(f'"{key}"') for key in TOP_LEVEL_KEYS]
    assert positions == sorted(positions)
    body = response.json()
    assert list(body["totals"]) == TOTAL_KEYS
    version_entry = body["versions"][0]
    assert list(version_entry) == VERSION_KEYS
    for field_entry in version_entry["fields"]:
        assert list(field_entry) == FIELD_KEYS


# --------------------------------------------------------------------------- #
# Field coverage states and ordering
# --------------------------------------------------------------------------- #


def test_coverage_fields_sort_by_name_and_report_all_three_states(
    client: TestClient,
) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    enabled_rule = create_rule(
        client, 1,
        {"name": "id_required", "kind": "not_null", "params": {"field": "id"}},
    )
    disabled_rule = create_rule(
        client, 1,
        {
            "name": "amount_range",
            "kind": "numeric_range",
            "params": {"field": "amount", "min": 0, "max": 100},
        },
    )
    disable_rule(client, 1, disabled_rule["id"])

    fields = get_coverage(client)["versions"][0]["fields"]
    assert [entry["field"] for entry in fields] == ["amount", "email", "id", "ssn"]
    by_name = {entry["field"]: entry for entry in fields}

    disabled = by_name["amount"]
    assert disabled["coverage"] == "disabled"
    assert disabled["kinds"] == ["numeric_range"]
    assert disabled["rule_ids"] == [disabled_rule["id"]]

    for name in ("email", "ssn"):
        unregistered = by_name[name]
        assert unregistered["coverage"] == "unregistered"
        assert unregistered["kinds"] is None
        assert unregistered["rule_ids"] is None

    enabled = by_name["id"]
    assert enabled["coverage"] == "enabled"
    assert enabled["kinds"] == ["not_null"]
    assert enabled["rule_ids"] == [enabled_rule["id"]]


def test_coverage_unique_rule_references_every_listed_field(client: TestClient) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    unique_rule = create_rule(
        client, 1,
        {
            "name": "email_ssn_unique",
            "kind": "unique",
            "params": {"fields": ["email", "ssn"]},
        },
    )

    by_name = {
        entry["field"]: entry
        for entry in get_coverage(client)["versions"][0]["fields"]
    }
    for name in ("email", "ssn"):
        assert by_name[name]["coverage"] == "enabled"
        assert by_name[name]["kinds"] == ["unique"]
        assert by_name[name]["rule_ids"] == [unique_rule["id"]]
    assert by_name["id"]["coverage"] == "unregistered"
    assert by_name["amount"]["coverage"] == "unregistered"


def test_coverage_enabled_state_needs_an_enabled_rule(client: TestClient) -> None:
    # A field referenced by both an enabled and a disabled rule is enabled;
    # a field referenced only by disabled rules is disabled even when several
    # rules reference it.
    make_dataset(client, fields=BASE_FIELDS)
    enabled_on_id = create_rule(
        client, 1,
        {"name": "id_required", "kind": "not_null", "params": {"field": "id"}},
    )
    disabled_on_id = create_rule(
        client, 1,
        {
            "name": "id_range",
            "kind": "numeric_range",
            "params": {"field": "id", "min": 0, "max": 1000},
        },
    )
    disable_rule(client, 1, disabled_on_id["id"])
    disabled_amount_a = create_rule(
        client, 1,
        {"name": "amount_required", "kind": "not_null",
         "params": {"field": "amount"}},
    )
    disabled_amount_b = create_rule(
        client, 1,
        {
            "name": "amount_range",
            "kind": "numeric_range",
            "params": {"field": "amount", "min": 0, "max": 100},
        },
    )
    disable_rule(client, 1, disabled_amount_a["id"])
    disable_rule(client, 1, disabled_amount_b["id"])

    by_name = {
        entry["field"]: entry
        for entry in get_coverage(client)["versions"][0]["fields"]
    }
    id_field = by_name["id"]
    assert id_field["coverage"] == "enabled"
    assert id_field["kinds"] == ["not_null", "numeric_range"]
    assert id_field["rule_ids"] == sorted(
        [enabled_on_id["id"], disabled_on_id["id"]]
    )

    amount_field = by_name["amount"]
    assert amount_field["coverage"] == "disabled"
    assert amount_field["kinds"] == ["not_null", "numeric_range"]
    assert amount_field["rule_ids"] == sorted(
        [disabled_amount_a["id"], disabled_amount_b["id"]]
    )


def test_coverage_kinds_and_rule_ids_are_deduplicated_and_sorted(
    client: TestClient,
) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    # Two enabled rules of different kinds on 'amount', created out of
    # literal-sort order; one of them also lands on 'id' via a unique rule.
    range_rule = create_rule(
        client, 1,
        {
            "name": "amount_range",
            "kind": "numeric_range",
            "params": {"field": "amount", "min": 0, "max": 100},
        },
    )
    not_null_rule = create_rule(
        client, 1,
        {"name": "amount_required", "kind": "not_null",
         "params": {"field": "amount"}},
    )
    unique_rule = create_rule(
        client, 1,
        {
            "name": "amount_id_unique",
            "kind": "unique",
            "params": {"fields": ["amount", "id"]},
        },
    )

    by_name = {
        entry["field"]: entry
        for entry in get_coverage(client)["versions"][0]["fields"]
    }
    amount = by_name["amount"]
    assert amount["kinds"] == ["not_null", "numeric_range", "unique"]
    assert amount["rule_ids"] == sorted(
        [range_rule["id"], not_null_rule["id"], unique_rule["id"]]
    )
    assert by_name["id"]["rule_ids"] == [unique_rule["id"]]


def test_coverage_versions_sort_ascending_and_cover_every_version(
    client: TestClient,
) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    create_rule(
        client, 1,
        {"name": "id_required", "kind": "not_null", "params": {"field": "id"}},
    )
    add_version(client, [{"name": "email", "type": "string", "nullable": True}])
    add_version(client, [{"name": "id", "type": "integer", "nullable": False}])

    body = get_coverage(client)
    assert [entry["version"] for entry in body["versions"]] == [1, 2, 3]
    for entry in body["versions"]:
        assert list(entry) == VERSION_KEYS
    assert [f["field"] for f in body["versions"][1]["fields"]] == ["email"]
    assert body["versions"][1]["fields"][0]["coverage"] == "unregistered"
    assert body["versions"][2]["fields"][0]["coverage"] == "unregistered"
    assert body["versions"][0]["fields"][2]["field"] == "id"
    assert body["versions"][0]["fields"][2]["coverage"] == "enabled"


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #


def test_coverage_totals_equal_the_sum_of_the_version_entries(
    client: TestClient,
) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    create_rule(
        client, 1,
        {"name": "id_required", "kind": "not_null", "params": {"field": "id"}},
    )
    disabled_rule = create_rule(
        client, 1,
        {
            "name": "email_ssn_unique",
            "kind": "unique",
            "params": {"fields": ["email", "ssn"]},
        },
    )
    disable_rule(client, 1, disabled_rule["id"])

    add_version(
        client,
        [
            {"name": "phone", "type": "string", "nullable": True},
            {"name": "note", "type": "string", "nullable": True},
        ],
    )
    create_rule(
        client, 2,
        {"name": "phone_required", "kind": "not_null",
         "params": {"field": "phone"}},
    )

    body = get_coverage(client)
    versions = body["versions"]
    totals = body["totals"]
    assert list(totals) == TOTAL_KEYS
    assert totals["version_count"] == len(versions)
    assert totals["field_count"] == sum(len(v["fields"]) for v in versions)
    for state, key in (
        ("enabled", "enabled_count"),
        ("disabled", "disabled_count"),
        ("unregistered", "unregistered_count"),
    ):
        assert totals[key] == sum(
            1 for v in versions for f in v["fields"] if f["coverage"] == state
        )
    assert (
        totals["field_count"]
        == totals["enabled_count"]
        + totals["disabled_count"]
        + totals["unregistered_count"]
    )
    assert totals == {
        "enabled_count": 2,
        "disabled_count": 2,
        "unregistered_count": 2,
        "version_count": 2,
        "field_count": 6,
    }


# --------------------------------------------------------------------------- #
# Errors: 404 / 422, no writes
# --------------------------------------------------------------------------- #


def test_coverage_unknown_dataset_returns_404(client: TestClient) -> None:
    response = client.get("/datasets/ghost/quality-rule-coverage")
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "not_found"


def test_coverage_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset(client)
    before = coverage_response(client).text

    with_body = client.request("GET", COVERAGE_PATH, content=b"{}")
    with_blank_body = client.request("GET", COVERAGE_PATH, content=b"   ")
    with_query = client.get(COVERAGE_PATH, params={"limit": 1})
    assert with_body.status_code == 422
    assert with_blank_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, with_blank_body, with_query):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]

    # The rejections wrote nothing.
    assert coverage_response(client).text == before


def test_coverage_shape_errors_keep_404_precedence(client: TestClient) -> None:
    make_dataset(client)
    assert client.get(
        "/datasets/ghost/quality-rule-coverage", params={"x": "1"}
    ).status_code == 404
    assert (
        client.request(
            "GET", "/datasets/ghost/quality-rule-coverage", content=b"{}"
        ).status_code
        == 404
    )
    assert (
        client.request(
            "GET", "/datasets/ghost/quality-rule-coverage", content=b"  "
        ).status_code
        == 404
    )


def test_coverage_is_strictly_read_only(client: TestClient) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    enabled_rule = create_rule(
        client, 1,
        {"name": "id_required", "kind": "not_null", "params": {"field": "id"}},
    )
    disabled_rule = create_rule(
        client, 1,
        {
            "name": "email_ssn_unique",
            "kind": "unique",
            "params": {"fields": ["email", "ssn"]},
        },
    )
    disable_rule(client, 1, disabled_rule["id"])
    rules_before = client.get(rules_path(1)).json()
    first_text = coverage_response(client).text

    for _ in range(3):
        response = coverage_response(client)
        assert response.text == first_text
    assert client.get(rules_path(1)).json() == rules_before
    # Rules kept their enabled flags and definitions.
    rules_after = {rule["id"]: rule for rule in client.get(rules_path(1)).json()}
    assert rules_after[enabled_rule["id"]]["enabled"] is True
    assert rules_after[disabled_rule["id"]]["enabled"] is False


# --------------------------------------------------------------------------- #
# Persistence across restarts
# --------------------------------------------------------------------------- #


_CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

assert client.post("/datasets", json={"name": "orders"}).status_code == 201
assert client.post(
    "/datasets/orders/versions",
    json={"fields": [
        {"name": "email", "type": "string", "nullable": True},
        {"name": "ssn", "type": "string", "nullable": True},
        {"name": "id", "type": "integer", "nullable": False},
    ]},
).status_code == 201
enabled = client.post(
    "/datasets/orders/versions/1/quality-rules",
    json={"name": "id_required", "kind": "not_null", "params": {"field": "id"}},
)
assert enabled.status_code == 201, enabled.text
unique = client.post(
    "/datasets/orders/versions/1/quality-rules",
    json={
        "name": "email_ssn_unique",
        "kind": "unique",
        "params": {"fields": ["email", "ssn"]},
    },
)
assert unique.status_code == 201, unique.text
assert client.patch(
    f"/datasets/orders/versions/1/quality-rules/{unique.json()['id']}",
    json={"enabled": False},
).status_code == 200
print("created")
"""

_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

response = client.get("/datasets/orders/quality-rule-coverage")
assert response.status_code == 200, response.text
assert response.text.endswith("\\n")
body = response.json()
assert list(body) == ["dataset", "versions", "totals"]
assert body["dataset"] == "orders"
assert [v["version"] for v in body["versions"]] == [1]
entry = body["versions"][0]
assert list(entry) == ["version", "fields"]
assert [f["field"] for f in entry["fields"]] == ["email", "id", "ssn"]
email, id_field, ssn = entry["fields"]
assert email["coverage"] == "disabled"
assert email["kinds"] == ["unique"]
assert email["rule_ids"] == [2]
assert id_field["coverage"] == "enabled"
assert id_field["kinds"] == ["not_null"]
assert id_field["rule_ids"] == [1]
assert ssn["coverage"] == "disabled"
assert ssn["kinds"] == ["unique"]
assert ssn["rule_ids"] == [2]
assert body["totals"] == {
    "enabled_count": 1,
    "disabled_count": 2,
    "unregistered_count": 0,
    "version_count": 1,
    "field_count": 3,
}
again = client.get("/datasets/orders/quality-rule-coverage")
assert again.text == response.text
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


def test_coverage_survives_a_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "quality-rule-coverage.db"
    assert _run_script(db_path, _CREATE_SCRIPT) == "created"
    assert _run_script(db_path, _VERIFY_SCRIPT) == "verified"
