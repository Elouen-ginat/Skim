"""StorageBackend Protocol — the interface all backends must implement."""

from __future__ import annotations

import builtins
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    """
    Protocol for async key-value storage backends.

    All Skaal storage backends (LocalMap, RedisBackend, DynamoBackend) implement
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

    async def list(self) -> builtins.list[tuple[str, Any]]:
        """Return all (key, value) pairs."""
        ...

    async def scan(self, prefix: str = "") -> builtins.list[tuple[str, Any]]:
        """Return all (key, value) pairs where key starts with prefix."""
        ...

    async def increment_counter(self, key: str, delta: int = 1) -> int:
        """
        Atomically increment a counter and return the new value.

        This method must be atomic to prevent race conditions in concurrent increment scenarios.
        Backends that don't support atomic operations should use a lock or transaction.

        Args:
            key: The counter key.
            delta: Amount to increment (default 1).

        Returns:
            The new counter value after the increment.
        """
        ...

    async def atomic_update(self, key: str, fn: Callable[[Any], Any]) -> Any:
        """
        Atomically read the raw value for key, apply fn, write the result back,
        and return the new raw value.

        Implementations must guarantee that no other write to key can occur
        between the read and the write (e.g. via a lock or a transaction).
        """
        ...

    async def close(self) -> None:
        """Release any resources held by this backend."""
        ...

    def __repr__(self) -> str: ...
