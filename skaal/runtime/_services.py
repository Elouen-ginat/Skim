from __future__ import annotations

import asyncio
import inspect
from typing import Any, cast

from skaal.runtime.agent_registry import AgentRegistry, AgentStatus
from skaal.types.runtime import MeshClient, RuntimeKwargs, StateService


def _agent_metadata(qualified_name: str, agent_cls: type[object]) -> dict[str, object]:
    return {
        "qualified_name": qualified_name,
        "persistent": bool(getattr(agent_cls, "__skaal_agent__", {}).get("persistent", True)),
        "persistent_fields": sorted(getattr(agent_cls, "__skaal_persistent_fields__", ())),
    }


def _status_name(status: object) -> str:
    value = getattr(status, "value", status)
    return str(value)


class LocalAgentsService:
    def __init__(self, registry: AgentRegistry, state: StateService) -> None:
        self._registry = registry
        self._state = state
        self._agent_types: dict[str, type[object]] = {}
        self._qualified_names: dict[type[object], str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def declare(self, qualified_name: str, agent_cls: type[object]) -> None:
        self._agent_types[qualified_name] = agent_cls
        self._agent_types.setdefault(agent_cls.__name__, agent_cls)
        self._qualified_names[agent_cls] = qualified_name
        self._registry.declare(
            qualified_name,
            agent_cls.__name__,
            metadata=_agent_metadata(qualified_name, agent_cls),
            status=AgentStatus.IDLE,
        )

    async def register(
        self,
        agent_id: str,
        function_name: str,
        instance: int = 0,
        metadata: dict[str, object] | None = None,
    ) -> object:
        return await self._registry.register(
            agent_id,
            function_name,
            instance=instance,
            metadata=cast(dict[str, Any] | None, metadata),
        )

    async def update_status(self, agent_id: str, status: object) -> None:
        await self._registry.update_status(agent_id, AgentStatus(_status_name(status)))

    async def deregister(self, agent_id: str) -> None:
        await self._registry.deregister(agent_id)

    async def list_agents(
        self,
        function_name: str | None = None,
        status: object | None = None,
    ) -> list[object]:
        resolved_status = AgentStatus(_status_name(status)) if status is not None else None
        return cast(
            list[object],
            await self._registry.list_agents(function_name=function_name, status=resolved_status),
        )

    async def get(self, agent_id: str) -> object | None:
        return await self._registry.get(agent_id)

    async def route(
        self,
        agent_type: str,
        agent_id: str,
        method: str,
        args: RuntimeKwargs | None = None,
    ) -> object:
        agent_cls = self._resolve_agent_class(agent_type)
        handler = getattr(agent_cls, method, None)
        if handler is None or not getattr(handler, "__skaal_handler__", False):
            raise AttributeError(f"agent {agent_cls.__name__!r} has no handler {method!r}")

        lock = self._locks.setdefault(f"{agent_cls.__name__}:{agent_id}", asyncio.Lock())
        async with lock:
            record = await self._registry.get(agent_id)
            if record is None:
                await self.register(agent_id, agent_cls.__name__)

            await self.update_status(agent_id, AgentStatus.RUNNING)
            agent = self._instantiate_agent(agent_cls, agent_id)
            await self._hydrate_agent(agent_cls, agent_id, agent)

            try:
                result = getattr(agent, method)(**(args or {}))
                if inspect.isawaitable(result):
                    result = await result
                await self._persist_agent(agent_cls, agent_id, agent)
            except Exception:
                await self.update_status(agent_id, AgentStatus.ERROR)
                raise

            await self.update_status(agent_id, AgentStatus.IDLE)
            return result

    def _resolve_agent_class(self, agent_type: str) -> type[object]:
        agent_cls = self._agent_types.get(agent_type)
        if agent_cls is None:
            raise KeyError(f"agent type {agent_type!r} is not registered with the runtime")
        return agent_cls

    def _instantiate_agent(self, agent_cls: type[object], agent_id: str) -> object:
        try:
            signature = inspect.signature(agent_cls)
        except (TypeError, ValueError):
            signature = None

        kwargs: dict[str, object] = {}
        required: list[str] = []
        if signature is not None:
            for name, parameter in signature.parameters.items():
                if name == "agent_id":
                    kwargs[name] = agent_id
                    continue
                if parameter.default is inspect.Signature.empty and parameter.kind in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                ):
                    required.append(name)

        if required:
            raise TypeError(
                f"agent {agent_cls.__name__!r} must have a no-arg constructor or accept agent_id"
            )

        agent = agent_cls(**kwargs)
        setattr(agent, "agent_id", agent_id)
        return agent

    async def _hydrate_agent(self, agent_cls: type[object], agent_id: str, agent: object) -> None:
        if not getattr(agent_cls, "__skaal_agent__", {}).get("persistent", True):
            return
        fields: frozenset[str] = getattr(agent_cls, "__skaal_persistent_fields__", frozenset())
        if not fields:
            return

        stored = await self._state.get(self._state_key(agent_cls, agent_id))
        if not isinstance(stored, dict):
            return
        for field in fields:
            if field in stored:
                setattr(agent, field, stored[field])

    async def _persist_agent(self, agent_cls: type[object], agent_id: str, agent: object) -> None:
        if not getattr(agent_cls, "__skaal_agent__", {}).get("persistent", True):
            return
        fields: frozenset[str] = getattr(agent_cls, "__skaal_persistent_fields__", frozenset())
        if not fields:
            return

        payload = {field: getattr(agent, field) for field in fields if hasattr(agent, field)}
        await self._state.set(self._state_key(agent_cls, agent_id), payload)

    def _state_key(self, agent_cls: type[object], agent_id: str) -> str:
        qualified_name = self._qualified_names.get(agent_cls, agent_cls.__name__)
        return f"agent:{qualified_name}:{agent_id}:state"


class MeshAgentsService:
    def __init__(self, mesh: MeshClient) -> None:
        self._mesh = mesh

    def declare(self, qualified_name: str, agent_cls: type[object]) -> None:
        metadata = _agent_metadata(qualified_name, agent_cls)
        if self._mesh.get_agent(qualified_name) is None:
            self._mesh.register_agent(agent_cls.__name__, qualified_name, metadata=metadata)
        self._mesh.update_agent_status(qualified_name, AgentStatus.IDLE.value)

    async def register(
        self,
        agent_id: str,
        function_name: str,
        instance: int = 0,
        metadata: dict[str, object] | None = None,
    ) -> object:
        return self._mesh.register_agent(
            function_name,
            agent_id,
            instance=instance,
            metadata=cast(dict[str, Any] | None, metadata),
        )

    async def update_status(self, agent_id: str, status: object) -> None:
        self._mesh.update_agent_status(agent_id, _status_name(status))

    async def deregister(self, agent_id: str) -> None:
        self._mesh.deregister_agent(agent_id)

    async def list_agents(
        self,
        function_name: str | None = None,
        status: object | None = None,
    ) -> list[object]:
        resolved_status = _status_name(status) if status is not None else None
        return cast(
            list[object],
            self._mesh.list_agents(agent_type=function_name, status=resolved_status),
        )

    async def get(self, agent_id: str) -> object | None:
        return self._mesh.get_agent(agent_id)

    async def route(
        self,
        agent_type: str,
        agent_id: str,
        method: str,
        args: RuntimeKwargs | None = None,
    ) -> object:
        return self._mesh.route_agent_call(agent_type, agent_id, method, args or {})


class MeshStateService:
    def __init__(self, mesh: MeshClient) -> None:
        self._mesh = mesh

    async def get(self, key: str) -> object | None:
        return self._mesh.state_get(key)

    async def set(self, key: str, value: object) -> None:
        self._mesh.state_set(key, value)

    async def delete(self, key: str) -> None:
        self._mesh.state_delete(key)

    async def exists(self, key: str) -> bool:
        return self._mesh.state_exists(key)

    async def keys(self, prefix: str = "") -> list[str]:
        return self._mesh.state_keys(prefix)
