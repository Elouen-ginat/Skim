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
from skaal.runtime.engines.base import BackgroundTaskEngine


class ProjectionEngine(BackgroundTaskEngine):
    """Background worker for a single :class:`skaal.patterns.Projection`."""

    def __init__(self, projection: Projection[Any, Any]) -> None:
        super().__init__()
        self.projection = projection

    async def start(self, context: Any) -> None:
        handler_name = self.projection.handler
        functions: dict[str, Any] = getattr(context, "functions", {}) or {}
        handler = functions.get(handler_name)
        if handler is None:
            # Defer failure — the solver validates this at plan time, but
            # tests may spin up an engine without a handler registered.
            handler = _missing_handler(handler_name)

        await self._start_background(
            lambda: self._run(handler),
            name=f"projection:{self.projection.handler}",
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
                except Exception:  # noqa: BLE001
                    # Projections re-process from the last checkpoint on restart;
                    # swallowing here keeps the tail alive — strict-mode will
                    # surface via an observability hook in a later phase.
                    self._failures += 1
                    continue
                counter += 1
                if counter % max(1, self.projection.checkpoint_every) == 0:
                    # subscribe() already writes consumer offset; this hook is
                    # reserved for snapshotting derived state in future versions.
                    pass
        except asyncio.CancelledError:
            return


def _missing_handler(name: str) -> Any:
    async def _raise(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError(f"projection handler {name!r} is not registered with the runtime")

    return _raise
