"""Data-access and business rules for datasets, schema versions and lineage."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.errors import ConflictError, NotFoundError, RequestInvalidError
from app.models import FieldSpec


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


QUALITY_RULE_KINDS = ("not_null", "numeric_range", "unique")


def _require_schema_version(
    conn: sqlite3.Connection, dataset: dict, version_number: int
) -> sqlite3.Row:
    version_row = conn.execute(
        "SELECT id, version, created_at FROM schema_versions "
        "WHERE dataset_id = ? AND version = ?",
        (dataset["id"], version_number),
    ).fetchone()
    if version_row is None:
        raise NotFoundError(
            f"Schema version {version_number} of dataset '{dataset['name']}' does not exist"
        )
    return version_row


def _version_field_names(conn: sqlite3.Connection, version_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM schema_fields WHERE version_id = ?", (version_id,)
    ).fetchall()
    return {row["name"] for row in rows}


def _is_finite_number(value: Any) -> bool:
    # bool is a subclass of int; a boolean is not a numeric value here.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _validate_rule_params(
    kind: str, params: dict[str, Any], field_names: set[str]
) -> None:
    """Validate rule parameters structurally (422) and against the schema (404)."""

    def require_existing_field(field_name: Any) -> str:
        if not isinstance(field_name, str) or not field_name:
            raise RequestInvalidError(
                f"Rule kind '{kind}' requires a non-empty 'field' parameter"
            )
        if field_name not in field_names:
            raise NotFoundError(f"Field '{field_name}' does not exist in this version")
        return field_name

    if kind == "not_null":
        require_existing_field(params.get("field"))
        return

    if kind == "numeric_range":
        field_name = require_existing_field(params.get("field"))
        minimum = params.get("min")
        maximum = params.get("max")
        if not _is_finite_number(minimum) or not _is_finite_number(maximum):
            raise RequestInvalidError(
                f"Rule on field '{field_name}' requires finite numeric 'min' and 'max'"
            )
        if minimum > maximum:
            raise RequestInvalidError(
                f"Rule on field '{field_name}' requires min <= max"
            )
        return

    if kind == "unique":
        fields = params.get("fields")
        if not isinstance(fields, list) or not fields:
            raise RequestInvalidError(
                "Rule kind 'unique' requires a non-empty 'fields' list"
            )
        if not all(isinstance(name, str) and name for name in fields):
            raise RequestInvalidError(
                "Rule kind 'unique' requires 'fields' to contain non-empty field names"
            )
        if len(set(fields)) != len(fields):
            raise RequestInvalidError(
                "Rule kind 'unique' requires 'fields' to contain no duplicates"
            )
        unknown = [name for name in fields if name not in field_names]
        if unknown:
            raise NotFoundError(
                f"Field '{unknown[0]}' does not exist in this version"
            )
        return


def _quality_rule_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "params": json.loads(row["params"]),
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
    }


def create_quality_rule(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    name: str,
    kind: str,
    params: dict[str, Any],
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    clean_name = name.strip()
    if not clean_name:
        raise RequestInvalidError("Quality rule name must not be empty")

    field_names = _version_field_names(conn, version_row["id"])
    _validate_rule_params(kind, params, field_names)

    try:
        cursor = conn.execute(
            "INSERT INTO quality_rules (version_id, name, kind, params, enabled, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (
                version_row["id"],
                clean_name,
                kind,
                json.dumps(params),
                utc_now_iso(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(
            f"Quality rule '{clean_name}' already exists for version "
            f"{version_number} of dataset '{dataset_name}'"
        ) from exc

    row = conn.execute(
        "SELECT id, name, kind, params, enabled, created_at "
        "FROM quality_rules WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _quality_rule_row_to_dict(row)


def list_quality_rules(
    conn: sqlite3.Connection, dataset_name: str, version_number: int
) -> list[dict]:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    rows = conn.execute(
        "SELECT id, name, kind, params, enabled, created_at "
        "FROM quality_rules WHERE version_id = ? ORDER BY id",
        (version_row["id"],),
    ).fetchall()
    return [_quality_rule_row_to_dict(row) for row in rows]


def set_quality_rule_enabled(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    rule_id: int,
    enabled: bool,
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    row = conn.execute(
        "SELECT id, name, kind, params, enabled, created_at "
        "FROM quality_rules WHERE version_id = ? AND id = ?",
        (version_row["id"], rule_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Quality rule {rule_id} does not exist in this version")

    conn.execute(
        "UPDATE quality_rules SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, row["id"]),
    )
    updated = conn.execute(
        "SELECT id, name, kind, params, enabled, created_at "
        "FROM quality_rules WHERE id = ?",
        (row["id"],),
    ).fetchone()
    return _quality_rule_row_to_dict(updated)


def _row_violates_not_null(row: dict[str, Any], field: str) -> bool:
    return field not in row or row[field] is None


def _row_violates_numeric_range(
    row: dict[str, Any], field: str, minimum: float, maximum: float
) -> bool:
    if field not in row:
        return True
    value = row[field]
    if value is None or not _is_finite_number(value):
        return True
    return value < minimum or value > maximum


def _row_violates_unique(row: dict[str, Any], fields: list[str]) -> tuple:
    return tuple(None if name not in row else row[name] for name in fields)


def _json_combo_key(combo: tuple) -> str:
    # Values are already JSON-decoded scalars (or nested arrays/objects); use
    # JSON text so that, e.g., the number 1 and the string "1" never compare
    # equal and nested objects with reordered keys still match.
    return json.dumps(combo, sort_keys=True, separators=(",", ":"))


def evaluate_quality_rules(
    conn: sqlite3.Connection, dataset_name: str, version_number: int, rows: list[dict]
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    rule_rows = conn.execute(
        "SELECT id, name, kind, params, enabled "
        "FROM quality_rules WHERE version_id = ? AND enabled = 1 ORDER BY id",
        (version_row["id"],),
    ).fetchall()

    results: list[dict] = []
    for rule_row in rule_rows:
        params = json.loads(rule_row["params"])
        kind = rule_row["kind"]
        violations: list[int] = []

        if kind == "not_null":
            field = params["field"]
            violations = [
                index
                for index, row in enumerate(rows)
                if _row_violates_not_null(row, field)
            ]
        elif kind == "numeric_range":
            field = params["field"]
            violations = [
                index
                for index, row in enumerate(rows)
                if _row_violates_numeric_range(
                    row, field, params["min"], params["max"]
                )
            ]
        elif kind == "unique":
            fields = params["fields"]
            seen: dict[str, int] = {}
            duplicated: set[int] = set()
            for index, row in enumerate(rows):
                combo = _row_violates_unique(row, fields)
                key = _json_combo_key(combo)
                if key in seen:
                    duplicated.add(seen[key])
                    duplicated.add(index)
                else:
                    seen[key] = index
            violations = sorted(duplicated)

        results.append(
            {
                "rule_id": rule_row["id"],
                "name": rule_row["name"],
                "passed": not violations,
                "violations": violations,
            }
        )

    return {
        "dataset": dataset["name"],
        "version": version_row["version"],
        "results": results,
    }


# --------------------------------------------------------------------------- #
# Privacy policies
# --------------------------------------------------------------------------- #


PRIVACY_MASKINGS = ("redact", "partial")


def _privacy_policy_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "field": row["field"],
        "classification": row["classification"],
        "masking": row["masking"],
        "allowed_roles": json.loads(row["allowed_roles"]),
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
    }


def create_privacy_policy(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    field: str,
    classification: str,
    masking: str,
    allowed_roles: list[str],
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    field_names = _version_field_names(conn, version_row["id"])
    if field not in field_names:
        raise NotFoundError(f"Field '{field}' does not exist in this version")

    try:
        cursor = conn.execute(
            "INSERT INTO privacy_policies ("
            "version_id, field, classification, masking, allowed_roles, "
            "enabled, created_at"
            ") VALUES (?, ?, ?, ?, ?, 1, ?)",
            (
                version_row["id"],
                field,
                classification,
                masking,
                json.dumps(allowed_roles),
                utc_now_iso(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(
            f"A privacy policy for field '{field}' already exists for version "
            f"{version_number} of dataset '{dataset_name}'"
        ) from exc

    row = conn.execute(
        "SELECT id, field, classification, masking, allowed_roles, enabled, created_at "
        "FROM privacy_policies WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _privacy_policy_row_to_dict(row)


def list_privacy_policies(
    conn: sqlite3.Connection, dataset_name: str, version_number: int
) -> list[dict]:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    rows = conn.execute(
        "SELECT id, field, classification, masking, allowed_roles, enabled, created_at "
        "FROM privacy_policies WHERE version_id = ? ORDER BY id",
        (version_row["id"],),
    ).fetchall()
    return [_privacy_policy_row_to_dict(row) for row in rows]


def set_privacy_policy_enabled(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    policy_id: int,
    enabled: bool,
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    row = conn.execute(
        "SELECT id, field, classification, masking, allowed_roles, enabled, created_at "
        "FROM privacy_policies WHERE version_id = ? AND id = ?",
        (version_row["id"], policy_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(
            f"Privacy policy {policy_id} does not exist in this version"
        )

    conn.execute(
        "UPDATE privacy_policies SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, row["id"]),
    )
    updated = conn.execute(
        "SELECT id, field, classification, masking, allowed_roles, enabled, created_at "
        "FROM privacy_policies WHERE id = ?",
        (row["id"],),
    ).fetchone()
    return _privacy_policy_row_to_dict(updated)


def mask_privacy_value(value: Any, masking: str) -> Any:
    """Apply a masking strategy to one non-covered-by-role cell value."""
    if value is None:
        return None
    if masking == "partial" and isinstance(value, str) and len(value) > 4:
        return f"{value[0]}***{value[-2:]}"
    return "***"


def view_private_rows(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    role: str,
    rows: list[dict[str, Any]],
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    policy_rows = conn.execute(
        "SELECT field, masking, allowed_roles, enabled "
        "FROM privacy_policies WHERE version_id = ? ORDER BY id",
        (version_row["id"],),
    ).fetchall()
    policies = [
        {
            "field": row["field"],
            "masking": row["masking"],
            "allowed_roles": json.loads(row["allowed_roles"]),
            "enabled": bool(row["enabled"]),
        }
        for row in policy_rows
    ]

    masked_rows: list[dict[str, Any]] = []
    for row in rows:
        masked_row = dict(row)
        for policy in policies:
            if not policy["enabled"]:
                continue
            if role in policy["allowed_roles"]:
                continue
            field_name = policy["field"]
            if field_name in masked_row:
                masked_row[field_name] = mask_privacy_value(
                    masked_row[field_name], policy["masking"]
                )
        masked_rows.append(masked_row)

    return {
        "dataset": dataset["name"],
        "version": version_row["version"],
        "rows": masked_rows,
    }
