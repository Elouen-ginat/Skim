from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, cast

from skaal.types.runtime import RuntimeCallable, SupportsAsyncSend


class _SchedulerMixin:
    if TYPE_CHECKING:

        def _collect_schedules(self) -> dict[str, RuntimeCallable]: ...

    def _make_schedule_job(
        self,
        fn: RuntimeCallable,
        *,
        name: str,
        emit_to: SupportsAsyncSend | None,
        log_runs: bool,
    ) -> RuntimeCallable:
        async def _job() -> None:
            from datetime import datetime, timezone

            from skaal.schedule import ScheduleContext

            ctx = ScheduleContext(fired_at=datetime.now(timezone.utc))
            if log_runs:
                print(f"[skaal/schedule] {name} fired at {ctx.fired_at.isoformat()}", flush=True)
            try:
                sig = inspect.signature(fn)
                if "ctx" in sig.parameters:
                    result = await fn(ctx=ctx) if inspect.iscoroutinefunction(fn) else fn(ctx=ctx)
                else:
                    result = await fn() if inspect.iscoroutinefunction(fn) else fn()
                if emit_to is not None and result is not None:
                    await emit_to.send(result)
                if log_runs:
                    print(f"[skaal/schedule] {name} completed", flush=True)
            except Exception as exc:  # noqa: BLE001
                if log_runs:
                    print(f"[skaal/schedule] {name} ERROR: {exc}", flush=True)
                else:
                    print(f"  [schedule/{name}] ERROR: {exc}")

        return _job

    def _register_schedules(
        self,
        scheduler: object,
        scheduled: dict[str, RuntimeCallable],
        *,
        log_runs: bool,
    ) -> None:
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        from skaal.schedule import Every

        for name, fn in scheduled.items():
            meta = cast(dict[str, Any], getattr(fn, "__skaal_schedule__"))
            trigger = meta["trigger"]
            emit_to = meta.get("emit_to")
            tz = meta.get("timezone", "UTC")

            if isinstance(trigger, Every):
                ap_trigger = IntervalTrigger(seconds=trigger.seconds, timezone=tz)
            else:
                ap_trigger = CronTrigger.from_crontab(trigger.expression, timezone=tz)

            scheduler.add_job(  # type: ignore[attr-defined]
                self._make_schedule_job(
                    fn,
                    name=name,
                    emit_to=emit_to,
                    log_runs=log_runs,
                ),
                ap_trigger,
            )

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
            except ImportError:
                print(
                    "[skaal/scheduler] WARNING: apscheduler not installed"
                    " — scheduled functions will not run.\n"
                    "                  Install with: pip install apscheduler"
                )
                return

            scheduler = AsyncIOScheduler(event_loop=loop)
            self._register_schedules(scheduler, scheduled, log_runs=True)
            scheduler.start()
            print(
                f"[skaal/scheduler] started {len(scheduled)} job(s):" f" {list(scheduled)}",
                flush=True,
            )
            loop.run_forever()

        thread = threading.Thread(target=_run, daemon=True, name="skaal-scheduler")
        thread.start()
