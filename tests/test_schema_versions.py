import pytest
from fastapi.testclient import TestClient

from app.main import create_app

FIELDS_V1 = [
    {"name": "id", "type": "int", "nullable": False},
    {"name": "amount", "type": "decimal", "nullable": True},
]


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    return TestClient(app)


@pytest.fixture()
def dataset(client):
    response = client.post("/datasets", json={"name": "orders"})
    assert response.status_code == 201
    return response.json()


def test_create_schema_version_starts_at_one(client, dataset):
    response = client.post("/datasets/orders/versions", json={"fields": FIELDS_V1})

    assert response.status_code == 201
    body = response.json()
    assert body["dataset"] == "orders"
    assert body["version"] == 1
    assert body["created_at"]
    assert body["fields"] == sorted(FIELDS_V1, key=lambda f: f["name"])


def test_schema_versions_increment(client, dataset):
    client.post("/datasets/orders/versions", json={"fields": FIELDS_V1})
    response = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "int", "nullable": False}]},
    )

    assert response.status_code == 201
    assert response.json()["version"] == 2


def test_create_version_rejects_empty_field_list(client, dataset):
    response = client.post("/datasets/orders/versions", json={"fields": []})

    assert 400 <= response.status_code < 500


def test_create_version_rejects_duplicate_field_names(client, dataset):
    response = client.post(
        "/datasets/orders/versions",
        json={
            "fields": [
                {"name": "id", "type": "int", "nullable": False},
                {"name": "id", "type": "bigint", "nullable": True},
            ]
        },
    )

    assert response.status_code == 400
    # nothing was written
    assert client.get("/datasets/orders/versions").json() == []


def test_create_version_rejects_incomplete_field(client, dataset):
    response = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "int"}]},
    )

    assert 400 <= response.status_code < 500
    assert "sql" not in response.text.lower()
    assert client.get("/datasets/orders/versions").json() == []


def test_create_version_unknown_dataset_returns_404(client):
    response = client.post("/datasets/ghost/versions", json={"fields": FIELDS_V1})

    assert response.status_code == 404


def test_get_schema_version_with_fields(client, dataset):
    client.post("/datasets/orders/versions", json={"fields": FIELDS_V1})

    response = client.get("/datasets/orders/versions/1")

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1
    assert {f["name"] for f in body["fields"]} == {"id", "amount"}
    assert all(set(f) == {"name", "type", "nullable"} for f in body["fields"])


def test_get_missing_schema_version_returns_404(client, dataset):
    response = client.get("/datasets/orders/versions/99")

    assert response.status_code == 404


def test_list_schema_versions(client, dataset):
    client.post("/datasets/orders/versions", json={"fields": FIELDS_V1})
    client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "int", "nullable": False}]},
    )

    response = client.get("/datasets/orders/versions")

    assert response.status_code == 200
    assert [v["version"] for v in response.json()] == [1, 2]
