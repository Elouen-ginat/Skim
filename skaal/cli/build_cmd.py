"""`skaal build` — generate deployment artifacts from plan.skaal.lock.

Reads the lock file produced by ``skaal plan`` and writes a self-contained
artifacts directory (default: ``artifacts/``) containing:

  AWS   — ``handler.py``, ``Pulumi.yaml``, ``pyproject.toml``, ``skaal-meta.json``
  GCP   — ``main.py``, ``Dockerfile``, ``Pulumi.yaml``, ``pyproject.toml``, ``skaal-meta.json``
    local — ``main.py``, ``Dockerfile``, ``Pulumi.yaml``, ``pyproject.toml``, ``skaal-meta.json``

Run ``skaal plan MODULE:APP --target TARGET`` first to produce the lock file,
then ``skaal deploy`` afterwards to push to the cloud.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

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
    stack: Optional[str] = typer.Option(
        None,
        "--stack",
        "-s",
        help=(
            "Stack profile to resolve per-stack settings against "
            "([tool.skaal.stacks.<name>]). Env: SKAAL_STACK."
        ),
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
            local — Docker + Pulumi (for local testing)
    """
    from skaal import api
    from skaal.deploy.registry import get_target
    from skaal.plan import PLAN_FILE_NAME

    cfg = SkaalSettings().for_stack(stack)
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

    typer.echo(f"Building from {plan_path} ...")

    try:
        generated = api.build(
            plan=plan_path,
            output_dir=resolved_out,
            region=resolved_region,
            stack=stack,
            dev=dev,
        )
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except ValueError as exc:
        # Unknown deploy target or missing source_module in the lock file.
        msg = str(exc)
        if "source_module" in msg:
            typer.echo(
                f"Error: {PLAN_FILE_NAME} is missing source_module — it was created by an "
                "older version of skaal.\n"
                "  Re-run `skaal plan MODULE:APP --target TARGET` to regenerate it.",
                err=True,
            )
        else:
            typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Error: could not build from {PLAN_FILE_NAME}: {exc}", err=True)
        raise typer.Exit(1) from exc

    # Read the plan once more just to show the deploy target in the banner.
    from skaal.plan import PlanFile

    plan_file = PlanFile.read(plan_path)

    typer.echo(f"Generating artifacts in {resolved_out}/ ...")
    typer.echo(f"\nGenerated {len(generated)} files:")
    for path in generated:
        typer.echo(f"  {path}")

    target_adapter = get_target(plan_file.deploy_target)
    if target_adapter.name == "local":
        typer.echo("\nRun `skaal deploy` to start the local stack.")
    else:
        typer.echo(f"\nRun `skaal deploy` to push to {plan_file.deploy_target.upper()}.")
