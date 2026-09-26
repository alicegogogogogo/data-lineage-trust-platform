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


# --------------------------------------------------------------------------- #
# Read-only schema version compatibility check
# --------------------------------------------------------------------------- #


class VersionBreakingChange(BaseModel):
    field: str
    kind: Literal["removed", "type_changed", "nullable_tightened"]
    # Null on the side where the field does not exist; never omitted.
    before: FieldChangeDefinition | None
    after: FieldChangeDefinition | None


class VersionCompatibilityResponse(BaseModel):
    base_version: int
    target_version: int
    breaking_changes: list[VersionBreakingChange]
    breaking_change_count: int


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


# The deletion response carries the complete mapping that was just removed,
# in the same shape as the registration response.
class LineageDeletedResponse(BaseModel):
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


# One audited field and the status of its current impact cache record:
# ``cached`` (the record matches a fresh recomputation), ``missing`` (the
# field was never cached) or ``mismatch`` (the record is stale or corrupt).
class LineageImpactCacheAuditEntry(BaseModel):
    field: str
    status: Literal["cached", "missing", "mismatch"]


# The three per-status totals, keyed by the status name plus ``_count``.
class LineageImpactCacheAuditCounts(BaseModel):
    cached_count: int
    missing_count: int
    mismatch_count: int


# Read-only consistency audit of one version's impact cache: exactly these
# keys, in this order. Entries are sorted by field name ascending and the
# document is serialized deterministically (compact JSON, trailing newline).
class LineageImpactCacheAuditResponse(BaseModel):
    dataset: str
    version: int
    entries: list[LineageImpactCacheAuditEntry]
    counts: LineageImpactCacheAuditCounts


# One repaired field and the action taken on its impact cache record:
# ``created`` (no record existed, one was inserted), ``updated`` (a stored
# record disagreed with the recomputation and was rewritten) or ``unchanged``
# (the stored record already matched and was left untouched).
class LineageImpactCacheRepairEntry(BaseModel):
    field: str
    action: Literal["created", "updated", "unchanged"]


# The three per-action totals, keyed by the action name plus ``_count``.
class LineageImpactCacheRepairCounts(BaseModel):
    created_count: int
    updated_count: int
    unchanged_count: int


# Controlled repair of one version's impact cache: exactly these keys, in
# this order. Entries are sorted by field name ascending and the document is
# serialized deterministically (compact JSON, trailing newline).
class LineageImpactCacheRepairResponse(BaseModel):
    dataset: str
    version: int
    entries: list[LineageImpactCacheRepairEntry]
    counts: LineageImpactCacheRepairCounts


# One impacted field together with the shortest lineage path from the source.
# ``path`` includes both ends; ``path_length`` is the number of path edges
# (1 for a direct downstream field).
class LineageImpactPathItem(BaseModel):
    dataset: str
    version: int
    field: str
    path: list[LineageSourceRef]
    path_length: int


# Deterministic shortest-path companion of the lineage impact query: exactly
# these keys, in this order.
class LineageImpactPathsResponse(BaseModel):
    source: LineageSourceRef
    impacts: list[LineageImpactPathItem]
    direct_count: int
    indirect_count: int


# Deterministic upstream companion of the lineage impact query: exactly these
# keys, in this order. Each origin carries the same located-field-plus-path
# shape as an impact path item.
class LineageSourcePathsResponse(BaseModel):
    source: LineageSourceRef
    origins: list[LineageImpactPathItem]
    direct_count: int
    indirect_count: int
    source_dataset_count: int


# --------------------------------------------------------------------------- #
# Read-only breaking-change compatibility check with downstream impact
# --------------------------------------------------------------------------- #


class VersionBreakingChangeImpact(BaseModel):
    field: str
    kind: Literal["removed", "type_changed", "nullable_tightened"]
    # Null on the side where the field does not exist; never omitted.
    before: FieldChangeDefinition | None
    after: FieldChangeDefinition | None
    # Direct and indirect downstream fields of the broken field, deduplicated
    # and sorted by dataset, version and field ascending.
    impacted: list[LineageSourceRef]


class VersionCompatibilityImpactResponse(BaseModel):
    base_version: int
    target_version: int
    breaking_changes: list[VersionBreakingChangeImpact]
    breaking_change_count: int


# --------------------------------------------------------------------------- #
# Read-only per-dataset adjacent-version evolution summary
# --------------------------------------------------------------------------- #


# One adjacent version pair of the dataset. ``impacted_datasets`` lists the
# distinct dataset names carrying a field impacted by the pair's breaking
# changes, sorted ascending.
class VersionEvolutionSummaryPair(BaseModel):
    base_version: int
    target_version: int
    breaking_count: int
    impacted_count: int
    impacted_datasets: list[str]


class VersionEvolutionSummaryTotals(BaseModel):
    pair_count: int
    breaking_count: int
    impacted_count: int


# Deterministic whole-dataset summary: exactly these keys, in this order.
class VersionEvolutionSummaryResponse(BaseModel):
    dataset: str
    pairs: list[VersionEvolutionSummaryPair]
    totals: VersionEvolutionSummaryTotals


# --------------------------------------------------------------------------- #
# Read-only per-field cross-version trajectory
# --------------------------------------------------------------------------- #


# One version's definition of the tracked field; ``definition`` is null when
# the field does not exist in that version and the key is never omitted.
class FieldTrajectoryEntry(BaseModel):
    version: int
    definition: FieldChangeDefinition | None


# The field's status change between two adjacent versions, together with the
# downstream impact computed with the same start-field rule as the
# compatibility impact response. ``impacted_datasets`` lists the distinct
# dataset names appearing in ``impacted``, sorted ascending.
class FieldStatusChange(BaseModel):
    base_version: int
    target_version: int
    status: Literal[
        "added",
        "removed",
        "type_changed",
        "nullable_tightened",
        "nullable_loosened",
        "unchanged",
    ]
    impacted: list[LineageSourceRef]
    impacted_datasets: list[str]


# Deterministic cross-version trajectory of one field: exactly these keys, in
# this order.
class FieldTrajectoryResponse(BaseModel):
    dataset: str
    field: str
    entries: list[FieldTrajectoryEntry]
    changes: list[FieldStatusChange]


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
# Quality gate: release readiness verdict over the persisted records
# --------------------------------------------------------------------------- #


QualityGateVerdict = Literal["pass", "undetermined", "fail"]

# 'violation' marks a rule with violations in the latest evaluation; the other
# kinds reuse the anomaly record literals.
QualityGateReasonKind = Literal["violation", "row_limit", "rule_limit", "trend"]


class QualityGateReason(BaseModel):
    kind: QualityGateReasonKind
    # History sequence of the evaluation the reason points to.
    sequence: int
    # Null when the reason is not tied to one rule (row_limit/trend records);
    # the key is never omitted.
    rule_id: int | None
    violation_count: int


class QualityGateCounts(BaseModel):
    evaluations: int
    anomalies: int
    reasons: int


class QualityGateResponse(BaseModel):
    dataset: str
    version: int
    verdict: QualityGateVerdict
    reasons: list[QualityGateReason]
    counts: QualityGateCounts


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


# Append-only audit of privacy-view masking hits. One record is written per
# field actually masked by a view request; ``sequence`` numbers the records of
# one version in write order.
class PrivacyViewAuditRecord(BaseModel):
    sequence: int
    field: str
    policy_id: int
    role: str
    masking: PrivacyMasking
    created_at: str


# One record per successful privacy-view request, independent of how many
# values it masked. ``sequence`` numbers the records of one version in write
# order; ``row_count`` is the number of submitted rows and ``masked_count``
# the number of masking-hit records the same view wrote, so the two logs
# cross-check.
class PrivacyViewAccessRecord(BaseModel):
    sequence: int
    role: str
    row_count: int
    masked_count: int
    created_at: str


# Read-only roll-up of the masking-hit records: one group per distinct
# (field, policy, role, masking) combination, with the number of hits and the
# timestamps of the group's earliest and latest written records. Recomputed
# from the persisted records on every read; nothing is cached or written.
class PrivacyViewAuditSummaryGroup(BaseModel):
    field: str
    policy_id: int
    role: str
    masking: PrivacyMasking
    hit_count: int
    first_hit_at: str
    last_hit_at: str


class PrivacyViewAuditSummaryResponse(BaseModel):
    dataset: str
    version: int
    groups: list[PrivacyViewAuditSummaryGroup]


# Read-only day-over-day comparison of the masking-hit records: the records of
# the two most recent UTC calendar days are grouped by
# (field, policy, role, masking) and each side reports its hit count and the
# timestamps of the group's earliest and latest records of that day. A group
# present on only one side keeps a null ``before``/``after`` for the missing
# side. Recomputed from the persisted records on every read; nothing is cached
# or written.
class PrivacyViewAuditDiffSide(BaseModel):
    hit_count: int
    first_hit_at: str
    last_hit_at: str


class PrivacyViewAuditDiffGroup(BaseModel):
    field: str
    policy_id: int
    role: str
    masking: PrivacyMasking
    kind: Literal["added", "removed", "changed"]
    before: PrivacyViewAuditDiffSide | None
    after: PrivacyViewAuditDiffSide | None
    hit_count_delta: int


class PrivacyViewAuditDiffResponse(BaseModel):
    from_period: str | None
    to_period: str | None
    groups: list[PrivacyViewAuditDiffGroup]


# Read-only per-day reconciliation of the access records against the
# masking-hit records: both logs are bucketed by the UTC calendar day of
# their write time and each day with records on either side reports the
# access-record count, the summed masked-value count and the hit-record
# count, plus whether the masked-value count matches the hit count. Days
# sort ascending by date; ``totals`` sums the same counts over the whole
# version. Recomputed from the persisted records on every read; nothing is
# cached or written.
class PrivacyViewAuditReconcileDay(BaseModel):
    day: str
    view_count: int
    masked_count: int
    hit_count: int
    consistent: bool


class PrivacyViewAuditReconcileTotals(BaseModel):
    view_count: int
    masked_count: int
    hit_count: int


class PrivacyViewAuditReconcileResponse(BaseModel):
    dataset: str
    version: int
    days: list[PrivacyViewAuditReconcileDay]
    totals: PrivacyViewAuditReconcileTotals


# Read-only hit trend aggregated by privacy policy: hits of one policy merge
# across roles (a disabled policy's historical hits still count) and are
# bucketed by the UTC calendar day of their write time. Each listed day reports
# its record count, the change against the previously listed day (null on the
# first day) and the up/down/flat/none direction. Recomputed from the
# persisted records on every read; nothing is cached or written.
class PrivacyViewAuditTrendDay(BaseModel):
    day: str
    hit_count: int
    hit_count_delta: int | None
    trend: Literal["up", "down", "flat", "none"]


class PrivacyViewAuditTrendPolicy(BaseModel):
    policy_id: int
    field: str
    classification: str
    masking: PrivacyMasking
    total_hits: int
    days: list[PrivacyViewAuditTrendDay]


class PrivacyViewAuditTrendTotals(BaseModel):
    total_hits: int
    policy_count: int
    day_count: int


class PrivacyViewAuditTrendResponse(BaseModel):
    dataset: str
    version: int
    policies: list[PrivacyViewAuditTrendPolicy]
    totals: PrivacyViewAuditTrendTotals


# Two-stage preview/confirm cleanup of masking-hit records. A request is
# created with a reason and a timezone-bearing ``before`` cutoff; creating one
# only previews the target set (records whose hit time is earlier than the
# cutoff) and never deletes anything. The target set is fixed at creation, so
# the preview block is stored with the request and stays identical after a
# later confirmation removes the records. Creation bodies are parsed from raw
# bytes in the repository (rather than through a Pydantic request model) so
# that unknown dataset/version keeps its 404 precedence over body-level 422s.
class PrivacyViewAuditCleanupPreview(BaseModel):
    hit_count: int
    first_hit_at: str | None
    last_hit_at: str | None
    fields: list[str]


class PrivacyViewAuditCleanupRequest(BaseModel):
    id: int
    reason: str
    before: str
    status: Literal["pending", "confirmed"]
    created_at: str
    preview: PrivacyViewAuditCleanupPreview


# Confirmation freezes the request: the response adds the confirmation time
# and the number of records actually deleted. Re-confirming a confirmed
# request is a 409 and never touches data again.
class ConfirmedPrivacyViewAuditCleanupRequest(PrivacyViewAuditCleanupRequest):
    confirmed_at: str
    deleted_count: int


# --------------------------------------------------------------------------- #
# Read-only cross-version privacy compliance export
# --------------------------------------------------------------------------- #


# One version's registered privacy policy as it appears in the export, sorted
# by policy ``id`` ascending. Carries the policy number plus its field name,
# classification, masking, allowed roles and enabled state.
class PrivacyComplianceExportPolicy(BaseModel):
    id: int
    field: str
    classification: str
    masking: PrivacyMasking
    allowed_roles: list[str]
    enabled: bool


# One version's cleanup request as it appears in the export, sorted by request
# ``id`` ascending. Pending and confirmed requests are both kept; the status
# field distinguishes them. Only the number, reason, status and creation time
# are exported.
class PrivacyComplianceExportCleanupRequest(BaseModel):
    id: int
    reason: str
    status: Literal["pending", "confirmed"]
    created_at: str


# One schema version's compliance summary. Exactly these keys, in this order;
# the three counters are computed from the records surviving any confirmed
# cleanup.
class PrivacyComplianceExportVersion(BaseModel):
    version: int
    policies: list[PrivacyComplianceExportPolicy]
    hit_count: int
    masked_count: int
    view_count: int
    cleanup_requests: list[PrivacyComplianceExportCleanupRequest]


class PrivacyComplianceExportTotals(BaseModel):
    policy_count: int
    hit_count: int
    masked_count: int
    view_count: int
    cleanup_request_count: int


# Deterministic whole-dataset export: exactly these keys, in this order.
class PrivacyComplianceExportResponse(BaseModel):
    dataset: str
    versions: list[PrivacyComplianceExportVersion]
    totals: PrivacyComplianceExportTotals


# --------------------------------------------------------------------------- #
# Sensitive-field identification (candidate annotation only)
# --------------------------------------------------------------------------- #


# A sample may only be a JSON scalar (string, number, boolean or null); objects
# and arrays reject the whole request. Strict members never coerce, so ``true``
# is not mistaken for the number 1.
SensitiveSample = StrictStr | StrictInt | StrictFloat | StrictBool | None


class SensitiveIdentificationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: StrictStr = Field(description="Existing field of the schema version")
    samples: list[SensitiveSample] = Field(
        description="Scalar sample values; may be empty. Objects and arrays are "
        "rejected; the submitted samples are never stored or echoed back",
    )


# Hit kinds are emitted in fixed order: name hits before sample hits, each
# group following the sensitive-word order (email, phone, id_card, password,
# token, birth). Sample hits exist only for email and phone.
SensitiveEvidenceKind = Literal[
    "name:email",
    "name:phone",
    "name:id_card",
    "name:password",
    "name:token",
    "name:birth",
    "sample:email",
    "sample:phone",
]


class SensitiveIdentification(BaseModel):
    id: int
    field: str
    # Declared type of the field, taken from the schema version; sample
    # matching runs only when this is "string".
    field_type: str
    # Ordered, de-duplicated hit kinds; empty when neither name nor samples hit
    # (the record is still generated, with confidence "none").
    evidence: list[SensitiveEvidenceKind]
    confidence: Literal["high", "medium", "low", "none"]
    # Reference to the identified field: its dataset, version and field name.
    source: LineageSourceRef
    created_at: str


# Advisory masking-strategy suggestions derived from the identification
# records. They never create or modify a privacy policy; the privacy view
# keeps masking according to registered policies only.
MaskingSuggestionClassification = Literal["PII", "CREDENTIAL"]


class MaskingSuggestion(BaseModel):
    field: str
    classification: MaskingSuggestionClassification
    # Same vocabulary as the privacy view ("partial"/"redact"); a suggestion
    # introduces no new masking format.
    masking: PrivacyMasking
    # Always empty: a candidate grants no role an unmasked view, so an empty
    # list means the suggested masking applies to every role.
    allowed_roles: list[str]


class MaskingSuggestionsRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Non-empty array of field names; StrictStr rejects non-string items at the
    # request layer. Blank-after-trimming names and duplicates are semantic
    # checks in the repository, after the path dataset/version resolves, so an
    # unknown dataset/version keeps its 404 precedence.
    fields: list[StrictStr] = Field(
        min_length=1,
        description="Distinct, non-empty names of existing version fields that "
        "each currently have a masking suggestion candidate",
    )


# --------------------------------------------------------------------------- #
# Read-only cross-version privacy policy coverage check
# --------------------------------------------------------------------------- #


# One field's policy coverage as it appears in the check, sorted by field
# name. ``coverage`` is "enabled" when an enabled policy is registered for the
# field, "disabled" when the registered policy is disabled and "unregistered"
# when the field has no policy; the registered policy's classification,
# masking and enabled state are reported alongside and are all null when no
# policy is registered.
class PrivacyPolicyCoverageField(BaseModel):
    field: str
    coverage: Literal["enabled", "disabled", "unregistered"]
    classification: str | None
    masking: PrivacyMasking | None
    enabled: bool | None


# Advisory candidate for a field that was identified with at least one name or
# sample hit but carries no privacy policy yet, sorted by identification
# record id ascending. Only the field name and the suggested classification
# and masking are reported; a candidate never registers a policy.
class PrivacyPolicyCoverageCandidate(BaseModel):
    field: str
    classification: MaskingSuggestionClassification
    masking: PrivacyMasking


# One schema version's coverage summary. Exactly these keys, in this order.
class PrivacyPolicyCoverageVersion(BaseModel):
    version: int
    fields: list[PrivacyPolicyCoverageField]
    candidates: list[PrivacyPolicyCoverageCandidate]


class PrivacyPolicyCoverageTotals(BaseModel):
    version_count: int
    field_count: int
    enabled_count: int
    disabled_count: int
    unregistered_count: int
    candidate_count: int


# Deterministic whole-dataset coverage check: exactly these keys, in this
# order.
class PrivacyPolicyCoverageResponse(BaseModel):
    dataset: str
    versions: list[PrivacyPolicyCoverageVersion]
    totals: PrivacyPolicyCoverageTotals


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
