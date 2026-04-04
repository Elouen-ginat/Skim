"""Unified settings for the Skaal CLI.

Priority (highest to lowest):
  1. CLI flags passed on the command line
  2. ``SKAAL_*`` environment variables
  3. ``.skaal.env`` file (optional)
  4. ``[tool.skaal]`` section in the nearest ``pyproject.toml``
  5. Built-in defaults

Example ``pyproject.toml``::

    [tool.skaal]
    app    = "mypackage.app:skaal_app"   # default MODULE:APP for build/plan/run
    target = "aws"                        # aws | gcp
    region = "eu-west-1"
    out    = "artifacts"
    stack  = "prod"
    # gcp_project = "my-gcp-project"     # required for GCP deploys
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# ── pyproject.toml discovery ──────────────────────────────────────────────────


def find_pyproject() -> Path | None:
    """Walk up from cwd until a ``pyproject.toml`` is found, or return None."""
    for directory in [Path.cwd(), *Path.cwd().parents]:
        candidate = directory / "pyproject.toml"
        if candidate.exists():
            return candidate
    return None


def load_skaal_section() -> dict[str, Any]:
    """Return the ``[tool.skaal]`` dict from the nearest pyproject.toml, or {}."""
    path = find_pyproject()
    if path is None:
        return {}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data.get("tool", {}).get("skaal", {})


# ── pydantic-settings custom source ───────────────────────────────────────────


class PyprojectTomlSource(PydanticBaseSettingsSource):
    """pydantic-settings source that reads ``[tool.skaal]`` from pyproject.toml."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._data: dict[str, Any] = load_skaal_section()

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        value = self._data.get(field_name)
        return value, field_name, False

    def field_is_complex(self, field: Any) -> bool:
        return False

    def __call__(self) -> dict[str, Any]:
        # Only return keys that are actually declared on the settings model.
        known = set(self.settings_cls.model_fields)
        return {k: v for k, v in self._data.items() if k in known}


# ── Unified settings model ────────────────────────────────────────────────────


class SkaalSettings(BaseSettings):
    """
    Merged settings used by every ``skaal`` sub-command.

    Values are resolved in priority order: CLI flag > env var > pyproject.toml
    > default.  CLI commands read this once and apply it only where the user
    did not pass an explicit flag.
    """

    model_config = SettingsConfigDict(
        env_prefix="SKAAL_",
        env_file=".skaal.env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Shared ────────────────────────────────────────────────────────────────
    app: str | None = Field(
        default=None,
        description="Default MODULE:APP used when no positional argument is given.",
    )
    target: str = Field(
        default="aws",
        description="Deploy target: aws or gcp.",
    )
    region: str = Field(
        default="us-east-1",
        description="Cloud region.",
    )

    # ── Build ─────────────────────────────────────────────────────────────────
    out: Path = Field(
        default=Path("artifacts"),
        description="Output directory for generated artifacts.",
    )
    catalog: Path | None = Field(
        default=None,
        description="Path to catalog TOML.",
    )

    # ── Deploy ────────────────────────────────────────────────────────────────
    stack: str = Field(
        default="dev",
        description="Pulumi stack name.",
    )
    gcp_project: str | None = Field(
        default=None,
        description="GCP project ID (required for GCP target).",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Insert pyproject.toml between dotenv and the built-in field defaults.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            PyprojectTomlSource(settings_cls),
        )
