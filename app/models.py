"""Pydantic models for the dataset, schema-version and lineage APIs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr


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


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None


# --------------------------------------------------------------------------- #
# Privacy policies
# --------------------------------------------------------------------------- #


PrivacyMasking = Literal["redact", "partial"]


class PrivacyPolicyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: StrictStr = Field(description="Existing field of the schema version")
    classification: StrictStr = Field(description="Non-empty sensitivity classification")
    masking: PrivacyMasking
    allowed_roles: list[StrictStr] = Field(
        description="Distinct, non-empty role names that see the raw value; may be empty"
    )


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

    role: StrictStr
    rows: list[dict[str, Any]]


class PrivacyViewResponse(BaseModel):
    dataset: str
    version: int
    rows: list[dict[str, Any]]


# --------------------------------------------------------------------------- #
# Row snapshots
# --------------------------------------------------------------------------- #


class SnapshotCreate(BaseModel):
    rows: list[dict[str, Any]] = Field(
        description="Row objects to persist; values and order are stored verbatim"
    )


class SnapshotMetadata(BaseModel):
    id: int
    dataset: str
    version: int
    created_at: str
    row_count: int


class SnapshotResponse(SnapshotMetadata):
    rows: list[dict[str, Any]]


class SnapshotDiffEntry(BaseModel):
    row: dict[str, Any]
    count: int


class SnapshotDiffResponse(BaseModel):
    from_snapshot_id: int
    to_snapshot_id: int
    added: list[SnapshotDiffEntry]
    removed: list[SnapshotDiffEntry]


# --------------------------------------------------------------------------- #
# Processing tasks
# --------------------------------------------------------------------------- #


class ProcessingTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: StrictStr = Field(description="Unique, non-empty task name within the version")
    depends_on: list[StrictInt] = Field(
        default_factory=list,
        description="Ids of tasks in the same version that must succeed first",
    )
    max_attempts: StrictInt = Field(
        default=1, ge=1, description="Maximum number of runs allowed for the task"
    )


class ProcessingTask(BaseModel):
    id: int
    dataset: str
    version: int
    name: str
    depends_on: list[int]
    max_attempts: int
    status: Literal["pending", "running", "succeeded", "failed"]
    attempt_count: int
    created_at: str


class ProcessingTaskRun(BaseModel):
    id: int
    task_id: int
    attempt: int
    status: Literal["running", "succeeded", "failed"]
    started_at: str
    finished_at: str | None
    error: str | None


class ProcessingTaskWithRuns(ProcessingTask):
    runs: list[ProcessingTaskRun]


class ProcessingRunFinish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "failed"]
    error: StrictStr | None = None


# --------------------------------------------------------------------------- #
# Processing task run audit records
# --------------------------------------------------------------------------- #


class AuditRecordCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: StrictStr = Field(description="Non-empty name of the audited event")
    input_summary: StrictStr = Field(description="Non-empty summary of the input")
    result_summary: StrictStr = Field(description="Non-empty summary of the result")


class AuditRecord(BaseModel):
    id: int
    sequence: int
    event: str
    input_summary: str
    result_summary: str
    run_status: Literal["running", "succeeded", "failed"]
    previous_hash: str | None
    evidence_hash: str
    created_at: str


class AuditChainVerifyResponse(BaseModel):
    dataset: str
    version: int
    task_id: int
    run_id: int
    valid: bool
    checked_count: int
