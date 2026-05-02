"""Tests for ADR 022 — `skaal catalog validate` / `sources` subcommands."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skaal.cli.catalog_cmd import app as catalog_app

_VALID = """
[storage.sqlite]
display_name = "SQLite"
read_latency  = { min = 0.1, max = 5.0, unit = "ms" }
write_latency = { min = 0.5, max = 20.0, unit = "ms" }
durability    = ["persistent"]
storage_kinds = ["kv"]
access_patterns = ["random-read", "random-write"]
cost_per_gb_month = 0.0
"""


def _write(p: Path, body: str) -> Path:
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _capture_skaal_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """The catalog CLI emits via `log.info`; pytest needs the level lowered."""
    caplog.set_level(logging.INFO, logger="skaal.cli")
    return caplog


def _output(result, caplog: pytest.LogCaptureFixture) -> str:
    """Combined stdout + captured log records."""
    return (result.output or "") + caplog.text


def test_validate_succeeds_on_well_formed_catalog(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    catalog = _write(tmp_path / "ok.toml", _VALID)
    runner = CliRunner()
    result = runner.invoke(catalog_app, ["validate", str(catalog)])
    assert result.exit_code == 0
    assert "OK " in _output(result, caplog)


def test_validate_fails_with_exit_code_2(tmp_path: Path) -> None:
    """A missing required field trips Catalog.from_raw → exit code 2."""
    catalog = _write(
        tmp_path / "bad.toml",
        """
[storage.sqlite]
display_name = "SQLite"
read_latency  = { min = 0.1, max = 5.0, unit = "ms" }
durability    = ["persistent"]
storage_kinds = ["kv"]
access_patterns = ["random-read"]
cost_per_gb_month = 0.0
""",  # missing write_latency — required by StorageBackendSpec
    )
    runner = CliRunner()
    result = runner.invoke(catalog_app, ["validate", str(catalog)])
    assert result.exit_code == 2


def test_validate_fails_on_invalid_toml(tmp_path: Path) -> None:
    catalog = _write(tmp_path / "broken.toml", "[storage.sqlite\n")
    runner = CliRunner()
    result = runner.invoke(catalog_app, ["validate", str(catalog)])
    assert result.exit_code != 0


def test_sources_prints_chain(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    base = _write(tmp_path / "base.toml", _VALID)
    child = _write(
        tmp_path / "dev.toml",
        '[skaal]\nextends = "base.toml"\n\n' + _VALID.replace("SQLite", "SQLite (dev)"),
    )
    runner = CliRunner()
    result = runner.invoke(catalog_app, ["sources", str(child)])
    assert result.exit_code == 0
    out = _output(result, caplog)
    assert str(base.resolve()) in out
    assert str(child.resolve()) in out


def test_sources_prints_removes(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _write(tmp_path / "base.toml", _VALID)
    child = _write(
        tmp_path / "lean.toml",
        '[skaal]\nextends = "base.toml"\nremove = ["storage.sqlite"]\n',
    )
    runner = CliRunner()
    result = runner.invoke(catalog_app, ["sources", str(child)])
    assert result.exit_code == 0
    out = _output(result, caplog)
    assert "removes" in out
    assert "storage.sqlite" in out
