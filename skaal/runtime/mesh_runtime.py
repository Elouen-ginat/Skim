"""MeshRuntime — distributed Skaal runtime backed by the Rust ``skaal_mesh`` extension.

Requires the ``mesh`` extra::

    pip install "skaal[mesh]"

If you are editing the Rust crate locally::

    make build-dev

Usage::

    runtime = MeshRuntime(app, plan_json=plan.model_dump_json())
    asyncio.run(runtime.serve())

Or via the CLI::

    skaal run examples.counter:app --distributed
"""

from __future__ import annotations

import inspect
import json
import logging
import traceback
from typing import Any, cast

_MAX_BODY_SIZE = 10 * 1024 * 1024
log = logging.getLogger("skaal.runtime")
_SKAAL_INVOKE_PREFIX = "/_skaal/invoke/"


def _format_banner(title: str, lines: list[str]) -> str:
    return "\n".join(["", title, *lines, ""])


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
                'Install it with: pip install "skaal[mesh]"\n'
                "If you are editing the Rust crate locally, run: make build-dev"
            ) from exc

        self.app = app
        self.host = host
        self.port = port
        if plan_json:
            plan_dict = json.loads(plan_json)
            plan_dict.setdefault("app_name", app.name)
            plan_json = json.dumps(plan_dict)
        self._mesh = cast(Any, skaal_mesh).SkaalMesh(app.name, plan_json)
        self._backends: dict[str, Any] = {}
        self._backend_overrides = backend_overrides or {}
        self._patch_storage()
        self._patch_channels()
        self._function_cache = self._collect_functions()
        self._engines: list[Any] = []

        from skaal.runtime.middleware import wrap_handler

        self._invokers: dict[str, Any] = {}
        _shared_invokers: dict[int, Any] = {}
        for name, fn in self._function_cache.items():
            invoker = _shared_invokers.get(id(fn))
            if invoker is None:
                invoker = wrap_handler(fn, fallback_lookup=self._function_cache.get)
                _shared_invokers[id(fn)] = invoker
            self._invokers[name] = invoker
        self.app._bind_runtime(self)

    # ── Setup (mirrors LocalRuntime) ──────────────────────────────────────────

    def _patch_storage(self) -> None:
        from skaal.backends.chroma_backend import ChromaVectorBackend
        from skaal.backends.file_blob_backend import FileBlobBackend
        from skaal.backends.local_backend import LocalMap
        from skaal.backends.sqlite_backend import SqliteBackend
        from skaal.blob import BlobStore, is_blob_model
        from skaal.relational import is_relational_model, wire_relational_model
        from skaal.storage import Store
        from skaal.vector import VectorStore, is_vector_model

        for qname, obj in self.app._collect_all().items():
            if not (isinstance(obj, type) and hasattr(obj, "__skaal_storage__")):
                continue

            backend = self._backend_overrides.get(qname) or self._backend_overrides.get(
                obj.__name__
            )

            if is_relational_model(obj):
                backend = backend or SqliteBackend("skaal_local.db", namespace=qname)
                self._backends[qname] = backend
                wire_relational_model(obj, backend)
                continue

            if is_vector_model(obj):
                backend = backend or ChromaVectorBackend("skaal_chroma", namespace=qname)
                self._backends[qname] = backend
                cast(type[VectorStore[Any]], obj).wire(backend)
                continue

            if is_blob_model(obj):
                backend = backend or FileBlobBackend(".skaal/blobs", namespace=qname)
                self._backends[qname] = backend
                cast(type[BlobStore], obj).wire(backend)
                continue

            if issubclass(obj, Store):
                backend = backend or LocalMap()
                self._backends[qname] = backend
                obj.wire(backend)
            elif issubclass(obj, VectorStore):
                backend = backend or ChromaVectorBackend("skaal_chroma", namespace=qname)
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

    def _public_functions(self) -> dict[str, Any]:
        return {
            qname: obj
            for qname, obj in self.app._collect_all().items()
            if callable(obj) and hasattr(obj, "__skaal_compute__")
        }

    @staticmethod
    def _invocation_target(path: str) -> str | None:
        if not path.startswith(_SKAAL_INVOKE_PREFIX):
            return None
        target = path[len(_SKAAL_INVOKE_PREFIX) :]
        return target or None

    async def invoke(self, function_name: str, kwargs: dict[str, Any]) -> Any:
        invoker = self._invokers.get(function_name)
        if invoker is None:
            raise KeyError(
                f"No function {function_name!r}. Available: {sorted(self._function_cache)}"
            )
        return await invoker.invoke(
            kwargs=kwargs,
            before_attempt=lambda attempt, payload: self.app._prepare_invoke_kwargs(
                function_name,
                payload,
                is_stream=False,
                attempt=attempt,
            ),
        )

    def invoke_stream(self, function_name: str, kwargs: dict[str, Any]) -> Any:
        invoker = self._invokers.get(function_name)
        if invoker is None:
            raise KeyError(
                f"No function {function_name!r}. Available: {sorted(self._function_cache)}"
            )
        return invoker.invoke_stream(
            kwargs=kwargs,
            before_attempt=lambda attempt, payload: self.app._prepare_invoke_kwargs(
                function_name,
                payload,
                is_stream=True,
                attempt=attempt,
            ),
        )

    # ── Mesh-aware dispatch ───────────────────────────────────────────────────

    async def _dispatch(self, method: str, path: str, body: bytes) -> tuple[Any, int]:
        funcs = self._function_cache

        if method == "GET" and path in ("/", ""):
            public = sorted(self._public_functions())
            return {
                "app": self.app.name,
                "endpoints": [
                    {"path": f"{_SKAAL_INVOKE_PREFIX}{n}", "function": n} for n in public
                ],
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
            fn_name = self._invocation_target(path)
            if fn_name is None:
                return {"error": f"No function route for {path!r}"}, 404
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
                    result = await self.invoke(fn_name, kwargs)
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
        self.app._unbind_runtime(self)

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

        public_fns = sorted(self._public_functions())
        banner_lines = [f"  http://{self.host}:{self.port}", ""]
        for name in public_fns:
            banner_lines.append(f"    POST {_SKAAL_INVOKE_PREFIX}{name}")
        log.info(_format_banner(f"  Skaal mesh runtime — {self.app.name}", banner_lines))

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
