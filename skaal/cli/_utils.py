"""Shared helpers for CLI commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from skaal.app import App


def get_app_name() -> str:
    """Return the app name from ``plan.skaal.lock``, falling back to cwd name."""
    from skaal.api import _current_app_name

    return _current_app_name()


def load_app(module_app: str) -> "App":
    """CLI-facing wrapper around :func:`skaal.api.load_app`."""

    from skaal import api

    return api.load_app(module_app)
