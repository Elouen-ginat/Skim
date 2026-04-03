"""`skaal deploy` — generate deployment artifacts for the target infrastructure."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Annotated, Literal, Optional

import typer
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeploySettings(BaseSettings):
    """
    Environment-variable overrides for ``skaal deploy`` defaults.

    Any option can be set via a ``SKAAL_`` -prefixed environment variable so
    that CI pipelines and developer profiles don't need to repeat flags on
    every invocation::

        export SKAAL_TARGET=gcp
        export SKAAL_REGION=us-central1
        export SKAAL_OUT=dist/infra
        skaal deploy myapp:app          # picks up all three from env

    CLI flags always take precedence over environment variables.
    """

    model_config = SettingsConfigDict(env_prefix="SKAAL_", env_file=".skaal.env", extra="ignore")

    target: str = "aws"
    region: str = "us-east-1"
    out: Path = Path("artifacts")

    @field_validator("target")
    @classmethod
    def _valid_target(cls, v: str) -> str:
        known = {"aws", "aws-lambda", "gcp", "gcp-cloudrun"}
        if v not in known:
            raise ValueError(f"Unknown deploy target {v!r}. Known targets: {sorted(known)}.")
        return v


app = typer.Typer(help="Generate deployment artifacts for the app.")


@app.callback(invoke_without_command=True)
def deploy(
    target_app: Optional[str] = typer.Argument(
        None,
        help="App to deploy as 'module:variable', e.g. 'examples.counter:app'.",
        metavar="MODULE:APP",
    ),
    target: Optional[str] = typer.Option(
        None,
        "--target",
        "-t",
        help=(
            "Deploy target: aws (Lambda + DynamoDB), gcp (Cloud Run + Firestore). "
            "Env: SKAAL_TARGET."
        ),
    ),
    region: Optional[str] = typer.Option(
        None,
        "--region",
        "-r",
        help=(
            "Cloud region (e.g. us-east-1 for AWS, us-central1 for GCP). "
            "Env: SKAAL_REGION."
        ),
    ),
    out: Optional[Path] = typer.Option(
        None,
        "--out",
        "-o",
        help="Output directory for generated artifacts. Env: SKAAL_OUT.",
    ),
    catalog: Path | None = typer.Option(
        None,
        "--catalog",
        help="Path to catalog TOML. Defaults to catalogs/aws.toml.",
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

    For --target=gcp this generates:
      - main.py (Cloud Run entry point)
      - Dockerfile
      - pulumi/__main__.py (Pulumi infrastructure stack)
      - pulumi/Pulumi.yaml
      - requirements.txt
      - README.md (deployment instructions)

    Example:

        skaal deploy examples.counter:app --target=aws --out=artifacts/
        skaal deploy examples.counter:app --target=gcp --region=us-central1 --out=artifacts/
    """
    # Merge CLI flags with environment-variable defaults from DeploySettings.
    # Explicit CLI flags (non-None) win; env vars fill gaps.
    try:
        _settings = DeploySettings()
    except Exception as exc:
        typer.echo(f"Error in deploy settings (env vars): {exc}", err=True)
        raise typer.Exit(1) from exc

    if target is None:
        target = _settings.target
    if region is None:
        region = _settings.region
    if out is None:
        out = _settings.out

    if target_app is None:
        typer.echo("Error: missing required argument MODULE:APP.", err=True)
        typer.echo("  Example: skaal deploy examples.counter:app --target=aws", err=True)
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

    from skaal.plan import PLAN_FILE_NAME, PlanFile
    from skaal.solver.solver import load_catalog, solve
    from skaal.solver.storage import UnsatisfiableConstraints

    # Load or generate plan
    plan_path = Path(PLAN_FILE_NAME)
    if target == "aws":
        solver_target = "aws-lambda"
    elif target == "gcp":
        solver_target = "gcp-cloudrun"
    else:
        solver_target = target

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

    if target in ("aws", "aws-lambda"):
        from skaal.deploy.aws_lambda import generate_artifacts as _gen

        typer.echo(f"Generating artifacts in {out}/ ...")
        generated = _gen(
            app=skim_app,
            plan=plan_file,
            output_dir=out,
            source_module=module_path,
            app_var=var_name,
        )
    elif target in ("gcp", "gcp-cloudrun"):
        from skaal.deploy.gcp_cloudrun import generate_artifacts as _gen  # type: ignore[assignment]

        gcp_region = region if region != "us-east-1" else "us-central1"
        typer.echo(f"Generating artifacts in {out}/ ...")
        generated = _gen(
            app=skim_app,
            plan=plan_file,
            output_dir=out,
            source_module=module_path,
            app_var=var_name,
            region=gcp_region,
        )
    else:
        typer.echo(
            f"Error: deploy target {target!r} not yet supported. "
            "Use --target=aws or --target=gcp.",
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(f"\nGenerated {len(generated)} files:")
    for path in generated:
        typer.echo(f"  {path}")

    typer.echo(f"\nSee {out}/README.md for deployment instructions.")
