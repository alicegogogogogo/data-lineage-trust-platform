"""Tests for dataset creation, lookup and error handling."""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient


def test_create_dataset_returns_stable_payload(client: TestClient) -> None:
    response = client.post(
        "/datasets", json={"name": "orders", "description": "Order events"}
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "name", "description", "created_at"}
    assert isinstance(body["id"], int)
    assert body["name"] == "orders"
    assert body["description"] == "Order events"
    # created_at is an ISO-8601 timestamp that can be parsed by clients.
    datetime.fromisoformat(body["created_at"])


def test_create_dataset_without_description_defaults_to_empty(
    client: TestClient,
) -> None:
    response = client.post("/datasets", json={"name": "orders"})

    assert response.status_code == 201
    assert response.json()["description"] == ""


def test_dataset_id_is_stable_on_read(client: TestClient) -> None:
    created = client.post("/datasets", json={"name": "orders"}).json()
    listed = client.get("/datasets").json()

    assert [dataset["id"] for dataset in listed if dataset["name"] == "orders"] == [
        created["id"]
    ]


def test_duplicate_dataset_name_is_rejected(client: TestClient) -> None:
    first = client.post("/datasets", json={"name": "orders"})
    second = client.post(
        "/datasets", json={"name": "orders", "description": "again"}
    )

    assert first.status_code == 201
    assert second.status_code == 409
    body = second.json()
    assert set(body) == {"error", "detail"}
    assert body["error"] == "conflict"
    assert "orders" in body["detail"]
    # The duplicate request must not have created a second row.
    datasets = client.get("/datasets").json()
    assert [d["name"] for d in datasets] == ["orders"]


def test_empty_dataset_name_is_rejected(client: TestClient) -> None:
    response = client.post("/datasets", json={"name": "", "description": "x"})

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get("/datasets").json() == []


def test_whitespace_dataset_name_is_rejected(client: TestClient) -> None:
    response = client.post("/datasets", json={"name": "   "})

    assert response.status_code == 422
    assert client.get("/datasets").json() == []


def test_missing_name_field_is_rejected(client: TestClient) -> None:
    response = client.post("/datasets", json={"description": "no name"})

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert "name" in body["detail"]


def test_datasets_are_listed_sorted_by_name(client: TestClient) -> None:
    client.post("/datasets", json={"name": "zebra"})
    client.post("/datasets", json={"name": "alpha"})
    client.post("/datasets", json={"name": "mango"})

    names = [d["name"] for d in client.get("/datasets").json()]
    assert names == ["alpha", "mango", "zebra"]


def test_error_response_does_not_leak_internals(client: TestClient) -> None:
    response = client.post("/datasets", json={"name": "orders"})
    assert response.status_code == 201
    duplicate = client.post("/datasets", json={"name": "orders"})
    text = duplicate.text.lower()

    assert "traceback" not in text
    assert "sqlite" not in text
    assert "select" not in text
    assert "insert" not in text
