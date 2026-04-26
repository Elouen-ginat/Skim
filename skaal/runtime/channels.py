"""Pub/sub channels for inter-function communication.

Provides the in-process channel helper used by ``wire_local`` during local
development and tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any


class LocalChannel:
    """
    In-process pub/sub channel backed by ``asyncio.Queue`` objects.

    Suitable for local development and testing only.  Each call to
    :meth:`subscribe` creates an independent consumer queue for that topic.
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[asyncio.Queue[Any]]] = {}

    async def publish(self, topic: str, message: Any) -> None:
        for q in self._queues.get(topic, []):
            await q.put(message)

    async def subscribe(self, topic: str) -> "AsyncIterator[Any]":
        q: asyncio.Queue[Any] = asyncio.Queue()
        self._queues.setdefault(topic, []).append(q)
        try:
            while True:
                msg = await q.get()
                yield msg
        finally:
            queues = self._queues.get(topic, [])
            if q in queues:
                queues.remove(q)
            # Clean up topic if no subscribers remain
            if not queues:
                self._queues.pop(topic, None)
