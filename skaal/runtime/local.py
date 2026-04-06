"""LocalRuntime — serve a Skaal App in-process for local development."""

from __future__ import annotations

import asyncio
import inspect
import json
import traceback
from pathlib import Path
from typing import Any

from skaal.backends.local_backend import LocalMap, patch_storage_class

# Maximum request body size: 10 MB
_MAX_BODY_SIZE = 10 * 1024 * 1024


def _wire_channel(channel_obj: Any) -> None:
    """Replace stub send/receive on a Channel instance with LocalChannel methods."""
    from skaal.runtime.channels import LocalChannel

    local = LocalChannel()

    async def _send(item: Any) -> None:
        await local.publish("default", item)

    async def _receive() -> Any:
        async for msg in local.subscribe("default"):
            yield msg

    channel_obj.send = _send
    channel_obj.receive = _receive
    channel_obj._local_channel = local


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
        """Patch all registered storage classes with appropriate backends."""
        for qname, obj in self.app._collect_all().items():
            if isinstance(obj, type) and hasattr(obj, "__skim_storage__"):
                backend = (
                    self._backend_overrides.get(qname)
                    or self._backend_overrides.get(obj.__name__)
                    or LocalMap()
                )
                self._backends[qname] = backend
                patch_storage_class(obj, backend)

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
            if isinstance(obj, type) and hasattr(obj, "__skim_storage__")
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

    # ── HTTP dispatch ──────────────────────────────────────────────────────────

    def _collect_functions(self) -> dict[str, Any]:
        """Flat map of qualified_name → callable for all registered functions."""
        funcs: dict[str, Any] = {
            qname: obj
            for qname, obj in self.app._collect_all().items()
            if callable(obj) and hasattr(obj, "__skim_compute__")
        }
        # Also expose top-level functions by short name for convenience.
        for name, fn in self.app._functions.items():
            funcs.setdefault(name, fn)
        return funcs

    async def _dispatch(self, method: str, path: str, body: bytes) -> tuple[Any, int]:
        """Route an HTTP request to a registered function."""
        funcs = self._function_cache

        if method == "GET" and path in ("/", ""):
            return {
                "app": self.app.name,
                "endpoints": [{"path": f"/{n}", "function": n} for n in sorted(funcs)],
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

            try:
                result = await fn(**kwargs) if inspect.iscoroutinefunction(fn) else fn(**kwargs)
                return result, 200
            except TypeError as exc:
                return {"error": f"Bad arguments for {fn_name!r}: {exc}"}, 422
            except Exception as exc:  # noqa: BLE001
                return {"error": str(exc), "traceback": traceback.format_exc()}, 500

        return {"error": f"Method {method} not allowed"}, 405

    # ── TCP server ─────────────────────────────────────────────────────────────

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not raw_line:
                return
            parts = raw_line.decode("utf-8", errors="replace").split()
            if len(parts) < 2:
                return
            method, path = parts[0].upper(), parts[1].split("?")[0]

            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10.0)
                if line in (b"\r\n", b"\n", b""):
                    break
                if b":" in line:
                    k, _, v = line.decode("utf-8", errors="replace").partition(":")
                    headers[k.lower().strip()] = v.strip()

            content_length = int(headers.get("content-length", 0))

            # Check for request size limit to prevent memory exhaustion
            if content_length > _MAX_BODY_SIZE:
                error_response = json.dumps(
                    {"error": f"Request body too large; max {_MAX_BODY_SIZE} bytes"}
                ).encode()
                response = (
                    f"HTTP/1.1 413 Payload Too Large\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(error_response)}\r\n"
                    f"Connection: close\r\n\r\n"
                ).encode() + error_response
                writer.write(response)
                await writer.drain()
                return

            body = b""
            if content_length > 0:
                body = await asyncio.wait_for(reader.readexactly(content_length), timeout=30.0)

            response_data, status_code = await self._dispatch(method, path, body)

            try:
                payload = json.dumps(response_data, default=str).encode("utf-8")
            except (TypeError, ValueError):
                payload = json.dumps({"error": "Response is not JSON-serialisable"}).encode()
                status_code = 500

            _STATUS = {
                200: "OK",
                400: "Bad Request",
                404: "Not Found",
                405: "Method Not Allowed",
                422: "Unprocessable Entity",
                500: "Internal Server Error",
            }
            response = (
                f"HTTP/1.1 {status_code} {_STATUS.get(status_code, 'Unknown')}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode() + payload

            writer.write(response)
            await writer.drain()

        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def serve(self) -> None:
        """
        Start the HTTP server and run until cancelled.

        When a WSGI app has been registered via ``app.mount_wsgi()``, delegates
        to :meth:`_serve_wsgi` which runs the WSGI app under uvicorn + starlette
        ``WSGIMiddleware``.  Otherwise falls back to the built-in asyncio TCP
        server that exposes Skaal functions as ``POST /{name}`` endpoints.
        """
        try:
            wsgi_app = getattr(self.app, "_wsgi_app", None)
            if wsgi_app is not None:
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
        """Built-in server: expose @app.function() as POST /{name} endpoints."""
        server = await asyncio.start_server(self._handle_connection, self.host, self.port)
        funcs = self._function_cache
        print(f"\n  Skaal local runtime — {self.app.name}")
        print(f"  http://{self.host}:{self.port}\n")
        for name in sorted(funcs):
            print(f"    POST /{name}")
        print()
        async with server:
            await server.serve_forever()

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
