"""mesh_counter — a Skaal app that demonstrates the runtime mesh.

The app is a named counter service.  Storage is managed by Skaal (solver picks
the backend based on constraints).  The mesh layer adds:

  * Agent registry  — each counter name is tracked as a ``CounterAgent`` instance.
  * State cache     — mesh state store holds a fast in-memory replica of counts.
  * Event channel   — every mutation publishes a ``counter.changed`` event.
  * Health endpoint — real-time telemetry from the mesh.

CLI workflow
------------
::

    # 1. Solve constraints, write plan.skaal.lock
    skaal plan examples.mesh_counter.app:app --target local

    # 2. Generate Docker Compose artifacts (--dev mounts local skaal source)
    skaal build --dev

    # 3. Start the local Docker stack
    skaal deploy

    # — or, run directly in-process (fastest for development) —
    skaal run examples.mesh_counter.app:app

Endpoints
---------
::

    POST /increment        {"name": "hits", "by": 1}
    POST /get_count        {"name": "hits"}
    POST /reset            {"name": "hits"}
    POST /list_counts      {}
    POST /mesh_health      {}
    POST /mesh_agents      {}

Mesh mode detection
-------------------
If ``skaal_mesh`` has not been compiled (``maturin develop`` not run), every
endpoint still works — the mesh layer is silently skipped and only durable
storage is used.  Build the extension to unlock the full feature set::

    maturin develop --manifest-path mesh/Cargo.toml
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from skaal import App, Map

# ── App declaration ────────────────────────────────────────────────────────────

app = App("mesh-counter")


@app.storage(read_latency="< 5ms", durability="ephemeral")
class Counts(Map[str, int]):
    """Named integer counters.

    Constraint ``durability="ephemeral"`` lets the solver choose the fastest
    local backend (LocalMap / SQLite) for development and Redis / DynamoDB in
    cloud targets.
    """


# ── Mesh singleton ─────────────────────────────────────────────────────────────
# Initialised lazily on the first function call so that ``skaal plan`` can
# import this module without requiring the compiled extension.

_UNSET: Any = object()
_mesh_instance: Any = _UNSET  # MeshClient | None


def _get_mesh() -> Any:
    """Return the MeshClient singleton, or None if the extension is not built."""
    global _mesh_instance
    if _mesh_instance is not _UNSET:
        return _mesh_instance

    try:
        from skaal.mesh import MeshClient
        from skaal.plan import PLAN_FILE_NAME

        # Pass the solved plan to the mesh so it knows which backends were chosen.
        plan: dict[str, Any] = {}
        plan_path = Path(PLAN_FILE_NAME)
        if plan_path.exists():
            plan = json.loads(plan_path.read_text())

        _mesh_instance = MeshClient("mesh-counter", plan)

        # Register the running app as the first agent in the registry.
        _mesh_instance.register_agent(
            "MeshCounterApp",
            "mesh-counter-0",
            metadata={"version": "0.1.0", "mode": "local"},
        )
        _mesh_instance.update_agent_status("mesh-counter-0", "running")

    except (RuntimeError, ImportError, Exception):  # noqa: BLE001
        # Extension not compiled or any initialisation error → degrade gracefully.
        _mesh_instance = None

    return _mesh_instance


# ── Helpers ────────────────────────────────────────────────────────────────────


def _ensure_counter_agent(mesh: Any, name: str) -> None:
    """Register a CounterAgent for *name* the first time it is seen."""
    agent_id = f"counter-{name}"
    try:
        if mesh.get_agent(agent_id) is None:
            mesh.register_agent(
                "CounterAgent",
                agent_id,
                metadata={"counter_name": name},
            )
    except Exception:  # noqa: BLE001
        pass


def _publish_changed(mesh: Any, name: str, value: int, action: str) -> None:
    """Publish a ``counter.changed`` event to the mesh channel."""
    try:
        mesh.publish(
            "counter.changed",
            {"action": action, "name": name, "value": value},
        )
    except Exception:  # noqa: BLE001
        pass


# ── Functions ──────────────────────────────────────────────────────────────────


@app.function()
async def increment(name: str, by: int = 1) -> dict:
    """Increment counter *name* by *by* (default 1). Returns the new value.

    Side-effects when mesh is available:
      - Registers a ``CounterAgent`` for *name* if not yet tracked.
      - Updates the agent's status to ``"running"`` while processing.
      - Caches the new value in the mesh state store (fast read path).
      - Publishes a ``counter.changed`` event.
    """
    current = await Counts.get(name) or 0
    new_value = current + by
    await Counts.set(name, new_value)

    mesh = _get_mesh()
    if mesh is not None:
        agent_id = f"counter-{name}"
        _ensure_counter_agent(mesh, name)
        try:
            mesh.update_agent_status(agent_id, "running")
        except Exception:  # noqa: BLE001
            pass
        mesh.state_set(f"cache:{name}", new_value)
        _publish_changed(mesh, name, new_value, "increment")
        try:
            mesh.update_agent_status(agent_id, "idle")
        except Exception:  # noqa: BLE001
            pass

    return {"name": name, "value": new_value, "mesh": mesh is not None}


@app.function()
async def get_count(name: str) -> dict:
    """Return the current value of counter *name*.

    When the mesh is available the value is served from the in-memory state
    cache (sub-millisecond).  Falls back to storage on a cache miss.
    """
    mesh = _get_mesh()
    if mesh is not None:
        cached = mesh.state_get(f"cache:{name}")
        if cached is not None:
            return {"name": name, "value": cached, "source": "mesh-cache"}

    value = await Counts.get(name) or 0
    if mesh is not None:
        mesh.state_set(f"cache:{name}", value)

    return {"name": name, "value": value, "source": "storage"}


@app.function()
async def reset(name: str) -> dict:
    """Reset counter *name* to zero."""
    await Counts.delete(name)

    mesh = _get_mesh()
    if mesh is not None:
        mesh.state_delete(f"cache:{name}")
        _publish_changed(mesh, name, 0, "reset")
        agent_id = f"counter-{name}"
        try:
            mesh.update_agent_status(agent_id, "idle")
        except Exception:  # noqa: BLE001
            pass

    return {"name": name, "value": 0, "mesh": mesh is not None}


@app.function()
async def list_counts() -> dict:
    """Return all counters and their current values."""
    entries = await Counts.list()
    return {"counts": dict(entries)}


@app.function()
async def mesh_health() -> dict:
    """Return a real-time health snapshot from the mesh control plane.

    Returns a ``{"available": false}`` response when the mesh extension has
    not been compiled.
    """
    mesh = _get_mesh()
    if mesh is None:
        return {
            "available": False,
            "reason": (
                "skaal_mesh extension not found. "
                "Build it with: maturin develop --manifest-path mesh/Cargo.toml"
            ),
        }

    snap = mesh.health_snapshot()
    return {
        "available": True,
        "app": snap.app,
        "status": snap.status,
        "agents": snap.agents,
        "state": snap.state,
        "migrations": snap.migrations,
        "channels": snap.channels,
    }


@app.function()
async def mesh_agents() -> dict:
    """Return all agents currently registered in the mesh.

    Returns ``{"available": false}`` when the mesh extension is not built.
    """
    mesh = _get_mesh()
    if mesh is None:
        return {"available": False}

    agents = mesh.list_agents()
    return {
        "available": True,
        "count": len(agents),
        "agents": [
            {
                "id": a.agent_id,
                "type": a.agent_type,
                "status": a.status,
                "instance": a.instance,
                "last_active": a.last_active,
                "metadata": a.metadata,
            }
            for a in agents
        ],
    }
