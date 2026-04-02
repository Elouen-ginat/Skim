"""Distributed state management for Skaal runtimes.

Provides a thin abstraction over key/value state stores that can be used
both locally (in-memory) and in distributed deployments (Redis, DynamoDB, …).
"""

from __future__ import annotations

import asyncio
from typing import Any


class StateStore:
    """
    Abstract key/value state store.

    Concrete implementations live in ``skaal.backends.*``.  The in-memory
    implementation below is used for local development and testing.
    """

    async def get(self, key: str) -> Any:
        raise NotImplementedError

    async def set(self, key: str, value: Any) -> None:
        raise NotImplementedError

    async def delete(self, key: str) -> None:
        raise NotImplementedError

    async def exists(self, key: str) -> bool:
        raise NotImplementedError

    async def keys(self, prefix: str = "") -> list[str]:
        raise NotImplementedError


class InMemoryStateStore(StateStore):
    """Thread-safe in-memory state store backed by a dict and asyncio.Lock."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any:
        async with self._lock:
            return self._data.get(key)

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            self._data[key] = value

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def exists(self, key: str) -> bool:
        async with self._lock:
            return key in self._data

    async def keys(self, prefix: str = "") -> list[str]:
        async with self._lock:
            return [k for k in self._data if k.startswith(prefix)]
