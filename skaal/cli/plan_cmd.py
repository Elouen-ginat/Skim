"""`skaal plan` — run constraint solver, generate plan.skaal.lock."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(help="Run the constraint solver and generate a plan.")


@app.callback(invoke_without_command=True)
def plan(
    target_app: Optional[str] = typer.Argument(
        None,
        help="App to plan as 'module:variable', e.g. 'examples.counter:app'.",
        metavar="MODULE:APP",
    ),
    target: str = typer.Option(
        "generic",
        "--target",
        "-t",
        help="Deploy target: generic, aws-lambda, k8s, ecs.",
    ),
    catalog: Optional[Path] = typer.Option(
        None,
        "--catalog",
        help="Path to catalog TOML. Defaults to catalogs/aws.toml.",
    ),
    reoptimize: bool = typer.Option(False, "--reoptimize", help="Force re-solving all backend choices."),
    pin: list[str] = typer.Option([], "--pin", help="Pin a variable to a backend, e.g. profiles=redis."),
) -> None:
    """
    Analyze the app's constraints via Z3 and write plan.skaal.lock.

    Solver output includes: backend selections, instance types, placement rules,
    estimated cost, and UNSAT explanations if constraints cannot be met.

    Example:

        skaal plan examples.counter:app --target aws-lambda
    """
    if target_app is None:
        typer.echo("Error: missing required argument MODULE:APP.", err=True)
        typer.echo("  Example: skaal plan examples.counter:app", err=True)
        raise typer.Exit(1)

    if ":" not in target_app:
        typer.echo(
            f"Error: target must be 'module:variable', got {target_app!r}", err=True
        )
        raise typer.Exit(1)

    module_path, _, var_name = target_app.partition(":")

    # Make the current directory importable.
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        typer.echo(f"Error: cannot import {module_path!r}: {exc}", err=True)
        raise typer.Exit(1) from exc

    skim_app = getattr(module, var_name, None)
    if skim_app is None:
        typer.echo(
            f"Error: {module_path!r} has no attribute {var_name!r}", err=True
        )
        raise typer.Exit(1)

    from skaal.catalog.loader import load_catalog
    from skaal.solver.solver import solve
    from skaal.solver.storage import UnsatisfiableConstraints

    try:
        cat = load_catalog(catalog)
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"Solving constraints for {skim_app.name!r} → target={target!r} ...")

    try:
        plan_file = solve(skim_app, cat, target=target)
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
        for spec in plan_file.compute.values():
            typer.echo(f"  {spec.function_name}: {spec.instance_type}  ({spec.reason})")
