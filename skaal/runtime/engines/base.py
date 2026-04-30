"""Pattern-engine base protocol and the runtime-side starter helper."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PatternEngine(Protocol):
    """Minimum lifecycle every engine exposes."""

    async def start(self, context: Any) -> None: ...
    async def stop(self) -> None: ...


class BackgroundTaskEngine:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._running = False
        self._failures = 0

    async def _start_background(
        self, worker: Callable[[], Coroutine[Any, Any, None]], *, name: str
    ) -> None:
        self._stopping = asyncio.Event()
        self._task = asyncio.create_task(worker(), name=name)
        self._running = True

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self._running = False

    def snapshot_telemetry(self) -> dict[str, int | bool]:
        return {"running": self._running, "failures": self._failures}


async def start_engines_for(app: Any, context: Any) -> list[PatternEngine]:
    """Inspect *app*'s collected resources and start the right engine for each
    registered pattern.  Returns the list of started engines so the caller can
    stop them on shutdown.

    *context* is passed through to every engine's :meth:`start` call — the
    runtime uses it to expose the function registry, storage overrides, etc.
    """
    from skaal.patterns import EventLog, Outbox, Projection, Saga
    from skaal.runtime.engines.eventlog import EventLogEngine
    from skaal.runtime.engines.outbox import OutboxEngine
    from skaal.runtime.engines.projection import ProjectionEngine
    from skaal.runtime.engines.saga import SagaEngine

    engines: list[PatternEngine] = []
    for obj in app._collect_all().values():
        if isinstance(obj, EventLog):
            engines.append(EventLogEngine(obj))
        elif isinstance(obj, Projection):
            engines.append(ProjectionEngine(obj))
        elif isinstance(obj, Saga):
            engines.append(SagaEngine(obj))
        elif isinstance(obj, Outbox):
            engines.append(OutboxEngine(obj))

    for eng in engines:
        await eng.start(context)
    return engines
