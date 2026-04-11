"""Typed dataclasses mirroring the Rust mesh structs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentInfo:
    """A registered agent instance record."""

    agent_id: str
    agent_type: str
    status: str
    instance: int
    registered_at: str
    last_active: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MigrationState:
    """Snapshot of a single storage variable's migration progress."""

    variable_name: str
    source_backend: str
    target_backend: str
    stage: int
    stage_name: str
    started_at: str
    advanced_at: str
    discrepancy_count: int
    keys_migrated: int
    app_name: str

    @property
    def is_complete(self) -> bool:
        """True once the migration reaches stage 6 (done)."""
        return self.stage >= 6


@dataclass(frozen=True)
class RoutingInfo:
    """Result of a ``route_agent_call`` invocation."""

    status: str
    agent_type: str
    agent_id: str
    method: str
    node: str


@dataclass(frozen=True)
class HealthSnapshot:
    """Mesh health summary returned by ``health_snapshot()``."""

    app: str
    status: str
    agents: dict[str, int]
    state: dict[str, int]
    migrations: dict[str, int]
    channels: dict[str, int]
