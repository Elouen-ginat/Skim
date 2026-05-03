"""skaal.mesh — Python API for the Skaal runtime mesh.

The mesh is the distributed control plane for Skaal applications.  It
provides:

* **Agent registry** — register, look up, and manage virtual actor instances.
* **Shared state store** — a thread-safe key/value store accessible from all
  functions.
* **Migration controller** — 6-stage zero-downtime backend migration protocol.
* **Pub/sub channels** — broadcast messaging between functions.
* **Health snapshot** — real-time telemetry summary.

Quick-start::

    from skaal.mesh import MeshClient

    mesh = MeshClient("myapp")

    # Register an agent
    info = mesh.register_agent("Counter", "counter-1")
    print(info.status)  # "starting"

    # Shared state
    mesh.state_set("counter:hits", 0)
    mesh.state_set("counter:hits", mesh.state_get("counter:hits") + 1)

    # Health
    snap = mesh.health_snapshot()
    print(snap)  # HealthSnapshot(app='myapp', status='ok', ...)

    # Start a backend migration
    state = mesh.start_migration("counter.Counts", "sqlite", "redis")
    print(state.stage_name)  # "shadow_write"

Install the published mesh wheel before using::

    pip install "skaal[mesh]"

If you are editing the Rust crate locally::

    make build-dev
"""

from skaal.mesh.client import MeshClient
from skaal.mesh.types import AgentInfo, HealthSnapshot, MigrationState, RoutingInfo

__all__ = [
    "MeshClient",
    # Types
    "AgentInfo",
    "HealthSnapshot",
    "MigrationState",
    "RoutingInfo",
]
