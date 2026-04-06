"""PlanFile — the plan.skaal.lock format: read, write, and diff."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

PLAN_FILE_NAME = "plan.skaal.lock"


class StorageSpec(BaseModel):
    variable_name: str
    backend: str
    previous_backend: str | None = None
    migration_plan: str | None = None
    migration_stage: int = 0
    schema_hash: str = ""
    instance_type: str | None = None
    nodes: int = 1
    storage_gb: int | None = None
    reason: str = ""
    # Provisioning parameters sourced from [storage.<backend>.deploy] in the
    # catalog TOML.  Not used by the constraint solver; only by deploy generators.
    deploy_params: dict[str, Any] = Field(default_factory=dict)


class ComputeSpec(BaseModel):
    function_name: str
    instance_type: str
    instances: int | str
    previous_instance_type: str | None = None
    scaling: str | None = None
    placement: str | None = None
    schedule: str | None = None
    reason: str = ""


class ComponentSpec(BaseModel):
    """Serialisable spec for a provisioned or external component."""

    component_name: str
    kind: str  # "proxy" | "api-gateway" | "external-storage" | ...
    implementation: str | None = None  # e.g. "traefik", "kong"; None if solver selects
    provisioned: bool = True  # False for ExternalComponent subclasses
    connection_env: str | None = None  # env var carrying the DSN (external only)
    config: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class PlanFile(BaseModel):
    """Serialisable representation of a ``skaal plan`` result."""

    app_name: str
    version: int = 1
    previous_version: int | None = None
    deploy_target: str = "local"
    # Source location — set by `skaal plan`, consumed by `skaal build`.
    source_module: str = ""
    app_var: str = "app"
    storage: dict[str, StorageSpec] = Field(default_factory=dict)
    compute: dict[str, ComputeSpec] = Field(default_factory=dict)
    components: dict[str, ComponentSpec] = Field(default_factory=dict)
    # Target-level deploy parameters sourced from [compute.<target>.deploy] in
    # the catalog (e.g. Lambda memory/timeout, Cloud Run memory/cpu).
    # Not used by the constraint solver; only by deploy generators.
    deploy_config: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)

    # ── Serialisation ──────────────────────────────────────────────────────

    def write(self, path: Path | None = None) -> Path:
        """Write the plan to ``plan.skaal.lock`` (or a custom path)."""
        target = path or Path(PLAN_FILE_NAME)
        target.write_text(self.model_dump_json(indent=2))
        return target

    @classmethod
    def read(cls, path: Path | None = None) -> "PlanFile":
        """Read and parse a ``plan.skaal.lock`` file."""
        source = path or Path(PLAN_FILE_NAME)
        return cls.model_validate_json(source.read_text())

    # ── Utilities ──────────────────────────────────────────────────────────

    @staticmethod
    def schema_hash(fields: dict[str, str]) -> str:
        """Stable hash of a schema dict (field name → type string)."""
        canonical = json.dumps(fields, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]
