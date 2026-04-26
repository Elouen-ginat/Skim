from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, List

from skaal.backends._spec import BackendSpec, Wiring
from skaal.deploy.kinds import StorageKind
from skaal.errors import SkaalConflict, SkaalUnavailable


class RedisBackend:
    def __init__(self, url: str = "redis://localhost:6379", namespace: str = "default") -> None:
        self.url = url
        self.namespace = namespace
        self._clients: dict[int, Any] = {}

    def _key(self, key: str) -> str:
        return f"skaal:{self.namespace}:{key}"

    def _strip_prefix(self, full_key: str) -> str:
        prefix = f"skaal:{self.namespace}:"
        if full_key.startswith(prefix):
            return full_key[len(prefix) :]
        return full_key

    async def connect(self) -> None:
        await self._ensure_connected()

    async def _ensure_connected(self) -> Any:
        import redis.asyncio as aioredis

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
        async for key_name in client.scan_iter(match=pattern):
            keys.append(key_name)
        if not keys:
            return []
        values = await client.mget(*keys)
        result = []
        for key_name, value in zip(keys, values):
            if value is not None:
                result.append((self._strip_prefix(key_name), json.loads(value)))
        return result

    async def scan(self, prefix: str = "") -> List[tuple[str, Any]]:
        client = await self._ensure_connected()
        pattern = f"skaal:{self.namespace}:{prefix}*"
        keys: list[str] = []
        async for key_name in client.scan_iter(match=pattern):
            keys.append(key_name)
        if not keys:
            return []
        values = await client.mget(*keys)
        result = []
        for key_name, value in zip(keys, values):
            if value is not None:
                result.append((self._strip_prefix(key_name), json.loads(value)))
        return result

    async def increment_counter(self, key: str, delta: int = 1) -> int:
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
        import redis.asyncio as aioredis
        from redis.exceptions import ConnectionError as RedisConnectionError

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


LOCAL_REDIS_SPEC = BackendSpec(
    name="local-redis",
    kinds=frozenset({StorageKind.KV}),
    wiring=Wiring(
        class_name="RedisBackend",
        module="skaal.backends.kv.redis",
        env_prefix="SKAAL_REDIS_URL",
        uses_namespace=True,
        local_service="redis",
        local_env_value="redis://redis:6379",
        dependency_sets=("redis-runtime",),
    ),
    supported_targets=frozenset({"local"}),
)

MEMORYSTORE_REDIS_SPEC = BackendSpec(
    name="memorystore-redis",
    kinds=frozenset({StorageKind.KV}),
    wiring=Wiring(
        class_name="RedisBackend",
        module="skaal.backends.kv.redis",
        env_prefix="SKAAL_REDIS_URL",
        uses_namespace=True,
        requires_vpc=True,
        local_service="redis",
        local_env_value="redis://redis:6379",
        dependency_sets=("redis-runtime",),
    ),
    supported_targets=frozenset({"gcp"}),
    local_fallbacks={StorageKind.KV: "local-redis"},
)

__all__ = ["LOCAL_REDIS_SPEC", "MEMORYSTORE_REDIS_SPEC", "RedisBackend"]
