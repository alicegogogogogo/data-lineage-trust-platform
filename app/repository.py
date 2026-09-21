"""Data-access and business rules for datasets, schema versions, lineage and
quality rules.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.errors import ConflictError, NotFoundError, RequestInvalidError
from app.models import FieldSpec
from app.quality import RULE_KINDS, evaluate_rule


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


# --------------------------------------------------------------------------- #
# Datasets
# --------------------------------------------------------------------------- #


def get_dataset_by_name(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute("SELECT * FROM datasets WHERE name = ?", (name,)).fetchone()
    return _row_to_dict(row) if row is not None else None


def require_dataset(conn: sqlite3.Connection, name: str, *, role: str = "Dataset") -> dict:
    dataset = get_dataset_by_name(conn, name)
    if dataset is None:
        raise NotFoundError(f"{role} '{name}' does not exist")
    return dataset


def create_dataset(conn: sqlite3.Connection, name: str, description: str) -> dict:
    clean_name = name.strip()
    if not clean_name:
        raise RequestInvalidError("Dataset name must not be empty")

    created_at = utc_now_iso()
    try:
        cursor = conn.execute(
            "INSERT INTO datasets (name, description, created_at) VALUES (?, ?, ?)",
            (clean_name, description, created_at),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(f"Dataset '{clean_name}' already exists") from exc
    row = conn.execute(
        "SELECT id, name, description, created_at FROM datasets WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _row_to_dict(row)


def list_datasets(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, description, created_at FROM datasets ORDER BY name"
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# Schema versions and fields
# --------------------------------------------------------------------------- #


def _validate_fields(fields: list[FieldSpec]) -> list[tuple[str, str, int, int]]:
    if not fields:
        raise RequestInvalidError("Field list must not be empty")

    cleaned: list[tuple[str, str, bool]] = []
    seen: set[str] = set()
    for field in fields:
        field_name = field.name.strip()
        field_type = field.type.strip()
        if not field_name:
            raise RequestInvalidError("Field name must not be empty")
        if not field_type:
            raise RequestInvalidError(
                f"Field '{field_name}' is missing a non-empty type"
            )
        key = field_name
        if key in seen:
            raise RequestInvalidError(f"Duplicate field name '{field_name}' in version")
        seen.add(key)
        cleaned.append((field_name, field_type, field.nullable))

    return [
        (name, ftype, 1 if nullable else 0, position)
        for position, (name, ftype, nullable) in enumerate(cleaned)
    ]


def create_schema_version(
    conn: sqlite3.Connection, dataset_name: str, fields: list[FieldSpec]
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    validated = _validate_fields(fields)

    next_version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS next_version "
        "FROM schema_versions WHERE dataset_id = ?",
        (dataset["id"],),
    ).fetchone()["next_version"]
    created_at = utc_now_iso()

    try:
        version_cursor = conn.execute(
            "INSERT INTO schema_versions (dataset_id, version, created_at) "
            "VALUES (?, ?, ?)",
            (dataset["id"], next_version, created_at),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(
            f"Schema version {next_version} of dataset '{dataset_name}' was "
            "created concurrently; retry to receive the next version number"
        ) from exc
    version_id = version_cursor.lastrowid
    conn.executemany(
        "INSERT INTO schema_fields (version_id, name, type, nullable, position) "
        "VALUES (?, ?, ?, ?, ?)",
        [(version_id, name, ftype, nullable, position) for name, ftype, nullable, position in validated],
    )
    return get_schema_version(conn, dataset_name, next_version)


def get_schema_version(
    conn: sqlite3.Connection, dataset_name: str, version: int
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = conn.execute(
        "SELECT id, version, created_at FROM schema_versions "
        "WHERE dataset_id = ? AND version = ?",
        (dataset["id"], version),
    ).fetchone()
    if version_row is None:
        raise NotFoundError(
            f"Schema version {version} of dataset '{dataset_name}' does not exist"
        )

    field_rows = conn.execute(
        "SELECT name, type, nullable FROM schema_fields "
        "WHERE version_id = ? ORDER BY position, name",
        (version_row["id"],),
    ).fetchall()
    return {
        "dataset_id": dataset["id"],
        "dataset_name": dataset["name"],
        "version": version_row["version"],
        "created_at": version_row["created_at"],
        "fields": [
            {
                "name": row["name"],
                "type": row["type"],
                "nullable": bool(row["nullable"]),
            }
            for row in field_rows
        ],
    }


def list_schema_versions(conn: sqlite3.Connection, dataset_name: str) -> list[dict]:
    dataset = require_dataset(conn, dataset_name)
    version_rows = conn.execute(
        "SELECT version FROM schema_versions WHERE dataset_id = ? ORDER BY version",
        (dataset["id"],),
    ).fetchall()
    return [
        get_schema_version(conn, dataset_name, row["version"]) for row in version_rows
    ]


# --------------------------------------------------------------------------- #
# Lineage
# --------------------------------------------------------------------------- #


def _require_field(
    conn: sqlite3.Connection,
    dataset: dict,
    version_number: int,
    field_name: str,
    *,
    role: str,
) -> tuple[int, int]:
    version_row = conn.execute(
        "SELECT id FROM schema_versions WHERE dataset_id = ? AND version = ?",
        (dataset["id"], version_number),
    ).fetchone()
    if version_row is None:
        raise NotFoundError(
            f"{role} schema version {version_number} of dataset "
            f"'{dataset['name']}' does not exist"
        )

    field_row = conn.execute(
        "SELECT id FROM schema_fields WHERE version_id = ? AND name = ?",
        (version_row["id"], field_name),
    ).fetchone()
    if field_row is None:
        raise NotFoundError(
            f"{role} field '{field_name}' does not exist in version "
            f"{version_number} of dataset '{dataset['name']}'"
        )
    return version_row["id"], field_row["id"]


def create_lineage_link(
    conn: sqlite3.Connection,
    *,
    target_dataset: str,
    target_version: int,
    target_field: str,
    source_dataset: str,
    source_version: int,
    source_field: str,
) -> dict:
    target = require_dataset(conn, target_dataset, role="Target dataset")
    source = require_dataset(conn, source_dataset, role="Source dataset")

    if source["id"] == target["id"]:
        raise RequestInvalidError("Source and target datasets must be different")

    target_version_id, target_field_id = _require_field(
        conn, target, target_version, target_field, role="Target"
    )
    source_version_id, source_field_id = _require_field(
        conn, source, source_version, source_field, role="Source"
    )

    existing = conn.execute(
        "SELECT id FROM lineage_links WHERE target_field_id = ? AND source_field_id = ?",
        (target_field_id, source_field_id),
    ).fetchone()
    if existing is not None:
        raise ConflictError(
            "This field mapping has already been registered: "
            f"'{source_dataset}' v{source_version}.{source_field} -> "
            f"'{target_dataset}' v{target_version}.{target_field}"
        )

    try:
        conn.execute(
            "INSERT INTO lineage_links ("
            "target_dataset_id, target_version_id, target_field_id, "
            "source_dataset_id, source_version_id, source_field_id, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                target["id"],
                target_version_id,
                target_field_id,
                source["id"],
                source_version_id,
                source_field_id,
                utc_now_iso(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError("This field mapping has already been registered") from exc

    return {
        "target_dataset": target["name"],
        "target_version": target_version,
        "target_field": target_field,
        "source": {
            "dataset": source["name"],
            "version": source_version,
            "field": source_field,
        },
    }


def get_lineage(
    conn: sqlite3.Connection, dataset_name: str, version: int
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = conn.execute(
        "SELECT id FROM schema_versions WHERE dataset_id = ? AND version = ?",
        (dataset["id"], version),
    ).fetchone()
    if version_row is None:
        raise NotFoundError(
            f"Schema version {version} of dataset '{dataset_name}' does not exist"
        )

    field_rows = conn.execute(
        "SELECT id, name FROM schema_fields WHERE version_id = ? ORDER BY name",
        (version_row["id"],),
    ).fetchall()

    links = conn.execute(
        """
        SELECT  tf.name AS target_field,
                sd.name AS source_dataset,
                slv.version AS source_version,
                sf.name AS source_field
        FROM    lineage_links ll
        JOIN    schema_fields tf ON tf.id = ll.target_field_id
        JOIN    schema_versions slv ON slv.id = ll.source_version_id
        JOIN    schema_fields sf ON sf.id = ll.source_field_id
        JOIN    datasets sd ON sd.id = ll.source_dataset_id
        WHERE   ll.target_version_id = ?
        ORDER BY tf.name, sd.name, slv.version, sf.name
        """,
        (version_row["id"],),
    ).fetchall()

    sources_by_target: dict[str, list[dict]] = {row["name"]: [] for row in field_rows}
    for link in links:
        sources_by_target[link["target_field"]].append(
            {
                "dataset": link["source_dataset"],
                "version": link["source_version"],
                "field": link["source_field"],
            }
        )

    return {
        "target_dataset": dataset["name"],
        "target_version": version,
        "fields": [
            {"target_field": name, "sources": sources_by_target[name]}
            for name in sorted(sources_by_target)
        ],
    }


# --------------------------------------------------------------------------- #
# Quality rules
# --------------------------------------------------------------------------- #


def _require_version(conn: sqlite3.Connection, dataset_name: str, version: int) -> tuple[int, int]:
    """Resolve (dataset_id, version_id), 404 on unknown dataset or version."""
    dataset = require_dataset(conn, dataset_name)
    version_row = conn.execute(
        "SELECT id FROM schema_versions WHERE dataset_id = ? AND version = ?",
        (dataset["id"], version),
    ).fetchone()
    if version_row is None:
        raise NotFoundError(
            f"Schema version {version} of dataset '{dataset_name}' does not exist"
        )
    return dataset["id"], version_row["id"]


def _version_field_names(conn: sqlite3.Connection, version_id: int) -> set[str]:
    return {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM schema_fields WHERE version_id = ?", (version_id,)
        ).fetchall()
    }


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _validate_rule_parameters(
    kind: str, parameters: dict[str, Any], field_names: set[str]
) -> dict[str, Any]:
    """Validate parameters semantically (422) and against existing fields (404)."""
    if kind == "not_null":
        field = parameters.get("field")
        if not isinstance(field, str) or not field.strip():
            raise RequestInvalidError(
                "not_null rule requires a non-empty 'field' parameter"
            )
        if field not in field_names:
            raise NotFoundError(f"Field '{field}' does not exist in this schema version")
        return {"field": field}

    if kind == "numeric_range":
        field = parameters.get("field")
        if not isinstance(field, str) or not field.strip():
            raise RequestInvalidError(
                "numeric_range rule requires a non-empty 'field' parameter"
            )
        minimum = parameters.get("min")
        maximum = parameters.get("max")
        if not _is_finite_number(minimum) or not _is_finite_number(maximum):
            raise RequestInvalidError(
                "numeric_range rule requires finite numeric 'min' and 'max' parameters"
            )
        if minimum > maximum:
            raise RequestInvalidError(
                "numeric_range rule requires 'min' to be less than or equal to 'max'"
            )
        if field not in field_names:
            raise NotFoundError(f"Field '{field}' does not exist in this schema version")
        return {"field": field, "min": minimum, "max": maximum}

    # kind == "unique"
    fields = parameters.get("fields")
    if not isinstance(fields, list) or not fields:
        raise RequestInvalidError(
            "unique rule requires a non-empty 'fields' array parameter"
        )
    if not all(isinstance(field, str) and field.strip() for field in fields):
        raise RequestInvalidError(
            "unique rule 'fields' must be non-empty field name strings"
        )
    if len(set(fields)) != len(fields):
        raise RequestInvalidError(
            "unique rule 'fields' must not contain duplicate field names"
        )
    unknown = [field for field in fields if field not in field_names]
    if unknown:
        raise NotFoundError(
            f"Field '{unknown[0]}' does not exist in this schema version"
        )
    return {"fields": list(fields)}


def _rule_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "parameters": json.loads(row["parameters"]),
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
    }


def create_quality_rule(
    conn: sqlite3.Connection,
    dataset_name: str,
    version: int,
    name: str,
    kind: str,
    parameters: dict[str, Any],
) -> dict:
    dataset_id, version_id = _require_version(conn, dataset_name, version)

    clean_name = name.strip()
    if not clean_name:
        raise RequestInvalidError("Rule name must not be empty")
    if kind not in RULE_KINDS:
        raise RequestInvalidError(
            f"Unknown rule kind '{kind}'; expected one of "
            + ", ".join(RULE_KINDS)
        )

    field_names = _version_field_names(conn, version_id)
    clean_parameters = _validate_rule_parameters(kind, parameters, field_names)

    try:
        cursor = conn.execute(
            "INSERT INTO quality_rules "
            "(dataset_id, version_id, name, kind, parameters, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (
                dataset_id,
                version_id,
                clean_name,
                kind,
                json.dumps(clean_parameters),
                utc_now_iso(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(
            f"A quality rule named '{clean_name}' already exists in this version"
        ) from exc

    row = conn.execute(
        "SELECT id, name, kind, parameters, enabled, created_at "
        "FROM quality_rules WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _rule_row_to_dict(row)


def list_quality_rules(
    conn: sqlite3.Connection, dataset_name: str, version: int
) -> list[dict]:
    _, version_id = _require_version(conn, dataset_name, version)
    rows = conn.execute(
        "SELECT id, name, kind, parameters, enabled, created_at "
        "FROM quality_rules WHERE version_id = ? ORDER BY id",
        (version_id,),
    ).fetchall()
    return [_rule_row_to_dict(row) for row in rows]


def _get_rule_row(
    conn: sqlite3.Connection,
    dataset_name: str,
    version: int,
    rule_id: int,
) -> sqlite3.Row:
    _, version_id = _require_version(conn, dataset_name, version)
    row = conn.execute(
        "SELECT id, name, kind, parameters, enabled, created_at "
        "FROM quality_rules WHERE version_id = ? AND id = ?",
        (version_id, rule_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Quality rule {rule_id} does not exist in this version")
    return row


def get_quality_rule(
    conn: sqlite3.Connection, dataset_name: str, version: int, rule_id: int
) -> dict:
    return _rule_row_to_dict(_get_rule_row(conn, dataset_name, version, rule_id))


def set_quality_rule_enabled(
    conn: sqlite3.Connection, dataset_name: str, version: int, rule_id: int, enabled: bool
) -> dict:
    row = _get_rule_row(conn, dataset_name, version, rule_id)
    conn.execute(
        "UPDATE quality_rules SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, row["id"]),
    )
    return get_quality_rule(conn, dataset_name, version, rule_id)


def evaluate_quality_rules(
    conn: sqlite3.Connection, dataset_name: str, version: int, rows: list[dict[str, Any]]
) -> dict:
    _, version_id = _require_version(conn, dataset_name, version)
    rule_rows = conn.execute(
        "SELECT id, name, kind, parameters, enabled FROM quality_rules "
        "WHERE version_id = ? AND enabled = 1 ORDER BY id",
        (version_id,),
    ).fetchall()

    results = []
    for row in rule_rows:
        parameters = json.loads(row["parameters"])
        violations = evaluate_rule(row["kind"], parameters, rows)
        results.append(
            {
                "rule_id": row["id"],
                "name": row["name"],
                "passed": not violations,
                "violations": violations,
            }
        )

    return {
        "dataset": dataset_name,
        "version": version,
        "results": results,
    }
