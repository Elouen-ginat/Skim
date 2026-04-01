"""PlanFile — the plan.skaal.lock format: read, write, and diff."""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

PLAN_FILE_NAME = "plan.skaal.lock"


@dataclass
class StorageSpec:
    variable_name: str
    backend: str
    previous_backend: str | None
    migration_plan: str | None
    migration_stage: int
    schema_hash: str
    instance_type: str | None = None
    nodes: int = 1
    storage_gb: int | None = None
    reason: str = ""


@dataclass
class ComputeSpec:
    function_name: str
    instance_type: str
    instances: int | str
    previous_instance_type: str | None
    scaling: str | None = None
    placement: str | None = None
    schedule: str | None = None
    reason: str = ""


@dataclass
class ComponentSpec:
    """Serializable spec for a provisioned or external component."""

    component_name: str
    kind: str               # "proxy" | "api-gateway" | "external-storage" | ...
    implementation: str | None  # e.g. "traefik", "kong"; None if solver selects
    provisioned: bool       # False for ExternalComponent subclasses
    connection_env: str | None  # env var carrying the DSN (external only)
    config: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass
class PlanFile:
    """Serializable representation of a `skaal plan` result."""

    app_name: str
    version: int
    previous_version: int | None
    deploy_target: str
    storage: dict[str, StorageSpec] = field(default_factory=dict)
    compute: dict[str, ComputeSpec] = field(default_factory=dict)
    components: dict[str, ComponentSpec] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    # ── Serialisation ──────────────────────────────────────────────────────

    def write(self, path: Path | None = None) -> Path:
        """Write the plan to plan.skaal.lock (or a custom path)."""
        target = path or Path(PLAN_FILE_NAME)
        target.write_text(json.dumps(self._to_dict(), indent=2))
        return target

    @classmethod
    def read(cls, path: Path | None = None) -> "PlanFile":
        """Read and parse a plan.skaal.lock file."""
        source = path or Path(PLAN_FILE_NAME)
        raw = json.loads(source.read_text())
        return cls._from_dict(raw)

    def _to_dict(self) -> dict[str, Any]:
        return {
            "app_name": self.app_name,
            "version": self.version,
            "previous_version": self.previous_version,
            "deploy_target": self.deploy_target,
            "storage": {k: asdict(v) for k, v in self.storage.items()},
            "compute": {k: asdict(v) for k, v in self.compute.items()},
            "components": {k: asdict(v) for k, v in self.components.items()},
            **self.extra,
        }

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> "PlanFile":
        storage = {k: StorageSpec(**v) for k, v in raw.get("storage", {}).items()}
        compute = {k: ComputeSpec(**v) for k, v in raw.get("compute", {}).items()}
        components = {k: ComponentSpec(**v) for k, v in raw.get("components", {}).items()}
        return cls(
            app_name=raw["app_name"],
            version=raw["version"],
            previous_version=raw.get("previous_version"),
            deploy_target=raw.get("deploy_target", "k8s"),
            storage=storage,
            compute=compute,
            components=components,
        )

    # ── Utilities ──────────────────────────────────────────────────────────

    @staticmethod
    def schema_hash(fields: dict[str, str]) -> str:
        """Stable hash of a schema dict (field name → type string)."""
        canonical = json.dumps(fields, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]
