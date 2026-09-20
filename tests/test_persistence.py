"""Data written through one app instance survives a 'restart' (new instance, same DB file)."""

from fastapi.testclient import TestClient

from app.main import create_app


def test_data_survives_restart(tmp_path):
    db_path = str(tmp_path / "lineage.db")

    first = TestClient(create_app(db_path))
    first.post("/datasets", json={"name": "orders", "description": "facts"})
    first.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "int", "nullable": False}]},
    )
    first.post("/datasets", json={"name": "staging_orders"})
    first.post(
        "/datasets/staging_orders/versions",
        json={"fields": [{"name": "id", "type": "int", "nullable": False}]},
    )
    first.post(
        "/lineage",
        json={
            "target_dataset": "orders",
            "target_version": 1,
            "target_field": "id",
            "source_dataset": "staging_orders",
            "source_version": 1,
            "source_field": "id",
        },
    )

    # simulate a service restart: a brand-new app on the same database file
    second = TestClient(create_app(db_path))

    dataset = second.get("/datasets/orders")
    assert dataset.status_code == 200
    assert dataset.json()["description"] == "facts"

    version = second.get("/datasets/orders/versions/1")
    assert version.status_code == 200
    assert version.json()["fields"] == [
        {"name": "id", "type": "int", "nullable": False}
    ]

    lineage = second.get("/lineage/orders/1")
    assert lineage.status_code == 200
    assert lineage.json()["fields"][0]["sources"] == [
        {"dataset": "staging_orders", "version": 1, "field": "id"}
    ]

    # version numbering continues from persisted state, not from 1
    response = second.post(
        "/datasets/orders/versions",
        json={"fields": [{"name": "id", "type": "bigint", "nullable": False}]},
    )
    assert response.status_code == 201
    assert response.json()["version"] == 2
