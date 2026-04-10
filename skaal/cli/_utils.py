"""Shared helpers for CLI commands.

The core logic lives in :mod:`skaal.api`.  These helpers only wrap API calls
with the CLI-specific concerns: turning exceptions into ``typer.Exit(1)`` and
formatting error messages.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from skaal.app import App


def get_app_name() -> str:
    """Return the app name from ``plan.skaal.lock``, falling back to cwd name."""
    from skaal.api import _current_app_name

    return _current_app_name()


def load_app(module_app: str) -> "App":
    """CLI-facing wrapper around :func:`skaal.api.load_app`.

    Converts any import/resolution failure into ``typer.Exit(1)`` with a
    user-friendly error message.
    """
    import typer

    from skaal import api

    try:
        return api.load_app(module_app)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except ModuleNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except AttributeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
