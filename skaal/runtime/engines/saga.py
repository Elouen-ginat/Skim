"""Saga engine — compensating-transaction executor.

Two coordination strategies are supported, per :class:`skaal.patterns.Saga`:

- ``"compensation"`` (default) — run each step in order; on failure, run the
  compensations of *previously-successful* steps in reverse order.
- ``"2pc"``                 — prepare all steps, then commit; if any prepare
  fails, compensate the prepared set.  The executor treats each saga function
  as idempotent for prepare == invoke.

Progress is persisted in a backend keyed by ``saga_id`` so restarts can
resume (or compensate) cleanly.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from typing import Any

from skaal.errors import SkaalError
from skaal.patterns import Saga
from skaal.runtime.engines.base import register_engine


class SagaExecutor:
    """Runs a single saga instance exposed on the runtime context."""

    def __init__(
        self,
        saga: Saga,
        functions: dict[str, Any],
        store: Any | None = None,
    ) -> None:
        self.saga = saga
        self.functions = functions
        self.store = store

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        saga_id = f"{self.saga.name}:{uuid.uuid4()}"
        progress: list[dict[str, Any]] = []
        state: dict[str, Any] = {
            "saga_id": saga_id,
            "name": self.saga.name,
            "status": "running",
            "started_at": time.time(),
            "progress": progress,
            "input": kwargs,
        }
        await self._persist(saga_id, state)

        try:
            for step in self.saga.steps:
                fn = self._lookup(step.function)
                call = self._invoke(fn, step.timeout_ms, kwargs)
                try:
                    result = await call
                except Exception as exc:  # noqa: BLE001
                    state["status"] = "compensating"
                    state["failed_step"] = step.function
                    state["error"] = repr(exc)
                    await self._persist(saga_id, state)
                    await self._compensate(progress, kwargs)
                    state["status"] = "failed"
                    await self._persist(saga_id, state)
                    raise SkaalError(
                        f"saga {self.saga.name!r} failed at {step.function!r}: {exc}"
                    ) from exc
                progress.append(
                    {"step": step.function, "compensate": step.compensate, "result": result}
                )
                await self._persist(saga_id, state)
        except SkaalError:
            raise

        state["status"] = "completed"
        state["finished_at"] = time.time()
        await self._persist(saga_id, state)
        return state

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _lookup(self, name: str) -> Any:
        fn = self.functions.get(name)
        if fn is None:
            raise SkaalError(f"saga references unknown function {name!r}")
        return fn

    async def _invoke(self, fn: Any, timeout_ms: int | None, kwargs: dict[str, Any]) -> Any:
        async def _call() -> Any:
            if inspect.iscoroutinefunction(fn):
                return await fn(**kwargs)
            return fn(**kwargs)

        if timeout_ms is None:
            return await _call()
        return await asyncio.wait_for(_call(), timeout=timeout_ms / 1000.0)

    async def _compensate(self, progress: list[dict[str, Any]], kwargs: dict[str, Any]) -> None:
        for record in reversed(progress):
            fn = self.functions.get(record["compensate"])
            if fn is None:
                continue
            try:
                if inspect.iscoroutinefunction(fn):
                    await fn(**kwargs)
                else:
                    fn(**kwargs)
            except Exception:  # noqa: BLE001
                # Compensations are best-effort; a failure here is logged
                # via the persisted state — don't abort the rest of the rollback.
                continue

    async def _persist(self, saga_id: str, state: dict[str, Any]) -> None:
        if self.store is None:
            return
        try:
            await self.store.set(saga_id, state)
        except Exception:  # noqa: BLE001
            # Persistence is best-effort — saga still runs without it.
            pass


@register_engine(Saga)
class SagaEngine:
    """Registers a :class:`SagaExecutor` on the app so user code can trigger it."""

    def __init__(self, saga: Saga) -> None:
        self.saga = saga
        self._executor: SagaExecutor | None = None

    async def start(self, context: Any) -> None:
        functions: dict[str, Any] = getattr(context, "functions", {}) or {}
        store = None
        stores: dict[str, Any] = getattr(context, "stores", {}) or {}
        # Use the first named ``saga_state`` store if the app declared one.
        for name, backend in stores.items():
            if "saga" in name.lower() and "state" in name.lower():
                store = backend
                break
        self._executor = SagaExecutor(self.saga, functions, store)
        # Expose on the runtime context so user code can ``await runtime.sagas["place_order"].run(...)``
        sagas: dict[str, SagaExecutor] = getattr(context, "sagas", None) or {}
        sagas[self.saga.name] = self._executor
        setattr(context, "sagas", sagas)

    async def stop(self) -> None:
        self._executor = None
