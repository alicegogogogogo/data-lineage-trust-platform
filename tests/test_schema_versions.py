"""Tests for immutable schema versions and their fields."""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient


def create_dataset(client: TestClient, name: str = "orders") -> None:
    response = client.post("/datasets", json={"name": name})
    assert response.status_code == 201


def test_first_schema_version_starts_at_one(client: TestClient) -> None:
    create_dataset(client)
    response = client.post(
        "/datasets/orders/versions",
        json={
            "fields": [
                {"name": "order_id", "type": "integer", "nullable": False},
                {"name": "amount", "type": "decimal(10,2)", "nullable": True},
            ]
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "dataset_id",
        "dataset_name",
        "version",
        "created_at",
        "fields",
    }
    assert body["version"] == 1
    assert body["dataset_name"] == "orders"
    assert isinstance(body["dataset_id"], int)
    datetime.fromisoformat(body["created_at"])
    assert body["fields"] == [
        {"name": "order_id", "type": "integer", "nullable": False},
        {"name": "amount", "type": "decimal(10,2)", "nullable": True},
    ]


def test_schema_version_numbers_increment_per_dataset(client: TestClient) -> None:
    create_dataset(client)
    create_dataset(client, "customers")

    def add_version(dataset: str) -> int:
        response = client.post(
            f"/datasets/{dataset}/versions",
            json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
        )
        assert response.status_code == 201
        return response.json()["version"]

    assert add_version("orders") == 1
    assert add_version("customers") == 1
    assert add_version("orders") == 2
    assert add_version("customers") == 2
    assert add_version("orders") == 3


def test_create_version_for_unknown_dataset_returns_404(client: TestClient) -> None:
    response = client.post(
        "/datasets/ghost/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    # No dataset may be created as a side effect.
    assert client.get("/datasets").json() == []


def test_empty_field_list_is_rejected(client: TestClient) -> None:
    create_dataset(client)
    response = client.post("/datasets/orders/versions", json={"fields": []})

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get("/datasets/orders/versions").json() == []


def test_missing_fields_key_is_rejected(client: TestClient) -> None:
    create_dataset(client)
    response = client.post("/datasets/orders/versions", json={})

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert "fields" in body["detail"]


def test_incomplete_field_definition_is_rejected(client: TestClient) -> None:
    create_dataset(client)
    missing_type = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "nullable": False}]},
    )
    missing_nullable = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer"}]},
    )
    missing_name = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"type": "integer", "nullable": False}]},
    )

    for response in (missing_type, missing_nullable, missing_name):
        assert response.status_code == 422
        assert response.json()["error"] == "validation_error"
    # Nothing was written.
    assert client.get("/datasets/orders/versions").json() == []


def test_empty_field_name_is_rejected(client: TestClient) -> None:
    create_dataset(client)
    blank = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "", "type": "integer", "nullable": False}]},
    )
    whitespace = client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "  ", "type": "integer", "nullable": False}]},
    )

    assert blank.status_code == 422
    assert whitespace.status_code == 422
    assert client.get("/datasets/orders/versions").json() == []


def test_duplicate_field_names_within_version_are_rejected(
    client: TestClient,
) -> None:
    create_dataset(client)
    response = client.post(
        "/datasets/orders/versions",
        json={
            "fields": [
                {"name": "id", "type": "integer", "nullable": False},
                {"name": "id", "type": "string", "nullable": True},
            ]
        },
    )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert client.get("/datasets/orders/versions").json() == []


def test_field_names_may_repeat_across_versions(client: TestClient) -> None:
    create_dataset(client)
    for _ in range(2):
        response = client.post(
            "/datasets/orders/versions",
            json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
        )
        assert response.status_code == 201

    versions = client.get("/datasets/orders/versions").json()
    assert [v["version"] for v in versions] == [1, 2]


def test_read_single_schema_version(client: TestClient) -> None:
    create_dataset(client)
    client.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "integer", "nullable": False}]},
    )

    response = client.get("/datasets/orders/versions/1")
    assert response.status_code == 200
    assert response.json()["version"] == 1
    assert response.json()["fields"][0]["name"] == "id"


def test_read_unknown_version_returns_404(client: TestClient) -> None:
    create_dataset(client)

    assert client.get("/datasets/orders/versions/1").status_code == 404
    assert client.get("/datasets/orders/versions/99").status_code == 404


def test_read_versions_of_unknown_dataset_returns_404(client: TestClient) -> None:
    assert client.get("/datasets/ghost/versions").status_code == 404
    assert client.get("/datasets/ghost/versions/1").status_code == 404


def test_field_order_is_preserved(client: TestClient) -> None:
    create_dataset(client)
    fields = [
        {"name": "zeta", "type": "string", "nullable": True},
        {"name": "alpha", "type": "string", "nullable": True},
        {"name": "mid", "type": "string", "nullable": False},
    ]
    client.post("/datasets/orders/versions", json={"fields": fields})

    returned = client.get("/datasets/orders/versions/1").json()["fields"]
    assert [f["name"] for f in returned] == ["zeta", "alpha", "mid"]
