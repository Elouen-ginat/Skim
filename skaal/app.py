"""App — the central registry for a Skaal application."""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from skaal.module import Module, ModuleExport

F = TypeVar("F", bound=Callable[..., Any])


class App(Module):
    """
    Central registry for a Skaal application.

    ``App`` extends ``Module`` with HTTP mounting (``mount()``).  All storage,
    agent, function, channel, pattern, and attach methods are inherited from
    ``Module``.

    Deployment target and region are environment concerns — they are passed to
    ``skaal deploy`` via CLI flags or environment variables (``SKAAL_TARGET``,
    ``SKAAL_REGION``), not declared in application code.  Scaling policy
    (min/max instances, concurrency) lives in the catalog's
    ``[compute.X.deploy]`` section so it can be overridden per environment
    without touching source code.

    Usage::

        app = App("my-service")

        @app.storage(read_latency="< 5ms", durability="persistent")
        class Profiles(Store[Profile]):
            pass

        @app.function()
        async def predict(customer_id: str) -> float:
            ...
    """

    # ── WSGI app mounting ──────────────────────────────────────────────────

    def mount_wsgi(self, wsgi_app: Any | None = None, *, attribute: str) -> None:
        """
        Register an external WSGI application to be served by this Skaal app.

        Args:
            wsgi_app:  The WSGI callable itself (e.g. ``dash_app.server``).
                       Pass ``None`` if only generating deploy artifacts without
                       a running Dash/Flask instance (e.g. Dash not installed).
            attribute: Dotted attribute path in the source module used by the
                       deploy generators to reference the WSGI app in generated
                       entry-point files, e.g. ``"dash_app.server"``.

        ``skaal run`` uses *wsgi_app* directly — it serves via uvicorn +
        starlette ``WSGIMiddleware`` so the full Dash/Flask UI is available
        at ``http://localhost:<port>``.

        ``skaal deploy`` uses *attribute* to generate the correct entry point:

        - **Cloud Run**: ``main.py`` with gunicorn serving ``application``
        - **Lambda**: ``handler.py`` with ``Mangum`` wrapping the WSGI app

        Example::

            import dash
            from skaal import App, Store

            app = App("dashboard")

            @app.storage(read_latency="< 5ms", durability="ephemeral", retention="30m")
            class Sessions(Store[dict]):
                pass

            dash_app = dash.Dash(__name__)
            app.mount_wsgi(dash_app.server, attribute="dash_app.server")

            # In Dash callbacks:
            @dash_app.callback(...)
            def update(session_id):
                state = Sessions.sync_get(session_id)
                Sessions.sync_set(session_id, state)
                return result
        """
        self._wsgi_app: Any | None = wsgi_app
        self._wsgi_attribute: str = attribute

    def mount_asgi(self, asgi_app: Any | None = None, *, attribute: str) -> None:
        """
        Register a native ASGI application (FastAPI, Starlette) to be served by
        this Skaal app.

        Prefer this over ``mount_wsgi()`` for ASGI-native frameworks — no
        ``WSGIMiddleware`` adapter is needed, so you get full HTTP/2 and
        WebSocket support.

        Args:
            asgi_app:  The ASGI callable (e.g. ``fastapi_app``).
                       Pass ``None`` when generating deploy artifacts without a
                       live instance.
            attribute: Dotted attribute path used by deploy generators in the
                       generated entry-point files, e.g. ``"fastapi_app"``.

        Example::

            from fastapi import FastAPI
            from skaal import App, Store

            skaal_app = App("api")

            @skaal_app.storage(read_latency="< 10ms", durability="persistent")
            class Items(Store[Item]):
                pass

            fastapi_app = FastAPI()

            @fastapi_app.get("/items/{item_id}")
            async def get_item(item_id: str):
                return await Items.get(item_id)

            skaal_app.mount_asgi(fastapi_app, attribute="fastapi_app")
        """
        self._asgi_app: Any | None = asgi_app
        self._asgi_attribute: str = attribute

    # ── Module mounting ────────────────────────────────────────────────────

    def mount(self, module: Module, *, prefix: str) -> ModuleExport:
        """
        Embed a Module AND map its HTTP-serving functions under a URL prefix.

        Equivalent to ``app.use(module)`` but additionally registers route
        prefix mappings so the deploy engine wires the proxy / API gateway
        correctly.

        Usage::

            app.mount(auth, prefix="/auth")
            # auth's functions are now accessible at /auth/*
        """
        exports = self.use(module)
        # Record the prefix mapping for the deploy engine
        ns = exports.namespace or module.name
        if not hasattr(self, "_mounts"):
            self._mounts: dict[str, str] = {}
        self._mounts[ns] = prefix
        return exports

    # ── Introspection ──────────────────────────────────────────────────────

    def describe(self) -> dict[str, Any]:
        base = super().describe()
        base["mounts"] = getattr(self, "_mounts", {})
        return base

    def __repr__(self) -> str:
        return (
            f"App({self.name!r}, "
            f"storage={list(self._storage)}, "
            f"agents={list(self._agents)}, "
            f"functions={list(self._functions)})"
        )
