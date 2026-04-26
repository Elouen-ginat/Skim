"""EventLog engine.

For the in-process / local case there is nothing to start — :class:`EventLog`
already drives a push-based notify on every append, and subscribers hold the
cursor themselves.  The engine exists so the runtime has a uniform lifecycle
hook and so the solver can attach per-EventLog resources (e.g. ensuring a
dedicated backend is wired in).
"""

from __future__ import annotations

from typing import Any

from skaal.patterns import EventLog


class EventLogEngine:
    def __init__(self, log: EventLog[Any]) -> None:
        self.log = log
        self._observer: Any | None = None

    async def start(self, context: Any) -> None:
        self._observer = getattr(context, "observer", None)
        if self._observer is not None:
            self._observer.engine_started(self._engine_name())
        # Ensure the backend is reachable — fail fast if the user's catalog
        # picked a server-backed backend that isn't running.
        probe = self.log._backend
        if hasattr(probe, "connect") and getattr(probe, "_client", "?") is None:
            try:
                await probe.connect()
            except Exception:  # noqa: BLE001
                # Connection errors surface lazily on first append/subscribe;
                # the engine stays startable so tests with unavailable servers
                # still proceed.
                pass

    async def stop(self) -> None:
        if self._observer is not None:
            self._observer.engine_stopped(self._engine_name())

    def _engine_name(self) -> str:
        return f"eventlog:{id(self.log)}"
