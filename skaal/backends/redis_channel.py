"""Redis Streams channel backend for distributed pub/sub.

Implements the :class:`~skaal.runtime.channels.Channel` interface using
Redis Streams (``XADD`` / ``XREADGROUP``).  Each topic maps to a Redis
Stream key; consumer groups provide competing-consumer semantics.

Usage::

    from skaal.backends.redis_channel import RedisStreamChannel

    ch = RedisStreamChannel(url="redis://localhost:6379")
    await ch.connect()

    # Publish
    await ch.publish("orders", {"id": "o1", "total": 42})

    # Subscribe (consumer group)
    async for msg in ch.subscribe("orders", group="projector", consumer="w1"):
        process(msg)
        await ch.ack("orders", "projector", msg["_id"])
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any


class RedisStreamChannel:
    """
    Distributed pub/sub channel backed by Redis Streams.

    Each topic is a Redis Stream keyed as ``skaal:ch:{namespace}:{topic}``.
    Subscribers use ``XREADGROUP`` with consumer groups for at-least-once
    delivery.  A ``subscribe()`` call auto-creates the consumer group on
    first use.

    Messages are JSON-serialized dicts.  The special ``_id`` field is injected
    on read so callers can acknowledge messages via :meth:`ack`.
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379",
        namespace: str = "default",
    ) -> None:
        self.url = url
        self.namespace = namespace
        self._client: Any = None

    def _stream_key(self, topic: str) -> str:
        return f"skaal:ch:{self.namespace}:{topic}"

    async def connect(self) -> None:
        """Create the async Redis client."""
        import redis.asyncio as aioredis

        self._client = aioredis.from_url(self.url, decode_responses=True)

    async def _ensure_connected(self) -> None:
        if self._client is None:
            await self.connect()

    # ── Publish ───────────────────────────────────────────────────────────────

    async def publish(self, topic: str, message: Any) -> str:
        """
        Append *message* to the stream for *topic*.

        Returns the Redis stream message ID (e.g. ``"1234567890-0"``).
        """
        await self._ensure_connected()
        key = self._stream_key(topic)
        payload = json.dumps(message) if not isinstance(message, str) else message
        msg_id: str = await self._client.xadd(key, {"data": payload})
        return msg_id

    # ── Subscribe ─────────────────────────────────────────────────────────────

    async def subscribe(
        self,
        topic: str,
        *,
        group: str = "default",
        consumer: str = "worker-0",
        from_beginning: bool = False,
        poll_interval_ms: int = 100,
        batch_size: int = 10,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Yield messages from *topic* using a Redis consumer group.

        Auto-creates the consumer group if it doesn't exist.  Messages are
        delivered with at-least-once semantics; call :meth:`ack` after
        processing to remove them from the pending entries list (PEL).

        Each yielded dict has a ``_id`` field with the stream entry ID
        and the original message payload merged in.

        Args:
            topic:             Stream topic name.
            group:             Consumer group name.
            consumer:          Consumer name within the group.
            from_beginning:    If ``True``, read all historical messages on
                               first subscription.  Otherwise start from new
                               messages only (``$``).
            poll_interval_ms:  Block time per ``XREADGROUP`` call (milliseconds).
            batch_size:        Max messages per ``XREADGROUP`` call.
        """
        await self._ensure_connected()
        key = self._stream_key(topic)

        # Create the consumer group (idempotent).
        start_id = "0" if from_beginning else "$"
        try:
            await self._client.xgroup_create(key, group, id=start_id, mkstream=True)
        except Exception as exc:  # noqa: BLE001
            # BUSYGROUP = group already exists — safe to ignore.
            if "BUSYGROUP" not in str(exc):
                raise

        while True:
            entries = await self._client.xreadgroup(
                groupname=group,
                consumername=consumer,
                streams={key: ">"},
                count=batch_size,
                block=poll_interval_ms,
            )
            if not entries:
                continue
            for _stream_name, messages in entries:
                for msg_id, fields in messages:
                    raw = fields.get("data", "{}")
                    try:
                        payload = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        payload = {"data": raw}

                    if isinstance(payload, dict):
                        payload["_id"] = msg_id
                    else:
                        payload = {"data": payload, "_id": msg_id}

                    yield payload

    # ── Acknowledge ───────────────────────────────────────────────────────────

    async def ack(self, topic: str, group: str, message_id: str) -> None:
        """
        Acknowledge a message so it is removed from the consumer group PEL.

        Args:
            topic:      Stream topic name.
            group:      Consumer group name.
            message_id: The ``_id`` field from the yielded message dict.
        """
        await self._ensure_connected()
        await self._client.xack(self._stream_key(topic), group, message_id)

    # ── Utilities ─────────────────────────────────────────────────────────────

    async def pending(self, topic: str, group: str) -> int:
        """Return the number of pending (unacknowledged) messages for *group*."""
        await self._ensure_connected()
        info = await self._client.xpending(self._stream_key(topic), group)
        # xpending returns a dict or list depending on redis-py version;
        # the first element / "pending" key is the pending count.
        if isinstance(info, dict):
            return int(info.get("pending", 0))
        if isinstance(info, (list, tuple)) and info:
            return int(info[0])
        return 0

    async def stream_length(self, topic: str) -> int:
        """Return the number of entries in the stream for *topic*."""
        await self._ensure_connected()
        return await self._client.xlen(self._stream_key(topic))

    async def close(self) -> None:
        """Close the Redis connection."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def __repr__(self) -> str:
        return f"RedisStreamChannel(url={self.url!r}, namespace={self.namespace!r})"
