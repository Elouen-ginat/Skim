"""PlanFile — the plan.skaal.lock format: read, write, and diff."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from skaal.types.secret import SecretSpec

PLAN_FILE_NAME = "plan.skaal.lock"

PatternType = Literal["event-log", "projection", "saga", "outbox"]


class StorageSpec(BaseModel):
    variable_name: str
    backend: str
    kind: str = "kv"
    previous_backend: str | None = None
    migration_plan: str | None = None
    migration_stage: int = 0
    schema_hash: str = ""
    instance_type: str | None = None
    nodes: int = 1
    storage_gb: int | None = None
    reason: str = ""
    # Qualified resource name this storage is co-located with (from the
    # declarative ``collocate_with=`` argument).  Used by deploy generators to
    # pin placement.
    collocate_with: str | None = None
    # Hint that the solver should consider alternative (cheaper) backends on
    # re-plan if metrics suggest the constraints can be relaxed.
    auto_optimize: bool = False
    # Provisioning parameters sourced from [storage.<backend>.deploy] in the
    # catalog TOML.  Not used by the constraint solver; only by deploy generators.
    deploy_params: dict[str, Any] = Field(default_factory=dict)
    # Wire metadata sourced from [storage.<backend>.wire] in the catalog TOML.
    # Tells deploy generators which Python class to instantiate and how.
    # Not used by the constraint solver.
    wire_params: dict[str, Any] = Field(default_factory=dict)


class ComputeSpec(BaseModel):
    function_name: str
    instance_type: str
    instances: int | str
    previous_instance_type: str | None = None
    scaling: str | None = None
    placement: str | None = None
    schedule: str | None = None
    reason: str = ""
    # Qualified resource name this function is co-located with (from
    # ``@function(compute=Compute(collocate_with=...))``).  Deploy generators
    # use this to pin the function to the same region/zone/cluster.
    collocate_with: str | None = None
    # Scale strategy from ``@scale(...)`` — one of "round-robin",
    # "partition-by-key", "broadcast", "race", "competing-consumer".
    scale_strategy: str | None = None
    # Resilience policies — serialised so deploy generators can wrap the
    # function with retry, circuit-breaker, rate-limit, and bulkhead middleware.
    retry: dict[str, Any] | None = None
    circuit_breaker: dict[str, Any] | None = None
    rate_limit: dict[str, Any] | None = None
    bulkhead: dict[str, Any] | None = None


class PatternSpec(BaseModel):
    """Serialisable spec for a distributed system pattern (EventLog, Saga, …)."""

    pattern_name: str
    pattern_type: PatternType
    # For event-log / outbox: the selected backing storage backend (e.g. "msk-kafka")
    backend: str | None = None
    # Human-readable reason explaining the solver's choices
    reason: str = ""
    # Free-form config: saga steps, projection handler, outbox channel, …
    config: dict[str, Any] = Field(default_factory=dict)


class ComponentSpec(BaseModel):
    """Serialisable spec for a provisioned or external component."""

    component_name: str
    kind: str  # "proxy" | "api-gateway" | "external-storage" | ...
    implementation: str | None = None  # e.g. "traefik", "kong"; None if solver selects
    provisioned: bool = True  # False for ExternalComponent subclasses
    secret_name: str | None = None  # references PlanFile.secrets[<name>] (external only)
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
    patterns: dict[str, PatternSpec] = Field(default_factory=dict)
    secrets: dict[str, SecretSpec] = Field(default_factory=dict)
    # Topological order of resources produced by the dependency graph — used
    # by deploy generators to respect collocate_with ordering at provision time.
    resource_order: list[str] = Field(default_factory=list)
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
