"""Tests for the persisted snapshot content fingerprint and read-only verify.

The fingerprint is canonical JSON (rows in row order, object keys sorted by
Unicode code point, compact text, non-ASCII unescaped) UTF-8 encoded and
SHA-256 digested. The verify endpoint compares the fingerprint stored at
creation with a fresh digest of the currently persisted rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def create_dataset_and_version(client: TestClient, dataset: str = "orders") -> None:
    response = client.post("/datasets", json={"name": dataset})
    assert response.status_code == 201, response.text
    response = client.post(
        f"/datasets/{dataset}/versions",
        json={"fields": [
            {"name": "id", "type": "integer", "nullable": False},
            {"name": "label", "type": "string", "nullable": True},
        ]},
    )
    assert response.status_code == 201, response.text


def make_snapshot(client: TestClient, rows: list, dataset: str = "orders", version: int = 1) -> dict:
    response = client.post(
        f"/datasets/{dataset}/versions/{version}/snapshots",
        json={"rows": rows},
    )
    assert response.status_code == 201, response.text
    return response.json()


def verify_path(snapshot_id: int, dataset: str = "orders", version: int = 1) -> str:
    return (
        f"/datasets/{dataset}/versions/{version}/snapshots/{snapshot_id}/verify"
    )


def expected_hash(rows: list) -> str:
    canonical = json.dumps(
        rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def db_execute(statement: str, values: tuple = ()) -> None:
    with sqlite3.connect(os.environ["DATA_LINEAGE_DB"]) as conn:
        conn.execute(statement, values)


# --------------------------------------------------------------------------- #
# Document shape and the stored fingerprint
# --------------------------------------------------------------------------- #


def test_verify_untampered_snapshot_is_valid_with_fixed_document_shape(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    rows = [{"zeta": 1, "alpha": ["x", 2]}, {"id": 3}]
    snapshot = make_snapshot(client, rows)

    response = client.get(verify_path(snapshot["id"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert list(body) == [
        "dataset",
        "version",
        "snapshot_id",
        "row_count",
        "stored_hash",
        "computed_hash",
        "valid",
    ]
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["snapshot_id"] == snapshot["id"]
    assert body["row_count"] == 2
    digest = expected_hash(rows)
    assert body["stored_hash"] == digest
    assert body["computed_hash"] == digest
    assert body["valid"] is True

    # Deterministic serialization: compact, lowercase booleans, one newline.
    expected_document = (
        json.dumps(body, separators=(",", ":"), ensure_ascii=False) + "\n"
    )
    assert response.text == expected_document
    assert response.text.endswith('"valid":true}\n')
    assert not response.text.endswith("\n\n")


def test_verify_empty_snapshot_is_fingerprinted_normally(client: TestClient) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [])

    response = client.get(verify_path(snapshot["id"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["row_count"] == 0
    assert body["stored_hash"] == body["computed_hash"] == expected_hash([])
    assert body["valid"] is True


def test_create_response_keeps_its_original_shape(client: TestClient) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    assert set(snapshot) == {"id", "dataset", "version", "created_at", "row_count"}
    assert "content_hash" not in snapshot
    assert "stored_hash" not in snapshot


def test_hash_ignores_object_key_order_but_respects_array_order(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    one = make_snapshot(client, [{"b": 2, "a": 1}, {"k": [1, 2, 3]}])
    reordered_keys = make_snapshot(client, [{"a": 1, "b": 2}, {"k": [1, 2, 3]}])
    reordered_array = make_snapshot(client, [{"b": 2, "a": 1}, {"k": [3, 2, 1]}])

    one_hash = client.get(verify_path(one["id"])).json()["stored_hash"]
    keys_hash = client.get(verify_path(reordered_keys["id"])).json()["stored_hash"]
    array_hash = client.get(verify_path(reordered_array["id"])).json()["stored_hash"]
    assert one_hash == keys_hash
    assert array_hash != one_hash


def test_hash_distinguishes_value_types(client: TestClient) -> None:
    create_dataset_and_version(client)
    variants = [
        [{"v": 1}],
        [{"v": 1.0}],
        [{"v": "1"}],
        [{"v": True}],
        [{"v": None}],
    ]
    hashes = {
        client.get(verify_path(make_snapshot(client, rows)["id"])).json()[
            "stored_hash"
        ]
        for rows in variants
    }
    assert hashes == {expected_hash(rows) for rows in variants}
    assert len(hashes) == len(variants)


def test_hash_represents_negative_zero_distinctly(client: TestClient) -> None:
    create_dataset_and_version(client)
    positive = make_snapshot(client, [{"v": 0.0}])
    negative = make_snapshot(client, [{"v": -0.0}])

    positive_hash = client.get(verify_path(positive["id"])).json()["stored_hash"]
    negative_hash = client.get(verify_path(negative["id"])).json()["stored_hash"]
    assert positive_hash == expected_hash([{"v": 0.0}])
    assert negative_hash == expected_hash([{"v": -0.0}])
    assert negative_hash != positive_hash


def test_hash_keeps_non_ascii_unescaped(client: TestClient) -> None:
    create_dataset_and_version(client)
    rows = [{"label": "café — 寿司"}]
    snapshot = make_snapshot(client, rows)

    response = client.get(verify_path(snapshot["id"]))
    assert response.status_code == 200
    assert response.json()["stored_hash"] == expected_hash(rows)
    # The digest text is ASCII hex; the stored row serialization itself must
    # have kept the non-ASCII characters verbatim.
    stored = client.get(
        f"/datasets/orders/versions/1/snapshots/{snapshot['id']}"
    ).json()["rows"]
    assert stored == rows


# --------------------------------------------------------------------------- #
# Tampering turns verification invalid
# --------------------------------------------------------------------------- #


def test_modifying_a_saved_row_invalidates_verification(client: TestClient) -> None:
    create_dataset_and_version(client)
    rows = [{"id": 1, "label": "a"}, {"id": 2, "label": "b"}]
    snapshot = make_snapshot(client, rows)
    before = client.get(verify_path(snapshot["id"])).json()
    assert before["valid"] is True

    tampered = [{"id": 1, "label": "a"}, {"id": 2, "label": "TAMPERED"}]
    db_execute(
        "UPDATE snapshots SET rows = ? WHERE id = ?",
        (json.dumps(tampered), snapshot["id"]),
    )

    response = client.get(verify_path(snapshot["id"]))
    assert response.status_code == 200, response.text
    after = response.json()
    assert after["valid"] is False
    assert after["stored_hash"] == before["stored_hash"]
    assert after["computed_hash"] == expected_hash(tampered)
    assert after["computed_hash"] != after["stored_hash"]
    # A false verdict is still the same deterministic compact document.
    assert response.text == json.dumps(after, separators=(",", ":")) + "\n"
    assert response.text.endswith('"valid":false}\n')


def test_adding_or_deleting_a_saved_row_invalidates_verification(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    stored_hash = client.get(verify_path(snapshot["id"])).json()["stored_hash"]

    db_execute(
        "UPDATE snapshots SET rows = ?, row_count = 2 WHERE id = ?",
        (json.dumps([{"id": 1}, {"id": 2}]), snapshot["id"]),
    )
    added = client.get(verify_path(snapshot["id"])).json()
    assert added["valid"] is False
    assert added["stored_hash"] == stored_hash
    assert added["computed_hash"] == expected_hash([{"id": 1}, {"id": 2}])

    db_execute(
        "UPDATE snapshots SET rows = ?, row_count = 0 WHERE id = ?",
        (json.dumps([]), snapshot["id"]),
    )
    removed = client.get(verify_path(snapshot["id"])).json()
    assert removed["valid"] is False
    assert removed["computed_hash"] == expected_hash([])


def test_replacing_row_content_and_tampering_the_stored_hash_both_fail(
    client: TestClient,
) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    original_hash = client.get(verify_path(snapshot["id"])).json()["stored_hash"]

    # Forging the stored fingerprint without touching the rows is detected too.
    db_execute(
        "UPDATE snapshots SET content_hash = ? WHERE id = ?",
        ("0" * 64, snapshot["id"]),
    )
    forged = client.get(verify_path(snapshot["id"])).json()
    assert forged["valid"] is False
    assert forged["stored_hash"] == "0" * 64
    assert forged["computed_hash"] == original_hash


# --------------------------------------------------------------------------- #
# Read-only behavior
# --------------------------------------------------------------------------- #


def test_verify_is_read_only_and_repeatable(client: TestClient) -> None:
    create_dataset_and_version(client)
    rows = [{"id": 1, "label": "a"}]
    snapshot = make_snapshot(client, rows)
    path = verify_path(snapshot["id"])

    first = client.get(path)
    second = client.get(path)
    assert first.text == second.text

    # The snapshot, its rows and the listing are untouched.
    read = client.get(f"/datasets/orders/versions/1/snapshots/{snapshot['id']}")
    assert read.json()["rows"] == rows
    listed = client.get("/datasets/orders/versions/1/snapshots").json()
    assert [item["id"] for item in listed] == [snapshot["id"]]


# --------------------------------------------------------------------------- #
# 404 precedence and 422 shape checks
# --------------------------------------------------------------------------- #


def test_verify_unknown_dataset_version_or_snapshot_is_404(
    client: TestClient,
) -> None:
    assert client.get("/datasets/ghost/versions/1/snapshots/1/verify").status_code == 404
    create_dataset_and_version(client)
    assert client.get(
        "/datasets/orders/versions/9/snapshots/1/verify"
    ).status_code == 404
    snapshot = make_snapshot(client, [])
    missing = client.get(verify_path(999))
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"

    # A snapshot owned by another dataset/version is invisible here.
    create_dataset_and_version(client, "other")
    foreign = make_snapshot(client, [], dataset="other")
    scoped = client.get(verify_path(foreign["id"]))
    assert scoped.status_code == 404


def test_404_takes_precedence_over_every_shape_check(client: TestClient) -> None:
    # Whitespace-only body on an unknown dataset.
    assert client.request(
        "GET",
        "/datasets/ghost/versions/1/snapshots/1/verify",
        content=b"  \n\t ",
    ).status_code == 404
    create_dataset_and_version(client)
    # Arbitrary query parameters on an unknown version and snapshot.
    assert client.get(
        "/datasets/orders/versions/9/snapshots/1/verify",
        params={"bogus": "1"},
    ).status_code == 404
    make_snapshot(client, [])
    assert client.get(
        verify_path(999), params={"bogus": "1"}
    ).status_code == 404
    assert client.request(
        "GET", verify_path(999), content=b"   "
    ).status_code == 404


def test_verify_rejects_any_body_bytes_and_query_parameters(client: TestClient) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [{"id": 1}])
    path = verify_path(snapshot["id"])

    whitespace = client.request("GET", path, content=b"  \n\t ")
    assert whitespace.status_code == 422
    assert whitespace.json()["error"] == "validation_error"
    assert set(whitespace.json()) == {"error", "detail"}

    json_body = client.request(
        "GET",
        path,
        content=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert json_body.status_code == 422
    assert json_body.json()["error"] == "validation_error"

    queried = client.get(path, params={"bogus": "1"})
    assert queried.status_code == 422
    assert queried.json()["error"] == "validation_error"

    # A rejected request still verifies the untouched snapshot afterwards.
    assert client.get(path).json()["valid"] is True


def test_verify_accepts_only_get(client: TestClient) -> None:
    create_dataset_and_version(client)
    snapshot = make_snapshot(client, [])
    path = verify_path(snapshot["id"])
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)(path)
        assert response.status_code == 405, method


# --------------------------------------------------------------------------- #
# Deleted snapshots are no longer verifiable
# --------------------------------------------------------------------------- #


def test_confirmed_deleted_snapshot_verify_is_404(client: TestClient) -> None:
    create_dataset_and_version(client)
    policy = client.post(
        "/datasets/orders/versions/1/retention-policies",
        json={"retention_days": 0},
    )
    assert policy.status_code == 201, policy.text
    snapshot = make_snapshot(client, [{"id": 1}])
    assert client.get(verify_path(snapshot["id"])).status_code == 200

    request = client.post(
        f"/datasets/orders/versions/1/snapshots/{snapshot['id']}/deletion-requests",
        json={"reason": "no longer needed"},
    )
    assert request.status_code == 201, request.text
    confirm = client.post(
        f"/datasets/orders/versions/1/snapshots/{snapshot['id']}"
        f"/deletion-requests/{request.json()['id']}/confirm"
    )
    assert confirm.status_code == 200, confirm.text

    assert client.get(
        f"/datasets/orders/versions/1/snapshots/{snapshot['id']}"
    ).status_code == 404
    assert client.get(verify_path(snapshot["id"])).status_code == 404


# --------------------------------------------------------------------------- #
# Determinism across process restarts
# --------------------------------------------------------------------------- #


CREATE_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

assert client.post("/datasets", json={"name": "orders"}).status_code == 201
fields = [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "label", "type": "string", "nullable": True},
]
assert client.post(
    "/datasets/orders/versions", json={"fields": fields}
).status_code == 201
rows = [{"label": "café", "id": 1}, {"id": 2, "nested": [1, True, -0.0, None]}]
created = client.post(
    "/datasets/orders/versions/1/snapshots", json={"rows": rows}
)
assert created.status_code == 201, created.text
print(json.dumps({"id": created.json()["id"], "rows": rows}))
"""

VERIFY_SCRIPT = """
import hashlib
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
payload = json.loads(input())
snapshot_id = payload["id"]
rows = payload["rows"]
response = client.get(
    f"/datasets/orders/versions/1/snapshots/{snapshot_id}/verify"
)
assert response.status_code == 200, response.text
body = response.json()
canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
assert body["valid"] is True
assert body["stored_hash"] == digest
assert body["computed_hash"] == digest
assert body["row_count"] == 2
# Repeated reads after the restart stay identical.
assert client.get(
    f"/datasets/orders/versions/1/snapshots/{snapshot_id}/verify"
).text == response.text
print(body["stored_hash"])
"""


def _run(db_path: Path, script: str, stdin: str = "") -> str:
    env = os.environ.copy()
    env["DATA_LINEAGE_DB"] = str(db_path)
    result = subprocess.run(
        [sys.executable, "-c", script],
        input=stdin,
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_fingerprint_and_verification_survive_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "verify-lineage.db"
    created = json.loads(_run(db_path, CREATE_SCRIPT))
    digest = _run(db_path, VERIFY_SCRIPT, stdin=json.dumps(created))
    assert digest == expected_hash(created["rows"])
