"""Content fingerprint of a persisted snapshot's row sequence.

The fingerprint is canonical JSON hashed with SHA-256: the rows are written as
an array in stored row order, every object's keys are sorted by Unicode code
point, no insignificant whitespace is emitted and non-ASCII characters are
written verbatim (not escaped). Array order and JSON value types are therefore
significant and the digest is identical across processes and restarts for the
same saved row sequence.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_snapshot_rows(rows: Any) -> str:
    """Serialize a snapshot's row sequence to its canonical JSON text."""
    return json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def snapshot_content_hash(rows: Any) -> str:
    """SHA-256 hex digest of the row sequence's canonical UTF-8 JSON text."""
    return hashlib.sha256(canonical_snapshot_rows(rows).encode("utf-8")).hexdigest()
