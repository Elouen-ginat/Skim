"""Projection engine — tails an EventLog and applies a handler per event.

The handler is a named function registered with the app/module; it receives
``(target, event)`` and is expected to update the target storage.  Progress
is checkpointed in the EventLog's backend under a dedicated key so restarts
resume cleanly.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from skaal.patterns import Projection
from skaal.runtime.engines.base import register_engine


@register_engine(Projection)
class ProjectionEngine:
    """Background worker for a single :class:`skaal.patterns.Projection`."""

    def __init__(self, projection: Projection[Any, Any]) -> None:
        self.projection = projection
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._observer: Any | None = None

    async def start(self, context: Any) -> None:
        handler_name = self.projection.handler
        functions: dict[str, Any] = getattr(context, "functions", {}) or {}
        handler = functions.get(handler_name)
        if handler is None:
            # Defer failure — the solver validates this at plan time, but
            # tests may spin up an engine without a handler registered.
            handler = _missing_handler(handler_name)

        self._stopping = asyncio.Event()
        self._observer = getattr(context, "observer", None)
        if self._observer is not None:
            self._observer.engine_started(self._engine_name())
        self._task = asyncio.create_task(
            self._run(handler), name=f"projection:{self.projection.handler}"
        )

    async def _run(self, handler: Any) -> None:
        group = f"projection:{self.projection.handler}"
        target = self.projection.target
        counter = 0
        try:
            async for offset, event in self.projection.source.subscribe(group):
                if self._stopping.is_set():
                    return
                try:
                    if inspect.iscoroutinefunction(handler):
                        await handler(target, event)
                    else:
                        handler(target, event)
                except Exception as exc:  # noqa: BLE001
                    if self._observer is not None:
                        self._observer.event_failed(self.projection.handler, offset, exc)
                    if self.projection.strict:
                        raise
                    continue
                counter += 1
                if self._observer is not None:
                    self._observer.event_handled(self.projection.handler, offset)
                if counter % max(1, self.projection.checkpoint_every) == 0:
                    try:
                        await _write_projection_checkpoint(self.projection, offset)
                    except Exception as exc:  # noqa: BLE001
                        if self._observer is not None:
                            self._observer.event_failed(self.projection.handler, offset, exc)
                        if self.projection.strict:
                            raise
        except asyncio.CancelledError:
            return
        finally:
            if self._observer is not None:
                self._observer.engine_stopped(self._engine_name())

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    def _engine_name(self) -> str:
        return f"projection:{self.projection.handler}"


def _missing_handler(name: str) -> Any:
    async def _raise(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError(f"projection handler {name!r} is not registered with the runtime")

    return _raise


async def _write_projection_checkpoint(projection: Projection[Any, Any], offset: int) -> None:
    backend = _target_backend_of(projection.target)
    if backend is None or not hasattr(backend, "set"):
        return
    await backend.set(f"__projection__:{projection.handler}:offset", offset)


def _target_backend_of(target: Any) -> Any | None:
    for attr in ("_backend", "__skaal_backend__"):
        backend = getattr(target, attr, None)
        if backend is not None:
            return backend
    return None
