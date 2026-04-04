"""`skaal build` — solve constraints and generate deployment artifacts.

Produces a self-contained directory (default: ``artifacts/``) containing:

  AWS  — ``handler.py``, ``Pulumi.yaml``, ``requirements.txt``, ``README.md``,
          ``skaal-meta.json``
  GCP  — ``main.py``, ``Dockerfile``, ``Pulumi.yaml``, ``requirements.txt``,
          ``README.md``, ``skaal-meta.json``

Run ``skaal deploy`` afterwards to package and push to the cloud.

Defaults are resolved from (highest to lowest priority):
  CLI flags > SKAAL_* env vars > .skaal.env > [tool.skaal] in pyproject.toml.

Example pyproject.toml::

    [tool.skaal]
    app    = "mypackage.app:skaal_app"
    target = "aws"
    out    = "artifacts"

Then just::

    skaal build
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from skaal.cli._utils import load_app
from skaal.cli.config import SkaalSettings

# Supported --target values and their internal solver names.
_TARGET_MAP: dict[str, str] = {
    "aws": "aws-lambda",
    "aws-lambda": "aws-lambda",
    "gcp": "gcp-cloudrun",
    "gcp-cloudrun": "gcp-cloudrun",
    "local": "local",
    "local-compose": "local",
}

app = typer.Typer(help="Generate deployment artifacts for the app.")


@app.callback(invoke_without_command=True)
def build(
    module_app: Optional[str] = typer.Argument(
        None,
        help=(
            "App to build as 'module:variable', e.g. 'mypackage.app:skaal_app'. "
            "Falls back to 'app' in [tool.skaal] of pyproject.toml."
        ),
        metavar="MODULE:APP",
    ),
    target: Optional[str] = typer.Option(
        None,
        "--target",
        "-t",
        help=(
            "Deploy target: aws (Lambda), gcp (Cloud Run), or local (Docker Compose). "
            "Env: SKAAL_TARGET. pyproject: tool.skaal.target."
        ),
    ),
    region: Optional[str] = typer.Option(
        None,
        "--region",
        "-r",
        help="Cloud region (e.g. us-east-1, us-central1). Env: SKAAL_REGION.",
    ),
    out: Optional[Path] = typer.Option(
        None,
        "--out",
        "-o",
        help="Output directory for generated artifacts. Env: SKAAL_OUT.",
    ),
    catalog: Optional[Path] = typer.Option(
        None,
        "--catalog",
        help="Path to catalog TOML. Env: SKAAL_CATALOG.",
    ),
) -> None:
    """
    Solve constraints and generate deployable artifacts.

    Writes a self-contained artifacts directory that ``skaal deploy`` can
    pick up directly — no manual packaging or Pulumi commands needed.

    Supported targets:

    \b
      aws   — AWS Lambda + DynamoDB + API Gateway (Pulumi YAML)
      gcp   — GCP Cloud Run + Firestore/Redis/Postgres (Pulumi YAML + Dockerfile)
      local — Docker Compose (for local testing)
    """
    cfg = SkaalSettings()

    # CLI flags win; fall back to merged config (env > pyproject.toml > default).
    resolved_app = module_app or cfg.app
    resolved_target = target or cfg.target
    resolved_region = region or cfg.region
    resolved_out = out or cfg.out
    resolved_catalog = catalog or cfg.catalog

    if resolved_app is None:
        typer.echo(
            "Error: missing MODULE:APP.\n"
            "  Pass it as an argument: skaal build mypackage.app:skaal_app\n"
            "  Or set 'app' in [tool.skaal] of pyproject.toml.",
            err=True,
        )
        raise typer.Exit(1)

    solver_target = _TARGET_MAP.get(resolved_target)
    if solver_target is None:
        typer.echo(
            f"Error: unknown target {resolved_target!r}. "
            f"Supported values: {', '.join(_TARGET_MAP)}.",
            err=True,
        )
        raise typer.Exit(1)

    module_path, _, var_name = resolved_app.partition(":")
    skaal_app = load_app(resolved_app)

    from skaal.catalog.loader import load_catalog
    from skaal.plan import PLAN_FILE_NAME, PlanFile
    from skaal.solver.solver import solve
    from skaal.solver.storage import UnsatisfiableConstraints

    plan_path = Path(PLAN_FILE_NAME)
    plan_file = None

    if plan_path.exists():
        typer.echo(f"Loading existing plan from {plan_path} ...")
        try:
            plan_file = PlanFile.read(plan_path)
        except Exception:
            typer.echo("Warning: could not read plan file, re-solving ...", err=True)

    if plan_file is None:
        try:
            cat = load_catalog(resolved_catalog, target=resolved_target)
        except FileNotFoundError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc

        typer.echo(f"Solving constraints for {skaal_app.name!r} → target={solver_target!r} ...")
        try:
            plan_file = solve(skaal_app, cat, target=solver_target)
        except UnsatisfiableConstraints as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc

        plan_file.write()
        typer.echo(f"Wrote {PLAN_FILE_NAME}")

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
    else:  # local-compose
        from skaal.deploy.docker_compose import generate_artifacts as _gen_local

        generated = _gen_local(
            app=skaal_app,
            plan=plan_file,
            output_dir=resolved_out,
            source_module=module_path,
            app_var=var_name,
        )

    typer.echo(f"\nGenerated {len(generated)} files:")
    for path in generated:
        typer.echo(f"  {path}")

    if resolved_target in ("local", "local-compose"):
        typer.echo("\nRun `docker-compose up --build` to start the local deployment.")
    else:
        typer.echo(f"\nRun `skaal deploy` to push to {resolved_target.upper()}.")
