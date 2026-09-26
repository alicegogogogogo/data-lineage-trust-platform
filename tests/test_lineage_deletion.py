"""Tests for field-level lineage mapping deletion (DELETE .../lineage)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from fastapi.testclient import TestClient


def make_dataset(client: TestClient, name: str, fields: list[str]) -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions",
        json={
            "fields": [
                {"name": field, "type": "string", "nullable": True}
                for field in fields
            ]
        },
    )
    assert response.status_code == 201, response.text


def lineage_payload(
    source: str,
    source_version: int,
    source_field: str,
    target_field: str,
    target: str = "dm_orders",
    target_version: int = 1,
) -> dict:
    return {
        "target_dataset": target,
        "target_version": target_version,
        "target_field": target_field,
        "source_dataset": source,
        "source_version": source_version,
        "source_field": source_field,
    }


def setup_two_datasets(client: TestClient) -> None:
    make_dataset(client, "raw_orders", ["order_id", "amount"])
    make_dataset(client, "dm_orders", ["id", "total", "orphan"])


def register(client: TestClient, payload: dict) -> None:
    response = client.post(
        f"/datasets/{payload['target_dataset']}"
        f"/versions/{payload['target_version']}/lineage",
        json=payload,
    )
    assert response.status_code == 201, response.text


def delete(
    client: TestClient,
    payload: dict,
    path_target: str | None = None,
    path_version: int | None = None,
):
    # The path target defaults to the body target but can be pinned so tests
    # can send a body whose target deliberately disagrees with the path.
    return client.request(
        "DELETE",
        f"/datasets/{path_target or payload['target_dataset']}"
        f"/versions/{path_version or payload['target_version']}/lineage",
        json=payload,
    )


def sources_of(client: TestClient, dataset: str, version: int) -> dict:
    lineage = client.get(f"/datasets/{dataset}/versions/{version}/lineage")
    assert lineage.status_code == 200
    return {f["target_field"]: f["sources"] for f in lineage.json()["fields"]}


def cache_rows(db_path: Path) -> list[tuple[str, int, str]]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT source_dataset, source_version, source_field "
            "FROM lineage_impact_cache ORDER BY 1, 2, 3"
        ).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_delete_registered_mapping_returns_200_with_removed_mapping(
    client: TestClient,
) -> None:
    setup_two_datasets(client)
    payload = lineage_payload("raw_orders", 1, "order_id", "id")
    register(client, payload)

    response = delete(client, payload)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "target_dataset": "dm_orders",
        "target_version": 1,
        "target_field": "id",
        "source": {"dataset": "raw_orders", "version": 1, "field": "order_id"},
    }


def test_deleted_mapping_disappears_from_target_field_sources(
    client: TestClient,
) -> None:
    setup_two_datasets(client)
    first = lineage_payload("raw_orders", 1, "order_id", "id")
    second = lineage_payload("raw_orders", 1, "amount", "id")
    register(client, first)
    register(client, second)

    assert delete(client, first).status_code == 200

    # The remaining source keeps its place; reading shape and sorting are
    # unchanged.
    assert sources_of(client, "dm_orders", 1) == {
        "id": [{"dataset": "raw_orders", "version": 1, "field": "amount"}],
        "orphan": [],
        "total": [],
    }


def test_deleted_mapping_can_be_registered_again(client: TestClient) -> None:
    setup_two_datasets(client)
    payload = lineage_payload("raw_orders", 1, "order_id", "id")
    register(client, payload)
    assert delete(client, payload).status_code == 200

    response = client.post("/datasets/dm_orders/versions/1/lineage", json=payload)
    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------- #
# 404: unknown resources and unregistered mappings
# --------------------------------------------------------------------------- #


def test_delete_unregistered_mapping_returns_404(client: TestClient) -> None:
    setup_two_datasets(client)
    # All six locating values name existing resources, but no mapping was
    # ever registered between them.
    response = delete(client, lineage_payload("raw_orders", 1, "order_id", "id"))

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_delete_twice_returns_404_the_second_time(client: TestClient) -> None:
    setup_two_datasets(client)
    payload = lineage_payload("raw_orders", 1, "order_id", "id")
    register(client, payload)

    assert delete(client, payload).status_code == 200
    second = delete(client, payload)
    assert second.status_code == 404
    assert second.json()["error"] == "not_found"


def test_delete_unknown_path_dataset_returns_404(client: TestClient) -> None:
    setup_two_datasets(client)
    payload = lineage_payload("raw_orders", 1, "order_id", "id", target="ghost")
    response = client.request(
        "DELETE",
        "/datasets/ghost/versions/1/lineage",
        json=payload,
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_delete_unknown_path_version_returns_404(client: TestClient) -> None:
    setup_two_datasets(client)
    payload = lineage_payload(
        "raw_orders", 1, "order_id", "id", target_version=9
    )
    response = client.request(
        "DELETE",
        "/datasets/dm_orders/versions/9/lineage",
        json=payload,
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_delete_unknown_source_dataset_returns_404(client: TestClient) -> None:
    setup_two_datasets(client)
    response = delete(client, lineage_payload("ghost", 1, "order_id", "id"))

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_delete_unknown_source_version_returns_404(client: TestClient) -> None:
    setup_two_datasets(client)
    response = delete(client, lineage_payload("raw_orders", 7, "order_id", "id"))

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_delete_unknown_source_field_returns_404(client: TestClient) -> None:
    setup_two_datasets(client)
    response = delete(client, lineage_payload("raw_orders", 1, "ghost", "id"))

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_delete_unknown_target_field_returns_404(client: TestClient) -> None:
    setup_two_datasets(client)
    response = delete(client, lineage_payload("raw_orders", 1, "order_id", "ghost"))

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_resource_lookup_precedes_shape_validation(client: TestClient) -> None:
    setup_two_datasets(client)
    # The body is missing required fields AND names an unknown source
    # dataset: the resource lookup wins and the answer is 404, not 422.
    response = client.request("DELETE",
        "/datasets/dm_orders/versions/1/lineage",
        json={"source_dataset": "ghost"},
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


# --------------------------------------------------------------------------- #
# 422: request shape
# --------------------------------------------------------------------------- #


def test_delete_with_missing_body_fields_returns_422(client: TestClient) -> None:
    setup_two_datasets(client)
    response = client.request("DELETE",
        "/datasets/dm_orders/versions/1/lineage",
        json={"target_dataset": "dm_orders", "target_version": 1},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_delete_with_extra_body_field_returns_422(client: TestClient) -> None:
    setup_two_datasets(client)
    payload = lineage_payload("raw_orders", 1, "order_id", "id")
    payload["note"] = "please"
    response = delete(client, payload)

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_delete_with_wrong_field_types_returns_422(client: TestClient) -> None:
    setup_two_datasets(client)
    for broken in (
        {"source_version": "1"},
        {"target_version": 1.5},
        {"source_field": 7},
        {"target_dataset": None},
        {"source_version": True},
    ):
        payload = lineage_payload("raw_orders", 1, "order_id", "id")
        payload.update(broken)
        # The path stays valid; only the body carries the broken value.
        response = delete(client, payload, path_target="dm_orders", path_version=1)
        assert response.status_code == 422, (broken, response.text)
        assert response.json()["error"] == "validation_error"


def test_delete_with_blank_names_returns_422(client: TestClient) -> None:
    setup_two_datasets(client)
    for key in ("target_dataset", "target_field", "source_dataset", "source_field"):
        payload = lineage_payload("raw_orders", 1, "order_id", "id")
        payload[key] = "   "
        response = delete(client, payload, path_target="dm_orders", path_version=1)
        assert response.status_code == 422, (key, response.text)
        assert response.json()["error"] == "validation_error"


def test_delete_with_empty_or_whitespace_body_returns_422(
    client: TestClient,
) -> None:
    setup_two_datasets(client)
    for content in (b"", b"   \n\t "):
        response = client.request("DELETE",
            "/datasets/dm_orders/versions/1/lineage",
            content=content,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"] == "validation_error"


def test_delete_with_invalid_json_returns_422(client: TestClient) -> None:
    setup_two_datasets(client)
    response = client.request("DELETE",
        "/datasets/dm_orders/versions/1/lineage",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_delete_with_non_object_json_returns_422(client: TestClient) -> None:
    setup_two_datasets(client)
    for content in (b"[1, 2]", b'"text"', b"42"):
        response = client.request("DELETE",
            "/datasets/dm_orders/versions/1/lineage",
            content=content,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"] == "validation_error"


def test_delete_with_query_parameter_returns_422(client: TestClient) -> None:
    setup_two_datasets(client)
    payload = lineage_payload("raw_orders", 1, "order_id", "id")
    register(client, payload)
    response = client.request("DELETE",
        "/datasets/dm_orders/versions/1/lineage?force=true", json=payload
    )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    # The rejected delete did not remove the mapping.
    assert sources_of(client, "dm_orders", 1)["id"] == [
        {"dataset": "raw_orders", "version": 1, "field": "order_id"}
    ]


def test_delete_same_source_and_target_dataset_returns_422(
    client: TestClient,
) -> None:
    make_dataset(client, "orders", ["id"])
    response = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id2", "type": "string", "nullable": True}]},
    )
    assert response.status_code == 201

    payload = lineage_payload("orders", 1, "id", "id2", target="orders", target_version=2)
    response = client.request(
        "DELETE",
        "/datasets/orders/versions/2/lineage",
        json=payload,
    )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_delete_body_target_must_match_path(client: TestClient) -> None:
    setup_two_datasets(client)
    # The body target names an existing dataset that is not the path target,
    # so the mismatch (not a missing resource) decides: 422, nothing written.
    make_dataset(client, "other", ["id"])
    payload = lineage_payload(
        "raw_orders", 1, "order_id", "id", target="other", target_version=1
    )
    response = client.request(
        "DELETE",
        "/datasets/dm_orders/versions/1/lineage",
        json=payload,
    )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


# --------------------------------------------------------------------------- #
# Rejections change nothing
# --------------------------------------------------------------------------- #


def test_rejected_deletes_do_not_change_lineage_or_cache(
    client: TestClient, isolated_database: Path
) -> None:
    setup_two_datasets(client)
    payload = lineage_payload("raw_orders", 1, "order_id", "id")
    register(client, payload)
    client.get(
        "/datasets/raw_orders/versions/1/lineage/impact",
        params={"field": "order_id"},
    )
    cached_before = cache_rows(isolated_database)
    sources_before = sources_of(client, "dm_orders", 1)

    rejections = [
        client.request("DELETE", "/datasets/dm_orders/versions/1/lineage", json={}),
        delete(client, lineage_payload("raw_orders", 1, "amount", "id")),
        delete(client, lineage_payload("ghost", 1, "order_id", "id")),
        client.request("DELETE",
            "/datasets/dm_orders/versions/1/lineage?force=true", json=payload
        ),
    ]
    assert [r.status_code for r in rejections] == [422, 404, 404, 422]

    assert cache_rows(isolated_database) == cached_before
    assert sources_of(client, "dm_orders", 1) == sources_before


# --------------------------------------------------------------------------- #
# Reads reflect the deletion immediately
# --------------------------------------------------------------------------- #


def setup_chain(client: TestClient) -> None:
    # raw_a.a1 -> mid_b.b1 -> mart_c.c1
    make_dataset(client, "raw_a", ["a1"])
    make_dataset(client, "mid_b", ["b1"])
    make_dataset(client, "mart_c", ["c1"])
    register(client, lineage_payload("raw_a", 1, "a1", "b1", target="mid_b"))
    register(client, lineage_payload("mid_b", 1, "b1", "c1", target="mart_c"))


def test_impact_reads_reflect_deletion(client: TestClient) -> None:
    setup_chain(client)
    impact = client.get(
        "/datasets/raw_a/versions/1/lineage/impact", params={"field": "a1"}
    )
    assert [i["field"] for i in impact.json()["impacted"]] == ["c1", "b1"]

    assert delete(
        client, lineage_payload("mid_b", 1, "b1", "c1", target="mart_c")
    ).status_code == 200

    impact = client.get(
        "/datasets/raw_a/versions/1/lineage/impact", params={"field": "a1"}
    )
    assert impact.json()["impacted"] == [
        {"dataset": "mid_b", "version": 1, "field": "b1"}
    ]


def test_impact_paths_and_source_paths_reflect_deletion(
    client: TestClient,
) -> None:
    setup_chain(client)
    assert delete(
        client, lineage_payload("mid_b", 1, "b1", "c1", target="mart_c")
    ).status_code == 200

    paths = client.get(
        "/datasets/raw_a/versions/1/lineage/impact-paths", params={"field": "a1"}
    )
    assert [i["field"] for i in paths.json()["impacts"]] == ["b1"]

    origins = client.get(
        "/datasets/mart_c/versions/1/lineage/impact/source-paths",
        params={"field": "c1"},
    )
    assert origins.json()["origins"] == []
    assert origins.json()["direct_count"] == 0
    assert origins.json()["indirect_count"] == 0


def test_delete_invalidates_source_and_upstream_cache(
    client: TestClient, isolated_database: Path
) -> None:
    setup_chain(client)
    make_dataset(client, "lonely", ["l1"])
    for dataset, field in (("raw_a", "a1"), ("mid_b", "b1"), ("lonely", "l1")):
        response = client.get(
            f"/datasets/{dataset}/versions/1/lineage/impact",
            params={"field": field},
        )
        assert response.status_code == 200

    assert delete(
        client, lineage_payload("mid_b", 1, "b1", "c1", target="mart_c")
    ).status_code == 200

    # The deleted mapping's source and every field that reaches it are
    # dropped; the unrelated entry is preserved.
    remaining = cache_rows(isolated_database)
    assert ("raw_a", 1, "a1") not in remaining
    assert ("mid_b", 1, "b1") not in remaining
    assert ("lonely", 1, "l1") in remaining


# --------------------------------------------------------------------------- #
# Concurrency and restart persistence
# --------------------------------------------------------------------------- #


def test_concurrent_deletes_of_same_mapping_succeed_exactly_once(
    client: TestClient,
) -> None:
    setup_two_datasets(client)
    payload = lineage_payload("raw_orders", 1, "order_id", "id")
    register(client, payload)

    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def worker() -> None:
        barrier.wait()
        statuses.append(delete(client, payload).status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [200, 404]
    assert sources_of(client, "dm_orders", 1)["id"] == []


def test_deletion_survives_restart(
    client: TestClient, isolated_database: Path
) -> None:
    setup_two_datasets(client)
    payload = lineage_payload("raw_orders", 1, "order_id", "id")
    register(client, payload)
    assert delete(client, payload).status_code == 200

    # A fresh client (nothing cached in process memory) sees the deletion.
    fresh = TestClient(client.app)
    assert sources_of(fresh, "dm_orders", 1)["id"] == []
    again = delete(fresh, payload)
    assert again.status_code == 404


# --------------------------------------------------------------------------- #
# Repeated 'field' query parameter on the lineage reads
# --------------------------------------------------------------------------- #


def test_impact_repeated_field_parameter_returns_422(client: TestClient) -> None:
    setup_chain(client)
    response = client.get(
        "/datasets/raw_a/versions/1/lineage/impact",
        params=[("field", "a1"), ("field", "a1")],
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_impact_repeated_field_beats_unknown_field_404(
    client: TestClient,
) -> None:
    setup_chain(client)
    # The scalar parameter resolves to the second value, which does not
    # exist; the repetition is still reported as 422, never as a field 404.
    response = client.get(
        "/datasets/raw_a/versions/1/lineage/impact",
        params=[("field", "a1"), ("field", "ghost")],
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_impact_paths_repeated_field_beats_unknown_field_404(
    client: TestClient,
) -> None:
    setup_chain(client)
    response = client.get(
        "/datasets/raw_a/versions/1/lineage/impact-paths",
        params=[("field", "a1"), ("field", "ghost")],
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_source_paths_repeated_field_beats_unknown_field_404(
    client: TestClient,
) -> None:
    setup_chain(client)
    response = client.get(
        "/datasets/mart_c/versions/1/lineage/impact/source-paths",
        params=[("field", "c1"), ("field", "ghost")],
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
