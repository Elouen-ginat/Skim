"""Fixtures for relational-migration tests."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture
def isolated_cwd(tmp_path: Path) -> Iterator[Path]:
    """Run the test inside *tmp_path* so .skaal/migrations/ is per-test."""
    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(previous)
