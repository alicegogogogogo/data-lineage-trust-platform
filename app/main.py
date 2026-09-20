"""REST API for datasets, immutable schema versions, and field-level lineage."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.db import Database, DuplicateError

DEFAULT_DB_PATH = "lineage.db"


class DatasetCreate(BaseModel):
    name: str
    description: str | None = None


class FieldSpec(BaseModel):
    name: str
    type: str
    nullable: bool


class SchemaVersionCreate(BaseModel):
    fields: list[FieldSpec] = Field(..., min_length=1)


class LineageCreate(BaseModel):
    target_dataset: str
    target_version: int
    target_field: str
    source_dataset: str
    source_version: int
    source_field: str


def _error(status_code: int, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or os.environ.get("DLTP_DATABASE_PATH", DEFAULT_DB_PATH)
    db = Database(path)

    app = FastAPI(title="Data Lineage Trust Platform", version="0.1.0")

    @app.exception_handler(RequestValidationError)
    def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"detail": "invalid request body", "errors": exc.errors()},
        )

    @app.exception_handler(Exception)
    def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # -- datasets ---------------------------------------------------------

    @app.post("/datasets", status_code=201)
    def create_dataset(payload: DatasetCreate) -> dict[str, Any]:
        name = payload.name.strip()
        if not name:
            raise _error(400, "dataset name must not be empty")
        try:
            dataset = db.create_dataset(name, payload.description)
        except DuplicateError:
            raise _error(409, f"dataset '{name}' already exists") from None
        return dataset

    @app.get("/datasets")
    def list_datasets() -> list[dict[str, Any]]:
        return db.list_datasets()

    @app.get("/datasets/{name}")
    def get_dataset(name: str) -> dict[str, Any]:
        dataset = db.get_dataset_by_name(name)
        if dataset is None:
            raise _error(404, f"dataset '{name}' not found")
        return dataset

    # -- schema versions --------------------------------------------------

    @app.post("/datasets/{name}/versions", status_code=201)
    def create_schema_version(
        name: str, payload: SchemaVersionCreate
    ) -> dict[str, Any]:
        dataset = db.get_dataset_by_name(name)
        if dataset is None:
            raise _error(404, f"dataset '{name}' not found")
        seen: set[str] = set()
        for field in payload.fields:
            if not field.name.strip() or not field.type.strip():
                raise _error(400, "field name and type must not be empty")
            if field.name in seen:
                raise _error(400, f"duplicate field name '{field.name}'")
            seen.add(field.name)
        version = db.create_schema_version(
            dataset["id"], [f.model_dump() for f in payload.fields]
        )
        return {"dataset": name, **version}

    @app.get("/datasets/{name}/versions")
    def list_schema_versions(name: str) -> list[dict[str, Any]]:
        dataset = db.get_dataset_by_name(name)
        if dataset is None:
            raise _error(404, f"dataset '{name}' not found")
        return db.list_schema_versions(dataset["id"])

    @app.get("/datasets/{name}/versions/{version}")
    def get_schema_version(name: str, version: int) -> dict[str, Any]:
        dataset = db.get_dataset_by_name(name)
        if dataset is None:
            raise _error(404, f"dataset '{name}' not found")
        schema_version = db.get_schema_version(dataset["id"], version)
        if schema_version is None:
            raise _error(
                404, f"dataset '{name}' has no schema version {version}"
            )
        return {"dataset": name, **schema_version}

    # -- lineage ----------------------------------------------------------

    def _resolve_field(dataset_name: str, version: int, field: str) -> dict[str, Any]:
        dataset = db.get_dataset_by_name(dataset_name)
        if dataset is None:
            raise _error(404, f"dataset '{dataset_name}' not found")
        schema_version = db.get_schema_version(dataset["id"], version)
        if schema_version is None:
            raise _error(
                404, f"dataset '{dataset_name}' has no schema version {version}"
            )
        if field not in {f["name"] for f in schema_version["fields"]}:
            raise _error(
                404,
                f"field '{field}' not found in dataset '{dataset_name}' "
                f"version {version}",
            )
        return dataset

    @app.post("/lineage", status_code=201)
    def create_lineage(payload: LineageCreate) -> dict[str, Any]:
        if (
            payload.source_dataset == payload.target_dataset
            and payload.source_version == payload.target_version
            and payload.source_field == payload.target_field
        ):
            raise _error(400, "source and target must not be identical")
        target = _resolve_field(
            payload.target_dataset, payload.target_version, payload.target_field
        )
        source = _resolve_field(
            payload.source_dataset, payload.source_version, payload.source_field
        )
        try:
            edge = db.create_lineage_edge(
                target["id"],
                payload.target_version,
                payload.target_field,
                source["id"],
                payload.source_version,
                payload.source_field,
            )
        except DuplicateError:
            raise _error(409, "lineage edge already exists") from None
        return {**payload.model_dump(), **edge}

    @app.get("/lineage/{dataset_name}/{version}")
    def get_lineage(dataset_name: str, version: int) -> dict[str, Any]:
        dataset = db.get_dataset_by_name(dataset_name)
        if dataset is None:
            raise _error(404, f"dataset '{dataset_name}' not found")
        schema_version = db.get_schema_version(dataset["id"], version)
        if schema_version is None:
            raise _error(
                404, f"dataset '{dataset_name}' has no schema version {version}"
            )
        edges = db.get_lineage_for_version(dataset["id"], version)
        sources_by_field: dict[str, list[dict[str, Any]]] = {
            f["name"]: [] for f in schema_version["fields"]
        }
        for edge in edges:
            sources_by_field.setdefault(edge["target_field"], []).append(
                {
                    "dataset": edge["source_dataset"],
                    "version": edge["source_version"],
                    "field": edge["source_field"],
                }
            )
        return {
            "target_dataset": dataset_name,
            "target_version": version,
            "fields": [
                {"field": field_name, "sources": sources}
                for field_name, sources in sorted(sources_by_field.items())
            ],
        }

    return app


app = create_app()
