"""`skaal run` — run the app locally."""

from __future__ import annotations

from typing import Optional

import typer

from skaal.cli.config import SkaalSettings

app = typer.Typer(help="Run a Skaal app locally.")


@app.callback(invoke_without_command=True)
def run(
    target: Optional[str] = typer.Argument(
        None,
        help=(
            "App to run as 'module:variable', e.g. 'examples.counter:app'. "
            "Falls back to 'app' in [tool.skaal] of pyproject.toml."
        ),
        metavar="MODULE:APP",
    ),
    host: str = typer.Option("127.0.0.1", "--host", "-H", help="Bind address."),
    port: int = typer.Option(8000, "--port", "-p", help="Port to listen on."),
    redis: str = typer.Option(
        "",
        "--redis",
        help="Use Redis backend with this URL, e.g. redis://localhost:6379.",
    ),
    persist: bool = typer.Option(
        False, "--persist", help="Use SQLite for persistent local storage."
    ),
    db: str = typer.Option("skaal_local.db", "--db", help="SQLite database path (with --persist)."),
    plan: Optional[str] = typer.Option(
        None,
        "--plan",
        help="Use a solved plan file and local fallbacks to wire runtime backends.",
    ),
    distributed: bool = typer.Option(
        False,
        "--distributed",
        help="Use the Rust mesh runtime for distributed execution (requires skaal[mesh]).",
    ),
) -> None:
    """
    Run a Skaal app locally.

    Starts an HTTP server where every @app.function() becomes a
    POST /{name} endpoint.  Storage is backed by in-memory LocalMap.

    Example:

        skaal run examples.counter:app
        skaal run examples.counter:app --persist
        skaal run examples.counter:app --distributed
        curl -s localhost:8000/increment -d '{"name": "hits"}' | jq
    """
    from skaal import api

    resolved_app = target or SkaalSettings().app
    if resolved_app is None:
        typer.echo(
            "Error: missing MODULE:APP.\n"
            "  Pass it as an argument: skaal run mypackage.app:skaal_app\n"
            "  Or set 'app' in [tool.skaal] of pyproject.toml.",
            err=True,
        )
        raise typer.Exit(1)

    if distributed:
        typer.echo("Using mesh runtime")
    elif plan:
        typer.echo(f"Using solved plan: {plan}")
    elif redis:
        typer.echo(f"Using Redis backend: {redis}")
    elif persist:
        typer.echo(f"Using SQLite backend: {db}")

    try:
        api.run(
            resolved_app,
            host=host,
            port=port,
            redis=redis or None,
            persist=persist,
            db=db,
            distributed=distributed,
            plan=plan,
        )
    except (ValueError, ModuleNotFoundError, AttributeError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:
        typer.echo("\nStopped.")
