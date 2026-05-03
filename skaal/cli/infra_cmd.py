"""`skaal infra` — inspect and manage active infrastructure."""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from skaal.cli._errors import cli_error_boundary
from skaal.cli._utils import get_app_name

app = typer.Typer(help="Manage active infrastructure.")
log = logging.getLogger("skaal.cli")


@app.command("status")
@cli_error_boundary
def status(
    plan: str = typer.Option(
        "plan.skaal.lock",
        "--plan",
        help="Path to the plan lock file.",
    ),
) -> None:
    """Show all active infrastructure from the current plan file."""
    from skaal import api

    plan_path = Path(plan)
    if not plan_path.exists():
        raise FileNotFoundError(
            f"No plan file found at {plan_path}. Run `skaal plan <module:app>` first."
        )

    snapshot = api.infra_status(plan_path)

    plan_file = snapshot.plan
    log.info("App:     %s", plan_file.app_name)
    log.info("Version: %s", plan_file.version)
    log.info("Target:  %s", plan_file.deploy_target)
    log.info("")

    if plan_file.storage:
        log.info("Storage (%s resources):", len(plan_file.storage))
        col = 36
        for name, spec in sorted(plan_file.storage.items()):
            migration_tag = ""
            info = snapshot.migrations.get(name)
            if info is not None:
                migration_tag = f"  [migrating: stage {info.stage} / {info.stage_name}]"
            log.info(f"  {name:<{col}} {spec.backend}{migration_tag}")
    else:
        log.info("Storage: (none)")

    log.info("")

    if plan_file.compute:
        log.info("Compute (%s functions):", len(plan_file.compute))
        for name, cspec in sorted(plan_file.compute.items()):
            log.info(f"  {name:<36} {cspec.instance_type}  ×{cspec.instances}")
    else:
        log.info("Compute: (none)")

    log.info("")

    if plan_file.components:
        log.info("Components (%s):", len(plan_file.components))
        for name, compspec in sorted(plan_file.components.items()):
            prov = "provisioned" if compspec.provisioned else "external"
            impl = compspec.implementation or "(solver-selected)"
            log.info(f"  {name:<30} [{compspec.kind}]  {impl}  ({prov})")
    else:
        log.info("Components: (none)")


@app.command("cleanup")
@cli_error_boundary
def cleanup(
    variable: str = typer.Option(
        ...,
        "--variable",
        "-v",
        help="Qualified variable name to decommission, e.g. 'counter.Counts'.",
    ),
    confirm: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt.",
    ),
) -> None:
    """Remove migration state and mark a variable as decommissioned."""
    from skaal import api
    from skaal.migrate.engine import MigrationEngine

    app_name = get_app_name()
    engine = MigrationEngine(app_name, variable)
    state = engine.load_state()

    if state is None:
        log.info("No active migration found for %r.", variable)
        raise typer.Exit(0)

    if state.stage < 6:
        log.warning(
            "migration for %r is at stage %s (not yet complete). Cleaning up now will "
            "discard migration progress.",
            variable,
            state.stage,
        )

    if not confirm:
        confirmed = typer.confirm(f"Remove migration state for {variable!r}?", default=False)
        if not confirmed:
            log.info("Aborted.")
            raise typer.Exit(0)

    if api.infra_cleanup(variable, app_name=app_name):
        log.info("Removed migration state for %r.", variable)
    else:
        log.info("Migration state file not found for %r.", variable)
