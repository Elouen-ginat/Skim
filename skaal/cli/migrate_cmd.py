"""`skaal migrate` — advance, rollback, or check 6-stage backend migrations."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from skaal.cli._utils import get_app_name
from skaal.migrate.engine import STAGE_NAMES, MigrationEngine

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
    app_name = get_app_name()
    engine = MigrationEngine(app_name, variable)

    existing = engine.load_state()
    if existing is not None and existing.stage < 6:
        typer.echo(
            f"Error: migration for {variable!r} already in progress "
            f"(stage {existing.stage}: {STAGE_NAMES.get(existing.stage, '?')}). "
            f"Use `skaal migrate advance` or `skaal migrate rollback`.",
            err=True,
        )
        raise typer.Exit(1)

    state = engine.start(from_backend, to_backend)
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
    app_name = get_app_name()
    engine = MigrationEngine(app_name, variable)

    state = engine.load_state()
    if state is None:
        typer.echo(
            f"Error: no migration in progress for {variable!r}. "
            "Use `skaal migrate start` first.",
            err=True,
        )
        raise typer.Exit(1)

    try:
        state = engine.advance(state)
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
    app_name = get_app_name()
    engine = MigrationEngine(app_name, variable)

    state = engine.load_state()
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
    app_name = get_app_name()
    engine = MigrationEngine(app_name, variable)

    state = engine.load_state()
    if state is None:
        typer.echo(
            f"Error: no migration in progress for {variable!r}.",
            err=True,
        )
        raise typer.Exit(1)

    try:
        state = engine.rollback(state)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    stage_name = STAGE_NAMES.get(state.stage, "unknown")
    typer.echo(f"Rolled back {variable} to stage {state.stage} ({stage_name})")


@app.command("list")
def list_migrations() -> None:
    """List all pending and in-progress migrations."""
    app_name = get_app_name()
    # List migrations across all apps under .skaal/migrations/
    base_dir = Path(".skaal/migrations")
    if not base_dir.exists():
        typer.echo("No migrations found.")
        return

    all_states = []
    for app_dir in sorted(base_dir.iterdir()):
        if not app_dir.is_dir():
            continue
        engine = MigrationEngine(app_dir.name, "__probe__")
        # Use list_all on a representative engine
        import json as _json

        for path in sorted(app_dir.glob("*.json")):
            try:
                from skaal.migrate.engine import MigrationState

                data = _json.loads(path.read_text())
                all_states.append(MigrationState(**data))
            except Exception:  # noqa: BLE001
                pass

    if not all_states:
        typer.echo("No migrations found.")
        return

    # Print header
    header = f"{'Variable':<30} {'From':<20} {'To':<20} {'Stage':<20} {'Discrepancies'}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for state in all_states:
        stage_label = f"{state.stage} ({STAGE_NAMES.get(state.stage, '?')})"
        typer.echo(
            f"{state.variable_name:<30} {state.source_backend:<20} "
            f"{state.target_backend:<20} {stage_label:<20} {state.discrepancy_count}"
        )
