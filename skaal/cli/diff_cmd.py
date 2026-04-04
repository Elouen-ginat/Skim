"""`skaal diff` — show infrastructure changes between plan versions."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from skaal.plan import PlanFile

import typer

from skaal.cli._utils import load_app

app = typer.Typer(help="Show what changes between plan versions.")

_PLAN_DEFAULT = "plan.skaal.lock"


def _load_plan(plan_path: str) -> "PlanFile":
    from skaal.plan import PlanFile

    p = Path(plan_path)
    if not p.exists():
        typer.echo(f"Error: plan file not found: {p}", err=True)
        raise typer.Exit(1)
    return PlanFile.read(p)


def _print_plan_summary(plan: "PlanFile") -> None:
    """Pretty-print the contents of a plan without a second plan to diff against."""
    typer.echo(f"Plan: {plan.app_name}  (version {plan.version}, target={plan.deploy_target})")
    typer.echo("")

    if plan.storage:
        typer.echo("Storage:")
        for name, spec in sorted(plan.storage.items()):
            prev = f"  [was: {spec.previous_backend}]" if spec.previous_backend else ""
            typer.echo(f"  ~ {name:<40} backend={spec.backend}{prev}")
    else:
        typer.echo("Storage: (none)")

    typer.echo("")

    if plan.compute:
        typer.echo("Compute:")
        for name, cspec in sorted(plan.compute.items()):
            prev = (
                f"  [was: {cspec.previous_instance_type}]" if cspec.previous_instance_type else ""
            )
            typer.echo(f"  ~ {name:<40} instance={cspec.instance_type}{prev}")
    else:
        typer.echo("Compute: (none)")


def _diff_plans(old: "PlanFile", new: "PlanFile") -> None:
    """Print a structured diff between old and new plans."""
    typer.echo(f"Diff: {old.app_name} v{old.version} → {new.app_name} v{new.version}")
    typer.echo("")

    # ── Storage diff ──────────────────────────────────────────────────────
    typer.echo("Storage:")
    old_storage = set(old.storage)
    new_storage = set(new.storage)

    added = new_storage - old_storage
    removed = old_storage - new_storage
    common = old_storage & new_storage

    changed = False
    for name in sorted(added):
        spec = new.storage[name]
        typer.echo(f"  + {name:<40} backend={spec.backend}")
        changed = True
    for name in sorted(removed):
        spec = old.storage[name]
        typer.echo(f"  - {name:<40} backend={spec.backend}")
        changed = True
    for name in sorted(common):
        old_spec = old.storage[name]
        new_spec = new.storage[name]
        if old_spec.backend != new_spec.backend:
            typer.echo(f"  ~ {name:<40} backend: {old_spec.backend} → {new_spec.backend}")
            changed = True

    if not changed:
        typer.echo("  (no changes)")

    typer.echo("")

    # ── Compute diff ──────────────────────────────────────────────────────
    typer.echo("Compute:")
    old_compute = set(old.compute)
    new_compute = set(new.compute)

    added_c = new_compute - old_compute
    removed_c = old_compute - new_compute
    common_c = old_compute & new_compute

    changed_c = False
    for name in sorted(added_c):
        cspec = new.compute[name]
        typer.echo(f"  + {name:<40} instance={cspec.instance_type}")
        changed_c = True
    for name in sorted(removed_c):
        cspec = old.compute[name]
        typer.echo(f"  - {name:<40} instance={cspec.instance_type}")
        changed_c = True
    for name in sorted(common_c):
        old_cspec = old.compute[name]
        new_cspec = new.compute[name]
        if old_cspec.instance_type != new_cspec.instance_type:
            typer.echo(
                f"  ~ {name:<40} instance: {old_cspec.instance_type} → {new_cspec.instance_type}"
            )
            changed_c = True

    if not changed_c:
        typer.echo("  (no changes)")


@app.callback(invoke_without_command=True)
def diff(
    module_app: Optional[str] = typer.Argument(
        None,
        help="Optional 'module:app' to re-solve and diff against current plan.",
        metavar="MODULE:APP",
    ),
    plan: str = typer.Option(
        _PLAN_DEFAULT,
        "--plan",
        help="Path to the plan lock file.",
    ),
) -> None:
    """
    Show what would change between the current plan and a freshly-solved plan.

    Without MODULE:APP, pretty-prints the existing plan file.
    With MODULE:APP, re-solves and diffs the result against the current plan.
    """
    existing_plan = _load_plan(plan)

    if module_app is None:
        # No app provided — just pretty-print the existing plan
        _print_plan_summary(existing_plan)
        return

    skim_app = load_app(module_app)

    try:
        from skaal.catalog.loader import load_catalog
        from skaal.solver.solver import solve

        catalog = load_catalog()
        new_plan = solve(skim_app, catalog, target=existing_plan.deploy_target)
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Error solving plan: {exc}", err=True)
        raise typer.Exit(1) from exc

    _diff_plans(existing_plan, new_plan)
