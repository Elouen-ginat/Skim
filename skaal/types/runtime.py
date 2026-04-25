"""Runtime typing surface shared across local, mesh, and deploy wiring."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Literal, Protocol, TypeAlias, TypedDict

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


class MeshClient(Protocol):
    def health_snapshot(self) -> str: ...

    def route_agent_call(
        self,
        agent_type: str,
        agent_id: str,
        method: str,
        args: str,
    ) -> str: ...

    def publish(self, topic: str, message: str) -> int: ...


__all__ = [
    "AsyncClosable",
    "BackendFactory",
    "BackendOverrides",
    "ConstructorKwargs",
    "DispatchResult",
    "MeshClient",
    "RuntimeApp",
    "RuntimeCallable",
    "RuntimeInstance",
    "RuntimeInvoker",
    "RuntimeKwargs",
    "RuntimeMode",
    "RuntimePayload",
    "RuntimePlanSource",
    "RuntimeWireParams",
    "StorageClassMap",
    "StorageKindName",
    "SupportsAsyncSend",
]
