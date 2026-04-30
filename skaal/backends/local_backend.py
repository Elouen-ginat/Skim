"""In-memory storage backend and backward-compat wiring for plain classes."""

from __future__ import annotations

from typing import Any, List

# Re-export for backward compatibility — canonical location is now skaal.storage.
from skaal.storage import (  # noqa: F401
    _deserialize,
    _list_page_from_entries,
    _query_index_from_entries,
    _scan_page_from_entries,
    _serialize,
)
from skaal.sync import run as _sync_bridge_run

# ── Sync/async bridge ─────────────────────────────────────────────────────────

# Backward-compatible private alias; public callers should use skaal.sync.run
# or skaal.sync_run instead of importing from a backend module.
_sync_run = _sync_bridge_run


# ── LocalMap ───────────────────────────────────────────────────────────────────


class LocalMap:
    """
    In-memory key-value store that satisfies the :class:`~skaal.backends.base.StorageBackend`
    protocol.

    Used by :class:`~skaal.runtime.local.LocalRuntime` to back storage classes
    during local development and testing.  All methods are async to match the
    production backend interface.
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        import asyncio

        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        return self._data.get(key)

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def list(self) -> list[tuple[str, Any]]:
        return list(self._data.items())

    async def list_page(self, *, limit: int, cursor: str | None):
        return _list_page_from_entries(await self.list(), limit=limit, cursor=cursor)

    async def scan(self, prefix: str = "") -> List[tuple[str, Any]]:
        return [(k, v) for k, v in self._data.items() if k.startswith(prefix)]

    async def scan_page(self, prefix: str = "", *, limit: int, cursor: str | None):
        return _scan_page_from_entries(
            await self.scan(prefix),
            prefix=prefix,
            limit=limit,
            cursor=cursor,
        )

    async def query_index(
        self,
        index_name: str,
        key: Any,
        *,
        limit: int,
        cursor: str | None,
    ):
        return _query_index_from_entries(
            await self.list(),
            backend=self,
            index_name=index_name,
            key=key,
            limit=limit,
            cursor=cursor,
        )

    async def increment_counter(self, key: str, delta: int = 1) -> int:
        """Atomically increment a counter using a lock."""
        async with self._lock:
            current = int(self._data.get(key, 0))
            new_value = current + delta
            self._data[key] = new_value
            return new_value

    async def atomic_update(self, key: str, fn: Any) -> Any:
        """Atomically read, apply fn to the raw value, write back, and return the result."""
        async with self._lock:
            current = self._data.get(key)
            updated = fn(current)
            self._data[key] = updated
            return updated

    async def close(self) -> None:
        pass

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"LocalMap({len(self._data)} keys)"
