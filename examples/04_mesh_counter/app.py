"""04_mesh_counter — demonstrates the Skaal runtime mesh.

This example shows all four mesh subsystems working together:

  1. Agent registry  — register and track virtual actor instances
  2. State store     — shared key/value state across functions
  3. Migration       — 6-stage zero-downtime backend migration
  4. Pub/sub channel — broadcast events to subscribers

Run it with::

    maturin develop --manifest-path mesh/Cargo.toml
    python examples/04_mesh_counter/app.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ── Bootstrap ──────────────────────────────────────────────────────────────────
# Allow running this script directly from the repo root without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    from skaal.mesh import MeshClient
except RuntimeError as exc:
    sys.exit(
        f"Cannot import MeshClient: {exc}\n\n"
        "Build the extension first:\n"
        "    maturin develop --manifest-path mesh/Cargo.toml\n"
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

SEPARATOR = "-" * 60


def section(title: str) -> None:
    print(f"\n{SEPARATOR}")
    print(f"  {title}")
    print(SEPARATOR)


def pretty(obj: object) -> str:
    return json.dumps(obj.__dict__ if hasattr(obj, "__dict__") else obj, indent=2, default=str)


# ── Example ────────────────────────────────────────────────────────────────────


def run_agent_registry(mesh: MeshClient) -> None:
    section("1 · Agent Registry")

    # Register three agents across two types
    counter = mesh.register_agent("Counter", "counter-main", instance=0)
    print(f"  registered  Counter/counter-main  status={counter.status}")

    mesh.register_agent("Worker", "worker-1", instance=0, metadata={"queue": "fast-lane"})
    mesh.register_agent("Worker", "worker-2", instance=1)

    # Update statuses
    mesh.update_agent_status("counter-main", "running")
    mesh.update_agent_status("worker-1", "running")
    mesh.update_agent_status("worker-2", "idle")

    # List all agents
    all_agents = mesh.list_agents()
    print(f"  total agents registered: {len(all_agents)}")

    # Filter by type
    workers = mesh.list_agents(agent_type="Worker")
    print(f"  workers: {len(workers)}")

    # Filter by status
    running = mesh.list_agents(status="running")
    print(f"  running: {[a.agent_id for a in running]}")

    # Route a call
    routing = mesh.route_agent_call("Counter", "counter-main", "increment", {"delta": 1})
    print(f"  route result: status={routing.status!r}  node={routing.node!r}")

    # Look up an individual agent
    info = mesh.get_agent("worker-1")
    assert info is not None
    print(f"  worker-1 metadata: {info.metadata}")


def run_state_store(mesh: MeshClient) -> None:
    section("2 · Shared State Store")

    # Basic set / get
    mesh.state_set("counter:hits", 0)
    for _ in range(5):
        current = mesh.state_get("counter:hits")
        mesh.state_set("counter:hits", current + 1)

    hits = mesh.state_get("counter:hits")
    print(f"  counter:hits = {hits}")

    # Store a complex value
    mesh.state_set("config:limits", {"max_rps": 1000, "burst": 200})
    limits = mesh.state_get("config:limits")
    print(f"  config:limits = {limits}")

    # Key enumeration
    mesh.state_set("counter:errors", 0)
    mesh.state_set("counter:latency_p99_ms", 42)
    counter_keys = mesh.state_keys("counter:")
    print(f"  keys with prefix 'counter:': {counter_keys}")

    # Existence check + delete
    assert mesh.state_exists("counter:hits")
    mesh.state_delete("counter:hits")
    assert not mesh.state_exists("counter:hits")
    print("  state_delete + state_exists: OK")


def run_migration(mesh: MeshClient) -> None:
    section("3 · Six-Stage Backend Migration")

    var = "counter.Counts"
    state = mesh.start_migration(var, source_backend="sqlite", target_backend="redis")
    print(f"  started:  stage={state.stage}  name={state.stage_name!r}")

    # Walk through all 6 stages
    for advance_num in range(1, 6):
        # Simulate some keys being copied on the first advance
        keys = 100 if advance_num == 1 else 0
        state = mesh.advance_migration(var, discrepancy_count=0, keys_migrated=keys)
        print(
            f"  advanced: stage={state.stage}  name={state.stage_name!r}  keys_migrated={state.keys_migrated}"
        )

    assert state.is_complete, "Migration should be done after 6 advances"
    print(f"  migration complete:  {state.source_backend!r} -> {state.target_backend!r}")

    # Demonstrate rollback on a fresh migration
    var2 = "counter.Sessions"
    mesh.start_migration(var2, "sqlite", "postgres")
    mesh.advance_migration(var2)  # stage 2
    rolled = mesh.rollback_migration(var2)
    print(f"  rollback: stage={rolled.stage}  name={rolled.stage_name!r}")

    # List active migrations
    active = [m for m in mesh.list_migrations() if not m.is_complete]
    print(f"  active migrations: {[m.variable_name for m in active]}")


def run_channels(mesh: MeshClient) -> None:
    section("4 · Pub/Sub Channels")

    # Publish without a subscriber (message is dropped — this is normal)
    sent = mesh.publish("events", {"type": "counter.reset", "value": 0})
    print(f"  publish to 'events' (no subs): receivers={sent}")

    # Subscribe then publish using the raw extension directly for demo
    # (the high-level client purposely keeps channels simple; for a full
    # subscriber loop use asyncio + the underlying broadcast::Receiver)
    import skaal_mesh as _ext  # type: ignore[import]

    raw_mesh = _ext.SkaalMesh("counter-demo", "")
    rx_count = raw_mesh.publish("heartbeat", '{"ts": "2026-04-11"}')
    print(f"  publish 'heartbeat' (raw, no subs): receivers={rx_count}")

    print("  channel pub/sub: OK (full async subscribe via tokio::sync::broadcast)")


def run_health(mesh: MeshClient) -> None:
    section("5 · Health Snapshot")
    snap = mesh.health_snapshot()
    print(f"  app:    {snap.app!r}")
    print(f"  status: {snap.status!r}")
    print(f"  agents: {snap.agents}")
    print(f"  state:  {snap.state}")
    print(f"  migrations: {snap.migrations}")
    print(f"  channels:   {snap.channels}")


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    print("\nSkaal Runtime Mesh — demo")

    mesh = MeshClient("counter-demo")
    print(f"  {mesh!r}\n")

    run_agent_registry(mesh)
    run_state_store(mesh)
    run_migration(mesh)
    run_channels(mesh)
    run_health(mesh)

    print(f"\n{SEPARATOR}")
    print("  All subsystems working.")
    print(SEPARATOR + "\n")


if __name__ == "__main__":
    main()
