import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    return TestClient(app)


@pytest.fixture()
def dataset(client):
    response = client.post("/datasets", json={"name": "orders"})
    assert response.status_code == 201
    return response.json()


def test_create_dataset_returns_stable_fields(client):
    response = client.post(
        "/datasets", json={"name": "orders", "description": "order facts"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "orders"
    assert body["description"] == "order facts"
    assert isinstance(body["id"], int)
    assert body["created_at"]


def test_create_dataset_rejects_empty_name(client):
    response = client.post("/datasets", json={"name": "  "})

    assert response.status_code == 400
    assert "detail" in response.json()


def test_create_dataset_rejects_missing_name(client):
    response = client.post("/datasets", json={})

    assert 400 <= response.status_code < 500
    assert "sql" not in response.text.lower()


def test_create_dataset_rejects_duplicate_name(client, dataset):
    response = client.post("/datasets", json={"name": "orders"})

    assert response.status_code == 409
    assert "detail" in response.json()


def test_get_unknown_dataset_returns_404(client):
    response = client.get("/datasets/unknown")

    assert response.status_code == 404
    assert "detail" in response.json()


def test_list_datasets(client, dataset):
    response = client.get("/datasets")

    assert response.status_code == 200
    names = [d["name"] for d in response.json()]
    assert names == ["orders"]
