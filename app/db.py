"""SQLite-backed persistence for datasets, schema versions, and lineage edges."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id INTEGER NOT NULL REFERENCES datasets (id),
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (dataset_id, version)
);
CREATE TABLE IF NOT EXISTS fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version_id INTEGER NOT NULL REFERENCES schema_versions (id),
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    nullable INTEGER NOT NULL,
    UNIQUE (schema_version_id, name)
);
CREATE TABLE IF NOT EXISTS lineage_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_dataset_id INTEGER NOT NULL REFERENCES datasets (id),
    target_version INTEGER NOT NULL,
    target_field TEXT NOT NULL,
    source_dataset_id INTEGER NOT NULL REFERENCES datasets (id),
    source_version INTEGER NOT NULL,
    source_field TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (
        target_dataset_id, target_version, target_field,
        source_dataset_id, source_version, source_field
    )
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DuplicateError(Exception):
    """A uniqueness constraint was violated."""


class Database:
    """Thread-safe wrapper around a single SQLite connection."""

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- datasets ---------------------------------------------------------

    def create_dataset(self, name: str, description: str | None) -> dict[str, Any]:
        with self._lock, self._conn:
            try:
                cursor = self._conn.execute(
                    "INSERT INTO datasets (name, description, created_at) VALUES (?, ?, ?)",
                    (name, description, _utc_now()),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateError(f"dataset '{name}' already exists") from exc
            return self.get_dataset_by_id(cursor.lastrowid)  # type: ignore[arg-type]

    def get_dataset_by_id(self, dataset_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT id, name, description, created_at FROM datasets WHERE id = ?",
            (dataset_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_dataset_by_name(self, name: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT id, name, description, created_at FROM datasets WHERE name = ?",
            (name,),
        ).fetchone()
        return dict(row) if row else None

    def list_datasets(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, name, description, created_at FROM datasets ORDER BY name"
        ).fetchall()
        return [dict(row) for row in rows]

    # -- schema versions --------------------------------------------------

    def create_schema_version(
        self, dataset_id: int, fields: list[dict[str, Any]]
    ) -> dict[str, Any]:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next_version "
                "FROM schema_versions WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchone()
            version = int(row["next_version"])
            cursor = self._conn.execute(
                "INSERT INTO schema_versions (dataset_id, version, created_at) "
                "VALUES (?, ?, ?)",
                (dataset_id, version, _utc_now()),
            )
            version_id = cursor.lastrowid
            for field in fields:
                self._conn.execute(
                    "INSERT INTO fields (schema_version_id, name, type, nullable) "
                    "VALUES (?, ?, ?, ?)",
                    (version_id, field["name"], field["type"], int(field["nullable"])),
                )
            return self.get_schema_version(dataset_id, version)  # type: ignore[return-value]

    def get_schema_version(
        self, dataset_id: int, version: int
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT id, version, created_at FROM schema_versions "
            "WHERE dataset_id = ? AND version = ?",
            (dataset_id, version),
        ).fetchone()
        if row is None:
            return None
        field_rows = self._conn.execute(
            "SELECT name, type, nullable FROM fields "
            "WHERE schema_version_id = ? ORDER BY name",
            (row["id"],),
        ).fetchall()
        return {
            "version": row["version"],
            "created_at": row["created_at"],
            "fields": [
                {"name": f["name"], "type": f["type"], "nullable": bool(f["nullable"])}
                for f in field_rows
            ],
        }

    def list_schema_versions(self, dataset_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT version FROM schema_versions WHERE dataset_id = ? ORDER BY version",
            (dataset_id,),
        ).fetchall()
        return [
            self.get_schema_version(dataset_id, row["version"])  # type: ignore[misc]
            for row in rows
        ]

    # -- lineage ----------------------------------------------------------

    def create_lineage_edge(
        self,
        target_dataset_id: int,
        target_version: int,
        target_field: str,
        source_dataset_id: int,
        source_version: int,
        source_field: str,
    ) -> dict[str, Any]:
        with self._lock, self._conn:
            try:
                cursor = self._conn.execute(
                    "INSERT INTO lineage_edges ("
                    "  target_dataset_id, target_version, target_field,"
                    "  source_dataset_id, source_version, source_field, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        target_dataset_id,
                        target_version,
                        target_field,
                        source_dataset_id,
                        source_version,
                        source_field,
                        _utc_now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateError("lineage edge already exists") from exc
            row = self._conn.execute(
                "SELECT id, created_at FROM lineage_edges WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            return {"id": row["id"], "created_at": row["created_at"]}

    def get_lineage_for_version(
        self, target_dataset_id: int, target_version: int
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT e.target_field, d.name AS source_dataset, e.source_version, "
            "       e.source_field "
            "FROM lineage_edges e "
            "JOIN datasets d ON d.id = e.source_dataset_id "
            "WHERE e.target_dataset_id = ? AND e.target_version = ? "
            "ORDER BY e.target_field, source_dataset, e.source_version, e.source_field",
            (target_dataset_id, target_version),
        ).fetchall()
        return [dict(row) for row in rows]
