"""Shared helpers for CLI commands."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from skaal.app import App


def get_app_name() -> str:
    """Return the app name from plan.skaal.lock, falling back to the cwd name."""
    plan_path = Path("plan.skaal.lock")
    if plan_path.exists():
        try:
            from skaal.plan import PlanFile

            return PlanFile.read(plan_path).app_name
        except Exception:  # noqa: BLE001
            pass
    return Path.cwd().name


def load_app(module_app: str) -> "App":
    """
    Import and return the Skaal App object from a ``"module:variable"`` string.

    Adds the current directory to ``sys.path`` so bare module names resolve.
    Raises ``typer.Exit(1)`` on any import or attribute error.
    """
    import typer

    if ":" not in module_app:
        typer.echo(f"Error: expected 'module:variable', got {module_app!r}", err=True)
        raise typer.Exit(1)

    module_path, _, var_name = module_app.partition(":")

    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        typer.echo(f"Error: cannot import {module_path!r}: {exc}", err=True)
        raise typer.Exit(1) from exc

    obj = getattr(module, var_name, None)
    if obj is None:
        typer.echo(f"Error: {module_path!r} has no attribute {var_name!r}", err=True)
        raise typer.Exit(1)

    return obj
