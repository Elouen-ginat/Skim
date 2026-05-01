"""Schedule trigger types and context for ``@app.schedule()``.

Trigger types are Pydantic models — validated on construction and
serialisable to JSON for the constraint solver / plan file.

Cloud mapping
-------------
- :class:`Every` → AWS ``rate(N unit)``  |  GCP/APScheduler ``IntervalTrigger``
- :class:`Cron`  → AWS ``cron(...)``     |  GCP ``CronTrigger.from_crontab()``
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Union

from pydantic import BaseModel, ConfigDict, field_validator

# ── Interval parsing ──────────────────────────────────────────────────────────

_INTERVAL_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(s|sec|seconds?|m|min|minutes?|h|hr|hours?)$",
    re.IGNORECASE,
)

_UNIT_SECONDS: dict[str, float] = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hour": 3600,
    "hours": 3600,
}


def _parse_seconds(interval: str) -> float:
    """Parse ``'30s'``, ``'5m'``, ``'2h'`` → seconds as float.

    Raises :class:`ValueError` on unrecognised format.
    """
    match = _INTERVAL_RE.match(interval.strip())
    if not match:
        raise ValueError(
            f"Invalid interval {interval!r}. "
            "Use a number followed by s/m/h (e.g. '30s', '5m', '2h')."
        )
    value = float(match.group(1))
    unit = match.group(2).lower().rstrip(".")
    return value * _UNIT_SECONDS[unit]


# ── Public types ──────────────────────────────────────────────────────────────


class Every(BaseModel):
    """Repeat on a fixed interval.

    Accepts ``'30s'``, ``'5m'``, ``'2h'`` (seconds / minutes / hours).

    Example::

        @app.schedule(trigger=Every(interval="5m"))
        async def cleanup(): ...
    """

    interval: str

    @field_validator("interval")
    @classmethod
    def _validate_interval(cls, v: str) -> str:
        _parse_seconds(v)  # raises ValueError on bad format
        return v

    @property
    def seconds(self) -> float:
        """Interval in seconds."""
        return _parse_seconds(self.interval)

    def as_rate_expression(self) -> str:
        """AWS EventBridge ``rate(N unit)`` expression."""
        secs = self.seconds
        if secs >= 3600 and secs % 3600 == 0:
            n = int(secs // 3600)
            unit = "hour" if n == 1 else "hours"
        elif secs >= 60 and secs % 60 == 0:
            n = int(secs // 60)
            unit = "minute" if n == 1 else "minutes"
        else:
            n = int(secs)
            unit = "second" if n == 1 else "seconds"
        return f"rate({n} {unit})"

    def as_cron_expression(self) -> str:
        """5-field cron expression for GCP Cloud Scheduler.

        Only supports intervals that divide evenly into minutes or hours.
        Sub-minute intervals are not representable as cron and raise
        :class:`ValueError`.
        """
        secs = self.seconds
        if secs < 60:
            raise ValueError(
                f"Interval {self.interval!r} ({secs}s) is sub-minute and cannot be "
                "expressed as a cron expression. Use APScheduler's IntervalTrigger for "
                "sub-minute schedules (local only)."
            )
        mins = secs / 60
        if mins >= 60 and mins % 60 == 0:
            hrs = int(mins // 60)
            return f"0 */{hrs} * * *"
        if mins % 1 != 0:
            raise ValueError(
                f"Interval {self.interval!r} ({secs}s) does not divide evenly into "
                "minutes and cannot be expressed as a cron expression."
            )
        return f"*/{int(mins)} * * * *"


class Cron(BaseModel):
    """Standard 5-field cron expression.

    Example::

        @app.schedule(trigger=Cron(expression="0 8 * * *"))  # daily 08:00 UTC
        async def daily_report(): ...
    """

    expression: str

    @field_validator("expression")
    @classmethod
    def _validate_expression(cls, v: str) -> str:
        fields = v.split()
        if len(fields) != 5:
            raise ValueError(
                f"Cron expression must have exactly 5 fields, got {len(fields)}: {v!r}. "
                "Format: 'minute hour day-of-month month day-of-week'"
            )
        return v

    def as_aws_expression(self) -> str:
        """AWS EventBridge 6-field cron expression (appends year wildcard ``*``)."""
        min_, hr, dom, mon, dow = self.expression.split()
        return f"cron({min_} {hr} {dom} {mon} {dow} *)"


class ScheduleContext(BaseModel):
    """Injected into scheduled functions that declare a ``ctx: ScheduleContext`` param.

    Example::

        @app.schedule(trigger=Every(interval="1h"))
        async def hourly_job(ctx: ScheduleContext) -> None:
            print(f"Fired at {ctx.fired_at}")
    """

    fired_at: datetime
    model_config = ConfigDict(frozen=True)


# Union alias for type annotations
Schedule = Union[Every, Cron]


def build_apscheduler_trigger(trigger: Schedule, *, timezone: str) -> Any:
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    if isinstance(trigger, Every):
        return IntervalTrigger(seconds=trigger.seconds, timezone=timezone)
    return CronTrigger.from_crontab(trigger.expression, timezone=timezone)


def build_scheduled_job(
    fn: Callable[..., Any],
    *,
    name: str,
    emit_to: Any = None,
    logger: Any | None = None,
    log_lifecycle: bool = False,
) -> Callable[[], Awaitable[None]]:
    async def _job() -> None:
        ctx = ScheduleContext(fired_at=datetime.now(timezone.utc))
        if logger is not None and log_lifecycle:
            logger.info("[skaal/schedule] %s fired at %s", name, ctx.fired_at.isoformat())
        try:
            if "ctx" in inspect.signature(fn).parameters:
                result = await fn(ctx=ctx) if inspect.iscoroutinefunction(fn) else fn(ctx=ctx)
            else:
                result = await fn() if inspect.iscoroutinefunction(fn) else fn()
            if emit_to is not None and result is not None:
                await emit_to.send(result)
            if logger is not None and log_lifecycle:
                logger.info("[skaal/schedule] %s completed", name)
        except Exception as exc:  # noqa: BLE001
            if logger is not None:
                prefix = "[skaal/schedule]" if log_lifecycle else "[schedule/%s]"
                if log_lifecycle:
                    logger.warning("%s %s ERROR: %s", prefix, name, exc)
                else:
                    logger.warning(prefix, name, exc)

    return _job


def create_async_scheduler(
    scheduled: Mapping[str, Any],
    *,
    event_loop: Any | None = None,
    logger: Any | None = None,
    log_lifecycle: bool = False,
) -> Any:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = (
        AsyncIOScheduler(event_loop=event_loop) if event_loop is not None else AsyncIOScheduler()
    )

    for name, fn in scheduled.items():
        meta = fn.__skaal_schedule__
        scheduler.add_job(
            build_scheduled_job(
                fn,
                name=name,
                emit_to=meta.get("emit_to"),
                logger=logger,
                log_lifecycle=log_lifecycle,
            ),
            build_apscheduler_trigger(meta["trigger"], timezone=meta.get("timezone", "UTC")),
        )

    return scheduler


__all__ = [
    "Cron",
    "Every",
    "Schedule",
    "ScheduleContext",
    "build_apscheduler_trigger",
    "build_scheduled_job",
    "create_async_scheduler",
]
