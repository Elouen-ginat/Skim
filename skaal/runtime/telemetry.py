from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any, cast

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Observation
from opentelemetry.propagate import extract
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_NAMESPACE, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode

from skaal.types import EngineTelemetrySnapshot, ReadinessState, TelemetryConfig

log = logging.getLogger("skaal.runtime.telemetry")


@dataclass(slots=True)
class RequestSpanContext:
    scope: AbstractContextManager[Any]
    span: Any | None
    started_at: float
    trace_id: str | None
    span_id: str | None
    attributes: dict[str, str]


class RuntimeTelemetry:
    def __init__(
        self,
        app_name: str,
        config: TelemetryConfig | None = None,
        *,
        span_exporter: SpanExporter | None = None,
        metric_reader: Any | None = None,
    ) -> None:
        self.app_name = app_name
        self.config = config
        self.enabled = bool(config or span_exporter is not None or metric_reader is not None)
        self.healthy = True
        self.last_error: str | None = None
        self._runtime: Any | None = None

        resource = Resource.create(
            {
                SERVICE_NAME: (config or {}).get("service_name", app_name),
                SERVICE_NAMESPACE: (config or {}).get("service_namespace", "skaal"),
            }
        )

        self._tracer_provider = TracerProvider(resource=resource)
        if span_exporter is not None:
            self._tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        elif config and config.get("endpoint"):
            self._tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=config["endpoint"].rstrip("/") + "/v1/traces",
                        headers=config.get("headers"),
                    )
                )
            )

        readers: list[Any] = []
        if metric_reader is not None:
            readers.append(metric_reader)
        elif config and config.get("endpoint"):
            readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(
                        endpoint=config["endpoint"].rstrip("/") + "/v1/metrics",
                        headers=config.get("headers"),
                    )
                )
            )
        self._meter_provider = MeterProvider(resource=resource, metric_readers=readers)

        self._tracer = self._tracer_provider.get_tracer("skaal.runtime")
        self._meter = self._meter_provider.get_meter("skaal.runtime")
        self._request_counter = self._meter.create_counter("skaal.http.requests")
        self._request_duration = self._meter.create_histogram(
            "skaal.http.request.duration", unit="s"
        )
        self._in_flight = self._meter.create_up_down_counter("skaal.http.in_flight_requests")
        self._auth_counter = self._meter.create_counter("skaal.auth.decisions")

        self._meter.create_observable_gauge(
            "skaal.runtime.ready",
            callbacks=[self._observe_ready],
        )
        self._meter.create_observable_gauge(
            "skaal.engine.running",
            callbacks=[self._observe_engine_running],
        )
        self._meter.create_observable_gauge(
            "skaal.engine.failures",
            callbacks=[self._observe_engine_failures],
        )
        self._meter.create_observable_gauge(
            "skaal.engine.queue_depth",
            callbacks=[self._observe_engine_queue_depth],
        )
        self._meter.create_observable_gauge(
            "skaal.engine.active_tasks",
            callbacks=[self._observe_engine_active_tasks],
        )

    def bind_runtime(self, runtime: Any) -> None:
        self._runtime = runtime

    def request_started(
        self, method: str, path: str, headers: Mapping[str, str]
    ) -> RequestSpanContext:
        attributes = {"http.method": method, "http.route": path, "skaal.app": self.app_name}
        self._in_flight.add(1, attributes)
        if not self.enabled:
            return RequestSpanContext(
                scope=nullcontext(),
                span=None,
                started_at=time.perf_counter(),
                trace_id=None,
                span_id=None,
                attributes=attributes,
            )

        carrier = {name: value for name, value in headers.items()}
        scope = self._tracer.start_as_current_span(
            f"{method} {path}",
            context=extract(carrier),
            kind=SpanKind.SERVER,
            attributes=attributes,
        )
        span = scope.__enter__()
        span_context = span.get_span_context()
        return RequestSpanContext(
            scope=scope,
            span=span,
            started_at=time.perf_counter(),
            trace_id=f"{span_context.trace_id:032x}",
            span_id=f"{span_context.span_id:016x}",
            attributes=attributes,
        )

    def request_finished(
        self,
        request: RequestSpanContext,
        *,
        status_code: int,
        error: Exception | None = None,
    ) -> None:
        duration = time.perf_counter() - request.started_at
        attributes = {**request.attributes, "http.status_code": str(status_code)}
        self._request_counter.add(1, attributes)
        self._request_duration.record(duration, attributes)
        self._in_flight.add(-1, request.attributes)

        if request.span is not None:
            request.span.set_attribute("http.status_code", status_code)
            if error is not None:
                request.span.record_exception(error)
                request.span.set_status(Status(StatusCode.ERROR, str(error)))
            elif status_code >= 500:
                request.span.set_status(Status(StatusCode.ERROR))
            request.scope.__exit__(None, None, None)

    def record_auth_result(self, result: str) -> None:
        self._auth_counter.add(1, {"result": result, "skaal.app": self.app_name})

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "healthy": self.healthy,
            "last_error": self.last_error,
        }

    def shutdown(self) -> None:
        try:
            self._meter_provider.shutdown()
        except Exception as exc:  # noqa: BLE001
            self.healthy = False
            self.last_error = str(exc)
            log.warning("telemetry meter shutdown failed: %s", exc)
        try:
            self._tracer_provider.shutdown()
        except Exception as exc:  # noqa: BLE001
            self.healthy = False
            self.last_error = str(exc)
            log.warning("telemetry tracer shutdown failed: %s", exc)

    def _observe_ready(self, options: Any) -> list[Observation]:  # noqa: ANN401
        if self._runtime is None:
            return []
        state = cast(ReadinessState, getattr(self._runtime, "readiness_state", "starting"))
        return [
            Observation(
                1 if state == "ready" else 0,
                {"skaal.app": self.app_name, "skaal.state": state},
            )
        ]

    def _observe_engine_running(self, options: Any) -> list[Observation]:  # noqa: ANN401
        return self._observe_engine_metric(
            lambda snapshot: 1 if snapshot.get("running", False) else 0,
            "running",
        )

    def _observe_engine_failures(self, options: Any) -> list[Observation]:  # noqa: ANN401
        return self._observe_engine_metric(
            lambda snapshot: int(snapshot.get("failures", 0)), "failures"
        )

    def _observe_engine_queue_depth(self, options: Any) -> list[Observation]:  # noqa: ANN401
        return self._observe_engine_metric(
            lambda snapshot: snapshot.get("queue_depth"),
            "queue_depth",
        )

    def _observe_engine_active_tasks(self, options: Any) -> list[Observation]:  # noqa: ANN401
        return self._observe_engine_metric(
            lambda snapshot: snapshot.get("active_tasks"),
            "active_tasks",
        )

    def _observe_engine_metric(
        self,
        value_getter: Callable[[EngineTelemetrySnapshot], Any],
        attribute_name: str,
    ) -> list[Observation]:
        if self._runtime is None:
            return []

        observations: list[Observation] = []
        for engine in getattr(self._runtime, "_engines", []):
            snapshot_fn = getattr(engine, "snapshot_telemetry", None)
            if not callable(snapshot_fn):
                continue
            snapshot = cast(EngineTelemetrySnapshot, snapshot_fn())
            value = value_getter(snapshot)
            if value is None:
                continue
            observations.append(
                Observation(
                    int(value),
                    {
                        "skaal.app": self.app_name,
                        "skaal.engine": type(engine).__name__,
                        "skaal.metric": attribute_name,
                    },
                )
            )
        return observations


def resolve_telemetry_config(
    app: Any, override: TelemetryConfig | None = None
) -> TelemetryConfig | None:
    if override is not None:
        return _normalize_telemetry_config(app.name, override)

    configs: list[TelemetryConfig] = []
    for component in getattr(app, "_components", {}).values():
        if getattr(component, "_skaal_component_kind", None) != "external-observability":
            continue
        provider = str(getattr(component, "provider", "")).lower().strip()
        if provider not in {"otlp", "otel", "signoz"}:
            continue
        endpoint_env = getattr(component, "connection_env", None)
        endpoint = getattr(component, "connection_string", None)
        if endpoint_env:
            endpoint = os.environ.get(endpoint_env, endpoint)
        headers = _parse_otlp_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS"))
        config: TelemetryConfig = {
            "exporter": "otlp",
            "service_name": app.name,
            "service_namespace": "skaal",
        }
        if endpoint:
            config["endpoint"] = endpoint
            config["insecure"] = endpoint.startswith("http://")
        if headers:
            config["headers"] = headers
        if config not in configs:
            configs.append(config)

    if not configs:
        return None
    if len(configs) > 1:
        raise ValueError(
            "Conflicting external observability components are attached to this app. "
            "Use one effective OTLP/SigNoz config."
        )
    return configs[0]


def _normalize_telemetry_config(app_name: str, config: TelemetryConfig) -> TelemetryConfig:
    normalized: TelemetryConfig = {
        "exporter": "otlp",
        "service_name": config.get("service_name", app_name),
        "service_namespace": config.get("service_namespace", "skaal"),
        "insecure": bool(config.get("insecure", False)),
    }
    endpoint = config.get("endpoint")
    if endpoint:
        normalized["endpoint"] = endpoint
        normalized["insecure"] = bool(config.get("insecure", endpoint.startswith("http://")))
    headers = config.get("headers")
    if headers:
        normalized["headers"] = dict(headers)
    return normalized


def _parse_otlp_headers(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    headers: dict[str, str] = {}
    for part in value.split(","):
        name, _, header_value = part.partition("=")
        if name and header_value:
            headers[name.strip()] = header_value.strip()
    return headers
