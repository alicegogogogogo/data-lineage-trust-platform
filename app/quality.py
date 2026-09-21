"""Pure evaluation logic for data-quality rules.

Each evaluator returns the ascending, zero-based indices of the rows that
violate the rule. Evaluators are deliberately free of I/O so they can be unit
tested and reused independently of the persistence layer.
"""

from __future__ import annotations

import json
import math
from typing import Any

#: Supported rule kinds.
RULE_KINDS = ("not_null", "numeric_range", "unique")


def _is_finite_number(value: Any) -> bool:
    """Booleans are not numbers for quality purposes; NaN/inf cannot cross JSON."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def evaluate_not_null(rows: list[dict[str, Any]], parameters: dict[str, Any]) -> list[int]:
    field = parameters["field"]
    return [
        index
        for index, row in enumerate(rows)
        if field not in row or row[field] is None
    ]


def evaluate_numeric_range(
    rows: list[dict[str, Any]], parameters: dict[str, Any]
) -> list[int]:
    field = parameters["field"]
    minimum = parameters["min"]
    maximum = parameters["max"]
    violations: list[int] = []
    for index, row in enumerate(rows):
        value = row.get(field)
        # Missing, null, non-numeric, boolean or out-of-range values all fail.
        if not _is_finite_number(value) or value < minimum or value > maximum:
            violations.append(index)
    return violations


def evaluate_unique(
    rows: list[dict[str, Any]], parameters: dict[str, Any]
) -> list[int]:
    fields = parameters["fields"]
    seen: set[str] = set()
    violations: list[int] = []
    for index, row in enumerate(rows):
        # Missing fields participate in the combination as null; JSON encoding
        # keeps nested values usable as part of the key.
        key = json.dumps(
            [None if field not in row else row[field] for field in fields],
            sort_keys=True,
        )
        if key in seen:
            violations.append(index)
        else:
            seen.add(key)
    return violations


_EVALUATORS = {
    "not_null": evaluate_not_null,
    "numeric_range": evaluate_numeric_range,
    "unique": evaluate_unique,
}


def evaluate_rule(
    kind: str, parameters: dict[str, Any], rows: list[dict[str, Any]]
) -> list[int]:
    return _EVALUATORS[kind](rows, parameters)
