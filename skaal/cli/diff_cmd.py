"""`skaal diff` — show infrastructure changes between plan versions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import typer

if TYPE_CHECKING:
    from skaal.api import PlanDiff
    from skaal.plan import PlanFile

app = typer.Typer(help="Show what changes between plan versions.")

_PLAN_DEFAULT = "plan.skaal.lock"


def _print_plan_summary(plan: "PlanFile") -> None:
    """Pretty-print a plan file without a second plan to diff against."""
    typer.echo(
        f"Plan: {plan.app_name}  (version {plan.version}, target={plan.deploy_target})"
    )
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
                f"  [was: {cspec.previous_instance_type}]"
                if cspec.previous_instance_type
                else ""
            )
            typer.echo(f"  ~ {name:<40} instance={cspec.instance_type}{prev}")
    else:
        typer.echo("Compute: (none)")


def _print_diff(plan_diff: "PlanDiff") -> None:
    """Print a structured diff between two plans."""
    old, new = plan_diff.old, plan_diff.new
    typer.echo(f"Diff: {old.app_name} v{old.version} → {new.app_name} v{new.version}")
    typer.echo("")

    for section, entries, key in (
        ("Storage", plan_diff.storage, "backend"),
        ("Compute", plan_diff.compute, "instance"),
    ):
        typer.echo(f"{section}:")
        if not entries:
            typer.echo("  (no changes)")
        else:
            for entry in entries:
                if entry.change == "added":
                    typer.echo(f"  + {entry.name:<40} {key}={entry.after}")
                elif entry.change == "removed":
                    typer.echo(f"  - {entry.name:<40} {key}={entry.before}")
                else:  # modified
                    typer.echo(
                        f"  ~ {entry.name:<40} {key}: {entry.before} → {entry.after}"
                    )
        typer.echo("")


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
    from skaal import api

    try:
        if module_app is None:
            from skaal.plan import PlanFile

            _print_plan_summary(PlanFile.read(plan))
            return

        plan_diff = api.diff(old_plan=plan, app=module_app)
    except FileNotFoundError as exc:
        typer.echo(f"Error: plan file not found: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (ValueError, ModuleNotFoundError, AttributeError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Error solving plan: {exc}", err=True)
        raise typer.Exit(1) from exc

    _print_diff(plan_diff)
