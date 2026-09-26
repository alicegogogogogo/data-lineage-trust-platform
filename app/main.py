"""HTTP API for datasets, immutable schema versions and field lineage."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import repository
from app.db import get_db
from app.errors import APIError, ConflictError, RequestInvalidError
from app.models import (
    AuditChainVerifyResponse,
    AuditRecord,
    AuditRecordCreate,
    Dataset,
    DatasetCreate,
    FieldTrajectoryResponse,
    LineageCreate,
    LineageCreatedResponse,
    LineageDeletedResponse,
    LineageImpactCacheAuditResponse,
    LineageImpactCacheRepairResponse,
    LineageImpactResponse,
    LineageImpactPathsResponse,
    LineageResponse,
    LineageSourcePathsResponse,
    PrivacyComplianceExportResponse,
    PrivacyPolicyCoverageResponse,
    MaskingSuggestion,
    MaskingSuggestionsRegisterRequest,
    PrivacyPolicy,
    PrivacyPolicyCreate,
    PrivacyPolicyEnabledUpdate,
    PrivacyViewAccessRecord,
    PrivacyViewAuditRecord,
    PrivacyViewAuditDiffResponse,
    PrivacyViewAuditReconcileResponse,
    PrivacyViewAuditSummaryResponse,
    PrivacyViewAuditTrendResponse,
    PrivacyViewAuditCleanupRequest,
    ConfirmedPrivacyViewAuditCleanupRequest,
    PrivacyViewRequest,
    PrivacyViewResponse,
    ProcessingRunCancel,
    ProcessingRunFinish,
    ProcessingRunBatchCompleteRequest,
    ProcessingRunBatchCompleteResponse,
    ProcessingTask,
    ProcessingTaskCreate,
    ProcessingTaskDependenciesUpdate,
    ProcessingTaskDispatchRequest,
    ProcessingTaskDispatchResponse,
    ProcessingTaskRun,
    ProcessingTaskWithRuns,
    ProcessingScheduleResponse,
    ProcessingAuditReportResponse,
    QualityAnomalyDetectionConfig,
    QualityAnomalyDetectionConfigCreate,
    QualityAnomalyRecord,
    QualityRule,
    QualityRuleCreate,
    QualityRuleEnabledUpdate,
    QualityRuleEvaluateRequest,
    QualityRuleEvaluateResponse,
    QualityRuleEvaluationDiffResponse,
    QualityRuleEvaluationRecord,
    QualityGateResponse,
    RetentionPolicy,
    RetentionPolicyCreate,
    RetentionException,
    RetentionExceptionCreate,
    SensitiveIdentification,
    SensitiveIdentificationCreate,
    SchemaVersion,
    SchemaVersionCreate,
    SnapshotCreate,
    SnapshotDeletionRequest,
    ConfirmedSnapshotDeletionRequest,
    SnapshotDeletionRequestCreate,
    SnapshotAtDiffResponse,
    CrossVersionSnapshotDiffResponse,
    SnapshotDiffResponse,
    SnapshotMaskedViewResponse,
    SnapshotMetadata,
    SnapshotResponse,
    VersionCompatibilityResponse,
    VersionCompatibilityImpactResponse,
    VersionDiffResponse,
    VersionEvolutionSummaryResponse,
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


async def _read_request_body(request: Request) -> bytes:
    return await request.body()


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


@app.get(
    "/datasets/{dataset_name}/versions/{from_version}/diff/{to_version}",
    response_model=VersionDiffResponse,
)
def diff_schema_versions(
    request: Request,
    dataset_name: str,
    from_version: int,
    to_version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> VersionDiffResponse:
    # Read-only and parameterless; any body bytes or query parameters are a
    # 422 validated in the repository once the path dataset and both versions
    # are known, preserving 404 precedence.
    return VersionDiffResponse(
        **repository.diff_schema_versions(
            conn,
            dataset_name,
            from_version,
            to_version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )


# Read-only breaking-change compatibility check of a target version against a
# base version. The body is serialized directly (rather than through the
# default JSON response) so the key order is fixed, the whitespace is compact
# and the document ends with exactly one newline.
@app.get(
    "/datasets/{dataset_name}/versions/{base_version}"
    "/compatibility/{target_version}",
    response_model=VersionCompatibilityResponse,
)
def check_schema_version_compatibility(
    request: Request,
    dataset_name: str,
    base_version: int,
    target_version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> Response:
    # Read-only and parameterless; any body bytes or query parameters are a
    # 422 validated in the repository once the path dataset and both versions
    # are known, preserving 404 precedence (mirrors the version diff
    # endpoint).
    compatibility = VersionCompatibilityResponse(
        **repository.check_schema_version_compatibility(
            conn,
            dataset_name,
            base_version,
            target_version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )
    payload = json.dumps(
        compatibility.model_dump(mode="json"),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return Response(content=payload + "\n", media_type="application/json")


# Read-only companion of the compatibility check: the same breaking-change
# entries, each additionally carrying the direct and indirect downstream
# fields of the broken field along the lineage mappings. The body is
# serialized directly (rather than through the default JSON response) so the
# key order is fixed, the whitespace is compact and the document ends with
# exactly one newline.
@app.get(
    "/datasets/{dataset_name}/versions/{base_version}"
    "/compatibility/{target_version}/impact",
    response_model=VersionCompatibilityImpactResponse,
)
def check_schema_version_compatibility_impact(
    request: Request,
    dataset_name: str,
    base_version: int,
    target_version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> Response:
    # Read-only and parameterless; any body bytes or query parameters are a
    # 422 validated in the repository once the path dataset and both versions
    # are known, preserving 404 precedence (mirrors the compatibility
    # endpoint). Nothing is written: version definitions, lineage mappings
    # and the impact cache are all left untouched.
    impact = VersionCompatibilityImpactResponse(
        **repository.check_schema_version_compatibility_impact(
            conn,
            dataset_name,
            base_version,
            target_version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )
    payload = json.dumps(
        impact.model_dump(mode="json"),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return Response(content=payload + "\n", media_type="application/json")


# Read-only whole-dataset summary of adjacent-version breaking changes and
# their downstream impact. The path carries only the dataset name. The body is
# serialized directly (rather than through the default JSON response) so the
# key order is fixed, the whitespace is compact and the document ends with
# exactly one newline.
@app.get(
    "/datasets/{dataset_name}/evolution-summary",
    response_model=VersionEvolutionSummaryResponse,
)
def get_schema_version_evolution_summary(
    request: Request,
    dataset_name: str,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> Response:
    # Read-only and parameterless; any body bytes or query parameters are a
    # 422 validated in the repository once the path dataset is known,
    # preserving the same 404 precedence as the compatibility reads. Nothing
    # is written: version definitions, lineage mappings and the impact cache
    # are all left untouched.
    summary = VersionEvolutionSummaryResponse(
        **repository.summarize_schema_version_evolution(
            conn,
            dataset_name,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )
    payload = json.dumps(
        summary.model_dump(mode="json"),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return Response(content=payload + "\n", media_type="application/json")


# Read-only cross-version trajectory of one named field of the dataset: the
# field's persisted definition in every schema version plus its status change
# and downstream impact between each adjacent pair. The path carries the
# dataset name and the field name. The body is serialized directly (rather
# than through the default JSON response) so the key order is fixed, the
# whitespace is compact and the document ends with exactly one newline.
@app.get(
    "/datasets/{dataset_name}/fields/{field_name}/trajectory",
    response_model=FieldTrajectoryResponse,
)
def get_field_trajectory(
    request: Request,
    dataset_name: str,
    field_name: str,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> Response:
    # Read-only and parameterless; any body bytes or query parameters are a
    # 422 validated in the repository once the path dataset and field are
    # known, preserving the same 404 precedence as the evolution summary.
    # Nothing is written: version definitions, lineage mappings and the
    # impact cache are all left untouched.
    trajectory = FieldTrajectoryResponse(
        **repository.get_field_trajectory(
            conn,
            dataset_name,
            field_name,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )
    payload = json.dumps(
        trajectory.model_dump(mode="json"),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return Response(content=payload + "\n", media_type="application/json")


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


# Deletion lives at the same address as registration, submitted as DELETE
# with the same body shape: the six locating fields name the complete mapping
# to remove. The body is read raw so the repository can resolve every
# readable locating value (404) before judging the request shape (422).
@app.delete(
    "/datasets/{dataset_name}/versions/{version}/lineage",
    response_model=LineageDeletedResponse,
)
def delete_lineage(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> LineageDeletedResponse:
    deleted = repository.delete_lineage_link(
        conn,
        dataset_name,
        version,
        body=body,
        query_keys=tuple(request.query_params.keys()),
    )
    return LineageDeletedResponse(**deleted)


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


@app.get(
    "/datasets/{dataset_name}/versions/{version}/lineage/impact",
    response_model=LineageImpactResponse,
)
def get_lineage_impact(
    request: Request,
    dataset_name: str,
    version: int,
    field: str | None = Query(default=None),
    conn=Depends(get_db),
) -> LineageImpactResponse:
    if field is None or not field.strip():
        raise RequestInvalidError(
            "Query parameter 'field' is required and must be a non-empty "
            "field name"
        )
    return LineageImpactResponse(
        **repository.get_lineage_impact(
            conn,
            dataset_name,
            version,
            field.strip(),
            field_values=tuple(request.query_params.getlist("field")),
        )
    )


# Read-only consistency audit of the version's impact cache, appended one
# segment after the lineage impact query address. Every field of the version
# is audited against a fresh recomputation; nothing is read for repair and
# the cache is never written, invalidated or repaired. The body is serialized
# directly (rather than through the default JSON response) so the key order
# is fixed, the whitespace is compact and the document ends with exactly one
# newline.
@app.get(
    "/datasets/{dataset_name}/versions/{version}/lineage/impact/cache-audit",
    response_model=LineageImpactCacheAuditResponse,
)
def get_lineage_impact_cache_audit(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> Response:
    # Read-only and parameterless; any body bytes (whitespace-only included)
    # or query parameters are a 422 validated in the repository once the path
    # dataset/version is known, preserving the impact query's 404-before-422
    # precedence. The audit recomputes on every read and never writes, so the
    # impact query, path explanations and source-path reads are unaffected.
    audit = LineageImpactCacheAuditResponse(
        **repository.audit_lineage_impact_cache(
            conn,
            dataset_name,
            version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )
    payload = json.dumps(
        audit.model_dump(mode="json"),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return Response(content=payload + "\n", media_type="application/json")


# Controlled repair of the version's impact cache, appended one segment
# after the read-only cache audit address and accepting POST only. Every
# field of the version is recomputed against the committed lineage graph:
# missing records are created, stale records rewritten and matching records
# left untouched. The body is serialized directly (rather than through the
# default JSON response) so the key order is fixed, the whitespace is compact
# and the document ends with exactly one newline.
def _repair_guard(dataset_name: str, version: int) -> Iterator[None]:
    # Declared before the database dependency so the lock is taken before the
    # request connection is opened, and — dependencies tearing down in
    # reverse order — released only after the transaction commits. A
    # concurrent repair of the same version therefore meets a held lock
    # wherever it starts and loses with a 409 instead of waiting.
    lock = repository.impact_cache_repair_lock(dataset_name, version)
    if not lock.acquire(blocking=False):
        raise ConflictError(
            f"Another repair of the impact cache of version {version} of "
            f"dataset '{dataset_name}' is already in progress"
        )
    try:
        yield
    finally:
        lock.release()


@app.post(
    "/datasets/{dataset_name}/versions/{version}"
    "/lineage/impact/cache-audit/repair",
    response_model=LineageImpactCacheRepairResponse,
)
def repair_lineage_impact_cache(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    _repair: None = Depends(_repair_guard),
    conn=Depends(get_db),
) -> Response:
    # The endpoint takes no input beyond the path; any body bytes
    # (whitespace-only included) or query parameters are a 422 validated in
    # the repository once the path dataset/version is known, preserving the
    # impact query's 404-before-422 precedence. Concurrent repairs of the
    # same version have a single winner; the loser is a 409 and changes
    # nothing. Every other rejection writes nothing either.
    repair = LineageImpactCacheRepairResponse(
        **repository.repair_lineage_impact_cache(
            conn,
            dataset_name,
            version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )
    payload = json.dumps(
        repair.model_dump(mode="json"),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return Response(content=payload + "\n", media_type="application/json")


# Read-only shortest-path companion of the lineage impact query, appended one
# segment after it: each impacted field carries the shortest node sequence and
# its edge count. The body is serialized directly (rather than through the
# default JSON response) so the key order is fixed, the whitespace is compact
# and the document ends with exactly one newline.
@app.get(
    "/datasets/{dataset_name}/versions/{version}/lineage/impact-paths",
    response_model=LineageImpactPathsResponse,
)
def get_lineage_impact_paths(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    field: str | None = Query(default=None),
    conn=Depends(get_db),
) -> Response:
    # Read-only: the paths are recomputed from the committed lineage graph on
    # every read and the impact cache is never read or written. A missing,
    # blank or repeated 'field' parameter, any other query parameter or any
    # request body is a 422 validated in the repository once the path
    # dataset/version and the field have resolved, preserving 404 precedence.
    result = LineageImpactPathsResponse(
        **repository.get_lineage_impact_paths(
            conn,
            dataset_name,
            version,
            field,
            body=body,
            query_keys=tuple(request.query_params.keys()),
            field_values=tuple(request.query_params.getlist("field")),
        )
    )
    payload = json.dumps(
        result.model_dump(mode="json"),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return Response(content=payload + "\n", media_type="application/json")


# Read-only upstream companion of the lineage impact query, appended one
# segment after it: each origin field carries the shortest node sequence and
# its edge count. The body is serialized directly (rather than through the
# default JSON response) so the key order is fixed, the whitespace is compact
# and the document ends with exactly one newline.
@app.get(
    "/datasets/{dataset_name}/versions/{version}/lineage/impact/source-paths",
    response_model=LineageSourcePathsResponse,
)
def get_lineage_source_paths(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    field: str | None = Query(default=None),
    conn=Depends(get_db),
) -> Response:
    # Read-only: the paths are recomputed from the committed lineage graph on
    # every read and the impact cache is never read or written. A missing,
    # blank or repeated 'field' parameter, any other query parameter or any
    # request body is a 422 validated in the repository once the path
    # dataset/version and the field have resolved, preserving 404 precedence
    # (mirrors the downstream impact paths endpoint).
    result = LineageSourcePathsResponse(
        **repository.get_lineage_source_paths(
            conn,
            dataset_name,
            version,
            field,
            body=body,
            query_keys=tuple(request.query_params.keys()),
            field_values=tuple(request.query_params.getlist("field")),
        )
    )
    payload = json.dumps(
        result.model_dump(mode="json"),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return Response(content=payload + "\n", media_type="application/json")


# --------------------------------------------------------------------------- #
# Quality rules
# --------------------------------------------------------------------------- #


@app.post(
    "/datasets/{dataset_name}/versions/{version}/quality-rules",
    response_model=QualityRule,
    status_code=201,
)
def create_quality_rule(
    dataset_name: str,
    version: int,
    payload: QualityRuleCreate,
    conn=Depends(get_db),
) -> QualityRule:
    rule = repository.create_quality_rule(
        conn,
        dataset_name,
        version,
        payload.name,
        payload.kind,
        payload.params,
    )
    return QualityRule(**rule)


@app.get(
    "/datasets/{dataset_name}/versions/{version}/quality-rules",
    response_model=list[QualityRule],
)
def list_quality_rules(
    dataset_name: str, version: int, conn=Depends(get_db)
) -> list[QualityRule]:
    return [
        QualityRule(**rule)
        for rule in repository.list_quality_rules(conn, dataset_name, version)
    ]


@app.patch(
    "/datasets/{dataset_name}/versions/{version}/quality-rules/{rule_id}",
    response_model=QualityRule,
)
def patch_quality_rule(
    dataset_name: str,
    version: int,
    rule_id: int,
    payload: QualityRuleEnabledUpdate,
    conn=Depends(get_db),
) -> QualityRule:
    return QualityRule(
        **repository.set_quality_rule_enabled(
            conn, dataset_name, version, rule_id, payload.enabled
        )
    )


@app.post(
    "/datasets/{dataset_name}/versions/{version}/quality-rules/evaluate",
    response_model=QualityRuleEvaluateResponse,
)
def evaluate_quality_rules(
    dataset_name: str,
    version: int,
    payload: QualityRuleEvaluateRequest,
    conn=Depends(get_db),
) -> QualityRuleEvaluateResponse:
    return QualityRuleEvaluateResponse(
        **repository.evaluate_quality_rules(
            conn, dataset_name, version, payload.rows
        )
    )


# Declared before the "/evaluations" collection route for symmetry with the
# other literal-segment routes; both are read-only and parameterless, so any
# body bytes or query parameters are a 422 validated in the repository once
# the path dataset/version is known, preserving 404 precedence.
@app.get(
    "/datasets/{dataset_name}/versions/{version}/quality-rules/evaluations/diff",
    response_model=QualityRuleEvaluationDiffResponse,
)
def diff_quality_rule_evaluations(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> QualityRuleEvaluationDiffResponse:
    return QualityRuleEvaluationDiffResponse(
        **repository.diff_quality_rule_evaluations(
            conn,
            dataset_name,
            version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )


@app.get(
    "/datasets/{dataset_name}/versions/{version}/quality-rules/evaluations",
    response_model=list[QualityRuleEvaluationRecord],
)
def list_quality_rule_evaluations(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> list[QualityRuleEvaluationRecord]:
    return [
        QualityRuleEvaluationRecord(**record)
        for record in repository.list_quality_rule_evaluations(
            conn,
            dataset_name,
            version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    ]


# Read-only release-readiness verdict of the version, computed fresh from the
# persisted evaluation history and anomaly records on every call; nothing is
# cached or written. The body is serialized directly (rather than through the
# default JSON response) so the key order is fixed, the whitespace is compact
# and the document ends with exactly one newline.
@app.get(
    "/datasets/{dataset_name}/versions/{version}/quality-rules/gate",
    response_model=QualityGateResponse,
)
def get_quality_gate(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> Response:
    # Read-only and parameterless; any body bytes (whitespace-only included)
    # or query parameters are a 422 validated in the repository once the path
    # dataset/version is known, preserving 404 precedence (mirrors the
    # evaluation history endpoint).
    gate = QualityGateResponse(
        **repository.get_quality_gate(
            conn,
            dataset_name,
            version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )
    payload = json.dumps(
        gate.model_dump(mode="json"),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return Response(content=payload + "\n", media_type="application/json")


# Read-only release-readiness verdict as it stood at a requested instant: the
# same computation and deterministic serialization as the bare gate, but over
# the records written at or before the 'timestamp' query parameter. Nothing is
# cached or written on any call.
@app.get(
    "/datasets/{dataset_name}/versions/{version}/quality-rules/gate/at",
    response_model=QualityGateResponse,
)
def get_quality_gate_at(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> Response:
    # The path dataset/version resolves first (404); any body bytes
    # (whitespace-only included), a missing/repeated/unparseable/timezone-less
    # 'timestamp' or any other query parameter are a 422 validated in the
    # repository afterwards. The raw query pairs are passed through so a
    # repeated 'timestamp' is rejected instead of silently collapsed.
    gate = QualityGateResponse(
        **repository.get_quality_gate_at(
            conn,
            dataset_name,
            version,
            body=body,
            query_params=tuple(request.query_params.multi_items()),
        )
    )
    payload = json.dumps(
        gate.model_dump(mode="json"),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return Response(content=payload + "\n", media_type="application/json")


# --------------------------------------------------------------------------- #
# Quality anomaly detection over the evaluation history
# --------------------------------------------------------------------------- #


ANOMALY_DETECTION_PATH = (
    "/datasets/{dataset_name}/versions/{version}"
    "/quality-rules/anomaly-detection"
)


@app.post(
    ANOMALY_DETECTION_PATH,
    response_model=QualityAnomalyDetectionConfig,
    status_code=201,
)
def register_anomaly_detection_config(
    dataset_name: str,
    version: int,
    payload: QualityAnomalyDetectionConfigCreate,
    conn=Depends(get_db),
) -> QualityAnomalyDetectionConfig:
    return QualityAnomalyDetectionConfig(
        **repository.register_anomaly_detection_config(
            conn,
            dataset_name,
            version,
            consecutive_worsening_steps=payload.consecutive_worsening_steps,
            violation_row_limit=payload.violation_row_limit,
            rule_violation_limit=payload.rule_violation_limit,
        )
    )


@app.get(ANOMALY_DETECTION_PATH, response_model=QualityAnomalyDetectionConfig)
def get_anomaly_detection_config(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> QualityAnomalyDetectionConfig:
    # Read-only and parameterless; any body bytes or query parameters are a
    # 422 validated in the repository once the path dataset/version is known,
    # preserving 404 precedence (mirrors the evaluation history endpoint).
    return QualityAnomalyDetectionConfig(
        **repository.get_anomaly_detection_config(
            conn,
            dataset_name,
            version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )


@app.post(
    f"{ANOMALY_DETECTION_PATH}/scan",
    response_model=list[QualityAnomalyRecord],
)
def scan_quality_anomalies(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> list[QualityAnomalyRecord]:
    # The scan takes no input beyond the path; any body bytes or query
    # parameters are a 422 validated in the repository after the path
    # resolves, preserving 404 precedence.
    return [
        QualityAnomalyRecord(**record)
        for record in repository.scan_quality_anomalies(
            conn,
            dataset_name,
            version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    ]


@app.get(
    f"{ANOMALY_DETECTION_PATH}/anomalies",
    response_model=list[QualityAnomalyRecord],
)
def list_quality_anomalies(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> list[QualityAnomalyRecord]:
    # Same parameterless read rules as the scan and config endpoints.
    return [
        QualityAnomalyRecord(**record)
        for record in repository.list_quality_anomalies(
            conn,
            dataset_name,
            version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    ]


# --------------------------------------------------------------------------- #
# Privacy policies
# --------------------------------------------------------------------------- #
@app.post(
    "/datasets/{dataset_name}/versions/{version}/privacy-policies",
    response_model=PrivacyPolicy,
    status_code=201,
)
def create_privacy_policy(
    dataset_name: str,
    version: int,
    payload: PrivacyPolicyCreate,
    conn=Depends(get_db),
) -> PrivacyPolicy:
    policy = repository.create_privacy_policy(
        conn,
        dataset_name,
        version,
        payload.field,
        payload.classification,
        payload.masking,
        payload.allowed_roles,
    )
    return PrivacyPolicy(**policy)


@app.get(
    "/datasets/{dataset_name}/versions/{version}/privacy-policies",
    response_model=list[PrivacyPolicy],
)
def list_privacy_policies(
    dataset_name: str, version: int, conn=Depends(get_db)
) -> list[PrivacyPolicy]:
    return [
        PrivacyPolicy(**policy)
        for policy in repository.list_privacy_policies(conn, dataset_name, version)
    ]


@app.patch(
    "/datasets/{dataset_name}/versions/{version}/privacy-policies/{policy_id}",
    response_model=PrivacyPolicy,
)
def patch_privacy_policy(
    dataset_name: str,
    version: int,
    policy_id: int,
    payload: PrivacyPolicyEnabledUpdate,
    conn=Depends(get_db),
) -> PrivacyPolicy:
    return PrivacyPolicy(
        **repository.set_privacy_policy_enabled(
            conn, dataset_name, version, policy_id, payload.enabled
        )
    )


@app.post(
    "/datasets/{dataset_name}/versions/{version}/privacy-policies/view",
    response_model=PrivacyViewResponse,
)
def view_privacy_rows(
    dataset_name: str,
    version: int,
    payload: PrivacyViewRequest,
    conn=Depends(get_db),
) -> PrivacyViewResponse:
    return PrivacyViewResponse(
        **repository.view_privacy_rows(
            conn, dataset_name, version, payload.role, payload.rows
        )
    )


@app.get(
    "/datasets/{dataset_name}/versions/{version}"
    "/privacy-policies/view/audit-records",
    response_model=list[PrivacyViewAuditRecord],
)
def list_privacy_view_audit_records(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> list[PrivacyViewAuditRecord]:
    # Read-only and parameterless; any body bytes or query parameters are a
    # 422 validated in the repository once the path dataset/version is known,
    # preserving 404 precedence (mirrors the evaluation history endpoint).
    return [
        PrivacyViewAuditRecord(**record)
        for record in repository.list_privacy_view_audit_records(
            conn,
            dataset_name,
            version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    ]


# Read-only one-per-view access trail appended after the privacy view path.
# Exactly one record is written per successful view; the endpoint lists them
# in sequence order and never writes anything itself.
@app.get(
    "/datasets/{dataset_name}/versions/{version}"
    "/privacy-policies/view/access-records",
    response_model=list[PrivacyViewAccessRecord],
)
def list_privacy_view_access_records(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> list[PrivacyViewAccessRecord]:
    # Read-only and parameterless; any body bytes or query parameters are a
    # 422 validated in the repository once the path dataset/version is known,
    # preserving 404 precedence, exactly like the masking-hit record list.
    return [
        PrivacyViewAccessRecord(**record)
        for record in repository.list_privacy_view_access_records(
            conn,
            dataset_name,
            version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    ]


# Read-only filtered search appended after the hit-record query path. Every
# filter is optional; the response is the same record collection as the full
# list, in the same sequence order, and nothing is ever written.
@app.get(
    "/datasets/{dataset_name}/versions/{version}"
    "/privacy-policies/view/audit-records/search",
    response_model=list[PrivacyViewAuditRecord],
)
def search_privacy_view_audit_records(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    role: str | None = Query(default=None),
    field: str | None = Query(default=None),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    conn=Depends(get_db),
) -> list[PrivacyViewAuditRecord]:
    return [
        PrivacyViewAuditRecord(**record)
        for record in repository.search_privacy_view_audit_records(
            conn,
            dataset_name,
            version,
            role=role,
            field=field,
            start=start,
            end=end,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    ]


# Read-only compliance summary appended after the hit-record query path. The
# summary is recomputed from the persisted records on every read.
@app.get(
    "/datasets/{dataset_name}/versions/{version}"
    "/privacy-policies/view/audit-records/summary",
    response_model=PrivacyViewAuditSummaryResponse,
)
def get_privacy_view_audit_summary(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> PrivacyViewAuditSummaryResponse:
    # Read-only and parameterless; any body bytes or query parameters are a
    # 422 validated in the repository once the path dataset/version is known,
    # preserving 404 precedence, exactly like the hit-record list.
    return PrivacyViewAuditSummaryResponse(
        **repository.summarize_privacy_view_audit_records(
            conn,
            dataset_name,
            version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )


# Read-only day-over-day comparison appended after the hit-record query path.
# The two most recent UTC calendar days with records are compared; the diff is
# recomputed from the persisted records on every read and never writes.
@app.get(
    "/datasets/{dataset_name}/versions/{version}"
    "/privacy-policies/view/audit-records/diff",
    response_model=PrivacyViewAuditDiffResponse,
)
def diff_privacy_view_audit_records(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> PrivacyViewAuditDiffResponse:
    # Read-only and parameterless; any body bytes or query parameters are a
    # 422 validated in the repository once the path dataset/version is known,
    # preserving 404 precedence, exactly like the hit-record list.
    return PrivacyViewAuditDiffResponse(
        **repository.diff_privacy_view_audit_records(
            conn,
            dataset_name,
            version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )


# Read-only per-day reconciliation appended after the hit-record query path.
# The access records and the masking-hit records are aligned by the UTC
# calendar day of their write time and their counts cross-checked; the
# result is recomputed from the persisted records on every read and never
# writes.
@app.get(
    "/datasets/{dataset_name}/versions/{version}"
    "/privacy-policies/view/audit-records/reconcile",
    response_model=PrivacyViewAuditReconcileResponse,
)
def reconcile_privacy_view_audit_records(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> PrivacyViewAuditReconcileResponse:
    # Read-only and parameterless; any body bytes or query parameters are a
    # 422 validated in the repository once the path dataset/version is known,
    # preserving 404 precedence, exactly like the hit-record list.
    return PrivacyViewAuditReconcileResponse(
        **repository.reconcile_privacy_view_audit_records(
            conn,
            dataset_name,
            version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )


# Read-only per-policy hit trend appended after the hit-record query path.
# Hits merge across roles per policy and bucket by UTC day; the trend is
# recomputed from the persisted records on every read and never writes.
@app.get(
    "/datasets/{dataset_name}/versions/{version}"
    "/privacy-policies/view/audit-records/trend",
    response_model=PrivacyViewAuditTrendResponse,
)
def get_privacy_view_audit_trend(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> PrivacyViewAuditTrendResponse:
    # Read-only and parameterless; any body bytes or query parameters are a
    # 422 validated in the repository once the path dataset/version is known,
    # preserving 404 precedence, exactly like the hit-record list.
    return PrivacyViewAuditTrendResponse(
        **repository.trend_privacy_view_audit_records(
            conn,
            dataset_name,
            version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )


# Two-stage preview/confirm cleanup of masking-hit records, nested under the
# version's hit-record collection. Creating a request only freezes and returns
# a preview — no record is deleted; a subsequent confirm removes exactly the
# frozen target set atomically.
PRIVACY_VIEW_AUDIT_CLEANUP_REQUESTS_PATH = (
    "/datasets/{dataset_name}/versions/{version}"
    "/privacy-policies/view/audit-records/cleanup-requests"
)


@app.post(
    PRIVACY_VIEW_AUDIT_CLEANUP_REQUESTS_PATH,
    response_model=PrivacyViewAuditCleanupRequest,
    status_code=201,
)
def create_privacy_view_audit_cleanup_request(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> PrivacyViewAuditCleanupRequest:
    # The body is parsed in the repository (after the path dataset/version
    # resolves) so a missing/malformed/extra field stays a 422 while an
    # unknown dataset or version keeps its 404 precedence.
    return PrivacyViewAuditCleanupRequest(
        **repository.create_privacy_view_audit_cleanup_request(
            conn,
            dataset_name,
            version,
            body,
            query_keys=tuple(request.query_params.keys()),
        )
    )


@app.get(
    PRIVACY_VIEW_AUDIT_CLEANUP_REQUESTS_PATH,
    response_model=list[PrivacyViewAuditCleanupRequest],
)
def list_privacy_view_audit_cleanup_requests(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> list[PrivacyViewAuditCleanupRequest]:
    # Read-only and parameterless, with the same 404-before-422 precedence as
    # the masking-hit record list.
    return [
        PrivacyViewAuditCleanupRequest(**cleanup_request)
        for cleanup_request in repository.list_privacy_view_audit_cleanup_requests(
            conn,
            dataset_name,
            version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    ]


@app.post(
    f"{PRIVACY_VIEW_AUDIT_CLEANUP_REQUESTS_PATH}/{{request_id}}/confirm",
    response_model=ConfirmedPrivacyViewAuditCleanupRequest,
)
def confirm_privacy_view_audit_cleanup_request(
    request: Request,
    dataset_name: str,
    version: int,
    request_id: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> ConfirmedPrivacyViewAuditCleanupRequest:
    # Empty body and no query parameters; both are 422 checked in the
    # repository after the path dataset/version/request resolves, so a missing
    # resource stays a 404 and an already-confirmed request stays a 409.
    return ConfirmedPrivacyViewAuditCleanupRequest(
        **repository.confirm_privacy_view_audit_cleanup_request(
            conn,
            dataset_name,
            version,
            request_id,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )


# --------------------------------------------------------------------------- #
# Read-only cross-version privacy compliance export
# --------------------------------------------------------------------------- #


# A dataset-level read that returns the privacy compliance state of every
# schema version at once. The path carries only the dataset name; there is no
# request body or query parameter.
PRIVACY_COMPLIANCE_EXPORT_PATH = (
    "/datasets/{dataset_name}/privacy-compliance-export"
)


@app.get(
    PRIVACY_COMPLIANCE_EXPORT_PATH,
    response_model=PrivacyComplianceExportResponse,
)
def export_dataset_privacy_compliance(
    request: Request,
    dataset_name: str,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> Response:
    # Read-only and parameterless; any body bytes or query parameters are a
    # 422 validated in the repository once the path dataset is known,
    # preserving the same 404 precedence as the masking-hit record reads. The
    # body is serialized directly (rather than through the default JSON
    # response) so the key order is fixed, the whitespace is compact and the
    # document ends with exactly one newline.
    export = PrivacyComplianceExportResponse(
        **repository.export_dataset_privacy_compliance(
            conn,
            dataset_name,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )
    payload = json.dumps(
        export.model_dump(mode="json"),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return Response(content=payload + "\n", media_type="application/json")


# --------------------------------------------------------------------------- #
# Read-only cross-version privacy policy coverage check
# --------------------------------------------------------------------------- #


# A dataset-level read that reports, for every schema version at once, which
# fields a privacy policy covers and which identified fields still lack one.
# The path carries only the dataset name; there is no request body or query
# parameter.
PRIVACY_POLICY_COVERAGE_PATH = "/datasets/{dataset_name}/privacy-policy-coverage"


@app.get(
    PRIVACY_POLICY_COVERAGE_PATH,
    response_model=PrivacyPolicyCoverageResponse,
)
def get_privacy_policy_coverage(
    request: Request,
    dataset_name: str,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> Response:
    # Read-only and parameterless; any body bytes or query parameters are a
    # 422 validated in the repository once the path dataset is known,
    # preserving the same 404 precedence as the compliance export. The body
    # is serialized directly (rather than through the default JSON response)
    # so the key order is fixed, the whitespace is compact and the document
    # ends with exactly one newline.
    coverage = PrivacyPolicyCoverageResponse(
        **repository.privacy_policy_coverage(
            conn,
            dataset_name,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )
    payload = json.dumps(
        coverage.model_dump(mode="json"),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return Response(content=payload + "\n", media_type="application/json")


# --------------------------------------------------------------------------- #
# Sensitive-field identification (candidate annotation only)
# --------------------------------------------------------------------------- #


SENSITIVE_IDENTIFICATIONS_PATH = (
    "/datasets/{dataset_name}/versions/{version}/sensitive-identifications"
)


@app.post(
    SENSITIVE_IDENTIFICATIONS_PATH,
    response_model=SensitiveIdentification,
    status_code=201,
)
def create_sensitive_identification(
    request: Request,
    dataset_name: str,
    version: int,
    payload: SensitiveIdentificationCreate,
    response: Response,
    conn=Depends(get_db),
) -> SensitiveIdentification:
    # The first submission for a field is a 201; re-running the same field
    # refreshes the record in place with a stable id and returns 200. Any query
    # parameter is a 422 checked in the repository after the path resolves,
    # preserving 404 precedence.
    record, created = repository.create_sensitive_identification(
        conn,
        dataset_name,
        version,
        payload.field,
        list(payload.samples),
        query_keys=tuple(request.query_params.keys()),
    )
    if not created:
        response.status_code = 200
    return SensitiveIdentification(**record)


@app.get(
    SENSITIVE_IDENTIFICATIONS_PATH,
    response_model=list[SensitiveIdentification],
)
def list_sensitive_identifications(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> list[SensitiveIdentification]:
    # Read-only and parameterless; any body bytes or query parameters are a
    # 422 validated in the repository once the path dataset/version is known,
    # preserving 404 precedence.
    return [
        SensitiveIdentification(**record)
        for record in repository.list_sensitive_identifications(
            conn,
            dataset_name,
            version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    ]


@app.get(
    f"{SENSITIVE_IDENTIFICATIONS_PATH}/masking-suggestions",
    response_model=list[MaskingSuggestion],
)
def list_masking_suggestions(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> list[MaskingSuggestion]:
    # Read-only advisory candidates recomputed from the current identification
    # records; they never register a privacy policy. The endpoint takes no
    # request body and no query parameters (422), validated after the path
    # dataset/version resolves so 404 keeps precedence (mirrors the
    # identifications list endpoint).
    return [
        MaskingSuggestion(**suggestion)
        for suggestion in repository.list_masking_suggestions(
            conn,
            dataset_name,
            version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    ]


@app.post(
    f"{SENSITIVE_IDENTIFICATIONS_PATH}/masking-suggestions/register",
    response_model=list[PrivacyPolicy],
    status_code=201,
)
def register_masking_suggestions(
    request: Request,
    dataset_name: str,
    version: int,
    payload: MaskingSuggestionsRegisterRequest,
    conn=Depends(get_db),
) -> list[PrivacyPolicy]:
    # Turn the version's current advisory candidates into registered privacy
    # policies for exactly the named fields. The candidates (classification,
    # masking and the empty allowed-role list) are read from the identification
    # records and recomputed the same way the read-only suggestions endpoint
    # does; the identification records themselves are never written. The batch
    # is validated as a whole before any policy is inserted, so a rejected
    # request writes nothing. Query parameters are a 422 checked in the
    # repository after the path resolves, preserving 404 precedence (mirrors
    # the batch-complete endpoint).
    return [
        PrivacyPolicy(**policy)
        for policy in repository.register_masking_suggestions(
            conn,
            dataset_name,
            version,
            list(payload.fields),
            query_keys=tuple(request.query_params.keys()),
        )
    ]


# --------------------------------------------------------------------------- #
# Row snapshots
# --------------------------------------------------------------------------- #


SNAPSHOTS_PATH = "/datasets/{dataset_name}/versions/{version}/snapshots"


@app.post(SNAPSHOTS_PATH, response_model=SnapshotMetadata, status_code=201)
def create_snapshot(
    dataset_name: str,
    version: int,
    payload: SnapshotCreate,
    conn=Depends(get_db),
) -> SnapshotMetadata:
    return SnapshotMetadata(
        **repository.create_snapshot(conn, dataset_name, version, payload.rows)
    )


@app.get(SNAPSHOTS_PATH, response_model=list[SnapshotMetadata])
def list_snapshots(
    dataset_name: str, version: int, conn=Depends(get_db)
) -> list[SnapshotMetadata]:
    return [
        SnapshotMetadata(**snapshot)
        for snapshot in repository.list_snapshots(conn, dataset_name, version)
    ]


# Declared before the "/{snapshot_id}" route so the literal "at" segment is
# matched there rather than parsed as a snapshot id.
@app.get(f"{SNAPSHOTS_PATH}/at", response_model=SnapshotResponse)
def get_snapshot_at(
    dataset_name: str,
    version: int,
    timestamp: str | None = Query(default=None),
    conn=Depends(get_db),
) -> SnapshotResponse:
    return SnapshotResponse(
        **repository.get_snapshot_at(conn, dataset_name, version, timestamp)
    )


# Role-masked time travel, appended one segment after the bare-row time lookup
# and accepting POST only. The raw body is validated in the repository so an
# unknown dataset/version or a missing snapshot stays a 404 checked ahead of
# every body/query shape check (all 422), mirroring the bare-row lookup.
@app.post(
    f"{SNAPSHOTS_PATH}/at/masked-view",
    response_model=SnapshotMaskedViewResponse,
)
def view_snapshot_masked_at(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> SnapshotMaskedViewResponse:
    return SnapshotMaskedViewResponse(
        **repository.view_snapshot_masked_at(
            conn,
            dataset_name,
            version,
            body,
            query_keys=tuple(request.query_params.keys()),
        )
    )


# Read-only time-travel diff, appended one segment after the bare-row time
# lookup and accepting GET only. Each timestamp independently selects the
# newest snapshot created not later than it; the two selected snapshots are
# compared with the same row-multiset semantics as the snapshot-id diff plus
# top-level field-name sets. The body is serialized directly (rather than
# through the default JSON response) so the key order is fixed, the whitespace
# is compact and the document ends with exactly one newline.
@app.get(f"{SNAPSHOTS_PATH}/at/diff", response_model=SnapshotAtDiffResponse)
def diff_snapshots_at(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    from_timestamp: str | None = Query(default=None, alias="from"),
    to_timestamp: str | None = Query(default=None, alias="to"),
    conn=Depends(get_db),
) -> Response:
    # The path dataset/version resolves first (404); every request-shape
    # problem — body bytes (whitespace included), unknown or repeated query
    # parameters, missing/blank, unparseable or timezone-less from/to — is a
    # 422 validated in the repository next, and only afterwards is each side's
    # snapshot selected (a missing snapshot is then a 404). The comparison is
    # fully read-only.
    diff = SnapshotAtDiffResponse(
        **repository.diff_snapshots_at(
            conn,
            dataset_name,
            version,
            raw_from=from_timestamp,
            raw_to=to_timestamp,
            from_values=tuple(request.query_params.getlist("from")),
            to_values=tuple(request.query_params.getlist("to")),
            query_keys=tuple(request.query_params.keys()),
            body=body,
        )
    )
    payload = json.dumps(
        diff.model_dump(mode="json"),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return Response(content=payload + "\n", media_type="application/json")


@app.get(f"{SNAPSHOTS_PATH}/{{snapshot_id}}", response_model=SnapshotResponse)
def get_snapshot(
    dataset_name: str,
    version: int,
    snapshot_id: int,
    conn=Depends(get_db),
) -> SnapshotResponse:
    return SnapshotResponse(
        **repository.get_snapshot(conn, dataset_name, version, snapshot_id)
    )


@app.get(
    f"{SNAPSHOTS_PATH}/{{snapshot_id}}/diff/{{other_snapshot_id}}",
    response_model=SnapshotDiffResponse,
)
def diff_snapshots(
    dataset_name: str,
    version: int,
    snapshot_id: int,
    other_snapshot_id: int,
    conn=Depends(get_db),
) -> SnapshotDiffResponse:
    return SnapshotDiffResponse(
        **repository.diff_snapshots(
            conn, dataset_name, version, snapshot_id, other_snapshot_id
        )
    )


# Read-only cross-version snapshot comparison mounted directly under the
# dataset (the two snapshots name two different schema versions, so no
# version appears in the path). The body is serialized directly (rather than
# through the default JSON response) so the key order is fixed, the
# whitespace is compact and the document ends with exactly one newline.
@app.get(
    "/datasets/{dataset_name}/snapshots/{base_snapshot_id}"
    "/diff/{target_snapshot_id}",
    response_model=CrossVersionSnapshotDiffResponse,
)
def diff_cross_version_snapshots(
    request: Request,
    dataset_name: str,
    base_snapshot_id: int,
    target_snapshot_id: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> Response:
    # The comparison takes no input beyond the two snapshot ids in the path:
    # any body bytes (whitespace-only included) or query parameters are a 422
    # validated in the repository once the path dataset and both snapshots
    # are known, so unknown resources stay 404 and snapshots from another
    # dataset or the same schema version stay 422. The comparison is fully
    # read-only.
    diff = CrossVersionSnapshotDiffResponse(
        **repository.diff_cross_version_snapshots(
            conn,
            dataset_name,
            base_snapshot_id,
            target_snapshot_id,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )
    payload = json.dumps(
        diff.model_dump(mode="json"),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return Response(content=payload + "\n", media_type="application/json")


@app.post(
    f"{SNAPSHOTS_PATH}/{{snapshot_id}}/quality-rules/evaluate",
    response_model=QualityRuleEvaluateResponse,
)
def evaluate_snapshot_quality_rules(
    request: Request,
    dataset_name: str,
    version: int,
    snapshot_id: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> QualityRuleEvaluateResponse:
    # The snapshot's persisted rows are evaluated as-is and never modified;
    # every successful evaluation appends the same history summary as a
    # row-submission evaluation. The endpoint takes no request body and no
    # query parameters — both are a 422 validated in the repository once the
    # path dataset/version/snapshot is known, preserving 404 precedence.
    return QualityRuleEvaluateResponse(
        **repository.evaluate_snapshot_quality_rules(
            conn,
            dataset_name,
            version,
            snapshot_id,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )


# --------------------------------------------------------------------------- #
# Retention policies and lineage-aware snapshot deletion
# --------------------------------------------------------------------------- #


RETENTION_POLICIES_PATH = (
    "/datasets/{dataset_name}/versions/{version}/retention-policies"
)


@app.post(
    RETENTION_POLICIES_PATH,
    response_model=RetentionPolicy,
    status_code=201,
)
def create_retention_policy(
    dataset_name: str,
    version: int,
    payload: RetentionPolicyCreate,
    conn=Depends(get_db),
) -> RetentionPolicy:
    return RetentionPolicy(
        **repository.create_retention_policy(
            conn, dataset_name, version, payload.retention_days
        )
    )


DELETION_REQUESTS_PATH = f"{SNAPSHOTS_PATH}/{{snapshot_id}}/deletion-requests"


@app.post(
    DELETION_REQUESTS_PATH,
    response_model=SnapshotDeletionRequest,
    status_code=201,
)
def create_snapshot_deletion_request(
    dataset_name: str,
    version: int,
    snapshot_id: int,
    payload: SnapshotDeletionRequestCreate,
    conn=Depends(get_db),
) -> SnapshotDeletionRequest:
    return SnapshotDeletionRequest(
        **repository.create_snapshot_deletion_request(
            conn, dataset_name, version, snapshot_id, payload.reason
        )
    )


@app.get(
    DELETION_REQUESTS_PATH,
    response_model=list[SnapshotDeletionRequest],
)
def list_snapshot_deletion_requests(
    dataset_name: str,
    version: int,
    snapshot_id: int,
    conn=Depends(get_db),
) -> list[SnapshotDeletionRequest]:
    return [
        SnapshotDeletionRequest(**request)
        for request in repository.list_snapshot_deletion_requests(
            conn, dataset_name, version, snapshot_id
        )
    ]


@app.post(
    f"{DELETION_REQUESTS_PATH}/{{request_id}}/confirm",
    response_model=ConfirmedSnapshotDeletionRequest,
)
def confirm_snapshot_deletion_request(
    dataset_name: str,
    version: int,
    snapshot_id: int,
    request_id: int,
    conn=Depends(get_db),
) -> ConfirmedSnapshotDeletionRequest:
    return ConfirmedSnapshotDeletionRequest(
        **repository.confirm_snapshot_deletion_request(
            conn, dataset_name, version, snapshot_id, request_id
        )
    )


# --------------------------------------------------------------------------- #
# Retention exceptions (compliance holds blocking snapshot deletion)
# --------------------------------------------------------------------------- #


RETENTION_EXCEPTIONS_PATH = (
    "/datasets/{dataset_name}/versions/{version}/retention-exceptions"
)


@app.post(
    RETENTION_EXCEPTIONS_PATH,
    response_model=RetentionException,
    status_code=201,
)
def create_retention_exception(
    dataset_name: str,
    version: int,
    payload: RetentionExceptionCreate,
    conn=Depends(get_db),
) -> RetentionException:
    return RetentionException(
        **repository.create_retention_exception(
            conn,
            dataset_name,
            version,
            payload.scope,
            payload.snapshot_id,
            payload.reason,
            payload.expires_at,
        )
    )


@app.get(RETENTION_EXCEPTIONS_PATH, response_model=list[RetentionException])
def list_retention_exceptions(
    dataset_name: str, version: int, conn=Depends(get_db)
) -> list[RetentionException]:
    return [
        RetentionException(**exception)
        for exception in repository.list_retention_exceptions(
            conn, dataset_name, version
        )
    ]


@app.post(
    f"{RETENTION_EXCEPTIONS_PATH}/{{exception_id}}/release",
    response_model=RetentionException,
)
def release_retention_exception(
    dataset_name: str,
    version: int,
    exception_id: int,
    conn=Depends(get_db),
) -> RetentionException:
    return RetentionException(
        **repository.release_retention_exception(
            conn, dataset_name, version, exception_id
        )
    )


# --------------------------------------------------------------------------- #
# Processing tasks
# --------------------------------------------------------------------------- #


PROCESSING_TASKS_PATH = "/datasets/{dataset_name}/versions/{version}/processing-tasks"


@app.post(PROCESSING_TASKS_PATH, response_model=ProcessingTask, status_code=201)
def create_processing_task(
    dataset_name: str,
    version: int,
    payload: ProcessingTaskCreate,
    conn=Depends(get_db),
) -> ProcessingTask:
    return ProcessingTask(
        **repository.create_processing_task(
            conn,
            dataset_name,
            version,
            payload.name,
            payload.depends_on,
            payload.max_attempts,
        )
    )


@app.get(PROCESSING_TASKS_PATH, response_model=list[ProcessingTask])
def list_processing_tasks(
    dataset_name: str, version: int, conn=Depends(get_db)
) -> list[ProcessingTask]:
    return [
        ProcessingTask(**task)
        for task in repository.list_processing_tasks(conn, dataset_name, version)
    ]


# Declared before the "/{task_id}" route so the literal "schedule" segment is not
# parsed as a task id (mirrors the snapshots "/at" route).
@app.get(
    f"{PROCESSING_TASKS_PATH}/schedule",
    response_model=ProcessingScheduleResponse,
)
def get_processing_schedule(
    dataset_name: str, version: int, conn=Depends(get_db)
) -> ProcessingScheduleResponse:
    return ProcessingScheduleResponse(
        **repository.get_processing_schedule(conn, dataset_name, version)
    )


# The report is a parameterless read of the whole task collection, so the
# literal "audit-report" segment must likewise be declared before "/{task_id}".
@app.get(
    f"{PROCESSING_TASKS_PATH}/audit-report",
    response_model=ProcessingAuditReportResponse,
)
def get_processing_audit_report(
    request: Request,
    dataset_name: str,
    version: int,
    body: bytes = Depends(_read_request_body),
    conn=Depends(get_db),
) -> ProcessingAuditReportResponse:
    # The endpoint is parameterless; any body bytes or query parameters are a
    # 422 validated in the repository once the path dataset/version is known,
    # preserving 404 precedence.
    return ProcessingAuditReportResponse(
        **repository.get_processing_audit_report(
            conn,
            dataset_name,
            version,
            body=body,
            query_keys=tuple(request.query_params.keys()),
        )
    )


# Likewise, the literal "dispatch" segment must not be parsed as a task id. The
# body is optional: an empty request dispatches a single task.
@app.post(
    f"{PROCESSING_TASKS_PATH}/dispatch",
    response_model=ProcessingTaskDispatchResponse,
    status_code=201,
)
def dispatch_processing_tasks(
    dataset_name: str,
    version: int,
    payload: ProcessingTaskDispatchRequest = ProcessingTaskDispatchRequest(),
    conn=Depends(get_db),
) -> ProcessingTaskDispatchResponse:
    return ProcessingTaskDispatchResponse(
        **repository.dispatch_processing_tasks(
            conn, dataset_name, version, payload.limit
        )
    )


# The literal "batch-complete" segment must not be parsed as a task id (mirrors
# the "dispatch" and "schedule" routes). The body is required and contains only
# the non-empty "runs" array. Items are dumped with exclude_unset so the
# repository can tell an omitted "error" field apart from an explicitly
# supplied one (a success item must omit it entirely).
@app.post(
    f"{PROCESSING_TASKS_PATH}/batch-complete",
    response_model=ProcessingRunBatchCompleteResponse,
)
def batch_complete_processing_task_runs(
    request: Request,
    dataset_name: str,
    version: int,
    payload: ProcessingRunBatchCompleteRequest,
    conn=Depends(get_db),
) -> ProcessingRunBatchCompleteResponse:
    return ProcessingRunBatchCompleteResponse(
        **repository.batch_complete_task_runs(
            conn,
            dataset_name,
            version,
            [item.model_dump(exclude_unset=True) for item in payload.runs],
            query_keys=tuple(request.query_params.keys()),
        )
    )


@app.put(
    f"{PROCESSING_TASKS_PATH}/{{task_id}}/dependencies",
    response_model=ProcessingTask,
)
def replace_processing_task_dependencies(
    dataset_name: str,
    version: int,
    task_id: int,
    payload: ProcessingTaskDependenciesUpdate,
    conn=Depends(get_db),
) -> ProcessingTask:
    return ProcessingTask(
        **repository.replace_task_dependencies(
            conn, dataset_name, version, task_id, payload.depends_on
        )
    )


@app.get(
    f"{PROCESSING_TASKS_PATH}/{{task_id}}", response_model=ProcessingTaskWithRuns
)
def get_processing_task(
    dataset_name: str,
    version: int,
    task_id: int,
    conn=Depends(get_db),
) -> ProcessingTaskWithRuns:
    return ProcessingTaskWithRuns(
        **repository.get_processing_task(conn, dataset_name, version, task_id)
    )


@app.post(
    f"{PROCESSING_TASKS_PATH}/{{task_id}}/runs",
    response_model=ProcessingTaskRun,
    status_code=201,
)
def create_task_run(
    dataset_name: str,
    version: int,
    task_id: int,
    conn=Depends(get_db),
) -> ProcessingTaskRun:
    return ProcessingTaskRun(
        **repository.create_task_run(conn, dataset_name, version, task_id)
    )


@app.patch(
    f"{PROCESSING_TASKS_PATH}/{{task_id}}/runs/{{run_id}}",
    response_model=ProcessingTaskRun,
)
def finish_task_run(
    dataset_name: str,
    version: int,
    task_id: int,
    run_id: int,
    payload: ProcessingRunFinish,
    conn=Depends(get_db),
) -> ProcessingTaskRun:
    return ProcessingTaskRun(
        **repository.finish_task_run(
            conn,
            dataset_name,
            version,
            task_id,
            run_id,
            payload.status,
            payload.error,
        )
    )


@app.post(
    f"{PROCESSING_TASKS_PATH}/{{task_id}}/runs/{{run_id}}/cancel",
    response_model=ProcessingTaskRun,
)
def cancel_task_run(
    request: Request,
    dataset_name: str,
    version: int,
    task_id: int,
    run_id: int,
    payload: ProcessingRunCancel,
    conn=Depends(get_db),
) -> ProcessingTaskRun:
    # Cancellation takes only the non-empty reason; any query parameter is a
    # 422 checked in the repository after the path resolves, preserving 404
    # precedence (mirrors the parameterless audit-report endpoint).
    return ProcessingTaskRun(
        **repository.cancel_task_run(
            conn,
            dataset_name,
            version,
            task_id,
            run_id,
            payload.reason,
            query_keys=tuple(request.query_params.keys()),
        )
    )


# --------------------------------------------------------------------------- #
# Processing task run audit records (append-only proof chain)
# --------------------------------------------------------------------------- #


AUDIT_RECORDS_PATH = (
    f"{PROCESSING_TASKS_PATH}/{{task_id}}/runs/{{run_id}}/audit-records"
)


@app.post(AUDIT_RECORDS_PATH, response_model=AuditRecord, status_code=201)
def create_audit_record(
    dataset_name: str,
    version: int,
    task_id: int,
    run_id: int,
    payload: AuditRecordCreate,
    conn=Depends(get_db),
) -> AuditRecord:
    return AuditRecord(
        **repository.create_audit_record(
            conn,
            dataset_name,
            version,
            task_id,
            run_id,
            payload.event,
            payload.input_summary,
            payload.result_summary,
        )
    )


# Declared before the "/{audit_record_id}" style routes would exist; the literal
# "verify" segment must not be mistaken for an id.
@app.get(f"{AUDIT_RECORDS_PATH}/verify", response_model=AuditChainVerifyResponse)
def verify_run_audit_chain(
    dataset_name: str,
    version: int,
    task_id: int,
    run_id: int,
    conn=Depends(get_db),
) -> AuditChainVerifyResponse:
    return AuditChainVerifyResponse(
        **repository.verify_audit_chain(
            conn, dataset_name, version, task_id, run_id
        )
    )


@app.get(AUDIT_RECORDS_PATH, response_model=list[AuditRecord])
def list_audit_records(
    dataset_name: str,
    version: int,
    task_id: int,
    run_id: int,
    conn=Depends(get_db),
) -> list[AuditRecord]:
    return [
        AuditRecord(**record)
        for record in repository.list_audit_records(
            conn, dataset_name, version, task_id, run_id
        )
    ]
