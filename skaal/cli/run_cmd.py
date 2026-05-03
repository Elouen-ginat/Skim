"""`skaal run` — run the app locally."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer

from skaal.cli._errors import cli_error_boundary
from skaal.cli.config import SkaalSettings
from skaal.types.cli import ReloadMode

app = typer.Typer(
    help="Run a Skaal app locally.",
    context_settings={"allow_interspersed_args": True},
)
log = logging.getLogger("skaal.cli")


@app.callback(invoke_without_command=True)
@cli_error_boundary
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
    distributed: bool = typer.Option(
        False,
        "--distributed",
        help="Use the Rust mesh runtime for distributed execution (requires skaal[mesh]).",
    ),
    node_id: str = typer.Option("node-0", "--node-id", help="Mesh node ID (with --distributed)."),
    reload: Optional[bool] = typer.Option(
        None,
        "--reload/--no-reload",
        help="Hot-reload on source change. Defaults to on for interactive dev.",
    ),
    reload_dir: list[Path] = typer.Option(
        [],
        "--reload-dir",
        help="Directory to watch (repeatable). Defaults to the project root.",
    ),
) -> None:
    """
    Run a Skaal app locally.

    Starts an HTTP server where every @app.function() becomes a
    POST /{name} endpoint.  Storage is backed by in-memory LocalMap.

    Hot-reload is on by default when stdout is a TTY and SKAAL_ENV is unset
    or 'dev' / 'local' / 'development'.  Pass ``--no-reload`` to disable.

    Example:

        skaal run examples.counter:app
        skaal run examples.counter:app --persist
        skaal run examples.counter:app --distributed
        curl -s localhost:8000/increment -d '{"name": "hits"}' | jq
    """
    from skaal import api
    from skaal.cli import _reload

    resolved_app = target or SkaalSettings().app
    if resolved_app is None:
        raise ValueError(
            "missing MODULE:APP.\n"
            "  Pass it as an argument: skaal run mypackage.app:skaal_app\n"
            "  Or set 'app' in [tool.skaal] of pyproject.toml."
        )

    mode: ReloadMode = "on" if reload is True else "off" if reload is False else "auto"
    if _reload.resolve_reload(mode):
        argv_tail = _argv_tail(
            target=resolved_app,
            host=host,
            port=port,
            redis=redis,
            persist=persist,
            db=db,
            distributed=distributed,
            node_id=node_id,
        )
        dirs = reload_dir or _reload.default_reload_dirs()
        raise typer.Exit(_reload.supervise(_reload.child_command(argv_tail), dirs))

    if distributed:
        log.info("Using mesh runtime (node: %s)", node_id)
    elif redis:
        log.info("Using Redis backend: %s", redis)
    elif persist:
        log.info("Using SQLite backend: %s", db)

    try:
        api.run(
            resolved_app,
            host=host,
            port=port,
            redis=redis or None,
            persist=persist,
            db=db,
            distributed=distributed,
            node_id=node_id,
        )
    except KeyboardInterrupt:
        log.info("Stopped.")


def _argv_tail(
    *,
    target: str,
    host: str,
    port: int,
    redis: str,
    persist: bool,
    db: str,
    distributed: bool,
    node_id: str,
) -> list[str]:
    """Forward flags onto the supervised child process."""
    argv: list[str] = [target, "--host", host, "--port", str(port), "--db", db, "--node-id", node_id]
    if redis:
        argv += ["--redis", redis]
    if persist:
        argv.append("--persist")
    if distributed:
        argv.append("--distributed")
    return argv
