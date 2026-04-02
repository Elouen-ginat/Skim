"""Agent registry — tracks running function agents in a distributed deployment.

In the local runtime agents are just coroutines.  In a distributed mesh,
this registry is backed by the Rust mesh via gRPC.  This module provides
the shared interface.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AgentStatus(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    IDLE = "idle"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class AgentRecord:
    agent_id: str
    function_name: str
    status: AgentStatus = AgentStatus.STARTING
    instance: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class AgentRegistry:
    """
    In-process agent registry for local development.

    In production, this is replaced by the distributed mesh registry.
    """

    def __init__(self) -> None:
        self._agents: dict[str, AgentRecord] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        agent_id: str,
        function_name: str,
        instance: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> AgentRecord:
        async with self._lock:
            record = AgentRecord(
                agent_id=agent_id,
                function_name=function_name,
                status=AgentStatus.STARTING,
                instance=instance,
                metadata=metadata or {},
            )
            self._agents[agent_id] = record
            return record

    async def update_status(self, agent_id: str, status: AgentStatus) -> None:
        async with self._lock:
            if agent_id in self._agents:
                self._agents[agent_id].status = status

    async def deregister(self, agent_id: str) -> None:
        async with self._lock:
            self._agents.pop(agent_id, None)

    async def list_agents(
        self,
        function_name: str | None = None,
        status: AgentStatus | None = None,
    ) -> list[AgentRecord]:
        async with self._lock:
            records = list(self._agents.values())
        if function_name:
            records = [r for r in records if r.function_name == function_name]
        if status:
            records = [r for r in records if r.status == status]
        return records

    async def get(self, agent_id: str) -> AgentRecord | None:
        async with self._lock:
            return self._agents.get(agent_id)
