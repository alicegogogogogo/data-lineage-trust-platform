"""HTTP API for datasets, immutable schema versions and field lineage."""

from __future__ import annotations

import sqlite3

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import repository
from app.db import get_db
from app.errors import APIError, RequestInvalidError
from app.models import (
    Dataset,
    DatasetCreate,
    LineageCreate,
    LineageCreatedResponse,
    LineageResponse,
    SchemaVersion,
    SchemaVersionCreate,
)

app = FastAPI(title="Data Lineage Trust Platform", version="0.1.0")


@app.exception_handler(APIError)
def handle_api_error(request: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": exc.message},
    )


@app.exception_handler(RequestValidationError)
def handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    missing: list[str] = []
    invalid: list[str] = []
    malformed_json = False
    for error in exc.errors():
        if error.get("type") in ("json_invalid", "json_decode"):
            malformed_json = True
            continue
        loc = [str(part) for part in error.get("loc", ()) if part != "body"]
        path = ".".join(loc)
        if error.get("type") == "missing":
            missing.append(path or "body")
        else:
            invalid.append(path or "body")
    if malformed_json and not missing and not invalid:
        message = "Request body is not valid JSON"
    elif missing:
        message = (
            "Request body is incomplete: missing field(s) "
            + ", ".join(sorted(set(missing)))
        )
    elif invalid:
        message = (
            "The request payload is invalid for field(s) "
            + ", ".join(sorted(set(invalid)))
        )
    else:
        message = "The request payload is invalid"
    return JSONResponse(
        status_code=422,
        content={"error": "validation_error", "detail": message},
    )


@app.exception_handler(sqlite3.Error)
def handle_sqlite_error(request: Request, exc: sqlite3.Error) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "detail": "An internal error occurred"},
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Datasets
# --------------------------------------------------------------------------- #


@app.post("/datasets", response_model=Dataset, status_code=201)
def create_dataset(
    payload: DatasetCreate,
    conn=Depends(get_db),
) -> Dataset:
    return Dataset(**repository.create_dataset(conn, payload.name, payload.description))


@app.get("/datasets", response_model=list[Dataset])
def list_datasets(conn=Depends(get_db)) -> list[Dataset]:
    return [Dataset(**row) for row in repository.list_datasets(conn)]


# --------------------------------------------------------------------------- #
# Schema versions
# --------------------------------------------------------------------------- #


@app.get("/datasets/{dataset_name}/versions", response_model=list[SchemaVersion])
def list_schema_versions(
    dataset_name: str, conn=Depends(get_db)
) -> list[SchemaVersion]:
    return [
        SchemaVersion(**version)
        for version in repository.list_schema_versions(conn, dataset_name)
    ]


@app.post(
    "/datasets/{dataset_name}/versions",
    response_model=SchemaVersion,
    status_code=201,
)
def create_schema_version(
    dataset_name: str,
    payload: SchemaVersionCreate,
    conn=Depends(get_db),
) -> SchemaVersion:
    version = repository.create_schema_version(conn, dataset_name, payload.fields)
    return SchemaVersion(**version)


@app.get("/datasets/{dataset_name}/versions/{version}", response_model=SchemaVersion)
def get_schema_version(
    dataset_name: str, version: int, conn=Depends(get_db)
) -> SchemaVersion:
    return SchemaVersion(
        **repository.get_schema_version(conn, dataset_name, version)
    )


# --------------------------------------------------------------------------- #
# Lineage
# --------------------------------------------------------------------------- #


@app.post(
    "/datasets/{dataset_name}/versions/{version}/lineage",
    response_model=LineageCreatedResponse,
    status_code=201,
)
def create_lineage(
    dataset_name: str,
    version: int,
    payload: LineageCreate,
    conn=Depends(get_db),
) -> LineageCreatedResponse:
    # The path identifies the target; require it to agree with the body so the
    # mapping has a single unambiguous target.
    if payload.target_dataset != dataset_name or payload.target_version != version:
        raise RequestInvalidError(
            "Target dataset and version in the body must match the request path"
        )
    created = repository.create_lineage_link(
        conn,
        target_dataset=payload.target_dataset,
        target_version=payload.target_version,
        target_field=payload.target_field,
        source_dataset=payload.source_dataset,
        source_version=payload.source_version,
        source_field=payload.source_field,
    )
    return LineageCreatedResponse(**created)


@app.get(
    "/datasets/{dataset_name}/versions/{version}/lineage",
    response_model=LineageResponse,
)
def get_lineage(
    dataset_name: str, version: int, conn=Depends(get_db)
) -> LineageResponse:
    return LineageResponse(
        **repository.get_lineage(conn, dataset_name, version)
    )
