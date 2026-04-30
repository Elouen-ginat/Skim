"""`skaal migrate` — advance, rollback, or check 6-stage backend migrations."""

from __future__ import annotations

import logging

import typer

from skaal.cli._errors import cli_error_boundary
from skaal.cli._utils import get_app_name
from skaal.migrate.engine import MigrationStage

app = typer.Typer(help="Manage backend migrations.")
log = logging.getLogger("skaal.cli")


@app.command("start")
@cli_error_boundary
def start(
    variable: str = typer.Option(
        ..., "--variable", help="Variable name to migrate, e.g. 'counter.Counts'."
    ),
    from_backend: str = typer.Option(
        ..., "--from", help="Source backend name, e.g. 'elasticache-redis'."
    ),
    to_backend: str = typer.Option(..., "--to", help="Target backend name, e.g. 'dynamodb'."),
) -> None:
    """Start a new migration for a storage variable."""
    from skaal import api

    try:
        api.migrate_start(variable, from_backend, to_backend, app_name=get_app_name())
    except RuntimeError as exc:
        raise ValueError(
            f"{exc} Use `skaal migrate advance` or `skaal migrate rollback`."
        )

    log.info(
        f"Migration started: {variable}  {from_backend} → {to_backend}\n"
        f"Stage: {MigrationStage.SHADOW_WRITE} ({MigrationStage.SHADOW_WRITE.name.lower()})"
    )


@app.command("advance")
@cli_error_boundary
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
        raise ValueError(f"{exc} Use `skaal migrate start` first.") from exc

    log.info("Advanced %s to stage %s (%s)", variable, state.stage, state.stage.name.lower())


@app.command("status")
@cli_error_boundary
def status(
    variable: str = typer.Option(
        ..., "--variable", help="Variable name to check, e.g. 'counter.Counts'."
    ),
) -> None:
    """Show the current migration stage and readiness for the next stage."""
    from skaal import api

    state = api.migrate_status(variable, app_name=get_app_name())
    if state is None:
        log.info("No migration in progress for %r.", variable)
        raise typer.Exit(0)

    log.info("Variable:      %s", state.variable_name)
    log.info("Migration:     %s → %s", state.source_backend, state.target_backend)
    log.info("Stage:         %s (%s)", state.stage, state.stage.name.lower())
    log.info("Started:       %s", state.started_at)
    log.info("Last advanced: %s", state.advanced_at)
    log.info("Discrepancies: %s", state.discrepancy_count)
    log.info("Keys migrated: %s", state.keys_migrated)


@app.command("rollback")
@cli_error_boundary
def rollback(
    variable: str = typer.Option(
        ..., "--variable", help="Variable name to roll back, e.g. 'counter.Counts'."
    ),
) -> None:
    """Roll back a migration to the previous stage."""
    from skaal import api

    state = api.migrate_rollback(variable, app_name=get_app_name())

    log.info("Rolled back %s to stage %s (%s)", variable, state.stage, state.stage.name.lower())


@app.command("list")
@cli_error_boundary
def list_migrations() -> None:
    """List all pending and in-progress migrations."""
    from skaal import api

    all_states = api.migrate_list()
    if not all_states:
        log.info("No migrations found.")
        return

    header = f"{'Variable':<30} {'From':<20} {'To':<20} {'Stage':<20} {'Discrepancies'}"
    log.info(header)
    log.info("-" * len(header))
    for state in all_states:
        stage_label = f"{state.stage} ({state.stage.name.lower()})"
        log.info(
            f"{state.variable_name:<30} {state.source_backend:<20} "
            f"{state.target_backend:<20} {stage_label:<20} {state.discrepancy_count}"
        )
