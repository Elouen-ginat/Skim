from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from skaal.channel import Channel


class RedisStreamChannel:
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
        import redis.asyncio as aioredis

        self._client = aioredis.from_url(self.url, decode_responses=True)

    async def _ensure_connected(self) -> None:
        if self._client is None:
            await self.connect()

    async def publish(self, topic: str, message: Any) -> str:
        await self._ensure_connected()
        key = self._stream_key(topic)
        payload = json.dumps(message) if not isinstance(message, str) else message
        msg_id: str = await self._client.xadd(key, {"data": payload})
        return msg_id

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
        await self._ensure_connected()
        key = self._stream_key(topic)

        start_id = "0" if from_beginning else "$"
        try:
            await self._client.xgroup_create(key, group, id=start_id, mkstream=True)
        except Exception as exc:  # noqa: BLE001
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

    async def ack(self, topic: str, group: str, message_id: str) -> None:
        await self._ensure_connected()
        await self._client.xack(self._stream_key(topic), group, message_id)

    async def pending(self, topic: str, group: str) -> int:
        await self._ensure_connected()
        info = await self._client.xpending(self._stream_key(topic), group)
        if isinstance(info, dict):
            return int(info.get("pending", 0))
        if isinstance(info, (list, tuple)) and info:
            return int(info[0])
        return 0

    async def stream_length(self, topic: str) -> int:
        await self._ensure_connected()
        return await self._client.xlen(self._stream_key(topic))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def __repr__(self) -> str:
        return f"RedisStreamChannel(url={self.url!r}, namespace={self.namespace!r})"


def wire_redis(
    channel: "Channel[Any]",
    *,
    url: str = "redis://localhost:6379",
    namespace: str = "default",
    topic: str = "default",
    group: str = "default",
    consumer: str = "worker-0",
) -> None:
    backend = RedisStreamChannel(url=url, namespace=namespace)
    bound_topic = topic
    bound_group = group
    bound_consumer = consumer

    async def _send(item: Any) -> None:
        await backend.publish(bound_topic, item)

    async def _receive() -> AsyncIterator[Any]:
        async for message in backend.subscribe(
            bound_topic,
            group=bound_group,
            consumer=bound_consumer,
        ):
            message.pop("_id", None)
            yield message

    channel.send = _send  # type: ignore[method-assign]
    channel.receive = _receive  # type: ignore[method-assign]
    channel._backend_name = "redis-streams"
    channel._wired = True
    channel._redis_backend = backend  # type: ignore[attr-defined]


__all__ = ["RedisStreamChannel", "wire_redis"]
