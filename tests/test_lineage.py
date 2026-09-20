import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    client = TestClient(app)
    client.post("/datasets", json={"name": "raw_orders"})
    client.post("/datasets", json={"name": "mart_orders"})
    client.post(
        "/datasets/raw_orders/versions",
        json={
            "fields": [
                {"name": "id", "type": "int", "nullable": False},
                {"name": "total", "type": "decimal", "nullable": True},
            ]
        },
    )
    client.post(
        "/datasets/mart_orders/versions",
        json={
            "fields": [
                {"name": "order_id", "type": "int", "nullable": False},
                {"name": "amount", "type": "decimal", "nullable": True},
            ]
        },
    )
    return client


def edge(target_field="order_id", source_field="id", **overrides):
    payload = {
        "target_dataset": "mart_orders",
        "target_version": 1,
        "target_field": target_field,
        "source_dataset": "raw_orders",
        "source_version": 1,
        "source_field": source_field,
    }
    payload.update(overrides)
    return payload


def test_create_lineage_edge(client):
    response = client.post("/lineage", json=edge())

    assert response.status_code == 201
    body = response.json()
    assert body["target_dataset"] == "mart_orders"
    assert body["target_field"] == "order_id"
    assert body["source_dataset"] == "raw_orders"
    assert body["source_field"] == "id"
    assert isinstance(body["id"], int)
    assert body["created_at"]


def test_create_lineage_rejects_duplicate(client):
    assert client.post("/lineage", json=edge()).status_code == 201

    response = client.post("/lineage", json=edge())

    assert response.status_code == 409


def test_create_lineage_rejects_identical_source_and_target(client):
    response = client.post(
        "/lineage",
        json=edge(
            target_dataset="raw_orders",
            target_field="id",
            source_dataset="raw_orders",
            source_field="id",
        ),
    )

    assert response.status_code == 400


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_dataset": "ghost"},
        {"source_dataset": "ghost"},
        {"target_version": 42},
        {"source_version": 42},
        {"target_field": "missing"},
        {"source_field": "missing"},
    ],
)
def test_create_lineage_validates_references(client, overrides):
    response = client.post("/lineage", json=edge(**overrides))

    assert response.status_code == 404
    assert "detail" in response.json()
    assert "sql" not in response.text.lower()


def test_get_lineage_returns_sources_sorted(client):
    client.post("/lineage", json=edge(target_field="amount", source_field="total"))
    client.post("/lineage", json=edge(target_field="order_id", source_field="id"))
    # a second source for the same target field
    client.post("/datasets", json={"name": "audit_orders"})
    client.post(
        "/datasets/audit_orders/versions",
        json={"fields": [{"name": "amount", "type": "decimal", "nullable": True}]},
    )
    client.post(
        "/lineage",
        json=edge(
            target_field="amount",
            source_dataset="audit_orders",
            source_field="amount",
        ),
    )

    response = client.get("/lineage/mart_orders/1")

    assert response.status_code == 200
    body = response.json()
    assert body["target_dataset"] == "mart_orders"
    assert body["target_version"] == 1
    assert [f["field"] for f in body["fields"]] == ["amount", "order_id"]
    amount_sources = body["fields"][0]["sources"]
    assert amount_sources == [
        {"dataset": "audit_orders", "version": 1, "field": "amount"},
        {"dataset": "raw_orders", "version": 1, "field": "total"},
    ]
    assert body["fields"][1]["sources"] == [
        {"dataset": "raw_orders", "version": 1, "field": "id"}
    ]


def test_get_lineage_empty_for_version_without_edges(client):
    response = client.get("/lineage/mart_orders/1")

    assert response.status_code == 200
    body = response.json()
    assert all(f["sources"] == [] for f in body["fields"])


def test_get_lineage_unknown_dataset_or_version_returns_404(client):
    assert client.get("/lineage/ghost/1").status_code == 404
    assert client.get("/lineage/mart_orders/9").status_code == 404
