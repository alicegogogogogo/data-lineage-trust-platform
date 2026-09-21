"""Pydantic models for the dataset, schema-version, lineage and quality-rule APIs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool


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


# --------------------------------------------------------------------------- #
# Quality rules
# --------------------------------------------------------------------------- #


class QualityRuleCreate(BaseModel):
    name: str = Field(description="Unique, non-empty rule name within the version")
    kind: str = Field(description="One of not_null, numeric_range, unique")
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="Kind-specific rule parameters"
    )


class QualityRule(BaseModel):
    id: int
    name: str
    kind: str
    parameters: dict[str, Any]
    enabled: bool
    created_at: str


class QualityRuleUpdate(BaseModel):
    enabled: StrictBool


class QualityEvaluateRequest(BaseModel):
    rows: list[dict[str, Any]] = Field(
        description="Row objects to evaluate against the enabled rules"
    )


class QualityEvaluationItem(BaseModel):
    rule_id: int
    name: str
    passed: bool
    violations: list[int]


class QualityEvaluationResponse(BaseModel):
    dataset: str
    version: int
    results: list[QualityEvaluationItem]
