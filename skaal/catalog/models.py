"""Catalog model types: BackendSpec, ComputeSpec, NetworkSpec."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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
    access_patterns: list[str] = Field(default_factory=list)
    cost_per_gb_month: float = 0.0
    requires_vpc: bool = False
    regions: list[str] = Field(default_factory=lambda: ["all"])
    notes: str = ""


class ComputeBackendSpec(BaseModel):
    """Specification for a compute backend entry in a catalog TOML."""

    display_name: str
    vcpus: int = 1
    memory_gb: float = 1.0
    compute_types: list[str] = Field(default_factory=lambda: ["cpu"])
    cost_per_hour: float = 0.0
    regions: list[str] = Field(default_factory=lambda: ["all"])
    notes: str = ""


class NetworkSpec(BaseModel):
    """Specification for a network/CDN backend entry in a catalog TOML."""

    display_name: str
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
        """Build a Catalog from a raw TOML dict, tolerating unknown keys."""
        storage = {
            k: StorageBackendSpec(**v)
            for k, v in data.get("storage", {}).items()
        }
        compute = {
            k: ComputeBackendSpec(**v)
            for k, v in data.get("compute", {}).items()
        }
        network = {
            k: NetworkSpec(**v)
            for k, v in data.get("network", {}).items()
        }
        return cls(storage=storage, compute=compute, network=network, raw=data)
