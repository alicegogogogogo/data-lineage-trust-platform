"""Tests for the two-phase cleanup of privacy view masking-hit records."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "email", "type": "string", "nullable": True},
    {"name": "ssn", "type": "string", "nullable": True},
]

FUTURE = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
PAST = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


def make_dataset_with_version(client: TestClient, name: str = "orders") -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions",
        json={"fields": BASE_FIELDS},
    )
    assert response.status_code == 201, response.text


def policies_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/privacy-policies"


def audit_path(dataset: str = "orders", version: int = 1) -> str:
    return policies_path(dataset, version) + "/view/audit-records"


def cleanup_path(dataset: str = "orders", version: int = 1) -> str:
    return audit_path(dataset, version) + "/cleanup-requests"


def confirm_path(request_id: int, dataset: str = "orders", version: int = 1) -> str:
    return cleanup_path(dataset, version) + f"/{request_id}/confirm"


def create_policy(client: TestClient, payload: dict) -> dict:
    response = client.post(policies_path(), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def post_view(client: TestClient, role: str, rows: list[dict]):
    return client.post(
        policies_path() + "/view", json={"role": role, "rows": rows}
    )


def seed_hits(client: TestClient) -> None:
    """Two policies and two views: three hit records (sequences 1..3)."""
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": []},
    )
    create_policy(
        client,
        {"field": "ssn", "classification": "secret", "masking": "redact",
         "allowed_roles": []},
    )
    assert post_view(client, "guest", [{"email": "a@b.c", "ssn": "1"}]).status_code == 200
    assert post_view(client, "guest", [{"email": "d@e.f"}]).status_code == 200


def create_request(client: TestClient, before: str = FUTURE, reason: str = "gdpr"):
    return client.post(cleanup_path(), json={"reason": reason, "before": before})


# --------------------------------------------------------------------------- #
# Preview (create)
# --------------------------------------------------------------------------- #


def test_create_returns_preview_and_changes_nothing(client: TestClient) -> None:
    seed_hits(client)
    records_before = client.get(audit_path()).json()
    summary_before = client.get(audit_path() + "/summary").json()
    trend_before = client.get(audit_path() + "/trend").json()

    response = create_request(client, reason="  retention window  ")
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"id", "reason", "before", "status", "preview", "created_at"}
    assert body["reason"] == "retention window"
    assert body["before"] == FUTURE
    assert body["status"] == "pending"
    datetime.fromisoformat(body["created_at"])
    preview = body["preview"]
    assert set(preview) == {"hit_count", "first_hit_at", "last_hit_at", "fields"}
    assert preview["hit_count"] == 3
    assert preview["first_hit_at"] == records_before[0]["created_at"]
    assert preview["last_hit_at"] == records_before[-1]["created_at"]
    assert preview["fields"] == ["email", "ssn"]

    # The preview deleted and changed nothing.
    assert client.get(audit_path()).json() == records_before
    assert client.get(audit_path() + "/summary").json() == summary_before
    assert client.get(audit_path() + "/trend").json() == trend_before
    assert client.get(audit_path() + "/diff").status_code == 200
    assert client.get(audit_path() + "/reconcile").status_code == 200
    assert client.get(audit_path() + "/search").json() == records_before


def test_create_with_no_matching_records_previews_empty(client: TestClient) -> None:
    seed_hits(client)
    response = create_request(client, before=PAST)
    assert response.status_code == 201, response.text
    preview = response.json()["preview"]
    assert preview == {
        "hit_count": 0,
        "first_hit_at": None,
        "last_hit_at": None,
        "fields": [],
    }


def test_second_pending_request_conflicts_and_writes_nothing(
    client: TestClient,
) -> None:
    seed_hits(client)
    assert create_request(client).status_code == 201

    conflict = create_request(client, reason="another")
    assert conflict.status_code == 409
    assert set(conflict.json()) == {"error", "detail"}

    requests = client.get(cleanup_path()).json()
    assert len(requests) == 1
    assert requests[0]["reason"] == "gdpr"
    # The conflict wrote nothing and the hits are untouched.
    assert len(client.get(audit_path()).json()) == 3


def test_new_request_allowed_after_confirm(client: TestClient) -> None:
    seed_hits(client)
    first = create_request(client).json()
    assert client.post(confirm_path(first["id"])).status_code == 200
    second = create_request(client, reason="second pass")
    assert second.status_code == 201, second.text
    assert second.json()["preview"]["hit_count"] == 0


# --------------------------------------------------------------------------- #
# Confirm
# --------------------------------------------------------------------------- #


def test_confirm_deletes_target_set_and_reports_count(client: TestClient) -> None:
    seed_hits(client)
    request = create_request(client).json()

    response = client.post(confirm_path(request["id"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["preview"]["hit_count"] == 3
    datetime.fromisoformat(body["confirmed_at"])
    assert body["deleted_count"] == 3

    assert client.get(audit_path()).json() == []
    summary = client.get(audit_path() + "/summary").json()
    assert summary["groups"] == []
    trend = client.get(audit_path() + "/trend").json()
    assert trend["policies"] == []
    assert trend["totals"]["total_hits"] == 0


def test_confirm_keeps_hits_written_after_the_request(client: TestClient) -> None:
    seed_hits(client)
    request = create_request(client).json()

    # Written after the request was created: outside the frozen target set.
    assert post_view(client, "guest", [{"email": "x@y.z"}]).status_code == 200

    confirmed = client.post(confirm_path(request["id"])).json()
    assert confirmed["deleted_count"] == 3

    remaining = client.get(audit_path()).json()
    assert [record["sequence"] for record in remaining] == [4]
    assert remaining[0]["field"] == "email"


def test_remaining_records_keep_sequences_and_new_hits_never_reuse(
    client: TestClient,
) -> None:
    seed_hits(client)
    # Clean everything, including the tail records.
    request = create_request(client).json()
    assert client.post(confirm_path(request["id"])).status_code == 200
    assert client.get(audit_path()).json() == []

    # New hits continue the original numbering instead of reusing 1..3.
    assert post_view(client, "guest", [{"email": "n@o.p", "ssn": "9"}]).status_code == 200
    records = client.get(audit_path()).json()
    assert [record["sequence"] for record in records] == [4, 5]


def test_partial_cleanup_keeps_remaining_sequences(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "partial",
         "allowed_roles": []},
    )
    assert post_view(client, "guest", [{"email": "a@b.c"}]).status_code == 200
    # Distinct write times for the two views, so the cutoff selects exactly
    # the first record.
    time.sleep(0.01)
    assert post_view(client, "guest", [{"email": "d@e.f"}]).status_code == 200
    records = client.get(audit_path()).json()
    assert len(records) == 2

    request = create_request(client, before=records[-1]["created_at"]).json()
    assert request["preview"]["hit_count"] == 1
    assert request["preview"]["fields"] == ["email"]

    confirmed = client.post(confirm_path(request["id"])).json()
    assert confirmed["deleted_count"] == 1
    remaining = client.get(audit_path()).json()
    assert [record["sequence"] for record in remaining] == [2]


def test_reconfirm_conflicts_and_changes_nothing(client: TestClient) -> None:
    seed_hits(client)
    request = create_request(client).json()
    assert client.post(confirm_path(request["id"])).status_code == 200

    again = client.post(confirm_path(request["id"]))
    assert again.status_code == 409
    assert set(again.json()) == {"error", "detail"}
    assert client.get(audit_path()).json() == []
    stored = client.get(cleanup_path()).json()
    assert len(stored) == 1
    assert stored[0]["status"] == "confirmed"


def test_concurrent_confirms_only_one_succeeds(client: TestClient) -> None:
    seed_hits(client)
    request = create_request(client).json()

    results: list[int] = []
    barrier = threading.Barrier(2)

    def confirm() -> None:
        local = TestClient(client.app)
        barrier.wait()
        response = local.post(confirm_path(request["id"]))
        results.append(response.status_code)

    threads = [threading.Thread(target=confirm) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [200, 409]
    # Exactly one atomic delete happened: no half-deleted state, no double
    # count.
    assert client.get(audit_path()).json() == []
    stored = client.get(cleanup_path()).json()
    assert stored[0]["status"] == "confirmed"


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


def test_list_orders_by_id_and_reflects_states(client: TestClient) -> None:
    seed_hits(client)
    assert client.get(cleanup_path()).json() == []

    first = create_request(client, reason="one").json()
    assert client.post(confirm_path(first["id"])).status_code == 200
    second = create_request(client, reason="two").json()

    requests = client.get(cleanup_path()).json()
    assert [entry["id"] for entry in requests] == [first["id"], second["id"]]
    assert [entry["status"] for entry in requests] == ["confirmed", "pending"]
    for entry in requests:
        assert set(entry) == {
            "id", "reason", "before", "status", "preview", "created_at",
        }


def test_list_rejects_body_and_query(client: TestClient) -> None:
    seed_hits(client)
    with_body = client.request("GET", cleanup_path(), content=b"{}")
    with_query = client.get(cleanup_path(), params={"limit": 1})
    assert with_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, with_query):
        assert set(response.json()) == {"error", "detail"}


# --------------------------------------------------------------------------- #
# Validation and 404 precedence
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "payload",
    [
        {"before": FUTURE},  # missing reason
        {"reason": 7, "before": FUTURE},  # non-string reason
        {"reason": "   ", "before": FUTURE},  # blank reason
        {"reason": "gdpr"},  # missing before
        {"reason": "gdpr", "before": 7},  # non-string before
        {"reason": "gdpr", "before": "not-a-date"},  # unparseable before
        {"reason": "gdpr", "before": "2030-01-01T00:00:00"},  # no timezone
        {"reason": "gdpr", "before": FUTURE, "extra": 1},  # extra field
    ],
)
def test_create_validation_errors_write_nothing(
    client: TestClient, payload: dict
) -> None:
    seed_hits(client)
    response = client.post(cleanup_path(), json=payload)
    assert response.status_code == 422
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "validation_error"
    assert client.get(cleanup_path()).json() == []
    assert len(client.get(audit_path()).json()) == 3


def test_create_rejects_query_parameters(client: TestClient) -> None:
    seed_hits(client)
    response = client.post(
        cleanup_path(), json={"reason": "gdpr", "before": FUTURE},
        params={"force": "1"},
    )
    assert response.status_code == 422
    assert client.get(cleanup_path()).json() == []


def test_confirm_rejects_body_and_query(client: TestClient) -> None:
    seed_hits(client)
    request = create_request(client).json()

    with_body = client.post(confirm_path(request["id"]), content=b"{}")
    with_query = client.post(confirm_path(request["id"]), params={"x": "1"})
    assert with_body.status_code == 422
    assert with_query.status_code == 422

    # The rejections confirmed nothing.
    assert client.get(cleanup_path()).json()[0]["status"] == "pending"
    assert len(client.get(audit_path()).json()) == 3


def test_unknown_resources_return_404_before_422(client: TestClient) -> None:
    seed_hits(client)
    request = create_request(client).json()

    # Unknown dataset, version and request id, each with otherwise-invalid
    # input, keep the 404 precedence of the hit-record reads.
    assert client.post(
        cleanup_path("ghost"), json={"reason": "x", "before": "junk"}
    ).status_code == 404
    assert client.post(
        cleanup_path("orders", 9), json={"reason": "x", "before": "junk"}
    ).status_code == 404
    assert client.get(cleanup_path("ghost"), params={"x": "1"}).status_code == 404
    assert client.post(confirm_path(999)).status_code == 404
    assert (
        client.post(confirm_path(999), content=b"{}", params={"x": "1"}).status_code
        == 404
    )
    # A request of another version is not visible here.
    assert client.post(
        f"/datasets/orders/versions",
        json={"fields": BASE_FIELDS},
    ).status_code == 201
    assert client.post(confirm_path(request["id"], version=2)).status_code == 404


# --------------------------------------------------------------------------- #
# Access records and audit chain stay untouched
# --------------------------------------------------------------------------- #


def test_cleanup_never_touches_access_records(client: TestClient) -> None:
    seed_hits(client)
    access_before = client.get(policies_path() + "/view/access-records").json()
    assert len(access_before) == 2

    request = create_request(client).json()
    assert client.post(confirm_path(request["id"])).status_code == 200

    assert client.get(policies_path() + "/view/access-records").json() == access_before


# --------------------------------------------------------------------------- #
# Persistence across a process restart
# --------------------------------------------------------------------------- #


_CREATE_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def ok(response):
    assert response.status_code in (200, 201), response.text

ok(client.post("/datasets", json={"name": "orders"}))
ok(client.post(
    "/datasets/orders/versions",
    json={"fields": [{"name": "email", "type": "string", "nullable": True}]},
))
ok(client.post(
    "/datasets/orders/versions/1/privacy-policies",
    json={"field": "email", "classification": "PII", "masking": "redact",
          "allowed_roles": []},
))
ok(client.post(
    "/datasets/orders/versions/1/privacy-policies/view",
    json={"role": "guest", "rows": [{"email": "a@b.c"}]},
))
created = client.post(
    "/datasets/orders/versions/1/privacy-policies/view/audit-records/"
    "cleanup-requests",
    json={"reason": "restart check", "before": "2999-01-01T00:00:00+00:00"},
)
assert created.status_code == 201, created.text
print("created")
"""

_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
base = "/datasets/orders/versions/1/privacy-policies/view/audit-records"

listed = client.get(base + "/cleanup-requests")
assert listed.status_code == 200, listed.text
requests = listed.json()
assert len(requests) == 1
assert requests[0]["reason"] == "restart check"
assert requests[0]["status"] == "pending"
assert requests[0]["preview"]["hit_count"] == 1

confirmed = client.post(
    base + f"/cleanup-requests/{requests[0]['id']}/confirm"
)
assert confirmed.status_code == 200, confirmed.text
assert confirmed.json()["deleted_count"] == 1
assert client.get(base).json() == []

# A new hit after the restart keeps the original numbering (no reuse of 1).
viewed = client.post(
    "/datasets/orders/versions/1/privacy-policies/view",
    json={"role": "guest", "rows": [{"email": "b@c.d"}]},
)
assert viewed.status_code == 200, viewed.text
records = client.get(base).json()
assert [record["sequence"] for record in records] == [2]
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


def test_requests_survive_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "cleanup-persistence.db"
    assert _run_script(db_path, _CREATE_SCRIPT) == "created"
    assert _run_script(db_path, _VERIFY_SCRIPT) == "verified"
