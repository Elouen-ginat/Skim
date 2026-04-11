"""MeshClient — Pythonic wrapper around the compiled ``skaal_mesh`` extension.

Handles all JSON serialisation/deserialisation so callers work with typed
Python objects instead of raw JSON strings.

Usage::

    from skaal.mesh import MeshClient

    mesh = MeshClient("myapp")
    info  = mesh.register_agent("Counter", "counter-1")
    snap  = mesh.health_snapshot()
    print(snap.agents["total"])
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from skaal.mesh.types import AgentInfo, HealthSnapshot, MigrationState, RoutingInfo

if TYPE_CHECKING:
    pass

# ── Extension import ───────────────────────────────────────────────────────────

try:
    from skaal_mesh import SkaalMesh as _RustMesh  # type: ignore[unused-ignore]

    _RUST_AVAILABLE = True
except ImportError:
    _RustMesh = None  # type: ignore[unused-ignore]
    _RUST_AVAILABLE = False


def _require_rust() -> None:
    if not _RUST_AVAILABLE:
        raise RuntimeError(
            "skaal_mesh extension not found.  Build it with:\n\n"
            "    maturin develop --manifest-path mesh/Cargo.toml\n"
        )


# ── MeshClient ─────────────────────────────────────────────────────────────────


class MeshClient:
    """
    Pythonic wrapper around :class:`skaal_mesh.SkaalMesh`.

    All methods deserialise JSON responses into typed dataclasses so callers
    never need to handle raw strings.

    Args:
        app_name: Application name used as a namespace.
        plan:     Optional solved plan dictionary (output of ``skaal plan``).

    Raises:
        RuntimeError: If the compiled ``skaal_mesh`` extension is not available.
    """

    def __init__(self, app_name: str, plan: dict[str, Any] | None = None) -> None:
        _require_rust()
        plan_json = json.dumps({"app_name": app_name, **(plan or {})})
        self._mesh: Any = _RustMesh(app_name, plan_json)

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def app_name(self) -> str:
        """Application name."""
        return self._mesh.app_name  # type: ignore[unused-ignore]

    # ── Agent Registry ────────────────────────────────────────────────────────

    def register_agent(
        self,
        agent_type: str,
        agent_id: str,
        instance: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> AgentInfo:
        """Register a new agent instance.

        Args:
            agent_type: Agent class name, e.g. ``"Customer"``.
            agent_id:   Unique identity key.
            instance:   Replica index (0-based).
            metadata:   Optional arbitrary metadata dict.

        Returns:
            :class:`AgentInfo` record.

        Raises:
            RuntimeError: If an agent with the same ID is already registered.
        """
        meta_json = json.dumps(metadata) if metadata else None
        raw = self._mesh.register_agent(agent_type, agent_id, instance, meta_json)
        return _agent_info(json.loads(raw))

    def update_agent_status(self, agent_id: str, status: str) -> None:
        """Update an agent's lifecycle status.

        Valid status values: ``"starting"``, ``"running"``, ``"idle"``,
        ``"stopping"``, ``"stopped"``, ``"error"``.
        """
        self._mesh.update_agent_status(agent_id, status)

    def deregister_agent(self, agent_id: str) -> None:
        """Deregister an agent.  No-op if the ID does not exist."""
        self._mesh.deregister_agent(agent_id)

    def get_agent(self, agent_id: str) -> AgentInfo | None:
        """Look up an agent by ID.  Returns ``None`` if not found."""
        raw = self._mesh.get_agent(agent_id)
        return _agent_info(json.loads(raw)) if raw else None

    def list_agents(
        self,
        agent_type: str | None = None,
        status: str | None = None,
    ) -> list[AgentInfo]:
        """List agents, optionally filtered by type and/or status."""
        raw = self._mesh.list_agents(agent_type, status)
        return [_agent_info(a) for a in json.loads(raw)]

    def route_agent_call(
        self,
        agent_type: str,
        agent_id: str,
        method: str,
        args: dict[str, Any] | None = None,
    ) -> RoutingInfo:
        """Route a call to an agent and return routing metadata.

        The mesh validates that the agent is live and returns a
        :class:`RoutingInfo` describing how to reach it.  The actual Python
        method dispatch is performed by the caller using this information.

        Raises:
            KeyError: If the agent is not registered or has stopped.
        """
        raw = self._mesh.route_agent_call(agent_type, agent_id, method, json.dumps(args or {}))
        data = json.loads(raw)
        return RoutingInfo(
            status=data["status"],
            agent_type=data["agent_type"],
            agent_id=data["agent_id"],
            method=data["method"],
            node=data["node"],
        )

    # ── State Store ───────────────────────────────────────────────────────────

    def state_get(self, key: str) -> Any:
        """Get a value from the shared state store.

        Returns the Python object, or ``None`` if the key does not exist.
        """
        raw = self._mesh.state_get(key)
        return json.loads(raw) if raw is not None else None

    def state_set(self, key: str, value: Any) -> None:
        """Store a JSON-serialisable value in the shared state store."""
        self._mesh.state_set(key, json.dumps(value))

    def state_delete(self, key: str) -> None:
        """Delete a key from the state store.  No-op if key does not exist."""
        self._mesh.state_delete(key)

    def state_exists(self, key: str) -> bool:
        """Return ``True`` if the key exists in the state store."""
        return self._mesh.state_exists(key)  # type: ignore[unused-ignore]

    def state_keys(self, prefix: str = "") -> list[str]:
        """Return all keys with the given prefix (sorted)."""
        return self._mesh.state_keys(prefix)  # type: ignore[unused-ignore]

    # ── Migration ─────────────────────────────────────────────────────────────

    def start_migration(
        self,
        variable_name: str,
        source_backend: str,
        target_backend: str,
    ) -> MigrationState:
        """Begin a zero-downtime migration at stage 1 (shadow_write).

        Raises:
            ValueError: If a non-complete migration already exists.
        """
        raw = self._mesh.start_migration(variable_name, source_backend, target_backend)
        return _migration_state(json.loads(raw))

    def advance_migration(
        self,
        variable_name: str,
        discrepancy_count: int = 0,
        keys_migrated: int = 0,
    ) -> MigrationState:
        """Advance the migration to the next stage."""
        raw = self._mesh.advance_migration(variable_name, discrepancy_count, keys_migrated)
        return _migration_state(json.loads(raw))

    def rollback_migration(self, variable_name: str) -> MigrationState:
        """Roll back the migration by one stage."""
        raw = self._mesh.rollback_migration(variable_name)
        return _migration_state(json.loads(raw))

    def get_migration(self, variable_name: str) -> MigrationState | None:
        """Get the current migration state.  Returns ``None`` if not found."""
        raw = self._mesh.get_migration(variable_name)
        return _migration_state(json.loads(raw)) if raw else None

    def list_migrations(self) -> list[MigrationState]:
        """List all migrations (active and completed)."""
        return [_migration_state(m) for m in json.loads(self._mesh.list_migrations())]

    # ── Channels ──────────────────────────────────────────────────────────────

    def publish(self, topic: str, payload: Any) -> int:
        """Publish a message to a topic.

        Returns:
            Number of active receivers that received the message.
        """
        return self._mesh.publish(topic, json.dumps(payload))  # type: ignore[unused-ignore]

    # ── Health ────────────────────────────────────────────────────────────────

    def health_snapshot(self) -> HealthSnapshot:
        """Return a typed health snapshot of the entire mesh."""
        data = json.loads(self._mesh.health_snapshot())
        return HealthSnapshot(
            app=data["app"],
            status=data["status"],
            agents=data["agents"],
            state=data["state"],
            migrations=data["migrations"],
            channels=data["channels"],
        )

    # ── Convenience ───────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return self._mesh.__repr__()


# ── Helpers ────────────────────────────────────────────────────────────────────


def _agent_info(data: dict[str, Any]) -> AgentInfo:
    return AgentInfo(
        agent_id=data["agent_id"],
        agent_type=data["agent_type"],
        status=data["status"],
        instance=data["instance"],
        registered_at=data["registered_at"],
        last_active=data["last_active"],
        metadata=data.get("metadata") or {},
    )


def _migration_state(data: dict[str, Any]) -> MigrationState:
    return MigrationState(
        variable_name=data["variable_name"],
        source_backend=data["source_backend"],
        target_backend=data["target_backend"],
        stage=data["stage"],
        stage_name=data["stage_name"],
        started_at=data["started_at"],
        advanced_at=data["advanced_at"],
        discrepancy_count=data["discrepancy_count"],
        keys_migrated=data["keys_migrated"],
        app_name=data["app_name"],
    )
