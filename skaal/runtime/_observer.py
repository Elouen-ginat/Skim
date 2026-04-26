from __future__ import annotations

from typing import TypedDict

from skaal.types.runtime import RuntimePayload


class EngineStats(TypedDict):
    starts: int
    stops: int
    status: str


class EventStats(TypedDict):
    handled: int
    failed: int
    last_offset: int | None
    last_error: str | None


class InMemoryRuntimeObserver:
    """Collects lightweight runtime lifecycle and event-processing metrics."""

    def __init__(self, *, log_to_stdout: bool = False) -> None:
        self._log_to_stdout = log_to_stdout
        self._engines: dict[str, EngineStats] = {}
        self._events: dict[str, EventStats] = {}

    def engine_started(self, name: str) -> None:
        entry = self._engines.setdefault(name, {"starts": 0, "stops": 0, "status": "stopped"})
        entry["starts"] += 1
        entry["status"] = "running"
        self._log(f"engine started: {name}")

    def engine_stopped(self, name: str) -> None:
        entry = self._engines.setdefault(name, {"starts": 0, "stops": 0, "status": "stopped"})
        entry["stops"] += 1
        entry["status"] = "stopped"
        self._log(f"engine stopped: {name}")

    def event_handled(self, name: str, offset: int) -> None:
        entry = self._events.setdefault(
            name,
            {"handled": 0, "failed": 0, "last_offset": None, "last_error": None},
        )
        entry["handled"] += 1
        entry["last_offset"] = offset
        self._log(f"event handled: {name}@{offset}")

    def event_failed(self, name: str, offset: int, exc: BaseException) -> None:
        entry = self._events.setdefault(
            name,
            {"handled": 0, "failed": 0, "last_offset": None, "last_error": None},
        )
        entry["failed"] += 1
        entry["last_offset"] = offset
        entry["last_error"] = repr(exc)
        self._log(f"event failed: {name}@{offset} ({exc!r})")

    def snapshot(self) -> RuntimePayload:
        handled_total = sum(entry["handled"] for entry in self._events.values())
        failed_total = sum(entry["failed"] for entry in self._events.values())
        return {
            "engines": {name: dict(entry) for name, entry in self._engines.items()},
            "events": {
                "handled_total": handled_total,
                "failed_total": failed_total,
                "streams": {name: dict(entry) for name, entry in self._events.items()},
            },
        }

    def _log(self, message: str) -> None:
        if self._log_to_stdout:
            print(f"[skaal/runtime] {message}", flush=True)


class StdoutRuntimeObserver(InMemoryRuntimeObserver):
    def __init__(self) -> None:
        super().__init__(log_to_stdout=True)
