"""Tests for field-level lineage registration and queries."""

from __future__ import annotations

from fastapi.testclient import TestClient


def make_dataset(client: TestClient, name: str, fields: list[dict]) -> None:
    assert client.post("/datasets", json={"name": name}).status_code == 201
    response = client.post(
        f"/datasets/{name}/versions", json={"fields": fields}
    )
    assert response.status_code == 201, response.text


def add_version(client: TestClient, name: str, fields: list[dict]) -> int:
    response = client.post(f"/datasets/{name}/versions", json={"fields": fields})
    assert response.status_code == 201, response.text
    return response.json()["version"]


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
    make_dataset(
        client,
        "raw_orders",
        [
            {"name": "order_id", "type": "integer", "nullable": False},
            {"name": "amount", "type": "decimal", "nullable": True},
        ],
    )
    make_dataset(
        client,
        "dm_orders",
        [
            {"name": "id", "type": "integer", "nullable": False},
            {"name": "total", "type": "decimal", "nullable": True},
            {"name": "orphan", "type": "string", "nullable": True},
        ],
    )


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #


def test_create_lineage_link(client: TestClient) -> None:
    setup_two_datasets(client)
    response = client.post(
        "/datasets/dm_orders/versions/1/lineage",
        json=lineage_payload("raw_orders", 1, "order_id", "id"),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body == {
        "target_dataset": "dm_orders",
        "target_version": 1,
        "target_field": "id",
        "source": {"dataset": "raw_orders", "version": 1, "field": "order_id"},
    }


def test_target_field_can_have_multiple_sources(client: TestClient) -> None:
    setup_two_datasets(client)
    make_dataset(
        client,
        "raw_refunds",
        [{"name": "refund_amount", "type": "decimal", "nullable": True}],
    )

    first = client.post(
        "/datasets/dm_orders/versions/1/lineage",
        json=lineage_payload("raw_orders", 1, "amount", "total"),
    )
    second = client.post(
        "/datasets/dm_orders/versions/1/lineage",
        json=lineage_payload("raw_refunds", 1, "refund_amount", "total"),
    )

    assert first.status_code == 201
    assert second.status_code == 201


def test_lineage_between_versions_of_same_dataset_is_rejected(
    client: TestClient,
) -> None:
    make_dataset(
        client, "orders", [{"name": "id", "type": "integer", "nullable": False}]
    )
    add_version(
        client, "orders", [{"name": "id", "type": "bigint", "nullable": False}]
    )

    response = client.post(
        "/datasets/orders/versions/2/lineage",
        json=lineage_payload(
            "orders", 1, "id", "id", target="orders", target_version=2
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_unknown_source_dataset_returns_404(client: TestClient) -> None:
    make_dataset(
        client, "dm_orders", [{"name": "id", "type": "integer", "nullable": False}]
    )
    response = client.post(
        "/datasets/dm_orders/versions/1/lineage",
        json=lineage_payload("ghost", 1, "id", "id"),
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_unknown_target_dataset_returns_404(client: TestClient) -> None:
    make_dataset(
        client, "raw_orders", [{"name": "id", "type": "integer", "nullable": False}]
    )
    response = client.post(
        "/datasets/ghost/versions/1/lineage",
        json=lineage_payload("raw_orders", 1, "id", "id", target="ghost"),
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_unknown_source_version_returns_404(client: TestClient) -> None:
    setup_two_datasets(client)
    response = client.post(
        "/datasets/dm_orders/versions/1/lineage",
        json=lineage_payload("raw_orders", 2, "order_id", "id"),
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_unknown_target_version_returns_404(client: TestClient) -> None:
    setup_two_datasets(client)
    response = client.post(
        "/datasets/dm_orders/versions/5/lineage",
        json=lineage_payload(
            "raw_orders", 1, "order_id", "id", target_version=5
        ),
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_unknown_source_field_returns_404(client: TestClient) -> None:
    setup_two_datasets(client)
    response = client.post(
        "/datasets/dm_orders/versions/1/lineage",
        json=lineage_payload("raw_orders", 1, "missing", "id"),
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_unknown_target_field_returns_404(client: TestClient) -> None:
    setup_two_datasets(client)
    response = client.post(
        "/datasets/dm_orders/versions/1/lineage",
        json=lineage_payload("raw_orders", 1, "order_id", "missing"),
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_duplicate_complete_mapping_is_rejected(client: TestClient) -> None:
    setup_two_datasets(client)
    payload = lineage_payload("raw_orders", 1, "order_id", "id")
    first = client.post("/datasets/dm_orders/versions/1/lineage", json=payload)
    second = client.post("/datasets/dm_orders/versions/1/lineage", json=payload)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"] == "conflict"

    lineage = client.get("/datasets/dm_orders/versions/1/lineage").json()
    id_field = next(f for f in lineage["fields"] if f["target_field"] == "id")
    assert len(id_field["sources"]) == 1


def test_same_source_field_to_two_targets_is_allowed(client: TestClient) -> None:
    setup_two_datasets(client)
    # raw_orders.order_id feeds both dm_orders.id and dm_orders.total — distinct
    # mappings and both must be accepted.
    for target_field in ("id", "total"):
        response = client.post(
            "/datasets/dm_orders/versions/1/lineage",
            json=lineage_payload("raw_orders", 1, "order_id", target_field),
        )
        assert response.status_code == 201, response.text


def test_lineage_with_missing_body_fields_is_rejected(client: TestClient) -> None:
    setup_two_datasets(client)
    response = client.post(
        "/datasets/dm_orders/versions/1/lineage",
        json={"target_dataset": "dm_orders", "target_version": 1},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    for required in ("source_dataset", "source_version", "source_field", "target_field"):
        assert required in body["detail"]


def test_path_target_must_match_body_target(client: TestClient) -> None:
    setup_two_datasets(client)
    response = client.post(
        "/datasets/dm_orders/versions/1/lineage",
        json=lineage_payload(
            "raw_orders", 1, "order_id", "id", target="other_dm", target_version=1
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


def test_lineage_query_shape_and_sorting(client: TestClient) -> None:
    setup_two_datasets(client)
    make_dataset(
        client,
        "raw_payments",
        [
            {"name": "cents", "type": "integer", "nullable": True},
            {"name": "currency", "type": "string", "nullable": False},
        ],
    )

    links = [
        # Insert deliberately out of order; the response ordering must not
        # depend on insertion order.
        ("raw_payments", 1, "cents", "total"),
        ("raw_orders", 1, "amount", "total"),
        ("raw_orders", 1, "order_id", "id"),
        ("raw_payments", 1, "currency", "total"),
    ]
    for source, version, field, target_field in links:
        response = client.post(
            "/datasets/dm_orders/versions/1/lineage",
            json=lineage_payload(source, version, field, target_field),
        )
        assert response.status_code == 201, response.text

    lineage = client.get("/datasets/dm_orders/versions/1/lineage")
    assert lineage.status_code == 200
    body = lineage.json()
    assert body["target_dataset"] == "dm_orders"
    assert body["target_version"] == 1

    # Target fields are sorted by name and fields without sources are included.
    assert [f["target_field"] for f in body["fields"]] == [
        "id",
        "orphan",
        "total",
    ]

    by_field = {f["target_field"]: f["sources"] for f in body["fields"]}
    assert by_field["orphan"] == []
    assert by_field["id"] == [
        {"dataset": "raw_orders", "version": 1, "field": "order_id"},
    ]
    # Sources sorted by dataset name, then version, then field name.
    assert by_field["total"] == [
        {"dataset": "raw_orders", "version": 1, "field": "amount"},
        {"dataset": "raw_payments", "version": 1, "field": "cents"},
        {"dataset": "raw_payments", "version": 1, "field": "currency"},
    ]


def test_lineage_sorting_includes_source_version(client: TestClient) -> None:
    make_dataset(
        client,
        "target",
        [{"name": "id", "type": "integer", "nullable": False}],
    )
    make_dataset(
        client,
        "source",
        [
            {"name": "v2_field", "type": "integer", "nullable": False},
            {"name": "v1_field", "type": "integer", "nullable": False},
        ],
    )
    # A second source version reusing one field name.
    add_version(
        client,
        "source",
        [
            {"name": "v2_field", "type": "bigint", "nullable": False},
            {"name": "v3_name", "type": "integer", "nullable": False},
        ],
    )
    add_version(
        client,
        "source",
        [{"name": "v3_name", "type": "bigint", "nullable": False}],
    )

    for version, field in ((2, "v2_field"), (1, "v1_field"), (3, "v3_name")):
        response = client.post(
            "/datasets/target/versions/1/lineage",
            json=lineage_payload("source", version, field, "id", target="target"),
        )
        assert response.status_code == 201, response.text

    sources = client.get("/datasets/target/versions/1/lineage").json()["fields"][0][
        "sources"
    ]
    assert [(s["version"], s["field"]) for s in sources] == [
        (1, "v1_field"),
        (2, "v2_field"),
        (3, "v3_name"),
    ]


def test_lineage_is_scoped_to_target_version(client: TestClient) -> None:
    setup_two_datasets(client)
    add_version(
        client,
        "dm_orders",
        [{"name": "id", "type": "bigint", "nullable": False}],
    )
    client.post(
        "/datasets/dm_orders/versions/1/lineage",
        json=lineage_payload("raw_orders", 1, "order_id", "id"),
    )

    v1 = client.get("/datasets/dm_orders/versions/1/lineage").json()
    v2 = client.get("/datasets/dm_orders/versions/2/lineage").json()

    assert v1["fields"][0]["sources"] != []
    assert v2["fields"] == [{"target_field": "id", "sources": []}]


def test_lineage_query_unknown_target_version_returns_404(client: TestClient) -> None:
    setup_two_datasets(client)
    response = client.get("/datasets/dm_orders/versions/99/lineage")

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert "traceback" not in response.text.lower()
