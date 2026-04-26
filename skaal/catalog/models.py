"""Catalog model types: BackendSpec, ComputeSpec, NetworkSpec."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, TypeAdapter


class LatencyRange(BaseModel):
    min: float
    max: float
    unit: str = "ms"


class StorageBackendSpec(BaseModel):
    """Specification for a storage backend entry in a catalog TOML."""

    display_name: str
    read_latency: LatencyRange
    write_latency: LatencyRange
    durability: list[str]
    max_size_gb: int = 0  # 0 = unlimited
    storage_kinds: list[str] = Field(default_factory=lambda: ["kv"])
    access_patterns: list[str] = Field(default_factory=list)
    cost_per_gb_month: float = 0.0
    requires_vpc: bool = False
    regions: list[str] = Field(default_factory=lambda: ["all"])
    notes: str = ""
    # Deployment-time provisioning parameters (not used by the solver).
    # Populated from the optional [storage.<name>.deploy] TOML subsection.
    # Values are validated via skaal.deploy.config.storage_deploy_config()
    # when the catalog is loaded; generators cast to the appropriate typed
    # model via the same factory.
    deploy: dict[str, Any] = Field(default_factory=dict)
    # Code-generation wiring metadata (not used by the solver).
    # Populated from the optional [storage.<name>.wire] TOML subsection.
    # Tells deploy generators which Python class to instantiate and how to
    # connect it.  Validated as deploy Wiring metadata at catalog load time.
    wire: dict[str, Any] = Field(default_factory=dict)


class ComputeBackendSpec(BaseModel):
    """Specification for a compute backend entry in a catalog TOML."""

    display_name: str
    vcpus: int = 1
    memory_gb: float = 1.0
    compute_types: list[str] = Field(default_factory=lambda: ["cpu"])
    cost_per_hour: float = 0.0
    regions: list[str] = Field(default_factory=lambda: ["all"])
    notes: str = ""
    # Deployment-time provisioning parameters (not used by the solver).
    # Validated via skaal.deploy.config.compute_deploy_config() at load time.
    deploy: dict[str, Any] = Field(default_factory=dict)


class NetworkSpec(BaseModel):
    """Specification for a network/CDN backend entry in a catalog TOML."""

    display_name: str
    latency_ms: LatencyRange | None = None
    regions: list[str] = Field(default_factory=lambda: ["all"])
    bandwidth_gbps: float = 0.0
    cost_per_gb_transfer: float = 0.0
    notes: str = ""


class Catalog(BaseModel):
    """Top-level parsed catalog containing all backend categories."""

    storage: dict[str, StorageBackendSpec] = Field(default_factory=dict)
    compute: dict[str, ComputeBackendSpec] = Field(default_factory=dict)
    network: dict[str, NetworkSpec] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @classmethod
    def from_raw(cls, data: dict[str, Any]) -> "Catalog":
        """
        Build a Catalog from a raw TOML dict, tolerating unknown keys.

        Also eagerly validates any ``[storage/compute.X.deploy]`` subsections
        using the typed models in :mod:`skaal.deploy.config`.  This surfaces
        bad catalog values (wrong tier format, out-of-range memory, unknown
        runtime, etc.) at load time with a clear error rather than silently
        producing broken Pulumi stacks later.
        """
        # Import here to avoid a circular dependency at module level
        # (deploy.config doesn't import catalog, so the dependency is one-way).
        from skaal.backends._spec import Wiring
        from skaal.deploy.config import (
            compute_deploy_config,
            storage_deploy_config,
        )

        storage: dict[str, StorageBackendSpec] = {}
        for k, v in data.get("storage", {}).items():
            spec = StorageBackendSpec(**v)
            if spec.deploy:
                storage_deploy_config(k, spec.deploy)  # raises ValueError on bad config
            if spec.wire:
                try:
                    TypeAdapter(Wiring).validate_python(spec.wire)
                except Exception as exc:
                    raise ValueError(f"Invalid [storage.{k}.wire] configuration: {exc}") from exc
            storage[k] = spec

        compute: dict[str, ComputeBackendSpec] = {}
        for k, v in data.get("compute", {}).items():
            cspec = ComputeBackendSpec(**v)
            if cspec.deploy:
                compute_deploy_config(k, cspec.deploy)  # raises ValueError on bad config
            compute[k] = cspec

        network = {k: NetworkSpec(**v) for k, v in data.get("network", {}).items()}
        return cls(storage=storage, compute=compute, network=network, raw=data)
