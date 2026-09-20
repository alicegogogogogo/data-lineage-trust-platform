"""Pydantic models for the dataset, schema-version and lineage APIs."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DatasetCreate(BaseModel):
    name: str = Field(description="Unique, non-empty dataset name")
    description: str = Field(default="", description="Optional human description")


class Dataset(BaseModel):
    id: int
    name: str
    description: str
    created_at: str


class FieldSpec(BaseModel):
    name: str = Field(description="Unique, non-empty field name within the version")
    type: str = Field(description="Field type, e.g. 'string' or 'integer'")
    nullable: bool


class SchemaVersionCreate(BaseModel):
    fields: list[FieldSpec] = Field(description="Non-empty list of field definitions")


class FieldInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    type: str
    nullable: bool


class SchemaVersion(BaseModel):
    dataset_id: int
    dataset_name: str
    version: int
    created_at: str
    fields: list[FieldInfo]


class LineageCreate(BaseModel):
    target_dataset: str
    target_version: int
    target_field: str
    source_dataset: str
    source_version: int
    source_field: str


class LineageSourceRef(BaseModel):
    dataset: str
    version: int
    field: str


class TargetFieldLineage(BaseModel):
    target_field: str
    sources: list[LineageSourceRef]


class LineageCreatedResponse(BaseModel):
    target_dataset: str
    target_version: int
    target_field: str
    source: LineageSourceRef


class LineageResponse(BaseModel):
    target_dataset: str
    target_version: int
    fields: list[TargetFieldLineage]


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
