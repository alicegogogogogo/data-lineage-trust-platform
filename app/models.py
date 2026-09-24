"""Pydantic models for the dataset, schema-version and lineage APIs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr


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
# Read-only schema version diff
# --------------------------------------------------------------------------- #


class FieldChangeDefinition(BaseModel):
    type: str
    nullable: bool


class VersionFieldChange(BaseModel):
    field: str
    kind: Literal["added", "removed", "changed"]
    # Null on the side where the field does not exist; never omitted.
    before: FieldChangeDefinition | None
    after: FieldChangeDefinition | None


class VersionDiffResponse(BaseModel):
    from_version: int
    to_version: int
    compatible: bool
    changes: list[VersionFieldChange]


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


# --------------------------------------------------------------------------- #
# Quality rule evaluation history and diff
# --------------------------------------------------------------------------- #


class QualityRuleEvaluationRecord(BaseModel):
    sequence: int
    dataset: str
    version: int
    row_count: int
    violation_row_count: int
    results: list[QualityRuleResult]
    created_at: str


class QualityRuleEvaluationSide(BaseModel):
    violation_count: int
    violations: list[int]


class QualityRuleEvaluationRuleDiff(BaseModel):
    rule_id: int
    name: str
    # Null on the side whose evaluation has no result for this rule (e.g. the
    # rule was disabled or did not exist yet); never omitted.
    before: QualityRuleEvaluationSide | None
    after: QualityRuleEvaluationSide | None
    # Null exactly when a side is missing: a row-level diff needs both sides.
    added_violations: list[int] | None
    removed_violations: list[int] | None
    violation_count_delta: int | None


class QualityRuleEvaluationDiffResponse(BaseModel):
    dataset: str
    version: int
    # Null (with empty lists) when fewer than two evaluations are recorded.
    from_sequence: int | None
    to_sequence: int | None
    added_violation_rows: list[int]
    removed_violation_rows: list[int]
    rules: list[QualityRuleEvaluationRuleDiff]


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None


# --------------------------------------------------------------------------- #
# Quality anomaly detection over the evaluation history
# --------------------------------------------------------------------------- #


class QualityAnomalyDetectionConfigCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consecutive_worsening_steps: StrictInt = Field(
        ge=2,
        description="Consecutive strictly increasing steps that judge a "
        "worsening trend",
    )
    violation_row_limit: StrictInt = Field(
        ge=0,
        description="Maximum tolerated distinct violating rows of one evaluation",
    )
    rule_violation_limit: StrictInt = Field(
        ge=0,
        description="Maximum tolerated violating rows of a single rule",
    )


class QualityAnomalyDetectionConfig(BaseModel):
    id: int
    dataset: str
    version: int
    consecutive_worsening_steps: int
    violation_row_limit: int
    rule_violation_limit: int
    created_at: str


QualityAnomalyKind = Literal["row_limit", "rule_limit", "trend"]


class QualityAnomalyRecord(BaseModel):
    id: int
    kind: QualityAnomalyKind
    # History sequence of the evaluation the record points to.
    sequence: int
    # Null except for 'rule_limit' records.
    rule_id: int | None
    violation_count: int
    created_at: str


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
# Sensitive field identification (candidate annotations only)
# --------------------------------------------------------------------------- #


# A sample value must be a JSON scalar: string, number, boolean or null.
# Objects and arrays are rejected by the request model before any write.
SensitiveSample = StrictStr | StrictBool | StrictInt | StrictFloat | None


class SensitiveIdentificationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: StrictStr = Field(description="Existing field of the schema version")
    samples: list[SensitiveSample] = Field(
        description="Scalar sample values of the field; may be empty"
    )


# A single piece of evidence supporting (or not) an identification. Name hits
# always precede sample hits in the ordered list.
class SensitiveEvidence(BaseModel):
    # How the hit was obtained: a sensitive word in the field name, or a
    # pattern observed in a sample value.
    kind: Literal["name", "sample"]
    # Sensitive category that was matched, e.g. "email" or "phone".
    category: str
    # The sample value that produced a sample hit; null for a name hit.
    value: str | None = None


# High when both name and sample hits exist, medium for samples only, low for
# a name hit only and none when neither side matched.
SensitiveConfidence = Literal["high", "medium", "low", "none"]


class SensitiveIdentification(BaseModel):
    # Per-version number in first-submission order; stable across refreshes.
    sequence: int
    dataset: str
    version: int
    field: str
    # Name hits first, then sample hits.
    evidence: list[SensitiveEvidence]
    confidence: SensitiveConfidence
    # The schema field the identification is about.
    source: LineageSourceRef
    created_at: str


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


# Sentinel default for ProcessingRunBatchCompleteItem.error: unlike the
# single-run finish payload, the batch item must distinguish an omitted error
# field from an explicitly supplied one, because a success item may only omit
# the field — writing it at all (even as null) is a 422.
ERROR_FIELD_UNSET: Any = object()


class ProcessingRunBatchCompleteItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: StrictInt
    run_id: StrictInt
    status: Literal["succeeded", "failed"]
    # Omitted for success, a non-empty message for failure. An explicitly
    # supplied value (null included) stays distinguishable from omission via
    # the sentinel default; the success/failure rules are enforced in the
    # repository after the path resolves so 404 precedence is preserved.
    error: StrictStr | None = ERROR_FIELD_UNSET


class ProcessingRunBatchCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs: list[ProcessingRunBatchCompleteItem] = Field(
        min_length=1,
        description="Non-empty batch of task/run completions for the path version",
    )


class ProcessingRunBatchCompleteResponse(BaseModel):
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
