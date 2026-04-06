"""`skaal build` — generate deployment artifacts from plan.skaal.lock.

Reads the lock file produced by ``skaal plan`` and writes a self-contained
artifacts directory (default: ``artifacts/``) containing:

  AWS  — ``handler.py``, ``Pulumi.yaml``, ``requirements.txt``, ``skaal-meta.json``
  GCP  — ``main.py``, ``Dockerfile``, ``Pulumi.yaml``, ``requirements.txt``, ``skaal-meta.json``
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

# Mapping from lock file deploy_target values to internal generator keys.
_TARGET_MAP: dict[str, str] = {
    "aws": "aws-lambda",
    "aws-lambda": "aws-lambda",
    "gcp": "gcp-cloudrun",
    "gcp-cloudrun": "gcp-cloudrun",
    "local": "local",
    "local-compose": "local",
}

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

    solver_target = _TARGET_MAP.get(plan_file.deploy_target)
    if solver_target is None:
        typer.echo(
            f"Error: unknown deploy_target {plan_file.deploy_target!r} in {PLAN_FILE_NAME}.\n"
            f"  Supported values: {', '.join(_TARGET_MAP)}.",
            err=True,
        )
        raise typer.Exit(1)

    module_path = plan_file.source_module
    var_name = plan_file.app_var
    skaal_app = load_app(f"{module_path}:{var_name}")

    typer.echo(f"Building from {plan_path} (target={plan_file.deploy_target!r}) ...")
    typer.echo(f"Generating artifacts in {resolved_out}/ ...")

    if solver_target == "aws-lambda":
        from skaal.deploy.aws_lambda import generate_artifacts as _gen_aws

        generated = _gen_aws(
            app=skaal_app,
            plan=plan_file,
            output_dir=resolved_out,
            source_module=module_path,
            app_var=var_name,
        )
    elif solver_target == "gcp-cloudrun":
        from skaal.deploy.gcp_cloudrun import generate_artifacts as _gen_gcp

        gcp_region = resolved_region if resolved_region != "us-east-1" else "us-central1"
        generated = _gen_gcp(
            app=skaal_app,
            plan=plan_file,
            output_dir=resolved_out,
            source_module=module_path,
            app_var=var_name,
            region=gcp_region,
        )
    else:  # local
        from skaal.deploy.docker_compose import generate_artifacts as _gen_local

        generated = _gen_local(
            app=skaal_app,
            plan=plan_file,
            output_dir=resolved_out,
            source_module=module_path,
            app_var=var_name,
            dev=dev,
        )

    typer.echo(f"\nGenerated {len(generated)} files:")
    for path in generated:
        typer.echo(f"  {path}")

    if solver_target == "local":
        typer.echo("\nRun `skaal deploy` to start the local stack.")
    else:
        typer.echo(f"\nRun `skaal deploy` to push to {plan_file.deploy_target.upper()}.")
