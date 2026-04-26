from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from skaal.channel import Channel


class LocalChannel:
    def __init__(self) -> None:
        self._queues: dict[str, list[asyncio.Queue[Any]]] = {}

    async def publish(self, topic: str, message: Any) -> None:
        for queue in self._queues.get(topic, []):
            await queue.put(message)

    async def subscribe(self, topic: str) -> AsyncIterator[Any]:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._queues.setdefault(topic, []).append(queue)
        try:
            while True:
                message = await queue.get()
                yield message
        finally:
            queues = self._queues.get(topic, [])
            if queue in queues:
                queues.remove(queue)
            if not queues:
                self._queues.pop(topic, None)


def wire_local(channel: "Channel[Any]", *, topic: str = "default") -> None:
    local = LocalChannel()
    bound_topic = topic

    async def _send(item: Any) -> None:
        await local.publish(bound_topic, item)

    async def _receive() -> AsyncIterator[Any]:
        async for message in local.subscribe(bound_topic):
            yield message

    channel.send = _send  # type: ignore[method-assign]
    channel.receive = _receive  # type: ignore[method-assign]
    channel._backend_name = "local"
    channel._wired = True


__all__ = ["LocalChannel", "wire_local"]
