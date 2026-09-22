"""HTTP API for datasets, immutable schema versions and field lineage."""

from __future__ import annotations

import sqlite3

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import repository
from app.db import get_db
from app.errors import APIError, RequestInvalidError
from app.models import (
    AuditChainVerifyResponse,
    AuditRecord,
    AuditRecordCreate,
    Dataset,
    DatasetCreate,
    LineageCreate,
    LineageCreatedResponse,
    LineageImpactResponse,
    LineageResponse,
    PrivacyPolicy,
    PrivacyPolicyCreate,
    PrivacyPolicyEnabledUpdate,
    PrivacyViewRequest,
    PrivacyViewResponse,
    ProcessingRunFinish,
    ProcessingTask,
    ProcessingTaskCreate,
    ProcessingTaskRun,
    ProcessingTaskWithRuns,
    QualityRule,
    QualityRuleCreate,
    QualityRuleEnabledUpdate,
    QualityRuleEvaluateRequest,
    QualityRuleEvaluateResponse,
    RetentionPolicy,
    RetentionPolicyCreate,
    RetentionException,
    RetentionExceptionCreate,
    SchemaVersion,
    SchemaVersionCreate,
    SnapshotCreate,
    SnapshotDeletionRequest,
    ConfirmedSnapshotDeletionRequest,
    SnapshotDeletionRequestCreate,
    SnapshotDiffResponse,
    SnapshotMetadata,
    SnapshotResponse,
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


@app.get(
    "/datasets/{dataset_name}/versions/{version}/lineage/impact",
    response_model=LineageImpactResponse,
)
def get_lineage_impact(
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
            conn, dataset_name, version, field.strip()
        )
    )


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
# Retention exceptions (compliance holds on snapshot deletion)
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
