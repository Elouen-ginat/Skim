"""Async Redis storage backend using redis.asyncio."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Callable, List

from skaal.errors import SkaalConflict, SkaalUnavailable
from skaal.storage import (
    _cursor_identity,
    _encode_cursor,
    _field_value,
    _get_backend_indexes,
    _normalize_limit,
    _sort_token,
    _validate_cursor,
)
from skaal.types.storage import Page


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

    def _key_index(self) -> str:
        return f"skaal:{self.namespace}:__keys__"

    def _index_bucket_key(self, index_name: str, partition_key: Any) -> str:
        token = base64.urlsafe_b64encode(
            json.dumps(partition_key, sort_keys=True, default=str).encode("utf-8")
        ).decode("ascii")
        return f"skaal:{self.namespace}:__idx__:{index_name}:{token}"

    async def _rewrite_index_bucket(
        self,
        client: Any,
        *,
        index_name: str,
        partition_key: Any,
        mutator: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    ) -> None:
        bucket_key = self._index_bucket_key(index_name, partition_key)
        raw_entries = await client.lrange(bucket_key, 0, -1)
        entries = [json.loads(entry) for entry in raw_entries]
        updated = mutator(entries)
        updated.sort(key=lambda item: (_sort_token(item.get("sort")), item["pk"]))
        pipe = client.pipeline(transaction=True)
        pipe.delete(bucket_key)
        if updated:
            pipe.rpush(bucket_key, *[json.dumps(entry) for entry in updated])
        await pipe.execute()

    async def _ensure_key_index(self, client: Any) -> None:
        if await client.zcard(self._key_index()) > 0:
            return
        pattern = f"skaal:{self.namespace}:*"
        keys: list[str] = []
        async for full_key in client.scan_iter(match=pattern):
            stripped = self._strip_prefix(full_key)
            if stripped.startswith("__"):
                continue
            keys.append(stripped)
        if keys:
            await client.zadd(self._key_index(), {key: 0 for key in keys})

    async def _sync_indexes(self, client: Any, key: str, old_value: Any, new_value: Any) -> None:
        for index_name, index in _get_backend_indexes(self).items():
            old_partition = (
                _field_value(old_value, index.partition_key) if old_value is not None else None
            )
            new_partition = (
                _field_value(new_value, index.partition_key) if new_value is not None else None
            )

            if old_partition is not None:

                def _remove(
                    entries: list[dict[str, Any]], existing_key: str = key
                ) -> list[dict[str, Any]]:
                    return [entry for entry in entries if entry["pk"] != existing_key]

                await self._rewrite_index_bucket(
                    client,
                    index_name=index_name,
                    partition_key=old_partition,
                    mutator=_remove,
                )

            if new_partition is not None:
                sort_value = (
                    _field_value(new_value, index.sort_key) if index.sort_key is not None else key
                )

                def _add(
                    entries: list[dict[str, Any]], key: str = key, sort_value: Any = sort_value
                ):
                    filtered = [entry for entry in entries if entry["pk"] != key]
                    filtered.append({"pk": key, "sort": sort_value})
                    return filtered

                await self._rewrite_index_bucket(
                    client,
                    index_name=index_name,
                    partition_key=new_partition,
                    mutator=_add,
                )

    async def connect(self) -> None:
        """Create a Redis client for the current event loop. Called lazily on first use."""
        await self._ensure_connected()

    async def _ensure_connected(self) -> Any:
        """Return the client for the running event loop, creating it if needed."""
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
        full_key = self._key(key)
        raw_old = await client.get(full_key)
        old_value = json.loads(raw_old) if raw_old is not None else None
        pipe = client.pipeline(transaction=True)
        pipe.set(full_key, json.dumps(value))
        pipe.zadd(self._key_index(), {key: 0})
        await pipe.execute()
        await self._sync_indexes(client, key, old_value, value)

    async def delete(self, key: str) -> None:
        client = await self._ensure_connected()
        full_key = self._key(key)
        raw_old = await client.get(full_key)
        old_value = json.loads(raw_old) if raw_old is not None else None
        pipe = client.pipeline(transaction=True)
        pipe.delete(full_key)
        pipe.zrem(self._key_index(), key)
        await pipe.execute()
        if old_value is not None:
            await self._sync_indexes(client, key, old_value, None)

    async def list(self) -> list[tuple[str, Any]]:
        client = await self._ensure_connected()
        await self._ensure_key_index(client)
        keys = await client.zrange(self._key_index(), 0, -1)
        if not keys:
            return []
        values = await client.mget(*[self._key(key) for key in keys])
        result = []
        for k, v in zip(keys, values):
            if v is not None:
                result.append((k, json.loads(v)))
        return result

    async def list_page(self, *, limit: int, cursor: str | None):
        client = await self._ensure_connected()
        await self._ensure_key_index(client)
        limit = _normalize_limit(limit)
        decoded = _validate_cursor(cursor, mode="list")
        last_key = decoded.get("last_key")
        min_value = f"({last_key}" if last_key is not None else "-"
        keys = await client.zrangebylex(self._key_index(), min_value, "+", start=0, num=limit + 1)
        page_keys = keys[:limit]
        has_more = len(keys) > limit
        values = await client.mget(*[self._key(key) for key in page_keys]) if page_keys else []
        items = [
            (key, json.loads(value)) for key, value in zip(page_keys, values) if value is not None
        ]
        next_cursor = None
        if has_more and page_keys:
            next_cursor = _encode_cursor({"mode": "list", "last_key": page_keys[-1]})
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    async def scan(self, prefix: str = "") -> List[tuple[str, Any]]:
        page = await self.scan_page(prefix=prefix, limit=10_000, cursor=None)
        items = list(page.items)
        while page.has_more:
            page = await self.scan_page(prefix=prefix, limit=10_000, cursor=page.next_cursor)
            items.extend(page.items)
        return items

    async def scan_page(self, prefix: str = "", *, limit: int, cursor: str | None):
        client = await self._ensure_connected()
        await self._ensure_key_index(client)
        limit = _normalize_limit(limit)
        decoded = _validate_cursor(cursor, mode="scan", extra={"prefix": prefix})
        if prefix:
            last_key = decoded.get("last_key")
            min_value = f"({last_key}" if last_key is not None else f"[{prefix}"
            max_value = f"[{prefix}\uffff"
            keys = await client.zrangebylex(
                self._key_index(),
                min_value,
                max_value,
                start=0,
                num=limit + 1,
            )
        else:
            return await self.list_page(limit=limit, cursor=cursor)

        page_keys = keys[:limit]
        has_more = len(keys) > limit
        values = await client.mget(*[self._key(key) for key in page_keys]) if page_keys else []
        items = [
            (key, json.loads(value)) for key, value in zip(page_keys, values) if value is not None
        ]
        next_cursor = None
        if has_more and page_keys:
            next_cursor = _encode_cursor(
                {"mode": "scan", "prefix": prefix, "last_key": page_keys[-1]}
            )
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    async def query_index(
        self,
        index_name: str,
        key: Any,
        *,
        limit: int,
        cursor: str | None,
    ):
        client = await self._ensure_connected()
        limit = _normalize_limit(limit)
        decoded = _validate_cursor(
            cursor,
            mode="index",
            extra={"index_name": index_name, "key": _cursor_identity(key)},
        )
        offset = int(decoded.get("offset", 0)) if decoded else 0
        bucket_key = self._index_bucket_key(index_name, key)
        raw_entries = await client.lrange(bucket_key, offset, offset + limit)
        entries = [json.loads(entry) for entry in raw_entries]
        page_entries = entries[:limit]
        has_more = len(entries) > limit
        primary_keys = [entry["pk"] for entry in page_entries]
        values = (
            await client.mget(*[self._key(primary_key) for primary_key in primary_keys])
            if primary_keys
            else []
        )
        items = [json.loads(value) for value in values if value is not None]
        next_cursor = None
        if has_more:
            next_cursor = _encode_cursor(
                {
                    "mode": "index",
                    "index_name": index_name,
                    "key": _cursor_identity(key),
                    "offset": offset + len(page_entries),
                }
            )
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    async def increment_counter(self, key: str, delta: int = 1) -> int:
        """Atomically increment a counter using Redis INCR."""
        client = await self._ensure_connected()
        new_value = await client.incrby(self._key(key), delta)
        await client.zadd(self._key_index(), {key: 0})
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
        from redis.exceptions import (
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
                        pipe.zadd(self._key_index(), {key: 0})
                        await pipe.execute()
                        await self._sync_indexes(client, key, current, new_value)
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
