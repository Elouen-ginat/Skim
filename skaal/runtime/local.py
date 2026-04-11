"""LocalRuntime — serve a Skaal App in-process for local development."""

from __future__ import annotations

import inspect
import json
import traceback
from pathlib import Path
from typing import Any

from skaal.backends.local_backend import LocalMap

_MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MiB — reject oversized request bodies


def _wire_channel(channel_obj: Any) -> None:
    """Replace stub send/receive on a Channel instance with a local backend."""
    from skaal.channel import wire_local

    wire_local(channel_obj)


class LocalRuntime:
    """
    Runs a Skaal App locally as a minimal asyncio HTTP server.

    - Each ``@app.function()`` becomes a ``POST /{name}`` endpoint.
    - Storage classes are patched with in-memory :class:`~skaal.backends.local_backend.LocalMap`
      backends (or overrides supplied via *backend_overrides*).
    - Channel instances are wired to :class:`~skaal.runtime.channels.LocalChannel`.
    - ``GET /`` returns a JSON index of available endpoints.
    - ``GET /health`` returns ``{"status": "ok"}``.

    Intended for development and testing only — not production.

    Usage::

        runtime = LocalRuntime(app, host="127.0.0.1", port=8000)
        asyncio.run(runtime.serve())
    """

    def __init__(
        self,
        app: Any,
        host: str = "127.0.0.1",
        port: int = 8000,
        backend_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.app = app
        self.host = host
        self.port = port
        self._backends: dict[str, Any] = {}
        self._backend_overrides = backend_overrides or {}
        self._patch_storage()
        self._patch_channels()
        # Cache the function map so it's not rebuilt on every HTTP request
        self._function_cache = self._collect_functions()

    # ── Setup ──────────────────────────────────────────────────────────────────

    def _patch_storage(self) -> None:
        """Wire all registered storage classes with appropriate backends."""
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
        """Wire Channel instances registered with the app to LocalChannel."""
        from skaal.channel import Channel as SkaalChannel

        for obj in self.app._collect_all().values():
            if isinstance(obj, SkaalChannel):
                _wire_channel(obj)

    # ── Factory methods ────────────────────────────────────────────────────────

    @staticmethod
    def _build_backends(app: Any, backend_factory: Any) -> dict[str, Any]:
        """
        Build a backends dict for all storage classes in app using a factory function.

        Args:
            app: The Skaal App.
            backend_factory: Callable that takes (qname, obj) and returns a backend instance.

        Returns:
            Dict mapping fully-qualified names to backend instances.
        """
        return {
            qname: backend_factory(qname, obj)
            for qname, obj in app._collect_all().items()
            if isinstance(obj, type) and hasattr(obj, "__skaal_storage__")
        }

    @classmethod
    def from_redis(
        cls,
        app: Any,
        redis_url: str,
        host: str = "127.0.0.1",
        port: int = 8000,
    ) -> "LocalRuntime":
        """Create a ``LocalRuntime`` using Redis backends for all storage classes."""
        from skaal.backends.redis_backend import RedisBackend

        def _make_backend(qname: str, obj: Any) -> RedisBackend:
            return RedisBackend(url=redis_url, namespace=qname.replace(".", "_").lower())

        backends = cls._build_backends(app, _make_backend)
        return cls(app, host=host, port=port, backend_overrides=backends)

    @classmethod
    def from_sqlite(
        cls,
        app: Any,
        db_path: str | Path = "skaal_local.db",
        host: str = "127.0.0.1",
        port: int = 8000,
    ) -> "LocalRuntime":
        """Create a ``LocalRuntime`` backed by SQLite."""
        from skaal.backends.sqlite_backend import SqliteBackend

        def _make_backend(qname: str, obj: Any) -> SqliteBackend:
            return SqliteBackend(Path(db_path), namespace=qname)

        backends = cls._build_backends(app, _make_backend)
        return cls(app, host=host, port=port, backend_overrides=backends)

    @classmethod
    def from_firestore(
        cls,
        app: Any,
        project: str | None = None,
        database: str = "(default)",
        host: str = "127.0.0.1",
        port: int = 8000,
    ) -> "LocalRuntime":
        """
        Create a ``LocalRuntime`` using Cloud Firestore backends for all storage classes.

        Each storage class gets its own Firestore collection named after the
        fully-qualified class name (dots replaced with underscores).

        Args:
            app:      The Skaal :class:`~skaal.app.App`.
            project:  GCP project ID.  Defaults to the ambient project from
                      Application Default Credentials.
            database: Firestore database name.  Defaults to ``"(default)"``.
        """
        from skaal.backends.firestore_backend import FirestoreBackend

        def _make_backend(qname: str, obj: Any) -> FirestoreBackend:
            return FirestoreBackend(
                collection=qname.replace(".", "_").lower(),
                project=project,
                database=database,
            )

        backends = cls._build_backends(app, _make_backend)
        return cls(app, host=host, port=port, backend_overrides=backends)

    @classmethod
    def from_postgres(
        cls,
        app: Any,
        dsn: str,
        host: str = "127.0.0.1",
        port: int = 8000,
        min_size: int = 1,
        max_size: int = 5,
    ) -> "LocalRuntime":
        """
        Create a ``LocalRuntime`` backed by PostgreSQL.

        Args:
            app:      The Skaal :class:`~skaal.app.App`.
            dsn:      asyncpg connection string, e.g.
                      ``"postgresql://user:pass@localhost/mydb"``.
            min_size: Connection pool minimum size.
            max_size: Connection pool maximum size.
        """
        from skaal.backends.postgres_backend import PostgresBackend

        def _make_backend(qname: str, obj: Any) -> PostgresBackend:
            return PostgresBackend(dsn=dsn, namespace=qname, min_size=min_size, max_size=max_size)

        backends = cls._build_backends(app, _make_backend)
        return cls(app, host=host, port=port, backend_overrides=backends)

    @classmethod
    def from_dynamodb(
        cls,
        app: Any,
        table_name: str,
        region: str = "us-east-1",
        host: str = "127.0.0.1",
        port: int = 8000,
    ) -> "LocalRuntime":
        """Create a ``LocalRuntime`` backed by DynamoDB."""
        from skaal.backends.dynamodb_backend import DynamoBackend

        def _make_backend(qname: str, obj: Any) -> DynamoBackend:
            return DynamoBackend(
                table_name=f"{table_name}_{qname.replace('.', '_').lower()}", region=region
            )

        backends = cls._build_backends(app, _make_backend)
        return cls(app, host=host, port=port, backend_overrides=backends)

    def wire_channels_redis(
        self,
        redis_url: str = "redis://localhost:6379",
        namespace: str | None = None,
    ) -> None:
        """Re-wire all Channel instances to use Redis Streams instead of local queues.

        Call after construction to upgrade channels to distributed pub/sub::

            runtime = LocalRuntime(app)
            runtime.wire_channels_redis("redis://localhost:6379")
        """
        from skaal.channel import Channel as SkaalChannel
        from skaal.channel import wire_redis

        ns = namespace or self.app.name
        for name, obj in self.app._collect_all().items():
            if isinstance(obj, SkaalChannel):
                wire_redis(obj, url=redis_url, namespace=ns, topic=name)

    # ── HTTP dispatch ──────────────────────────────────────────────────────────

    def _collect_functions(self) -> dict[str, Any]:
        """Flat map of qualified_name → callable for all HTTP-invocable functions.

        Includes:
        - ``@app.function()`` decorated callables (have ``__skaal_compute__``)
        - ``@app.schedule()`` decorated callables (invocable by Cloud Scheduler /
          EventBridge; excluded from the public ``GET /`` index)
        """
        funcs: dict[str, Any] = {
            qname: obj
            for qname, obj in self.app._collect_all().items()
            if callable(obj) and hasattr(obj, "__skaal_compute__")
        }
        # Also expose top-level functions by short name for convenience.
        for name, fn in self.app._functions.items():
            funcs.setdefault(name, fn)
        # Include scheduled functions so Cloud Scheduler / EventBridge can invoke
        # them via HTTP POST.  They are excluded from the GET / listing.
        for name, fn in getattr(self.app, "_schedules", {}).items():
            funcs.setdefault(name, fn)
        return funcs

    def _collect_schedules(self) -> dict[str, Any]:
        """Flat map of name → callable for all ``@app.schedule()`` functions."""
        return dict(getattr(self.app, "_schedules", {}))

    async def _dispatch(self, method: str, path: str, body: bytes) -> tuple[Any, int]:
        """Route an HTTP request to a registered function."""
        funcs = self._function_cache

        if method == "GET" and path in ("/", ""):
            # Only expose @app.function() endpoints in the public index.
            public = [n for n in sorted(funcs) if not hasattr(funcs[n], "__skaal_schedule__")]
            return {
                "app": self.app.name,
                "endpoints": [{"path": f"/{n}", "function": n} for n in public],
                "storage": list(self._backends.keys()),
            }, 200

        if method == "GET" and path == "/health":
            return {"status": "ok", "app": self.app.name}, 200

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

            # Strip the internal schedule-trigger marker and inject ScheduleContext
            # when Cloud Scheduler / EventBridge includes it in the request body.
            is_schedule_invocation = kwargs.pop("_skaal_trigger", None) is not None
            if is_schedule_invocation:
                sig = inspect.signature(fn)
                if "ctx" in sig.parameters:
                    from datetime import timezone

                    from skaal.schedule import ScheduleContext

                    kwargs["ctx"] = ScheduleContext(
                        fired_at=__import__("datetime").datetime.now(timezone.utc)
                    )

            try:
                result = await fn(**kwargs) if inspect.iscoroutinefunction(fn) else fn(**kwargs)
                return result, 200
            except TypeError as exc:
                return {"error": f"Bad arguments for {fn_name!r}: {exc}"}, 422
            except Exception as exc:  # noqa: BLE001
                return {"error": str(exc), "traceback": traceback.format_exc()}, 500

        return {"error": f"Method {method} not allowed"}, 405

    async def _handle_connection(self, reader: Any, writer: Any) -> None:
        """Handle a single raw TCP connection with HTTP/1.0-style request parsing.

        Enforces ``_MAX_BODY_SIZE`` and writes a plain-text HTTP response.
        Intended for testing and low-level inspection; production traffic goes
        through the uvicorn path in :meth:`_serve_skaal`.
        """
        try:
            # Read the request line
            request_line_bytes = await reader.readline()
            if not request_line_bytes:
                return
            request_line = request_line_bytes.decode("utf-8", errors="replace").strip()
            parts = request_line.split(" ", 2)
            if len(parts) < 2:
                return
            method, path = parts[0], parts[1]

            # Read headers until blank line
            headers: dict[str, str] = {}
            while True:
                line_bytes = await reader.readline()
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    break
                if ":" in line:
                    name, _, value = line.partition(":")
                    headers[name.strip().lower()] = value.strip()

            # Enforce body size limit
            content_length = int(headers.get("content-length", "0"))
            if content_length > _MAX_BODY_SIZE:
                response = (
                    "HTTP/1.1 413 Payload Too Large\r\n"
                    "Content-Type: application/json\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                    '{"error": "Request body too large"}'
                ).encode()
                writer.write(response)
                await writer.drain()
                return

            body = await reader.read(content_length) if content_length > 0 else b""

            result, status = await self._dispatch(method, path, body)
            result_bytes = json.dumps(result).encode()
            response = (
                f"HTTP/1.1 {status} OK\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(result_bytes)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode() + result_bytes
            writer.write(response)
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass

    def build_asgi(self) -> Any:
        """Return a Starlette ASGI app that serves all ``@app.function()`` endpoints.

        Use this in deployment entry-points where the ASGI server (gunicorn,
        uvicorn) is started externally rather than via :meth:`serve`::

            runtime   = LocalRuntime(app, backend_overrides={...})
            application = runtime.build_asgi()   # gunicorn main:application

        Returns:
            A ``starlette.applications.Starlette`` instance wired to
            :meth:`_dispatch`.
        """
        try:
            from starlette.applications import Starlette
            from starlette.requests import Request as StarletteRequest
            from starlette.responses import JSONResponse
            from starlette.routing import Route
        except ImportError as exc:
            raise RuntimeError(
                "build_asgi() requires starlette.\n"
                "Install it with:  pip install starlette\n"
                f"Missing: {exc}"
            ) from exc

        async def _handle(request: StarletteRequest) -> JSONResponse:
            body = await request.body()
            result, status = await self._dispatch(request.method, request.url.path, body)
            return JSONResponse(result, status_code=status)

        return Starlette(
            routes=[
                Route("/", _handle, methods=["GET"]),
                Route("/health", _handle, methods=["GET"]),
                Route("/{path:path}", _handle, methods=["GET", "POST"]),
            ]
        )

    async def serve(self) -> None:
        """
        Start the HTTP server and run until cancelled.

        Dispatch order:
        - ASGI app registered via ``app.mount_asgi()`` → :meth:`_serve_asgi`
        - WSGI app registered via ``app.mount_wsgi()`` → :meth:`_serve_wsgi`
        - Otherwise → :meth:`_serve_skaal` (Skaal functions as POST endpoints)
        """
        try:
            asgi_app = getattr(self.app, "_asgi_app", None)
            wsgi_app = getattr(self.app, "_wsgi_app", None)
            if asgi_app is not None:
                await self._serve_asgi(asgi_app)
            elif wsgi_app is not None:
                await self._serve_wsgi(wsgi_app)
            else:
                await self._serve_skaal()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """
        Shut down the runtime by closing all backend connections.

        Called automatically when serve() exits. Can also be called explicitly
        to clean up resources.
        """
        import contextlib

        for backend in self._backends.values():
            with contextlib.suppress(Exception):
                await backend.close()

    async def _serve_skaal(self) -> None:
        """Expose @app.function() as POST /{name} endpoints via uvicorn + Starlette.

        Also starts an APScheduler ``AsyncIOScheduler`` for any functions
        registered with ``@app.schedule()``.
        """
        from datetime import timezone

        try:
            import uvicorn
            from starlette.applications import Starlette
            from starlette.requests import Request as StarletteRequest
            from starlette.responses import JSONResponse
            from starlette.routing import Route
        except ImportError as exc:
            raise RuntimeError(
                "skaal run requires uvicorn and starlette.\n"
                "Install them with:  pip install uvicorn starlette\n"
                f"Missing: {exc}"
            ) from exc

        # ── Print startup banner ───────────────────────────────────────────────
        funcs = self._function_cache
        public_fns = [n for n in sorted(funcs) if not hasattr(funcs[n], "__skaal_schedule__")]
        scheduled = self._collect_schedules()

        print(f"\n  Skaal local runtime — {self.app.name}")
        print(f"  http://{self.host}:{self.port}\n")
        for name in public_fns:
            print(f"    POST /{name}")
        if scheduled:
            print()
            for name, fn in sorted(scheduled.items()):
                meta = fn.__skaal_schedule__
                trigger = meta["trigger"]
                print(f"    schedule /{name}  [{trigger!r}]")
        print()

        # ── Starlette ASGI app — delegates to existing _dispatch ──────────────
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

        # ── Start APScheduler for scheduled functions ──────────────────────────
        scheduler = None
        if scheduled:
            try:
                from apscheduler.schedulers.asyncio import AsyncIOScheduler
                from apscheduler.triggers.cron import CronTrigger
                from apscheduler.triggers.interval import IntervalTrigger

                from skaal.schedule import Every, ScheduleContext

                scheduler = AsyncIOScheduler()

                for name, fn in scheduled.items():
                    meta = fn.__skaal_schedule__
                    trigger = meta["trigger"]
                    emit_to = meta.get("emit_to")
                    tz = meta.get("timezone", "UTC")

                    if isinstance(trigger, Every):
                        ap_trigger = IntervalTrigger(seconds=trigger.seconds, timezone=tz)
                    else:
                        ap_trigger = CronTrigger.from_crontab(trigger.expression, timezone=tz)

                    # Capture loop variables explicitly to avoid closure issues.
                    def _make_job(
                        _fn: Any = fn,
                        _emit_to: Any = emit_to,
                        _name: str = name,
                    ) -> Any:
                        async def _job() -> None:
                            sig = inspect.signature(_fn)
                            ctx = ScheduleContext(
                                fired_at=__import__("datetime").datetime.now(timezone.utc)
                            )
                            try:
                                if "ctx" in sig.parameters:
                                    result = (
                                        await _fn(ctx=ctx)
                                        if inspect.iscoroutinefunction(_fn)
                                        else _fn(ctx=ctx)
                                    )
                                else:
                                    result = (
                                        await _fn() if inspect.iscoroutinefunction(_fn) else _fn()
                                    )
                                if _emit_to is not None and result is not None:
                                    await _emit_to.send(result)
                            except Exception as exc:  # noqa: BLE001
                                print(f"  [schedule/{_name}] ERROR: {exc}")

                        return _job

                    scheduler.add_job(_make_job(), ap_trigger)

                scheduler.start()
            except ImportError:
                print(
                    "  WARNING: apscheduler not installed — scheduled functions will not run.\n"
                    "           Install with: pip install apscheduler\n"
                )

        try:
            config = uvicorn.Config(asgi_app, host=self.host, port=self.port, log_level="info")
            await uvicorn.Server(config).serve()
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)

    async def _serve_wsgi(self, wsgi_app: Any) -> None:
        """
        Serve a WSGI app (Dash/Flask) via uvicorn + starlette WSGIMiddleware.

        Skaal storage is already wired by ``__init__``; this method only
        handles the HTTP layer.  A ``/health`` endpoint is grafted onto the
        starlette router before the WSGI catch-all so that load-balancer
        probes work without touching the Flask app.

        Requires ``uvicorn`` and ``starlette`` — both are in ``skaal[gcp]``
        and can be installed standalone with::

            pip install uvicorn starlette
        """
        try:
            import uvicorn
            from starlette.applications import Starlette
            from starlette.middleware.wsgi import WSGIMiddleware
            from starlette.responses import JSONResponse
            from starlette.routing import Mount, Route
        except ImportError as exc:
            raise RuntimeError(
                "Serving a WSGI app locally requires uvicorn and starlette.\n"
                "Install them with:  pip install uvicorn starlette\n"
                f"Missing: {exc}"
            ) from exc

        async def _health(request: Any) -> JSONResponse:  # noqa: ANN001
            return JSONResponse({"status": "ok", "app": self.app.name})

        asgi_app = Starlette(
            routes=[
                Route("/health", _health),
                Mount("/", WSGIMiddleware(wsgi_app)),
            ]
        )

        attribute = getattr(self.app, "_wsgi_attribute", "wsgi_app")
        print(f"\n  Skaal local runtime — {self.app.name}  [WSGI: {attribute}]")
        print(f"  http://{self.host}:{self.port}\n")
        print("    /health  → Skaal health check")
        print(f"    /*       → {attribute}  (Dash / Flask)")
        print()

        config = uvicorn.Config(
            asgi_app,
            host=self.host,
            port=self.port,
            log_level="info",
        )
        server = uvicorn.Server(config)
        await server.serve()

    async def _serve_asgi(self, asgi_app: Any) -> None:
        """
        Serve a native ASGI app (FastAPI, Starlette) directly via uvicorn.

        Unlike WSGI apps, no middleware adapter is needed — the app is passed
        straight to uvicorn.  A ``/health`` endpoint is grafted in front so
        load-balancer probes work without touching the user's app.

        Requires ``uvicorn`` and ``starlette``::

            pip install uvicorn starlette
        """
        try:
            import uvicorn
            from starlette.applications import Starlette
            from starlette.responses import JSONResponse
            from starlette.routing import Mount, Route
        except ImportError as exc:
            raise RuntimeError(
                "Serving an ASGI app locally requires uvicorn and starlette.\n"
                "Install them with:  pip install uvicorn starlette\n"
                f"Missing: {exc}"
            ) from exc

        async def _health(request: Any) -> JSONResponse:  # noqa: ANN001
            return JSONResponse({"status": "ok", "app": self.app.name})

        wrapped = Starlette(
            routes=[
                Route("/health", _health),
                Mount("/", asgi_app),
            ]
        )

        attribute = getattr(self.app, "_asgi_attribute", "asgi_app")
        print(f"\n  Skaal local runtime — {self.app.name}  [ASGI: {attribute}]")
        print(f"  http://{self.host}:{self.port}\n")
        print("    /health  → Skaal health check")
        print(f"    /*       → {attribute}  (FastAPI / Starlette)")
        print()

        config = uvicorn.Config(wrapped, host=self.host, port=self.port, log_level="info")
        await uvicorn.Server(config).serve()
