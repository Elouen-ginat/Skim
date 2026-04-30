from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skaal.cli.main import app as cli_app


@pytest.fixture(autouse=True)
def reset_skaal_logging() -> None:
    skaal_logger = logging.getLogger("skaal")
    skaal_logger.handlers = [
        handler for handler in skaal_logger.handlers if isinstance(handler, logging.NullHandler)
    ]
    skaal_logger.setLevel(logging.NOTSET)
    skaal_logger.propagate = True

    for name in ("skaal.cli", "skaal.deploy", "skaal.solver", "skaal.plan"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)


def _runner() -> CliRunner:
    return CliRunner()


def _fake_destroy(
    *,
    artifacts_dir: Path,
    stack: str | None,
    yes: bool,
) -> None:
    del artifacts_dir, stack, yes
    logging.getLogger("skaal.deploy").info("deploy info")
    logging.getLogger("skaal.solver").info("solver info")
    logging.getLogger("skaal.deploy").debug("deploy debug")


def test_cli_logging_verbosity_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("skaal.api.destroy", _fake_destroy)

    default_result = _runner().invoke(
        cli_app,
        ["destroy", "--artifacts-dir", str(tmp_path), "--stack", "dev", "--yes"],
    )
    assert default_result.exit_code == 0
    assert "==> deploy info" in default_result.output
    assert "solver info" not in default_result.output
    assert "deploy debug" not in default_result.output

    verbose_result = _runner().invoke(
        cli_app,
        ["-v", "destroy", "--artifacts-dir", str(tmp_path), "--stack", "dev", "--yes"],
    )
    assert verbose_result.exit_code == 0
    assert "solver info" in verbose_result.output
    assert "deploy debug" not in verbose_result.output

    debug_result = _runner().invoke(
        cli_app,
        ["-vv", "destroy", "--artifacts-dir", str(tmp_path), "--stack", "dev", "--yes"],
    )
    assert debug_result.exit_code == 0
    assert "deploy debug" in debug_result.output

    quiet_result = _runner().invoke(
        cli_app,
        ["-q", "destroy", "--artifacts-dir", str(tmp_path), "--stack", "dev", "--yes"],
    )
    assert quiet_result.exit_code == 0
    assert quiet_result.output == ""


def test_cli_logging_respects_pyproject_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.skaal.logging]\nlevel = \"DEBUG\"\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("skaal.api.destroy", _fake_destroy)

    result = _runner().invoke(
        cli_app,
        ["destroy", "--artifacts-dir", str(tmp_path), "--stack", "dev", "--yes"],
    )

    assert result.exit_code == 0
    assert "deploy debug" in result.output


def test_cli_logging_json_format(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_destroy_json(*, artifacts_dir: Path, stack: str | None, yes: bool) -> None:
        del artifacts_dir, stack, yes
        logging.getLogger("skaal.deploy").info(
            "deploy info",
            extra={"app": "demo", "stack": "dev", "target": "local"},
        )

    monkeypatch.setattr("skaal.api.destroy", fake_destroy_json)

    result = _runner().invoke(
        cli_app,
        [
            "--log-format",
            "json",
            "destroy",
            "--artifacts-dir",
            str(tmp_path),
            "--stack",
            "dev",
            "--yes",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output.strip())
    assert payload["level"] == "INFO"
    assert payload["logger"] == "skaal.deploy"
    assert payload["msg"] == "deploy info"
    assert payload["app"] == "demo"
    assert payload["stack"] == "dev"
    assert payload["target"] == "local"
    assert "ts" in payload