"""Unified settings used by the Skaal CLI and Python API.

Priority (highest to lowest):
  1. Keyword arguments passed at call time (or CLI flags)
  2. ``SKAAL_*`` environment variables
  3. ``.skaal.env`` file (optional)
  4. ``[tool.skaal]`` section in the nearest ``pyproject.toml``
  5. Built-in defaults

Per-stack overrides can be declared under ``[tool.skaal.stacks.<name>]``;
call :meth:`SkaalSettings.for_stack` to resolve them against the base
settings.

Example ``pyproject.toml``::

    [tool.skaal]
    app    = "mypackage.app:skaal_app"   # default MODULE:APP for build/plan/run
    target = "gcp"                        # aws | gcp
    region = "europe-west1"
    out    = "artifacts"
    stack  = "p-dev"

    [tool.skaal.stacks.p-dev]
    gcp_project = "my-dev-proj"

    [tool.skaal.stacks.p-ppr]
    gcp_project = "my-ppr-proj"

    [tool.skaal.stacks.p-prd]
    gcp_project = "my-prd-proj"
    region      = "europe-west4"
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
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


# ── Per-stack profile ─────────────────────────────────────────────────────────


class StackProfile(BaseModel):
    """Per-stack overrides layered on top of the base :class:`SkaalSettings`.

    Only the fields that may legitimately differ between stacks are exposed
    here.  Everything left as ``None`` falls through to the base value.

    ``overrides`` is a free-form dict of Pulumi config keys applied after
    the core project/region on every deploy — e.g.
    ``{"cloudRunMemory": "1Gi", "cloudRunMinInstances": 2}``.  Values are
    stringified before being passed to ``pulumi config set``.

    ``deletion_protection`` is a shortcut that expands at deploy time into
    ``sqlDeletionProtection<ClassName>`` overrides for every
    ``cloud-sql-postgres`` storage in the plan.  Set ``True`` on production
    stacks to make Cloud SQL instances undeletable.
    """

    model_config = {"extra": "forbid"}

    target: str | None = None
    region: str | None = None
    catalog: Path | None = None
    gcp_project: str | None = None
    enable_mesh: bool | None = None
    overrides: dict[str, str | int | bool] = Field(default_factory=dict)
    deletion_protection: bool | None = None
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Literal environment variables baked into the compute container.",
    )
    invokers: list[str] = Field(
        default_factory=list,
        description=(
            "IAM members allowed to invoke the service. "
            "Defaults to ``['allUsers']`` (public) when the list is empty."
        ),
    )
    labels: dict[str, str] = Field(
        default_factory=dict,
        description="Labels applied to supporting resources (Cloud Run, SQL, Redis).",
    )
    pre_deploy: list[list[str]] = Field(
        default_factory=list,
        description=(
            "Commands to run before ``pulumi up``. Each entry is an argv list, "
            'e.g. [["skaal", "migrate", "advance", "cache"]].'
        ),
    )
    post_deploy: list[list[str]] = Field(
        default_factory=list,
        description=(
            "Commands to run after a successful deploy. Each entry is an argv "
            "list; Pulumi outputs are exported as SKAAL_OUTPUT_<KEY> env vars."
        ),
    )


# ── Unified settings model ────────────────────────────────────────────────────


class SkaalSettings(BaseSettings):
    """
    Merged settings used by every ``skaal`` sub-command and Python API call.

    Values are resolved in priority order: explicit argument > env var >
    ``.skaal.env`` > ``pyproject.toml`` > default.  Both the CLI commands and
    the :mod:`skaal.api` functions read this once and apply it only where the
    caller did not pass an explicit value.
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
    enable_mesh: bool = Field(
        default=False,
        description="Include the skaal-mesh runtime dependency in generated deploy artifacts.",
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
    overrides: dict[str, str | int | bool] = Field(
        default_factory=dict,
        description=(
            "Raw Pulumi config overrides applied on every deploy. "
            "Usually populated from a stack profile rather than the top level."
        ),
    )
    deletion_protection: bool | None = Field(
        default=None,
        description=(
            "Shortcut that expands into sqlDeletionProtection<Class> overrides "
            "for every cloud-sql-postgres storage in the plan at deploy time."
        ),
    )
    env: dict[str, str] = Field(default_factory=dict)
    invokers: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    pre_deploy: list[list[str]] = Field(default_factory=list)
    post_deploy: list[list[str]] = Field(default_factory=list)

    # ── Stack profiles ────────────────────────────────────────────────────────
    stacks: dict[str, StackProfile] = Field(
        default_factory=dict,
        description=(
            "Per-stack overrides keyed by stack name. Populated from "
            "``[tool.skaal.stacks.<name>]`` in pyproject.toml. "
            "Call :meth:`for_stack` to resolve them against the base settings."
        ),
    )

    # ── Stack resolution ──────────────────────────────────────────────────────
    def for_stack(self, name: str | None = None) -> "SkaalSettings":
        """Return a new :class:`SkaalSettings` with the *name* profile applied.

        Passing ``None`` or a stack name that has no profile returns a copy of
        ``self`` unchanged (so callers can call this unconditionally).  When a
        profile exists, any non-``None`` field on the profile wins over the
        base setting, and the resolved ``stack`` field is set to *name*.
        """
        resolved_name = name if name is not None else self.stack
        profile = self.stacks.get(resolved_name)

        updates: dict[str, Any] = {"stack": resolved_name}
        if profile is not None:
            for field_name, value in profile.model_dump(exclude_none=True).items():
                updates[field_name] = value

        return self.model_copy(update=updates)

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
