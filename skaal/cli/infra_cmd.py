"""`skaal infra` — inspect and manage active infrastructure."""

from __future__ import annotations

from pathlib import Path

import typer

from skaal.cli._utils import get_app_name

app = typer.Typer(help="Manage active infrastructure.")


@app.command("status")
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
        typer.echo(
            f"No plan file found at {plan_path}. Run `skaal plan <module:app>` first.",
            err=True,
        )
        raise typer.Exit(1)

    try:
        snapshot = api.infra_status(plan_path)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Error reading plan: {exc}", err=True)
        raise typer.Exit(1) from exc

    plan_file = snapshot.plan
    typer.echo(f"App:     {plan_file.app_name}")
    typer.echo(f"Version: {plan_file.version}")
    typer.echo(f"Target:  {plan_file.deploy_target}")
    typer.echo("")

    if plan_file.storage:
        typer.echo(f"Storage ({len(plan_file.storage)} resources):")
        col = 36
        for name, spec in sorted(plan_file.storage.items()):
            migration_tag = ""
            info = snapshot.migrations.get(name)
            if info is not None:
                migration_tag = f"  [migrating: stage {info.stage} / {info.stage_name}]"
            typer.echo(f"  {name:<{col}} {spec.backend}{migration_tag}")
    else:
        typer.echo("Storage: (none)")

    typer.echo("")

    if plan_file.compute:
        typer.echo(f"Compute ({len(plan_file.compute)} functions):")
        for name, cspec in sorted(plan_file.compute.items()):
            typer.echo(f"  {name:<36} {cspec.instance_type}  ×{cspec.instances}")
    else:
        typer.echo("Compute: (none)")

    typer.echo("")

    if plan_file.components:
        typer.echo(f"Components ({len(plan_file.components)}):")
        for name, compspec in sorted(plan_file.components.items()):
            prov = "provisioned" if compspec.provisioned else "external"
            impl = compspec.implementation or "(solver-selected)"
            typer.echo(f"  {name:<30} [{compspec.kind}]  {impl}  ({prov})")
    else:
        typer.echo("Components: (none)")


@app.command("cleanup")
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
        typer.echo(f"No active migration found for {variable!r}.")
        raise typer.Exit(0)

    if state.stage < 6:
        typer.echo(
            f"Warning: migration for {variable!r} is at stage {state.stage} "
            f"(not yet complete). Cleaning up now will discard migration progress.",
            err=True,
        )

    if not confirm:
        confirmed = typer.confirm(
            f"Remove migration state for {variable!r}?", default=False
        )
        if not confirmed:
            typer.echo("Aborted.")
            raise typer.Exit(0)

    if api.infra_cleanup(variable, app_name=app_name):
        typer.echo(f"Removed migration state for {variable!r}.")
    else:
        typer.echo(f"Migration state file not found for {variable!r}.")
