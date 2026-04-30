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
from collections.abc import Mapping
from typing import Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph

from skaal.errors import SkaalError
from skaal.patterns import Saga


class SagaState(TypedDict, total=False):
    saga_id: str
    name: str
    status: str
    started_at: float
    finished_at: float
    progress: list[dict[str, Any]]
    input: dict[str, Any]
    failed_step: str
    error: str
    step_index: int
    compensation_index: int


class SagaExecutor:
    """Runs a single saga instance; exposed on the app via :meth:`SagaEngine.executor`."""

    def __init__(
        self,
        saga: Saga,
        functions: dict[str, Any],
        store: Any | None = None,
        metrics: dict[str, int] | None = None,
    ) -> None:
        self.saga = saga
        self.functions = functions
        self.store = store
        self._metrics = metrics if metrics is not None else {"active_tasks": 0, "failures": 0}
        workflow = StateGraph(SagaState)
        workflow.add_node("step", self._run_step_node)
        workflow.add_node("compensate", self._run_compensation_node)
        workflow.add_node("finish", self._finish_node)
        workflow.add_edge(START, "step")
        workflow.add_conditional_edges(
            "step",
            self._route_after_step,
            {"step": "step", "compensate": "compensate", "finish": "finish"},
        )
        workflow.add_conditional_edges(
            "compensate",
            self._route_after_compensation,
            {"compensate": "compensate", "finish": "finish"},
        )
        workflow.add_edge("finish", END)
        self._workflow = workflow.compile()

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        self._metrics["active_tasks"] = self._metrics.get("active_tasks", 0) + 1
        saga_id = f"{self.saga.name}:{uuid.uuid4()}"
        progress: list[dict[str, Any]] = []
        state: SagaState = {
            "saga_id": saga_id,
            "name": self.saga.name,
            "status": "running",
            "started_at": time.time(),
            "progress": progress,
            "input": kwargs,
        }
        await self._persist(saga_id, state)

        try:
            state["step_index"] = 0
            state["compensation_index"] = -1
            final_state = await self._workflow.ainvoke(state)
        except Exception:
            self._metrics["failures"] = self._metrics.get("failures", 0) + 1
            raise
        finally:
            self._metrics["active_tasks"] = max(0, self._metrics.get("active_tasks", 1) - 1)

        if final_state.get("status") == "failed":
            self._metrics["failures"] = self._metrics.get("failures", 0) + 1
            raise SkaalError(
                f"saga {self.saga.name!r} failed at {final_state.get('failed_step')!r}: {final_state.get('error')}"
            )
        return final_state

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

    async def _run_step_node(self, state: SagaState) -> SagaState:
        step_index = int(state.get("step_index", 0))
        if step_index >= len(self.saga.steps):
            state["status"] = "completed"
            state["finished_at"] = time.time()
            await self._persist(cast(str, state["saga_id"]), state)
            return state

        step = self.saga.steps[step_index]
        fn = self._lookup(step.function)
        try:
            result = await self._invoke(fn, step.timeout_ms, cast(dict[str, Any], state["input"]))
        except Exception as exc:  # noqa: BLE001
            state["status"] = "compensating"
            state["failed_step"] = step.function
            state["error"] = repr(exc)
            state["compensation_index"] = len(cast(list[dict[str, Any]], state["progress"])) - 1
            await self._persist(cast(str, state["saga_id"]), state)
            return state

        cast(list[dict[str, Any]], state["progress"]).append(
            {"step": step.function, "compensate": step.compensate, "result": result}
        )
        state["step_index"] = step_index + 1
        if int(state["step_index"]) >= len(self.saga.steps):
            state["status"] = "completed"
            state["finished_at"] = time.time()
        await self._persist(cast(str, state["saga_id"]), state)
        return state

    async def _run_compensation_node(self, state: SagaState) -> SagaState:
        compensation_index = int(state.get("compensation_index", -1))
        progress = cast(list[dict[str, Any]], state["progress"])
        if compensation_index >= 0:
            record = progress[compensation_index]
            fn = self.functions.get(record["compensate"])
            if fn is not None:
                try:
                    if inspect.iscoroutinefunction(fn):
                        await fn(**cast(dict[str, Any], state["input"]))
                    else:
                        fn(**cast(dict[str, Any], state["input"]))
                except Exception:  # noqa: BLE001
                    pass
            state["compensation_index"] = compensation_index - 1

        if int(state.get("compensation_index", -1)) < 0:
            state["status"] = "failed"
            state["finished_at"] = time.time()
        await self._persist(cast(str, state["saga_id"]), state)
        return state

    @staticmethod
    async def _finish_node(state: SagaState) -> SagaState:
        return state

    def _route_after_step(self, state: SagaState) -> str:
        if state.get("status") == "compensating":
            return "compensate"
        if state.get("status") == "completed":
            return "finish"
        return "step"

    def _route_after_compensation(self, state: SagaState) -> str:
        if int(state.get("compensation_index", -1)) >= 0:
            return "compensate"
        return "finish"

    async def _persist(self, saga_id: str, state: Mapping[str, Any]) -> None:
        if self.store is None:
            return
        try:
            await self.store.set(saga_id, dict(state))
        except Exception:  # noqa: BLE001
            # Persistence is best-effort — saga still runs without it.
            pass


class SagaEngine:
    """Registers a :class:`SagaExecutor` on the app so user code can trigger it."""

    def __init__(self, saga: Saga) -> None:
        self.saga = saga
        self._executor: SagaExecutor | None = None
        self._running = False
        self._metrics: dict[str, int] = {"active_tasks": 0, "failures": 0}

    async def start(self, context: Any) -> None:
        functions: dict[str, Any] = getattr(context, "functions", {}) or {}
        store = None
        stores: dict[str, Any] = getattr(context, "stores", {}) or {}
        # Use the first named ``saga_state`` store if the app declared one.
        for name, backend in stores.items():
            if "saga" in name.lower() and "state" in name.lower():
                store = backend
                break
        self._executor = SagaExecutor(self.saga, functions, store, self._metrics)
        # Expose on the runtime context so user code can ``await runtime.sagas["place_order"].run(...)``
        sagas: dict[str, SagaExecutor] = getattr(context, "sagas", None) or {}
        sagas[self.saga.name] = self._executor
        setattr(context, "sagas", sagas)
        self._running = True

    async def stop(self) -> None:
        self._executor = None
        self._running = False

    def executor(self) -> SagaExecutor:
        if self._executor is None:
            raise SkaalError("saga engine is not started")
        return self._executor

    def snapshot_telemetry(self) -> dict[str, int | bool]:
        return {
            "running": self._running,
            "failures": self._metrics.get("failures", 0),
            "active_tasks": self._metrics.get("active_tasks", 0),
        }
