"""MeshRuntime — distributed Skaal runtime backed by the Rust ``skaal_mesh`` extension.

Requires the ``mesh`` extra::

    maturin build --manifest-path mesh/Cargo.toml --release
    pip install target/wheels/skaal-*.whl

Usage::

    runtime = MeshRuntime(app, plan_json=plan.model_dump_json())
    asyncio.run(runtime.serve())

Or via the CLI::

    skaal run examples.counter:app --distributed
"""

from __future__ import annotations

import inspect
import json
import traceback
from typing import Any

_MAX_BODY_SIZE = 10 * 1024 * 1024


class MeshRuntime:
    """Distributed runtime that delegates agent routing and channels to the Rust mesh.

    The HTTP server / dispatch path mirrors :class:`~skaal.runtime.local.LocalRuntime`
    so existing Starlette / uvicorn plumbing is reused.  The mesh layer adds:

    - **Agent placement**: :meth:`route_agent` wraps
      ``SkaalMesh.route_agent_call`` so agent calls resolve to a live instance.
    - **Distributed channels**: :meth:`channel_publish` forwards to
      ``SkaalMesh.publish`` (``tokio::sync::broadcast``-backed pub/sub).
    - **Health**: :meth:`health` returns the mesh's aggregated health snapshot
      (agents, state, migrations, channels).
    """

    def __init__(
        self,
        app: Any,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        plan_json: str = "",
        node_id: str | None = None,
        backend_overrides: dict[str, Any] | None = None,
    ) -> None:
        try:
            import skaal_mesh
        except ImportError as exc:
            raise ImportError(
                "MeshRuntime requires the skaal_mesh native extension.\n"
                "Build it with: maturin build --manifest-path mesh/Cargo.toml --release\n"
                "Then:          pip install target/wheels/skaal-*.whl"
            ) from exc

        self.app = app
        self.host = host
        self.port = port
        if plan_json:
            plan_dict = json.loads(plan_json)
            plan_dict.setdefault("app_name", app.name)
            plan_json = json.dumps(plan_dict)
        self._mesh = skaal_mesh.SkaalMesh(app.name, plan_json)
        self._backends: dict[str, Any] = {}
        self._backend_overrides = backend_overrides or {}
        self._patch_storage()
        self._patch_channels()
        self._function_cache = self._collect_functions()
        self._engines: list[Any] = []

        from skaal.runtime.middleware import wrap_handler

        self._invokers: dict[str, Any] = {
            name: wrap_handler(fn, fallback_lookup=self._function_cache.get)
            for name, fn in self._function_cache.items()
        }

    # ── Setup (mirrors LocalRuntime) ──────────────────────────────────────────

    def _patch_storage(self) -> None:
        from skaal.backends.local_backend import LocalMap
        from skaal.storage import Collection, Map

        for qname, obj in self.app._collect_all().items():
            if (
                isinstance(obj, type)
                and hasattr(obj, "__skaal_storage__")
                and issubclass(obj, (Map, Collection))
            ):
                backend = (
                    self._backend_overrides.get(qname)
                    or self._backend_overrides.get(obj.__name__)
                    or LocalMap()
                )
                self._backends[qname] = backend
                obj.wire(backend)

    def _patch_channels(self) -> None:
        from skaal.channel import Channel as SkaalChannel
        from skaal.channel import wire_local

        for obj in self.app._collect_all().values():
            if isinstance(obj, SkaalChannel):
                wire_local(obj)

    def _collect_functions(self) -> dict[str, Any]:
        funcs: dict[str, Any] = {
            qname: obj
            for qname, obj in self.app._collect_all().items()
            if callable(obj) and hasattr(obj, "__skaal_compute__")
        }
        for name, fn in self.app._functions.items():
            funcs.setdefault(name, fn)
        for name, fn in getattr(self.app, "_schedules", {}).items():
            funcs.setdefault(name, fn)
        return funcs

    # ── Mesh-aware dispatch ───────────────────────────────────────────────────

    async def _dispatch(self, method: str, path: str, body: bytes) -> tuple[Any, int]:
        funcs = self._function_cache

        if method == "GET" and path in ("/", ""):
            public = [n for n in sorted(funcs) if not hasattr(funcs[n], "__skaal_schedule__")]
            return {
                "app": self.app.name,
                "endpoints": [{"path": f"/{n}", "function": n} for n in public],
                "storage": list(self._backends.keys()),
                "mesh": json.loads(self._mesh.health_snapshot()),
            }, 200

        if method == "GET" and path == "/health":
            return {
                "status": "ok",
                "app": self.app.name,
                "mesh": json.loads(self._mesh.health_snapshot()),
            }, 200

        if method == "POST":
            fn_name = path.lstrip("/")
            if fn_name not in funcs:
                return {"error": f"No function {fn_name!r}. Available: {sorted(funcs)}"}, 404

            fn = funcs[fn_name]
            kwargs: dict[str, Any] = {}
            if body:
                try:
                    kwargs = json.loads(body)
                    if not isinstance(kwargs, dict):
                        return {"error": "Request body must be a JSON object"}, 400
                except json.JSONDecodeError as exc:
                    return {"error": f"Invalid JSON: {exc}"}, 400

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

    # ── Serve (mirrors LocalRuntime._serve_skaal) ─────────────────────────────

    async def serve(self) -> None:
        await self._start_engines()
        try:
            await self._serve_skaal()
        finally:
            await self.shutdown()

    async def _start_engines(self) -> None:
        from skaal.runtime.engines import start_engines_for

        self._engines = await start_engines_for(self.app, self)

    @property
    def functions(self) -> dict[str, Any]:
        return self._function_cache

    @property
    def stores(self) -> dict[str, Any]:
        return {
            qname: obj
            for qname, obj in self.app._collect_all().items()
            if isinstance(obj, type) and hasattr(obj, "__skaal_storage__")
        }

    async def shutdown(self) -> None:
        import contextlib

        for engine in self._engines:
            with contextlib.suppress(Exception):
                await engine.stop()
        self._engines = []
        for backend in self._backends.values():
            with contextlib.suppress(Exception):
                await backend.close()

    async def _serve_skaal(self) -> None:
        try:
            import uvicorn
            from starlette.applications import Starlette
            from starlette.requests import Request as StarletteRequest
            from starlette.responses import JSONResponse
            from starlette.routing import Route
        except ImportError as exc:
            raise RuntimeError(
                "MeshRuntime requires uvicorn and starlette.\n"
                "Install them with:  pip install uvicorn starlette\n"
                f"Missing: {exc}"
            ) from exc

        funcs = self._function_cache
        public_fns = [n for n in sorted(funcs) if not hasattr(funcs[n], "__skaal_schedule__")]
        print(f"\n  Skaal mesh runtime — {self.app.name}")
        print(f"  http://{self.host}:{self.port}\n")
        for name in public_fns:
            print(f"    POST /{name}")
        print()

        async def _handle(request: StarletteRequest) -> JSONResponse:
            body = await request.body()
            result, status = await self._dispatch(request.method, request.url.path, body)
            return JSONResponse(result, status_code=status)

        asgi_app = Starlette(
            routes=[
                Route("/", _handle, methods=["GET"]),
                Route("/health", _handle, methods=["GET"]),
                Route("/{path:path}", _handle, methods=["GET", "POST"]),
            ]
        )

        config = uvicorn.Config(asgi_app, host=self.host, port=self.port, log_level="info")
        await uvicorn.Server(config).serve()

    # ── Mesh bridge API (used by engines / agents) ────────────────────────────

    def route_agent(self, agent_type: str, agent_id: str, method: str, args: dict[str, Any]) -> str:
        return self._mesh.route_agent_call(agent_type, agent_id, method, json.dumps(args))

    def channel_publish(self, topic: str, message: Any) -> int:
        return self._mesh.publish(topic, json.dumps(message))

    def health(self) -> dict[str, Any]:
        return json.loads(self._mesh.health_snapshot())
