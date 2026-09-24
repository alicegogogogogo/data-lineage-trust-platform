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


# --------------------------------------------------------------------------- #
# Schema version diff (read-only)
# ---------------------------------------------------------------------------


class SchemaFieldDefinition(BaseModel):
    type: str
    nullable: bool


class SchemaVersionChange(BaseModel):
    field: str
    kind: Literal["added", "removed", "changed"]
    # Exactly one side is null for added/removed; both are present for changed.
    before: SchemaFieldDefinition | None
    after: SchemaFieldDefinition | None


class SchemaVersionDiffResponse(BaseModel):
    from_version: int
    to_version: int
    compatible: bool
    changes: list[SchemaVersionChange]


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


class LineageImpactResponse(BaseModel):
    source: LineageSourceRef
    impacted: list[LineageSourceRef]


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
# Retention policies and lineage-aware snapshot deletion
# --------------------------------------------------------------------------- #


class RetentionPolicyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retention_days: StrictInt = Field(
        ge=0, description="Non-negative minimum snapshot age in days"
    )


class RetentionPolicy(BaseModel):
    id: int
    dataset: str
    version: int
    retention_days: int
    created_at: str


class SnapshotDeletionRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: StrictStr = Field(description="Non-empty reason for the requested deletion")


class SnapshotDeletionRequest(BaseModel):
    id: int
    snapshot_id: int
    policy_id: int
    reason: str
    status: Literal["pending", "blocked", "confirmed"]
    impacted: list[LineageSourceRef]
    created_at: str


class ConfirmedSnapshotDeletionRequest(SnapshotDeletionRequest):
    confirmed_at: str


# --------------------------------------------------------------------------- #
# Retention exceptions (compliance holds blocking snapshot deletion)
# --------------------------------------------------------------------------- #


RetentionExceptionScope = Literal["version", "snapshot"]


class RetentionExceptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: RetentionExceptionScope
    snapshot_id: StrictInt | None = Field(
        description="Null for scope 'version'; an existing snapshot id of this "
        "version for scope 'snapshot'"
    )
    reason: StrictStr = Field(description="Non-empty justification for the hold")
    expires_at: StrictStr = Field(
        description="Future ISO-8601 date-time including a timezone"
    )


class RetentionException(BaseModel):
    id: int
    dataset: str
    version: int
    scope: RetentionExceptionScope
    snapshot_id: int | None
    reason: str
    expires_at: str
    status: Literal["active", "expired", "released"]
    created_at: str
    released_at: str | None


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


class ProcessingRunCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: StrictStr = Field(
        description="Non-empty reason for cancelling the run; trimmed before storing"
    )


class ProcessingTaskDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Non-optional with a default: omission yields 1, while an explicit null is
    # rejected like any other non-integer ("given" values must be positive ints).
    limit: StrictInt = Field(
        default=1,
        ge=1,
        description="Maximum number of tasks to start; defaults to 1 when omitted",
    )


class ProcessingTaskDispatchResponse(BaseModel):
    dataset: str
    version: int
    runs: list[ProcessingTaskRun]


class ProcessingTaskDependenciesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    depends_on: list[StrictInt] = Field(
        description="Replacement list of ids of tasks in the same version; may be empty"
    )


ScheduleState = Literal[
    "running", "succeeded", "retryable", "exhausted", "ready", "blocked",
    "upstream_failed",
]


class ScheduledTask(ProcessingTask):
    schedule_state: ScheduleState
    blocking_task_ids: list[int]


class ProcessingScheduleResponse(BaseModel):
    dataset: str
    version: int
    tasks: list[ScheduledTask]


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


# --------------------------------------------------------------------------- #
# Read-only per-version processing audit report
# --------------------------------------------------------------------------- #


class AuditChainProof(BaseModel):
    valid: bool
    checked_count: int
    # Stored evidence hash of the chain's final record; null only when the
    # chain is empty (it is still reported when valid is false).
    last_evidence_hash: str | None


class AuditReportRun(ProcessingTaskRun):
    proof: AuditChainProof


class AuditReportTask(ProcessingTask):
    runs: list[AuditReportRun]


class ProcessingAuditReportSummary(BaseModel):
    task_count: int
    run_count: int
    pending_tasks: int
    running_tasks: int
    succeeded_tasks: int
    failed_tasks: int
    # Subset of failed_tasks whose attempt budget is used up.
    exhausted_tasks: int
    # Runs whose audit chain failed re-verification.
    invalid_audit_runs: int


class ProcessingAuditReportResponse(BaseModel):
    dataset: str
    version: int
    summary: ProcessingAuditReportSummary
    tasks: list[AuditReportTask]
