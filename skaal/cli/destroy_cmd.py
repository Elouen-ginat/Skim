"""`skaal destroy` — destroy previously-deployed Pulumi resources."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(help="Destroy previously-deployed Pulumi resources.")


@app.callback(invoke_without_command=True)
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

    try:
        api.destroy(
            artifacts_dir=artifacts_dir,
            stack=stack,
            yes=yes,
        )
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Destroy failed: {exc}", err=True)
        raise typer.Exit(1) from exc
