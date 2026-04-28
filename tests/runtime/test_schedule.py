"""Tests for schedule trigger types and @app.schedule() integration."""

from __future__ import annotations

from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from skaal import App
from skaal.components import ScheduleTrigger
from skaal.runtime.local import LocalRuntime
from skaal.schedule import Cron, Every, ScheduleContext

# ── Every — interval parsing ───────────────────────────────────────────────────


def test_every_seconds():
    assert Every(interval="30s").seconds == 30.0


def test_every_minutes():
    assert Every(interval="5m").seconds == 300.0


def test_every_hours():
    assert Every(interval="2h").seconds == 7200.0


def test_every_long_form():
    assert Every(interval="10 minutes").seconds == 600.0


def test_every_invalid_format():
    with pytest.raises(ValueError, match="Invalid interval"):
        Every(interval="5x")


def test_every_invalid_no_unit():
    with pytest.raises(ValueError, match="Invalid interval"):
        Every(interval="100")


# ── Every — cloud expression conversion ───────────────────────────────────────


def test_every_as_rate_expression_seconds():
    assert Every(interval="30s").as_rate_expression() == "rate(30 seconds)"


def test_every_as_rate_expression_single_second():
    assert Every(interval="1s").as_rate_expression() == "rate(1 second)"


def test_every_as_rate_expression_minutes():
    assert Every(interval="5m").as_rate_expression() == "rate(5 minutes)"


def test_every_as_rate_expression_single_minute():
    assert Every(interval="1m").as_rate_expression() == "rate(1 minute)"


def test_every_as_rate_expression_hours():
    assert Every(interval="2h").as_rate_expression() == "rate(2 hours)"


def test_every_as_rate_expression_single_hour():
    assert Every(interval="1h").as_rate_expression() == "rate(1 hour)"


def test_every_as_cron_expression_minutes():
    assert Every(interval="5m").as_cron_expression() == "*/5 * * * *"


def test_every_as_cron_expression_hours():
    assert Every(interval="2h").as_cron_expression() == "0 */2 * * *"


def test_every_as_cron_expression_sub_minute_raises():
    with pytest.raises(ValueError, match="sub-minute"):
        Every(interval="30s").as_cron_expression()


# ── Cron — validation ─────────────────────────────────────────────────────────


def test_cron_valid():
    c = Cron(expression="*/5 * * * *")
    assert c.expression == "*/5 * * * *"


def test_cron_invalid_four_fields():
    with pytest.raises(ValueError, match="5 fields"):
        Cron(expression="* * * *")


def test_cron_invalid_six_fields():
    with pytest.raises(ValueError, match="5 fields"):
        Cron(expression="0 0 * * * *")


# ── Cron — cloud expression conversion ────────────────────────────────────────


def test_cron_as_aws_expression():
    c = Cron(expression="0 8 * * *")
    assert c.as_aws_expression() == "cron(0 8 * * * *)"


def test_cron_as_aws_expression_complex():
    c = Cron(expression="*/15 6-20 ? * MON-FRI")
    assert c.as_aws_expression() == "cron(*/15 6-20 ? * MON-FRI *)"


# ── ScheduleContext ────────────────────────────────────────────────────────────


def test_schedule_context_frozen():
    from datetime import datetime, timezone

    ctx = ScheduleContext(fired_at=datetime.now(timezone.utc))
    with pytest.raises(Exception):
        ctx.fired_at = datetime.now(timezone.utc)  # type: ignore[misc]


# ── @app.schedule() decorator ─────────────────────────────────────────────────


def test_schedule_decorator_registers_function():
    app = App("test-sched")

    @app.schedule(trigger=Every(interval="5m"))
    async def my_job():
        pass

    assert "my_job" in app._schedules
    assert app._schedules["my_job"] is my_job


def test_schedule_decorator_attaches_skaal_schedule():
    app = App("test-sched2")

    @app.schedule(trigger=Cron(expression="0 * * * *"))
    async def hourly():
        pass

    assert hasattr(hourly, "__skaal_schedule__")
    meta = hourly.__skaal_schedule__
    assert isinstance(meta["trigger"], Cron)
    assert meta["trigger"].expression == "0 * * * *"


def test_schedule_decorator_auto_creates_component():
    app = App("test-sched3")

    @app.schedule(trigger=Every(interval="10m"))
    async def poll():
        pass

    # ScheduleTrigger component should be in app._components
    assert "poll-schedule" in app._components
    comp = app._components["poll-schedule"]
    assert isinstance(comp, ScheduleTrigger)
    assert comp.target_function == "poll"
    assert comp.__skaal_component__["trigger_type"] == "every"


def test_schedule_decorator_with_parentheses():
    app = App("test-sched4")

    @app.schedule(trigger=Every(interval="1h"), timezone="US/Eastern")
    async def nightly():
        pass

    assert "nightly" in app._schedules
    comp = app._components["nightly-schedule"]
    assert comp.timezone == "US/Eastern"


def test_schedule_collect_all_includes_scheduled_fns():
    app = App("test-collect")

    @app.schedule(trigger=Every(interval="1m"))
    async def background():
        pass

    all_items = app._collect_all()
    # _collect_all() prefixes with the module name
    assert any("background" in k for k in all_items)


def test_schedule_describe_includes_schedules():
    app = App("test-describe")

    @app.schedule(trigger=Every(interval="30s"))
    async def poller():
        pass

    desc = app.describe()
    assert "schedules" in desc
    assert "poller" in desc["schedules"]


# ── LocalRuntime integration ───────────────────────────────────────────────────


def test_collect_schedules():
    app = App("test-rt-sched")

    @app.schedule(trigger=Every(interval="5m"))
    async def background():
        pass

    runtime = LocalRuntime(app)
    schedules = runtime._collect_schedules()
    assert "background" in schedules


def test_scheduled_fns_in_function_cache():
    app = App("test-fn-cache")

    @app.schedule(trigger=Every(interval="5m"))
    async def bg():
        pass

    runtime = LocalRuntime(app)
    assert "bg" in runtime._function_cache


@pytest.mark.asyncio
async def test_dispatch_schedule_trigger_injects_context():
    """POST with _skaal_trigger should inject ScheduleContext if fn accepts ctx."""
    import json

    app = App("test-dispatch-sched")
    received_ctx: list[ScheduleContext] = []

    @app.schedule(trigger=Every(interval="5m"))
    async def check(ctx: ScheduleContext):
        received_ctx.append(ctx)
        return "ok"

    runtime = LocalRuntime(app)
    body = json.dumps({"_skaal_trigger": "check-schedule"}).encode()
    result, status = await runtime._dispatch("POST", "/check", body)

    assert status == 200
    assert result == "ok"
    assert len(received_ctx) == 1
    assert isinstance(received_ctx[0], ScheduleContext)


@pytest.mark.asyncio
async def test_dispatch_schedule_trigger_no_ctx():
    """POST with _skaal_trigger on a fn without ctx param should still work."""
    import json

    app = App("test-dispatch-no-ctx")
    call_count = [0]

    @app.schedule(trigger=Every(interval="5m"))
    async def simple():
        call_count[0] += 1
        return "done"

    runtime = LocalRuntime(app)
    body = json.dumps({"_skaal_trigger": "simple-schedule"}).encode()
    result, status = await runtime._dispatch("POST", "/simple", body)

    assert status == 200
    assert result == "done"
    assert call_count[0] == 1


@pytest.mark.asyncio
async def test_dispatch_index_excludes_scheduled_fns():
    """GET / should not list scheduled functions in the public index."""
    app = App("test-index")

    @app.function()
    async def public_fn():
        return {}

    @app.schedule(trigger=Every(interval="5m"))
    async def bg():
        pass

    runtime = LocalRuntime(app)
    result, status = await runtime._dispatch("GET", "/", b"")

    assert status == 200
    endpoints = [e["function"] for e in result["endpoints"]]
    assert "public_fn" in endpoints
    assert "bg" not in endpoints


def test_mesh_runtime_uses_shared_scheduler_and_server_mixins():
    from skaal.runtime._local_scheduler import _SchedulerMixin
    from skaal.runtime._local_server import _StarletteServerMixin
    from skaal.runtime.mesh_runtime import MeshRuntime

    assert issubclass(MeshRuntime, _SchedulerMixin)
    assert issubclass(MeshRuntime, _StarletteServerMixin)


@pytest.mark.asyncio
async def test_mesh_runtime_serve_runtime_prefers_mounted_asgi_app() -> None:
    from skaal.runtime.mesh_runtime import MeshRuntime

    runtime = object.__new__(MeshRuntime)
    runtime.app = SimpleNamespace(name="mesh-test", _asgi_app=object(), _wsgi_app=None)
    runtime.host = "127.0.0.1"
    runtime.port = 8000
    runtime._serve_asgi = AsyncMock()
    runtime._serve_wsgi = AsyncMock()
    runtime._serve_skaal = AsyncMock()

    await runtime._serve_runtime()

    runtime._serve_asgi.assert_awaited_once_with(runtime.app._asgi_app)
    runtime._serve_wsgi.assert_not_called()
    runtime._serve_skaal.assert_not_called()


@pytest.mark.asyncio
async def test_mesh_runtime_serve_runtime_runs_scheduler_when_jobs_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skaal.runtime.mesh_runtime import MeshRuntime

    runtime = object.__new__(MeshRuntime)
    runtime.app = SimpleNamespace(name="mesh-test", _asgi_app=None, _wsgi_app=None)
    runtime.host = "127.0.0.1"
    runtime.port = 8000
    runtime._serve_skaal = AsyncMock()
    runtime._collect_schedules = Mock(return_value={"job": AsyncMock()})
    runtime._register_schedules = Mock()

    scheduler = Mock()

    async_module = ModuleType("apscheduler.schedulers.asyncio")
    async_module.AsyncIOScheduler = Mock(return_value=scheduler)
    monkeypatch.setitem(__import__("sys").modules, "apscheduler", ModuleType("apscheduler"))
    monkeypatch.setitem(
        __import__("sys").modules, "apscheduler.schedulers", ModuleType("apscheduler.schedulers")
    )
    monkeypatch.setitem(__import__("sys").modules, "apscheduler.schedulers.asyncio", async_module)

    await runtime._serve_runtime()

    runtime._register_schedules.assert_called_once_with(
        scheduler, {"job": runtime._collect_schedules.return_value["job"]}, log_runs=False
    )
    scheduler.start.assert_called_once_with()
    runtime._serve_skaal.assert_awaited_once_with()
    scheduler.shutdown.assert_called_once_with(wait=False)
