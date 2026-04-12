"""In-memory state store for the local runtime."""

from __future__ import annotations

import asyncio
from typing import Any


class InMemoryStateStore:
    """Thread-safe in-memory key/value store backed by a dict and asyncio.Lock.

    Used by :class:`~skaal.runtime.local.LocalRuntime` during local development
    and testing.  All methods are async so they can be awaited uniformly.
    """

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
