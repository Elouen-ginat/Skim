"""`skaal build` — generate deployment artifacts from plan.skaal.lock.

Reads the lock file produced by ``skaal plan`` and writes a self-contained
artifacts directory (default: ``artifacts/``) containing:

  AWS   — ``handler.py``, ``Pulumi.yaml``, ``pyproject.toml``, ``skaal-meta.json``
  GCP   — ``main.py``, ``Dockerfile``, ``Pulumi.yaml``, ``pyproject.toml``, ``skaal-meta.json``
  local — ``main.py``, ``Dockerfile``, ``docker-compose.yml``, ``pyproject.toml``, ``skaal-meta.json``

Run ``skaal plan MODULE:APP --target TARGET`` first to produce the lock file,
then ``skaal deploy`` afterwards to push to the cloud.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from skaal.cli._utils import load_app
from skaal.cli.config import SkaalSettings

app = typer.Typer(help="Generate deployment artifacts from plan.skaal.lock.")


@app.callback(invoke_without_command=True)
def build(
    region: Optional[str] = typer.Option(
        None,
        "--region",
        "-r",
        help="Cloud region override (e.g. us-east-1, us-central1). Env: SKAAL_REGION.",
    ),
    out: Optional[Path] = typer.Option(
        None,
        "--out",
        "-o",
        help="Output directory for generated artifacts. Env: SKAAL_OUT.",
    ),
    dev: bool = typer.Option(
        False,
        "--dev",
        help=(
            "Bundle the local skaal source into the artifact so the Docker image "
            "uses your working copy instead of the PyPI release. "
            "Useful when developing skaal itself."
        ),
    ),
) -> None:
    """
    Generate deployable artifacts from ``plan.skaal.lock``.

    The lock file is the single source of truth — target, source module, and
    backend assignments are all read from it.  Run ``skaal plan`` first if the
    lock file does not exist or needs to be updated.

    Supported targets (determined by the lock file):

    \b
      aws   — AWS Lambda + DynamoDB + API Gateway (Pulumi YAML)
      gcp   — GCP Cloud Run + Firestore/Redis/Postgres (Pulumi YAML + Dockerfile)
      local — Docker Compose (for local testing)
    """
    from skaal.plan import PLAN_FILE_NAME, PlanFile

    cfg = SkaalSettings()
    resolved_region = region or cfg.region
    resolved_out = out or cfg.out

    plan_path = Path(PLAN_FILE_NAME)
    if not plan_path.exists():
        typer.echo(
            f"Error: {PLAN_FILE_NAME} not found.\n"
            "  Run `skaal plan MODULE:APP --target TARGET` first.",
            err=True,
        )
        raise typer.Exit(1)

    try:
        plan_file = PlanFile.read(plan_path)
    except Exception as exc:
        typer.echo(f"Error: could not parse {PLAN_FILE_NAME}: {exc}", err=True)
        raise typer.Exit(1) from exc

    if not plan_file.source_module:
        typer.echo(
            f"Error: {PLAN_FILE_NAME} is missing source_module — it was created by an "
            "older version of skaal.\n"
            "  Re-run `skaal plan MODULE:APP --target TARGET` to regenerate it.",
            err=True,
        )
        raise typer.Exit(1)

    from skaal.deploy.registry import get_target

    try:
        target = get_target(plan_file.deploy_target)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    module_path = plan_file.source_module
    var_name = plan_file.app_var
    skaal_app = load_app(f"{module_path}:{var_name}")

    typer.echo(f"Building from {plan_path} (target={plan_file.deploy_target!r}) ...")
    typer.echo(f"Generating artifacts in {resolved_out}/ ...")

    generated = target.generate_artifacts(
        app=skaal_app,
        plan=plan_file,
        output_dir=resolved_out,
        source_module=module_path,
        app_var=var_name,
        region=resolved_region or None,
        dev=dev,
    )

    typer.echo(f"\nGenerated {len(generated)} files:")
    for path in generated:
        typer.echo(f"  {path}")

    if target.name == "local":
        typer.echo("\nRun `skaal deploy` to start the local stack.")
    else:
        typer.echo(f"\nRun `skaal deploy` to push to {plan_file.deploy_target.upper()}.")
