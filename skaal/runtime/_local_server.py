from __future__ import annotations

from typing import TYPE_CHECKING, Any

from skaal.types.runtime import DispatchResult, RuntimeApp, RuntimeCallable


class _StarletteServerMixin:
    if TYPE_CHECKING:
        app: RuntimeApp
        host: str
        port: int

        def _collect_schedules(self) -> dict[str, RuntimeCallable]: ...

        def _register_schedules(
            self,
            scheduler: object,
            scheduled: dict[str, RuntimeCallable],
            *,
            log_runs: bool,
        ) -> None: ...

        async def _dispatch(self, method: str, path: str, body: bytes) -> DispatchResult: ...

        async def _serve_skaal(self) -> None: ...

    async def _serve_runtime(self) -> None:
        asgi_app = getattr(self.app, "_asgi_app", None)
        wsgi_app = getattr(self.app, "_wsgi_app", None)
        if asgi_app is not None:
            await self._serve_asgi(asgi_app)
            return
        if wsgi_app is not None:
            await self._serve_wsgi(wsgi_app)
            return

        scheduled = self._collect_schedules()
        scheduler = None
        if scheduled:
            try:
                from apscheduler.schedulers.asyncio import AsyncIOScheduler

                scheduler = AsyncIOScheduler()
                self._register_schedules(scheduler, scheduled, log_runs=False)
                scheduler.start()
            except ImportError:
                print(
                    "  WARNING: apscheduler not installed — scheduled functions will not run.\n"
                    "           Install with: pip install apscheduler\n"
                )

        try:
            await self._serve_skaal()
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)

    def _skaal_prefix(self) -> str:
        return "/_skaal"

    def _build_dispatch_routes(self, *, prefix: str = "") -> list[Any]:
        from starlette.requests import Request as StarletteRequest
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        normalized_prefix = prefix.rstrip("/")

        async def _handle(request: StarletteRequest) -> JSONResponse:
            body = await request.body()
            path = request.url.path
            if normalized_prefix and path.startswith(normalized_prefix):
                path = path[len(normalized_prefix) :] or "/"
                if not path.startswith("/"):
                    path = f"/{path}"
            result, status = await self._dispatch(request.method, path, body)
            return JSONResponse(result, status_code=status)

        if not normalized_prefix:
            return [
                Route("/", _handle, methods=["GET"]),
                Route("/health", _handle, methods=["GET"]),
                Route("/{path:path}", _handle, methods=["GET", "POST"]),
            ]

        return [
            Route(normalized_prefix, _handle, methods=["GET"]),
            Route(f"{normalized_prefix}/", _handle, methods=["GET"]),
            Route(f"{normalized_prefix}/health", _handle, methods=["GET"]),
            Route(f"{normalized_prefix}/{{path:path}}", _handle, methods=["GET", "POST"]),
        ]

    def _build_starlette_app(self, mounted_app: Any | None = None) -> Any:
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Mount, Route

        if mounted_app is None:
            return Starlette(routes=self._build_dispatch_routes())

        async def _health(request: Any) -> JSONResponse:  # noqa: ANN001
            return JSONResponse({"status": "ok", "app": self.app.name})

        return Starlette(
            routes=[
                Route("/health", _health),
                *self._build_dispatch_routes(prefix=self._skaal_prefix()),
                Mount("/", mounted_app),
            ]
        )

    async def _serve_with_starlette(
        self,
        mounted_app: Any,
        *,
        runtime_label: str,
        attribute: str,
        framework_label: str,
        missing_message: str,
    ) -> None:
        try:
            import uvicorn
        except ImportError as exc:
            raise RuntimeError(
                f"{missing_message}\n"
                "Install them with:  pip install uvicorn starlette\n"
                f"Missing: {exc}"
            ) from exc

        wrapped = self._build_starlette_app(mounted_app)

        print(f"\n  Skaal local runtime — {self.app.name}  [{runtime_label}: {attribute}]")
        print(f"  http://{self.host}:{self.port}\n")
        print("    /health     → Skaal health check")
        print(f"    {self._skaal_prefix()}/*  → Skaal runtime endpoints")
        print(f"    /*          → {attribute}  ({framework_label})")
        print()

        config = uvicorn.Config(wrapped, host=self.host, port=self.port, log_level="info")
        await uvicorn.Server(config).serve()

    def build_asgi(self) -> Any:
        """Return a Starlette ASGI app that serves all ``@app.function()`` endpoints.

        Use this in deployment entry-points where the ASGI server (gunicorn,
        uvicorn) is started externally rather than via :meth:`serve`::

            runtime   = LocalRuntime(app, backend_overrides={...})
            application = runtime.build_asgi()   # gunicorn main:application

        Returns:
            A ``starlette.applications.Starlette`` instance wired to
            :meth:`_dispatch`. If the app mounted a user ASGI/WSGI app,
            Skaal endpoints are namespaced under ``/_skaal/*`` and the user
            app remains mounted at ``/``.
        """
        try:
            mounted_app = getattr(self.app, "_asgi_app", None)
            if mounted_app is None:
                wsgi_app = getattr(self.app, "_wsgi_app", None)
                if wsgi_app is not None:
                    from starlette.middleware.wsgi import WSGIMiddleware

                    mounted_app = WSGIMiddleware(wsgi_app)
        except ImportError as exc:
            raise RuntimeError(
                "build_asgi() requires starlette.\n"
                "Install it with:  pip install starlette\n"
                f"Missing: {exc}"
            ) from exc

        return self._build_starlette_app(mounted_app)

    async def _serve_wsgi(self, wsgi_app: Any) -> None:
        """
        Serve a WSGI app (Dash/Flask) via uvicorn + starlette WSGIMiddleware.

        Skaal storage is already wired by ``__init__``; this method only
        handles the HTTP layer.  The user app remains mounted at ``/`` while
        Skaal endpoints are namespaced under ``/_skaal/*`` and ``/health``
        stays reserved for the runtime health probe.

        Requires ``uvicorn`` and ``starlette`` — both are in ``skaal[gcp]``
        and can be installed standalone with::

            pip install uvicorn starlette
        """
        try:
            from starlette.middleware.wsgi import WSGIMiddleware
        except ImportError as exc:
            raise RuntimeError(
                "Serving a WSGI app locally requires uvicorn and starlette.\n"
                "Install them with:  pip install uvicorn starlette\n"
                f"Missing: {exc}"
            ) from exc

        await self._serve_with_starlette(
            WSGIMiddleware(wsgi_app),
            runtime_label="WSGI",
            attribute=getattr(self.app, "_wsgi_attribute", "wsgi_app"),
            framework_label="Dash / Flask",
            missing_message="Serving a WSGI app locally requires uvicorn and starlette.",
        )

    async def _serve_asgi(self, asgi_app: Any) -> None:
        """
        Serve a native ASGI app (FastAPI, Starlette) directly via uvicorn.

        Unlike WSGI apps, no middleware adapter is needed — the app is passed
        straight to uvicorn.  The user app remains mounted at ``/`` while
        Skaal endpoints move under ``/_skaal/*`` and ``/health`` stays
        reserved for the runtime health probe.

        Requires ``uvicorn`` and ``starlette``::

            pip install uvicorn starlette
        """
        await self._serve_with_starlette(
            asgi_app,
            runtime_label="ASGI",
            attribute=getattr(self.app, "_asgi_attribute", "asgi_app"),
            framework_label="FastAPI / Starlette",
            missing_message="Serving an ASGI app locally requires uvicorn and starlette.",
        )
