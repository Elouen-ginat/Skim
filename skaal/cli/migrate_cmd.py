"""`skaal migrate` — advance, rollback, or check 6-stage backend migrations."""

from __future__ import annotations

from typing import Optional

import typer

app = typer.Typer(help="Manage backend migrations.")


@app.command("advance")
def advance(
    variable: str = typer.Argument(..., help="Variable name to migrate, e.g. 'profiles'."),
    stage: int = typer.Option(..., "--stage", help="Target migration stage (1–5)."),
) -> None:
    """Advance a variable's migration to the specified stage."""
    raise NotImplementedError("`skim migrate advance` is not yet implemented (Phase 5).")


@app.command("status")
def status(
    variable: str = typer.Argument(..., help="Variable name to check."),
) -> None:
    """Show the current migration stage and readiness for the next stage."""
    raise NotImplementedError("`skim migrate status` is not yet implemented (Phase 5).")


@app.command("rollback")
def rollback(
    variable: str = typer.Argument(..., help="Variable name to roll back."),
) -> None:
    """Roll back a migration to the previous stage or backend."""
    raise NotImplementedError("`skim migrate rollback` is not yet implemented (Phase 5).")


@app.command("list")
def list_migrations() -> None:
    """List all pending and in-progress migrations."""
    raise NotImplementedError("`skim migrate list` is not yet implemented (Phase 5).")
