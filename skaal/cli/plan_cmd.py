"""`skaal plan` — run constraint solver, generate plan.skaal.lock."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from skaal.cli._utils import load_app
from skaal.cli.config import SkaalSettings

app = typer.Typer(help="Run the constraint solver and generate a plan.")


@app.callback(invoke_without_command=True)
def plan(
    target_app: Optional[str] = typer.Argument(
        None,
        help=(
            "App to plan as 'module:variable', e.g. 'examples.counter:app'. "
            "Falls back to 'app' in [tool.skaal] of pyproject.toml."
        ),
        metavar="MODULE:APP",
    ),
    target: Optional[str] = typer.Option(
        None,
        "--target",
        "-t",
        help=(
            "Deploy target: aws, gcp, aws-lambda, gcp-cloudrun, k8s, ecs. "
            "Env: SKAAL_TARGET. pyproject: tool.skaal.target."
        ),
    ),
    catalog: Optional[Path] = typer.Option(
        None,
        "--catalog",
        help="Path to catalog TOML. Env: SKAAL_CATALOG. pyproject: tool.skaal.catalog.",
    ),
    reoptimize: bool = typer.Option(
        False, "--reoptimize", help="Force re-solving all backend choices."
    ),
    pin: list[str] = typer.Option(
        [], "--pin", help="Pin a variable to a backend, e.g. profiles=redis."
    ),
) -> None:
    """
    Analyze the app's constraints via Z3 and write plan.skaal.lock.

    Solver output includes: backend selections, instance types, placement rules,
    estimated cost, and UNSAT explanations if constraints cannot be met.

    Example:

        skaal plan examples.counter:app --target aws
    """
    cfg = SkaalSettings()

    resolved_app = target_app or cfg.app
    resolved_target = target or cfg.target
    resolved_catalog = catalog or cfg.catalog

    if resolved_app is None:
        typer.echo(
            "Error: missing MODULE:APP.\n"
            "  Pass it as an argument: skaal plan mypackage.app:skaal_app\n"
            "  Or set 'app' in [tool.skaal] of pyproject.toml.",
            err=True,
        )
        raise typer.Exit(1)

    skim_app = load_app(resolved_app)

    from skaal.catalog.loader import load_catalog
    from skaal.solver.solver import solve
    from skaal.solver.storage import UnsatisfiableConstraints

    try:
        cat = load_catalog(resolved_catalog, target=resolved_target)
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"Solving constraints for {skim_app.name!r} → target={resolved_target!r} ...")

    try:
        plan_file = solve(skim_app, cat, target=resolved_target)
    except UnsatisfiableConstraints as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    out_path = plan_file.write()
    typer.echo(f"Wrote {out_path}\n")

    # Print a human-readable table
    col_w = [30, 25, 60]
    header = (
        f"| {'Storage variable':<{col_w[0]}} | {'Backend':<{col_w[1]}} | {'Reason':<{col_w[2]}} |"
    )
    sep = f"+-{'-' * col_w[0]}-+-{'-' * col_w[1]}-+-{'-' * col_w[2]}-+"
    typer.echo(sep)
    typer.echo(header)
    typer.echo(sep)
    for spec in plan_file.storage.values():
        typer.echo(
            f"| {spec.variable_name:<{col_w[0]}} | {spec.backend:<{col_w[1]}} | {spec.reason[:col_w[2]]:<{col_w[2]}} |"
        )
    typer.echo(sep)

    if plan_file.compute:
        typer.echo("\nCompute assignments:")
        for cspec in plan_file.compute.values():
            typer.echo(f"  {cspec.function_name}: {cspec.instance_type}  ({cspec.reason})")
