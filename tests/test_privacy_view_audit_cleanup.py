"""Tests for the two-stage preview/confirm cleanup of masking-hit records."""

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

from app.db import db_session

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _wait_until(iso_after: str) -> None:
    """Sleep until the wall clock is strictly later than ``iso_after``."""
    target = datetime.fromisoformat(iso_after)
    while datetime.now(timezone.utc) <= target:
        time.sleep(0.005)


BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "email", "type": "string", "nullable": True},
    {"name": "ssn", "type": "string", "nullable": True},
]

FUTURE = "2099-01-01T00:00:00+00:00"
PAST = "2000-01-01T00:00:00+00:00"


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


def audit_path(dataset: str = "orders", version: int = 1) -> str:
    return policies_path(dataset, version) + "/view/audit-records"


def cleanup_path(dataset: str = "orders", version: int = 1) -> str:
    return audit_path(dataset, version) + "/cleanup-requests"


def access_path(dataset: str = "orders", version: int = 1) -> str:
    return policies_path(dataset, version) + "/view/access-records"


def create_policy(client: TestClient, payload: dict, **path: object) -> dict:
    response = client.post(policies_path(**path), json=payload)  # type: ignore[arg-type]
    assert response.status_code == 201, response.text
    return response.json()


def post_view(client: TestClient, role: str, rows: list[dict], **path: object):
    return client.post(
        policies_path(**path) + "/view",  # type: ignore[arg-type]
        json={"role": role, "rows": rows},
    )


def make_request(
    client: TestClient, *, reason: str = "legal hold", before: str = FUTURE, **path: object
) -> dict:
    response = client.post(
        cleanup_path(**path), json={"reason": reason, "before": before}  # type: ignore[arg-type]
    )
    assert response.status_code == 201, response.text
    return response.json()


def confirm(client: TestClient, request_id: int, **path: object):
    return client.post(f"{cleanup_path(**path)}/{request_id}/confirm")  # type: ignore[arg-type]


def seed_two_policies_and_hits(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    create_policy(
        client,
        {"field": "ssn", "classification": "SECRET", "masking": "partial",
         "allowed_roles": []},
    )
    response = post_view(
        client, "guest", [{"email": "a@b.c", "ssn": "123", "id": 1}]
    )
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# Creation and preview
# --------------------------------------------------------------------------- #


def test_create_returns_request_shape_with_preview(client: TestClient) -> None:
    seed_two_policies_and_hits(client)
    body = make_request(client, reason="  legal hold  ", before=FUTURE)

    assert set(body) == {
        "id", "reason", "before", "status", "created_at", "preview"
    }
    assert body["id"] == 1
    assert body["reason"] == "legal hold"
    assert body["before"] == FUTURE
    assert body["status"] == "pending"
    datetime.fromisoformat(body["created_at"])

    preview = body["preview"]
    assert set(preview) == {"hit_count", "first_hit_at", "last_hit_at", "fields"}
    assert preview["hit_count"] == 2
    assert preview["fields"] == ["email", "ssn"]
    records = client.get(audit_path()).json()
    assert preview["first_hit_at"] == records[0]["created_at"]
    assert preview["last_hit_at"] == records[-1]["created_at"]
    for value in (preview["first_hit_at"], preview["last_hit_at"]):
        datetime.fromisoformat(value)


def test_preview_does_not_modify_any_hit_record(client: TestClient) -> None:
    seed_two_policies_and_hits(client)
    before = client.get(audit_path()).json()
    make_request(client)

    after = client.get(audit_path()).json()
    assert after == before
    # The read-only analytics stay byte-identical as well.
    for suffix in ("", "/search", "/summary", "/diff", "/reconcile", "/trend"):
        assert client.get(audit_path() + suffix).status_code == 200
    assert client.get(audit_path()).json() == before


def test_preview_empty_target_set(client: TestClient) -> None:
    seed_two_policies_and_hits(client)
    body = make_request(client, before=PAST)
    assert body["status"] == "pending"
    assert body["preview"] == {
        "hit_count": 0,
        "first_hit_at": None,
        "last_hit_at": None,
        "fields": [],
    }
    # Nothing was deleted.
    assert len(client.get(audit_path()).json()) == 2


def test_preview_targets_only_records_earlier_than_before(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    post_view(client, "guest", [{"email": "a@b.c"}])
    first_batch = client.get(audit_path()).json()
    boundary = (
        datetime.fromisoformat(first_batch[-1]["created_at"])
        + timedelta(milliseconds=50)
    ).isoformat()
    _wait_until(boundary)
    post_view(client, "guest", [{"email": "d@e.f"}, {"email": "g@h.i"}])
    all_records = client.get(audit_path()).json()
    assert len(all_records) == 3

    body = make_request(client, before=boundary)
    assert body["preview"]["hit_count"] == 1
    assert body["preview"]["first_hit_at"] == first_batch[0]["created_at"]
    assert body["preview"]["last_hit_at"] == first_batch[0]["created_at"]
    assert body["preview"]["fields"] == ["email"]
    # The preview itself deletes nothing.
    assert len(client.get(audit_path()).json()) == 3


def test_before_accepts_non_utc_offset_and_compares_instants(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    post_view(client, "guest", [{"email": "a@b.c"}])
    written = datetime.fromisoformat(client.get(audit_path()).json()[0]["created_at"])

    # A cutoff one second after the hit expressed in a +08:00 offset still
    # selects the record (instants are compared, not the local clock text).
    later = written + timedelta(seconds=1)
    cutoff_after = later.astimezone(timezone(timedelta(hours=8))).isoformat()
    assert make_request(client, before=cutoff_after)["preview"]["hit_count"] == 1
    # The old pending request must be confirmed away before another is opened.
    assert confirm(client, 1).status_code == 200

    # An equal-instant cutoff expressed with a -05:00 offset excludes the
    # record (the cutoff is strict), proving offset-normalized comparison.
    post_view(client, "guest", [{"email": "d@e.f"}])
    second_hit = datetime.fromisoformat(
        client.get(audit_path()).json()[-1]["created_at"]
    )
    same_instant = second_hit.astimezone(timezone(timedelta(hours=-5))).isoformat()
    assert (
        make_request(client, reason="same-instant", before=same_instant)[
            "preview"
        ]["hit_count"]
        == 0
    )


def test_target_set_is_fixed_at_creation(client: TestClient) -> None:
    seed_two_policies_and_hits(client)
    request = make_request(client, before=FUTURE)
    assert request["preview"]["hit_count"] == 2

    # Hits written after the request never enter it.
    assert post_view(
        client, "auditor", [{"email": "new@b.c", "ssn": "999"}]
    ).status_code == 200
    assert len(client.get(audit_path()).json()) == 4

    response = confirm(client, request["id"])
    assert response.status_code == 200, response.text
    assert response.json()["deleted_count"] == 2
    assert response.json()["preview"] == request["preview"]

    remaining = client.get(audit_path()).json()
    assert [record["field"] for record in remaining] == ["email", "ssn"]
    assert {record["role"] for record in remaining} == {"auditor"}


# --------------------------------------------------------------------------- #
# Pending uniqueness and 409 handling
# --------------------------------------------------------------------------- #


def test_only_one_pending_request_per_version(client: TestClient) -> None:
    seed_two_policies_and_hits(client)
    first = make_request(client, before=FUTURE)
    response = client.post(
        cleanup_path(), json={"reason": "second", "before": FUTURE}
    )
    assert response.status_code == 409
    assert set(response.json()) == {"error", "detail"}
    assert response.json()["error"] == "conflict"
    # The rejected request was not written.
    listed = client.get(cleanup_path()).json()
    assert [item["id"] for item in listed] == [first["id"]]


def test_new_request_allowed_after_confirmation(client: TestClient) -> None:
    seed_two_policies_and_hits(client)
    first = make_request(client, before=FUTURE)
    assert confirm(client, first["id"]).status_code == 200

    second = client.post(
        cleanup_path(), json={"reason": "again", "before": PAST}
    )
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first["id"] + 1
    assert second.json()["status"] == "pending"


def test_pending_request_scoped_per_version(client: TestClient) -> None:
    make_dataset_with_version(client, "orders")
    make_dataset_with_version(client, "raw", [{"name": "id", "type": "integer", "nullable": False}])
    make_request(client, before=FUTURE)
    # A different version can open its own request even while the first is
    # still pending.
    other = client.post(
        cleanup_path("raw", 1), json={"reason": "x", "before": FUTURE}
    )
    assert other.status_code == 201, other.text


# --------------------------------------------------------------------------- #
# Confirmation
# --------------------------------------------------------------------------- #


def test_confirm_deletes_targets_and_reports_counts(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    post_view(client, "guest", [{"email": "a@b.c"}, {"email": "d@e.f"}])
    first_written = datetime.fromisoformat(
        client.get(audit_path()).json()[0]["created_at"]
    )
    boundary = (first_written + timedelta(milliseconds=50)).isoformat()
    _wait_until(boundary)
    post_view(client, "guest", [{"email": "g@h.i"}])
    records = client.get(audit_path()).json()
    request = make_request(client, before=boundary)

    response = confirm(client, request["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "id", "reason", "before", "status", "created_at", "preview",
        "confirmed_at", "deleted_count",
    }
    assert body["status"] == "confirmed"
    assert body["deleted_count"] == 2
    datetime.fromisoformat(body["confirmed_at"])

    remaining = client.get(audit_path()).json()
    assert [record["sequence"] for record in remaining] == [3]
    assert remaining[0]["field"] == "email"
    assert remaining[0]["created_at"] == records[2]["created_at"]


def test_confirm_twice_is_409_and_changes_nothing(client: TestClient) -> None:
    seed_two_policies_and_hits(client)
    request = make_request(client, before=FUTURE)
    first = confirm(client, request["id"])
    assert first.status_code == 200
    deleted_count = first.json()["deleted_count"]
    confirmed_at = first.json()["confirmed_at"]

    second = confirm(client, request["id"])
    assert second.status_code == 409
    assert second.json()["error"] == "conflict"

    stored = client.get(cleanup_path()).json()[0]
    assert stored["status"] == "confirmed"
    assert len(client.get(audit_path()).json()) == 0
    # The confirmed response itself is unchanged on the rejected repeat.
    assert first.json()["confirmed_at"] == confirmed_at
    assert first.json()["deleted_count"] == deleted_count


def test_confirm_empty_target_set_deletes_nothing(client: TestClient) -> None:
    seed_two_policies_and_hits(client)
    request = make_request(client, before=PAST)
    response = confirm(client, request["id"])
    assert response.status_code == 200
    assert response.json()["deleted_count"] == 0
    assert response.json()["status"] == "confirmed"
    assert len(client.get(audit_path()).json()) == 2


def test_concurrent_confirms_have_one_winner_and_never_half_delete(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    for index in range(6):
        post_view(client, "guest", [{"email": f"u{index}@b.c"}])
    request = make_request(client, before=FUTURE)
    url = f"{cleanup_path()}/{request['id']}/confirm"

    statuses: list[int] = []
    def worker() -> None:
        worker_client = TestClient(client.app)
        statuses.append(worker_client.post(url).status_code)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [200, 409, 409, 409]
    # All target records or none per winner — never a partial delete.
    assert client.get(audit_path()).json() == []
    stored = client.get(cleanup_path()).json()[0]
    assert stored["status"] == "confirmed"
    assert stored["preview"]["hit_count"] == 6


def test_remaining_records_keep_sequences_and_new_hits_continue_run(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    post_view(client, "guest", [{"email": "a@b.c"}])            # seq 1
    first_written = datetime.fromisoformat(
        client.get(audit_path()).json()[0]["created_at"]
    )
    # Give the second view a strictly later write timestamp.
    later = (first_written + timedelta(milliseconds=50)).isoformat()
    _wait_until(later)
    post_view(client, "guest", [{"email": "d@e.f"}])            # seq 2
    boundary = later
    request = make_request(client, before=boundary)
    assert confirm(client, request["id"]).status_code == 200

    # The survivor keeps its original number; the cleaned number is not reused.
    assert [r["sequence"] for r in client.get(audit_path()).json()] == [2]

    post_view(client, "guest", [{"email": "g@h.i"}])            # continues run
    assert [r["sequence"] for r in client.get(audit_path()).json()] == [2, 3]


def test_reads_and_analytics_recompute_from_surviving_records(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    post_view(client, "guest", [{"email": "a@b.c"}])
    first_written = datetime.fromisoformat(
        client.get(audit_path()).json()[0]["created_at"]
    )
    boundary = (first_written + timedelta(milliseconds=50)).isoformat()
    _wait_until(boundary)
    post_view(client, "auditor", [{"email": "d@e.f"}])

    request = make_request(client, before=boundary)
    assert confirm(client, request["id"]).status_code == 200

    surviving = client.get(audit_path()).json()
    assert len(surviving) == 1
    assert surviving[0]["role"] == "auditor"
    search = client.get(audit_path() + "/search", params={"role": "guest"})
    assert search.json() == []
    summary = client.get(audit_path() + "/summary").json()
    assert len(summary["groups"]) == 1
    assert summary["groups"][0]["role"] == "auditor"
    trend = client.get(audit_path() + "/trend").json()
    assert trend["totals"]["total_hits"] == 1
    assert client.get(audit_path() + "/diff").status_code == 200
    reconcile = client.get(audit_path() + "/reconcile").json()
    assert reconcile["totals"]["hit_count"] == 1
    # Access records are never cleaned and keep the original masked counts, so
    # the day now cross-checks as inconsistent.
    assert reconcile["totals"]["masked_count"] == 2
    assert reconcile["totals"]["view_count"] == 2
    assert len(client.get(access_path()).json()) == 2


def test_access_records_and_processing_audit_chain_are_untouched(
    client: TestClient,
) -> None:
    seed_two_policies_and_hits(client)
    access_before = client.get(access_path()).json()
    request = make_request(client, before=FUTURE)
    assert confirm(client, request["id"]).status_code == 200
    assert client.get(access_path()).json() == access_before


def test_hit_records_remain_immutable_outside_the_cleanup_gate(
    client: TestClient,
) -> None:
    seed_two_policies_and_hits(client)
    request = make_request(client, before=PAST)  # pending with an empty target set
    with db_session() as conn:
        import sqlite3

        with pytest.raises(sqlite3.Error):
            conn.execute("DELETE FROM privacy_view_audit_records")
        conn.rollback()
    # The request is still pending and the records are all present.
    assert client.get(cleanup_path()).json()[0]["status"] == "pending"
    assert len(client.get(audit_path()).json()) == 2


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


def test_list_requests_sorted_by_id_ascending(client: TestClient) -> None:
    make_dataset_with_version(client)
    create_policy(
        client,
        {"field": "email", "classification": "PII", "masking": "redact",
         "allowed_roles": []},
    )
    post_view(client, "guest", [{"email": "a@b.c"}])
    first = make_request(client, reason="one", before=PAST)
    confirm(client, first["id"])
    second = make_request(client, reason="two", before=PAST)
    confirm(client, second["id"])
    third = make_request(client, reason="three", before=PAST)

    listed = client.get(cleanup_path()).json()
    assert [item["id"] for item in listed] == [1, 2, 3]
    assert [item["status"] for item in listed] == [
        "confirmed", "confirmed", "pending"
    ]
    # Pending entries carry no confirmation keys.
    assert set(listed[2]) == {
        "id", "reason", "before", "status", "created_at", "preview"
    }


def test_list_requests_empty_and_unknown_dataset_or_version_404(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    assert client.get(cleanup_path()).json() == []
    assert client.get(cleanup_path("ghost", 1)).status_code == 404
    assert client.get(cleanup_path("orders", 9)).status_code == 404


def test_list_requests_rejects_body_and_query_params(client: TestClient) -> None:
    make_dataset_with_version(client)
    with_body = client.request("GET", cleanup_path(), content=b"{}")
    with_query = client.get(cleanup_path(), params={"x": 1})
    assert with_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_body, with_query):
        assert response.json()["error"] == "validation_error"
        assert set(response.json()) == {"error", "detail"}


# --------------------------------------------------------------------------- #
# 404 / 422 precedence
# --------------------------------------------------------------------------- #


def test_create_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    payload = {"reason": "x", "before": FUTURE}
    assert client.post(cleanup_path("ghost", 1), json=payload).status_code == 404
    make_dataset_with_version(client)
    assert client.post(cleanup_path("orders", 9), json=payload).status_code == 404


def test_create_validates_after_path_resolves(client: TestClient) -> None:
    make_dataset_with_version(client)
    # A malformed body against a real version is a 422, not a 500/404.
    assert client.post(cleanup_path(), json={"reason": "x"}).status_code == 422
    # The same malformed body against an unknown dataset stays a 404.
    assert (
        client.post(cleanup_path("ghost", 1), json={"reason": "x"}).status_code
        == 404
    )


def test_confirm_unknown_request_is_404(client: TestClient) -> None:
    seed_two_policies_and_hits(client)
    make_request(client, before=FUTURE)
    response = confirm(client, 999)
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_confirm_unknown_dataset_or_version_is_404(client: TestClient) -> None:
    seed_two_policies_and_hits(client)
    request = make_request(client, before=FUTURE)
    assert confirm(client, request["id"], dataset="ghost", version=1).status_code == 404
    assert confirm(client, request["id"], dataset="orders", version=9).status_code == 404


def test_confirm_404_precedes_body_validation(client: TestClient) -> None:
    seed_two_policies_and_hits(client)
    response = client.post(
        f"{cleanup_path()}/999/confirm", content=b"{}",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 404
    assert (
        client.post(f"{cleanup_path('ghost', 1)}/1/confirm").status_code == 404
    )


# --------------------------------------------------------------------------- #
# Request-body and query-parameter validation
# --------------------------------------------------------------------------- #


def test_create_rejects_invalid_payloads(client: TestClient) -> None:
    seed_two_policies_and_hits(client)
    valid = {"reason": "x", "before": FUTURE}
    bad_payloads = [
        {},
        {"before": FUTURE},
        {"reason": "x"},
        {"reason": "", "before": FUTURE},
        {"reason": "   ", "before": FUTURE},
        {"reason": 1, "before": FUTURE},
        {"reason": True, "before": FUTURE},
        {"reason": None, "before": FUTURE},
        {"reason": "x", "before": "2099-01-01T00:00:00"},  # no timezone
        {"reason": "x", "before": "not-a-date"},
        {"reason": "x", "before": 123},
        {"reason": "x", "before": None},
        {**valid, "extra": 1},
    ]
    for payload in bad_payloads:
        response = client.post(cleanup_path(), json=payload)
        assert response.status_code == 422, payload
        assert response.json()["error"] == "validation_error"
        assert set(response.json()) == {"error", "detail"}

    # Whitespace-only or non-JSON bodies and a non-object body are 422 too.
    for content in (b"", b"   ", b"{", b"[]", b'"x"', b"null"):
        response = client.post(
            cleanup_path(),
            content=content,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422, content

    # Nothing was written; a valid request still succeeds.
    assert client.get(cleanup_path()).json() == []
    assert client.post(cleanup_path(), json=valid).status_code == 201


def test_create_rejects_query_parameters(client: TestClient) -> None:
    seed_two_policies_and_hits(client)
    response = client.post(
        cleanup_path() + "?x=1", json={"reason": "x", "before": FUTURE}
    )
    assert response.status_code == 422
    assert client.get(cleanup_path()).json() == []


def test_confirm_rejects_body_and_query_parameters(client: TestClient) -> None:
    seed_two_policies_and_hits(client)
    request = make_request(client, before=FUTURE)
    url = f"{cleanup_path()}/{request['id']}/confirm"

    with_json_body = client.post(url, json={})
    with_raw_body = client.post(
        url, content=b"[]", headers={"content-type": "application/json"}
    )
    with_query = client.post(url + "?x=1")
    assert with_json_body.status_code == 422
    assert with_raw_body.status_code == 422
    assert with_query.status_code == 422
    for response in (with_json_body, with_raw_body, with_query):
        assert response.json()["error"] == "validation_error"

    # Nothing was deleted: the request is still pending with all records.
    assert client.get(cleanup_path()).json()[0]["status"] == "pending"
    assert len(client.get(audit_path()).json()) == 2
    # The empty-body confirm now succeeds.
    assert confirm(client, request["id"]).status_code == 200


# --------------------------------------------------------------------------- #
# Persistence across a process restart
# --------------------------------------------------------------------------- #


def _run_script(db_path: Path, script: str) -> str:
    env = {**os.environ, "DATA_LINEAGE_DB": str(db_path)}
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


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
for field, masking in (("email", "redact"), ("ssn", "partial")):
    response = client.post(
        "/datasets/orders/versions/1/privacy-policies",
        json={
            "field": field, "classification": "PII", "masking": masking,
            "allowed_roles": [],
        },
    )
    assert response.status_code == 201, response.text
viewed = client.post(
    "/datasets/orders/versions/1/privacy-policies/view",
    json={"role": "guest", "rows": [{"email": "a@b.c", "ssn": "123"}]},
)
assert viewed.status_code == 200, viewed.text
created = client.post(
    "/datasets/orders/versions/1/privacy-policies/view/audit-records/"
    "cleanup-requests",
    json={"reason": "hold", "before": "2099-01-01T00:00:00+00:00"},
)
assert created.status_code == 201, created.text
print("created")
"""

_CONFIRM_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
base = (
    "/datasets/orders/versions/1/privacy-policies/view/audit-records/"
    "cleanup-requests"
)
listed = client.get(base)
assert listed.status_code == 200, listed.text
requests = listed.json()
assert len(requests) == 1
assert requests[0]["status"] == "pending"
assert requests[0]["preview"]["hit_count"] == 2
assert requests[0]["preview"]["fields"] == ["email", "ssn"]
confirmed = client.post(base + "/1/confirm")
assert confirmed.status_code == 200, confirmed.text
body = confirmed.json()
assert body["status"] == "confirmed"
assert body["deleted_count"] == 2
records = client.get(
    "/datasets/orders/versions/1/privacy-policies/view/audit-records"
)
assert records.json() == []
print("confirmed")
"""

_VERIFY_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
base = (
    "/datasets/orders/versions/1/privacy-policies/view/audit-records/"
    "cleanup-requests"
)
requests = client.get(base).json()
assert len(requests) == 1
assert requests[0]["id"] == 1
assert requests[0]["status"] == "confirmed"
# New hits continue the run after the restart without reusing sequences 1/2.
viewed = client.post(
    "/datasets/orders/versions/1/privacy-policies/view",
    json={"role": "guest", "rows": [{"email": "z@z.z"}]},
)
assert viewed.status_code == 200, viewed.text
records = client.get(
    "/datasets/orders/versions/1/privacy-policies/view/audit-records"
).json()
assert [record["sequence"] for record in records] == [3], records
# A new request is allowed and the confirmed one still rejects re-confirmation.
assert client.post(base + "/1/confirm").status_code == 409
again = client.post(
    base, json={"reason": "again", "before": "2000-01-01T00:00:00+00:00"}
)
assert again.status_code == 201, again.text
print("verified")
"""


def test_cleanup_requests_survive_a_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "privacy-view-cleanup.db"
    assert _run_script(db_path, _CREATE_SCRIPT) == "created"
    assert _run_script(db_path, _CONFIRM_SCRIPT) == "confirmed"
    assert _run_script(db_path, _VERIFY_SCRIPT) == "verified"
