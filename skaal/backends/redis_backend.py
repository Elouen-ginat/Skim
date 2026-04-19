"""Async Redis storage backend using redis.asyncio."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, List

from skaal.errors import SkaalConflict, SkaalUnavailable


class RedisBackend:
    """
    Redis storage backend using redis.asyncio.

    Keys are stored as: skaal:{namespace}:{key}
    Values are JSON-serialized.

    scan(prefix) uses SCAN with MATCH pattern.
    list() uses SCAN * then MGET.

    Connection is lazy and **per event-loop**: each asyncio event loop that
    uses this backend gets its own redis.asyncio client and connection pool.
    This avoids "Future attached to a different loop" errors when the same
    backend instance is shared between the scheduler daemon thread (which has
    its own loop) and the sync-bridge background loop used by Dash callbacks.
    """

    def __init__(self, url: str = "redis://localhost:6379", namespace: str = "default") -> None:
        self.url = url
        self.namespace = namespace
        # Keyed by id(event_loop) so every loop gets its own connection pool.
        self._clients: dict[int, Any] = {}

    def _key(self, key: str) -> str:
        return f"skaal:{self.namespace}:{key}"

    def _strip_prefix(self, full_key: str) -> str:
        prefix = f"skaal:{self.namespace}:"
        if full_key.startswith(prefix):
            return full_key[len(prefix) :]
        return full_key

    async def connect(self) -> None:
        """Create a Redis client for the current event loop. Called lazily on first use."""
        await self._ensure_connected()

    async def _ensure_connected(self) -> Any:
        """Return the client for the running event loop, creating it if needed."""
        import redis.asyncio as aioredis  # type: ignore[import-untyped]

        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        if loop_id not in self._clients:
            self._clients[loop_id] = aioredis.from_url(self.url, decode_responses=True)
        return self._clients[loop_id]

    async def get(self, key: str) -> Any | None:
        client = await self._ensure_connected()
        raw = await client.get(self._key(key))
        if raw is None:
            return None
        return json.loads(raw)

    async def set(self, key: str, value: Any) -> None:
        client = await self._ensure_connected()
        await client.set(self._key(key), json.dumps(value))

    async def delete(self, key: str) -> None:
        client = await self._ensure_connected()
        await client.delete(self._key(key))

    async def list(self) -> list[tuple[str, Any]]:
        client = await self._ensure_connected()
        pattern = f"skaal:{self.namespace}:*"
        keys: list[str] = []
        async for k in client.scan_iter(match=pattern):
            keys.append(k)
        if not keys:
            return []
        values = await client.mget(*keys)
        result = []
        for k, v in zip(keys, values):
            if v is not None:
                result.append((self._strip_prefix(k), json.loads(v)))
        return result

    async def scan(self, prefix: str = "") -> List[tuple[str, Any]]:
        """Scan keys with prefix, using MGET for efficient bulk retrieval."""
        client = await self._ensure_connected()
        pattern = f"skaal:{self.namespace}:{prefix}*"
        keys: list[str] = []
        async for k in client.scan_iter(match=pattern):
            keys.append(k)
        if not keys:
            return []
        values = await client.mget(*keys)
        result = []
        for k, v in zip(keys, values):
            if v is not None:
                result.append((self._strip_prefix(k), json.loads(v)))
        return result

    async def increment_counter(self, key: str, delta: int = 1) -> int:
        """Atomically increment a counter using Redis INCR."""
        client = await self._ensure_connected()
        new_value = await client.incrby(self._key(key), delta)
        return int(new_value)

    async def atomic_update(
        self,
        key: str,
        fn: Callable[[Any], Any],
        *,
        max_retries: int = 64,
    ) -> Any:
        """Atomically read, apply *fn*, and write back using a Redis pipeline with WATCH.

        Retries up to *max_retries* times on ``WatchError`` before surfacing
        a :class:`skaal.errors.SkaalConflict`.  Transient connection errors
        become :class:`skaal.errors.SkaalUnavailable`.
        """
        import redis.asyncio as aioredis
        from redis.exceptions import (  # type: ignore[import-untyped]
            ConnectionError as RedisConnectionError,
        )

        client = await self._ensure_connected()
        full_key = self._key(key)
        try:
            async with client.pipeline(transaction=True) as pipe:
                for _ in range(max_retries):
                    try:
                        await pipe.watch(full_key)
                        raw = await pipe.get(full_key)
                        current = json.loads(raw) if raw is not None else None
                        new_value = fn(current)
                        pipe.multi()
                        pipe.set(full_key, json.dumps(new_value))
                        await pipe.execute()
                        return new_value
                    except aioredis.WatchError:
                        continue
                raise SkaalConflict(
                    f"atomic_update on {key!r} lost {max_retries} consecutive races"
                )
        except RedisConnectionError as exc:
            raise SkaalUnavailable(f"Redis unavailable: {exc}") from exc

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    def __repr__(self) -> str:
        return f"RedisBackend(url={self.url!r}, namespace={self.namespace!r})"
