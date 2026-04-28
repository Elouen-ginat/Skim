"""Schedule trigger types and context for ``@app.schedule()``.

Trigger types are Pydantic models — validated on construction and
serialisable to JSON for the constraint solver / plan file.

Cloud mapping
-------------
- :class:`Every` → AWS ``rate(N unit)``  |  GCP/APScheduler ``IntervalTrigger``
- :class:`Cron`  → AWS ``cron(...)``     |  GCP ``CronTrigger.from_crontab()``
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TypeAlias

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

    def to_aws_expression(self) -> str:
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

    def to_gcp_expression(self) -> str:
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

    def as_rate_expression(self) -> str:
        return self.to_aws_expression()

    def as_cron_expression(self) -> str:
        return self.to_gcp_expression()


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

    def to_aws_expression(self) -> str:
        """AWS EventBridge 6-field cron expression (appends year wildcard ``*``)."""
        min_, hr, dom, mon, dow = self.expression.split()
        return f"cron({min_} {hr} {dom} {mon} {dow} *)"

    def to_gcp_expression(self) -> str:
        """GCP Cloud Scheduler accepts standard 5-field cron expressions."""

        return self.expression

    def as_aws_expression(self) -> str:
        return self.to_aws_expression()


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
Schedule: TypeAlias = Every | Cron

__all__ = ["Cron", "Every", "Schedule", "ScheduleContext"]
