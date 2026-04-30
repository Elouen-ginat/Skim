"""`skaal destroy` — destroy previously-deployed Pulumi resources."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer

from skaal.cli._errors import cli_error_boundary

app = typer.Typer(help="Destroy previously-deployed Pulumi resources.")
log = logging.getLogger("skaal.cli")


@app.callback(invoke_without_command=True)
@cli_error_boundary
def destroy(
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
    yes: bool = typer.Option(
        True,
        "--yes/--no-yes",
        help="Pass --yes to pulumi destroy (non-interactive).",
    ),
) -> None:
    """Destroy the app resources tracked by the Pulumi stack."""
    from skaal import api

    log.debug("Destroying stack from %s", artifacts_dir)
    api.destroy(
        artifacts_dir=artifacts_dir,
        stack=stack,
        yes=yes,
    )
