"""Tests for ``skaal init``."""

from __future__ import annotations

import ast
import os
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skaal.cli.main import app as cli_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _run(runner: CliRunner, cwd: Path, *argv: str) -> tuple[int, str]:
    here = os.getcwd()
    try:
        os.chdir(cwd)
        result = runner.invoke(cli_app, ["init", *argv])
        return result.exit_code, result.output
    finally:
        os.chdir(here)


def test_init_creates_full_layout(runner: CliRunner, tmp_path: Path) -> None:
    code, _ = _run(runner, tmp_path, "demo")
    assert code == 0

    root = tmp_path / "demo"
    assert (root / "pyproject.toml").is_file()
    assert (root / "demo" / "__init__.py").is_file()
    assert (root / "demo" / "app.py").is_file()
    assert (root / "catalogs" / "local.toml").is_file()
    assert (root / ".gitignore").is_file()
    assert (root / "README.md").is_file()


def test_init_pyproject_is_valid_toml_with_skaal_section(
    runner: CliRunner, tmp_path: Path
) -> None:
    _run(runner, tmp_path, "demo")
    data = tomllib.loads((tmp_path / "demo" / "pyproject.toml").read_text())
    assert data["tool"]["skaal"]["app"] == "demo.app:app"
    assert data["project"]["name"] == "demo"


def test_init_app_module_parses(runner: CliRunner, tmp_path: Path) -> None:
    _run(runner, tmp_path, "demo")
    ast.parse((tmp_path / "demo" / "demo" / "app.py").read_text())


def test_init_here_uses_cwd(runner: CliRunner, tmp_path: Path) -> None:
    code, _ = _run(runner, tmp_path, "demo", "--here")
    assert code == 0
    assert (tmp_path / "pyproject.toml").is_file()
    assert (tmp_path / "demo" / "app.py").is_file()


def test_init_refuses_to_overwrite_without_force(runner: CliRunner, tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "pyproject.toml").write_text("# existing\n")

    code, _ = _run(runner, tmp_path, "demo")
    assert code != 0
    assert (tmp_path / "demo" / "pyproject.toml").read_text() == "# existing\n"


def test_init_force_overwrites(runner: CliRunner, tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "pyproject.toml").write_text("# existing\n")

    code, _ = _run(runner, tmp_path, "demo", "--force")
    assert code == 0
    assert (tmp_path / "demo" / "pyproject.toml").read_text() != "# existing\n"


@pytest.mark.parametrize("bad_name", ["1bad", "a-b", "with space", ""])
def test_init_rejects_invalid_name(runner: CliRunner, tmp_path: Path, bad_name: str) -> None:
    code, _ = _run(runner, tmp_path, bad_name)
    assert code != 0
