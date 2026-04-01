"""`skaal infra` — inspect, clean up, and catalog active infrastructure."""

from __future__ import annotations

import typer

app = typer.Typer(help="Manage active infrastructure.")


@app.command("status")
def status() -> None:
    """Show all active, decommissioning, and archived infrastructure."""
    raise NotImplementedError("`skim infra status` is not yet implemented (Phase 6).")


@app.command("cleanup")
def cleanup(
    variable: str = typer.Argument(..., help="Variable name to decommission."),
) -> None:
    """Remove orphaned infrastructure for the given variable."""
    raise NotImplementedError("`skim infra cleanup` is not yet implemented (Phase 6).")


@app.command("catalog")
def catalog() -> None:
    """Show all available backends from the loaded infrastructure catalogs."""
    raise NotImplementedError("`skim infra catalog` is not yet implemented (Phase 6).")
