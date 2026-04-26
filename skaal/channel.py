"""Channel[T] — typed distributed message bus."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Generic, TypeVar

from skaal.backends.channels.local import wire_local
from skaal.backends.channels.redis import wire_redis

T = TypeVar("T")

__all__ = ["Channel", "wire", "wire_local", "wire_redis"]


class Channel(Generic[T]):
    """
    A typed, buffered channel for inter-component communication.

    When marked ``@shared``, the channel becomes a distributed message bus backed
    by Redis Streams, Kafka, or any other wire-function registered under
    ``[project.entry-points."skaal.channels"]``.  When local, it is an
    in-process ``asyncio.Queue``.

    The runtime patches the channel's ``send`` / ``receive`` methods with a
    concrete backend.  Before patching, both raise :class:`NotImplementedError`.

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
            "Channel.send() is not wired yet. Call a wire function (e.g. wire_local, wire_redis)."
        )

    async def receive(self) -> AsyncIterator[T]:
        """Receive items from the channel as an async iterator.

        Raises :class:`NotImplementedError` until the runtime wires a backend.
        """
        raise NotImplementedError(
            "Channel.receive() is not wired yet. Call a wire function (e.g. wire_local, wire_redis)."
        )
        # Unreachable — keeps mypy happy about the return type.
        yield  # pragma: no cover

    def __repr__(self) -> str:
        status = "wired" if self._wired else "unwired"
        return f"Channel(buffer={self.buffer}, backend={self._backend_name!r}, {status})"


def wire(channel: Channel[Any], backend: str, **kwargs: Any) -> None:
    """Wire *channel* using the channel-backend registered under *backend*.

    Looks up a ``wire_<backend>`` function via :mod:`skaal.plugins` — built-in
    backends (``local``, ``redis``) are registered in Skaal's own pyproject;
    third-party backends (``kafka``, ``sqs``, …) register themselves via the
    ``skaal.channels`` entry-point group and become available to this call
    without any core edits.
    """
    from skaal.plugins import get_channel

    wire_fn = get_channel(backend)
    wire_fn(channel, **kwargs)
