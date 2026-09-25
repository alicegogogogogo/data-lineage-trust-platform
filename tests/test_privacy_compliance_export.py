"""Tests for the read-only cross-version privacy compliance export."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXPORT_PATH = "/datasets/orders/privacy-compliance-export"

BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "email", "type": "string", "nullable": True},
    {"name": "ssn", "type": "string", "nullable": True},
]

TOP_LEVEL_KEYS = ["dataset", "versions", "totals"]
VERSION_KEYS = [
    "version",
    "policies",
    "hit_count",
    "masked_count",
    "view_count",
    "cleanup_requests",
]
POLICY_KEYS = [
    "id",
    "field",
    "classification",
    "masking",
    "allowed_roles",
    "enabled",
]
CLEANUP_KEYS = ["id", "reason", "status", "created_at"]
TOTAL_KEYS = [
    "policy_count",
    "hit_count",
    "masked_count",
    "view_count",
    "cleanup_request_count",
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


def policies_path(version: int, dataset: str = "orders") -> str:
    return f"/datasets/{dataset}/versions/{version}/privacy-policies"


def create_policy(
    client: TestClient, version: int, payload: dict, dataset: str = "orders"
) -> dict:
    response = client.post(policies_path(version, dataset), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def post_view(
    client: TestClient, version: int, role: str, rows: list[dict],
    dataset: str = "orders",
):
    return client.post(
        policies_path(version, dataset) + "/view",
        json={"role": role, "rows": rows},
    )


def cleanup_path(version: int, dataset: str = "orders") -> str:
    return (
        f"/datasets/{dataset}/versions/{version}"
        "/privacy-policies/view/audit-records/cleanup-requests"
    )


def audit_path(version: int, dataset: str = "orders") -> str:
    return policies_path(version, dataset) + "/view/audit-records"


def access_path(version: int, dataset: str = "orders") -> str:
    return policies_path(version, dataset) + "/view/access-records"


def get_export(client: TestClient, dataset: str = "orders") -> dict:
    response = client.get(f"/datasets/{dataset}/privacy-compliance-export")
    assert response.status_code == 200, response.text
    return response.json()


def export_response(client: TestClient, dataset: str = "orders"):
    response = client.get(f"/datasets/{dataset}/privacy-compliance-export")
    assert response.status_code == 200, response.text
    return response


# --------------------------------------------------------------------------- #
# Empty state and wire format
# --------------------------------------------------------------------------- #


def test_export_without_versions_is_empty_not_an_error(client: TestClient) -> None:
    make_dataset(client)
    body = get_export(client)
    assert set(body) == set(TOP_LEVEL_KEYS)
    assert body["dataset"] == "orders"
    assert body["versions"] == []
    assert body["totals"] == {
        "policy_count": 0,
        "hit_count": 0,
        "masked_count": 0,
        "view_count": 0,
        "cleanup_request_count": 0,
    }


def test_export_is_get_only(client: TestClient) -> None:
    make_dataset(client)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, EXPORT_PATH)
        assert response.status_code == 405, method


def test_export_wire_format_is_compact_with_fixed_key_order_and_newline(
    client: TestClient,
) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    create_policy(
        client, 1,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": ["analyst"]},
    )
    assert post_view(client, 1, "guest", [{"email": "a@b.c"}]).status_code == 200

    response = export_response(client)
    assert response.headers["content-type"].startswith("application/json")
    text = response.text
    # Compact whitespace: no separator spaces, and the only line break is the
    # single trailing newline.
    assert ": " not in text
    assert ", " not in text
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    assert "\n" not in text[:-1]
    # Lower-case booleans, no scientific notation for the integer counters.
    assert '"enabled":true' in text
    assert "True" not in text
    assert "e+" not in text.lower().replace("email", "")

    # Top-level key order.
    positions = [text.index(f'"{key}"') for key in TOP_LEVEL_KEYS]
    assert positions == sorted(positions)
    body = response.json()
    assert list(body) == TOP_LEVEL_KEYS
    assert list(body["totals"]) == TOTAL_KEYS
    version_entry = body["versions"][0]
    assert list(version_entry) == VERSION_KEYS
    assert list(version_entry["policies"][0]) == POLICY_KEYS


# --------------------------------------------------------------------------- #
# Versions, policies and ordering
# --------------------------------------------------------------------------- #


def test_export_versions_sort_ascending_and_cover_every_version(
    client: TestClient,
) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    create_policy(
        client, 1,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    add_version(client, [{"name": "email", "type": "string", "nullable": True}])
    create_policy(
        client, 2,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": ["auditor"]},
    )
    add_version(client, [{"name": "id", "type": "integer", "nullable": False}])

    body = get_export(client)
    assert [entry["version"] for entry in body["versions"]] == [1, 2, 3]
    for entry in body["versions"]:
        assert set(entry) == set(VERSION_KEYS)
        assert isinstance(entry["hit_count"], int)
        assert isinstance(entry["masked_count"], int)
        assert isinstance(entry["view_count"], int)

    first = body["versions"][0]
    assert first["policies"] == [
        {
            "id": first["policies"][0]["id"],
            "field": "email",
            "classification": "PII",
            "masking": "redact",
            "allowed_roles": [],
            "enabled": True,
        }
    ]
    second = body["versions"][1]
    assert second["policies"][0]["masking"] == "partial"
    assert second["policies"][0]["allowed_roles"] == ["auditor"]
    assert body["versions"][2]["policies"] == []


def test_export_policies_sort_by_id_and_keep_enabled_state(
    client: TestClient,
) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    email_policy = create_policy(
        client, 1,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    ssn_policy = create_policy(
        client, 1,
        {"field": "ssn", "classification": "secret", "masking": "partial",
         "allowed_roles": ["analyst", "auditor"]},
    )
    disabled = client.patch(
        policies_path(1) + f"/{email_policy['id']}", json={"enabled": False}
    )
    assert disabled.status_code == 200

    policies = get_export(client)["versions"][0]["policies"]
    assert [policy["id"] for policy in policies] == sorted(
        policy["id"] for policy in policies
    )
    assert [policy["id"] for policy in policies] == [
        email_policy["id"], ssn_policy["id"]
    ]
    by_id = {policy["id"]: policy for policy in policies}
    assert by_id[email_policy["id"]]["enabled"] is False
    assert by_id[ssn_policy["id"]]["enabled"] is True
    assert by_id[ssn_policy["id"]]["allowed_roles"] == ["analyst", "auditor"]
    for policy in policies:
        assert set(policy) == set(POLICY_KEYS)


# --------------------------------------------------------------------------- #
# Counters, including the post-cleanup surviving data
# --------------------------------------------------------------------------- #


def test_export_counts_reflect_every_view(client: TestClient) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    create_policy(
        client, 1,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    # Three masked values in the first view, one in the second, none in the
    # third; every successful view adds one access record.
    assert post_view(
        client, 1, "guest",
        [{"email": "a@b.c"}, {"email": "d@e.f"}, {"email": "g@h.i"}],
    ).status_code == 200
    assert post_view(client, 1, "guest", [{"email": "x@y.z"}]).status_code == 200
    assert post_view(client, 1, "analyst", []).status_code == 200

    entry = get_export(client)["versions"][0]
    assert entry["hit_count"] == 4
    assert entry["masked_count"] == 4
    assert entry["view_count"] == 3


def test_export_counts_use_data_surviving_cleanup(client: TestClient) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    create_policy(
        client, 1,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, 1, "guest", [{"email": "a@b.c"}]).status_code == 200
    created = client.post(
        cleanup_path(1),
        json={"reason": "hold lifted", "before": "2027-01-01T00:00:00Z"},
    )
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    confirmed = client.post(f"{cleanup_path(1)}/{request_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["deleted_count"] == 1

    entry = get_export(client)["versions"][0]
    # The hit record was deleted by the confirmed cleanup ...
    assert entry["hit_count"] == 0
    assert len(client.get(audit_path(1)).json()) == 0
    # ... while the access trail survives and still reports the masked value.
    assert entry["masked_count"] == 1
    assert entry["view_count"] == 1
    assert len(client.get(access_path(1)).json()) == 1

    # New data after the cleanup is counted alongside the surviving records.
    assert post_view(client, 1, "guest", [{"email": "n@o.p"}]).status_code == 200
    entry = get_export(client)["versions"][0]
    assert entry["hit_count"] == 1
    assert entry["masked_count"] == 2
    assert entry["view_count"] == 2


def test_export_totals_equal_the_sum_of_the_version_entries(
    client: TestClient,
) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    create_policy(
        client, 1,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, 1, "guest", [{"email": "a@b.c"}]).status_code == 200
    client.post(
        cleanup_path(1),
        json={"reason": "r", "before": "2027-01-01T00:00:00Z"},
    )

    add_version(client, [{"name": "email", "type": "string", "nullable": True}])
    create_policy(
        client, 2,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": []},
    )
    assert post_view(
        client, 2, "guest",
        [{"email": "a@b.c"}, {"email": "d@e.f"}],
    ).status_code == 200

    body = get_export(client)
    versions = body["versions"]
    totals = body["totals"]
    assert totals["policy_count"] == sum(len(v["policies"]) for v in versions)
    assert totals["hit_count"] == sum(v["hit_count"] for v in versions)
    assert totals["masked_count"] == sum(v["masked_count"] for v in versions)
    assert totals["view_count"] == sum(v["view_count"] for v in versions)
    assert totals["cleanup_request_count"] == sum(
        len(v["cleanup_requests"]) for v in versions
    )
    assert totals == {
        "policy_count": 2,
        # The v1 request is only pending, so its hit record survives.
        "hit_count": 3,
        "masked_count": 3,
        "view_count": 2,
        "cleanup_request_count": 1,
    }


# --------------------------------------------------------------------------- #
# Cleanup requests
# --------------------------------------------------------------------------- #


def test_export_lists_pending_and_confirmed_cleanup_requests_sorted(
    client: TestClient,
) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    assert post_view(client, 1, "guest", [{"email": "a@b.c"}]).status_code == 200
    first = client.post(
        cleanup_path(1),
        json={"reason": "first", "before": "2027-01-01T00:00:00Z"},
    ).json()
    assert client.post(f"{cleanup_path(1)}/{first['id']}/confirm").status_code == 200
    # A later hit lets a second (pending) request select it.
    assert post_view(client, 1, "guest", [{"email": "d@e.f"}]).status_code == 200
    second = client.post(
        cleanup_path(1),
        json={"reason": "second", "before": "2027-01-01T00:00:00Z"},
    )
    assert second.status_code == 201, second.text
    second_body = second.json()

    requests_ = get_export(client)["versions"][0]["cleanup_requests"]
    assert [request["id"] for request in requests_] == [
        first["id"], second_body["id"]
    ]
    by_id = {request["id"]: request for request in requests_}
    assert by_id[first["id"]]["status"] == "confirmed"
    assert by_id[second_body["id"]]["status"] == "pending"
    for request in requests_:
        assert list(request) == CLEANUP_KEYS
        assert request["reason"]
        # Time fields carry their timezone verbatim.
        assert "+" in request["created_at"] or request["created_at"].endswith("Z")


# --------------------------------------------------------------------------- #
# Errors: 404 / 422, no writes
# --------------------------------------------------------------------------- #


def test_export_unknown_dataset_returns_404(client: TestClient) -> None:
    response = client.get("/datasets/ghost/privacy-compliance-export")
    assert response.status_code == 404
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "not_found"


def test_export_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset(client)
    before = export_response(client).text

    with_body = client.request("GET", EXPORT_PATH, content=b"{}")
    with_query = client.get(EXPORT_PATH, params={"limit": 1})
    assert with_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, with_query):
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "validation_error"
        assert response.json()["detail"]

    # The rejections wrote nothing.
    assert export_response(client).text == before


def test_export_shape_errors_keep_404_precedence(client: TestClient) -> None:
    make_dataset(client)
    assert client.get(
        "/datasets/ghost/privacy-compliance-export", params={"x": "1"}
    ).status_code == 404
    assert (
        client.request(
            "GET", "/datasets/ghost/privacy-compliance-export", content=b"{}"
        ).status_code
        == 404
    )


def test_export_is_strictly_read_only(client: TestClient) -> None:
    make_dataset(client, fields=BASE_FIELDS)
    create_policy(
        client, 1,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, 1, "guest", [{"email": "a@b.c"}]).status_code == 200
    hits_before = client.get(audit_path(1)).json()
    access_before = client.get(access_path(1)).json()
    cleanup_before = client.get(cleanup_path(1)).json()
    first_text = export_response(client).text

    for _ in range(3):
        response = export_response(client)
        assert response.text == first_text
    assert client.get(audit_path(1)).json() == hits_before
    assert client.get(access_path(1)).json() == access_before
    assert client.get(cleanup_path(1)).json() == cleanup_before


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
    ]},
).status_code == 201
assert client.post(
    "/datasets/orders/versions/1/privacy-policies",
    json={
        "field": "email",
        "classification": "PII",
        "masking": "redact",
        "allowed_roles": ["analyst"],
    },
).status_code == 201
viewed = client.post(
    "/datasets/orders/versions/1/privacy-policies/view",
    json={"role": "guest", "rows": [{"email": "a@b.c", "ssn": "123456"}]},
)
assert viewed.status_code == 200, viewed.text
print("created")
"""

_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

response = client.get("/datasets/orders/privacy-compliance-export")
assert response.status_code == 200, response.text
assert response.text.endswith("\\n")
body = response.json()
assert list(body) == ["dataset", "versions", "totals"]
assert body["dataset"] == "orders"
assert [v["version"] for v in body["versions"]] == [1]
entry = body["versions"][0]
assert list(entry) == [
    "version", "policies", "hit_count", "masked_count",
    "view_count", "cleanup_requests",
]
assert len(entry["policies"]) == 1
assert entry["policies"][0]["field"] == "email"
assert entry["policies"][0]["allowed_roles"] == ["analyst"]
assert entry["hit_count"] == 1
assert entry["masked_count"] == 1
assert entry["view_count"] == 1
assert entry["cleanup_requests"] == []
assert body["totals"] == {
    "policy_count": 1,
    "hit_count": 1,
    "masked_count": 1,
    "view_count": 1,
    "cleanup_request_count": 0,
}
again = client.get("/datasets/orders/privacy-compliance-export")
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


def test_export_survives_a_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "privacy-compliance-export.db"
    assert _run_script(db_path, _CREATE_SCRIPT) == "created"
    assert _run_script(db_path, _VERIFY_SCRIPT) == "verified"
