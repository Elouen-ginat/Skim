"""Channel[T] — typed distributed message bus."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class Channel(Generic[T]):
    """
    A typed, buffered channel for inter-component communication.

    When marked ``@shared``, the channel becomes a distributed message bus backed
    by Redis Streams or Kafka. When local, it is an in-process ``asyncio.Queue``.

    The runtime (:class:`~skaal.runtime.local.LocalRuntime` or
    :class:`~skaal.runtime.distributed.DistributedRuntime`) patches the
    channel's ``send`` / ``receive`` methods with a concrete backend.
    Before patching, both raise :class:`NotImplementedError`.

    Usage::

        events: Channel[GameEvent] = Channel(buffer=1000)

        # Producer
        await events.send(GameEvent(...))

        # Consumer
        async for event in events.receive():
            handle(event)
    """

    def __init__(self, buffer: int = 1000) -> None:
        self.buffer = buffer
        self._backend_name: str | None = None  # set by runtime after planning
        self._wired: bool = False

    async def send(self, item: T) -> None:
        """Send an item to the channel.

        Raises :class:`NotImplementedError` until the runtime wires a backend.
        """
        raise NotImplementedError(
            "Channel.send() is not wired yet. "
            "Use LocalRuntime or DistributedRuntime to wire a backend."
        )

    async def receive(self) -> AsyncIterator[T]:
        """Receive items from the channel as an async iterator.

        Raises :class:`NotImplementedError` until the runtime wires a backend.
        """
        raise NotImplementedError(
            "Channel.receive() is not wired yet. "
            "Use LocalRuntime or DistributedRuntime to wire a backend."
        )
        # Unreachable — keeps mypy happy about the return type.
        yield  # pragma: no cover

    def __repr__(self) -> str:
        status = "wired" if self._wired else "unwired"
        return f"Channel(buffer={self.buffer}, backend={self._backend_name!r}, {status})"


def wire_local(channel: Channel[Any], *, topic: str = "default") -> None:
    """Wire a :class:`Channel` to an in-process ``asyncio.Queue`` backend.

    After calling this, ``channel.send()`` and ``channel.receive()`` work
    using :class:`~skaal.runtime.channels.LocalChannel` under the hood.
    """
    from skaal.runtime.channels import LocalChannel

    local = LocalChannel()
    _topic = topic

    async def _send(item: Any) -> None:
        await local.publish(_topic, item)

    async def _receive() -> AsyncIterator[Any]:
        async for msg in local.subscribe(_topic):
            yield msg

    channel.send = _send  # type: ignore[method-assign]
    channel.receive = _receive  # type: ignore[method-assign]
    channel._backend_name = "local"
    channel._wired = True


def wire_redis(
    channel: Channel[Any],
    *,
    url: str = "redis://localhost:6379",
    namespace: str = "default",
    topic: str = "default",
    group: str = "default",
    consumer: str = "worker-0",
) -> None:
    """Wire a :class:`Channel` to a Redis Streams backend.

    After calling this, ``channel.send()`` and ``channel.receive()`` work
    using :class:`~skaal.backends.redis_channel.RedisStreamChannel`.
    """
    from skaal.backends.redis_channel import RedisStreamChannel

    backend = RedisStreamChannel(url=url, namespace=namespace)
    _topic = topic
    _group = group
    _consumer = consumer

    async def _send(item: Any) -> None:
        await backend.publish(_topic, item)

    async def _receive() -> AsyncIterator[Any]:
        async for msg in backend.subscribe(_topic, group=_group, consumer=_consumer):
            # Strip internal _id from the yielded message for typed Channel API.
            msg.pop("_id", None)
            yield msg

    channel.send = _send  # type: ignore[method-assign]
    channel.receive = _receive  # type: ignore[method-assign]
    channel._backend_name = "redis-streams"
    channel._wired = True
    channel._redis_backend = backend  # type: ignore[attr-defined]
