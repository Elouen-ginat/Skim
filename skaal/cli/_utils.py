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


def resolve_app_ref() -> "App":
    """Resolve and wire the configured app for runtime-aware CLI commands.

    Reads ``MODULE:APP`` from CLI settings (env / pyproject), loads it, and
    instantiates a :class:`~skaal.runtime.local.LocalRuntime` so storage
    backends are bound to relational SQLModel classes.
    """
    from skaal import api
    from skaal.cli.config import SkaalSettings
    from skaal.runtime.local import LocalRuntime

    cfg = SkaalSettings()
    if cfg.app is None:
        raise ValueError(
            "missing MODULE:APP. Set 'app' in [tool.skaal] of pyproject.toml "
            "or export SKAAL_APP=module:app."
        )
    skaal_app = api.resolve_app(cfg.app)
    LocalRuntime(skaal_app)
    return skaal_app
