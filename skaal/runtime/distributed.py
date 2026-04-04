"""Distributed runtime — production runtime backed by the Skaal mesh.

This module defines the interface for the distributed runtime.  The concrete
implementation lives in the Rust mesh (``mesh/``) and communicates over gRPC.
This Python class handles the orchestration layer: routing, health-checking,
and graceful shutdown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from skaal.runtime.agent_registry import AgentRegistry
from skaal.runtime.channels import LocalChannel
from skaal.runtime.state import InMemoryStateStore

if TYPE_CHECKING:
    from skaal.app import App


class DistributedRuntime:
    """
    Distributed runtime stub.

    In a full deployment this connects to the Rust mesh gRPC endpoint and
    delegates function dispatch to it.  This stub implements the same
    interface as :class:`~skaal.local.runtime.LocalRuntime` for testing and
    gradual migration.

    Not yet fully implemented — production use requires the compiled mesh.
    """

    def __init__(
        self,
        app: "App",
        *,
        mesh_address: str = "localhost:50051",
        state_store: Any | None = None,
        channel: Any | None = None,
        registry: AgentRegistry | None = None,
    ) -> None:
        self.app = app
        self.mesh_address = mesh_address
        self.state = state_store or InMemoryStateStore()
        self.channel = channel or LocalChannel()
        self.registry = registry or AgentRegistry()

    async def invoke(self, function_name: str, **kwargs: Any) -> Any:
        """
        Invoke a registered function by name.

        In local mode falls back to direct Python call.  In production,
        routes through the mesh gRPC endpoint.
        """
        all_resources = self.app._collect_all()
        fn = all_resources.get(function_name) or self.app._functions.get(function_name)
        if fn is None:
            raise KeyError(f"No function {function_name!r} registered in {self.app.name!r}")

        import inspect

        if inspect.iscoroutinefunction(fn):
            return await fn(**kwargs)
        return fn(**kwargs)

    async def health(self) -> dict[str, Any]:
        """Return a health summary for monitoring."""
        agents = await self.registry.list_agents()
        return {
            "app": self.app.name,
            "mesh_address": self.mesh_address,
            "agents": len(agents),
            "status": "ok",
        }
