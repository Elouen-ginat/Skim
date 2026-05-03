"""CLI surface for solver UNSAT (ADR 021) — exit code 2 + rendered diagnosis."""

from __future__ import annotations

import typer
from typer.testing import CliRunner

from skaal.cli._errors import cli_error_boundary
from skaal.errors import UnsatisfiableConstraints
from skaal.types.solver import CandidateReport, Diagnosis, Violation


def _fake_diagnosis() -> Diagnosis:
    sqlite = CandidateReport(
        backend_name="sqlite",
        display_name="SQLite",
        violations=(Violation("read_latency", "read_latency < 1ms", "≤ 5ms", 4.0),),
        cost=0.0,
    )
    return Diagnosis(
        resource_name="Profiles",
        resource_kind="storage",
        requested={"read_latency": "read_latency < 1ms"},
        candidates=(sqlite,),
        closest=sqlite,
        suggestion=None,
    )


def _build_runner(exc: UnsatisfiableConstraints) -> tuple[CliRunner, typer.Typer]:
    @cli_error_boundary
    def cmd() -> None:
        raise exc

    typer_app = typer.Typer()
    typer_app.command()(cmd)
    return CliRunner(), typer_app


def test_cli_error_boundary_translates_unsat_to_exit_code_2():
    """The boundary turns UnsatisfiableConstraints into typer.Exit(2)."""
    runner, typer_app = _build_runner(
        UnsatisfiableConstraints("Profiles", diagnosis=_fake_diagnosis())
    )
    result = runner.invoke(typer_app)
    assert result.exit_code == 2


def test_cli_error_boundary_renders_diagnosis_block():
    """Without a diagnosis, the CLI still falls back to the legacy short message."""
    runner, typer_app = _build_runner(UnsatisfiableConstraints("Profiles", "no backend"))
    result = runner.invoke(typer_app)
    assert result.exit_code == 2
