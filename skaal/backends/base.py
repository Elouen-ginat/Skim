"""StorageBackend Protocol — the interface all backends must implement."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    """
    Protocol for async key-value storage backends.

    All Skim storage backends (LocalMap, RedisBackend, DynamoBackend) implement
    this interface. The protocol is runtime-checkable so isinstance() works.
    """

    async def get(self, key: str) -> Any | None:
        """Return the value for key, or None if not found."""
        ...

    async def set(self, key: str, value: Any) -> None:
        """Store value under key."""
        ...

    async def delete(self, key: str) -> None:
        """Remove key (no-op if not present)."""
        ...

    async def list(self) -> list[tuple[str, Any]]:
        """Return all (key, value) pairs."""
        ...

    async def scan(self, prefix: str = "") -> list[tuple[str, Any]]:
        """Return all (key, value) pairs where key starts with prefix."""
        ...

    async def close(self) -> None:
        """Release any resources held by this backend."""
        ...

    def __repr__(self) -> str:
        ...
