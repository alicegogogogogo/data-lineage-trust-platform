"""Pydantic models for the dataset, schema-version and lineage APIs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    field_validator,
)


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


# --------------------------------------------------------------------------- #
# Quality rules
# --------------------------------------------------------------------------- #


QualityRuleKind = Literal["not_null", "numeric_range", "unique"]


class QualityRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Unique rule name within the version")
    kind: QualityRuleKind
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Kind-specific parameters: {'field': ...}, "
        "{'field': ..., 'min': ..., 'max': ...} or {'fields': [...]}",
    )


class QualityRule(BaseModel):
    id: int
    name: str
    kind: QualityRuleKind
    params: dict[str, Any]
    enabled: bool
    created_at: str


class QualityRuleEnabledUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


class QualityRuleEvaluateRequest(BaseModel):
    rows: list[dict[str, Any]] = Field(
        description="Rows to check; only enabled rules are executed"
    )


class QualityRuleResult(BaseModel):
    rule_id: int
    name: str
    passed: bool
    violations: list[int]


class QualityRuleEvaluateResponse(BaseModel):
    dataset: str
    version: int
    results: list[QualityRuleResult]


# --------------------------------------------------------------------------- #
# Privacy policies
# --------------------------------------------------------------------------- #


PrivacyMasking = Literal["redact", "partial"]


class PrivacyPolicyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: StrictStr = Field(description="Existing field of the schema version")
    classification: StrictStr = Field(
        description="Non-empty classification label, e.g. 'pii'"
    )
    masking: PrivacyMasking
    allowed_roles: list[StrictStr] = Field(
        description="Roles allowed to see the unmasked value; may be empty"
    )

    @field_validator("classification")
    @classmethod
    def _classification_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("classification must be a non-empty string")
        return value

    @field_validator("allowed_roles")
    @classmethod
    def _roles_non_empty_and_unique(cls, value: list[str]) -> list[str]:
        if any(not role.strip() for role in value):
            raise ValueError("allowed_roles must contain only non-empty strings")
        if len(set(value)) != len(value):
            raise ValueError("allowed_roles must not contain duplicates")
        return value


class PrivacyPolicy(BaseModel):
    id: int
    field: str
    classification: str
    masking: PrivacyMasking
    allowed_roles: list[str]
    enabled: bool
    created_at: str


class PrivacyPolicyEnabledUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


class PrivacyViewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: StrictStr = Field(description="Non-empty viewing role")
    rows: list[dict[str, Any]] = Field(description="Rows to mask for this role")

    @field_validator("role")
    @classmethod
    def _role_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("role must be a non-empty string")
        return value


class PrivacyViewResponse(BaseModel):
    dataset: str
    version: int
    rows: list[dict[str, Any]]


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
