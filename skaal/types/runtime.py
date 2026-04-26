"""Runtime typing surface shared across local, mesh, and deploy wiring."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias, TypedDict

from skaal.plan import PlanFile

RuntimeMode = Literal["memory", "sqlite", "redis", "postgres"]
StorageKindName = Literal["kv", "relational", "vector"]

RuntimeCallable: TypeAlias = Callable[..., object]
RuntimeInvoker: TypeAlias = Callable[..., Awaitable[object]]
RuntimeKwargs: TypeAlias = dict[str, object]
RuntimePayload: TypeAlias = dict[str, object]
DispatchResult: TypeAlias = tuple[object, int]
BackendOverrides: TypeAlias = dict[str, object]
ConstructorKwargs: TypeAlias = dict[str, object]
StorageClassMap: TypeAlias = dict[str, type[object]]
RuntimePlanSource: TypeAlias = PlanFile | Path | str | Mapping[str, object]
BackendFactory: TypeAlias = Callable[[str, type[object]], object]


class RuntimeWireParams(TypedDict, total=False):
    class_name: str
    module: str
    env_prefix: str | None
    path_default: str | None
    connection_value: str | None
    uses_namespace: bool
    constructor_kwargs: ConstructorKwargs
    dependency_sets: tuple[str, ...] | list[str]
    requires_vpc: bool
    local_service: str | None
    local_env_value: str | None


class RuntimeApp(Protocol):
    name: str

    @property
    def _functions(self) -> Mapping[str, object]: ...

    @property
    def _schedules(self) -> Mapping[str, object]: ...

    def _collect_all(self) -> Mapping[str, object]: ...


class RuntimeInstance(Protocol):
    app: RuntimeApp
    host: str
    port: int

    async def serve(self) -> None: ...

    async def shutdown(self) -> None: ...


class SupportsAsyncSend(Protocol):
    async def send(self, item: object) -> None: ...


class AsyncClosable(Protocol):
    async def close(self) -> None: ...


class StateService(Protocol):
    async def get(self, key: str) -> object | None: ...

    async def set(self, key: str, value: object) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def exists(self, key: str) -> bool: ...

    async def keys(self, prefix: str = "") -> list[str]: ...


class AgentsService(Protocol):
    def declare(self, qualified_name: str, agent_cls: type[object]) -> None: ...

    async def register(
        self,
        agent_id: str,
        function_name: str,
        instance: int = 0,
        metadata: dict[str, object] | None = None,
    ) -> object: ...

    async def update_status(self, agent_id: str, status: object) -> None: ...

    async def deregister(self, agent_id: str) -> None: ...

    async def list_agents(
        self,
        function_name: str | None = None,
        status: object | None = None,
    ) -> list[object]: ...

    async def get(self, agent_id: str) -> object | None: ...

    async def route(
        self,
        agent_type: str,
        agent_id: str,
        method: str,
        args: RuntimeKwargs | None = None,
    ) -> object: ...


class RuntimeObserver(Protocol):
    def engine_started(self, name: str) -> None: ...

    def engine_stopped(self, name: str) -> None: ...

    def event_handled(self, name: str, offset: int) -> None: ...

    def event_failed(self, name: str, offset: int, exc: BaseException) -> None: ...

    def snapshot(self) -> RuntimePayload: ...


class RuntimeServices(Protocol):
    agents: AgentsService
    state: StateService


class MeshClient(Protocol):
    def register_agent(
        self,
        agent_type: str,
        agent_id: str,
        instance: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> object: ...

    def update_agent_status(self, agent_id: str, status: str) -> None: ...

    def deregister_agent(self, agent_id: str) -> None: ...

    def get_agent(self, agent_id: str) -> object | None: ...

    def list_agents(
        self,
        agent_type: str | None = None,
        status: str | None = None,
    ) -> list[object]: ...

    def route_agent_call(
        self,
        agent_type: str,
        agent_id: str,
        method: str,
        args: dict[str, Any] | None = None,
    ) -> object: ...

    def state_get(self, key: str) -> object: ...

    def state_set(self, key: str, value: object) -> None: ...

    def state_delete(self, key: str) -> None: ...

    def state_exists(self, key: str) -> bool: ...

    def state_keys(self, prefix: str = "") -> list[str]: ...

    def publish(self, topic: str, message: object) -> int: ...

    def health_snapshot(self) -> object: ...


__all__ = [
    "AgentsService",
    "AsyncClosable",
    "BackendFactory",
    "BackendOverrides",
    "ConstructorKwargs",
    "DispatchResult",
    "MeshClient",
    "RuntimeObserver",
    "RuntimeApp",
    "RuntimeCallable",
    "RuntimeInstance",
    "RuntimeInvoker",
    "RuntimeKwargs",
    "RuntimeMode",
    "RuntimePayload",
    "RuntimePlanSource",
    "RuntimeServices",
    "RuntimeWireParams",
    "StateService",
    "StorageClassMap",
    "StorageKindName",
    "SupportsAsyncSend",
]
