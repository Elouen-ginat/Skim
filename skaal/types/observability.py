from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypeAlias, TypedDict

TelemetryExporter: TypeAlias = Literal["otlp"]
ReadinessState: TypeAlias = Literal["starting", "ready", "degraded", "stopped"]


class TelemetryConfig(TypedDict, total=False):
    exporter: TelemetryExporter
    endpoint: str
    service_name: str
    service_namespace: str
    headers: dict[str, str]
    insecure: bool


class EngineTelemetrySnapshot(TypedDict, total=False):
    running: bool
    failures: int
    queue_depth: int
    active_tasks: int


HeaderMap: TypeAlias = Mapping[str, str]
