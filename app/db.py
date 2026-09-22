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
