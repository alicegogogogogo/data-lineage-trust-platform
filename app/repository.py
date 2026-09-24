"""Data-access and business rules for datasets, schema versions and lineage."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
    # A new version of this dataset may become part of future impact answers;
    # drop every cached entry related to the dataset.
    invalidate_impact_cache_for_dataset(conn, dataset_name)
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

    # The new edge can only extend the impact of fields that reach its source.
    _invalidate_impact_cache_for_upstream(
        conn, source["name"], source_version, source_field, source_field_id
    )

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
# Lineage impact queries (with persistent cache)
# --------------------------------------------------------------------------- #


def _lineage_forward_edges(
    conn: sqlite3.Connection,
) -> dict[int, list[tuple[int, dict]]]:
    """Adjacency of the lineage graph: source field id -> target field refs."""
    rows = conn.execute(
        """
        SELECT  ll.source_field_id AS source_field_id,
                ll.target_field_id AS target_field_id,
                td.name            AS target_dataset,
                tv.version         AS target_version,
                tf.name            AS target_field
        FROM    lineage_links ll
        JOIN    datasets td ON td.id = ll.target_dataset_id
        JOIN    schema_versions tv ON tv.id = ll.target_version_id
        JOIN    schema_fields tf ON tf.id = ll.target_field_id
        """
    ).fetchall()
    edges: dict[int, list[tuple[int, dict]]] = {}
    for row in rows:
        ref = {
            "dataset": row["target_dataset"],
            "version": row["target_version"],
            "field": row["target_field"],
        }
        edges.setdefault(row["source_field_id"], []).append(
            (row["target_field_id"], ref)
        )
    return edges


def _compute_impacted(conn: sqlite3.Connection, source_field_id: int) -> list[dict]:
    """All fields reachable downstream of the source, deduplicated and sorted.

    The source field id is seeded into the visited set, so cycles terminate
    and the source itself never appears in the result.
    """
    edges = _lineage_forward_edges(conn)
    visited = {source_field_id}
    impacted: dict[tuple[str, int, str], dict] = {}
    queue = [source_field_id]
    while queue:
        current = queue.pop()
        for target_field_id, ref in edges.get(current, ()):
            if target_field_id in visited:
                continue
            visited.add(target_field_id)
            impacted[(ref["dataset"], ref["version"], ref["field"])] = ref
            queue.append(target_field_id)
    return [impacted[key] for key in sorted(impacted)]


def _impact_cache_lookup(
    conn: sqlite3.Connection, dataset: str, version: int, field: str
) -> list[dict] | None:
    row = conn.execute(
        "SELECT impacted FROM lineage_impact_cache "
        "WHERE source_dataset = ? AND source_version = ? AND source_field = ?",
        (dataset, version, field),
    ).fetchone()
    return None if row is None else json.loads(row["impacted"])


def _impact_cache_store(
    conn: sqlite3.Connection,
    dataset: str,
    version: int,
    field: str,
    impacted: list[dict],
) -> None:
    conn.execute(
        "DELETE FROM lineage_impact_cache "
        "WHERE source_dataset = ? AND source_version = ? AND source_field = ?",
        (dataset, version, field),
    )
    cursor = conn.execute(
        "INSERT INTO lineage_impact_cache ("
        "source_dataset, source_version, source_field, impacted, created_at"
        ") VALUES (?, ?, ?, ?, ?)",
        (dataset, version, field, json.dumps(impacted), utc_now_iso()),
    )
    cache_id = cursor.lastrowid
    mentioned = {dataset} | {item["dataset"] for item in impacted}
    conn.executemany(
        "INSERT INTO lineage_impact_cache_datasets (cache_id, dataset) "
        "VALUES (?, ?)",
        [(cache_id, name) for name in sorted(mentioned)],
    )


def invalidate_impact_cache_for_dataset(
    conn: sqlite3.Connection, dataset_name: str
) -> None:
    """Drop every cache entry that mentions the dataset in any role."""
    conn.execute(
        "DELETE FROM lineage_impact_cache WHERE id IN ("
        "SELECT cache_id FROM lineage_impact_cache_datasets WHERE dataset = ?"
        ")",
        (dataset_name,),
    )


def _invalidate_impact_cache_for_upstream(
    conn: sqlite3.Connection,
    source_dataset: str,
    source_version: int,
    source_field: str,
    source_field_id: int,
) -> None:
    """Invalidate cached impacts of the new link's source and its upstream.

    A new mapping ``source -> target`` only changes the impact result of
    fields that can reach ``source`` (``source`` itself included), so exactly
    those cached entries are dropped; unrelated entries are kept. The reverse
    walk runs after the link was inserted so cycles are covered too.
    """
    rows = conn.execute(
        """
        SELECT  ll.target_field_id AS target_field_id,
                ll.source_field_id AS source_field_id,
                sd.name            AS source_dataset,
                sv.version         AS source_version,
                sf.name            AS source_field
        FROM    lineage_links ll
        JOIN    datasets sd ON sd.id = ll.source_dataset_id
        JOIN    schema_versions sv ON sv.id = ll.source_version_id
        JOIN    schema_fields sf ON sf.id = ll.source_field_id
        """
    ).fetchall()
    reverse: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        reverse.setdefault(row["target_field_id"], []).append(row)

    visited = {source_field_id}
    upstream = {(source_dataset, source_version, source_field)}
    queue = [source_field_id]
    while queue:
        current = queue.pop()
        for row in reverse.get(current, ()):
            if row["source_field_id"] in visited:
                continue
            visited.add(row["source_field_id"])
            upstream.add(
                (row["source_dataset"], row["source_version"], row["source_field"])
            )
            queue.append(row["source_field_id"])

    conn.executemany(
        "DELETE FROM lineage_impact_cache "
        "WHERE source_dataset = ? AND source_version = ? AND source_field = ?",
        sorted(upstream),
    )


def get_lineage_impact(
    conn: sqlite3.Connection, dataset_name: str, version: int, field: str
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    _, field_id = _require_field(conn, dataset, version, field, role="Source")

    impacted = _impact_cache_lookup(conn, dataset["name"], version, field)
    if impacted is None:
        impacted = _compute_impacted(conn, field_id)
        _impact_cache_store(conn, dataset["name"], version, field, impacted)

    return {
        "source": {"dataset": dataset["name"], "version": version, "field": field},
        "impacted": impacted,
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
# Retention policies and lineage-aware snapshot deletion
# --------------------------------------------------------------------------- #


def _retention_policy_row_to_dict(
    row: sqlite3.Row, dataset_name: str, version_number: int
) -> dict:
    return {
        "id": row["id"],
        "dataset": dataset_name,
        "version": version_number,
        "retention_days": row["retention_days"],
        "created_at": row["created_at"],
    }


def create_retention_policy(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    retention_days: int,
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    try:
        cursor = conn.execute(
            "INSERT INTO retention_policies (version_id, retention_days, created_at) "
            "VALUES (?, ?, ?)",
            (version_row["id"], retention_days, utc_now_iso()),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(
            f"A retention policy already exists for version "
            f"{version_number} of dataset '{dataset_name}'"
        ) from exc

    row = conn.execute(
        "SELECT id, retention_days, created_at FROM retention_policies WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _retention_policy_row_to_dict(row, dataset["name"], version_row["version"])


def _compute_version_field_impacted(
    conn: sqlite3.Connection, version_id: int
) -> list[dict]:
    """Downstream union of every field of one schema version.

    All of the version's fields seed the same reachability walk, so direct and
    indirect downstream fields are found once, cycles terminate and the version's
    own fields never appear in the result.
    """
    field_rows = conn.execute(
        "SELECT id FROM schema_fields WHERE version_id = ?", (version_id,)
    ).fetchall()
    seeds = [row["id"] for row in field_rows]

    edges = _lineage_forward_edges(conn)
    visited = set(seeds)
    impacted: dict[tuple[str, int, str], dict] = {}
    queue = list(seeds)
    while queue:
        current = queue.pop()
        for target_field_id, ref in edges.get(current, ()):
            if target_field_id in visited:
                continue
            visited.add(target_field_id)
            impacted[(ref["dataset"], ref["version"], ref["field"])] = ref
            queue.append(target_field_id)
    return [impacted[key] for key in sorted(impacted)]


def _deletion_request_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "snapshot_id": row["snapshot_id"],
        "policy_id": row["policy_id"],
        "reason": row["reason"],
        "status": row["status"],
        "impacted": json.loads(row["impacted"]),
        "created_at": row["created_at"],
    }


def create_snapshot_deletion_request(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    snapshot_id: int,
    reason: str,
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    clean_reason = reason.strip()
    if not clean_reason:
        raise RequestInvalidError("'reason' must not be empty")

    snapshot_row = _get_scoped_snapshot_row(conn, version_row["id"], snapshot_id)
    if snapshot_row is None:
        raise NotFoundError(
            f"Snapshot {snapshot_id} does not exist in version "
            f"{version_number} of dataset '{dataset_name}'"
        )

    policy_row = conn.execute(
        "SELECT id FROM retention_policies WHERE version_id = ?",
        (version_row["id"],),
    ).fetchone()
    if policy_row is None:
        raise NotFoundError(
            f"No retention policy exists for version {version_number} of "
            f"dataset '{dataset_name}'"
        )

    open_request = conn.execute(
        "SELECT id FROM snapshot_deletion_requests "
        "WHERE snapshot_id = ? AND status IN ('pending', 'blocked')",
        (snapshot_id,),
    ).fetchone()
    if open_request is not None:
        raise ConflictError(
            f"Snapshot {snapshot_id} already has an open deletion request "
            f"({open_request['id']})"
        )

    # An active retention exception (version- or snapshot-scoped) forbids
    # opening a deletion request; the check shares this transaction with the
    # insert below, so the two commit atomically.
    _require_no_active_retention_exception(
        conn, version_row, snapshot_id, dataset_name, version_number
    )

    impacted = _compute_version_field_impacted(conn, version_row["id"])
    status = "blocked" if impacted else "pending"
    try:
        cursor = conn.execute(
            "INSERT INTO snapshot_deletion_requests ("
            "version_id, snapshot_id, policy_id, reason, status, impacted, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                version_row["id"],
                snapshot_id,
                policy_row["id"],
                clean_reason,
                status,
                json.dumps(impacted),
                utc_now_iso(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        # Lost a race against a concurrent open-request insert.
        raise ConflictError(
            f"Snapshot {snapshot_id} already has an open deletion request"
        ) from exc
    row = conn.execute(
        "SELECT * FROM snapshot_deletion_requests WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _deletion_request_row_to_dict(row)


def list_snapshot_deletion_requests(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    snapshot_id: int,
) -> list[dict]:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    # The collection stays addressable after its snapshot has been confirmed
    # away: the snapshot itself may be gone, but its request records remain.
    # A snapshot id that never existed in this version and has no requests is an
    # unknown resource.
    snapshot_row = _get_scoped_snapshot_row(conn, version_row["id"], snapshot_id)
    request_rows = conn.execute(
        "SELECT id FROM snapshot_deletion_requests "
        "WHERE version_id = ? AND snapshot_id = ?",
        (version_row["id"], snapshot_id),
    ).fetchall()
    if snapshot_row is None and not request_rows:
        raise NotFoundError(
            f"Snapshot {snapshot_id} does not exist in version "
            f"{version_number} of dataset '{dataset_name}'"
        )

    rows = conn.execute(
        "SELECT * FROM snapshot_deletion_requests "
        "WHERE version_id = ? AND snapshot_id = ? ORDER BY id",
        (version_row["id"], snapshot_id),
    ).fetchall()
    return [_deletion_request_row_to_dict(row) for row in rows]


def confirm_snapshot_deletion_request(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    snapshot_id: int,
    request_id: int,
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    request_row = conn.execute(
        "SELECT * FROM snapshot_deletion_requests "
        "WHERE id = ? AND version_id = ? AND snapshot_id = ?",
        (request_id, version_row["id"], snapshot_id),
    ).fetchone()
    if request_row is None:
        raise NotFoundError(
            f"Snapshot deletion request {request_id} does not exist for "
            f"snapshot {snapshot_id} in version {version_number} of dataset "
            f"'{dataset_name}'"
        )

    if request_row["status"] != "pending":
        raise ConflictError(
            f"Snapshot deletion request {request_id} is "
            f"'{request_row['status']}'; only pending requests can be confirmed"
        )

    snapshot_row = _get_scoped_snapshot_row(conn, version_row["id"], snapshot_id)
    if snapshot_row is None:
        raise NotFoundError(
            f"Snapshot {snapshot_id} does not exist in version "
            f"{version_number} of dataset '{dataset_name}'"
        )

    policy_row = conn.execute(
        "SELECT retention_days FROM retention_policies WHERE id = ?",
        (request_row["policy_id"],),
    ).fetchone()
    retention_days = policy_row["retention_days"] if policy_row is not None else None

    created_at = datetime.fromisoformat(snapshot_row["created_at"])
    age = datetime.now(timezone.utc) - created_at
    if retention_days is None or age < timedelta(days=retention_days):
        raise ConflictError(
            f"Snapshot {snapshot_id} has not reached the retention age of "
            f"{retention_days} day(s) yet"
        )

    # An active retention exception (version- or snapshot-scoped) forbids the
    # deletion; the check shares this transaction with the delete below, so the
    # two commit atomically.
    _require_no_active_retention_exception(
        conn, version_row, snapshot_id, dataset_name, version_number
    )

    # Snapshot removal and request confirmation commit together: either both
    # take effect or neither does.
    conn.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))
    confirmed_at = utc_now_iso()
    conn.execute(
        "UPDATE snapshot_deletion_requests "
        "SET status = 'confirmed', confirmed_at = ? WHERE id = ?",
        (confirmed_at, request_id),
    )
    updated = conn.execute(
        "SELECT * FROM snapshot_deletion_requests WHERE id = ?", (request_id,)
    ).fetchone()
    result = _deletion_request_row_to_dict(updated)
    result["confirmed_at"] = updated["confirmed_at"]
    return result


# --------------------------------------------------------------------------- #
# Retention exceptions (compliance holds blocking snapshot deletion)
# --------------------------------------------------------------------------- #


def _parse_expires_at(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise RequestInvalidError(
            "'expires_at' must be an ISO-8601 date-time"
        ) from exc
    # A trailing timezone designator (offset or 'Z') is mandatory.
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RequestInvalidError("'expires_at' must include a timezone")
    if parsed <= datetime.now(timezone.utc):
        raise RequestInvalidError("'expires_at' must be in the future")
    return parsed


def _retention_exception_status(row: sqlite3.Row, now: datetime) -> str:
    if row["status"] == "released":
        return "released"
    if datetime.fromisoformat(row["expires_at"]) <= now:
        return "expired"
    return "active"


def _retention_exception_row_to_dict(
    row: sqlite3.Row, dataset_name: str, version_number: int, now: datetime
) -> dict:
    return {
        "id": row["id"],
        "dataset": dataset_name,
        "version": version_number,
        "scope": row["scope"],
        "snapshot_id": row["snapshot_id"],
        "reason": row["reason"],
        "expires_at": row["expires_at"],
        "status": _retention_exception_status(row, now),
        "created_at": row["created_at"],
        "released_at": row["released_at"],
    }


def create_retention_exception(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    scope: str,
    snapshot_id: int | None,
    reason: str,
    expires_at: str,
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    if scope == "version":
        if snapshot_id is not None:
            raise RequestInvalidError(
                "'snapshot_id' must be null when 'scope' is 'version'"
            )
    else:
        if snapshot_id is None:
            raise RequestInvalidError(
                "'snapshot_id' is required when 'scope' is 'snapshot'"
            )

    clean_reason = reason.strip()
    if not clean_reason:
        raise RequestInvalidError("'reason' must not be empty")

    _parse_expires_at(expires_at)

    if scope == "snapshot":
        snapshot_row = _get_scoped_snapshot_row(conn, version_row["id"], snapshot_id)
        if snapshot_row is None:
            raise NotFoundError(
                f"Snapshot {snapshot_id} does not exist in version "
                f"{version_number} of dataset '{dataset_name}'"
            )

    cursor = conn.execute(
        "INSERT INTO retention_exceptions ("
        "version_id, scope, snapshot_id, reason, expires_at, status, "
        "created_at, released_at"
        ") VALUES (?, ?, ?, ?, ?, 'active', ?, NULL)",
        (
            version_row["id"],
            scope,
            snapshot_id,
            clean_reason,
            expires_at,
            utc_now_iso(),
        ),
    )
    row = conn.execute(
        "SELECT * FROM retention_exceptions WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _retention_exception_row_to_dict(
        row, dataset["name"], version_row["version"], datetime.now(timezone.utc)
    )


def list_retention_exceptions(
    conn: sqlite3.Connection, dataset_name: str, version_number: int
) -> list[dict]:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    rows = conn.execute(
        "SELECT * FROM retention_exceptions WHERE version_id = ? ORDER BY id",
        (version_row["id"],),
    ).fetchall()
    now = datetime.now(timezone.utc)
    return [
        _retention_exception_row_to_dict(
            row, dataset["name"], version_row["version"], now
        )
        for row in rows
    ]


def release_retention_exception(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    exception_id: int,
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    row = conn.execute(
        "SELECT * FROM retention_exceptions WHERE version_id = ? AND id = ?",
        (version_row["id"], exception_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(
            f"Retention exception {exception_id} does not exist in version "
            f"{version_number} of dataset '{dataset_name}'"
        )

    now = datetime.now(timezone.utc)
    status = _retention_exception_status(row, now)
    if status != "active":
        raise ConflictError(
            f"Retention exception {exception_id} is '{status}'; only active "
            "exceptions can be released"
        )

    released_at = utc_now_iso()
    conn.execute(
        "UPDATE retention_exceptions SET status = 'released', released_at = ? "
        "WHERE id = ?",
        (released_at, row["id"]),
    )
    updated = conn.execute(
        "SELECT * FROM retention_exceptions WHERE id = ?", (row["id"],)
    ).fetchone()
    return _retention_exception_row_to_dict(
        updated, dataset["name"], version_row["version"], now
    )


def _require_no_active_retention_exception(
    conn: sqlite3.Connection,
    version_row: sqlite3.Row,
    snapshot_id: int,
    dataset_name: str,
    version_number: int,
) -> None:
    """Reject with 409 when an active exception protects the snapshot.

    Both version-scoped exceptions of this version and snapshot-scoped
    exceptions naming this snapshot block; expired and released exceptions
    never do. The caller runs this inside the same transaction as the write it
    protects, so the check and the write commit atomically.
    """
    rows = conn.execute(
        "SELECT id, scope, expires_at FROM retention_exceptions "
        "WHERE version_id = ? AND status = 'active' "
        "AND (scope = 'version' OR snapshot_id = ?)",
        (version_row["id"], snapshot_id),
    ).fetchall()
    now = datetime.now(timezone.utc)
    blocking = [
        row["id"]
        for row in rows
        if datetime.fromisoformat(row["expires_at"]) > now
    ]
    if blocking:
        raise ConflictError(
            f"Snapshot {snapshot_id} of version {version_number} of dataset "
            f"'{dataset_name}' is protected by active retention exception(s) "
            + ", ".join(str(exception_id) for exception_id in sorted(blocking))
        )


# --------------------------------------------------------------------------- #
# Processing tasks
# --------------------------------------------------------------------------- #


# Serializes the read-eligible-tasks -> start-runs sequence within this
# process, so concurrent dispatch requests and single-task starts interleave as
# whole transactions and never start the same task twice. The conditional
# UPDATE in _start_task_run and the UNIQUE(task_id, attempt) constraint remain
# the hard guards against writers in other processes.
_processing_start_lock = threading.Lock()


@contextmanager
def _processing_write_section():
    _processing_start_lock.acquire()
    try:
        yield
    finally:
        _processing_start_lock.release()


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


def _require_task(
    conn: sqlite3.Connection,
    version_row: sqlite3.Row,
    dataset_name: str,
    version_number: int,
    task_id: int,
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM processing_tasks WHERE version_id = ? AND id = ?",
        (version_row["id"], task_id),
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

    if len(set(depends_on)) != len(depends_on):
        raise RequestInvalidError("'depends_on' must not contain duplicate task ids")
    for dependency_id in depends_on:
        dependency = conn.execute(
            "SELECT id FROM processing_tasks WHERE version_id = ? AND id = ?",
            (version_row["id"], dependency_id),
        ).fetchone()
        if dependency is None:
            raise NotFoundError(
                f"Dependency task {dependency_id} does not exist in version "
                f"{version_number} of dataset '{dataset_name}'"
            )

    try:
        cursor = conn.execute(
            "INSERT INTO processing_tasks ("
            "version_id, name, depends_on, max_attempts, status, attempt_count, "
            "created_at"
            ") VALUES (?, ?, ?, ?, 'pending', 0, ?)",
            (
                version_row["id"],
                clean_name,
                json.dumps(list(depends_on)),
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
        "SELECT * FROM processing_tasks WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return _task_row_to_dict(row, dataset["name"], version_row["version"])


def list_processing_tasks(
    conn: sqlite3.Connection, dataset_name: str, version_number: int
) -> list[dict]:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    rows = conn.execute(
        "SELECT * FROM processing_tasks WHERE version_id = ? ORDER BY id",
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
    task_row = _require_task(
        conn, version_row, dataset_name, version_number, task_id
    )

    run_rows = conn.execute(
        "SELECT * FROM processing_task_runs WHERE task_id = ? ORDER BY attempt",
        (task_row["id"],),
    ).fetchall()
    result = _task_row_to_dict(task_row, dataset["name"], version_row["version"])
    result["runs"] = [_run_row_to_dict(row) for row in run_rows]
    return result


def _insert_task_run(
    conn: sqlite3.Connection, task_id: int, attempt: int
) -> sqlite3.Row:
    """Insert one ``running`` run and return its stored row.

    The UNIQUE(task_id, attempt) constraint is the hard guard against a
    duplicate attempt created by a competing transaction (the dispatch
    endpoint or a concurrent single-task start).
    """
    cursor = conn.execute(
        "INSERT INTO processing_task_runs (task_id, attempt, status, started_at) "
        "VALUES (?, ?, 'running', ?)",
        (task_id, attempt, utc_now_iso()),
    )
    return conn.execute(
        "SELECT * FROM processing_task_runs WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()


def _start_task_run(conn: sqlite3.Connection, task_row: sqlite3.Row) -> dict:
    """Flip one eligible task to ``running`` and create its next attempt.

    The conditional UPDATE re-checks status and attempt budget against the
    current row so a task selected earlier in the same transaction (a
    dependency started by this very dispatch) or by a concurrent transaction
    cannot be started twice: it only matches a row still in the state read
    during eligibility checks.
    """
    attempt = task_row["attempt_count"] + 1
    cursor = conn.execute(
        "UPDATE processing_tasks SET attempt_count = ?, status = 'running' "
        "WHERE id = ? AND status = ? AND attempt_count = ? "
        "AND attempt_count < max_attempts",
        (attempt, task_row["id"], task_row["status"], task_row["attempt_count"]),
    )
    if cursor.rowcount != 1:
        # Lost a race against another transaction that started this task.
        raise ConflictError(
            f"Processing task {task_row['id']} was started concurrently"
        )
    return _run_row_to_dict(_insert_task_run(conn, task_row["id"], attempt))


def create_task_run(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    task_id: int,
) -> dict:
    # Take the write lock before any read on this fresh connection: a writer
    # transaction established up front never has to upgrade a shared lock
    # later (which can deadlock against another holder), and every eligibility
    # read already sees the most recently committed state. The process-wide
    # lock additionally makes concurrent starts in this process interleave as
    # whole request transactions.
    with _processing_write_section():
        conn.execute("BEGIN IMMEDIATE")
        try:
            return _create_task_run_locked(
                conn, dataset_name, version_number, task_id
            )
        except BaseException:
            conn.rollback()
            raise


def _create_task_run_locked(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    task_id: int,
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)
    task_row = _require_task(
        conn, version_row, dataset_name, version_number, task_id
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

    depends_on = json.loads(task_row["depends_on"])
    if depends_on:
        placeholders = ", ".join("?" for _ in depends_on)
        dependency_rows = conn.execute(
            f"SELECT id, status FROM processing_tasks WHERE id IN ({placeholders})",
            depends_on,
        ).fetchall()
        blocking = [row["id"] for row in dependency_rows if row["status"] != "succeeded"]
        if blocking:
            raise ConflictError(
                "Processing task "
                f"{task_id} cannot start: dependency task(s) "
                + ", ".join(str(dep_id) for dep_id in sorted(blocking))
                + " have not succeeded"
            )

    # The conditional UPDATE and UNIQUE(task_id, attempt) remain the hard
    # guards against a writer in another process that won the IMMEDIATE race.
    return _start_task_run(conn, task_row)


def dispatch_processing_tasks(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    limit: int,
) -> dict:
    """Start up to ``limit`` startable tasks of one version in one transaction.

    A task is startable when it is ``pending``, or ``failed`` with attempts
    left, and every direct dependency has ``succeeded`` at the moment of
    selection. Tasks are considered by id ascending and a task started earlier
    in this request is no longer ``succeeded``, so tasks that only become
    eligible through a same-request start are never selected. ``running``,
    ``succeeded``, attempt-exhausted and dependency-blocked tasks are skipped.

    The whole selection and every run insert share one transaction: either all
    selected starts commit together or none do.
    """
    with _processing_write_section():
        # BEGIN IMMEDIATE takes the SQLite write (RESERVED) lock up front, so a
        # concurrent dispatch (or single-task start) blocks here until the
        # holder commits instead of failing with a busy error deep in its
        # writes. The lock is released by the request transaction's
        # commit/rollback in db_session.
        conn.execute("BEGIN IMMEDIATE")
        try:
            dataset = require_dataset(conn, dataset_name)
            version_row = _require_schema_version(conn, dataset, version_number)

            rows = _version_task_rows(conn, version_row["id"])
            statuses: dict[int, str] = {row["id"]: row["status"] for row in rows}
            started: list[dict] = []
            for row in rows:
                if len(started) >= limit:
                    break
                if row["status"] not in ("pending", "failed"):
                    continue
                if row["attempt_count"] >= row["max_attempts"]:
                    continue
                dependencies = json.loads(row["depends_on"])
                # Reflect tasks started earlier in this very request: their
                # status is now 'running', so a dependent task is not (yet)
                # eligible.
                if any(statuses.get(dep_id) != "succeeded" for dep_id in dependencies):
                    continue
                started.append(_start_task_run(conn, row))
                statuses[row["id"]] = "running"
        except BaseException:
            conn.rollback()
            raise

    started.sort(key=lambda run: run["task_id"])
    return {
        "dataset": dataset["name"],
        "version": version_row["version"],
        "runs": started,
    }


def finish_task_run(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    task_id: int,
    run_id: int,
    status: str,
    error: str | None,
) -> dict:
    # Serialize against starts, dispatch and cancellation so a finish and a
    # cancel racing for the same running run interleave as whole transactions;
    # the conditional UPDATE below remains the hard cross-process guard.
    with _processing_write_section():
        conn.execute("BEGIN IMMEDIATE")
        try:
            return _finish_task_run_locked(
                conn,
                dataset_name,
                version_number,
                task_id,
                run_id,
                status,
                error,
            )
        except BaseException:
            conn.rollback()
            raise


def _finish_task_run_locked(
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
    task_row = _require_task(
        conn, version_row, dataset_name, version_number, task_id
    )

    if status == "succeeded":
        if error is not None:
            raise RequestInvalidError(
                "A successful run must not carry an error message"
            )
    else:
        if error is None or not error.strip():
            raise RequestInvalidError(
                "A failed run requires a non-empty error message"
            )

    run_row = conn.execute(
        "SELECT * FROM processing_task_runs WHERE id = ?", (run_id,)
    ).fetchone()
    if run_row is None:
        raise NotFoundError(f"Processing task run {run_id} does not exist")
    if run_row["task_id"] != task_row["id"]:
        raise RequestInvalidError(
            f"Processing task run {run_id} does not belong to task {task_id} "
            f"in version {version_number} of dataset '{dataset_name}'"
        )
    if run_row["status"] != "running":
        raise ConflictError(
            f"Processing task run {run_id} is already '{run_row['status']}' "
            "and cannot be finished again"
        )

    # The status predicate is the hard guard against a concurrent cancellation:
    # exactly one of finish/cancel matches the still-running row and completes
    # the transition; the loser matches no row and reports 409 instead of
    # overwriting the committed terminal state.
    cursor = conn.execute(
        "UPDATE processing_task_runs SET status = ?, finished_at = ?, error = ? "
        "WHERE id = ? AND status = 'running'",
        (status, utc_now_iso(), error, run_row["id"]),
    )
    if cursor.rowcount != 1:
        raise ConflictError(
            f"Processing task run {run_id} was finished or cancelled concurrently"
        )
    conn.execute(
        "UPDATE processing_tasks SET status = ? WHERE id = ? AND status = 'running'",
        (status, task_row["id"]),
    )
    updated = conn.execute(
        "SELECT * FROM processing_task_runs WHERE id = ?", (run_row["id"],)
    ).fetchone()
    return _run_row_to_dict(updated)


def cancel_task_run(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    task_id: int,
    run_id: int,
    reason: str,
    *,
    query_keys: tuple[str, ...] = (),
) -> dict:
    # Cancellation is a state-transition write like start/finish, so it shares
    # the process-wide processing-write lock and takes the SQLite write lock up
    # front; the conditional UPDATE below is the cross-process guard.
    with _processing_write_section():
        conn.execute("BEGIN IMMEDIATE")
        try:
            return _cancel_task_run_locked(
                conn,
                dataset_name,
                version_number,
                task_id,
                run_id,
                reason,
                query_keys=query_keys,
            )
        except BaseException:
            conn.rollback()
            raise


def _cancel_task_run_locked(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    task_id: int,
    run_id: int,
    reason: str,
    *,
    query_keys: tuple[str, ...],
) -> dict:
    # Resolve the path target first so an unknown dataset/version/task stays
    # 404, mirroring every other processing-task endpoint (and the audit
    # report's 404-before-422 precedence for parameter rejection).
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)
    task_row = _require_task(
        conn, version_row, dataset_name, version_number, task_id
    )

    # The endpoint takes no parameters beyond the single reason in the body:
    # any query parameter is rejected with the same stable 422 shape as an
    # invalid body, after the path task is resolved. Like the finish endpoint,
    # body-class validation (422) precedes run existence (404).
    if query_keys:
        raise RequestInvalidError(
            "The run cancellation endpoint does not accept query parameters"
        )

    clean_reason = reason.strip()
    if not clean_reason:
        raise RequestInvalidError("'reason' must not be empty")

    run_row = conn.execute(
        "SELECT * FROM processing_task_runs WHERE id = ?", (run_id,)
    ).fetchone()
    if run_row is None:
        raise NotFoundError(f"Processing task run {run_id} does not exist")
    if run_row["task_id"] != task_row["id"]:
        raise RequestInvalidError(
            f"Processing task run {run_id} does not belong to task {task_id} "
            f"in version {version_number} of dataset '{dataset_name}'"
        )
    if run_row["status"] != "running":
        raise ConflictError(
            f"Processing task run {run_id} is already '{run_row['status']}' "
            "and cannot be cancelled"
        )

    finished_at = utc_now_iso()
    # Run and task transition share this one IMMEDIATE transaction and the
    # predicates re-check the running state, so finish and cancel can never both
    # commit: either the cancellation writes finished_at/failed everywhere or it
    # leaves the prior committed state untouched.
    cursor = conn.execute(
        "UPDATE processing_task_runs "
        "SET status = 'failed', finished_at = ?, error = ? "
        "WHERE id = ? AND status = 'running'",
        (finished_at, clean_reason, run_row["id"]),
    )
    if cursor.rowcount != 1:
        raise ConflictError(
            f"Processing task run {run_id} was finished or cancelled concurrently"
        )
    task_cursor = conn.execute(
        "UPDATE processing_tasks SET status = 'failed' "
        "WHERE id = ? AND status = 'running'",
        (task_row["id"],),
    )
    if task_cursor.rowcount != 1:
        raise ConflictError(
            f"Processing task {task_row['id']} was finished concurrently"
        )
    updated = conn.execute(
        "SELECT * FROM processing_task_runs WHERE id = ?", (run_row["id"],)
    ).fetchone()
    return _run_row_to_dict(updated)


# --------------------------------------------------------------------------- #
# Dependency graph editing and the scheduling view
# --------------------------------------------------------------------------- #


def _version_task_rows(
    conn: sqlite3.Connection, version_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM processing_tasks WHERE version_id = ? ORDER BY id",
        (version_id,),
    ).fetchall()


def _task_dependency_graph(rows: list[sqlite3.Row]) -> dict[int, list[int]]:
    """Adjacency task id -> its direct dependency ids for one version."""
    return {row["id"]: json.loads(row["depends_on"]) for row in rows}


def _graph_reaches_target(
    graph: dict[int, list[int]], start: int, target: int
) -> bool:
    """Whether ``target`` is reachable from ``start`` along dependency edges."""
    visited = {start}
    stack = [start]
    while stack:
        current = stack.pop()
        for dependency in graph.get(current, ()):
            if dependency == target:
                return True
            if dependency not in visited:
                visited.add(dependency)
                stack.append(dependency)
    return False


def replace_task_dependencies(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    task_id: int,
    depends_on: list[int],
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)
    task_row = _require_task(
        conn, version_row, dataset_name, version_number, task_id
    )

    if len(set(depends_on)) != len(depends_on):
        raise RequestInvalidError("'depends_on' must not contain duplicate task ids")
    if task_id in depends_on:
        raise ConflictError(
            f"Processing task {task_id} must not depend on itself"
        )

    task_rows = _version_task_rows(conn, version_row["id"])
    existing_ids = {row["id"] for row in task_rows}
    unknown = [dependency_id for dependency_id in depends_on if dependency_id not in existing_ids]
    if unknown:
        raise NotFoundError(
            f"Dependency task {unknown[0]} does not exist in version "
            f"{version_number} of dataset '{dataset_name}'"
        )

    if task_row["status"] != "pending":
        raise ConflictError(
            f"Processing task {task_id} is '{task_row['status']}'; only pending "
            "tasks can have their dependencies replaced"
        )

    # Build the prospective graph (the target task's edges replaced, every other
    # task unchanged) and reject cycles. Only the target's edges change, so any
    # newly introduced cycle must pass through the target: it exists iff a new
    # dependency can already reach the target.
    graph = _task_dependency_graph(task_rows)
    graph[task_row["id"]] = list(depends_on)
    if any(
        _graph_reaches_target(graph, dependency_id, task_row["id"])
        for dependency_id in depends_on
    ):
        raise ConflictError(
            "Replacing the dependencies would introduce a cycle in the task graph"
        )

    # Validation above shares this transaction with the write, so the replacement
    # either commits atomically or leaves the graph untouched.
    conn.execute(
        "UPDATE processing_tasks SET depends_on = ? WHERE id = ?",
        (json.dumps(list(depends_on)), task_row["id"]),
    )
    updated = conn.execute(
        "SELECT * FROM processing_tasks WHERE id = ?", (task_row["id"],)
    ).fetchone()
    return _task_row_to_dict(updated, dataset["name"], version_row["version"])


def _schedule_states(
    graph: dict[int, tuple[str, int, int, list[int]]],
) -> dict[int, str]:
    """Derive the schedule state of every task in one version.

    Non-pending tasks map directly from their stored status (``failed`` splits
    into ``retryable``/``exhausted`` on remaining attempts). Pending tasks are
    resolved against their dependencies by fixed-point propagation: all direct
    dependencies succeeded -> ``ready``; any dependency chain exhausted or
    already ``upstream_failed`` -> ``upstream_failed``; otherwise ``blocked``.
    The graph is acyclic (enforced at creation and on every replacement), so
    the propagation terminates.
    """
    states: dict[int, str] = {}
    for task_id, (status, attempt_count, max_attempts, _deps) in graph.items():
        if status == "pending":
            continue
        if status == "failed":
            states[task_id] = (
                "retryable" if attempt_count < max_attempts else "exhausted"
            )
        else:
            states[task_id] = status

    changed = True
    while changed:
        changed = False
        for task_id, (status, _attempt_count, _max_attempts, deps) in graph.items():
            if status != "pending":
                continue
            if any(
                states.get(dependency_id) in ("exhausted", "upstream_failed")
                for dependency_id in deps
            ):
                new_state = "upstream_failed"
            elif all(
                states.get(dependency_id) == "succeeded" for dependency_id in deps
            ):
                # An empty dependency list vacuously satisfies "all succeeded".
                new_state = "ready"
            else:
                new_state = "blocked"
            if states.get(task_id) != new_state:
                states[task_id] = new_state
                changed = True
    return states


def get_processing_schedule(
    conn: sqlite3.Connection, dataset_name: str, version_number: int
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    rows = _version_task_rows(conn, version_row["id"])
    graph: dict[int, tuple[str, int, int, list[int]]] = {
        row["id"]: (
            row["status"],
            row["attempt_count"],
            row["max_attempts"],
            json.loads(row["depends_on"]),
        )
        for row in rows
    }
    states = _schedule_states(graph)

    tasks: list[dict] = []
    for row in rows:
        task = _task_row_to_dict(row, dataset["name"], version_row["version"])
        task["schedule_state"] = states[row["id"]]
        if row["status"] == "pending":
            deps = graph[row["id"]][3]
            task["blocking_task_ids"] = sorted(
                dependency_id
                for dependency_id in deps
                if states.get(dependency_id) != "succeeded"
            )
        else:
            task["blocking_task_ids"] = []
        tasks.append(task)

    return {
        "dataset": dataset["name"],
        "version": version_row["version"],
        "tasks": tasks,
    }


# --------------------------------------------------------------------------- #
# Processing task run audit records (append-only proof chain)
# --------------------------------------------------------------------------- #


# Serializes read-tail -> append within this process so concurrent requests
# never compute the same sequence. SQLite's UNIQUE(run_id, sequence) constraint
# is the hard guard against writers in other processes; such a collision is
# resolved with a bounded re-read-and-retry.
_audit_append_locks: dict[int, threading.Lock] = {}
_audit_append_locks_guard = threading.Lock()

# Bound on re-read-and-retry attempts when a writer in another process wins the
# race for the same (run_id, sequence).
_AUDIT_APPEND_MAX_ATTEMPTS = 100


def _audit_append_lock(run_id: int) -> threading.Lock:
    with _audit_append_locks_guard:
        lock = _audit_append_locks.get(run_id)
        if lock is None:
            lock = threading.Lock()
            _audit_append_locks[run_id] = lock
        return lock


AUDIT_EVIDENCE_FIELDS = (
    "event",
    "input_summary",
    "previous_hash",
    "result_summary",
    "run_status",
    "sequence",
)


def _audit_evidence_hash(record: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON of every hashed field.

    Keys are sorted by Unicode code point, no whitespace is emitted and the
    text is UTF-8 encoded (non-ASCII characters are not escaped). ``id``,
    ``created_at`` and ``evidence_hash`` itself are excluded.
    """
    payload = {field: record[field] for field in AUDIT_EVIDENCE_FIELDS}
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _audit_record_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "sequence": row["sequence"],
        "event": row["event"],
        "input_summary": row["input_summary"],
        "result_summary": row["result_summary"],
        "run_status": row["run_status"],
        "previous_hash": row["previous_hash"],
        "evidence_hash": row["evidence_hash"],
        "created_at": row["created_at"],
    }


def _resolve_run(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    task_id: int,
    run_id: int,
) -> tuple[dict, sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    """Resolve a path to (dataset, version, task, run).

    Unknown dataset/version/task/run are 404; a run that exists but belongs to a
    different task (including a task in another version) is 422.
    """
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)
    task_row = _require_task(
        conn, version_row, dataset_name, version_number, task_id
    )
    run_row = conn.execute(
        "SELECT * FROM processing_task_runs WHERE id = ?", (run_id,)
    ).fetchone()
    if run_row is None:
        raise NotFoundError(f"Processing task run {run_id} does not exist")
    if run_row["task_id"] != task_row["id"]:
        raise RequestInvalidError(
            f"Processing task run {run_id} does not belong to task {task_id} "
            f"in version {version_number} of dataset '{dataset_name}'"
        )
    return dataset, version_row, task_row, run_row


def create_audit_record(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    task_id: int,
    run_id: int,
    event: str,
    input_summary: str,
    result_summary: str,
) -> dict:
    # Precedence mirrors finishing a run: the path dataset/version/task must
    # exist (404); blank fields are then rejected (422); finally the run itself
    # must exist (404) and belong to the path task (422).
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)
    task_row = _require_task(
        conn, version_row, dataset_name, version_number, task_id
    )

    for label, value in (
        ("event", event),
        ("input_summary", input_summary),
        ("result_summary", result_summary),
    ):
        if not value.strip():
            raise RequestInvalidError(f"'{label}' must not be empty")

    run_row = conn.execute(
        "SELECT * FROM processing_task_runs WHERE id = ?", (run_id,)
    ).fetchone()
    if run_row is None:
        raise NotFoundError(f"Processing task run {run_id} does not exist")
    if run_row["task_id"] != task_row["id"]:
        raise RequestInvalidError(
            f"Processing task run {run_id} does not belong to task {task_id} "
            f"in version {version_number} of dataset '{dataset_name}'"
        )

    # The per-run lock makes read-tail -> append atomic within this process
    # (FastAPI runs this synchronous endpoint in one process's threadpool).
    # The UNIQUE(run_id, sequence) constraint is the hard cross-process guard;
    # a writer that loses a race against another process retries with the new
    # tail instead of failing the request.
    with _audit_append_lock(run_id):
        for attempt in range(_AUDIT_APPEND_MAX_ATTEMPTS):
            # Read the run status and chain tail immediately before inserting so
            # both reflect the state at write time.
            run_status = conn.execute(
                "SELECT status FROM processing_task_runs WHERE id = ?", (run_id,)
            ).fetchone()["status"]
            tail = conn.execute(
                "SELECT sequence, evidence_hash FROM processing_task_audit_records "
                "WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if tail is None:
                sequence = 1
                previous_hash = None
            else:
                sequence = tail["sequence"] + 1
                previous_hash = tail["evidence_hash"]

            record = {
                "sequence": sequence,
                "event": event,
                "input_summary": input_summary,
                "result_summary": result_summary,
                "run_status": run_status,
                "previous_hash": previous_hash,
            }
            try:
                cursor = conn.execute(
                    "INSERT INTO processing_task_audit_records ("
                    "run_id, sequence, event, input_summary, result_summary, "
                    "run_status, previous_hash, evidence_hash, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        sequence,
                        event,
                        input_summary,
                        result_summary,
                        run_status,
                        previous_hash,
                        _audit_evidence_hash(record),
                        utc_now_iso(),
                    ),
                )
                break
            except sqlite3.IntegrityError:
                # Another process inserted the same sequence first. End the
                # current (pinned) transaction so the re-read establishes a new
                # snapshot that includes the competing commit, then retry.
                if attempt + 1 == _AUDIT_APPEND_MAX_ATTEMPTS:
                    conn.rollback()
                    raise ConflictError(
                        "Too many concurrent audit-record writes; retry the request"
                    )
                conn.rollback()
                continue

    stored = conn.execute(
        "SELECT * FROM processing_task_audit_records WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _audit_record_row_to_dict(stored)


def list_audit_records(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    task_id: int,
    run_id: int,
) -> list[dict]:
    _resolve_run(conn, dataset_name, version_number, task_id, run_id)
    rows = conn.execute(
        "SELECT * FROM processing_task_audit_records WHERE run_id = ? "
        "ORDER BY sequence ASC",
        (run_id,),
    ).fetchall()
    return [_audit_record_row_to_dict(row) for row in rows]


def _verify_run_audit_rows(rows: list[sqlite3.Row]) -> tuple[bool, int, str | None]:
    """Re-verify one run's stored audit chain.

    Recomputes every ``evidence_hash`` and checks that ``sequence`` values are
    continuous from ``1`` and each ``previous_hash`` equals the preceding
    record's ``evidence_hash`` (the first must be null). Returns
    ``(valid, checked_count, last_evidence_hash)``; the trailing hash is the
    stored evidence hash of the final record and is null only for an empty
    chain, so even a failed verification still reports the stored chain tail.
    """
    valid = True
    expected_sequence = 1
    previous_hash: str | None = None
    for row in rows:
        record = _audit_record_row_to_dict(row)
        if record["sequence"] != expected_sequence:
            valid = False
        if record["previous_hash"] != previous_hash:
            valid = False
        if _audit_evidence_hash(record) != record["evidence_hash"]:
            valid = False
        previous_hash = record["evidence_hash"]
        expected_sequence += 1

    last_evidence_hash = rows[-1]["evidence_hash"] if rows else None
    return valid, len(rows), last_evidence_hash


def verify_audit_chain(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    task_id: int,
    run_id: int,
) -> dict:
    _resolve_run(conn, dataset_name, version_number, task_id, run_id)
    rows = conn.execute(
        "SELECT * FROM processing_task_audit_records WHERE run_id = ? "
        "ORDER BY sequence ASC",
        (run_id,),
    ).fetchall()

    valid, checked_count, _last_hash = _verify_run_audit_rows(rows)

    return {
        "dataset": dataset_name,
        "version": version_number,
        "task_id": task_id,
        "run_id": run_id,
        "valid": valid,
        "checked_count": checked_count,
    }


def get_processing_audit_report(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> dict:
    """Read-only audit report over every task and run of one version.

    The endpoint takes no parameters: a non-empty request body or any query
    parameter is rejected (422) only after the path dataset/version has been
    resolved, so an unknown dataset/version stays a 404. The summary and the
    task/run details are then computed in the same pass, so the counters always
    agree with the returned details. Each run's ``proof`` re-verifies its
    append-only audit chain (see ``_verify_run_audit_rows``); an empty chain
    verifies with a null ``last_evidence_hash``.
    """
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    if body.strip():
        raise RequestInvalidError(
            "The audit report endpoint does not accept a request body"
        )
    if query_keys:
        raise RequestInvalidError(
            "The audit report endpoint does not accept query parameters"
        )

    task_rows = _version_task_rows(conn, version_row["id"])
    tasks: list[dict] = []
    run_count = 0
    invalid_audit_runs = 0
    status_counts = {"pending": 0, "running": 0, "succeeded": 0, "failed": 0}
    exhausted_tasks = 0

    for task_row in task_rows:
        task = _task_row_to_dict(
            task_row, dataset["name"], version_row["version"]
        )
        run_rows = conn.execute(
            "SELECT * FROM processing_task_runs WHERE task_id = ? ORDER BY attempt",
            (task_row["id"],),
        ).fetchall()
        runs: list[dict] = []
        for run_row in run_rows:
            run = _run_row_to_dict(run_row)
            audit_rows = conn.execute(
                "SELECT * FROM processing_task_audit_records WHERE run_id = ? "
                "ORDER BY sequence ASC",
                (run_row["id"],),
            ).fetchall()
            valid, checked_count, last_hash = _verify_run_audit_rows(audit_rows)
            if not valid:
                invalid_audit_runs += 1
            run["proof"] = {
                "valid": valid,
                "checked_count": checked_count,
                "last_evidence_hash": last_hash,
            }
            runs.append(run)

        run_count += len(runs)
        status_counts[task["status"]] += 1
        if (
            task["status"] == "failed"
            and task["attempt_count"] >= task["max_attempts"]
        ):
            exhausted_tasks += 1
        task["runs"] = runs
        tasks.append(task)

    return {
        "dataset": dataset["name"],
        "version": version_row["version"],
        "summary": {
            "task_count": len(tasks),
            "run_count": run_count,
            "pending_tasks": status_counts["pending"],
            "running_tasks": status_counts["running"],
            "succeeded_tasks": status_counts["succeeded"],
            "failed_tasks": status_counts["failed"],
            "exhausted_tasks": exhausted_tasks,
            "invalid_audit_runs": invalid_audit_runs,
        },
        "tasks": tasks,
    }
