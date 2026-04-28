"""Runtime engines that execute declared Skaal patterns.

Each engine turns metadata on a :class:`skaal.patterns.*` instance into a
running async worker.  Engines share a small lifecycle protocol
(:class:`PatternEngine`) so the runtime can start and stop them uniformly
when the app boots / shuts down.
"""

from __future__ import annotations

from skaal.runtime.engines.base import PatternEngine, engine_for, register_engine, start_engines_for
from skaal.runtime.engines.eventlog import EventLogEngine
from skaal.runtime.engines.outbox import OutboxEngine
from skaal.runtime.engines.projection import ProjectionEngine
from skaal.runtime.engines.saga import SagaEngine, SagaExecutor

__all__ = [
    "EventLogEngine",
    "OutboxEngine",
    "PatternEngine",
    "ProjectionEngine",
    "SagaEngine",
    "SagaExecutor",
    "engine_for",
    "register_engine",
    "start_engines_for",
]
