"""LocalRuntime — serve a Skaal App in-process for local development."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import traceback
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from skaal.backends.local_backend import LocalMap
from skaal.runtime.base import _SKAAL_INVOKE_PREFIX, BaseRuntime

if TYPE_CHECKING:
    import httpx

    from skaal.runtime.telemetry import RuntimeTelemetry
    from skaal.types import TelemetryConfig

_MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MiB — reject oversized request bodies
log = logging.getLogger("skaal.runtime")
_SKAAL_AGENT_PREFIX = "/_skaal/agents/"


def _format_banner(title: str, lines: list[str]) -> str:
    return "\n".join(["", title, *lines, ""])


def _wire_channel(channel_obj: Any) -> None:
    """Replace stub send/receive on a Channel instance with a local backend."""
    from skaal.channel import wire_local

    wire_local(channel_obj)


class LocalRuntime(BaseRuntime):
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
        kv_backend_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._kv_backend_factory = kv_backend_factory or (lambda _namespace: LocalMap())
        self.sagas: dict[str, Any] = {}
        super().__init__(
            app,
            host=host,
            port=port,
            backend_overrides=backend_overrides,
            telemetry=telemetry,
            telemetry_runtime=telemetry_runtime,
            auth_http_client=auth_http_client,
        )

    def _initialize_runtime_state(self) -> None:
        self._agent_backends: dict[str, Any] = {}
        self._agent_routes = self._collect_agents()
        self._agent_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _default_kv_backend(self, namespace: str) -> Any:
        return self._kv_backend_factory(namespace)

    def _default_relational_backend(self, namespace: str) -> Any:
        from skaal.backends.sqlite_backend import SqliteBackend

        return SqliteBackend(Path("skaal_local.db"), namespace=namespace)

    def _default_vector_backend(self, namespace: str) -> Any:
        from skaal.backends.chroma_backend import ChromaVectorBackend

        return ChromaVectorBackend(Path("skaal_chroma"), namespace=namespace)

    def _default_blob_backend(self, namespace: str) -> Any:
        from skaal.backends.file_blob_backend import FileBlobBackend

        return FileBlobBackend(Path(".skaal") / "blobs", namespace=namespace)

    def _wire_channel_instance(self, channel_obj: Any) -> None:
        _wire_channel(channel_obj)

    def _root_payload(self) -> dict[str, Any]:
        public = sorted({*self._public_functions(), *self.app._functions})
        return {
            "app": self.app.name,
            "endpoints": [
                {"path": f"{_SKAAL_INVOKE_PREFIX}{name}", "function": name} for name in public
            ],
            "agents": [
                {
                    "path": f"{_SKAAL_AGENT_PREFIX}{name}/{{identity}}/{{handler}}",
                    "agent": name,
                    "handlers": sorted(self._agent_handlers(agent_cls)),
                }
                for name, (qualified_name, agent_cls) in sorted(self._agent_routes.items())
                if name == qualified_name
            ],
            "storage": list(self.stores.keys()),
        }

    async def _dispatch_extra_post(
        self,
        path: str,
        request_payload: Any,
        request_headers: Mapping[str, str],
    ) -> tuple[Any, int, Exception | None] | None:
        from skaal.runtime.auth import RuntimeAuthFailure

        agent_target = self._agent_invocation_target(path)
        if agent_target is None:
            return None
        if not isinstance(request_payload, dict):
            return {"error": "Agent request body must be a JSON object"}, 400, None

        args = request_payload.get("args", [])
        kwargs = request_payload.get("kwargs", {})
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            return (
                {"error": "Agent request body must be {'args': [...], 'kwargs': {...}}"},
                400,
                None,
            )

        try:
            auth_claims, auth_subject = await self._authenticate_request(request_headers)
        except RuntimeAuthFailure as exc:
            return {"error": exc.message}, exc.status_code, None

        try:
            agent_name, identity, handler_name = agent_target
            del auth_claims, auth_subject
            result = await self.invoke_agent(agent_name, identity, handler_name, *args, **kwargs)
            return result, 200, None
        except KeyError as exc:
            return {"error": str(exc)}, 404, None
        except TypeError as exc:
            return {"error": f"Bad arguments for agent route {path!r}: {exc}"}, 422, None
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "traceback": traceback.format_exc()}, 500, exc

    async def _close_extra_resources(self) -> None:
        import contextlib

        for backend in self._agent_backends.values():
            with contextlib.suppress(Exception):
                await backend.close()
        self._agent_backends.clear()

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
        backends: dict[str, Any] = {}
        for qname, obj in app._collect_all().items():
            if not (isinstance(obj, type) and hasattr(obj, "__skaal_storage__")):
                continue
            backend = backend_factory(qname, obj)
            if backend is not None:
                backends[qname] = backend
        return backends

    @staticmethod
    def _normalized_backend_namespace(namespace: str) -> str:
        return namespace.replace(".", "_").lower()

    @staticmethod
    def _plugin_backend(name: str) -> type[Any]:
        from skaal import plugins

        return cast(type[Any], plugins.get_backend(name))

    @classmethod
    def _make_backend_instance(cls, name: str, namespace: str, **config: Any) -> Any:
        backend_cls = cls._plugin_backend(name)

        if name == "sqlite":
            db_path = Path(cast(str | Path, config["db_path"]))
            return backend_cls(db_path, namespace=namespace)
        if name == "redis":
            return backend_cls(
                url=cast(str, config["redis_url"]),
                namespace=cls._normalized_backend_namespace(namespace),
            )
        if name == "firestore":
            return backend_cls(
                collection=cls._normalized_backend_namespace(namespace),
                project=config.get("project"),
                database=cast(str, config.get("database", "(default)")),
            )
        if name == "postgres":
            return backend_cls(
                dsn=cast(str, config["dsn"]),
                namespace=namespace,
                min_size=cast(int, config.get("min_size", 1)),
                max_size=cast(int, config.get("max_size", 5)),
            )
        if name == "dynamodb":
            table_name = cast(str, config["table_name"])
            return backend_cls(
                table_name=f"{table_name}_{cls._normalized_backend_namespace(namespace)}",
                region=cast(str, config.get("region", "us-east-1")),
            )
        raise ValueError(f"LocalRuntime.from_backend() does not know how to configure {name!r}.")

    @classmethod
    def _make_vector_backend_instance(cls, name: str, namespace: str, **config: Any) -> Any:
        if name == "sqlite":
            chroma_cls = cls._plugin_backend("chroma")
            db_path = Path(cast(str | Path, config["db_path"]))
            chroma_path = db_path.parent / f"{db_path.stem}_chroma"
            return chroma_cls(chroma_path, namespace=namespace)
        if name == "postgres":
            pgvector_cls = cls._plugin_backend("pgvector")
            return pgvector_cls(dsn=cast(str, config["dsn"]), namespace=namespace)
        raise ValueError(
            f'LocalRuntime.from_backend({name!r}) does not support @app.storage(kind="vector") models.'
        )

    @classmethod
    def from_backend(
        cls,
        app: Any,
        name: str,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        **config: Any,
    ) -> "LocalRuntime":
        """Create a ``LocalRuntime`` from a named backend plugin plus backend config."""
        from skaal.blob import is_blob_model
        from skaal.relational import is_relational_model
        from skaal.vector import is_vector_model

        def _make_backend(qname: str, obj: Any) -> Any | None:
            if is_blob_model(obj):
                return None
            if is_vector_model(obj):
                return cls._make_vector_backend_instance(name, qname, **config)
            if is_relational_model(obj) and name not in {"sqlite", "postgres"}:
                raise ValueError(
                    f'LocalRuntime.from_backend({name!r}) does not support @app.storage(kind="relational") models.'
                )
            return cls._make_backend_instance(name, qname, **config)

        backends = cls._build_backends(app, _make_backend)
        return cls(
            app,
            host=host,
            port=port,
            backend_overrides=backends,
            kv_backend_factory=lambda namespace: cls._make_backend_instance(
                name, namespace, **config
            ),
        )

    @classmethod
    def from_redis(
        cls,
        app: Any,
        redis_url: str,
        host: str = "127.0.0.1",
        port: int = 8000,
    ) -> "LocalRuntime":
        """Create a ``LocalRuntime`` using Redis backends for all storage classes."""
        return cls.from_backend(
            app,
            "redis",
            host=host,
            port=port,
            redis_url=redis_url,
        )

    @classmethod
    def from_sqlite(
        cls,
        app: Any,
        db_path: str | Path = "skaal_local.db",
        host: str = "127.0.0.1",
        port: int = 8000,
    ) -> "LocalRuntime":
        """Create a ``LocalRuntime`` backed by SQLite."""
        return cls.from_backend(
            app,
            "sqlite",
            host=host,
            port=port,
            db_path=db_path,
        )

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
        return cls.from_backend(
            app,
            "firestore",
            host=host,
            port=port,
            project=project,
            database=database,
        )

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
        return cls.from_backend(
            app,
            "postgres",
            host=host,
            port=port,
            dsn=dsn,
            min_size=min_size,
            max_size=max_size,
        )

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
        return cls.from_backend(
            app,
            "dynamodb",
            host=host,
            port=port,
            table_name=table_name,
            region=region,
        )

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

    def _collect_schedules(self) -> dict[str, Any]:
        """Flat map of name → callable for all ``@app.schedule()`` functions."""
        return dict(getattr(self.app, "_schedules", {}))

    def _collect_agents(self) -> dict[str, tuple[str, type[Any]]]:
        agents: dict[str, tuple[str, type[Any]]] = {}
        short_names: dict[str, list[str]] = {}
        for qname, obj in self.app._collect_all().items():
            if not (isinstance(obj, type) and hasattr(obj, "__skaal_agent__")):
                continue
            agents[qname] = (qname, obj)
            short_names.setdefault(obj.__name__, []).append(qname)

        for short_name, qualified_names in short_names.items():
            if len(qualified_names) == 1:
                qualified_name = qualified_names[0]
                agents[short_name] = agents[qualified_name]

        return agents

    @staticmethod
    def _agent_invocation_target(path: str) -> tuple[str, str, str] | None:
        if not path.startswith(_SKAAL_AGENT_PREFIX):
            return None
        parts = path[len(_SKAAL_AGENT_PREFIX) :].split("/")
        if len(parts) != 3 or not all(parts):
            return None
        return parts[0], parts[1], parts[2]

    @staticmethod
    def _agent_handlers(agent_cls: type[Any]) -> dict[str, Any]:
        return {
            name: member
            for name, member in inspect.getmembers(agent_cls)
            if callable(member) and getattr(member, "__skaal_handler__", False)
        }

    def _agent_lock(self, agent_name: str, identity: str) -> asyncio.Lock:
        key = (agent_name, identity)
        lock = self._agent_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._agent_locks[key] = lock
        return lock

    def _agent_backend_name(self, agent_name: str) -> str:
        return f"{self.app.name}.__skaal_agents__.{agent_name}"

    def _agent_backend(self, agent_name: str) -> Any:
        backend_name = self._agent_backend_name(agent_name)
        backend = self._agent_backends.get(backend_name)
        if backend is not None:
            return backend

        backend = self._backend_overrides.get(backend_name)
        if backend is None:
            backend = self._kv_backend_factory(backend_name)
        self._agent_backends[backend_name] = backend
        return backend

    @staticmethod
    def _decode_agent_state(raw_state: Any) -> dict[str, Any] | None:
        if raw_state is None:
            return None
        if isinstance(raw_state, str):
            raw_state = json.loads(raw_state)
        if not isinstance(raw_state, dict):
            raise TypeError("Persisted agent state must be a JSON object")
        return raw_state

    async def invoke_agent(
        self,
        agent_name: str,
        identity: Any,
        handler_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        resolved = self._agent_routes.get(agent_name)
        if resolved is None:
            raise KeyError(f"No agent {agent_name!r}. Available: {sorted(self._agent_routes)}")

        qualified_name, agent_cls = resolved
        handler = self._agent_handlers(agent_cls).get(handler_name)
        if handler is None:
            raise KeyError(
                f"No handler {handler_name!r} on agent {agent_name!r}. "
                f"Available: {sorted(self._agent_handlers(agent_cls))}"
            )

        identity_key = str(identity)
        persistent = bool(getattr(agent_cls, "__skaal_agent__", {}).get("persistent", True))
        backend = self._agent_backend(qualified_name) if persistent else None

        async with self._agent_lock(qualified_name, identity_key):
            agent = agent_cls()
            setattr(agent, "identity", identity_key)
            persisted_state = self._decode_agent_state(
                await backend.get(identity_key) if backend is not None else None
            )
            agent._load_state(persisted_state)
            bound_handler = getattr(agent, handler_name)
            try:
                result = bound_handler(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
            except Exception:
                if backend is not None:
                    await backend.set(identity_key, persisted_state or {})
                raise

            if backend is not None:
                await backend.set(identity_key, agent._serialize_state())
            return result

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

    async def _serve_skaal(self) -> None:
        """Expose @app.function() as POST /{name} endpoints via uvicorn + Starlette.

        Also starts an APScheduler ``AsyncIOScheduler`` for any functions
        registered with ``@app.schedule()``.
        """

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
                from skaal.schedule import create_async_scheduler

                scheduler = create_async_scheduler(scheduled, logger=log)

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
                from skaal.schedule import create_async_scheduler
            except ImportError:
                log.warning(
                    "[skaal/scheduler] WARNING: apscheduler not installed"
                    " — scheduled functions will not run.\n"
                    "                  Install with: pip install apscheduler"
                )
                return

            scheduler = create_async_scheduler(
                scheduled,
                event_loop=loop,
                logger=log,
                log_lifecycle=True,
            )

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
