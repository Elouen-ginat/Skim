"""LocalRuntime — serve a Skaal App in-process for local development."""

from __future__ import annotations

import inspect
import json
import logging
import traceback
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from skaal.backends.local_backend import LocalMap

if TYPE_CHECKING:
    import httpx

    from skaal.runtime.telemetry import RuntimeTelemetry
    from skaal.types import ReadinessState, TelemetryConfig

_MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MiB — reject oversized request bodies
log = logging.getLogger("skaal.runtime")
_SKAAL_INVOKE_PREFIX = "/_skaal/invoke/"


def _format_banner(title: str, lines: list[str]) -> str:
    return "\n".join(["", title, *lines, ""])


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
        *,
        telemetry: "TelemetryConfig | None" = None,
        telemetry_runtime: "RuntimeTelemetry | None" = None,
        auth_http_client: "httpx.AsyncClient | None" = None,
    ) -> None:
        from skaal.runtime.auth import JwtVerifier, resolve_gateway_auth
        from skaal.runtime.telemetry import RuntimeTelemetry, resolve_telemetry_config

        self.app = app
        self.host = host
        self.port = port
        self._backends: dict[str, Any] = {}
        self._backend_overrides = backend_overrides or {}
        self._started = False
        self._startup_lock: Any | None = None
        self._startup_error: str | None = None
        self._readiness_state: ReadinessState = "starting"
        self._patch_storage()
        self._patch_channels()
        # Cache the function map so it's not rebuilt on every HTTP request
        self._function_cache = self._collect_functions()
        # Pre-build resilience wrappers so breaker/bulkhead state is per-function
        # and persists across invocations.
        from skaal.runtime.middleware import wrap_handler

        self._invokers: dict[str, Any] = {}
        _shared_invokers: dict[int, Any] = {}
        for name, fn in self._function_cache.items():
            invoker = _shared_invokers.get(id(fn))
            if invoker is None:
                invoker = wrap_handler(fn, fallback_lookup=self._function_cache.get)
                _shared_invokers[id(fn)] = invoker
            self._invokers[name] = invoker
        # Pattern engines are started lazily by ``serve()`` so an asyncio loop
        # is already running when they spin up background tasks.
        self._engines: list[Any] = []
        self.sagas: dict[str, Any] = {}
        # Storage-class references indexed by name so engines can look them up.
        self._stores: dict[str, Any] = {
            qname: obj
            for qname, obj in self.app._collect_all().items()
            if isinstance(obj, type) and hasattr(obj, "__skaal_storage__")
        }
        self._auth_config = resolve_gateway_auth(app)
        self._auth_verifier = (
            JwtVerifier(self._auth_config, http_client=auth_http_client)
            if self._auth_config is not None
            else None
        )
        resolved_telemetry = resolve_telemetry_config(app, telemetry)
        self._telemetry = telemetry_runtime or RuntimeTelemetry(app.name, resolved_telemetry)
        self._telemetry.bind_runtime(self)
        self.app._bind_runtime(self)

    # ── Setup ──────────────────────────────────────────────────────────────────

    def _patch_storage(self) -> None:
        """Wire all registered storage classes with appropriate backends."""
        from skaal.backends.chroma_backend import ChromaVectorBackend
        from skaal.backends.file_blob_backend import FileBlobBackend
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
                backend = backend or SqliteBackend(Path("skaal_local.db"), namespace=qname)
                self._backends[qname] = backend
                wire_relational_model(obj, backend)
                continue

            if is_vector_model(obj):
                backend = backend or ChromaVectorBackend(Path("skaal_chroma"), namespace=qname)
                self._backends[qname] = backend
                cast(type[VectorStore[Any]], obj).wire(backend)
                continue

            if is_blob_model(obj):
                backend = backend or FileBlobBackend(Path(".skaal") / "blobs", namespace=qname)
                self._backends[qname] = backend
                cast(type[BlobStore], obj).wire(backend)
                continue

            if issubclass(obj, Store):
                backend = backend or LocalMap()
                self._backends[qname] = backend
                obj.wire(backend)
            elif issubclass(obj, VectorStore):
                backend = backend or ChromaVectorBackend(Path("skaal_chroma"), namespace=qname)
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
        from skaal.relational import is_relational_model
        from skaal.vector import is_vector_model

        def _make_backend(qname: str, obj: Any) -> RedisBackend:
            if is_relational_model(obj) or is_vector_model(obj):
                raise ValueError(
                    "LocalRuntime.from_redis() does not support @app.relational or @app.vector models."
                )
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
        from skaal.backends.chroma_backend import ChromaVectorBackend
        from skaal.backends.sqlite_backend import SqliteBackend
        from skaal.vector import is_vector_model

        def _make_backend(qname: str, obj: Any) -> Any:
            if is_vector_model(obj):
                chroma_path = Path(db_path).parent / f"{Path(db_path).stem}_chroma"
                return ChromaVectorBackend(chroma_path, namespace=qname)
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
        from skaal.relational import is_relational_model
        from skaal.vector import is_vector_model

        def _make_backend(qname: str, obj: Any) -> FirestoreBackend:
            if is_relational_model(obj) or is_vector_model(obj):
                raise ValueError(
                    "LocalRuntime.from_firestore() does not support @app.relational or @app.vector models."
                )
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
        from skaal.backends.pgvector_backend import PgVectorBackend
        from skaal.backends.postgres_backend import PostgresBackend
        from skaal.vector import is_vector_model

        def _make_backend(qname: str, obj: Any) -> Any:
            if is_vector_model(obj):
                return PgVectorBackend(dsn=dsn, namespace=qname)
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
        from skaal.relational import is_relational_model
        from skaal.vector import is_vector_model

        def _make_backend(qname: str, obj: Any) -> DynamoBackend:
            if is_relational_model(obj) or is_vector_model(obj):
                raise ValueError(
                    "LocalRuntime.from_dynamodb() does not support @app.relational or @app.vector models."
                )
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

    @property
    def readiness_state(self) -> ReadinessState:
        return self._readiness_state

    async def ensure_started(self) -> None:
        import asyncio

        if self._started:
            return
        if self._startup_lock is None:
            self._startup_lock = asyncio.Lock()

        async with self._startup_lock:
            if self._started:
                return
            self._readiness_state = "starting"
            self._startup_error = None
            try:
                if self._auth_verifier is not None and not self._auth_verifier.ready:
                    await self._auth_verifier.initialize()
                await self._start_engines()
            except Exception as exc:
                self._startup_error = str(exc)
                self._readiness_state = "degraded"
                raise
            self._started = True
            self._readiness_state = "ready"

    def _readiness_payload(self) -> dict[str, Any]:
        auth_ready = self._auth_verifier.ready if self._auth_verifier is not None else True
        checks = {
            "engines_started": self._started,
            "auth": auth_ready,
            "telemetry": self._telemetry.status(),
        }
        if self._startup_error is not None:
            checks["error"] = self._startup_error
        return {
            "status": self._readiness_state,
            "app": self.app.name,
            "checks": checks,
        }

    async def _authenticate_request(
        self, headers: Mapping[str, str]
    ) -> tuple[dict[str, Any] | None, str | None]:
        from skaal.runtime.auth import RuntimeAuthFailure

        if self._auth_verifier is None:
            self._telemetry.record_auth_result("skipped")
            return None, None

        try:
            claims = await self._auth_verifier.verify_headers(headers)
        except RuntimeAuthFailure:
            self._telemetry.record_auth_result("rejected")
            raise

        if claims is None:
            self._telemetry.record_auth_result("skipped")
            return None, None

        subject = claims.get("sub")
        self._telemetry.record_auth_result("accepted")
        return claims, subject if isinstance(subject, str) else None

    async def invoke(
        self,
        function_name: str,
        kwargs: dict[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
        auth_claims: Mapping[str, Any] | None = None,
        auth_subject: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> Any:
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
                headers=headers,
                auth_claims=auth_claims,
                auth_subject=auth_subject,
                trace_id=trace_id,
                span_id=span_id,
            ),
        )

    def invoke_stream(
        self,
        function_name: str,
        kwargs: dict[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
        auth_claims: Mapping[str, Any] | None = None,
        auth_subject: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> Any:
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
                headers=headers,
                auth_claims=auth_claims,
                auth_subject=auth_subject,
                trace_id=trace_id,
                span_id=span_id,
            ),
        )

    async def _dispatch(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any, int]:
        """Route an HTTP request to a registered function."""
        from skaal.runtime.auth import RuntimeAuthFailure

        funcs = self._function_cache
        request_headers = dict(headers or {})
        request_span = self._telemetry.request_started(method, path, request_headers)
        status = 500
        telemetry_error: Exception | None = None

        try:
            if method == "GET" and path in ("/", ""):
                public = sorted(self._public_functions())
                status = 200
                return {
                    "app": self.app.name,
                    "endpoints": [
                        {"path": f"{_SKAAL_INVOKE_PREFIX}{n}", "function": n} for n in public
                    ],
                    "storage": list(self._backends.keys()),
                }, status

            if method == "GET" and path == "/health":
                status = 200
                return {"status": "ok", "app": self.app.name}, status

            if method == "GET" and path == "/ready":
                payload = self._readiness_payload()
                status = 200 if self._readiness_state == "ready" else 503
                return payload, status

            if method == "POST":
                try:
                    await self.ensure_started()
                except Exception:
                    status = 503
                    return self._readiness_payload(), status

                fn_name = self._invocation_target(path)
                if fn_name is None:
                    status = 404
                    return {"error": f"No function route for {path!r}"}, status
                if fn_name not in funcs:
                    status = 404
                    return {"error": f"No function {fn_name!r}. Available: {sorted(funcs)}"}, status

                try:
                    auth_claims, auth_subject = await self._authenticate_request(request_headers)
                except RuntimeAuthFailure as exc:
                    status = exc.status_code
                    return {"error": exc.message}, status

                fn = funcs[fn_name]
                kwargs: dict[str, Any] = {}
                if body:
                    try:
                        kwargs = json.loads(body)
                        if not isinstance(kwargs, dict):
                            status = 400
                            return {"error": "Request body must be a JSON object"}, status
                    except json.JSONDecodeError as exc:
                        status = 400
                        return {"error": f"Invalid JSON: {exc}"}, status

                is_schedule_invocation = kwargs.pop("_skaal_trigger", None) is not None
                if is_schedule_invocation:
                    sig = inspect.signature(fn)
                    if "ctx" in sig.parameters:
                        from datetime import timezone

                        from skaal.schedule import ScheduleContext

                        kwargs["ctx"] = ScheduleContext(
                            fired_at=__import__("datetime").datetime.now(timezone.utc)
                        )

                invoker = self._invokers.get(fn_name)
                try:
                    if invoker is not None:
                        result = await self.invoke(
                            fn_name,
                            kwargs,
                            headers=request_headers,
                            auth_claims=auth_claims,
                            auth_subject=auth_subject,
                            trace_id=request_span.trace_id,
                            span_id=request_span.span_id,
                        )
                    else:
                        result = (
                            await fn(**kwargs) if inspect.iscoroutinefunction(fn) else fn(**kwargs)
                        )
                    status = 200
                    return result, status
                except TypeError as exc:
                    status = 422
                    return {"error": f"Bad arguments for {fn_name!r}: {exc}"}, status
                except Exception as exc:  # noqa: BLE001
                    telemetry_error = exc
                    status = 500
                    return {"error": str(exc), "traceback": traceback.format_exc()}, status

            status = 405
            return {"error": f"Method {method} not allowed"}, status
        finally:
            self._telemetry.request_finished(
                request_span,
                status_code=status,
                error=telemetry_error,
            )

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

            result, status = await self._dispatch(method, path, body, headers=headers)
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
        """Return a Starlette ASGI app for the active runtime surface.

        Use this in deployment entry-points where the ASGI server (gunicorn,
        uvicorn) is started externally rather than via :meth:`serve`::

            runtime   = LocalRuntime(app, backend_overrides={...})
            application = runtime.build_asgi()   # gunicorn main:application

        Returns:
            A ``starlette.applications.Starlette`` instance wired to
            the mounted ASGI/WSGI app or, when none is mounted, :meth:`_dispatch`.
        """
        try:
            from contextlib import asynccontextmanager

            from starlette.applications import Starlette
            from starlette.middleware.wsgi import WSGIMiddleware
            from starlette.requests import Request as StarletteRequest
            from starlette.responses import JSONResponse
            from starlette.routing import Mount, Route
        except ImportError as exc:
            raise RuntimeError(
                "build_asgi() requires starlette.\n"
                "Install it with:  pip install starlette\n"
                f"Missing: {exc}"
            ) from exc

        @asynccontextmanager
        async def _lifespan(app: Any) -> AsyncIterator[None]:  # noqa: ANN401
            del app
            await self.ensure_started()
            try:
                yield
            finally:
                await self.shutdown()

        async def _handle(request: StarletteRequest) -> JSONResponse:
            body = await request.body()
            result, status = await self._dispatch(
                request.method,
                request.url.path,
                body,
                headers=dict(request.headers.items()),
            )
            return JSONResponse(result, status_code=status)

        async def _health(request: Any) -> JSONResponse:  # noqa: ANN001
            return JSONResponse({"status": "ok", "app": self.app.name})

        async def _ready(request: Any) -> JSONResponse:  # noqa: ANN001
            payload = self._readiness_payload()
            status = 200 if self._readiness_state == "ready" else 503
            return JSONResponse(payload, status_code=status)

        asgi_app = getattr(self.app, "_asgi_app", None)
        wsgi_app = getattr(self.app, "_wsgi_app", None)
        if asgi_app is not None:
            application = Starlette(
                lifespan=_lifespan,
                routes=[
                    Route("/health", _health),
                    Route("/ready", _ready),
                    Route("/_skaal/{path:path}", _handle, methods=["GET", "POST"]),
                    Mount("/", asgi_app),
                ],
            )
            return application

        if wsgi_app is not None:
            application = Starlette(
                lifespan=_lifespan,
                routes=[
                    Route("/health", _health),
                    Route("/ready", _ready),
                    Route("/_skaal/{path:path}", _handle, methods=["GET", "POST"]),
                    Mount("/", WSGIMiddleware(wsgi_app)),
                ],
            )
            return application

        application = Starlette(
            lifespan=_lifespan,
            routes=[
                Route("/", _handle, methods=["GET"]),
                Route("/health", _handle, methods=["GET"]),
                Route("/ready", _handle, methods=["GET"]),
                Route("/{path:path}", _handle, methods=["GET", "POST"]),
            ],
        )
        return application

    async def serve(self) -> None:
        """
        Start the HTTP server and run until cancelled.

        Dispatch order:
        - ASGI app registered via ``app.mount_asgi()`` → :meth:`_serve_asgi`
        - WSGI app registered via ``app.mount_wsgi()`` → :meth:`_serve_wsgi`
        - Otherwise → :meth:`_serve_skaal` (Skaal functions as POST endpoints)
        """
        await self.ensure_started()
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

    async def _start_engines(self) -> None:
        """Spin up all pattern engines (EventLog / Projection / Saga / Outbox)."""
        from skaal.runtime.engines import start_engines_for

        if self._engines:
            return
        self._engines = await start_engines_for(self.app, self)

    @property
    def functions(self) -> dict[str, Any]:
        """Expose the handler registry to pattern engines (read-only view)."""
        return self._function_cache

    @property
    def stores(self) -> dict[str, Any]:
        """Expose storage classes by name to pattern engines."""
        return self._stores

    async def shutdown(self) -> None:
        """
        Shut down the runtime by closing all backend connections.

        Called automatically when serve() exits. Can also be called explicitly
        to clean up resources.
        """
        import contextlib

        for engine in self._engines:
            with contextlib.suppress(Exception):
                await engine.stop()
        self._engines = []
        self._started = False
        self._readiness_state = "stopped"

        for backend in self._backends.values():
            with contextlib.suppress(Exception):
                await backend.close()
        with contextlib.suppress(Exception):
            self._telemetry.shutdown()
        self.app._unbind_runtime(self)

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
        public_fns = sorted(self._public_functions())
        scheduled = self._collect_schedules()

        banner_lines = [f"  http://{self.host}:{self.port}", ""]
        for name in public_fns:
            banner_lines.append(f"    POST {_SKAAL_INVOKE_PREFIX}{name}")
        if scheduled:
            banner_lines.append("")
            for name, fn in sorted(scheduled.items()):
                meta = fn.__skaal_schedule__
                trigger = meta["trigger"]
                banner_lines.append(f"    schedule /{name}  [{trigger!r}]")
        log.info(_format_banner(f"  Skaal local runtime — {self.app.name}", banner_lines))

        # ── Starlette ASGI app — delegates to existing _dispatch ──────────────
        async def _handle(request: StarletteRequest) -> JSONResponse:
            body = await request.body()
            result, status = await self._dispatch(
                request.method,
                request.url.path,
                body,
                headers=dict(request.headers.items()),
            )
            return JSONResponse(result, status_code=status)

        asgi_app = Starlette(
            routes=[
                Route("/", _handle, methods=["GET"]),
                Route("/health", _handle, methods=["GET"]),
                Route("/ready", _handle, methods=["GET"]),
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
                        ap_trigger = IntervalTrigger(
                            seconds=cast(Any, trigger.seconds), timezone=tz
                        )
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
                                log.warning("[schedule/%s] ERROR: %s", _name, exc)

                        return _job

                    scheduler.add_job(_make_job(), ap_trigger)

                scheduler.start()
            except ImportError:
                log.warning(
                    "  WARNING: apscheduler not installed — scheduled functions will not run.\n"
                    "           Install with: pip install apscheduler\n"
                )

        try:
            config = uvicorn.Config(asgi_app, host=self.host, port=self.port, log_level="info")
            await uvicorn.Server(config).serve()
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)

    def start_background_scheduler(self) -> None:
        """Start APScheduler in a daemon thread for WSGI / gunicorn deployments.

        ``_serve_skaal`` runs APScheduler inside an asyncio event loop that it
        owns.  When gunicorn serves a WSGI app it never calls ``serve()``, so the
        scheduler would not start.  Call this method from the generated ``main.py``
        (or any gunicorn entry-point) immediately after constructing
        ``LocalRuntime`` to get the same scheduling behaviour::

            runtime = LocalRuntime(app, backend_overrides={...})
            runtime.start_background_scheduler()   # ← add this line
            application = app.dash_app.server

        The thread is daemonised so it does not prevent gunicorn from shutting
        down.  Each scheduled function fires in its own asyncio event loop
        running inside the thread; the ``ScheduleContext.fired_at`` timestamp
        and any errors are printed to stdout so they appear in ``docker logs``.
        """
        import threading

        scheduled = self._collect_schedules()
        if not scheduled:
            return

        def _run() -> None:
            import asyncio as _asyncio

            loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(loop)

            try:
                from apscheduler.schedulers.asyncio import AsyncIOScheduler
                from apscheduler.triggers.cron import CronTrigger
                from apscheduler.triggers.interval import IntervalTrigger

                from skaal.schedule import Every, ScheduleContext
            except ImportError:
                log.warning(
                    "[skaal/scheduler] WARNING: apscheduler not installed"
                    " — scheduled functions will not run.\n"
                    "                  Install with: pip install apscheduler"
                )
                return

            scheduler = AsyncIOScheduler(event_loop=loop)

            for name, fn in scheduled.items():
                meta = fn.__skaal_schedule__
                trigger = meta["trigger"]
                emit_to = meta.get("emit_to")
                tz = meta.get("timezone", "UTC")

                if isinstance(trigger, Every):
                    ap_trigger = IntervalTrigger(seconds=cast(Any, trigger.seconds), timezone=tz)
                else:
                    ap_trigger = CronTrigger.from_crontab(trigger.expression, timezone=tz)

                def _make_job(
                    _fn: Any = fn,
                    _emit_to: Any = emit_to,
                    _name: str = name,
                ) -> Any:
                    async def _job() -> None:
                        from datetime import timezone as _tz

                        ctx = ScheduleContext(fired_at=__import__("datetime").datetime.now(_tz.utc))
                        log.info("[skaal/schedule] %s fired at %s", _name, ctx.fired_at.isoformat())
                        try:
                            if "ctx" in inspect.signature(_fn).parameters:
                                result = (
                                    await _fn(ctx=ctx)
                                    if inspect.iscoroutinefunction(_fn)
                                    else _fn(ctx=ctx)
                                )
                            else:
                                result = await _fn() if inspect.iscoroutinefunction(_fn) else _fn()
                            if _emit_to is not None and result is not None:
                                await _emit_to.send(result)
                            log.info("[skaal/schedule] %s completed", _name)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("[skaal/schedule] %s ERROR: %s", _name, exc)

                    return _job

                scheduler.add_job(_make_job(), ap_trigger)

            scheduler.start()
            log.info("[skaal/scheduler] started %s job(s): %s", len(scheduled), list(scheduled))
            loop.run_forever()

        thread = threading.Thread(target=_run, daemon=True, name="skaal-scheduler")
        thread.start()

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
            from starlette.requests import Request as StarletteRequest
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

        async def _internal(request: StarletteRequest) -> JSONResponse:
            body = await request.body()
            result, status = await self._dispatch(
                request.method,
                request.url.path,
                body,
                headers=dict(request.headers.items()),
            )
            return JSONResponse(result, status_code=status)

        asgi_app = Starlette(
            routes=[
                Route("/health", _health),
                Route("/ready", _internal, methods=["GET"]),
                Route("/_skaal/{path:path}", _internal, methods=["GET", "POST"]),
                Mount("/", WSGIMiddleware(wsgi_app)),
            ]
        )

        attribute = getattr(self.app, "_wsgi_attribute", "wsgi_app")
        log.info(
            _format_banner(
                f"  Skaal local runtime — {self.app.name}  [WSGI: {attribute}]",
                [
                    f"  http://{self.host}:{self.port}",
                    "",
                    "    /health  → Skaal health check",
                    "    /_skaal/* → Skaal internal invoke endpoints",
                    f"    /*       → {attribute}  (Dash / Flask)",
                ],
            )
        )

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

        async def _handle(request: Any) -> JSONResponse:  # noqa: ANN001
            body = await request.body()
            result, status = await self._dispatch(
                request.method,
                request.url.path,
                body,
                headers=dict(request.headers.items()),
            )
            return JSONResponse(result, status_code=status)

        wrapped = Starlette(
            routes=[
                Route("/health", _health),
                Route("/ready", _handle, methods=["GET"]),
                Route("/_skaal/{path:path}", _handle, methods=["GET", "POST"]),
                Mount("/", asgi_app),
            ]
        )

        attribute = getattr(self.app, "_asgi_attribute", "asgi_app")
        log.info(
            _format_banner(
                f"  Skaal local runtime — {self.app.name}  [ASGI: {attribute}]",
                [
                    f"  http://{self.host}:{self.port}",
                    "",
                    "    /health  → Skaal health check",
                    "    /_skaal/* → Skaal internal invoke endpoints",
                    f"    /*       → {attribute}  (FastAPI / Starlette)",
                ],
            )
        )

        config = uvicorn.Config(wrapped, host=self.host, port=self.port, log_level="info")
        await uvicorn.Server(config).serve()
