from __future__ import annotations

import inspect
import json
import traceback
from typing import TYPE_CHECKING, cast

from skaal.types.runtime import (
    AsyncClosable,
    DispatchResult,
    RuntimeApp,
    RuntimeCallable,
    RuntimeInvoker,
    RuntimeKwargs,
    RuntimePayload,
)


class _RuntimeDispatchMixin:
    if TYPE_CHECKING:
        app: RuntimeApp
        _backends: dict[str, AsyncClosable]
        _function_cache: dict[str, RuntimeCallable]
        _invokers: dict[str, RuntimeInvoker]

    def _collect_schedules(self) -> dict[str, RuntimeCallable]:
        return {
            name: cast(RuntimeCallable, fn)
            for name, fn in self.app._schedules.items()
            if callable(fn)
        }

    def _index_payload(self) -> RuntimePayload:
        return {}

    def _health_payload(self) -> RuntimePayload:
        return {}

    def _prepare_invocation_kwargs(
        self,
        fn: RuntimeCallable,
        kwargs: RuntimeKwargs,
    ) -> RuntimeKwargs:
        is_schedule_invocation = kwargs.pop("_skaal_trigger", None) is not None
        if is_schedule_invocation:
            sig = inspect.signature(fn)
            if "ctx" in sig.parameters:
                from datetime import datetime, timezone

                from skaal.schedule import ScheduleContext

                kwargs["ctx"] = ScheduleContext(fired_at=datetime.now(timezone.utc))
        return kwargs

    async def _dispatch(self, method: str, path: str, body: bytes) -> DispatchResult:
        funcs = self._function_cache

        if method == "GET" and path in ("/", ""):
            public = [n for n in sorted(funcs) if not hasattr(funcs[n], "__skaal_schedule__")]
            index_payload: RuntimePayload = {
                "app": self.app.name,
                "endpoints": [{"path": f"/{n}", "function": n} for n in public],
                "storage": list(self._backends.keys()),
            }
            index_payload.update(self._index_payload())
            return index_payload, 200

        if method == "GET" and path == "/health":
            health_payload: RuntimePayload = {"status": "ok", "app": self.app.name}
            health_payload.update(self._health_payload())
            return health_payload, 200

        if method == "POST":
            fn_name = path.lstrip("/")
            if fn_name not in funcs:
                return {"error": f"No function {fn_name!r}. Available: {sorted(funcs)}"}, 404

            fn = funcs[fn_name]
            kwargs: RuntimeKwargs = {}
            if body:
                try:
                    decoded_body = json.loads(body)
                    if not isinstance(decoded_body, dict):
                        return {"error": "Request body must be a JSON object"}, 400
                    kwargs = cast(RuntimeKwargs, decoded_body)
                except json.JSONDecodeError as exc:
                    return {"error": f"Invalid JSON: {exc}"}, 400

            kwargs = self._prepare_invocation_kwargs(fn, kwargs)
            invoker = self._invokers.get(fn_name)
            try:
                if invoker is not None:
                    result = await invoker(**kwargs)
                else:
                    result = await fn(**kwargs) if inspect.iscoroutinefunction(fn) else fn(**kwargs)
                return result, 200
            except TypeError as exc:
                return {"error": f"Bad arguments for {fn_name!r}: {exc}"}, 422
            except Exception as exc:  # noqa: BLE001
                return {"error": str(exc), "traceback": traceback.format_exc()}, 500

        return {"error": f"Method {method} not allowed"}, 405
