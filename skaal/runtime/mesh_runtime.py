"""MeshRuntime — distributed Skaal runtime backed by the Rust ``skaal_mesh`` extension.

Requires the ``mesh`` extra::

    maturin build --manifest-path mesh/Cargo.toml --release
    pip install target/wheels/skaal-*.whl

Usage::

    runtime = MeshRuntime(app, plan_json=plan.model_dump_json())
    asyncio.run(runtime.serve())

Or via the CLI::

    skaal run examples.counter:app --distributed
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, cast

from skaal.plan import PlanFile
from skaal.runtime._observer import InMemoryRuntimeObserver
from skaal.runtime._services import MeshAgentsService, MeshStateService
from skaal.types.runtime import (
    AsyncClosable,
    BackendOverrides,
    RuntimeApp,
    RuntimePayload,
    RuntimePlanSource,
)

from ._core import _RuntimeCoreMixin
from ._dispatch import _RuntimeDispatchMixin
from ._lifecycle import _RuntimeLifecycleMixin
from ._local_scheduler import _SchedulerMixin
from ._local_server import _StarletteServerMixin
from ._planning import (
    _default_local_storage_factories,
    build_backend_overrides,
    build_development_plan,
    coerce_runtime_plan,
)
from ._transport import _RuntimeHttpTransportMixin


class MeshRuntime(
    _RuntimeCoreMixin,
    _RuntimeDispatchMixin,
    _RuntimeHttpTransportMixin,
    _StarletteServerMixin,
    _SchedulerMixin,
    _RuntimeLifecycleMixin,
):
    """Distributed runtime that delegates agent routing and channels to the Rust mesh.

    The mesh layer currently shares the same local HTTP dispatch and channel
    wiring path as :class:`~skaal.runtime.local.LocalRuntime`. The distributed
    behavior today comes from the Rust mesh bridge APIs and health snapshot:

    - **Agent placement**: :meth:`route_agent` wraps
      ``SkaalMesh.route_agent_call`` so agent calls resolve to a live instance.
    - **Health**: :meth:`health` returns the mesh's aggregated health snapshot
      (agents, state, migrations, channels).
    """

    def __init__(
        self,
        app: RuntimeApp,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        plan_json: str = "",
        backend_overrides: BackendOverrides | None = None,
        runtime_plan: RuntimePlanSource | None = None,
        target: str | None = None,
    ) -> None:
        if backend_overrides is not None and runtime_plan is not None:
            raise ValueError("Pass either backend_overrides or runtime_plan, not both.")

        try:
            import skaal_mesh
        except ImportError as exc:
            raise ImportError(
                "MeshRuntime requires the skaal_mesh native extension.\n"
                "Build it with: maturin build --manifest-path mesh/Cargo.toml --release\n"
                "Then:          pip install target/wheels/skaal-*.whl"
            ) from exc

        from skaal.mesh import MeshClient as SkaalMeshClient

        self.app = app
        self.host = host
        self.port = port
        self._runtime_plan: PlanFile | None
        if runtime_plan is not None:
            self._runtime_plan = coerce_runtime_plan(runtime_plan)
            self._backend_overrides = build_backend_overrides(
                app,
                self._runtime_plan,
                target=target,
            )
            if not plan_json:
                plan_json = self._runtime_plan.model_dump_json()
        elif backend_overrides is not None:
            self._runtime_plan = None
            self._backend_overrides = backend_overrides
        else:
            self._runtime_plan = build_development_plan(app, mode="memory")
            self._backend_overrides = build_backend_overrides(app, self._runtime_plan)

        plan_dict: dict[str, object] | None = None
        if plan_json:
            loaded_plan = cast(dict[str, object], json.loads(plan_json))
            loaded_plan.setdefault("app_name", app.name)
            plan_dict = loaded_plan

        self._mesh: Any = SkaalMeshClient(app.name, plan=plan_dict)
        self.state = MeshStateService(self._mesh)
        self.observer = InMemoryRuntimeObserver()
        self.agents = MeshAgentsService(self._mesh)
        self._backends: dict[str, AsyncClosable] = {}
        self._patch_storage()
        self._patch_channels()
        self._initialize_runtime_state()

    # ── Setup (mirrors LocalRuntime) ──────────────────────────────────────────

    def _patch_storage(self) -> None:
        store_factory, vector_factory, relational_factory = _default_local_storage_factories()

        self._patch_storage_backends(
            store_factory=store_factory,
            vector_factory=vector_factory,
            relational_factory=relational_factory,
        )

    def _patch_channels(self) -> None:
        self._wire_local_channels()

    @classmethod
    def from_plan(
        cls,
        app: RuntimeApp,
        plan: RuntimePlanSource,
        *,
        target: str | None = None,
        host: str = "127.0.0.1",
        port: int = 8000,
    ) -> "MeshRuntime":
        return cls(app, host=host, port=port, runtime_plan=plan, target=target)

    def _runtime_label(self) -> str:
        return "mesh"

    def _index_payload(self) -> RuntimePayload:
        return {"mesh": self.health()}

    def _health_payload(self) -> RuntimePayload:
        return {"mesh": self.health()}

    # ── Mesh bridge API (used by engines / agents) ────────────────────────────

    async def route_agent(
        self,
        agent_type: str,
        agent_id: str,
        method: str,
        args: RuntimePayload,
    ) -> object:
        return await self.agents.route(agent_type, agent_id, method, args)

    def channel_publish(self, topic: str, message: object) -> int:
        return self._mesh.publish(topic, message)

    def health(self) -> RuntimePayload:
        snapshot = cast(RuntimePayload, asdict(self._mesh.health_snapshot()))
        snapshot["observer"] = self.observer.snapshot()
        return snapshot
