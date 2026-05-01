"""`skaal deploy` — package and deploy previously-built artifacts.

Reads ``skaal-meta.json`` from the artifacts directory to detect the target
platform, then packages and runs ``pulumi up`` in one cross-platform step.

Works on Windows, macOS, and Linux — no shell scripts required.

Defaults are resolved from (highest to lowest priority):
  CLI flags > SKAAL_* env vars > .skaal.env > [tool.skaal] in pyproject.toml.

Example pyproject.toml::

    [tool.skaal]
    stack       = "prod"
    region      = "eu-west-1"
    gcp_project = "my-project"    # GCP only

Then::

    skaal deploy
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer

from skaal.cli._errors import cli_error_boundary

app = typer.Typer(help="Package and deploy previously-built artifacts.")
log = logging.getLogger("skaal.cli")


@app.callback(invoke_without_command=True)
@cli_error_boundary
def deploy(
    artifacts_dir: Path = typer.Option(
        Path("artifacts"),
        "--artifacts-dir",
        "-a",
        help="Path to the artifacts directory produced by `skaal build`.",
    ),
    stack: Optional[str] = typer.Option(
        None,
        "--stack",
        "-s",
        help="Pulumi stack name. Env: SKAAL_STACK. pyproject: tool.skaal.stack.",
    ),
    region: Optional[str] = typer.Option(
        None,
        "--region",
        "-r",
        help="Cloud region override. Env: SKAAL_REGION. pyproject: tool.skaal.region.",
    ),
    gcp_project: Optional[str] = typer.Option(
        None,
        "--gcp-project",
        help=(
            "GCP project ID (required for GCP target). "
            "Env: SKAAL_GCP_PROJECT. pyproject: tool.skaal.gcp_project."
        ),
    ),
    yes: bool = typer.Option(
        True,
        "--yes/--no-yes",
        help="Pass --yes to pulumi up (non-interactive).",
    ),
) -> None:
    """
    Package the app and deploy it using Pulumi.

    Reads ``skaal-meta.json`` from the artifacts directory to detect the
    target platform (AWS Lambda or GCP Cloud Run), then:

    \b
    AWS  — installs deps, packages handler.py + source, runs pulumi up.
    GCP  — runs pulumi up (infra), builds + pushes Docker image, runs pulumi up.

        Prerequisites:
            AWS: AWS credentials configured.
            GCP: Application Default Credentials configured and a reachable Docker daemon.
    """
    from skaal import api

    log.debug("Deploying artifacts from %s", artifacts_dir)
    api.deploy(
        artifacts_dir=artifacts_dir,
        stack=stack,
        region=region,
        gcp_project=gcp_project,
        yes=yes,
    )
