"""Data-access and business rules for datasets, schema versions and lineage."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from collections import Counter, deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import db_session
from app.errors import ConflictError, NotFoundError, RequestInvalidError
from app.models import ERROR_FIELD_UNSET, FieldSpec


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
# Read-only schema version diff
# --------------------------------------------------------------------------- #


def _version_field_definitions(
    conn: sqlite3.Connection, version_id: int
) -> dict[str, dict]:
    """Persisted field definitions of one version keyed by field name."""
    rows = conn.execute(
        "SELECT name, type, nullable FROM schema_fields WHERE version_id = ?",
        (version_id,),
    ).fetchall()
    return {
        row["name"]: {"type": row["type"], "nullable": bool(row["nullable"])}
        for row in rows
    }


def diff_schema_versions(
    conn: sqlite3.Connection,
    dataset_name: str,
    from_version: int,
    to_version: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> dict:
    """Read-only diff of the persisted field definitions of two versions.

    ``from`` is the baseline and ``to`` the target. Only the stored field
    definitions (name, type, nullable) are compared; field position is not a
    difference. Changes are sorted by field name and each carries ``before``
    and ``after`` definitions, null on the side where the field does not
    exist. ``compatible`` reports whether the target can take over the
    baseline's fields: a removal, a type change or a nullable tightening
    (nullable -> not nullable) makes it false; additions and nullable
    loosening keep it true.

    The endpoint takes no parameters: a non-positive version number is a 422
    checked before path resolution, while a non-empty request body or any
    query parameter is a 422 raised only after the path dataset and both
    versions have resolved, so an unknown dataset/version stays a 404
    (mirroring the parameterless audit-report endpoint). Nothing is written.
    """
    if from_version < 1 or to_version < 1:
        raise RequestInvalidError(
            "Version numbers in the diff path must be positive integers"
        )

    dataset = require_dataset(conn, dataset_name)
    from_row = _require_schema_version(conn, dataset, from_version)
    to_row = _require_schema_version(conn, dataset, to_version)

    if body.strip():
        raise RequestInvalidError(
            "The version diff endpoint does not accept a request body"
        )
    if query_keys:
        raise RequestInvalidError(
            "The version diff endpoint does not accept query parameters"
        )

    from_fields = _version_field_definitions(conn, from_row["id"])
    to_fields = _version_field_definitions(conn, to_row["id"])

    changes: list[dict] = []
    compatible = True
    for name in sorted(set(from_fields) | set(to_fields)):
        before = from_fields.get(name)
        after = to_fields.get(name)
        if before == after:
            continue
        if before is None:
            changes.append(
                {"field": name, "kind": "added", "before": None, "after": after}
            )
        elif after is None:
            changes.append(
                {"field": name, "kind": "removed", "before": before, "after": None}
            )
            compatible = False
        else:
            changes.append(
                {"field": name, "kind": "changed", "before": before, "after": after}
            )
            if before["type"] != after["type"] or (
                before["nullable"] and not after["nullable"]
            ):
                compatible = False

    return {
        "from_version": from_row["version"],
        "to_version": to_row["version"],
        "compatible": compatible,
        "changes": changes,
    }


# --------------------------------------------------------------------------- #
# Read-only schema version compatibility check
# --------------------------------------------------------------------------- #


def check_schema_version_compatibility(
    conn: sqlite3.Connection,
    dataset_name: str,
    base_version: int,
    target_version: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> dict:
    """Read-only breaking-change check of a target version against a base.

    Only the persisted field definitions (name, type, nullable) are compared;
    field position is not a difference. A breaking change is exactly one of:
    the target removed a base field (``removed``), the target changed a
    field's type (``type_changed``) or the target tightened a nullable field
    to not nullable (``nullable_tightened``). Added fields and nullable
    loosening are not breaking. Entries are sorted by field name and each
    carries ``before`` (base) and ``after`` (target) definitions, null on the
    side where the field does not exist. ``breaking_change_count`` equals the
    number of entries. Comparing a version with itself yields an empty list
    and a zero count; either version order is allowed.

    The endpoint takes no parameters: a non-positive version number is a 422
    checked before path resolution, while any request body bytes (including
    whitespace-only bytes) or any query parameter is a 422 raised only after
    the path dataset and both versions have resolved, so an unknown
    dataset/version stays a 404 (mirroring the version diff endpoint).
    Nothing is written.
    """
    if base_version < 1 or target_version < 1:
        raise RequestInvalidError(
            "Version numbers in the compatibility path must be positive integers"
        )

    dataset = require_dataset(conn, dataset_name)
    base_row = _require_schema_version(conn, dataset, base_version)
    target_row = _require_schema_version(conn, dataset, target_version)

    if body:
        raise RequestInvalidError(
            "The version compatibility endpoint does not accept a request body"
        )
    if query_keys:
        raise RequestInvalidError(
            "The version compatibility endpoint does not accept query parameters"
        )

    base_fields = _version_field_definitions(conn, base_row["id"])
    target_fields = _version_field_definitions(conn, target_row["id"])

    breaking_changes: list[dict] = []
    for name in sorted(set(base_fields) | set(target_fields)):
        before = base_fields.get(name)
        after = target_fields.get(name)
        if before is None:
            # Added in the target: never breaking.
            continue
        if after is None:
            breaking_changes.append(
                {"field": name, "kind": "removed", "before": before, "after": None}
            )
        elif before["type"] != after["type"]:
            breaking_changes.append(
                {
                    "field": name,
                    "kind": "type_changed",
                    "before": before,
                    "after": after,
                }
            )
        elif before["nullable"] and not after["nullable"]:
            breaking_changes.append(
                {
                    "field": name,
                    "kind": "nullable_tightened",
                    "before": before,
                    "after": after,
                }
            )

    return {
        "base_version": base_row["version"],
        "target_version": target_row["version"],
        "breaking_changes": breaking_changes,
        "breaking_change_count": len(breaking_changes),
    }


# --------------------------------------------------------------------------- #
# Read-only compatibility check joined with downstream lineage impact
# --------------------------------------------------------------------------- #


def _field_id_by_name(
    conn: sqlite3.Connection, version_id: int, field_name: str
) -> int | None:
    row = conn.execute(
        "SELECT id FROM schema_fields WHERE version_id = ? AND name = ?",
        (version_id, field_name),
    ).fetchone()
    return None if row is None else row["id"]


def _compute_impacted_multi(
    conn: sqlite3.Connection, source_field_ids: list[int]
) -> list[dict]:
    """All fields reachable downstream of any of the sources.

    Every source id is seeded into the visited set, so cycles terminate and
    none of the start fields ever appears in the result. Purely read-only:
    unlike :func:`get_lineage_impact` this never touches the impact cache.
    """
    edges = _lineage_forward_edges(conn)
    visited = set(source_field_ids)
    impacted: dict[tuple[str, int, str], dict] = {}
    queue = list(source_field_ids)
    while queue:
        current = queue.pop()
        for target_field_id, ref in edges.get(current, ()):
            if target_field_id in visited:
                continue
            visited.add(target_field_id)
            impacted[(ref["dataset"], ref["version"], ref["field"])] = ref
            queue.append(target_field_id)
    return [impacted[key] for key in sorted(impacted)]


def _breaking_changes_with_impact(
    conn: sqlite3.Connection,
    base_row: sqlite3.Row,
    target_row: sqlite3.Row,
) -> list[dict]:
    """Breaking-change entries of the target version against the base.

    Shared by the pairwise compatibility impact check and the per-dataset
    evolution summary. A breaking change is exactly one of ``removed``,
    ``type_changed`` or ``nullable_tightened``; a field with several changes
    collapses into one entry, a type change winning over a nullable
    tightening. Each entry additionally carries ``impacted``: every field
    directly or indirectly downstream of the broken field along the lineage
    mappings. The traversal starts from the same-named field of the base
    version and, when the field still exists in the target version, also from
    the target version's field; the merged set is deduplicated, never
    contains a start field itself (cycles terminate) and is sorted by
    dataset, version and field ascending. Entries are sorted by field name.
    """
    base_fields = _version_field_definitions(conn, base_row["id"])
    target_fields = _version_field_definitions(conn, target_row["id"])

    breaking_changes: list[dict] = []
    for name in sorted(set(base_fields) | set(target_fields)):
        before = base_fields.get(name)
        after = target_fields.get(name)
        if before is None:
            # Added in the target: never breaking.
            continue
        if after is None:
            kind = "removed"
        elif before["type"] != after["type"]:
            kind = "type_changed"
        elif before["nullable"] and not after["nullable"]:
            kind = "nullable_tightened"
        else:
            continue

        # The base version always has the broken field; the target version's
        # same-named field joins the traversal only when it still exists.
        start_ids = [_field_id_by_name(conn, base_row["id"], name)]
        if after is not None:
            start_ids.append(_field_id_by_name(conn, target_row["id"], name))
        impacted = _compute_impacted_multi(
            conn, [field_id for field_id in start_ids if field_id is not None]
        )

        breaking_changes.append(
            {
                "field": name,
                "kind": kind,
                "before": before,
                "after": after,
                "impacted": impacted,
            }
        )
    return breaking_changes


def check_schema_version_compatibility_impact(
    conn: sqlite3.Connection,
    dataset_name: str,
    base_version: int,
    target_version: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> dict:
    """Read-only breaking-change check joined with downstream lineage impact.

    The breaking changes are exactly those of
    :func:`check_schema_version_compatibility` (``removed``, ``type_changed``
    or ``nullable_tightened``; a field with several changes collapses into one
    entry, a type change winning over a nullable tightening), and each entry
    additionally carries ``impacted``: every field directly or indirectly
    downstream of the broken field along the lineage mappings. The traversal
    starts from the same-named field of the base version and, when the field
    still exists in the target version, also from the target version's field;
    the merged set is deduplicated, never contains a start field itself
    (cycles terminate) and is sorted by dataset, version and field ascending.
    A field with no downstream yields an empty list; comparing a version with
    itself yields an empty entry list and a zero count.

    The endpoint takes no parameters: a non-positive version number is a 422
    checked before path resolution, while any request body bytes (including
    whitespace-only bytes) or any query parameter is a 422 raised only after
    the path dataset and both versions have resolved, so an unknown
    dataset/version stays a 404 (mirroring the compatibility endpoint).
    Nothing is written: version definitions, lineage mappings and the impact
    cache are all left untouched.
    """
    if base_version < 1 or target_version < 1:
        raise RequestInvalidError(
            "Version numbers in the compatibility path must be positive integers"
        )

    dataset = require_dataset(conn, dataset_name)
    base_row = _require_schema_version(conn, dataset, base_version)
    target_row = _require_schema_version(conn, dataset, target_version)

    if body:
        raise RequestInvalidError(
            "The version compatibility impact endpoint does not accept a "
            "request body"
        )
    if query_keys:
        raise RequestInvalidError(
            "The version compatibility impact endpoint does not accept query "
            "parameters"
        )

    breaking_changes = _breaking_changes_with_impact(conn, base_row, target_row)
    return {
        "base_version": base_row["version"],
        "target_version": target_row["version"],
        "breaking_changes": breaking_changes,
        "breaking_change_count": len(breaking_changes),
    }


# --------------------------------------------------------------------------- #
# Read-only per-dataset adjacent-version evolution summary
# --------------------------------------------------------------------------- #


def summarize_schema_version_evolution(
    conn: sqlite3.Connection,
    dataset_name: str,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> dict:
    """Read-only summary of adjacent-version breaking changes and impact.

    One entry per adjacent version pair of the dataset, ordered by base
    version number ascending; a pair with no breaking change is listed with
    zero counts. The breaking entries of a pair are exactly those of
    :func:`check_schema_version_compatibility_impact` (``removed``,
    ``type_changed`` or ``nullable_tightened``, several changes of one field
    collapsing into a single entry) and ``breaking_count`` is their number.
    The pair's impacted set is the union of every entry's downstream fields —
    same start-field rule as the pairwise impact response, merged and
    deduplicated, never containing a start field itself (cycles terminate) —
    and ``impacted_count`` is its size; ``impacted_datasets`` lists the
    distinct dataset names appearing in it, sorted ascending. ``totals``
    counts the pairs and sums the per-pair breaking and deduplicated impacted
    counts over the whole dataset. A dataset with fewer than two versions
    yields an empty pair list and all-zero totals, never an error.

    The summary is recomputed from the persisted definitions and lineage
    mappings on every read: it caches nothing and never writes, so version
    definitions, lineage mappings and the impact cache are all left
    untouched, and quality or privacy state plays no role. The dataset
    resolves first (404); any request body bytes (including whitespace-only
    bytes) or any query parameter is a 422 checked afterwards, and every
    ordering is computed explicitly rather than read from database order.
    """
    dataset = require_dataset(conn, dataset_name)

    if body:
        raise RequestInvalidError(
            "The schema evolution summary endpoint does not accept a "
            "request body"
        )
    if query_keys:
        raise RequestInvalidError(
            "The schema evolution summary endpoint does not accept query "
            "parameters"
        )

    version_rows = conn.execute(
        "SELECT id, version FROM schema_versions WHERE dataset_id = ?",
        (dataset["id"],),
    ).fetchall()
    ordered = sorted(version_rows, key=lambda row: row["version"])

    pairs: list[dict] = []
    for base_row, target_row in zip(ordered, ordered[1:]):
        breaking_changes = _breaking_changes_with_impact(conn, base_row, target_row)
        impacted_refs = {
            (item["dataset"], item["version"], item["field"])
            for change in breaking_changes
            for item in change["impacted"]
        }
        pairs.append(
            {
                "base_version": base_row["version"],
                "target_version": target_row["version"],
                "breaking_count": len(breaking_changes),
                "impacted_count": len(impacted_refs),
                "impacted_datasets": sorted({ref[0] for ref in impacted_refs}),
            }
        )

    totals = {
        "pair_count": len(pairs),
        "breaking_count": sum(pair["breaking_count"] for pair in pairs),
        "impacted_count": sum(pair["impacted_count"] for pair in pairs),
    }
    return {"dataset": dataset["name"], "pairs": pairs, "totals": totals}


# --------------------------------------------------------------------------- #
# Read-only per-field cross-version trajectory
# --------------------------------------------------------------------------- #


def _field_status_change(before: dict | None, after: dict | None) -> str:
    """Status literal of one field between two adjacent versions.

    The literals reuse the breaking-entry vocabulary of the compatibility
    check (``removed``, ``type_changed``, ``nullable_tightened``) extended
    with the non-breaking outcomes: ``added`` when the field appears on the
    target side, ``nullable_loosened`` when only the nullability relaxes and
    ``unchanged`` when both definitions agree (including both absent).
    Several changes of one field collapse into a single status, a type
    change winning over a nullable tightening (mirrors the breaking-entry
    rule of the compatibility check).
    """
    if before is None:
        return "added" if after is not None else "unchanged"
    if after is None:
        return "removed"
    if before == after:
        return "unchanged"
    if before["type"] != after["type"]:
        return "type_changed"
    if before["nullable"] and not after["nullable"]:
        return "nullable_tightened"
    return "nullable_loosened"


def get_field_trajectory(
    conn: sqlite3.Connection,
    dataset_name: str,
    field_name: str,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> dict:
    """Read-only cross-version trajectory of one field of a dataset.

    ``entries`` lists every schema version of the dataset ordered by version
    number ascending, each carrying the field's persisted definition in that
    version (only ``type`` and ``nullable``) or ``None`` — key retained —
    when the field does not exist there. ``changes`` lists one entry per
    adjacent version pair, ordered by base version ascending; each entry
    carries the pair's ``status`` (see :func:`_field_status_change`) and the
    field's downstream impact for that pair, computed with the same
    start-field rule as the compatibility impact response: the traversal
    starts from the base version's same-named field and, when the field
    exists in the target version, also from the target version's field. The
    impacted set is deduplicated, never contains a start field itself
    (cycles terminate), is sorted by dataset, version and field ascending
    and is empty when the field has no downstream; ``impacted_datasets``
    lists the distinct dataset names appearing in it, sorted ascending. A
    dataset with fewer than two versions yields an empty change list, never
    an error.

    Only the persisted version field definitions and lineage mappings are
    read; quality, privacy, snapshot and processing-task state play no
    role. The trajectory is recomputed on every read: it caches nothing and
    never writes, so version definitions, lineage mappings and the impact
    cache are all left untouched, and every ordering is computed explicitly
    rather than read from database order.

    The dataset resolves first (404), then the field must appear in at
    least one of the dataset's versions (404); any request body bytes
    (including whitespace-only bytes) or any query parameter is a 422
    checked afterwards, so both 404 cases keep precedence over the request
    shape checks (mirroring the evolution summary endpoint).
    """
    dataset = require_dataset(conn, dataset_name)

    version_rows = conn.execute(
        "SELECT id, version FROM schema_versions WHERE dataset_id = ?",
        (dataset["id"],),
    ).fetchall()
    ordered = sorted(version_rows, key=lambda row: row["version"])
    definitions = [
        _version_field_definitions(conn, row["id"]) for row in ordered
    ]
    if not any(field_name in fields for fields in definitions):
        raise NotFoundError(
            f"Field '{field_name}' does not exist in any schema version of "
            f"dataset '{dataset_name}'"
        )

    if body:
        raise RequestInvalidError(
            "The field trajectory endpoint does not accept a request body"
        )
    if query_keys:
        raise RequestInvalidError(
            "The field trajectory endpoint does not accept query parameters"
        )

    entries = [
        {"version": row["version"], "definition": fields.get(field_name)}
        for row, fields in zip(ordered, definitions)
    ]

    changes: list[dict] = []
    for index, (base_row, target_row) in enumerate(zip(ordered, ordered[1:])):
        before = definitions[index].get(field_name)
        after = definitions[index + 1].get(field_name)
        # The base version's same-named field starts the traversal when it
        # exists; the target version's joins when it exists (the same
        # start-field rule as the compatibility impact response).
        start_ids = []
        if before is not None:
            start_ids.append(_field_id_by_name(conn, base_row["id"], field_name))
        if after is not None:
            start_ids.append(
                _field_id_by_name(conn, target_row["id"], field_name)
            )
        impacted = _compute_impacted_multi(
            conn, [field_id for field_id in start_ids if field_id is not None]
        )
        changes.append(
            {
                "base_version": base_row["version"],
                "target_version": target_row["version"],
                "status": _field_status_change(before, after),
                "impacted": impacted,
                "impacted_datasets": sorted(
                    {item["dataset"] for item in impacted}
                ),
            }
        )

    return {
        "dataset": dataset["name"],
        "field": field_name,
        "entries": entries,
        "changes": changes,
    }


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
# Lineage impact shortest-path query (read-only, never cached)
# --------------------------------------------------------------------------- #


def _lineage_field_refs(conn: sqlite3.Connection) -> dict[int, dict]:
    """Location key (dataset/version/field) of every persisted field by id."""
    rows = conn.execute(
        """
        SELECT  sf.id    AS field_id,
                d.name   AS dataset,
                sv.version AS version,
                sf.name  AS field
        FROM    schema_fields sf
        JOIN    schema_versions sv ON sv.id = sf.version_id
        JOIN    datasets d ON d.id = sv.dataset_id
        """
    ).fetchall()
    return {
        row["field_id"]: {
            "dataset": row["dataset"],
            "version": row["version"],
            "field": row["field"],
        }
        for row in rows
    }


def _compute_impact_paths(
    conn: sqlite3.Connection, source_field_id: int
) -> list[dict]:
    """Shortest lineage path from the source to every downstream field.

    Breadth-first traversal over the lineage graph guarantees the minimum
    number of edges; each node's outgoing links are expanded in location-key
    (dataset, version, field) order, so the first discovery of a node is the
    lexicographically smallest node sequence among its shortest paths. The
    source id is seeded as visited, so cycles terminate and the source itself
    never appears in the result. Every ordering is computed explicitly here
    rather than read from database order.
    """
    edges = _lineage_forward_edges(conn)
    refs = _lineage_field_refs(conn)
    for targets in edges.values():
        targets.sort(
            key=lambda item: (
                item[1]["dataset"],
                item[1]["version"],
                item[1]["field"],
            )
        )

    shortest: dict[int, list[dict]] = {source_field_id: [refs[source_field_id]]}
    queue = deque([source_field_id])
    while queue:
        current = queue.popleft()
        current_path = shortest[current]
        for target_field_id, ref in edges.get(current, ()):
            if target_field_id in shortest:
                continue
            shortest[target_field_id] = current_path + [ref]
            queue.append(target_field_id)

    items: list[dict] = []
    for target_field_id in sorted(
        (field_id for field_id in shortest if field_id != source_field_id),
        key=lambda field_id: (
            refs[field_id]["dataset"],
            refs[field_id]["version"],
            refs[field_id]["field"],
        ),
    ):
        path = shortest[target_field_id]
        items.append(
            {
                "dataset": path[-1]["dataset"],
                "version": path[-1]["version"],
                "field": path[-1]["field"],
                "path": path,
                "path_length": len(path) - 1,
            }
        )
    return items


def get_lineage_impact_paths(
    conn: sqlite3.Connection,
    dataset_name: str,
    version: int,
    field: str | None,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
    field_values: tuple[str, ...] = (),
) -> dict:
    """Read-only shortest-path explanation of one source field's impact.

    The path dataset and version resolve first (404); a missing or blank
    ``field`` parameter cannot name a resource and is a 422, after which the
    field itself must exist (404). Only then are request-body bytes, query
    parameters other than ``field`` and a repeated ``field`` parameter
    rejected with 422, so an unknown dataset, version or field always keeps
    its 404 precedence over request-shape errors (mirroring the other
    read-only lineage reads). Computed fresh on every read: nothing is
    written and the impact cache is never read or written.
    """
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version)

    if field is None or not field.strip():
        raise RequestInvalidError(
            "Query parameter 'field' is required and must be a non-empty "
            "field name"
        )
    field = field.strip()

    field_row = conn.execute(
        "SELECT id FROM schema_fields WHERE version_id = ? AND name = ?",
        (version_row["id"], field),
    ).fetchone()
    if field_row is None:
        raise NotFoundError(
            f"Source field '{field}' does not exist in version "
            f"{version} of dataset '{dataset['name']}'"
        )

    if body:
        raise RequestInvalidError(
            "The lineage impact paths endpoint does not accept a request body"
        )
    unknown_keys = sorted(set(query_keys) - {"field"})
    if unknown_keys:
        raise RequestInvalidError(
            "Unknown query parameter(s): " + ", ".join(unknown_keys)
        )
    if len(field_values) > 1:
        raise RequestInvalidError(
            "Query parameter 'field' must be provided exactly once"
        )

    impacts = _compute_impact_paths(conn, field_row["id"])
    direct_count = sum(1 for item in impacts if item["path_length"] == 1)
    return {
        "source": {
            "dataset": dataset["name"],
            "version": version,
            "field": field,
        },
        "impacts": impacts,
        "direct_count": direct_count,
        "indirect_count": len(impacts) - direct_count,
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

    # Every successful evaluation appends an immutable summary to the history
    # (an empty submission is recorded too, with zero violations). A rejected
    # or failed evaluation raises above and leaves no record.
    _record_quality_rule_evaluation(
        conn,
        version_row["id"],
        row_count=len(rows),
        violation_row_count=len(
            {index for result in results for index in result["violations"]}
        ),
        results=results,
    )

    return {
        "dataset": dataset["name"],
        "version": version_row["version"],
        "results": results,
    }


# --------------------------------------------------------------------------- #
# Quality rule evaluation history (append-only) and read-only diff
# --------------------------------------------------------------------------- #


# Serializes read-tail -> append within this process so concurrent evaluations
# never compute the same sequence. SQLite's UNIQUE(version_id, sequence)
# constraint is the hard guard against writers in other processes; such a
# collision is resolved with a bounded re-read-and-retry (mirrors the audit
# record append).
_evaluation_append_locks: dict[int, threading.Lock] = {}
_evaluation_append_locks_guard = threading.Lock()

# Bound on re-read-and-retry attempts when a writer in another process wins the
# race for the same (version_id, sequence).
_EVALUATION_APPEND_MAX_ATTEMPTS = 100


def _evaluation_append_lock(version_id: int) -> threading.Lock:
    with _evaluation_append_locks_guard:
        lock = _evaluation_append_locks.get(version_id)
        if lock is None:
            lock = threading.Lock()
            _evaluation_append_locks[version_id] = lock
        return lock


def _record_quality_rule_evaluation(
    conn: sqlite3.Connection,
    version_id: int,
    *,
    row_count: int,
    violation_row_count: int,
    results: list[dict],
) -> None:
    """Append one evaluation summary as the next sequence of the version.

    The per-version lock makes read-tail -> append atomic within this process
    (FastAPI runs this synchronous endpoint in one process's threadpool). The
    UNIQUE(version_id, sequence) constraint is the hard cross-process guard; a
    writer that loses a race against another process retries with the new tail
    instead of failing the request.
    """
    stored_results = json.dumps(results)
    with _evaluation_append_lock(version_id):
        for attempt in range(_EVALUATION_APPEND_MAX_ATTEMPTS):
            tail = conn.execute(
                "SELECT MAX(sequence) AS max_sequence "
                "FROM quality_rule_evaluations WHERE version_id = ?",
                (version_id,),
            ).fetchone()
            sequence = (tail["max_sequence"] or 0) + 1
            try:
                conn.execute(
                    "INSERT INTO quality_rule_evaluations ("
                    "version_id, sequence, row_count, violation_row_count, "
                    "results, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        version_id,
                        sequence,
                        row_count,
                        violation_row_count,
                        stored_results,
                        utc_now_iso(),
                    ),
                )
                return
            except sqlite3.IntegrityError:
                # Another process inserted the same sequence first. End the
                # current (pinned) transaction so the re-read establishes a new
                # snapshot that includes the competing commit, then retry.
                if attempt + 1 == _EVALUATION_APPEND_MAX_ATTEMPTS:
                    conn.rollback()
                    raise ConflictError(
                        "Too many concurrent evaluation writes; retry the request"
                    )
                conn.rollback()
                continue


def _evaluation_row_to_dict(
    row: sqlite3.Row, dataset_name: str, version_number: int
) -> dict:
    return {
        "sequence": row["sequence"],
        "dataset": dataset_name,
        "version": version_number,
        "row_count": row["row_count"],
        "violation_row_count": row["violation_row_count"],
        "results": json.loads(row["results"]),
        "created_at": row["created_at"],
    }


def _require_evaluation_path(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    *,
    body: bytes,
    query_keys: tuple[str, ...],
    endpoint: str,
) -> tuple[dict, sqlite3.Row]:
    """Resolve the path (404) before rejecting body bytes/query params (422).

    These read-only/parameterless endpoints share one rule: the shape checks
    run only once the dataset and version are known, so an unknown
    dataset/version stays a 404 (mirroring the parameterless audit-report
    endpoint).
    """
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)
    if body.strip():
        raise RequestInvalidError(
            f"The {endpoint} endpoint does not accept a request body"
        )
    if query_keys:
        raise RequestInvalidError(
            f"The {endpoint} endpoint does not accept query parameters"
        )
    return dataset, version_row


def list_quality_rule_evaluations(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> list[dict]:
    """Every recorded evaluation summary of one version, in occurrence order."""
    dataset, version_row = _require_evaluation_path(
        conn, dataset_name, version_number, body=body, query_keys=query_keys,
        endpoint="evaluation history",
    )
    rows = conn.execute(
        "SELECT * FROM quality_rule_evaluations WHERE version_id = ? "
        "ORDER BY sequence ASC",
        (version_row["id"],),
    ).fetchall()
    return [
        _evaluation_row_to_dict(row, dataset["name"], version_row["version"])
        for row in rows
    ]


def diff_quality_rule_evaluations(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> dict:
    """Read-only diff between the two most recent evaluations of one version.

    The latest recorded evaluation (``to``) is compared against its immediate
    predecessor (``from``) using exactly the results each of them recorded;
    the persisted rule definitions and submitted rows of the two evaluations
    are all that is consulted. Row positions are the 0-based indices each
    evaluation reported. A rule missing from one side (disabled or not yet
    created between the two evaluations) keeps a null side, and its row-level
    diff fields are null as well, so a missing side stays distinguishable from
    a present side with zero violations. With fewer than two recorded
    evaluations the result is explicitly empty (null sequences, empty lists),
    never an error. Nothing is written.
    """
    dataset, version_row = _require_evaluation_path(
        conn, dataset_name, version_number, body=body, query_keys=query_keys,
        endpoint="evaluation diff",
    )
    rows = conn.execute(
        "SELECT * FROM quality_rule_evaluations WHERE version_id = ? "
        "ORDER BY sequence DESC LIMIT 2",
        (version_row["id"],),
    ).fetchall()

    empty = {
        "dataset": dataset["name"],
        "version": version_row["version"],
        "from_sequence": None,
        "to_sequence": None,
        "added_violation_rows": [],
        "removed_violation_rows": [],
        "rules": [],
    }
    if len(rows) < 2:
        return empty

    to_row, from_row = rows[0], rows[1]
    before_results = {r["rule_id"]: r for r in json.loads(from_row["results"])}
    after_results = {r["rule_id"]: r for r in json.loads(to_row["results"])}

    rule_diffs: list[dict] = []
    for rule_id in sorted(set(before_results) | set(after_results)):
        before = before_results.get(rule_id)
        after = after_results.get(rule_id)
        if before is not None and after is not None:
            before_set = set(before["violations"])
            after_set = set(after["violations"])
            added: list[int] | None = sorted(after_set - before_set)
            removed: list[int] | None = sorted(before_set - after_set)
            delta: int | None = len(after["violations"]) - len(before["violations"])
        else:
            added = removed = delta = None
        rule_diffs.append(
            {
                "rule_id": rule_id,
                "name": (after if after is not None else before)["name"],
                "before": (
                    None
                    if before is None
                    else {
                        "violation_count": len(before["violations"]),
                        "violations": before["violations"],
                    }
                ),
                "after": (
                    None
                    if after is None
                    else {
                        "violation_count": len(after["violations"]),
                        "violations": after["violations"],
                    }
                ),
                "added_violations": added,
                "removed_violations": removed,
                "violation_count_delta": delta,
            }
        )

    before_rows = {
        index for result in before_results.values() for index in result["violations"]
    }
    after_rows = {
        index for result in after_results.values() for index in result["violations"]
    }

    return {
        "dataset": dataset["name"],
        "version": version_row["version"],
        "from_sequence": from_row["sequence"],
        "to_sequence": to_row["sequence"],
        "added_violation_rows": sorted(after_rows - before_rows),
        "removed_violation_rows": sorted(before_rows - after_rows),
        "rules": rule_diffs,
    }


# --------------------------------------------------------------------------- #
# Quality anomaly detection over the evaluation history
# --------------------------------------------------------------------------- #


QUALITY_ANOMALY_KINDS = ("row_limit", "rule_limit", "trend")


def _anomaly_config_row_to_dict(
    row: sqlite3.Row, dataset_name: str, version_number: int
) -> dict:
    return {
        "id": row["id"],
        "dataset": dataset_name,
        "version": version_number,
        "consecutive_worsening_steps": row["consecutive_worsening_steps"],
        "violation_row_limit": row["violation_row_limit"],
        "rule_violation_limit": row["rule_violation_limit"],
        "created_at": row["created_at"],
    }


def register_anomaly_detection_config(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    *,
    consecutive_worsening_steps: int,
    violation_row_limit: int,
    rule_violation_limit: int,
) -> dict:
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    try:
        cursor = conn.execute(
            "INSERT INTO quality_anomaly_detection_configs ("
            "version_id, consecutive_worsening_steps, violation_row_limit, "
            "rule_violation_limit, created_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                version_row["id"],
                consecutive_worsening_steps,
                violation_row_limit,
                rule_violation_limit,
                utc_now_iso(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(
            f"An anomaly detection config already exists for version "
            f"{version_number} of dataset '{dataset_name}'"
        ) from exc

    row = conn.execute(
        "SELECT * FROM quality_anomaly_detection_configs WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _anomaly_config_row_to_dict(row, dataset["name"], version_row["version"])


def get_anomaly_detection_config(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> dict:
    dataset, version_row = _require_evaluation_path(
        conn,
        dataset_name,
        version_number,
        body=body,
        query_keys=query_keys,
        endpoint="anomaly detection config",
    )
    row = conn.execute(
        "SELECT * FROM quality_anomaly_detection_configs WHERE version_id = ?",
        (version_row["id"],),
    ).fetchone()
    if row is None:
        raise NotFoundError(
            f"No anomaly detection config exists for version "
            f"{version_number} of dataset '{dataset_name}'"
        )
    return _anomaly_config_row_to_dict(row, dataset["name"], version_row["version"])


def _anomaly_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "sequence": row["sequence"],
        "rule_id": row["rule_id"],
        "violation_count": row["violation_count"],
        "created_at": row["created_at"],
    }


# Serializes read-existing -> insert within this process so concurrent scans
# never insert the same anomaly twice (mirrors the evaluation append lock).
# The UNIQUE index on (version_id, kind, sequence, rule) is the hard
# cross-process guard; a scan that loses such a race re-reads and retries.
_anomaly_scan_locks: dict[int, threading.Lock] = {}
_anomaly_scan_locks_guard = threading.Lock()

# Bound on re-scan-and-retry attempts when a scan in another process commits
# between this scan's read of the existing records and its inserts.
_ANOMALY_SCAN_MAX_ATTEMPTS = 100


def _anomaly_scan_lock(version_id: int) -> threading.Lock:
    with _anomaly_scan_locks_guard:
        lock = _anomaly_scan_locks.get(version_id)
        if lock is None:
            lock = threading.Lock()
            _anomaly_scan_locks[version_id] = lock
        return lock


def _anomaly_candidates(
    history_rows: list[sqlite3.Row], config_row: sqlite3.Row
) -> list[tuple[str, int, int | None, int]]:
    """All anomalies of one scan, in deterministic insertion order.

    Row-limit and rule-limit records are emitted per evaluation in history
    sequence order (rule-limit records ordered by rule id within one
    evaluation); the trend record, which always points at the final
    evaluation, comes last.
    """
    candidates: list[tuple[str, int, int | None, int]] = []
    row_limit = config_row["violation_row_limit"]
    rule_limit = config_row["rule_violation_limit"]
    for history_row in history_rows:
        sequence = history_row["sequence"]
        violation_row_count = history_row["violation_row_count"]
        if violation_row_count > row_limit:
            candidates.append(("row_limit", sequence, None, violation_row_count))
        results = json.loads(history_row["results"])
        for result in sorted(results, key=lambda item: item["rule_id"]):
            violation_count = len(result["violations"])
            if violation_count > rule_limit:
                candidates.append(
                    ("rule_limit", sequence, result["rule_id"], violation_count)
                )

    # Trend: the violation row counts of adjacent evaluations must strictly
    # increase step by step; when the consecutive increase count at the tail
    # of the history reaches the configured step count, the final evaluation
    # of the increasing sequence is anomalous. A shorter run or any decline
    # (or plateau) at the tail produces no trend record.
    counts = [row["violation_row_count"] for row in history_rows]
    trailing_increases = 0
    for previous, current in zip(counts, counts[1:]):
        trailing_increases = trailing_increases + 1 if current > previous else 0
    if trailing_increases >= config_row["consecutive_worsening_steps"]:
        candidates.append(("trend", history_rows[-1]["sequence"], None, counts[-1]))
    return candidates


def _scan_anomalies_once(conn: sqlite3.Connection, version_id: int) -> list[dict]:
    config_row = conn.execute(
        "SELECT * FROM quality_anomaly_detection_configs WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    if config_row is None:
        raise ConflictError(
            "No anomaly detection config is registered for this version; "
            "register one before scanning"
        )

    history_rows = conn.execute(
        "SELECT * FROM quality_rule_evaluations WHERE version_id = ? "
        "ORDER BY sequence ASC",
        (version_id,),
    ).fetchall()
    if not history_rows:
        return []

    existing = {
        (
            row["kind"],
            row["sequence"],
            row["rule_id"] if row["rule_id"] is not None else 0,
        )
        for row in conn.execute(
            "SELECT kind, sequence, rule_id FROM quality_anomaly_records "
            "WHERE version_id = ?",
            (version_id,),
        ).fetchall()
    }

    inserted_ids: list[int] = []
    for kind, sequence, rule_id, violation_count in _anomaly_candidates(
        history_rows, config_row
    ):
        key = (kind, sequence, rule_id if rule_id is not None else 0)
        if key in existing:
            continue
        cursor = conn.execute(
            "INSERT INTO quality_anomaly_records ("
            "version_id, kind, sequence, rule_id, violation_count, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (version_id, kind, sequence, rule_id, violation_count, utc_now_iso()),
        )
        existing.add(key)
        inserted_ids.append(cursor.lastrowid)

    if not inserted_ids:
        return []
    placeholders = ", ".join("?" for _ in inserted_ids)
    rows = conn.execute(
        f"SELECT * FROM quality_anomaly_records WHERE id IN ({placeholders}) "
        "ORDER BY id ASC",
        inserted_ids,
    ).fetchall()
    return [_anomaly_row_to_dict(row) for row in rows]


def scan_quality_anomalies(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> list[dict]:
    """Run one anomaly detection pass over the persisted evaluation history.

    Only the persisted evaluation summaries and the version's registered
    config are consulted; rule definitions are never written. A record already
    stored (same kind, history sequence and rule id) is never duplicated, so a
    repeated scan only persists — and returns — the anomalies that newly
    appear. The response holds exactly the records this scan inserted, ordered
    by id. Scanning without a registered config is a 409; an empty history is
    a successful scan that writes nothing.
    """
    dataset, version_row = _require_evaluation_path(
        conn,
        dataset_name,
        version_number,
        body=body,
        query_keys=query_keys,
        endpoint="anomaly scan",
    )
    version_id = version_row["id"]
    with _anomaly_scan_lock(version_id):
        for attempt in range(_ANOMALY_SCAN_MAX_ATTEMPTS):
            try:
                return _scan_anomalies_once(conn, version_id)
            except sqlite3.IntegrityError:
                # Another process committed records between this scan's read
                # of the existing records and its inserts. End the current
                # (pinned) transaction so the re-read sees that commit, then
                # only insert what is still missing (mirrors the evaluation
                # append retry).
                if attempt + 1 == _ANOMALY_SCAN_MAX_ATTEMPTS:
                    conn.rollback()
                    raise ConflictError(
                        "Too many concurrent anomaly scans; retry the request"
                    )
                conn.rollback()
                continue


def list_quality_anomalies(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> list[dict]:
    """Every anomaly record of one version, ordered by id ascending."""
    dataset, version_row = _require_evaluation_path(
        conn,
        dataset_name,
        version_number,
        body=body,
        query_keys=query_keys,
        endpoint="anomaly records",
    )
    rows = conn.execute(
        "SELECT * FROM quality_anomaly_records WHERE version_id = ? "
        "ORDER BY id ASC",
        (version_row["id"],),
    ).fetchall()
    return [_anomaly_row_to_dict(row) for row in rows]


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
        "SELECT id, field, masking, allowed_roles FROM privacy_policies "
        "WHERE version_id = ? AND enabled = 1 ORDER BY id",
        (version_row["id"],),
    ).fetchall()

    policies: dict[str, tuple[int, str, set[str]]] = {
        row["field"]: (row["id"], row["masking"], set(json.loads(row["allowed_roles"])))
        for row in policy_rows
    }

    masked_rows: list[dict[str, Any]] = []
    # One audit hit per value actually masked by this request, in row order
    # (the fields of one row in policy id order): the same field masked in
    # several rows hits once per row, and different fields hit independently.
    # A null value, an allowed role, an uncovered field, a field missing from
    # the row or a disabled policy never hits.
    hits: list[tuple[str, int, str]] = []
    for row in rows:
        masked_row = dict(row)
        for field, (policy_id, masking, allowed_roles) in policies.items():
            if field in masked_row and clean_role not in allowed_roles:
                value = masked_row[field]
                masked_row[field] = _mask_value(value, masking)
                if value is not None:
                    hits.append((field, policy_id, masking))
        masked_rows.append(masked_row)

    # Every successful view leaves exactly one access record, whether or not
    # any value was masked (an empty row set or a view that masked nothing is
    # traced too). The per-value masking-hit records and the single access
    # record are appended together and share the request transaction: a
    # rejected or failed view leaves neither kind of trace behind, and a
    # committed view never loses either. A view never fails because of the
    # append itself — cross-process contention is retried and, if the retries
    # are exhausted, resolved by a serialized fallback.
    _record_privacy_view_trail(
        conn,
        version_row["id"],
        clean_role,
        len(rows),
        hits,
    )

    return {
        "dataset": dataset["name"],
        "version": version_row["version"],
        "rows": masked_rows,
    }


# --------------------------------------------------------------------------- #
# Privacy view audit records (per-value masking-hit log) and access records
# (one-per-view access trail)
# --------------------------------------------------------------------------- #


# Serializes read-tail -> append within this process so concurrent views never
# compute the same sequence (mirrors the evaluation append lock). One lock per
# version serializes the whole view trail — the per-value hit batch and the
# single access record of the request. The UNIQUE(version_id, sequence)
# constraints on both tables are the hard guards against writers in other
# processes; such a collision is resolved with a bounded re-read-and-retry.
_privacy_view_audit_locks: dict[int, threading.Lock] = {}
_privacy_view_audit_locks_guard = threading.Lock()

# Bound on re-read-and-retry attempts when a writer in another process wins the
# race for the same (version_id, sequence).
_PRIVACY_VIEW_AUDIT_MAX_ATTEMPTS = 100


def _privacy_view_audit_lock(version_id: int) -> threading.Lock:
    with _privacy_view_audit_locks_guard:
        lock = _privacy_view_audit_locks.get(version_id)
        if lock is None:
            lock = threading.Lock()
            _privacy_view_audit_locks[version_id] = lock
        return lock


def _append_privacy_view_audit_batch(
    conn: sqlite3.Connection,
    version_id: int,
    role: str,
    hits: list[tuple[str, int, str]],
) -> None:
    """Insert all hit records of one view as the next sequences of the version.

    All records of one view request share a single write timestamp.

    Sequences come from the version's high-water counter rather than
    ``MAX(sequence)``: confirmed cleanup requests delete records (so the max
    would move backwards), but the counter survives those deletions, keeping
    the run continuous with no reused sequence. A database created before the
    counter existed seeds it from the current maximum once.
    """
    # Connections already seed every version's counter, so this only covers a
    # version whose row somehow does not exist yet. INSERT OR IGNORE avoids
    # the parser ambiguity between ON CONFLICT and a join ON after a SELECT.
    conn.execute(
        "INSERT OR IGNORE INTO privacy_view_audit_sequences "
        "(version_id, next_sequence) "
        "SELECT ?, COALESCE(("
        "SELECT MAX(sequence) FROM privacy_view_audit_records "
        "WHERE version_id = ?"
        "), 0) + 1",
        (version_id, version_id),
    )
    tail = conn.execute(
        "SELECT next_sequence FROM privacy_view_audit_sequences "
        "WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    sequence = tail["next_sequence"]
    # All records of one view request share a single write timestamp.
    created_at = utc_now_iso()
    for field, policy_id, masking in hits:
        conn.execute(
            "INSERT INTO privacy_view_audit_records ("
            "version_id, sequence, field, policy_id, role, masking, "
            "created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                version_id,
                sequence,
                field,
                policy_id,
                role,
                masking,
                created_at,
            ),
        )
        sequence += 1
    conn.execute(
        "UPDATE privacy_view_audit_sequences SET next_sequence = ? "
        "WHERE version_id = ?",
        (sequence, version_id),
    )


def _append_privacy_view_access_record(
    conn: sqlite3.Connection,
    version_id: int,
    role: str,
    row_count: int,
    masked_count: int,
) -> None:
    """Insert the single access record of one view as the next sequence."""
    tail = conn.execute(
        "SELECT MAX(sequence) AS max_sequence "
        "FROM privacy_view_access_records WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    sequence = (tail["max_sequence"] or 0) + 1
    conn.execute(
        "INSERT INTO privacy_view_access_records ("
        "version_id, sequence, role, row_count, masked_count, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        (version_id, sequence, role, row_count, masked_count, utc_now_iso()),
    )


def _append_privacy_view_trail(
    conn: sqlite3.Connection,
    version_id: int,
    role: str,
    row_count: int,
    hits: list[tuple[str, int, str]],
) -> None:
    """Append one view's whole trail: its hit batch plus its access record.

    The access record's ``masked_count`` is the number of hit records the same
    view writes, so the two logs always cross-check. Both appends ride the
    same transaction, so a collision rolls both back together and a retry
    re-appends both — the trail is never half-written.
    """
    if hits:
        _append_privacy_view_audit_batch(conn, version_id, role, hits)
    _append_privacy_view_access_record(
        conn, version_id, role, row_count, len(hits)
    )


def _record_privacy_view_trail_serialized(
    version_id: int,
    role: str,
    row_count: int,
    hits: list[tuple[str, int, str]],
) -> None:
    """Fallback append on a dedicated connection serialized by BEGIN IMMEDIATE.

    Used when the optimistic append on the request connection keeps losing
    the cross-process race for the tail sequence. BEGIN IMMEDIATE takes the
    database write lock before the tail is read, so a competing writer can
    only make this wait (bounded by the connection timeout), never collide:
    the view request returns normally and its records are not lost.
    """
    with db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _append_privacy_view_trail(conn, version_id, role, row_count, hits)


def _record_privacy_view_trail(
    conn: sqlite3.Connection,
    version_id: int,
    role: str,
    row_count: int,
    hits: list[tuple[str, int, str]],
) -> None:
    """Append one view's hit batch and access record as the next sequences.

    The per-version lock makes read-tail -> append atomic within this process
    (FastAPI runs this synchronous endpoint in one process's threadpool). The
    UNIQUE(version_id, sequence) constraints are the hard cross-process guard;
    a writer that loses a race against another process rolls the partial trail
    back and retries with the new tail, so a lost race never leaves half a
    trail behind. If the bounded retries are ever exhausted, the append falls
    back to a dedicated connection serialized by BEGIN IMMEDIATE, so the view
    request still succeeds and no record is lost.
    """
    with _privacy_view_audit_lock(version_id):
        for attempt in range(_PRIVACY_VIEW_AUDIT_MAX_ATTEMPTS):
            try:
                _append_privacy_view_trail(conn, version_id, role, row_count, hits)
                return
            except sqlite3.IntegrityError:
                # Another process inserted the same sequence first. End the
                # current (pinned) transaction so the re-read establishes a
                # new snapshot that includes the competing commit, then retry
                # (mirrors the evaluation append retry).
                conn.rollback()
                if attempt + 1 == _PRIVACY_VIEW_AUDIT_MAX_ATTEMPTS:
                    break
        # The bounded retries are exhausted: the view must still return its
        # result and the records must not be dropped, so append them through
        # a dedicated connection serialized by BEGIN IMMEDIATE (still under
        # the in-process lock, so in-process writers stay ordered too).
        _record_privacy_view_trail_serialized(version_id, role, row_count, hits)


def _privacy_view_audit_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "sequence": row["sequence"],
        "field": row["field"],
        "policy_id": row["policy_id"],
        "role": row["role"],
        "masking": row["masking"],
        "created_at": row["created_at"],
    }


def list_privacy_view_audit_records(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> list[dict]:
    """Every masking-hit record of one version, in write (sequence) order.

    Read-only and parameterless: the path dataset/version resolves first
    (404); a non-empty request body or any query parameter is a 422 checked
    afterwards, mirroring the evaluation history endpoint. Nothing is written.
    """
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    if body.strip():
        raise RequestInvalidError(
            "The privacy view audit records endpoint does not accept a "
            "request body"
        )
    if query_keys:
        raise RequestInvalidError(
            "The privacy view audit records endpoint does not accept query "
            "parameters"
        )

    rows = conn.execute(
        "SELECT * FROM privacy_view_audit_records WHERE version_id = ? "
        "ORDER BY sequence ASC",
        (version_row["id"],),
    ).fetchall()
    return [_privacy_view_audit_row_to_dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# Privacy view access records (one-per-view access trail)
# --------------------------------------------------------------------------- #


def _privacy_view_access_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "sequence": row["sequence"],
        "role": row["role"],
        "row_count": row["row_count"],
        "masked_count": row["masked_count"],
        "created_at": row["created_at"],
    }


def list_privacy_view_access_records(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> list[dict]:
    """Every access record of one version, in write (sequence) order.

    Exactly one record is written per successful view request, independent of
    how many values it masked; ordering by ``sequence`` never relies on the
    database's natural row order, and the sequence is continuous within the
    version. Read-only and parameterless: the path dataset/version resolves
    first (404); a non-empty request body or any query parameter is a 422
    checked afterwards, with the same 404-before-422 precedence as the
    masking-hit record list. A version without access records yields an empty
    list; nothing is written.
    """
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    if body.strip():
        raise RequestInvalidError(
            "The privacy view access records endpoint does not accept a "
            "request body"
        )
    if query_keys:
        raise RequestInvalidError(
            "The privacy view access records endpoint does not accept query "
            "parameters"
        )

    rows = conn.execute(
        "SELECT * FROM privacy_view_access_records WHERE version_id = ? "
        "ORDER BY sequence ASC",
        (version_row["id"],),
    ).fetchall()
    return [_privacy_view_access_row_to_dict(row) for row in rows]


_PRIVACY_VIEW_AUDIT_SEARCH_PARAMETERS: frozenset[str] = frozenset(
    {"role", "field", "start", "end"}
)


def _parse_audit_interval_timestamp(raw: str, param: str) -> datetime:
    """Parse one bound of the search write-time interval.

    The bound must be an ISO-8601 date-time carrying a timezone designator
    (offset or 'Z'); anything else (including an empty value) is a 422.
    """
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise RequestInvalidError(
            f"Query parameter '{param}' must be an ISO-8601 date-time"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RequestInvalidError(
            f"Query parameter '{param}' must include a timezone"
        )
    return parsed


def search_privacy_view_audit_records(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    *,
    role: str | None = None,
    field: str | None = None,
    start: str | None = None,
    end: str | None = None,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> list[dict]:
    """Masking-hit records of a version matching optional read-only filters.

    The filters are all optional and combine with AND: ``role`` and ``field``
    match the stored values exactly apart from letter case (no trimming, no
    substring matching); ``start``/``end`` are timezone-aware ISO-8601 bounds
    of a closed interval on the record write time, either one being omissible.
    Matched records keep the full-list field shape and sequence-ascending
    order. No match yields an empty list; an interval whose start is later
    than its end is a 422.

    Read-only: the path dataset/version resolves first (404); a non-empty
    request body, an unknown query parameter, an unparseable or timezone-less
    bound, or a reversed interval is a 422 checked afterwards, mirroring the
    full record list. Nothing is written.
    """
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    if body.strip():
        raise RequestInvalidError(
            "The privacy view audit records search endpoint does not accept "
            "a request body"
        )
    unknown_keys = sorted(set(query_keys) - _PRIVACY_VIEW_AUDIT_SEARCH_PARAMETERS)
    if unknown_keys:
        raise RequestInvalidError(
            "Unknown query parameter(s): " + ", ".join(unknown_keys)
        )

    start_at = (
        _parse_audit_interval_timestamp(start, "start")
        if start is not None
        else None
    )
    end_at = (
        _parse_audit_interval_timestamp(end, "end") if end is not None else None
    )
    if start_at is not None and end_at is not None and start_at > end_at:
        raise RequestInvalidError(
            "Query parameter 'start' must not be later than 'end'"
        )

    rows = conn.execute(
        "SELECT * FROM privacy_view_audit_records WHERE version_id = ? "
        "ORDER BY sequence ASC",
        (version_row["id"],),
    ).fetchall()

    role_match = role.lower() if role is not None else None
    field_match = field.lower() if field is not None else None
    results: list[dict] = []
    for row in rows:
        if role_match is not None and row["role"].lower() != role_match:
            continue
        if field_match is not None and row["field"].lower() != field_match:
            continue
        if start_at is not None or end_at is not None:
            written_at = datetime.fromisoformat(row["created_at"])
            if start_at is not None and written_at < start_at:
                continue
            if end_at is not None and written_at > end_at:
                continue
        results.append(_privacy_view_audit_row_to_dict(row))
    return results


def summarize_privacy_view_audit_records(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> dict:
    """Read-only compliance summary of the masking-hit records of a version.

    The records are grouped by ``(field, policy_id, role, masking)``; each
    group reports its record count (one record is one hit, never row- or
    field-weighted) and the earliest/latest ``created_at`` among its records.
    Groups sort by field, then policy id, role and masking, all ascending. The
    summary is recomputed from the persisted records on every call: it caches
    nothing and writes nothing.

    Like the record list, the path dataset/version resolves first (404); a
    non-empty request body or any query parameter is a 422 checked afterwards.
    A version without records yields an empty group list.
    """
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    if body.strip():
        raise RequestInvalidError(
            "The privacy view audit summary endpoint does not accept a "
            "request body"
        )
    if query_keys:
        raise RequestInvalidError(
            "The privacy view audit summary endpoint does not accept query "
            "parameters"
        )

    rows = conn.execute(
        "SELECT field, policy_id, role, masking, "
        "COUNT(*) AS hit_count, "
        "MIN(created_at) AS first_hit_at, "
        "MAX(created_at) AS last_hit_at "
        "FROM privacy_view_audit_records WHERE version_id = ? "
        "GROUP BY field, policy_id, role, masking "
        "ORDER BY field ASC, policy_id ASC, role ASC, masking ASC",
        (version_row["id"],),
    ).fetchall()
    groups = [
        {
            "field": row["field"],
            "policy_id": row["policy_id"],
            "role": row["role"],
            "masking": row["masking"],
            "hit_count": row["hit_count"],
            "first_hit_at": row["first_hit_at"],
            "last_hit_at": row["last_hit_at"],
        }
        for row in rows
    ]
    return {
        "dataset": dataset["name"],
        "version": version_row["version"],
        "groups": groups,
    }


def diff_privacy_view_audit_records(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> dict:
    """Read-only day-over-day diff of the masking-hit records of a version.

    Records are bucketed by the UTC calendar day of their ``created_at`` write
    time; the two most recent days with records are compared, the earlier day
    being the baseline (``from_period``) and the later one the target
    (``to_period``). Each side is grouped by ``(field, policy_id, role,
    masking)``; a group reports its record count (one record is one hit) and
    the earliest/latest ``created_at`` of its records of that day. A group
    present on both days is ``changed``; one only on the target day is
    ``added`` and one only on the baseline day is ``removed``. The missing
    side of a one-sided group is null (never omitted) and counts as zero in
    ``hit_count_delta`` (target minus baseline). Groups sort by field, policy
    id, role and masking, all ascending, never relying on database order.

    With fewer than two calendar days of records the comparison is explicitly
    empty (null periods, empty group list), never an error. The diff is
    recomputed from the persisted records on every call: it caches nothing,
    writes nothing and never touches the hit records.

    Like the record list, the path dataset/version resolves first (404); a
    non-empty request body or any query parameter is a 422 checked afterwards.
    """
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    if body.strip():
        raise RequestInvalidError(
            "The privacy view audit diff endpoint does not accept a "
            "request body"
        )
    if query_keys:
        raise RequestInvalidError(
            "The privacy view audit diff endpoint does not accept query "
            "parameters"
        )

    day_rows = conn.execute(
        "SELECT DISTINCT date(created_at) AS day "
        "FROM privacy_view_audit_records WHERE version_id = ? "
        "ORDER BY day DESC LIMIT 2",
        (version_row["id"],),
    ).fetchall()

    if len(day_rows) < 2:
        return {"from_period": None, "to_period": None, "groups": []}

    to_period = day_rows[0]["day"]
    from_period = day_rows[1]["day"]

    rows = conn.execute(
        "SELECT field, policy_id, role, masking, date(created_at) AS day, "
        "COUNT(*) AS hit_count, "
        "MIN(created_at) AS first_hit_at, "
        "MAX(created_at) AS last_hit_at "
        "FROM privacy_view_audit_records "
        "WHERE version_id = ? AND date(created_at) IN (?, ?) "
        "GROUP BY field, policy_id, role, masking, day",
        (version_row["id"], from_period, to_period),
    ).fetchall()

    def side(row: sqlite3.Row) -> dict:
        return {
            "hit_count": row["hit_count"],
            "first_hit_at": row["first_hit_at"],
            "last_hit_at": row["last_hit_at"],
        }

    by_key: dict[tuple, dict[str, dict | None]] = {}
    for row in rows:
        key = (row["field"], row["policy_id"], row["role"], row["masking"])
        entry = by_key.setdefault(key, {"before": None, "after": None})
        entry["before" if row["day"] == from_period else "after"] = side(row)

    groups: list[dict] = []
    for key in sorted(by_key):
        field, policy_id, role, masking = key
        before = by_key[key]["before"]
        after = by_key[key]["after"]
        if before is None:
            kind = "added"
        elif after is None:
            kind = "removed"
        else:
            kind = "changed"
        groups.append(
            {
                "field": field,
                "policy_id": policy_id,
                "role": role,
                "masking": masking,
                "kind": kind,
                "before": before,
                "after": after,
                "hit_count_delta": (after or {"hit_count": 0})["hit_count"]
                - (before or {"hit_count": 0})["hit_count"],
            }
        )

    return {"from_period": from_period, "to_period": to_period, "groups": groups}


def reconcile_privacy_view_audit_records(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> dict:
    """Read-only per-day reconciliation of access records against hit records.

    Both logs are bucketed by the UTC calendar day of their ``created_at``
    write time. Each day with records on either side reports ``view_count``
    (access records written that day), ``masked_count`` (the masked-value
    counts of those access records summed) and ``hit_count`` (masking-hit
    records written that day); a side without records on that day counts as
    zero. ``consistent`` is true exactly when ``masked_count`` equals
    ``hit_count`` — the access log's masked-value total cross-checks the hit
    log. Days sort ascending by date, never relying on database order; days
    without any record on either side do not appear. ``totals`` sums the
    same three counts over the whole version, so it always equals the
    per-day entries added together (all zero when there are no records).

    The reconciliation is recomputed from the persisted records on every
    call: it caches nothing, writes nothing and never touches either log.
    Like the record list, the path dataset/version resolves first (404); a
    non-empty request body or any query parameter is a 422 checked
    afterwards.
    """
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    if body.strip():
        raise RequestInvalidError(
            "The privacy view audit reconcile endpoint does not accept a "
            "request body"
        )
    if query_keys:
        raise RequestInvalidError(
            "The privacy view audit reconcile endpoint does not accept query "
            "parameters"
        )

    access_rows = conn.execute(
        "SELECT date(created_at) AS day, COUNT(*) AS view_count, "
        "SUM(masked_count) AS masked_count "
        "FROM privacy_view_access_records WHERE version_id = ? "
        "GROUP BY day",
        (version_row["id"],),
    ).fetchall()
    hit_rows = conn.execute(
        "SELECT date(created_at) AS day, COUNT(*) AS hit_count "
        "FROM privacy_view_audit_records WHERE version_id = ? "
        "GROUP BY day",
        (version_row["id"],),
    ).fetchall()

    by_day: dict[str, dict[str, int]] = {}
    for row in access_rows:
        entry = by_day.setdefault(
            row["day"], {"view_count": 0, "masked_count": 0, "hit_count": 0}
        )
        entry["view_count"] = row["view_count"]
        entry["masked_count"] = row["masked_count"]
    for row in hit_rows:
        entry = by_day.setdefault(
            row["day"], {"view_count": 0, "masked_count": 0, "hit_count": 0}
        )
        entry["hit_count"] = row["hit_count"]

    days = [
        {
            "day": day,
            "view_count": entry["view_count"],
            "masked_count": entry["masked_count"],
            "hit_count": entry["hit_count"],
            "consistent": entry["masked_count"] == entry["hit_count"],
        }
        for day, entry in sorted(by_day.items())
    ]
    totals = {
        "view_count": sum(entry["view_count"] for entry in days),
        "masked_count": sum(entry["masked_count"] for entry in days),
        "hit_count": sum(entry["hit_count"] for entry in days),
    }
    return {
        "dataset": dataset["name"],
        "version": version_row["version"],
        "days": days,
        "totals": totals,
    }


def trend_privacy_view_audit_records(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> dict:
    """Read-only per-policy hit trend of one version's masking-hit records.

    Records are aggregated by the privacy policy that produced the hit: hits
    of the same policy merge into one count no matter which role triggered
    them, and a policy that has since been disabled still has its historical
    hits counted. Each policy row carries the policy's registered field name,
    classification and masking (the current registration values) plus its
    total hit count and a ``days`` list.

    ``days`` lists only the UTC calendar days on which the policy has at least
    one hit, sorted ascending; days without a hit are neither zero-filled nor
    emitted. A write time carrying a non-zero UTC offset is bucketed by its
    UTC calendar day (the bucketing is done in Python rather than relying on
    SQLite's ``date()``, which does not normalize offsets). Each day reports
    its record count (one record counts once — never row- or field-weighted),
    the difference against the previously listed day (null on the first day)
    and the ``up``/``down``/``flat``/``none`` direction (``none`` marks the
    first day). Policies sort by policy id and their days sort by date, never
    relying on database natural order.

    ``totals`` reports the version-wide total (always the sum of the row
    totals), the number of policies with at least one hit and the number of
    distinct hit days over the whole version (the per-policy day union, not
    the per-row days added together). A version without records yields an
    empty policy list and zero totals. The trend is recomputed from the
    persisted records on every call: it caches nothing, writes nothing and
    never touches the hit records.

    Like the record list, the path dataset/version resolves first (404); a
    non-empty request body or any query parameter is a 422 checked afterwards.
    """
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    if body.strip():
        raise RequestInvalidError(
            "The privacy view audit trend endpoint does not accept a "
            "request body"
        )
    if query_keys:
        raise RequestInvalidError(
            "The privacy view audit trend endpoint does not accept query "
            "parameters"
        )

    rows = conn.execute(
        "SELECT p.id AS policy_id, p.field AS field, "
        "p.classification AS classification, p.masking AS masking, "
        "a.created_at AS created_at "
        "FROM privacy_view_audit_records AS a "
        "JOIN privacy_policies AS p "
        "ON p.id = a.policy_id AND p.version_id = a.version_id "
        "WHERE a.version_id = ? "
        "ORDER BY p.id ASC, a.sequence ASC",
        (version_row["id"],),
    ).fetchall()

    # policy_id -> registration fields and {utc_day: hit_count}
    grouped: dict[int, dict[str, Any]] = {}
    all_days: set[str] = set()
    for row in rows:
        written_at = datetime.fromisoformat(row["created_at"])
        if written_at.tzinfo is None:
            written_at = written_at.replace(tzinfo=timezone.utc)
        day = written_at.astimezone(timezone.utc).date().isoformat()
        entry = grouped.setdefault(
            row["policy_id"],
            {
                "field": row["field"],
                "classification": row["classification"],
                "masking": row["masking"],
                "day_counts": {},
            },
        )
        entry["day_counts"][day] = entry["day_counts"].get(day, 0) + 1
        all_days.add(day)

    policies: list[dict] = []
    for policy_id in sorted(grouped):
        entry = grouped[policy_id]
        days: list[dict] = []
        previous: int | None = None
        for day in sorted(entry["day_counts"]):
            hit_count = entry["day_counts"][day]
            if previous is None:
                delta: int | None = None
                trend = "none"
            else:
                delta = hit_count - previous
                if hit_count > previous:
                    trend = "up"
                elif hit_count < previous:
                    trend = "down"
                else:
                    trend = "flat"
            days.append(
                {
                    "day": day,
                    "hit_count": hit_count,
                    "hit_count_delta": delta,
                    "trend": trend,
                }
            )
            previous = hit_count
        policies.append(
            {
                "policy_id": policy_id,
                "field": entry["field"],
                "classification": entry["classification"],
                "masking": entry["masking"],
                "total_hits": sum(entry["day_counts"].values()),
                "days": days,
            }
        )

    return {
        "dataset": dataset["name"],
        "version": version_row["version"],
        "policies": policies,
        "totals": {
            "total_hits": sum(policy["total_hits"] for policy in policies),
            "policy_count": len(policies),
            "day_count": len(all_days),
        },
    }


# --------------------------------------------------------------------------- #
# Privacy view masking-hit cleanup requests (preview then two-stage confirm)
# --------------------------------------------------------------------------- #


# Fields accepted by a cleanup-request creation body; anything else is a 422.
_PRIVACY_VIEW_AUDIT_CLEANUP_FIELDS: frozenset[str] = frozenset(
    {"reason", "before"}
)


def _parse_cleanup_request_body(body: bytes) -> tuple[str, str, datetime]:
    """Validate and parse a raw cleanup-request creation body.

    The body must be a JSON object carrying exactly ``reason`` (a non-empty
    string after trimming whitespace) and ``before`` (an ISO-8601 date-time
    carrying a timezone). Returns the reason, the raw ``before`` string (echoed
    back verbatim) and the parsed cutoff. Every structural problem is a 422;
    the request row is written only afterwards.
    """
    if not body.strip():
        raise RequestInvalidError("A JSON request body is required")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RequestInvalidError("Request body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RequestInvalidError("Request body must be a JSON object")

    extra_keys = sorted(set(payload) - _PRIVACY_VIEW_AUDIT_CLEANUP_FIELDS)
    if extra_keys:
        raise RequestInvalidError(
            "Unknown field(s): " + ", ".join(extra_keys)
        )

    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise RequestInvalidError("'reason' must be a non-empty string")

    raw_before = payload.get("before")
    if not isinstance(raw_before, str):
        raise RequestInvalidError(
            "'before' must be an ISO-8601 date-time with a timezone"
        )
    try:
        before_at = datetime.fromisoformat(raw_before)
    except ValueError as exc:
        raise RequestInvalidError(
            "'before' must be an ISO-8601 date-time with a timezone"
        ) from exc
    if before_at.tzinfo is None or before_at.utcoffset() is None:
        raise RequestInvalidError("'before' must include a timezone")
    return reason, raw_before, before_at


def _reject_cleanup_query_parameters(query_keys: tuple[str, ...], *, what: str) -> None:
    if query_keys:
        raise RequestInvalidError(
            f"The privacy view audit record cleanup {what} endpoint does not "
            "accept query parameters"
        )


def _cleanup_preview_for_rows(rows: list[sqlite3.Row]) -> dict:
    """Build the preview block from the selected target rows."""
    if not rows:
        return {"hit_count": 0, "first_hit_at": None, "last_hit_at": None,
                "fields": []}
    # The rows arrive sequence-ascending; the earliest and latest are picked
    # by parsed write time, never relying on the database's natural order or
    # on text collation.
    earliest = latest = rows[0]
    for row in rows[1:]:
        if datetime.fromisoformat(row["created_at"]) < datetime.fromisoformat(
            earliest["created_at"]
        ):
            earliest = row
        if datetime.fromisoformat(row["created_at"]) > datetime.fromisoformat(
            latest["created_at"]
        ):
            latest = row
    return {
        "hit_count": len(rows),
        "first_hit_at": earliest["created_at"],
        "last_hit_at": latest["created_at"],
        "fields": sorted({row["field"] for row in rows}),
    }


def _cleanup_request_row_to_dict(row: sqlite3.Row) -> dict:
    result = {
        "id": row["id"],
        "reason": row["reason"],
        "before": row["before"],
        "status": row["status"],
        "created_at": row["created_at"],
        "preview": json.loads(row["preview"]),
    }
    if row["status"] == "confirmed":
        result["confirmed_at"] = row["confirmed_at"]
        result["deleted_count"] = row["deleted_count"]
    return result


def create_privacy_view_audit_cleanup_request(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    body: bytes,
    *,
    query_keys: tuple[str, ...] = (),
) -> dict:
    """Create a cleanup request and return its fixed preview.

    Only records whose hit write time is strictly earlier than ``before`` are
    selected; the selected set is frozen into
    ``privacy_view_audit_cleanup_targets`` at creation, so later writes never
    enter this request. Nothing is deleted or modified. At most one pending
    request may exist per version (409).
    """
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)
    reason, raw_before, before_at = _parse_cleanup_request_body(body)
    _reject_cleanup_query_parameters(query_keys, what="request")
    clean_reason = reason.strip()

    open_request = conn.execute(
        "SELECT id FROM privacy_view_audit_cleanup_requests "
        "WHERE version_id = ? AND status = 'pending'",
        (version_row["id"],),
    ).fetchone()
    if open_request is not None:
        raise ConflictError(
            f"Version {version_number} of dataset '{dataset_name}' already has "
            f"a pending masking-hit cleanup request ({open_request['id']})"
        )

    # Fix the target set at creation time: only records written strictly
    # before the cutoff participate, and later writes are out of scope even if
    # they would otherwise match.
    rows = conn.execute(
        "SELECT id, field, created_at FROM privacy_view_audit_records "
        "WHERE version_id = ? ORDER BY sequence ASC",
        (version_row["id"],),
    ).fetchall()
    target_rows = [
        row
        for row in rows
        if datetime.fromisoformat(row["created_at"]) < before_at
    ]
    preview = _cleanup_preview_for_rows(target_rows)

    try:
        cursor = conn.execute(
            "INSERT INTO privacy_view_audit_cleanup_requests ("
            "version_id, reason, before, status, preview, deleted_count, "
            "created_at"
            ") VALUES (?, ?, ?, 'pending', ?, NULL, ?)",
            (
                version_row["id"],
                clean_reason,
                raw_before,
                json.dumps(preview),
                utc_now_iso(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        # Lost a race against a concurrent pending-request insert.
        raise ConflictError(
            f"Version {version_number} of dataset '{dataset_name}' already has "
            "a pending masking-hit cleanup request"
        ) from exc
    request_id = cursor.lastrowid
    conn.executemany(
        "INSERT INTO privacy_view_audit_cleanup_targets "
        "(request_id, audit_record_id) VALUES (?, ?)",
        [(request_id, row["id"]) for row in target_rows],
    )
    stored = conn.execute(
        "SELECT * FROM privacy_view_audit_cleanup_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    return _cleanup_request_row_to_dict(stored)


def list_privacy_view_audit_cleanup_requests(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> list[dict]:
    """List a version's cleanup requests by request id ascending.

    Confirmed requests remain listed forever; requests survive process
    restarts. Read-only and parameterless, with the same 404-before-422
    precedence as the masking-hit record list.
    """
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    if body.strip():
        raise RequestInvalidError(
            "The privacy view audit record cleanup request list endpoint does "
            "not accept a request body"
        )
    if query_keys:
        raise RequestInvalidError(
            "The privacy view audit record cleanup request list endpoint does "
            "not accept query parameters"
        )

    rows = conn.execute(
        "SELECT * FROM privacy_view_audit_cleanup_requests "
        "WHERE version_id = ? ORDER BY id ASC",
        (version_row["id"],),
    ).fetchall()
    return [_cleanup_request_row_to_dict(row) for row in rows]


def _get_scoped_cleanup_request_row(
    conn: sqlite3.Connection, version_id: int, request_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM privacy_view_audit_cleanup_requests "
        "WHERE id = ? AND version_id = ?",
        (request_id, version_id),
    ).fetchone()


def confirm_privacy_view_audit_cleanup_request(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    request_id: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> dict:
    """Confirm a cleanup request, atomically deleting its frozen targets.

    The record deletion and the status change commit together: either both
    take effect or neither does. A request that is already confirmed is a 409
    and its records are never touched again.
    """
    # Take the write lock before any read so two concurrent confirms cannot
    # deadlock on a read-then-upgrade race: the loser blocks here until the
    # winner commits, then sees status 'confirmed' and fails with 409.
    conn.execute("BEGIN IMMEDIATE")

    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    request_row = _get_scoped_cleanup_request_row(
        conn, version_row["id"], request_id
    )
    if request_row is None:
        raise NotFoundError(
            f"Masking-hit cleanup request {request_id} does not exist for "
            f"version {version_number} of dataset '{dataset_name}'"
        )

    # Resource resolution (dataset/version/request) precedes request-content
    # validation, preserving the 404-before-422 precedence of the hit-record
    # reads.
    if body.strip():
        raise RequestInvalidError(
            "The privacy view audit record cleanup confirm endpoint does not "
            "accept a request body"
        )
    if query_keys:
        raise RequestInvalidError(
            "The privacy view audit record cleanup confirm endpoint does not "
            "accept query parameters"
        )

    if request_row["status"] != "pending":
        raise ConflictError(
            f"Masking-hit cleanup request {request_id} is "
            f"'{request_row['status']}'; only pending requests can be confirmed"
        )

    # The database-level immutability trigger allows exactly these rows: the
    # frozen target set of this request. Deleting and confirming ride one
    # transaction, so a crash or a lost race can never leave the records half
    # deleted.
    deleted_cursor = conn.execute(
        "DELETE FROM privacy_view_audit_records WHERE id IN ("
        "SELECT audit_record_id FROM privacy_view_audit_cleanup_targets "
        "WHERE request_id = ?"
        ")",
        (request_id,),
    )
    deleted_count = deleted_cursor.rowcount
    confirmed_at = utc_now_iso()
    updated_cursor = conn.execute(
        "UPDATE privacy_view_audit_cleanup_requests "
        "SET status = 'confirmed', confirmed_at = ?, deleted_count = ? "
        "WHERE id = ? AND status = 'pending'",
        (confirmed_at, deleted_count, request_id),
    )
    if updated_cursor.rowcount == 0:
        # Another process confirmed it first; roll the delete back and report
        # the conflict.
        raise ConflictError(
            f"Masking-hit cleanup request {request_id} is already confirmed"
        )

    updated = conn.execute(
        "SELECT * FROM privacy_view_audit_cleanup_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    return _cleanup_request_row_to_dict(updated)


# --------------------------------------------------------------------------- #
# Read-only cross-version privacy compliance export
# --------------------------------------------------------------------------- #


def export_dataset_privacy_compliance(
    conn: sqlite3.Connection,
    dataset_name: str,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> dict:
    """Read-only whole-dataset export of the privacy compliance state.

    One entry per schema version of the dataset, ordered by version number
    ascending. Each entry lists the version's registered privacy policies
    (ordered by policy id) and cleanup requests (ordered by request id, with
    pending and confirmed requests both retained), plus three counters
    computed from the records that survive any confirmed cleanup: the number
    of masking-hit records, the sum of the access records' masked-value
    counts, and the number of access records. ``totals`` sums every counter
    (the policy and cleanup-request counts included) over all versions; a
    dataset without versions yields an empty version list and all-zero
    totals, never an error.

    The export is recomputed on every read: it caches nothing and never
    writes, modifies or deletes a policy, hit record, access record or
    cleanup request. Like the hit-record reads, the dataset resolves first
    (404); a non-empty request body or any query parameter is a 422 checked
    afterwards.
    """
    dataset = require_dataset(conn, dataset_name)

    if body.strip():
        raise RequestInvalidError(
            "The privacy compliance export endpoint does not accept a "
            "request body"
        )
    if query_keys:
        raise RequestInvalidError(
            "The privacy compliance export endpoint does not accept query "
            "parameters"
        )

    version_rows = conn.execute(
        "SELECT id, version FROM schema_versions "
        "WHERE dataset_id = ? ORDER BY version ASC",
        (dataset["id"],),
    ).fetchall()

    versions: list[dict] = []
    for version_row in version_rows:
        version_id = version_row["id"]

        policy_rows = conn.execute(
            "SELECT id, field, classification, masking, allowed_roles, enabled "
            "FROM privacy_policies WHERE version_id = ? ORDER BY id ASC",
            (version_id,),
        ).fetchall()
        policies = [
            {
                "id": row["id"],
                "field": row["field"],
                "classification": row["classification"],
                "masking": row["masking"],
                "allowed_roles": json.loads(row["allowed_roles"]),
                "enabled": bool(row["enabled"]),
            }
            for row in policy_rows
        ]

        # All three counters read the current tables, so a confirmed cleanup
        # is reflected exactly as in the per-version hit-record reads.
        hit_count = conn.execute(
            "SELECT COUNT(*) AS hit_count FROM privacy_view_audit_records "
            "WHERE version_id = ?",
            (version_id,),
        ).fetchone()["hit_count"]
        access_totals = conn.execute(
            "SELECT COUNT(*) AS view_count, "
            "COALESCE(SUM(masked_count), 0) AS masked_count "
            "FROM privacy_view_access_records WHERE version_id = ?",
            (version_id,),
        ).fetchone()

        cleanup_rows = conn.execute(
            "SELECT id, reason, status, created_at "
            "FROM privacy_view_audit_cleanup_requests "
            "WHERE version_id = ? ORDER BY id ASC",
            (version_id,),
        ).fetchall()
        cleanup_requests = [
            {
                "id": row["id"],
                "reason": row["reason"],
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in cleanup_rows
        ]

        versions.append(
            {
                "version": version_row["version"],
                "policies": policies,
                "hit_count": hit_count,
                "masked_count": access_totals["masked_count"],
                "view_count": access_totals["view_count"],
                "cleanup_requests": cleanup_requests,
            }
        )

    totals = {
        "policy_count": sum(len(version["policies"]) for version in versions),
        "hit_count": sum(version["hit_count"] for version in versions),
        "masked_count": sum(version["masked_count"] for version in versions),
        "view_count": sum(version["view_count"] for version in versions),
        "cleanup_request_count": sum(
            len(version["cleanup_requests"]) for version in versions
        ),
    }
    return {"dataset": dataset["name"], "versions": versions, "totals": totals}


# --------------------------------------------------------------------------- #
# Sensitive-field identification (candidate annotation only)
# --------------------------------------------------------------------------- #


# Sensitive words matched case-insensitively as substrings of the field name,
# in evidence-emission order.
SENSITIVE_WORDS: tuple[str, ...] = (
    "email",
    "phone",
    "id_card",
    "password",
    "token",
    "birth",
)

# Phone numbers are matched against ASCII 0-9 only (str.isdigit() also accepts
# e.g. superscript or full-width Unicode digits).
_ASCII_DIGITS = re.compile(r"[0-9]+\Z")


def _sensitive_name_evidence(field_name: str) -> list[str]:
    """Name-hit kinds for one field name, in sensitive-word order."""
    lowered = field_name.lower()
    return [f"name:{word}" for word in SENSITIVE_WORDS if word in lowered]


def _text_sample_hits(text: str) -> set[str]:
    """Sample-hit kinds for one string value.

    Email: exactly one ``@`` with non-empty text on both sides and a dot in the
    domain. Phone: an 11-digit ASCII decimal string starting with ``1``. The
    two formats are disjoint (an email must contain ``@``).
    """
    hits: set[str] = set()
    if text.count("@") == 1:
        local, _, domain = text.partition("@")
        if local and domain and "." in domain:
            hits.add("sample:email")
    if len(text) == 11 and text[0] == "1" and _ASCII_DIGITS.match(text):
        hits.add("sample:phone")
    return hits


def _sensitive_sample_evidence(samples: list[Any]) -> list[str]:
    """Sample-hit kinds across all samples; only string values participate."""
    hits: set[str] = set()
    for sample in samples:
        if isinstance(sample, str):
            hits |= _text_sample_hits(sample)
    return [
        kind for kind in ("sample:email", "sample:phone") if kind in hits
    ]


def _identify_sensitive(
    field_name: str, field_type: str, samples: list[Any]
) -> tuple[list[str], str]:
    """Combine name and sample evidence for one field.

    Name evidence is independent of the declared type. Sample evidence is only
    computed for fields declared ``string``: every other declared type is
    matched by name only even when a sample looks like an email. Evidence is
    name hits followed by sample hits (each group de-duplicated). Confidence is
    ``high`` when both sides hit, ``medium`` for samples only, ``low`` for the
    name only and ``none`` when neither hits (the record is still generated).
    """
    name_evidence = _sensitive_name_evidence(field_name)
    sample_evidence = (
        _sensitive_sample_evidence(samples) if field_type == "string" else []
    )
    evidence = name_evidence + sample_evidence
    if name_evidence and sample_evidence:
        confidence = "high"
    elif sample_evidence:
        confidence = "medium"
    elif name_evidence:
        confidence = "low"
    else:
        confidence = "none"
    return evidence, confidence


def _identification_row_to_dict(
    row: sqlite3.Row, dataset_name: str, version_number: int
) -> dict:
    return {
        "id": row["id"],
        "field": row["field"],
        "field_type": row["field_type"],
        "evidence": json.loads(row["evidence"]),
        "confidence": row["confidence"],
        "source": {
            "dataset": dataset_name,
            "version": version_number,
            "field": row["field"],
        },
        "created_at": row["created_at"],
    }


def create_sensitive_identification(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    field: str,
    samples: list[Any],
    *,
    query_keys: tuple[str, ...] = (),
) -> tuple[dict, bool]:
    """Identify one field or refresh its existing identification in place.

    Returns ``(record, created)`` with ``created`` true for the first
    submission (201) and false for an in-place refresh that keeps the same id
    (200). The path dataset/version must resolve first (404); a blank field
    name or any query parameter is a 422 checked afterwards, and a field that
    does not exist in the version is a 404 — mirroring the privacy-policy
    precedence. Identification only reads the declared name and type and the
    submitted scalars; the samples are never stored. A concurrent first
    submission in another process turns the losing insert into a refresh, so
    exactly one record per (version, field) ever exists.
    """
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    clean_field = field.strip()
    if not clean_field:
        raise RequestInvalidError("Field name must not be empty")
    if query_keys:
        raise RequestInvalidError(
            "The sensitive identification endpoint does not accept query parameters"
        )

    field_row = conn.execute(
        "SELECT name, type FROM schema_fields WHERE version_id = ? AND name = ?",
        (version_row["id"], clean_field),
    ).fetchone()
    if field_row is None:
        raise NotFoundError(
            f"Field '{clean_field}' does not exist in version "
            f"{version_number} of dataset '{dataset_name}'"
        )

    evidence, confidence = _identify_sensitive(
        field_row["name"], field_row["type"], samples
    )

    existing = conn.execute(
        "SELECT id FROM sensitive_identifications "
        "WHERE version_id = ? AND field = ?",
        (version_row["id"], field_row["name"]),
    ).fetchone()

    if existing is None:
        try:
            cursor = conn.execute(
                "INSERT INTO sensitive_identifications ("
                "version_id, field, field_type, evidence, confidence, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    version_row["id"],
                    field_row["name"],
                    field_row["type"],
                    json.dumps(evidence),
                    confidence,
                    utc_now_iso(),
                ),
            )
            identification_id = cursor.lastrowid
            created = True
        except sqlite3.IntegrityError:
            # Another process inserted the same (version, field) first: end the
            # current transaction so the competing commit is visible, then treat
            # the request as an in-place refresh rather than a conflict.
            conn.rollback()
            existing = conn.execute(
                "SELECT id FROM sensitive_identifications "
                "WHERE version_id = ? AND field = ?",
                (version_row["id"], field_row["name"]),
            ).fetchone()
            identification_id = existing["id"]
            conn.execute(
                "UPDATE sensitive_identifications "
                "SET field_type = ?, evidence = ?, confidence = ? WHERE id = ?",
                (
                    field_row["type"],
                    json.dumps(evidence),
                    confidence,
                    identification_id,
                ),
            )
            created = False
    else:
        conn.execute(
            "UPDATE sensitive_identifications "
            "SET field_type = ?, evidence = ?, confidence = ? WHERE id = ?",
            (
                field_row["type"],
                json.dumps(evidence),
                confidence,
                existing["id"],
            ),
        )
        identification_id = existing["id"]
        created = False

    row = conn.execute(
        "SELECT * FROM sensitive_identifications WHERE id = ?",
        (identification_id,),
    ).fetchone()
    return (
        _identification_row_to_dict(row, dataset["name"], version_row["version"]),
        created,
    )


def list_sensitive_identifications(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> list[dict]:
    """Every sensitive identification of one version, ordered by id ascending."""
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    if body.strip():
        raise RequestInvalidError(
            "The sensitive identifications endpoint does not accept a request body"
        )
    if query_keys:
        raise RequestInvalidError(
            "The sensitive identifications endpoint does not accept query parameters"
        )

    rows = conn.execute(
        "SELECT * FROM sensitive_identifications WHERE version_id = ? ORDER BY id ASC",
        (version_row["id"],),
    ).fetchall()
    return [
        _identification_row_to_dict(row, dataset["name"], version_row["version"])
        for row in rows
    ]


# Evidence kinds that place an identified field in the PII classification
# (email, phone, id card, birthday). Credential kinds are passwords and tokens.
# A field hitting both groups stays PII (see ``_identification_suggestion``).
_PII_EVIDENCE_KINDS: frozenset[str] = frozenset(
    {
        "name:email",
        "name:phone",
        "name:id_card",
        "name:birth",
        "sample:email",
        "sample:phone",
    }
)
_CREDENTIAL_EVIDENCE_KINDS: frozenset[str] = frozenset(
    {"name:password", "name:token"}
)


def _identification_suggestion(row: sqlite3.Row) -> dict | None:
    """Build the advisory masking suggestion for one identification record.

    Returns ``None`` for a record with no evidence (neither the name nor the
    samples ever hit): such a record produces no suggestion. Email, phone, id
    card and birthday hits classify the field as ``PII`` and suggest
    ``partial`` masking; password and token hits classify it as ``CREDENTIAL``
    and suggest ``redact``. When both groups hit, PII wins and the masking is
    ``partial``. Credential suggestions always carry an empty role list: the
    suggested masking applies to every role.
    """
    evidence = json.loads(row["evidence"])
    hits_pii = any(kind in _PII_EVIDENCE_KINDS for kind in evidence)
    if hits_pii:
        classification, masking = "PII", "partial"
    elif any(kind in _CREDENTIAL_EVIDENCE_KINDS for kind in evidence):
        classification, masking = "CREDENTIAL", "redact"
    else:
        return None
    return {
        "field": row["field"],
        "classification": classification,
        "masking": masking,
        "allowed_roles": [],
    }


def list_masking_suggestions(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> list[dict]:
    """Advisory masking suggestions for one version, ordered by record id.

    Suggestions are recomputed on every read from the current identification
    records; the endpoint writes nothing and never registers a privacy policy.
    Only records with at least one name or sample hit yield a suggestion, so a
    version without identifications (or with only unhit records) returns an
    empty list. The path dataset/version resolves first (404 precedence); any
    body bytes or query parameters are a 422 checked afterwards, mirroring the
    identifications list endpoint.
    """
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)

    if body.strip():
        raise RequestInvalidError(
            "The masking suggestions endpoint does not accept a request body"
        )
    if query_keys:
        raise RequestInvalidError(
            "The masking suggestions endpoint does not accept query parameters"
        )

    rows = conn.execute(
        "SELECT * FROM sensitive_identifications WHERE version_id = ? ORDER BY id ASC",
        (version_row["id"],),
    ).fetchall()
    suggestions: list[dict] = []
    for row in rows:
        suggestion = _identification_suggestion(row)
        if suggestion is not None:
            suggestions.append(suggestion)
    return suggestions


# Serializes validate-existing -> insert within this process so two concurrent
# registrations can never both pass the "no policy yet" check (mirrors the
# evaluation append lock). The privacy_policies UNIQUE(version_id, field)
# constraint is the hard cross-process guard; the losing registration turns
# its insert into a 409 and the whole batch is rolled back.
_suggestion_register_locks: dict[int, threading.Lock] = {}
_suggestion_register_locks_guard = threading.Lock()


def _suggestion_register_lock(version_id: int) -> threading.Lock:
    with _suggestion_register_locks_guard:
        lock = _suggestion_register_locks.get(version_id)
        if lock is None:
            lock = threading.Lock()
            _suggestion_register_locks[version_id] = lock
        return lock


def _current_masking_suggestions(
    conn: sqlite3.Connection, version_id: int
) -> dict[str, dict]:
    """Read-time-recomputed candidates of one version keyed by field name.

    This is the same recomputation as the read-only suggestions endpoint, so
    registration acts on exactly the candidates a concurrent GET would list:
    only identification records with at least one name/sample hit contribute.
    """
    rows = conn.execute(
        "SELECT * FROM sensitive_identifications WHERE version_id = ? ORDER BY id ASC",
        (version_id,),
    ).fetchall()
    return {
        row["field"]: suggestion
        for row in rows
        if (suggestion := _identification_suggestion(row)) is not None
    }


def register_masking_suggestions(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    fields: list[str],
    *,
    query_keys: tuple[str, ...] = (),
) -> list[dict]:
    """Register one privacy policy per requested field from its candidate.

    The whole batch is validated before any policy or timestamp is written;
    any rejection rolls back without a partial result. Validation order,
    mirroring the manual policy creation, is:

    1. The path dataset/version must resolve (404).
    2. Query parameters (422) and the field-name list shape: every name is
       non-empty after trimming and no name repeats (422).
    3. Every requested field must exist in the version (404).
    4. Each field must currently have a candidate (a hit-bearing
       identification record) and must not already carry a policy (409).

    Each new policy copies the candidate's classification and masking verbatim
    with its empty allowed-role list, is enabled by default and gets a
    service-generated id and timestamp; records are returned in the request's
    field-name order. Identification records are only read, never written.
    """
    dataset = require_dataset(conn, dataset_name)
    version_row = _require_schema_version(conn, dataset, version_number)
    version_id = version_row["id"]

    if query_keys:
        raise RequestInvalidError(
            "The masking suggestion registration endpoint does not accept "
            "query parameters"
        )

    cleaned_fields: list[str] = []
    seen: set[str] = set()
    for name in fields:
        clean_name = name.strip()
        if not clean_name:
            raise RequestInvalidError("Field names must not be empty")
        if clean_name in seen:
            raise RequestInvalidError(
                f"Field '{clean_name}' is listed more than once in 'fields'"
            )
        seen.add(clean_name)
        cleaned_fields.append(clean_name)

    version_field_names = _version_field_names(conn, version_id)
    for clean_name in cleaned_fields:
        if clean_name not in version_field_names:
            raise NotFoundError(
                f"Field '{clean_name}' does not exist in version "
                f"{version_number} of dataset '{dataset_name}'"
            )

    with _suggestion_register_lock(version_id):
        suggestions_by_field = _current_masking_suggestions(conn, version_id)
        for clean_name in cleaned_fields:
            if clean_name not in suggestions_by_field:
                # Covers both "never identified" and an identification record
                # whose two classes both missed on its latest run.
                raise ConflictError(
                    f"No masking suggestion candidate is available for field "
                    f"'{clean_name}'; identify the field before registering a "
                    "privacy policy"
                )

        existing = {
            row["field"]
            for row in conn.execute(
                "SELECT field FROM privacy_policies WHERE version_id = ?",
                (version_id,),
            ).fetchall()
        }
        for clean_name in cleaned_fields:
            if clean_name in existing:
                raise ConflictError(
                    f"A privacy policy for field '{clean_name}' already exists "
                    f"for version {version_number} of dataset '{dataset_name}'"
                )

        # All checks passed: the inserts below are the only writes, and they
        # share the request's transaction, so a failure still rolls back every
        # policy and timestamp. The UNIQUE(version_id, field) constraint turns a
        # competing registration in another process into a 409 here.
        inserted_ids: list[int] = []
        try:
            for clean_name in cleaned_fields:
                suggestion = suggestions_by_field[clean_name]
                cursor = conn.execute(
                    "INSERT INTO privacy_policies ("
                    "version_id, field, classification, masking, allowed_roles, "
                    "enabled, created_at"
                    ") VALUES (?, ?, ?, ?, ?, 1, ?)",
                    (
                        version_id,
                        clean_name,
                        suggestion["classification"],
                        suggestion["masking"],
                        json.dumps(suggestion["allowed_roles"]),
                        utc_now_iso(),
                    ),
                )
                inserted_ids.append(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "A privacy policy for one of the requested fields was "
                "registered concurrently; retry the request"
            ) from exc

        rows_by_field: dict[str, sqlite3.Row] = {}
        for policy_id in inserted_ids:
            row = conn.execute(
                "SELECT id, field, classification, masking, allowed_roles, "
                "enabled, created_at FROM privacy_policies WHERE id = ?",
                (policy_id,),
            ).fetchone()
            rows_by_field[row["field"]] = row

    return [_policy_row_to_dict(rows_by_field[name]) for name in cleaned_fields]


# --------------------------------------------------------------------------- #
# Read-only cross-version privacy policy coverage check
# --------------------------------------------------------------------------- #


def privacy_policy_coverage(
    conn: sqlite3.Connection,
    dataset_name: str,
    *,
    body: bytes = b"",
    query_keys: tuple[str, ...] = (),
) -> dict:
    """Read-only whole-dataset privacy policy coverage check.

    One entry per schema version of the dataset, ordered by version number
    ascending. Each entry lists every field of the version (ordered by field
    name) with its coverage state — ``enabled`` (an enabled policy is
    registered), ``disabled`` (a policy is registered but disabled) or
    ``unregistered`` (no policy) — together with the registered policy's
    classification, masking and enabled state (all null when no policy is
    registered), plus the version's advisory candidates: identified fields
    whose name or samples hit but that carry no privacy policy yet, ordered
    by identification record id ascending. A record whose name and samples
    both missed yields no candidate. ``totals`` sums the version, field,
    per-state and candidate counters over all versions; a dataset without
    versions yields an empty version list and all-zero totals, never an
    error.

    The check is recomputed on every read: it caches nothing and never
    writes, modifies or deletes a policy or an identification record, and
    the candidates never register a policy. Like the compliance export, the
    dataset resolves first (404); a non-empty request body or any query
    parameter is a 422 checked afterwards.
    """
    dataset = require_dataset(conn, dataset_name)

    if body.strip():
        raise RequestInvalidError(
            "The privacy policy coverage endpoint does not accept a request body"
        )
    if query_keys:
        raise RequestInvalidError(
            "The privacy policy coverage endpoint does not accept query "
            "parameters"
        )

    version_rows = conn.execute(
        "SELECT id, version FROM schema_versions "
        "WHERE dataset_id = ? ORDER BY version ASC",
        (dataset["id"],),
    ).fetchall()

    versions: list[dict] = []
    for version_row in version_rows:
        version_id = version_row["id"]

        field_rows = conn.execute(
            "SELECT name FROM schema_fields WHERE version_id = ? "
            "ORDER BY name ASC",
            (version_id,),
        ).fetchall()
        policy_rows = conn.execute(
            "SELECT field, classification, masking, enabled "
            "FROM privacy_policies WHERE version_id = ?",
            (version_id,),
        ).fetchall()
        # At most one policy per field (UNIQUE(version_id, field)).
        policies_by_field = {row["field"]: row for row in policy_rows}

        fields: list[dict] = []
        for field_row in field_rows:
            policy = policies_by_field.get(field_row["name"])
            if policy is None:
                fields.append(
                    {
                        "field": field_row["name"],
                        "coverage": "unregistered",
                        "classification": None,
                        "masking": None,
                        "enabled": None,
                    }
                )
            else:
                enabled = bool(policy["enabled"])
                fields.append(
                    {
                        "field": field_row["name"],
                        "coverage": "enabled" if enabled else "disabled",
                        "classification": policy["classification"],
                        "masking": policy["masking"],
                        "enabled": enabled,
                    }
                )

        identification_rows = conn.execute(
            "SELECT * FROM sensitive_identifications WHERE version_id = ? "
            "ORDER BY id ASC",
            (version_id,),
        ).fetchall()
        candidates: list[dict] = []
        for row in identification_rows:
            if row["field"] in policies_by_field:
                continue
            suggestion = _identification_suggestion(row)
            if suggestion is not None:
                candidates.append(
                    {
                        "field": suggestion["field"],
                        "classification": suggestion["classification"],
                        "masking": suggestion["masking"],
                    }
                )

        versions.append(
            {
                "version": version_row["version"],
                "fields": fields,
                "candidates": candidates,
            }
        )

    totals = {
        "version_count": len(versions),
        "field_count": sum(len(version["fields"]) for version in versions),
        "enabled_count": sum(
            1
            for version in versions
            for field in version["fields"]
            if field["coverage"] == "enabled"
        ),
        "disabled_count": sum(
            1
            for version in versions
            for field in version["fields"]
            if field["coverage"] == "disabled"
        ),
        "unregistered_count": sum(
            1
            for version in versions
            for field in version["fields"]
            if field["coverage"] == "unregistered"
        ),
        "candidate_count": sum(
            len(version["candidates"]) for version in versions
        ),
    }
    return {"dataset": dataset["name"], "versions": versions, "totals": totals}


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
    # Serialize against run starts, dispatch and cancellations in this process
    # (see _processing_write_section) and take the database write lock up
    # front, so a finish racing a cancellation reads the committed terminal
    # state instead of upgrading a shared lock mid-request.
    with _processing_write_section():
        conn.execute("BEGIN IMMEDIATE")
        try:
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

            # The conditional write is the hard guard against a concurrent
            # cancellation: it matches only while the run is still 'running',
            # so exactly one of finish/cancel commits the terminal transition.
            cursor = conn.execute(
                "UPDATE processing_task_runs SET status = ?, finished_at = ?, "
                "error = ? WHERE id = ? AND status = 'running'",
                (status, utc_now_iso(), error, run_row["id"]),
            )
            if cursor.rowcount != 1:
                raise ConflictError(
                    f"Processing task run {run_id} was finished concurrently"
                )
            conn.execute(
                "UPDATE processing_tasks SET status = ? WHERE id = ?",
                (status, task_row["id"]),
            )
            updated = conn.execute(
                "SELECT * FROM processing_task_runs WHERE id = ?", (run_row["id"],)
            ).fetchone()
            return _run_row_to_dict(updated)
        except BaseException:
            conn.rollback()
            raise


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
    """Cancel a still-``running`` run, storing the trimmed reason as its error.

    The run is ended atomically (``finished_at`` plus status ``failed``)
    together with the task's transition to ``failed``; ``attempt_count`` is
    deliberately not rolled back, so the existing start rules hand out the
    next continuous attempt while attempts remain. Racing a finish, a
    single-task start or a batch dispatch is single-winner: the conditional
    ``UPDATE ... WHERE status = 'running'`` only matches for the transaction
    that performs the transition, every other request gets a 409 and no field
    is left half-written.

    The request path must resolve first (unknown dataset/version/task/run is a
    404 and a run owned by another task/version is a 422); the blank-reason
    and query-parameter checks are 422s evaluated after the path resolves,
    mirroring finish/audit-record precedence. Nothing is written when any
    check fails.
    """
    with _processing_write_section():
        conn.execute("BEGIN IMMEDIATE")
        try:
            dataset = require_dataset(conn, dataset_name)
            version_row = _require_schema_version(conn, dataset, version_number)
            task_row = _require_task(
                conn, version_row, dataset_name, version_number, task_id
            )

            clean_reason = reason.strip()
            if not clean_reason:
                raise RequestInvalidError("Cancellation reason must not be empty")
            if query_keys:
                raise RequestInvalidError(
                    "The run cancel endpoint does not accept query parameters"
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
                    "and cannot be cancelled"
                )

            # The conditional write is the hard guard against a concurrent
            # finish or cancellation: it matches only while the run is still
            # 'running', so exactly one racing transaction commits the
            # terminal transition and every other one gets a 409.
            cursor = conn.execute(
                "UPDATE processing_task_runs SET status = 'failed', "
                "finished_at = ?, error = ? WHERE id = ? AND status = 'running'",
                (utc_now_iso(), clean_reason, run_row["id"]),
            )
            if cursor.rowcount != 1:
                raise ConflictError(
                    f"Processing task run {run_id} was finished concurrently"
                )
            conn.execute(
                "UPDATE processing_tasks SET status = 'failed' WHERE id = ?",
                (task_row["id"],),
            )
            updated = conn.execute(
                "SELECT * FROM processing_task_runs WHERE id = ?", (run_row["id"],)
            ).fetchone()
            return _run_row_to_dict(updated)
        except BaseException:
            conn.rollback()
            raise


def batch_complete_task_runs(
    conn: sqlite3.Connection,
    dataset_name: str,
    version_number: int,
    items: list[dict[str, Any]],
    *,
    query_keys: tuple[str, ...] = (),
) -> dict:
    """Finish a non-empty batch of currently running runs in one transaction.

    Every item names a task of the path version together with one of its runs
    and declares ``succeeded`` (the error field must be omitted altogether;
    an explicit value, including null, is rejected) or ``failed`` (an error
    that is non-empty after trimming). The whole batch is validated before any
    write: the path dataset/version must exist (404); query parameters are
    rejected (422); per-item status/error rules are checked (422); every named
    task must exist in the version (404) and must not appear twice (409); every
    run must exist (404), belong to the named task when it is in the same
    version (422) and still be ``running`` with its task currently completable
    (409). A run owned by a task in another dataset/version is treated as not
    existing in this version (404).

    On success every run gets a ``finished_at`` timestamp and its terminal
    status, and the owning task moves to the same status atomically; the result
    runs are returned ordered by task id ascending. A ``failed`` task keeps its
    ``attempt_count`` and follows the existing retry rules. The process lock
    and ``BEGIN IMMEDIATE`` serialize against single finish/cancel/start and
    dispatch; the conditional ``UPDATE ... WHERE status = 'running'`` is the
    hard single-winner guard, so any racing request gets a 409 with the whole
    batch rolled back and no half-written row.
    """
    with _processing_write_section():
        conn.execute("BEGIN IMMEDIATE")
        try:
            dataset = require_dataset(conn, dataset_name)
            version_row = _require_schema_version(conn, dataset, version_number)

            if query_keys:
                raise RequestInvalidError(
                    "The batch-complete endpoint does not accept query parameters"
                )

            # Phase 1: resolve every named task within the path version. All
            # existence checks precede every other per-item check so an unknown
            # task stays a 404 regardless of the batch order or any other
            # invalid item.
            task_rows: dict[int, sqlite3.Row] = {}
            for item in items:
                task_id = item["task_id"]
                task_row = conn.execute(
                    "SELECT * FROM processing_tasks WHERE version_id = ? AND id = ?",
                    (version_row["id"], task_id),
                ).fetchone()
                if task_row is None:
                    raise NotFoundError(
                        f"Processing task {task_id} does not exist in version "
                        f"{version_number} of dataset '{dataset_name}'"
                    )
                task_rows[task_id] = task_row

            # Phase 2: per-item payload semantics, mirroring the single finish
            # endpoint (which resolves the path task before these same checks):
            # a success may only omit the error field — writing it explicitly
            # in any form, including null, is rejected — and a failure needs a
            # non-blank error string.
            for item in items:
                status = item["status"]
                error = item.get("error", ERROR_FIELD_UNSET)
                if status == "succeeded":
                    if error is not ERROR_FIELD_UNSET:
                        raise RequestInvalidError(
                            "A successful run must not carry an error message"
                        )
                elif error is None or error is ERROR_FIELD_UNSET or not error.strip():
                    raise RequestInvalidError(
                        "A failed run requires a non-empty error message"
                    )

            # Phase 3: resolve every run. Existence across the whole batch is
            # checked before ownership: a run in another dataset/version is out
            # of scope and reported as not found (404), never as an ownership
            # mismatch.
            run_rows: dict[int, sqlite3.Row] = {}
            for item in items:
                run_id = item["run_id"]
                run_row = conn.execute(
                    "SELECT r.*, t.version_id AS owner_version_id "
                    "FROM processing_task_runs r "
                    "JOIN processing_tasks t ON t.id = r.task_id "
                    "WHERE r.id = ?",
                    (run_id,),
                ).fetchone()
                if run_row is None or run_row["owner_version_id"] != version_row["id"]:
                    raise NotFoundError(
                        f"Processing task run {run_id} does not exist in version "
                        f"{version_number} of dataset '{dataset_name}'"
                    )
                run_rows[run_id] = run_row

            # Phase 4: ownership within the version. A run of another task in
            # the same (dataset, version) is a 422.
            for item in items:
                run_row = run_rows[item["run_id"]]
                if run_row["task_id"] != item["task_id"]:
                    raise RequestInvalidError(
                        f"Processing task run {item['run_id']} does not belong to "
                        f"task {item['task_id']} in version {version_number} of "
                        f"dataset '{dataset_name}'"
                    )

            # Phase 5: conflicts, evaluated only once every reference resolves
            # and every payload is valid (404/422 take precedence, as on the
            # single finish endpoint). The same task may appear at most once;
            # only the task's current running run can be completed and the task
            # must still be running.
            seen_task_ids: set[int] = set()
            for item in items:
                task_id = item["task_id"]
                if task_id in seen_task_ids:
                    raise ConflictError(
                        f"Processing task {task_id} appears more than once in the "
                        "batch; each task may be completed at most once"
                    )
                seen_task_ids.add(task_id)

                task_row = task_rows[task_id]
                run_row = run_rows[item["run_id"]]
                if run_row["status"] != "running":
                    raise ConflictError(
                        f"Processing task run {run_row['id']} is already "
                        f"'{run_row['status']}' and cannot be finished again"
                    )
                if task_row["status"] != "running":
                    raise ConflictError(
                        f"Processing task {task_row['id']} is "
                        f"'{task_row['status']}' and cannot be completed currently"
                    )

            # All checks passed: write the batch. Each conditional update is
            # the hard guard against a concurrent terminal transition; a lost
            # race rolls back the entire batch.
            finished_at = utc_now_iso()
            for item in items:
                run_row = run_rows[item["run_id"]]
                error = item["error"] if item["status"] == "failed" else None
                cursor = conn.execute(
                    "UPDATE processing_task_runs SET status = ?, finished_at = ?, "
                    "error = ? WHERE id = ? AND status = 'running'",
                    (item["status"], finished_at, error, run_row["id"]),
                )
                if cursor.rowcount != 1:
                    raise ConflictError(
                        f"Processing task run {run_row['id']} was finished "
                        "concurrently"
                    )
                conn.execute(
                    "UPDATE processing_tasks SET status = ? WHERE id = ?",
                    (item["status"], item["task_id"]),
                )

            completed: list[dict] = []
            for item in items:
                updated = conn.execute(
                    "SELECT * FROM processing_task_runs WHERE id = ?",
                    (item["run_id"],),
                ).fetchone()
                completed.append(_run_row_to_dict(updated))
        except BaseException:
            conn.rollback()
            raise

    completed.sort(key=lambda run: run["task_id"])
    return {
        "dataset": dataset["name"],
        "version": version_row["version"],
        "runs": completed,
    }


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
