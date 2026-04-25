"""LocalRuntime — serve a Skaal App in-process for local development."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from skaal.backends.local_backend import LocalMap
from skaal.plan import PlanFile
from skaal.types.runtime import (
    AsyncClosable,
    BackendOverrides,
    RuntimeApp,
    RuntimeCallable,
    RuntimePlanSource,
)

from ._core import _RuntimeCoreMixin
from ._dispatch import _RuntimeDispatchMixin
from ._lifecycle import _RuntimeLifecycleMixin
from ._local_scheduler import _LocalSchedulerMixin
from ._local_server import _LocalServerMixin
from ._planning import build_backend_overrides, build_development_plan, coerce_runtime_plan
from ._transport import _RuntimeHttpTransportMixin


class LocalRuntime(
    _RuntimeCoreMixin,
    _RuntimeDispatchMixin,
    _RuntimeHttpTransportMixin,
    _RuntimeLifecycleMixin,
    _LocalServerMixin,
    _LocalSchedulerMixin,
):
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
        app: RuntimeApp,
        host: str = "127.0.0.1",
        port: int = 8000,
        backend_overrides: BackendOverrides | None = None,
        runtime_plan: RuntimePlanSource | None = None,
        target: str | None = None,
    ) -> None:
        if backend_overrides is not None and runtime_plan is not None:
            raise ValueError("Pass either backend_overrides or runtime_plan, not both.")

        self.app = app
        self.host = host
        self.port = port
        self._backends: dict[str, AsyncClosable] = {}
        self._runtime_plan: PlanFile | None
        if runtime_plan is not None:
            self._runtime_plan = coerce_runtime_plan(runtime_plan)
            self._backend_overrides = build_backend_overrides(
                app,
                self._runtime_plan,
                target=target,
            )
        elif backend_overrides is not None:
            self._runtime_plan = None
            self._backend_overrides = backend_overrides
        else:
            self._runtime_plan = build_development_plan(app, mode="memory")
            self._backend_overrides = build_backend_overrides(app, self._runtime_plan)

        self._patch_storage()
        self._patch_channels()
        self._initialize_runtime_state()

    # ── Setup ──────────────────────────────────────────────────────────────────

    def _patch_storage(self) -> None:
        """Wire all registered storage classes with appropriate backends."""
        from skaal.backends.chroma_backend import ChromaVectorBackend
        from skaal.backends.sqlite_backend import SqliteBackend

        self._patch_storage_backends(
            store_factory=lambda qname, obj: LocalMap(),
            vector_factory=lambda qname, obj: ChromaVectorBackend(
                Path("skaal_chroma"), namespace=qname
            ),
            relational_factory=lambda qname, obj: SqliteBackend(
                Path("skaal_local.db"), namespace=qname
            ),
        )

    def _patch_channels(self) -> None:
        """Wire Channel instances registered with the app to LocalChannel."""
        self._wire_local_channels()

    # ── Factory methods ────────────────────────────────────────────────────────

    @classmethod
    def from_plan(
        cls,
        app: RuntimeApp,
        plan: RuntimePlanSource,
        *,
        target: str | None = None,
        host: str = "127.0.0.1",
        port: int = 8000,
    ) -> "LocalRuntime":
        return cls(app, host=host, port=port, runtime_plan=plan, target=target)

    @classmethod
    def from_redis(
        cls,
        app: RuntimeApp,
        redis_url: str,
        host: str = "127.0.0.1",
        port: int = 8000,
    ) -> "LocalRuntime":
        """Create a ``LocalRuntime`` using Redis backends for all storage classes."""
        plan = build_development_plan(app, mode="redis", redis_url=redis_url)
        return cls.from_plan(app, plan, host=host, port=port)

    @classmethod
    def from_sqlite(
        cls,
        app: RuntimeApp,
        db_path: str | Path = "skaal_local.db",
        host: str = "127.0.0.1",
        port: int = 8000,
    ) -> "LocalRuntime":
        """Create a ``LocalRuntime`` backed by SQLite."""
        plan = build_development_plan(app, mode="sqlite", db_path=db_path)
        return cls.from_plan(app, plan, host=host, port=port)

    @classmethod
    def from_postgres(
        cls,
        app: RuntimeApp,
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
        plan = build_development_plan(
            app,
            mode="postgres",
            dsn=dsn,
            min_size=min_size,
            max_size=max_size,
        )
        return cls.from_plan(app, plan, host=host, port=port)

    def _banner_lines(self, public_fns: list[str]) -> list[str]:
        lines = [f"    POST /{name}" for name in public_fns]
        scheduled: dict[str, RuntimeCallable] = self._collect_schedules()
        if scheduled:
            lines.append("")
            for name, fn in sorted(scheduled.items()):
                schedule_meta = cast(dict[str, Any], getattr(fn, "__skaal_schedule__"))
                trigger = schedule_meta["trigger"]
                lines.append(f"    schedule /{name}  [{trigger!r}]")
        return lines

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
