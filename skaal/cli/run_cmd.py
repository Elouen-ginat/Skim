"""`skaal run` — run the app locally."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import typer

app = typer.Typer(help="Run a Skim app locally.")


@app.callback(invoke_without_command=True)
def run(
    target: str = typer.Argument(
        ...,
        help="App to run as 'module:variable', e.g. 'examples.counter:app'.",
        metavar="MODULE:APP",
    ),
    host: str = typer.Option("127.0.0.1", "--host", "-H", help="Bind address."),
    port: int = typer.Option(8000, "--port", "-p", help="Port to listen on."),
    redis: str = typer.Option(
        "",
        "--redis",
        help="Use Redis backend with this URL, e.g. redis://localhost:6379.",
    ),
) -> None:
    """
    Run a Skim app locally.

    Starts an HTTP server where every @app.function() becomes a
    POST /{name} endpoint.  Storage is backed by in-memory LocalMap.

    Example:

        skaal run examples.counter:app
        curl -s localhost:8000/increment -d '{"name": "hits"}' | jq
    """
    if ":" not in target:
        typer.echo(
            f"Error: target must be 'module:variable', got {target!r}", err=True
        )
        raise typer.Exit(1)

    module_path, _, var_name = target.partition(":")

    # Make the current directory importable so `skaal run myapp:app` works.
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

    from skaal.local.runtime import LocalRuntime

    if redis:
        typer.echo(f"Using Redis backend: {redis}")
        runtime = LocalRuntime.from_redis(skim_app, redis_url=redis, host=host, port=port)
    else:
        runtime = LocalRuntime(skim_app, host=host, port=port)
    try:
        asyncio.run(runtime.serve())
    except KeyboardInterrupt:
        typer.echo("\nStopped.")
