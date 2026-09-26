"""Tests for privacy policy revision (PUT .../privacy-policies/{policy_id})."""

from __future__ import annotations

import json
import threading

from fastapi.testclient import TestClient


BASE_FIELDS = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "email", "type": "string", "nullable": True},
    {"name": "ssn", "type": "string", "nullable": True},
]


def make_dataset_with_version(client: TestClient, name: str = "orders") -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions", json={"fields": BASE_FIELDS}
    )
    assert response.status_code == 201, response.text


def policies_path(dataset: str = "orders", version: int = 1) -> str:
    return f"/datasets/{dataset}/versions/{version}/privacy-policies"


def create_policy(client: TestClient, **overrides: object) -> dict:
    payload = {
        "field": "email",
        "classification": "PII",
        "masking": "partial",
        "allowed_roles": ["analyst"],
    }
    payload.update(overrides)
    response = client.post(policies_path(), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def revise(client: TestClient, policy_id: int, payload: object, **kwargs: object):
    return client.put(f"{policies_path()}/{policy_id}", json=payload, **kwargs)


def revision_payload(**overrides: object) -> dict:
    payload = {
        "classification": "confidential",
        "masking": "redact",
        "allowed_roles": ["auditor"],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# Successful revision
# --------------------------------------------------------------------------- #


def test_revise_policy_returns_updated_record(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client)

    response = revise(client, policy["id"], revision_payload())

    assert response.status_code == 200, response.text
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
    assert body["id"] == policy["id"]
    assert body["field"] == "email"
    assert body["classification"] == "confidential"
    assert body["masking"] == "redact"
    assert body["allowed_roles"] == ["auditor"]
    # The revision never touches the enabled state or the registration time.
    assert body["enabled"] is True
    assert body["created_at"] == policy["created_at"]


def test_revise_policy_is_visible_in_list_and_survives_restart(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client)
    revise(client, policy["id"], revision_payload(allowed_roles=[]))

    listed = client.get(policies_path()).json()
    assert len(listed) == 1
    assert listed[0]["classification"] == "confidential"
    assert listed[0]["masking"] == "redact"
    assert listed[0]["allowed_roles"] == []

    # A fresh app instance over the same database sees the revised policy.
    restarted = TestClient(client.app)
    listed_again = restarted.get(policies_path()).json()
    assert listed_again == listed


def test_revise_policy_accepts_empty_allowed_roles(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client)

    response = revise(client, policy["id"], revision_payload(allowed_roles=[]))

    assert response.status_code == 200, response.text
    assert response.json()["allowed_roles"] == []


def test_revise_disabled_policy_keeps_it_disabled(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client)
    assert (
        client.patch(
            f"{policies_path()}/{policy['id']}", json={"enabled": False}
        ).status_code
        == 200
    )

    response = revise(client, policy["id"], revision_payload())

    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False


def test_revision_takes_effect_on_the_next_view(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client)
    rows = [{"id": 1, "email": "alice@example.com", "ssn": "111-22-3333"}]

    # Before the revision: partial masking for a non-allowed role.
    before = client.post(
        f"{policies_path()}/view", json={"role": "guest", "rows": rows}
    ).json()
    assert before["rows"][0]["email"] == "aom"
    # The registered allowed role sees the raw value.
    allowed = client.post(
        f"{policies_path()}/view", json={"role": "analyst", "rows": rows}
    ).json()
    assert allowed["rows"][0]["email"] == "alice@example.com"

    revise(
        client,
        policy["id"],
        revision_payload(masking="redact", allowed_roles=["auditor"]),
    )

    # After the revision: redact masking, and only the revised roles are
    # allowed.
    after = client.post(
        f"{policies_path()}/view", json={"role": "guest", "rows": rows}
    ).json()
    assert after["rows"][0]["email"] == "***"
    demoted = client.post(
        f"{policies_path()}/view", json={"role": "analyst", "rows": rows}
    ).json()
    assert demoted["rows"][0]["email"] == "***"
    promoted = client.post(
        f"{policies_path()}/view", json={"role": "auditor", "rows": rows}
    ).json()
    assert promoted["rows"][0]["email"] == "alice@example.com"


def test_revision_leaves_history_and_trend_counts_untouched(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client)
    rows = [{"id": 1, "email": "alice@example.com", "ssn": "111-22-3333"}]
    assert (
        client.post(
            f"{policies_path()}/view", json={"role": "guest", "rows": rows}
        ).status_code
        == 200
    )
    hits_before = client.get(f"{policies_path()}/view/audit-records").json()
    access_before = client.get(f"{policies_path()}/view/access-records").json()

    revise(client, policy["id"], revision_payload())

    # Already written hit and access records are byte-identical.
    assert client.get(f"{policies_path()}/view/audit-records").json() == hits_before
    assert (
        client.get(f"{policies_path()}/view/access-records").json() == access_before
    )
    # The trend keeps its counts but reports the revised current values.
    trend = client.get(f"{policies_path()}/view/audit-records/trend").json()
    assert len(trend["policies"]) == 1
    entry = trend["policies"][0]
    assert entry["policy_id"] == policy["id"]
    assert entry["classification"] == "confidential"
    assert entry["masking"] == "redact"
    assert entry["total_hits"] == 1
    assert trend["totals"]["total_hits"] == 1


def test_revision_updates_coverage_and_compliance_export(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client)

    revise(client, policy["id"], revision_payload())

    coverage = client.get("/datasets/orders/privacy-policy-coverage").json()
    covered = {f["field"]: f for f in coverage["versions"][0]["fields"]}
    assert covered["email"]["classification"] == "confidential"
    assert covered["email"]["masking"] == "redact"

    export = client.get("/datasets/orders/privacy-compliance-export").json()
    export_policies = export["versions"][0]["policies"]
    assert len(export_policies) == 1
    assert export_policies[0]["classification"] == "confidential"
    assert export_policies[0]["masking"] == "redact"
    assert export_policies[0]["allowed_roles"] == ["auditor"]


# --------------------------------------------------------------------------- #
# Validation (422)
# --------------------------------------------------------------------------- #


def _assert_rejected_and_unchanged(
    client: TestClient, policy: dict, response
) -> None:
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "validation_error"
    listed = client.get(policies_path()).json()
    assert listed == [policy]


def test_revise_missing_field_is_422(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client)
    payload = revision_payload()
    del payload["masking"]
    _assert_rejected_and_unchanged(client, policy, revise(client, policy["id"], payload))


def test_revise_extra_field_is_422(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client)
    payload = revision_payload(field="ssn", enabled=False)
    _assert_rejected_and_unchanged(client, policy, revise(client, policy["id"], payload))


def test_revise_blank_classification_is_422(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client)
    _assert_rejected_and_unchanged(
        client,
        policy,
        revise(client, policy["id"], revision_payload(classification="   ")),
    )


def test_revise_invalid_masking_is_422(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client)
    _assert_rejected_and_unchanged(
        client,
        policy,
        revise(client, policy["id"], revision_payload(masking="hash")),
    )


def test_revise_duplicate_or_blank_roles_are_422(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client)
    _assert_rejected_and_unchanged(
        client,
        policy,
        revise(
            client,
            policy["id"],
            revision_payload(allowed_roles=["auditor", " auditor "]),
        ),
    )
    _assert_rejected_and_unchanged(
        client,
        policy,
        revise(client, policy["id"], revision_payload(allowed_roles=["  "])),
    )


def test_revise_empty_whitespace_and_malformed_bodies_are_422(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client)
    url = f"{policies_path()}/{policy['id']}"

    for content in (b"", b"   \n\t ", b"{not json"):
        response = client.put(
            url, content=content, headers={"Content-Type": "application/json"}
        )
        _assert_rejected_and_unchanged(client, policy, response)


def test_revise_non_object_body_is_422(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client)
    url = f"{policies_path()}/{policy['id']}"
    response = client.put(
        url, content=b"[1, 2]", headers={"Content-Type": "application/json"}
    )
    _assert_rejected_and_unchanged(client, policy, response)


def test_revise_with_query_parameter_is_422(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client)
    response = client.put(
        f"{policies_path()}/{policy['id']}?confirm=true",
        json=revision_payload(),
    )
    _assert_rejected_and_unchanged(client, policy, response)


# --------------------------------------------------------------------------- #
# Not found (404) and precedence
# --------------------------------------------------------------------------- #


def test_revise_unknown_policy_is_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    response = revise(client, 999, revision_payload())
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_revise_unknown_dataset_and_version_are_404(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client)

    response = client.put(
        f"/datasets/unknown/versions/1/privacy-policies/{policy['id']}",
        json=revision_payload(),
    )
    assert response.status_code == 404

    response = client.put(
        f"/datasets/orders/versions/9/privacy-policies/{policy['id']}",
        json=revision_payload(),
    )
    assert response.status_code == 404


def test_not_found_takes_precedence_over_request_shape(
    client: TestClient,
) -> None:
    make_dataset_with_version(client)
    # Malformed body plus a query parameter against an unknown policy: 404.
    response = client.put(
        f"{policies_path()}/999?x=1",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_revisions_have_a_single_winner(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client)
    url = f"{policies_path()}/{policy['id']}"
    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def worker(masking: str) -> None:
        thread_client = TestClient(client.app)
        barrier.wait()
        statuses.append(
            thread_client.put(
                url, json=revision_payload(masking=masking)
            ).status_code
        )

    threads = [
        threading.Thread(target=worker, args=("redact",)),
        threading.Thread(target=worker, args=("partial",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [200, 409], statuses
    # Exactly one revision committed; the loser changed nothing.
    listed = client.get(policies_path()).json()
    assert len(listed) == 1
    assert listed[0]["id"] == policy["id"]
    assert listed[0]["classification"] == "confidential"
    assert listed[0]["masking"] in {"redact", "partial"}
    assert listed[0]["allowed_roles"] == ["auditor"]
    assert listed[0]["enabled"] is True
    assert listed[0]["created_at"] == policy["created_at"]


# --------------------------------------------------------------------------- #
# The enable/disable toggle is unaffected
# --------------------------------------------------------------------------- #


def test_enabled_toggle_still_works_after_revision(client: TestClient) -> None:
    make_dataset_with_version(client)
    policy = create_policy(client)
    revise(client, policy["id"], revision_payload())

    response = client.patch(
        f"{policies_path()}/{policy['id']}", json={"enabled": False}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is False
    assert body["classification"] == "confidential"
    assert body["masking"] == "redact"
    assert body["allowed_roles"] == ["auditor"]

    # A non-boolean toggle is still rejected.
    response = client.patch(
        f"{policies_path()}/{policy['id']}", json={"enabled": "yes"}
    )
    assert response.status_code == 422
