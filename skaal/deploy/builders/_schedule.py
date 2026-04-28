from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import TypeAdapter

from skaal.schedule import Schedule

_schedule_adapter: TypeAdapter[Schedule] = TypeAdapter(Schedule)


def _load_schedule(config: Mapping[str, Any]) -> Schedule:
    """Validate a schedule-trigger config payload into a typed schedule model."""

    return _schedule_adapter.validate_python(config["trigger"])
