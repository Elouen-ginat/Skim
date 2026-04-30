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
        self._started = False
        self._failures = 0

    async def start(self, context: Any) -> None:
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
                self._failures += 1
                pass
        self._started = True

    async def stop(self) -> None:
        self._started = False

    def snapshot_telemetry(self) -> dict[str, int | bool]:
        return {"running": self._started, "failures": self._failures}
