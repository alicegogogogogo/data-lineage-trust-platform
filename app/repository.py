"""Data-access and business rules for datasets, schema versions and lineage."""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
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


REDACTED = "***"


def _policy_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "field": row["field"],
        "classification": row["classification"],
        "masking": row["masking"],
        "allowed_roles": json.loads(row["allowed_roles"]),
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
    }


def _clean_allowed_roles(roles: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for role in roles:
        clean_role = role.strip()
        if not clean_role:
            raise RequestInvalidError(
                "'allowed_roles' must not contain empty role names"
            )
        if clean_role in seen:
            raise RequestInvalidError(
                f"Role '{clean_role}' is listed more than once in 'allowed_roles'"
            )
        seen.add(clean_role)
        cleaned.append(clean_role)
    return cleaned


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

    clean_field = field.strip()
    if not clean_field:
        raise RequestInvalidError("Field name must not be empty")

    clean_classification = classification.strip()
    if not clean_classification:
        raise RequestInvalidError("Classification must not be empty")

    roles = _clean_allowed_roles(allowed_roles)

    field_names = _version_field_names(conn, version_row["id"])
    if clean_field not in field_names:
        raise NotFoundError(
            f"Field '{clean_field}' does not exist in version "
            f"{version_number} of dataset '{dataset_name}'"
        )

    try:
        cursor = conn.execute(
            "INSERT INTO privacy_policies ("
            "version_id, field, classification, masking, allowed_roles, "
            "enabled, created_at"
            ") VALUES (?, ?, ?, ?, ?, 1, ?)",
            (
                version_row["id"],
                clean_field,
                clean_classification,
                masking,
                json.dumps(roles),
                utc_now_iso(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(
            f"A privacy policy for field '{clean_field}' already exists for "
            f"version {version_number} of dataset '{dataset_name}'"
        ) from exc

    row = conn.execute(
        "SELECT id, field, classification, masking, allowed_roles, enabled, created_at "
        "FROM privacy_policies WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _policy_row_to_dict(row)


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
    return [_policy_row_to_dict(row) for row in rows]


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
        raise NotFoundError(f"Privacy policy {policy_id} does not exist in this version")

    conn.execute(
        "UPDATE privacy_policies SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, row["id"]),
    )
    updated = conn.execute(
        "SELECT id, field, classification, masking, allowed_roles, enabled, created_at "
        "FROM privacy_policies WHERE id = ?",
        (row["id"],),
    ).fetchone()
    return _policy_row_to_dict(updated)


def _mask_value(value: Any, masking: str) -> Any:
    if value is None:
        return None
    if masking == "partial" and isinstance(value, str) and len(value) > 4:
        return value[0] + value[-2:]
    return REDACTED


def view_privacy_rows(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    role: str,
    rows: list[dict[str, Any]],
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    clean_role = role.strip()
    if not clean_role:
        raise RequestInvalidError("Role must not be empty")

    policy_rows = conn.execute(
        "SELECT field, masking, allowed_roles FROM privacy_policies "
        "WHERE version_id = ? AND enabled = 1",
        (version_row["id"],),
    ).fetchall()

    policies: dict[str, tuple[str, set[str]]] = {
        row["field"]: (row["masking"], set(json.loads(row["allowed_roles"])))
        for row in policy_rows
    }

    masked_rows: list[dict[str, Any]] = []
    for row in rows:
        masked_row = dict(row)
        for field, (masking, allowed_roles) in policies.items():
            if field in masked_row and clean_role not in allowed_roles:
                masked_row[field] = _mask_value(masked_row[field], masking)
        masked_rows.append(masked_row)

    return {
        "dataset": dataset["name"],
        "version": version_row["version"],
        "rows": masked_rows,
    }


# --------------------------------------------------------------------------- #
# Row snapshots
# --------------------------------------------------------------------------- #


def _snapshot_metadata(row: sqlite3.Row, dataset_name: str, version_number: int) -> dict:
    return {
        "id": row["id"],
        "dataset": dataset_name,
        "version": version_number,
        "created_at": row["created_at"],
        "row_count": row["row_count"],
    }


def create_snapshot(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    rows: list[dict[str, Any]],
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    created_at = utc_now_iso()
    # Re-serializing stores an independent deep copy of the JSON values; list
    # order and the received object key order are both preserved.
    stored_rows = json.dumps(rows)
    cursor = conn.execute(
        "INSERT INTO snapshots (version_id, row_count, rows, created_at) "
        "VALUES (?, ?, ?, ?)",
        (version_row["id"], len(rows), stored_rows, created_at),
    )
    row = conn.execute(
        "SELECT id, row_count, created_at FROM snapshots WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _snapshot_metadata(row, dataset["name"], version_row["version"])


def list_snapshots(
    conn: sqlite3.Connection, dataset_name: str, version_number: int
) -> list[dict]:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    rows = conn.execute(
        "SELECT id, row_count, created_at FROM snapshots "
        "WHERE version_id = ? ORDER BY id",
        (version_row["id"],),
    ).fetchall()
    return [_snapshot_metadata(row, dataset["name"], version_row["version"]) for row in rows]


def _get_scoped_snapshot_row(
    conn: sqlite3.Connection, version_id: int, snapshot_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, version_id, row_count, rows, created_at FROM snapshots "
        "WHERE version_id = ? AND id = ?",
        (version_id, snapshot_id),
    ).fetchone()


def get_snapshot(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    snapshot_id: int,
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    row = _get_scoped_snapshot_row(conn, version_row["id"], snapshot_id)
    if row is None:
        raise NotFoundError(
            f"Snapshot {snapshot_id} does not exist in version "
            f"{version_number} of dataset '{dataset_name}'"
        )
    result = _snapshot_metadata(row, dataset["name"], version_row["version"])
    result["rows"] = json.loads(row["rows"])
    return result


def _parse_at_timestamp(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise RequestInvalidError(
            "Query parameter 'timestamp' must be an ISO-8601 date-time"
        ) from exc
    # A trailing timezone designator (offset or 'Z') is mandatory.
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RequestInvalidError(
            "Query parameter 'timestamp' must include a timezone"
        )
    return parsed


def get_snapshot_at(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    raw_timestamp: str | None,
) -> dict:
    if raw_timestamp is None or not raw_timestamp:
        raise RequestInvalidError(
            "Query parameter 'timestamp' is required and must be an ISO-8601 date-time"
        )
    target = _parse_at_timestamp(raw_timestamp)

    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    # Select the newest snapshot chronologically (ties broken by the highest
    # id); comparing parsed instants also handles any offset in the target.
    candidates = conn.execute(
        "SELECT id, created_at FROM snapshots WHERE version_id = ?",
        (version_row["id"],),
    ).fetchall()
    eligible = [
        (datetime.fromisoformat(row["created_at"]), row["id"])
        for row in candidates
        if datetime.fromisoformat(row["created_at"]) <= target
    ]
    if not eligible:
        raise NotFoundError(
            f"No snapshot of version {version_number} of dataset "
            f"'{dataset_name}' exists at or before the requested timestamp"
        )
    match_id = max(eligible, key=lambda item: (item[0], item[1]))[1]

    row = conn.execute(
        "SELECT id, row_count, rows, created_at FROM snapshots WHERE id = ?",
        (match_id,),
    ).fetchone()
    result = _snapshot_metadata(row, dataset["name"], version_row["version"])
    result["rows"] = json.loads(row["rows"])
    return result


def _find_snapshot_globally(
    conn: sqlite3.Connection, snapshot_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT  s.id, s.version_id, s.row_count, s.rows, s.created_at,
                sv.version AS version_number,
                d.name AS dataset_name
        FROM    snapshots s
        JOIN    schema_versions sv ON sv.id = s.version_id
        JOIN    datasets d ON d.id = sv.dataset_id
        WHERE   s.id = ?
        """,
        (snapshot_id,),
    ).fetchone()


def _canonical_row_text(row: Any) -> str:
    # Object key order is ignored (sort_keys) while array order and value types
    # are preserved (e.g. 1 vs "1" vs 1.0 vs true all differ).
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _snapshot_multiset(
    rows: list[dict[str, Any]],
) -> tuple[Counter, dict[str, dict[str, Any]]]:
    counts: Counter = Counter()
    examples: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _canonical_row_text(row)
        counts[key] += 1
        examples.setdefault(key, row)
    return counts, examples


def diff_snapshots(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    from_snapshot_id: int,
    to_snapshot_id: int,
) -> dict:
    # Validate the path target first so unknown dataset/version stay 404.
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    from_row = _find_snapshot_globally(conn, from_snapshot_id)
    if from_row is None:
        raise NotFoundError(f"Snapshot {from_snapshot_id} does not exist")
    to_row = _find_snapshot_globally(conn, to_snapshot_id)
    if to_row is None:
        raise NotFoundError(f"Snapshot {to_snapshot_id} does not exist")

    for row, snapshot_id in ((from_row, from_snapshot_id), (to_row, to_snapshot_id)):
        if row["version_id"] != version_row["id"]:
            raise RequestInvalidError(
                f"Snapshot {snapshot_id} does not belong to version "
                f"{version_number} of dataset '{dataset_name}'; snapshots can "
                "only be compared within the same dataset and version"
            )

    from_rows = json.loads(from_row["rows"])
    to_rows = json.loads(to_row["rows"])
    from_counts, from_examples = _snapshot_multiset(from_rows)
    to_counts, to_examples = _snapshot_multiset(to_rows)

    added_keys = sorted(key for key in to_counts if to_counts[key] > from_counts[key])
    removed_keys = sorted(key for key in from_counts if from_counts[key] > to_counts[key])

    return {
        "from_snapshot_id": from_snapshot_id,
        "to_snapshot_id": to_snapshot_id,
        "added": [
            {"row": to_examples[key], "count": to_counts[key] - from_counts[key]}
            for key in added_keys
        ],
        "removed": [
            {"row": from_examples[key], "count": from_counts[key] - to_counts[key]}
            for key in removed_keys
        ],
    }


# --------------------------------------------------------------------------- #
# Processing tasks
# --------------------------------------------------------------------------- #


def _task_row_to_dict(
    row: sqlite3.Row, dataset_name: str, version_number: int
) -> dict:
    return {
        "id": row["id"],
        "dataset": dataset_name,
        "version": version_number,
        "name": row["name"],
        "depends_on": json.loads(row["depends_on"]),
        "max_attempts": row["max_attempts"],
        "status": row["status"],
        "attempt_count": row["attempt_count"],
        "created_at": row["created_at"],
    }


def _run_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "attempt": row["attempt"],
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
    }


def _require_processing_task(
    conn: sqlite3.Connection,
    version_id: int,
    task_id: int,
    dataset_name: str,
    version_number: int,
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, version_id, name, depends_on, max_attempts, status, "
        "attempt_count, created_at FROM processing_tasks "
        "WHERE version_id = ? AND id = ?",
        (version_id, task_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(
            f"Processing task {task_id} does not exist in version "
            f"{version_number} of dataset '{dataset_name}'"
        )
    return row


def create_processing_task(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    name: str,
    depends_on: list[int],
    max_attempts: int,
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    clean_name = name.strip()
    if not clean_name:
        raise RequestInvalidError("Processing task name must not be empty")

    if isinstance(max_attempts, bool) or max_attempts < 1:
        raise RequestInvalidError("'max_attempts' must be a positive integer")

    if len(set(depends_on)) != len(depends_on):
        raise RequestInvalidError("'depends_on' must not contain duplicate task ids")
    for dependency_id in depends_on:
        dependency = conn.execute(
            "SELECT id FROM processing_tasks WHERE version_id = ? AND id = ?",
            (version_row["id"], dependency_id),
        ).fetchone()
        if dependency is None:
            raise RequestInvalidError(
                f"Dependency task {dependency_id} does not exist in version "
                f"{version_number} of dataset '{dataset_name}'"
            )

    try:
        cursor = conn.execute(
            "INSERT INTO processing_tasks ("
            "version_id, name, depends_on, max_attempts, status, "
            "attempt_count, created_at"
            ") VALUES (?, ?, ?, ?, 'pending', 0, ?)",
            (
                version_row["id"],
                clean_name,
                json.dumps(depends_on),
                max_attempts,
                utc_now_iso(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(
            f"Processing task '{clean_name}' already exists for version "
            f"{version_number} of dataset '{dataset_name}'"
        ) from exc

    row = conn.execute(
        "SELECT id, name, depends_on, max_attempts, status, attempt_count, "
        "created_at FROM processing_tasks WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _task_row_to_dict(row, dataset["name"], version_row["version"])


def list_processing_tasks(
    conn: sqlite3.Connection, dataset_name: str, version_number: int
) -> list[dict]:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    rows = conn.execute(
        "SELECT id, name, depends_on, max_attempts, status, attempt_count, "
        "created_at FROM processing_tasks WHERE version_id = ? ORDER BY id",
        (version_row["id"],),
    ).fetchall()
    return [
        _task_row_to_dict(row, dataset["name"], version_row["version"])
        for row in rows
    ]


def get_processing_task(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    task_id: int,
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)
    task_row = _require_processing_task(
        conn, version_row["id"], task_id, dataset_name, version_number
    )

    run_rows = conn.execute(
        "SELECT id, task_id, attempt, status, started_at, finished_at, error "
        "FROM processing_task_runs WHERE task_id = ? ORDER BY attempt",
        (task_row["id"],),
    ).fetchall()
    result = _task_row_to_dict(task_row, dataset["name"], version_row["version"])
    result["runs"] = [_run_row_to_dict(row) for row in run_rows]
    return result


def create_processing_task_run(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    task_id: int,
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)
    task_row = _require_processing_task(
        conn, version_row["id"], task_id, dataset_name, version_number
    )

    if task_row["status"] not in ("pending", "failed"):
        raise ConflictError(
            f"Processing task {task_id} is '{task_row['status']}' and cannot "
            "start a new run"
        )
    if task_row["attempt_count"] >= task_row["max_attempts"]:
        raise ConflictError(
            f"Processing task {task_id} has exhausted its "
            f"{task_row['max_attempts']} attempt(s)"
        )

    for dependency_id in json.loads(task_row["depends_on"]):
        dependency = conn.execute(
            "SELECT status FROM processing_tasks WHERE id = ?",
            (dependency_id,),
        ).fetchone()
        if dependency is None or dependency["status"] != "succeeded":
            raise ConflictError(
                f"Processing task {task_id} cannot run because dependency task "
                f"{dependency_id} has not succeeded"
            )

    attempt = task_row["attempt_count"] + 1
    started_at = utc_now_iso()
    cursor = conn.execute(
        "INSERT INTO processing_task_runs ("
        "task_id, attempt, status, started_at, finished_at, error"
        ") VALUES (?, ?, 'running', ?, NULL, NULL)",
        (task_row["id"], attempt, started_at),
    )
    conn.execute(
        "UPDATE processing_tasks SET attempt_count = ?, status = 'running' "
        "WHERE id = ?",
        (attempt, task_row["id"]),
    )
    row = conn.execute(
        "SELECT id, task_id, attempt, status, started_at, finished_at, error "
        "FROM processing_task_runs WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _run_row_to_dict(row)


def finish_processing_task_run(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    task_id: int,
    run_id: int,
    status: str,
    error: str | None,
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)
    task_row = _require_processing_task(
        conn, version_row["id"], task_id, dataset_name, version_number
    )

    run_row = conn.execute(
        "SELECT id, task_id, attempt, status, started_at, finished_at, error "
        "FROM processing_task_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if run_row is None:
        raise NotFoundError(f"Processing task run {run_id} does not exist")
    if run_row["task_id"] != task_row["id"]:
        raise RequestInvalidError(
            f"Processing task run {run_id} does not belong to task {task_id} "
            f"of version {version_number} of dataset '{dataset_name}'"
        )

    if status == "failed":
        if error is None or not error.strip():
            raise RequestInvalidError(
                "Finishing a run as 'failed' requires a non-empty 'error'"
            )
    elif error is not None:
        raise RequestInvalidError(
            "Finishing a run as 'succeeded' must not include an 'error'"
        )

    if run_row["status"] != "running":
        raise ConflictError(
            f"Processing task run {run_id} is '{run_row['status']}' and "
            "cannot be finished"
        )

    conn.execute(
        "UPDATE processing_task_runs SET status = ?, finished_at = ?, error = ? "
        "WHERE id = ?",
        (status, utc_now_iso(), error if status == "failed" else None, run_id),
    )
    conn.execute(
        "UPDATE processing_tasks SET status = ? WHERE id = ?",
        (status, task_row["id"]),
    )
    updated = conn.execute(
        "SELECT id, task_id, attempt, status, started_at, finished_at, error "
        "FROM processing_task_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    return _run_row_to_dict(updated)
