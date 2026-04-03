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
        class Profiles(Map[str, Profile]):
            pass

        @app.function()
        async def predict(customer_id: str) -> float:
            ...
    """

    # ── WSGI app mounting ──────────────────────────────────────────────────

    def mount_wsgi(self, wsgi_attribute: str) -> None:
        """
        Register an external WSGI application to be served by this Skaal app.

        This tells the deploy generators to produce entry points that serve the
        WSGI app instead of Skaal's own JSON API.  Skaal storage is wired at
        startup so WSGI callbacks can use the ``sync_get`` / ``sync_set``
        methods on storage classes.

        Args:
            wsgi_attribute: Dotted attribute path to the WSGI callable in the
                            source module, e.g. ``"dash_app.server"`` or
                            ``"flask_app"``.

        Generated entry points:

        - **Cloud Run**: ``main.py`` with gunicorn serving the WSGI app.
          Storage is wired before gunicorn starts.
        - **Lambda**: ``handler.py`` with ``mangum`` wrapping the WSGI app
          as a Lambda handler.

        Example::

            import dash
            from skaal import App, Map

            app = App("dashboard")

            @app.storage(read_latency="< 5ms", durability="ephemeral", retention="30m")
            class Sessions(Map[str, dict]):
                pass

            dash_app = dash.Dash(__name__)
            app.mount_wsgi("dash_app.server")

            # In Dash callbacks — use sync wrappers:
            @dash_app.callback(...)
            def my_callback(session_id):
                state = Sessions.sync_get(session_id)   # safe in sync context
                Sessions.sync_set(session_id, new_state)
                return result
        """
        self._wsgi_attribute: str = wsgi_attribute

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
