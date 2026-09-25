"""SQLite persistence for dataset versions and field-level lineage.

The database file location is configured through the ``DATA_LINEAGE_DB``
environment variable so tests (and deployments) can point the service at an
isolated database. A new connection is opened per request and nothing is cached
in process memory, which keeps persisted data available across restarts.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = "data/lineage.db"

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS datasets (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schema_versions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
        version    INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (dataset_id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schema_fields (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id INTEGER NOT NULL REFERENCES schema_versions(id) ON DELETE CASCADE,
        name       TEXT NOT NULL,
        type       TEXT NOT NULL,
        nullable   INTEGER NOT NULL CHECK (nullable IN (0, 1)),
        position   INTEGER NOT NULL,
        UNIQUE (version_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lineage_links (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        target_dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
        target_version_id INTEGER NOT NULL REFERENCES schema_versions(id) ON DELETE CASCADE,
        target_field_id   INTEGER NOT NULL REFERENCES schema_fields(id) ON DELETE CASCADE,
        source_dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
        source_version_id INTEGER NOT NULL REFERENCES schema_versions(id) ON DELETE CASCADE,
        source_field_id   INTEGER NOT NULL REFERENCES schema_fields(id) ON DELETE CASCADE,
        created_at        TEXT NOT NULL,
        UNIQUE (target_field_id, source_field_id),
        CHECK (source_dataset_id <> target_dataset_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quality_rules (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id INTEGER NOT NULL REFERENCES schema_versions(id) ON DELETE CASCADE,
        name       TEXT NOT NULL,
        kind       TEXT NOT NULL,
        params     TEXT NOT NULL,
        enabled    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        created_at TEXT NOT NULL,
        UNIQUE (version_id, name)
    )
    """,
    # Append-only history of quality-rule evaluation summaries. ``results``
    # holds the exact per-rule outcome list of the evaluation as JSON;
    # ``sequence`` numbers the evaluations of one version in occurrence order.
    """
    CREATE TABLE IF NOT EXISTS quality_rule_evaluations (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id          INTEGER NOT NULL
                            REFERENCES schema_versions(id) ON DELETE CASCADE,
        sequence            INTEGER NOT NULL CHECK (sequence >= 1),
        row_count           INTEGER NOT NULL CHECK (row_count >= 0),
        violation_row_count INTEGER NOT NULL CHECK (violation_row_count >= 0),
        results             TEXT NOT NULL,
        created_at          TEXT NOT NULL,
        UNIQUE (version_id, sequence)
    )
    """,
    # At most one anomaly detection config per schema version; the three
    # thresholds are constrained here as well so a stored config can never be
    # out of range.
    """
    CREATE TABLE IF NOT EXISTS quality_anomaly_detection_configs (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id                  INTEGER NOT NULL UNIQUE
                                    REFERENCES schema_versions(id) ON DELETE CASCADE,
        consecutive_worsening_steps INTEGER NOT NULL
                                    CHECK (consecutive_worsening_steps >= 2),
        violation_row_limit         INTEGER NOT NULL
                                    CHECK (violation_row_limit >= 0),
        rule_violation_limit        INTEGER NOT NULL
                                    CHECK (rule_violation_limit >= 0),
        created_at                  TEXT NOT NULL
    )
    """,
    # Append-only anomaly records produced by scans of the evaluation history.
    # ``rule_id`` is set only for 'rule_limit' records; ``sequence`` is the
    # history sequence of the evaluation the record points to.
    """
    CREATE TABLE IF NOT EXISTS quality_anomaly_records (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id      INTEGER NOT NULL
                        REFERENCES schema_versions(id) ON DELETE CASCADE,
        kind            TEXT NOT NULL
                        CHECK (kind IN ('row_limit', 'rule_limit', 'trend')),
        sequence        INTEGER NOT NULL CHECK (sequence >= 1),
        rule_id         INTEGER,
        violation_count INTEGER NOT NULL CHECK (violation_count >= 0),
        created_at      TEXT NOT NULL
    )
    """,
    # One record per (kind, history sequence, rule) within a version; the null
    # rule ids of row-limit and trend records dedupe through the IFNULL
    # expression (plain UNIQUE would treat NULLs as distinct).
    """
    CREATE UNIQUE INDEX IF NOT EXISTS
        idx_quality_anomaly_records_unique
    ON quality_anomaly_records (version_id, kind, sequence, IFNULL(rule_id, 0))
    """,
    """
    CREATE TABLE IF NOT EXISTS privacy_policies (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id    INTEGER NOT NULL REFERENCES schema_versions(id) ON DELETE CASCADE,
        field         TEXT NOT NULL,
        classification TEXT NOT NULL,
        masking       TEXT NOT NULL CHECK (masking IN ('redact', 'partial')),
        allowed_roles TEXT NOT NULL,
        enabled       INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        created_at    TEXT NOT NULL,
        UNIQUE (version_id, field)
    )
    """,
    # Append-only audit of privacy-view masking hits: one record per value
    # masked by a view request (a field masked in several rows yields one
    # record per masked value). ``sequence`` numbers the records of
    # one version in write order; ``policy_id`` keeps the id of the policy
    # that masked the field (policies are never deleted, only enabled or
    # disabled, so the plain integer reference always resolves).
    """
    CREATE TABLE IF NOT EXISTS privacy_view_audit_records (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id INTEGER NOT NULL REFERENCES schema_versions(id) ON DELETE CASCADE,
        sequence   INTEGER NOT NULL CHECK (sequence >= 1),
        field      TEXT NOT NULL,
        policy_id  INTEGER NOT NULL,
        role       TEXT NOT NULL,
        masking    TEXT NOT NULL CHECK (masking IN ('redact', 'partial')),
        created_at TEXT NOT NULL,
        UNIQUE (version_id, sequence)
    )
    """,
    # Append-only access log of privacy views: exactly one record per
    # successful view request (independent of how many values it masked, if
    # any). ``sequence`` numbers the records of one version in write order;
    # ``masked_count`` equals the number of masking-hit records written by the
    # same view so the two logs can be cross-checked.
    """
    CREATE TABLE IF NOT EXISTS privacy_view_access_records (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id   INTEGER NOT NULL REFERENCES schema_versions(id) ON DELETE CASCADE,
        sequence     INTEGER NOT NULL CHECK (sequence >= 1),
        role         TEXT NOT NULL,
        row_count    INTEGER NOT NULL CHECK (row_count >= 0),
        masked_count INTEGER NOT NULL CHECK (masked_count >= 0),
        created_at   TEXT NOT NULL,
        UNIQUE (version_id, sequence)
    )
    """,
    # High-water allocator for masking-hit sequences. Cleaning removes hit
    # records (so MAX(sequence) can no longer be used to continue the run),
    # but this counter survives those deletions: new hits keep the continuous,
    # never-reused sequence rule. One row per version that has ever written a
    # hit record; ``next_sequence`` is the sequence of the next hit to append.
    """
    CREATE TABLE IF NOT EXISTS privacy_view_audit_sequences (
        version_id    INTEGER PRIMARY KEY
                      REFERENCES schema_versions(id) ON DELETE CASCADE,
        next_sequence INTEGER NOT NULL CHECK (next_sequence >= 1)
    )
    """,
    # Two-stage preview/confirm cleanup of masking-hit records. A request is
    # created under a version's hit-record collection with a reason and a
    # timezone-bearing ``before`` cutoff; its target set is fixed at creation
    # time (snapshotted in privacy_view_audit_cleanup_targets) and only removed
    # when the request is confirmed. ``preview`` keeps the exact preview block
    # returned at creation so the request stays fully readable after the target
    # records have been deleted. The version link is a plain integer, mirroring
    # snapshot_deletion_requests.
    """
    CREATE TABLE IF NOT EXISTS privacy_view_audit_cleanup_requests (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id    INTEGER NOT NULL,
        reason        TEXT NOT NULL,
        before        TEXT NOT NULL,
        status        TEXT NOT NULL CHECK (status IN ('pending', 'confirmed')),
        preview       TEXT NOT NULL,
        deleted_count INTEGER CHECK (deleted_count IS NULL OR deleted_count >= 0),
        created_at    TEXT NOT NULL,
        confirmed_at  TEXT
    )
    """,
    # At most one pending cleanup request per version; a confirmed request
    # never blocks a new one.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS
        idx_privacy_view_audit_cleanup_one_pending
    ON privacy_view_audit_cleanup_requests (version_id)
    WHERE status = 'pending'
    """,
    # Frozen target set of a cleanup request: one row per masking-hit record
    # the request selected at creation time. Rows are retained after the
    # records are deleted, keeping the request history and the delete trigger's
    # decision auditable.
    """
    CREATE TABLE IF NOT EXISTS privacy_view_audit_cleanup_targets (
        request_id      INTEGER NOT NULL
                        REFERENCES privacy_view_audit_cleanup_requests(id)
                        ON DELETE CASCADE,
        audit_record_id INTEGER NOT NULL,
        PRIMARY KEY (request_id, audit_record_id)
    )
    """,
    # Candidate sensitive-field identification results, one per (version,
    # field): a re-run refreshes evidence/confidence/field_type in place while
    # the id and created_at stay fixed. The submitted samples are deliberately
    # not stored, so a row never carries raw values.
    """
    CREATE TABLE IF NOT EXISTS sensitive_identifications (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id  INTEGER NOT NULL REFERENCES schema_versions(id) ON DELETE CASCADE,
        field       TEXT NOT NULL,
        field_type  TEXT NOT NULL,
        evidence    TEXT NOT NULL,
        confidence  TEXT NOT NULL
                    CHECK (confidence IN ('high', 'medium', 'low', 'none')),
        created_at  TEXT NOT NULL,
        UNIQUE (version_id, field)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id INTEGER NOT NULL REFERENCES schema_versions(id) ON DELETE CASCADE,
        row_count  INTEGER NOT NULL CHECK (row_count >= 0),
        rows       TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS processing_tasks (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id    INTEGER NOT NULL REFERENCES schema_versions(id) ON DELETE CASCADE,
        name          TEXT NOT NULL,
        depends_on    TEXT NOT NULL DEFAULT '[]',
        max_attempts  INTEGER NOT NULL CHECK (max_attempts >= 1),
        status        TEXT NOT NULL
                      CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        created_at    TEXT NOT NULL,
        UNIQUE (version_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS processing_task_runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id     INTEGER NOT NULL REFERENCES processing_tasks(id) ON DELETE CASCADE,
        attempt     INTEGER NOT NULL,
        status      TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        error       TEXT,
        UNIQUE (task_id, attempt)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS processing_task_audit_records (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id        INTEGER NOT NULL
                      REFERENCES processing_task_runs(id) ON DELETE CASCADE,
        sequence      INTEGER NOT NULL CHECK (sequence >= 1),
        event         TEXT NOT NULL,
        input_summary TEXT NOT NULL,
        result_summary TEXT NOT NULL,
        run_status    TEXT NOT NULL
                      CHECK (run_status IN ('running', 'succeeded', 'failed')),
        previous_hash TEXT,
        evidence_hash TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        UNIQUE (run_id, sequence)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS retention_policies (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id     INTEGER NOT NULL UNIQUE
                       REFERENCES schema_versions(id) ON DELETE CASCADE,
        retention_days INTEGER NOT NULL CHECK (retention_days >= 0),
        created_at     TEXT NOT NULL
    )
    """,
    # A confirmed request must survive the deletion of its snapshot, so the
    # snapshot link is intentionally a plain integer instead of a cascading
    # foreign key (snapshots are only deleted through a confirmed request).
    """
    CREATE TABLE IF NOT EXISTS snapshot_deletion_requests (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id   INTEGER NOT NULL,
        snapshot_id  INTEGER NOT NULL,
        policy_id    INTEGER NOT NULL
                     REFERENCES retention_policies(id) ON DELETE CASCADE,
        reason       TEXT NOT NULL,
        status       TEXT NOT NULL
                     CHECK (status IN ('pending', 'blocked', 'confirmed')),
        impacted     TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        confirmed_at TEXT
    )
    """,
    # At most one open (pending/blocked) request per snapshot; confirmed
    # requests never block a new request once the snapshot is deleted.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS
        idx_snapshot_deletion_requests_one_open
    ON snapshot_deletion_requests (snapshot_id)
    WHERE status IN ('pending', 'blocked')
    """,
    # A released or expired exception must survive the deletion of the snapshot
    # it named, so the snapshot link is intentionally a plain integer instead of
    # a cascading foreign key (mirroring snapshot_deletion_requests).
    """
    CREATE TABLE IF NOT EXISTS retention_exceptions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id  INTEGER NOT NULL
                    REFERENCES schema_versions(id) ON DELETE CASCADE,
        scope       TEXT NOT NULL CHECK (scope IN ('version', 'snapshot')),
        snapshot_id INTEGER,
        reason      TEXT NOT NULL,
        expires_at  TEXT NOT NULL,
        status      TEXT NOT NULL
                    CHECK (status IN ('active', 'released')),
        created_at  TEXT NOT NULL,
        released_at TEXT,
        CHECK (
            (scope = 'version' AND snapshot_id IS NULL) OR
            (scope = 'snapshot' AND snapshot_id IS NOT NULL)
        )
    )
    """,
    # Persistent cache of field-impact query results, keyed by the source
    # field. Entries are invalidated whenever a committed write could change
    # what is reachable from the cached source (see app.repository).
    """
    CREATE TABLE IF NOT EXISTS lineage_impact_cache (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        source_dataset TEXT NOT NULL,
        source_version INTEGER NOT NULL,
        source_field   TEXT NOT NULL,
        impacted       TEXT NOT NULL,
        created_at     TEXT NOT NULL,
        UNIQUE (source_dataset, source_version, source_field)
    )
    """,
    # Datasets mentioned by each cache entry (as source or inside the impacted
    # result) so that per-dataset invalidation stays a simple indexed delete.
    """
    CREATE TABLE IF NOT EXISTS lineage_impact_cache_datasets (
        cache_id INTEGER NOT NULL
                 REFERENCES lineage_impact_cache(id) ON DELETE CASCADE,
        dataset  TEXT NOT NULL
    )
    """,
    # Audit records are an append-only proof chain: the database itself refuses
    # updates and deletes so the evidence history cannot be rewritten.
    """
    CREATE TRIGGER IF NOT EXISTS trg_processing_audit_records_no_update
    BEFORE UPDATE ON processing_task_audit_records
    BEGIN
        SELECT RAISE(ABORT, 'processing task audit records are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_processing_audit_records_no_delete
    BEFORE DELETE ON processing_task_audit_records
    BEGIN
        SELECT RAISE(ABORT, 'processing task audit records are immutable');
    END
    """,
    # Evaluation history is append-only as well: the database itself refuses
    # updates and deletes so a recorded summary can never be rewritten.
    """
    CREATE TRIGGER IF NOT EXISTS trg_quality_rule_evaluations_no_update
    BEFORE UPDATE ON quality_rule_evaluations
    BEGIN
        SELECT RAISE(ABORT, 'quality rule evaluation records are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_quality_rule_evaluations_no_delete
    BEFORE DELETE ON quality_rule_evaluations
    BEGIN
        SELECT RAISE(ABORT, 'quality rule evaluation records are immutable');
    END
    """,
    # Privacy view audit records are append-only as well: the database itself
    # refuses updates and deletes so the masking-hit history cannot be
    # rewritten.
    """
    CREATE TRIGGER IF NOT EXISTS trg_privacy_view_audit_records_no_update
    BEFORE UPDATE ON privacy_view_audit_records
    BEGIN
        SELECT RAISE(ABORT, 'privacy view audit records are immutable');
    END
    """,
    # Privacy view audit records are append-only apart from the two-stage
    # cleanup flow: the database refuses updates outright and refuses deletes
    # except for records frozen as the target of a cleanup request (see
    # privacy_view_audit_cleanup_targets). The trigger is dropped and recreated
    # so databases initialized by an earlier revision pick up the gated form.
    "DROP TRIGGER IF EXISTS trg_privacy_view_audit_records_no_delete",
    """
    CREATE TRIGGER IF NOT EXISTS trg_privacy_view_audit_records_no_delete
    BEFORE DELETE ON privacy_view_audit_records
    FOR EACH ROW
    WHEN NOT EXISTS (
        SELECT 1 FROM privacy_view_audit_cleanup_targets
        WHERE audit_record_id = OLD.id
    )
    BEGIN
        SELECT RAISE(ABORT, 'privacy view audit records are immutable');
    END
    """,
    # Privacy view access records are append-only as well: the database itself
    # refuses updates and deletes so the access history cannot be rewritten.
    """
    CREATE TRIGGER IF NOT EXISTS trg_privacy_view_access_records_no_update
    BEFORE UPDATE ON privacy_view_access_records
    BEGIN
        SELECT RAISE(ABORT, 'privacy view access records are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_privacy_view_access_records_no_delete
    BEFORE DELETE ON privacy_view_access_records
    BEGIN
        SELECT RAISE(ABORT, 'privacy view access records are immutable');
    END
    """,
)


def database_path() -> Path:
    return Path(os.environ.get("DATA_LINEAGE_DB", DEFAULT_DB_PATH))


def _connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Schema creation is idempotent and committed independently, so every
    # request works against an initialized database file.
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    # Establish every version's sequence high-water before any cleanup-aware
    # code can delete records: at this point the surviving maximum equals the
    # historical maximum, so seeding here (and INSERT OR IGNORE on every later
    # connect) keeps the counter ahead of cleaned sequences forever.
    conn.execute(
        """
        INSERT OR IGNORE INTO privacy_view_audit_sequences
            (version_id, next_sequence)
        SELECT versions.id,
               COALESCE((
                   SELECT MAX(records.sequence)
                   FROM privacy_view_audit_records AS records
                   WHERE records.version_id = versions.id
               ), 0) + 1
        FROM schema_versions AS versions
        """
    )
    conn.commit()
    return conn


@contextmanager
def db_session() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency yielding a transactional database connection."""
    with db_session() as conn:
        yield conn
