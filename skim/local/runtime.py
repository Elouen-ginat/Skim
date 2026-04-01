"""LocalRuntime — serve a Skim App in-process for local development."""

from __future__ import annotations

import asyncio
import inspect
import json
import traceback
from typing import Any

from skim.local.storage import LocalMap, patch_storage_class


class LocalRuntime:
    """
    Runs a Skim App locally as a tiny HTTP server.

    - Each ``@app.function()`` becomes a ``POST /{name}`` endpoint.
    - Storage classes are patched with in-memory ``LocalMap`` backends.
    - ``GET /`` returns a JSON listing of available endpoints.

    Intended for development and testing only — not production.

    Usage::

        runtime = LocalRuntime(app, host="127.0.0.1", port=8000)
        asyncio.run(runtime.serve())
    """

    def __init__(
        self,
        app: Any,               # skim.App — avoid circular import
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

    # ── Setup ──────────────────────────────────────────────────────────────

    def _patch_storage(self) -> None:
        """Create LocalMap backends and patch all registered storage classes."""
        # Walk the full module tree so submodule storage is also patched.
        all_resources = self.app._collect_all()
        for qname, obj in all_resources.items():
            if isinstance(obj, type) and hasattr(obj, "__skim_storage__"):
                # Use override backend if provided, otherwise create a new LocalMap
                backend = self._backend_overrides.get(qname) or self._backend_overrides.get(obj.__name__)
                if backend is None:
                    backend = LocalMap()
                self._backends[qname] = backend
                patch_storage_class(obj, backend)

    # ── Factory methods ────────────────────────────────────────────────────

    @classmethod
    def from_redis(
        cls,
        app: Any,
        redis_url: str,
        host: str = "127.0.0.1",
        port: int = 8000,
    ) -> "LocalRuntime":
        """Create a LocalRuntime that uses Redis backends for all storage classes."""
        from skim.backends.redis_backend import RedisBackend

        backends: dict[str, Any] = {}
        all_resources = app._collect_all()
        for qname, obj in all_resources.items():
            if isinstance(obj, type) and hasattr(obj, "__skim_storage__"):
                # Use the qualified name as the namespace so keys don't collide
                namespace = qname.replace(".", "_").lower()
                backends[qname] = RedisBackend(url=redis_url, namespace=namespace)
        return cls(app, host=host, port=port, backend_overrides=backends)

    # ── HTTP dispatch ──────────────────────────────────────────────────────

    def _collect_functions(self) -> dict[str, Any]:
        """Flat map of qualified_name → callable for all registered functions."""
        funcs: dict[str, Any] = {}
        all_resources = self.app._collect_all()
        for qname, obj in all_resources.items():
            if callable(obj) and hasattr(obj, "__skim_compute__"):
                funcs[qname] = obj
        # Also include top-level functions by unqualified name for convenience.
        for name, fn in self.app._functions.items():
            funcs.setdefault(name, fn)
        return funcs

    async def _dispatch(
        self, method: str, path: str, body: bytes
    ) -> tuple[Any, int]:
        """
        Route an HTTP request to a registered function.

        Returns ``(response_body, http_status_code)``.
        """
        funcs = self._collect_functions()

        # GET / → index of available endpoints
        if method == "GET" and path in ("/", ""):
            index = {
                "app": self.app.name,
                "endpoints": [
                    {"path": f"/{name}", "function": name}
                    for name in sorted(funcs)
                ],
                "storage": list(self._backends.keys()),
            }
            return index, 200

        # GET /health → liveness probe
        if method == "GET" and path == "/health":
            return {"status": "ok", "app": self.app.name}, 200

        # POST /{fn_name} → call function
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
                if inspect.iscoroutinefunction(fn):
                    result = await fn(**kwargs)
                else:
                    result = fn(**kwargs)
                return result, 200
            except TypeError as exc:
                return {"error": f"Bad arguments for {fn_name!r}: {exc}"}, 422
            except Exception as exc:  # noqa: BLE001
                tb = traceback.format_exc()
                return {"error": str(exc), "traceback": tb}, 500

        return {"error": f"Method {method} not allowed"}, 405

    # ── TCP handler ────────────────────────────────────────────────────────

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            # Request line
            raw_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not raw_line:
                return
            parts = raw_line.decode("utf-8", errors="replace").split()
            if len(parts) < 2:
                return
            method = parts[0].upper()
            path = parts[1].split("?")[0]   # strip query string

            # Headers
            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10.0)
                if line in (b"\r\n", b"\n", b""):
                    break
                if b":" in line:
                    k, _, v = line.decode("utf-8", errors="replace").partition(":")
                    headers[k.lower().strip()] = v.strip()

            # Body
            content_length = int(headers.get("content-length", 0))
            body = b""
            if content_length > 0:
                body = await asyncio.wait_for(
                    reader.readexactly(content_length), timeout=30.0
                )

            # Dispatch
            response_data, status_code = await self._dispatch(method, path, body)

            # Serialise response
            try:
                payload = json.dumps(response_data, default=str).encode("utf-8")
            except (TypeError, ValueError):
                payload = json.dumps({"error": "Response is not JSON-serialisable"}).encode()
                status_code = 500

            reason = {200: "OK", 400: "Bad Request", 404: "Not Found",
                      405: "Method Not Allowed", 422: "Unprocessable Entity",
                      500: "Internal Server Error"}.get(status_code, "Unknown")

            response = (
                f"HTTP/1.1 {status_code} {reason}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            ).encode("utf-8") + payload

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

    # ── Entry point ────────────────────────────────────────────────────────

    async def serve(self) -> None:
        """Start the server and run until cancelled."""
        server = await asyncio.start_server(
            self._handle_connection, self.host, self.port
        )

        funcs = self._collect_functions()
        print(f"\n  Skim local runtime — {self.app.name}")
        print(f"  http://{self.host}:{self.port}\n")
        for name in sorted(funcs):
            print(f"    POST /{name}")
        print()

        async with server:
            await server.serve_forever()
