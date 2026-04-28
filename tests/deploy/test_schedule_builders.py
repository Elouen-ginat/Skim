from __future__ import annotations

from skaal.app import App
from skaal.components import ScheduleTrigger
from skaal.deploy.builders._schedule import _load_schedule
from skaal.deploy.builders.aws_stack import _build_pulumi_stack as build_aws_stack
from skaal.deploy.builders.gcp_stack import _build_pulumi_stack as build_gcp_stack
from skaal.plan import ComponentSpec, PlanFile
from skaal.schedule import Cron, Every


def test_load_schedule_round_trips_every_payload() -> None:
    trigger = Every(interval="5m")

    loaded = _load_schedule({"trigger": trigger.model_dump()})

    assert isinstance(loaded, Every)
    assert loaded.to_aws_expression() == "rate(5 minutes)"


def test_aws_schedule_trigger_uses_typed_schedule_expression() -> None:
    app = App("demo")
    trigger = ScheduleTrigger(
        "poll-schedule",
        trigger=Every(interval="5m"),
        target_function="poll",
    )
    plan = PlanFile(
        app_name="demo",
        components={
            "poll-schedule": ComponentSpec(
                component_name="poll-schedule",
                kind="schedule-trigger",
                config=dict(trigger.__skaal_component__),
            )
        },
    )

    stack = build_aws_stack(app, plan)

    assert stack["resources"]["poll-schedule-rule"]["properties"]["scheduleExpression"] == (
        "rate(5 minutes)"
    )


def test_gcp_schedule_trigger_uses_typed_schedule_expression() -> None:
    app = App("demo")
    trigger = ScheduleTrigger(
        "daily-schedule",
        trigger=Cron(expression="0 8 * * *"),
        target_function="daily",
    )
    plan = PlanFile(
        app_name="demo",
        components={
            "daily-schedule": ComponentSpec(
                component_name="daily-schedule",
                kind="schedule-trigger",
                config=dict(trigger.__skaal_component__),
            )
        },
    )

    stack = build_gcp_stack(app, plan, region="us-central1")

    assert stack["resources"]["daily-schedule-scheduler"]["properties"]["schedule"] == ("0 8 * * *")
