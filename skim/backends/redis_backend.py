"""Async Redis storage backend using redis.asyncio."""

from __future__ import annotations

import json
from typing import Any


class RedisBackend:
    """
    Redis storage backend using redis.asyncio.

    Keys are stored as: skim:{namespace}:{key}
    Values are JSON-serialized.

    scan(prefix) uses SCAN with MATCH pattern.
    list() uses SCAN * then MGET.

    Connection is lazy: the client is created on first use if connect()
    has not been called explicitly.
    """

    def __init__(self, url: str = "redis://localhost:6379", namespace: str = "default") -> None:
        self.url = url
        self.namespace = namespace
        self._client: Any | None = None

    def _key(self, key: str) -> str:
        return f"skim:{self.namespace}:{key}"

    def _strip_prefix(self, full_key: str) -> str:
        prefix = f"skim:{self.namespace}:"
        if full_key.startswith(prefix):
            return full_key[len(prefix):]
        return full_key

    async def connect(self) -> None:
        """Create the async Redis client. Call before first use."""
        import redis.asyncio as aioredis
        self._client = aioredis.from_url(self.url, decode_responses=True)

    async def _ensure_connected(self) -> None:
        if self._client is None:
            await self.connect()

    async def get(self, key: str) -> Any | None:
        await self._ensure_connected()
        raw = await self._client.get(self._key(key))
        if raw is None:
            return None
        return json.loads(raw)

    async def set(self, key: str, value: Any) -> None:
        await self._ensure_connected()
        await self._client.set(self._key(key), json.dumps(value))

    async def delete(self, key: str) -> None:
        await self._ensure_connected()
        await self._client.delete(self._key(key))

    async def list(self) -> list[tuple[str, Any]]:
        await self._ensure_connected()
        pattern = f"skim:{self.namespace}:*"
        keys: list[str] = []
        async for k in self._client.scan_iter(match=pattern):
            keys.append(k)
        if not keys:
            return []
        values = await self._client.mget(*keys)
        result = []
        for k, v in zip(keys, values):
            if v is not None:
                result.append((self._strip_prefix(k), json.loads(v)))
        return result

    async def scan(self, prefix: str = "") -> list[tuple[str, Any]]:
        await self._ensure_connected()
        pattern = f"skim:{self.namespace}:{prefix}*"
        result = []
        async for k in self._client.scan_iter(match=pattern):
            raw = await self._client.get(k)
            if raw is not None:
                result.append((self._strip_prefix(k), json.loads(raw)))
        return result

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def __repr__(self) -> str:
        return f"RedisBackend(url={self.url!r}, namespace={self.namespace!r})"
