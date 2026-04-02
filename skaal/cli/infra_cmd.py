"""`skaal infra` — inspect, clean up, and catalog active infrastructure."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

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
    p = Path(plan)
    if not p.exists():
        typer.echo(
            f"No plan file found at {p}. Run `skaal plan <module:app>` first.",
            err=True,
        )
        raise typer.Exit(1)

    from skaal.plan import PlanFile

    try:
        plan_file = PlanFile.read(p)
    except Exception as exc:
        typer.echo(f"Error reading plan: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"App:     {plan_file.app_name}")
    typer.echo(f"Version: {plan_file.version}")
    typer.echo(f"Target:  {plan_file.deploy_target}")
    typer.echo("")

    if plan_file.storage:
        typer.echo(f"Storage ({len(plan_file.storage)} resources):")
        col = 36
        for name, spec in sorted(plan_file.storage.items()):
            migration_tag = ""
            if spec.previous_backend and spec.previous_backend != spec.backend:
                from skaal.migrate.engine import MigrationEngine, STAGE_NAMES
                try:
                    engine = MigrationEngine(plan_file.app_name, name)
                    state = engine.load_state()
                    if state:
                        stage_name = STAGE_NAMES.get(state.stage, "?")
                        migration_tag = f"  [migrating: stage {state.stage} / {stage_name}]"
                except Exception:  # noqa: BLE001
                    pass
            typer.echo(f"  {name:<{col}} {spec.backend}{migration_tag}")
    else:
        typer.echo("Storage: (none)")

    typer.echo("")

    if plan_file.compute:
        typer.echo(f"Compute ({len(plan_file.compute)} functions):")
        for name, spec in sorted(plan_file.compute.items()):
            typer.echo(f"  {name:<36} {spec.instance_type}  ×{spec.instances}")
    else:
        typer.echo("Compute: (none)")

    typer.echo("")

    if plan_file.components:
        typer.echo(f"Components ({len(plan_file.components)}):")
        for name, spec in sorted(plan_file.components.items()):
            prov = "provisioned" if spec.provisioned else "external"
            impl = spec.implementation or "(solver-selected)"
            typer.echo(f"  {name:<30} [{spec.kind}]  {impl}  ({prov})")
    else:
        typer.echo("Components: (none)")


@app.command("cleanup")
def cleanup(
    variable: str = typer.Option(
        ..., "--variable", "-v",
        help="Qualified variable name to decommission, e.g. 'counter.Counts'.",
    ),
    confirm: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip confirmation prompt.",
    ),
) -> None:
    """Remove migration state and mark a variable as decommissioned."""
    from skaal.migrate.engine import MigrationEngine

    app_name = _get_app_name()
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

    state_path = engine._state_path
    if state_path.exists():
        state_path.unlink()
        typer.echo(f"Removed migration state for {variable!r}.")
    else:
        typer.echo(f"Migration state file not found for {variable!r}.")


@app.command("catalog")
def catalog(
    catalog_path: Optional[Path] = typer.Option(
        None, "--catalog", help="Path to catalog TOML. Defaults to catalogs/aws.toml."
    ),
    section: str = typer.Option(
        "all",
        "--section",
        "-s",
        help="Section to display: all, storage, compute, network.",
    ),
) -> None:
    """Show all available backends from the loaded infrastructure catalog."""
    from skaal.catalog.loader import load_typed_catalog

    try:
        cat = load_typed_catalog(catalog_path)
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    if section in ("all", "storage") and cat.storage:
        typer.echo(f"Storage backends ({len(cat.storage)}):")
        typer.echo(
            f"  {'Name':<28} {'Display':<32} {'Read lat':<12} {'Durability':<22} {'$/GB/mo'}"
        )
        typer.echo(f"  {'-'*28} {'-'*32} {'-'*12} {'-'*22} {'-'*8}")
        for name, spec in sorted(cat.storage.items()):
            dur = ", ".join(spec.durability)
            lat = f"{spec.read_latency.min}–{spec.read_latency.max}ms"
            typer.echo(
                f"  {name:<28} {spec.display_name:<32} {lat:<12} {dur:<22} ${spec.cost_per_gb_month:.3f}"
            )
        typer.echo("")

    if section in ("all", "compute") and cat.compute:
        typer.echo(f"Compute backends ({len(cat.compute)}):")
        typer.echo(
            f"  {'Name':<28} {'Display':<32} {'vCPUs':<8} {'Memory':<10} {'$/hr'}"
        )
        typer.echo(f"  {'-'*28} {'-'*32} {'-'*8} {'-'*10} {'-'*8}")
        for name, spec in sorted(cat.compute.items()):
            typer.echo(
                f"  {name:<28} {spec.display_name:<32} {spec.vcpus:<8} {spec.memory_gb:<10.1f} ${spec.cost_per_hour:.4f}"
            )
        typer.echo("")

    if section in ("all", "network") and cat.network:
        typer.echo(f"Network backends ({len(cat.network)}):")
        for name, spec in sorted(cat.network.items()):
            typer.echo(f"  {name}: {spec.display_name}")
        typer.echo("")

    if not cat.storage and not cat.compute and not cat.network:
        typer.echo("Catalog is empty.")


def _get_app_name() -> str:
    """Load app name from plan.skaal.lock, fall back to directory name."""
    plan_path = Path("plan.skaal.lock")
    if plan_path.exists():
        try:
            from skaal.plan import PlanFile
            plan = PlanFile.read(plan_path)
            return plan.app_name
        except Exception:  # noqa: BLE001
            pass
    return Path.cwd().name
