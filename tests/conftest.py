"""Shared pytest fixtures.

Each in-process test runs against an isolated SQLite database file. The
application opens a fresh connection per request and reads the database path
from the environment at connect time, so pointing ``DATA_LINEAGE_DB`` at a
temporary location is enough to isolate a test.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "test-lineage.db"
    monkeypatch.setenv("DATA_LINEAGE_DB", str(db_path))
    return db_path


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
