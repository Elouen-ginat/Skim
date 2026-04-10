"""`skaal migrate` — advance, rollback, or check 6-stage backend migrations."""

from __future__ import annotations

import typer

from skaal.cli._utils import get_app_name
from skaal.migrate.engine import STAGE_NAMES

app = typer.Typer(help="Manage backend migrations.")


@app.command("start")
def start(
    variable: str = typer.Option(
        ..., "--variable", help="Variable name to migrate, e.g. 'counter.Counts'."
    ),
    from_backend: str = typer.Option(
        ..., "--from", help="Source backend name, e.g. 'elasticache-redis'."
    ),
    to_backend: str = typer.Option(
        ..., "--to", help="Target backend name, e.g. 'dynamodb'."
    ),
) -> None:
    """Start a new migration for a storage variable."""
    from skaal import api

    try:
        api.migrate_start(
            variable, from_backend, to_backend, app_name=get_app_name()
        )
    except RuntimeError as exc:
        typer.echo(
            f"Error: {exc} Use `skaal migrate advance` or `skaal migrate rollback`.",
            err=True,
        )
        raise typer.Exit(1) from exc

    typer.echo(
        f"Migration started: {variable}  {from_backend} → {to_backend}\n"
        f"Stage: 1 ({STAGE_NAMES[1]})"
    )


@app.command("advance")
def advance(
    variable: str = typer.Option(
        ..., "--variable", help="Variable name to advance, e.g. 'counter.Counts'."
    ),
) -> None:
    """Advance a variable's migration to the next stage."""
    from skaal import api

    try:
        state = api.migrate_advance(variable, app_name=get_app_name())
    except RuntimeError as exc:
        typer.echo(
            f"Error: {exc} Use `skaal migrate start` first.", err=True
        )
        raise typer.Exit(1) from exc
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    stage_name = STAGE_NAMES.get(state.stage, "unknown")
    typer.echo(f"Advanced {variable} to stage {state.stage} ({stage_name})")


@app.command("status")
def status(
    variable: str = typer.Option(
        ..., "--variable", help="Variable name to check, e.g. 'counter.Counts'."
    ),
) -> None:
    """Show the current migration stage and readiness for the next stage."""
    from skaal import api

    state = api.migrate_status(variable, app_name=get_app_name())
    if state is None:
        typer.echo(f"No migration in progress for {variable!r}.")
        raise typer.Exit(0)

    stage_name = STAGE_NAMES.get(state.stage, "unknown")
    typer.echo(f"Variable:      {state.variable_name}")
    typer.echo(f"Migration:     {state.source_backend} → {state.target_backend}")
    typer.echo(f"Stage:         {state.stage} ({stage_name})")
    typer.echo(f"Started:       {state.started_at}")
    typer.echo(f"Last advanced: {state.advanced_at}")
    typer.echo(f"Discrepancies: {state.discrepancy_count}")
    typer.echo(f"Keys migrated: {state.keys_migrated}")


@app.command("rollback")
def rollback(
    variable: str = typer.Option(
        ..., "--variable", help="Variable name to roll back, e.g. 'counter.Counts'."
    ),
) -> None:
    """Roll back a migration to the previous stage."""
    from skaal import api

    try:
        state = api.migrate_rollback(variable, app_name=get_app_name())
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    stage_name = STAGE_NAMES.get(state.stage, "unknown")
    typer.echo(f"Rolled back {variable} to stage {state.stage} ({stage_name})")


@app.command("list")
def list_migrations() -> None:
    """List all pending and in-progress migrations."""
    from skaal import api

    all_states = api.migrate_list()
    if not all_states:
        typer.echo("No migrations found.")
        return

    header = (
        f"{'Variable':<30} {'From':<20} {'To':<20} {'Stage':<20} {'Discrepancies'}"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for state in all_states:
        stage_label = f"{state.stage} ({STAGE_NAMES.get(state.stage, '?')})"
        typer.echo(
            f"{state.variable_name:<30} {state.source_backend:<20} "
            f"{state.target_backend:<20} {stage_label:<20} {state.discrepancy_count}"
        )
