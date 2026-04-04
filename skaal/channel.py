"""Channel[T] — typed distributed message bus."""

from __future__ import annotations

from typing import AsyncIterator, Generic, TypeVar

T = TypeVar("T")


class Channel(Generic[T]):
    """
    A typed, buffered channel for inter-component communication.

    When marked @shared, the channel becomes a distributed message bus backed
    by Redis Streams or Kafka. When local, it is an in-process asyncio queue.

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
        self._backend: str | None = None  # set by runtime after planning

    async def send(self, item: T) -> None:
        """Send an item to the channel. Stub — requires runtime mesh."""
        raise NotImplementedError("Channel.send() requires the Skaal runtime.")

    async def receive(self) -> AsyncIterator[T]:
        """Receive items from the channel. Stub — requires runtime mesh."""
        raise NotImplementedError("Channel.receive() requires the Skaal runtime.")
        # make mypy happy — this is an async generator stub
        yield

    def __repr__(self) -> str:
        return f"Channel(buffer={self.buffer}, backend={self._backend!r})"
