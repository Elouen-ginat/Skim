"""`skim deploy` — generate deployment artifacts for the target infrastructure."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(help="Generate deployment artifacts for the app.")


@app.callback(invoke_without_command=True)
def deploy(
    target_app: Optional[str] = typer.Argument(
        None,
        help="App to deploy as 'module:variable', e.g. 'examples.counter:app'.",
        metavar="MODULE:APP",
    ),
    target: str = typer.Option(
        "aws",
        "--target",
        "-t",
        help="Deploy target: aws (Lambda + DynamoDB), k8s, ecs.",
    ),
    region: str = typer.Option(
        "us-east-1",
        "--region",
        "-r",
        help="AWS region for deployment.",
    ),
    out: Path = typer.Option(
        Path("artifacts"),
        "--out",
        "-o",
        help="Output directory for generated artifacts.",
    ),
    catalog: Path | None = typer.Option(
        None,
        "--catalog",
        help="Path to catalog TOML. Defaults to catalog/aws.toml.",
    ),
    preview: bool = typer.Option(False, "--preview", help="Dry run — show what would change."),
    rollback: bool = typer.Option(False, "--rollback", help="Roll back to the previous version."),
    version: int = typer.Option(2, "--version", help="Target version to deploy."),
) -> None:
    """
    Generate deployable artifacts for the app.

    For --target=aws this generates:
      - handler.py (Lambda entry point)
      - pulumi/__main__.py (Pulumi infrastructure stack)
      - pulumi/Pulumi.yaml
      - requirements.txt
      - README.md (deployment instructions)

    Example:

        skim deploy examples.counter:app --target=aws --out=artifacts/
    """
    if target_app is None:
        typer.echo("Error: missing required argument MODULE:APP.", err=True)
        typer.echo("  Example: skim deploy examples.counter:app --target=aws", err=True)
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

    from skim.plan import PLAN_FILE_NAME, PlanFile
    from skim.solver.solver import load_catalog, solve
    from skim.solver.storage import UnsatisfiableConstraints

    # Load or generate plan
    plan_path = Path(PLAN_FILE_NAME)
    solver_target = "aws-lambda" if target == "aws" else target

    if plan_path.exists() and not preview:
        typer.echo(f"Loading existing plan from {plan_path} ...")
        try:
            plan_file = PlanFile.read(plan_path)
        except Exception:
            typer.echo("Warning: could not read plan file, re-solving ...", err=True)
            plan_file = None
    else:
        plan_file = None

    if plan_file is None:
        try:
            cat = load_catalog(catalog)
        except FileNotFoundError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc

        typer.echo(f"Solving constraints for {skim_app.name!r} → target={solver_target!r} ...")
        try:
            plan_file = solve(skim_app, cat, target=solver_target)
        except UnsatisfiableConstraints as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc

        plan_file.write()
        typer.echo(f"Wrote {PLAN_FILE_NAME}")

    if target not in ("aws", "aws-lambda"):
        typer.echo(f"Error: deploy target {target!r} not yet supported. Use --target=aws.", err=True)
        raise typer.Exit(1)

    from skim.deploy.aws_lambda import generate_artifacts

    typer.echo(f"Generating artifacts in {out}/ ...")
    generated = generate_artifacts(
        app=skim_app,
        plan=plan_file,
        output_dir=out,
        source_module=module_path,
        app_var=var_name,
    )

    typer.echo(f"\nGenerated {len(generated)} files:")
    for path in generated:
        typer.echo(f"  {path}")

    typer.echo(f"\nSee {out}/README.md for deployment instructions.")
