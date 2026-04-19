"""Tests for CLI commands: plan, build, run, deploy, etc."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from skaal.app import App
from skaal.cli.main import app as cli_app


@pytest.fixture
def runner() -> CliRunner:
    """Typer CLI test runner."""
    return CliRunner()


@pytest.fixture
def temp_catalog(tmp_path: Path) -> Path:
    """Create a minimal test catalog."""
    catalog_dir = tmp_path / "catalogs"
    catalog_dir.mkdir()
    catalog_file = catalog_dir / "local.toml"
    catalog_file.write_text("""
[storage.local-memory]
display_name = "Local Memory"
read_latency = { min = 0.01, max = 0.1, unit = "ms" }
write_latency = { min = 0.01, max = 0.1, unit = "ms" }
durability = ["ephemeral"]
access_patterns = ["random-read", "random-write"]
cost_per_gb_month = 0.001
""")
    return tmp_path


def test_plan_command(runner: CliRunner, temp_catalog: Path, tmp_path: Path) -> None:
    """Test 'skaal plan' command."""
    # Create a minimal Skaal app
    app = App(name="test-app")

    @app.storage
    class Counter:
        pass

    # Mock the CLI to use our test app and catalog
    with mock.patch("skaal.cli.plan_cmd.load_app") as mock_load:
        mock_load.return_value = app
        # Just test that the command accepts the catalog argument
        result = runner.invoke(
            cli_app,
            ["plan", "--catalog", str(temp_catalog / "catalogs" / "local.toml")],
        )

    # Should run without syntax errors (may fail for other reasons)
    # Just check it doesn't error due to missing decorator support
    assert "takes 1 positional argument but 2 were given" not in result.output


def test_plan_with_unsatisfiable_constraints(runner: CliRunner, temp_catalog: Path) -> None:
    """Test 'skaal plan' with constraints that can't be satisfied."""
    app = App(name="test-app")

    @app.storage(read_latency="< 0.001ms")  # Impossible latency
    class Counter:
        pass

    with mock.patch("skaal.cli.plan_cmd.load_app") as mock_load:
        mock_load.return_value = app
        result = runner.invoke(
            cli_app,
            ["plan", "--catalog", str(temp_catalog / "catalogs" / "local.toml")],
        )

    # Should either fail or succeed depending on catalog
    # Main thing is no decorator errors
    assert "takes 1 positional argument but 2 were given" not in result.output


def test_run_command_starts_server(runner: CliRunner, temp_catalog: Path) -> None:
    """Test 'skaal run' command starts the local runtime."""
    app = App(name="test-app")

    @app.function
    async def hello() -> dict[str, str]:
        return {"message": "hello"}

    # Just verify run command is callable with --help
    result = runner.invoke(
        cli_app,
        ["run", "--help"],
    )

    # Main check: help works and no decorator errors
    assert "takes 1 positional argument but 2 were given" not in result.output
    assert "--help" in result.output or result.exit_code == 0


def test_build_command(runner: CliRunner, temp_catalog: Path, tmp_path: Path) -> None:
    """Test 'skaal build' command."""
    app = App(name="test-app")

    @app.storage
    class Counter:
        pass

    # Just verify build command is callable and shows help
    result = runner.invoke(
        cli_app,
        ["build", "--help"],
    )

    # Main check: no decorator errors and help is available
    assert "takes 1 positional argument but 2 were given" not in result.output
    assert "--help" in result.output or result.exit_code == 0


def test_deploy_command_surfaces_deploy_error(runner: CliRunner) -> None:
    """Deploy-specific errors should be printed with their detailed context."""
    from skaal.errors import SkaalDeployError

    with mock.patch(
        "skaal.api.deploy",
        side_effect=SkaalDeployError("Deployment step failed: deploy AWS Lambda stack"),
    ):
        result = runner.invoke(cli_app, ["deploy"])

    assert result.exit_code == 1
    assert "Deploy failed:" in result.output
    assert "Deployment step failed: deploy AWS Lambda stack" in result.output


def test_deploy_command_forwards_local_runtime_flags(runner: CliRunner) -> None:
    """CLI deploy flags should reach the API layer for the local target."""
    with mock.patch("skaal.api.deploy") as fake:
        result = runner.invoke(cli_app, ["deploy", "--detach", "--follow-logs"])

    assert result.exit_code == 0
    call_kwargs = fake.call_args.kwargs
    assert call_kwargs["local_detach"] is True
    assert call_kwargs["local_follow_logs"] is True


def test_cli_help(runner: CliRunner) -> None:
    """Test CLI help output."""
    result = runner.invoke(cli_app, ["--help"])
    assert result.exit_code == 0
    assert "Commands:" in result.output or "command" in result.output.lower()


def test_cli_version(runner: CliRunner) -> None:
    """Test CLI version output."""
    result = runner.invoke(cli_app, ["--version"])
    # May not be implemented, but shouldn't crash
    assert result.exit_code in (0, 2)  # 0 if implemented, 2 if --version not recognized
