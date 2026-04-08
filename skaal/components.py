"""
Skaal external and provisioned components.

Components are infrastructure declared at **module level** (not inside an App)
and wired in via ``app.attach(component)``. They appear in ``plan.skaal.lock``
and are handled by the Pulumi backend in Phase 3.

Two categories:
- ``ProvisionedComponent`` — the solver selects an implementation; Pulumi provisions it.
- ``ExternalComponent``    — pre-existing infrastructure; Pulumi only configures
                             connectivity (network rules, secrets injection).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from skaal.types import AccessPattern, Durability, Latency, RateLimitPolicy, Throughput

if TYPE_CHECKING:
    from skaal.schedule import Cron, Every

# ── Base classes ──────────────────────────────────────────────────────────


class ComponentBase:
    """
    Abstract base for all Skaal components.

    Subclasses set ``_skim_component_kind`` as a class variable.
    On instantiation they populate ``__skim_component__`` — the metadata
    dict consumed by the solver and Pulumi backend (mirrors ``__skim_storage__``).
    """

    _skim_component_kind: ClassVar[str] = "base"

    def __init__(self, name: str) -> None:
        self.name = name
        self.__skim_component__: dict[str, Any] = {
            "kind": self._skim_component_kind,
            "name": name,
        }

    def describe(self) -> dict[str, Any]:
        """Return the component's spec dict. Used by the solver and plan writer."""
        return dict(self.__skim_component__)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r})"


class ProvisionedComponent(ComponentBase):
    """
    A component that Skaal provisions via Pulumi.

    The solver selects a concrete implementation from the infrastructure
    catalog (e.g. chooses Traefik vs Nginx based on constraints and deploy
    target) unless ``implementation`` is pinned explicitly.
    """


class ExternalComponent(ComponentBase):
    """
    A pre-existing infrastructure component that Skaal did NOT provision.

    Skaal does NOT create external components. Instead it:

    1. Writes their spec into ``plan.skaal.lock`` as a reference.
    2. Injects connection info into the app runtime via env vars.
    3. Lets the solver account for their declared latency in co-location decisions.

    Connection info is supplied via ``connection_string`` (literal, not recommended
    for production) or ``connection_env`` (name of the env var / Secrets Manager
    key that holds the DSN at deploy time).
    """

    def __init__(
        self,
        name: str,
        *,
        connection_string: str | None = None,
        connection_env: str | None = None,
        latency: Latency | str | None = None,
        region: str | None = None,
    ) -> None:
        super().__init__(name)
        self.connection_string = connection_string
        self.connection_env = connection_env
        self.latency = Latency(latency) if isinstance(latency, str) else latency
        self.region = region
        self.__skim_component__.update(
            {
                "external": True,
                "connection_env": connection_env,
                "latency_ms": self.latency.ms if self.latency else None,
                "region": region,
            }
        )


# ── Provisioned components ────────────────────────────────────────────────


@dataclass
class Route:
    """
    A single routing rule for a ``Proxy`` or ``APIGateway``.

    ``target`` is the **string name** of a registered ``@app.function`` or a
    literal external URL (e.g. ``"https://cdn.example.com"``). Using a name
    rather than a direct object reference avoids circular imports and lets
    the deploy engine resolve the endpoint URL after provisioning.
    """

    path: str
    target: str  # @app.function name or external URL
    methods: list[str] = field(default_factory=lambda: ["GET", "POST"])
    strip_prefix: bool = False
    timeout_ms: int | None = None
    rewrite: str | None = None  # path rewrite template


@dataclass
class AuthConfig:
    """Authentication / authorisation configuration for an ``APIGateway``."""

    provider: str  # "jwt" | "oauth2" | "api-key" | "mtls"
    issuer: str | None = None  # token issuer URL (JWT / OAuth2)
    audience: str | None = None
    header: str = "Authorization"
    required: bool = True


class Proxy(ProvisionedComponent):
    """
    A reverse proxy provisioned by Skaal.

    The solver selects the implementation (Traefik, Nginx, Envoy, AWS ALB, …)
    based on the declared throughput/latency constraints and the ``@deploy``
    target, unless ``implementation`` is pinned.

    Usage::

        proxy = Proxy(
            "edge",
            routes=[
                Route("/api/*", target="handle_request", strip_prefix=True),
                Route("/health", target="health_check", methods=["GET"]),
            ],
            tls=True,
            throughput="> 10000 req/s",
        )
        app.attach(proxy)
    """

    _skim_component_kind = "proxy"

    def __init__(
        self,
        name: str,
        routes: list[Route],
        *,
        tls: bool = True,
        latency: Latency | str | None = None,
        throughput: Throughput | str | None = None,
        health_check_path: str = "/healthz",
        implementation: str | None = None,  # None = solver selects
    ) -> None:
        super().__init__(name)
        self.routes = routes
        self.tls = tls
        self.latency = Latency(latency) if isinstance(latency, str) else latency
        self.throughput = Throughput(throughput) if isinstance(throughput, str) else throughput
        self.health_check_path = health_check_path
        self.implementation = implementation
        self.__skim_component__.update(
            {
                "tls": tls,
                "routes": [
                    {"path": r.path, "target": r.target, "methods": r.methods} for r in routes
                ],
                "latency_ms": self.latency.ms if self.latency else None,
                "health_check_path": health_check_path,
                "implementation": implementation,
            }
        )


class APIGateway(ProvisionedComponent):
    """
    A full API gateway with auth, rate limiting, CORS, and routing.

    The solver selects the implementation (Kong, AWS API Gateway, Traefik, …)
    based on constraints and deploy target.

    Usage::

        gw = APIGateway(
            "public-api",
            routes=[Route("/v1/orders", target="place_order")],
            auth=AuthConfig(provider="jwt", issuer="https://auth.example.com"),
            rate_limit=RateLimitPolicy(requests_per_second=100, scope="per-client"),
            throughput="> 5000 req/s",
        )
        app.attach(gw)
    """

    _skim_component_kind = "api-gateway"

    def __init__(
        self,
        name: str,
        routes: list[Route],
        *,
        auth: AuthConfig | None = None,
        rate_limit: RateLimitPolicy | None = None,
        latency: Latency | str | None = None,
        throughput: Throughput | str | None = None,
        cors_origins: list[str] | None = None,
        implementation: str | None = None,
    ) -> None:
        super().__init__(name)
        self.routes = routes
        self.auth = auth
        self.rate_limit = rate_limit
        self.latency = Latency(latency) if isinstance(latency, str) else latency
        self.throughput = Throughput(throughput) if isinstance(throughput, str) else throughput
        self.cors_origins = cors_origins
        self.implementation = implementation
        self.__skim_component__.update(
            {
                "routes": [
                    {
                        "path": r.path,
                        "target": r.target,
                        "methods": r.methods,
                        "strip_prefix": r.strip_prefix,
                        "timeout_ms": r.timeout_ms,
                        "rewrite": r.rewrite,
                    }
                    for r in routes
                ],
                "auth": {
                    "provider": auth.provider,
                    "issuer": auth.issuer,
                    "audience": auth.audience,
                    "header": auth.header,
                    "required": auth.required,
                }
                if auth
                else None,
                "rate_limit": {
                    "requests_per_second": rate_limit.requests_per_second,
                    "burst": rate_limit.burst,
                    "scope": rate_limit.scope,
                }
                if rate_limit
                else None,
                "cors_origins": cors_origins,
                "implementation": implementation,
            }
        )


# ── External components ───────────────────────────────────────────────────


class ExternalStorage(ExternalComponent):
    """
    Reference to a pre-existing database or object store.

    Skaal does not provision this — it only injects connectivity. Declaring
    ``latency`` allows the solver to reason about co-location (e.g. keep a
    function on the same rack as the external DB).

    Usage::

        legacy_db = ExternalStorage(
            "legacy-postgres",
            connection_env="LEGACY_DATABASE_URL",
            access_pattern="transactional",
            latency="< 20ms",
        )
        app.attach(legacy_db)
    """

    _skim_component_kind = "external-storage"

    def __init__(
        self,
        name: str,
        *,
        access_pattern: AccessPattern | str = AccessPattern.TRANSACTIONAL,
        durability: Durability | str = Durability.PERSISTENT,
        connection_string: str | None = None,
        connection_env: str | None = None,
        latency: Latency | str | None = None,
        region: str | None = None,
    ) -> None:
        super().__init__(
            name,
            connection_string=connection_string,
            connection_env=connection_env,
            latency=latency,
            region=region,
        )
        self.access_pattern = (
            AccessPattern(access_pattern) if isinstance(access_pattern, str) else access_pattern
        )
        self.durability = Durability(durability) if isinstance(durability, str) else durability
        self.__skim_component__.update(
            {
                "access_pattern": self.access_pattern,
                "durability": self.durability,
            }
        )


class ExternalQueue(ExternalComponent):
    """
    Reference to a pre-existing message broker or queue (Kafka, RabbitMQ, …).

    Usage::

        kafka = ExternalQueue(
            "company-kafka",
            connection_env="KAFKA_BOOTSTRAP_SERVERS",
            throughput="> 50000 events/s",
        )
        app.attach(kafka)
    """

    _skim_component_kind = "external-queue"

    def __init__(
        self,
        name: str,
        *,
        throughput: Throughput | str | None = None,
        connection_string: str | None = None,
        connection_env: str | None = None,
        region: str | None = None,
    ) -> None:
        super().__init__(
            name,
            connection_string=connection_string,
            connection_env=connection_env,
            region=region,
        )
        self.throughput = Throughput(throughput) if isinstance(throughput, str) else throughput
        self.__skim_component__.update(
            {
                "throughput": str(self.throughput) if self.throughput else None,
            }
        )


class ExternalObservability(ExternalComponent):
    """
    Reference to an external observability stack.

    The Skaal runtime emits metrics and traces to this endpoint when declared.

    Usage::

        prom = ExternalObservability(
            "prometheus",
            provider="prometheus",
            endpoint_env="PROMETHEUS_PUSHGATEWAY_URL",
        )
        app.attach(prom)
    """

    _skim_component_kind = "external-observability"

    def __init__(
        self,
        name: str,
        provider: str,  # "prometheus" | "grafana" | "datadog" | "otel"
        *,
        endpoint: str | None = None,
        endpoint_env: str | None = None,
    ) -> None:
        super().__init__(
            name,
            connection_string=endpoint,
            connection_env=endpoint_env,
        )
        self.provider = provider
        self.__skim_component__.update({"provider": provider})


# ── Schedule trigger ──────────────────────────────────────────────────────────


class ScheduleTrigger(ProvisionedComponent):
    """
    A cloud-native schedule trigger provisioned by Skaal.

    Maps to **AWS EventBridge** (rule + Lambda permission), **GCP Cloud
    Scheduler** (job → Cloud Run), or **APScheduler** for local development.

    Created automatically by ``@app.schedule()``; attach manually only for
    advanced configuration.

    Usage (manual)::

        trigger = ScheduleTrigger(
            "daily-report",
            trigger=Cron(expression="0 8 * * *"),
            target_function="generate_report",
            timezone="America/New_York",
        )
        app.attach(trigger)
    """

    _skim_component_kind = "schedule-trigger"

    def __init__(
        self,
        name: str,
        *,
        trigger: "Every | Cron",
        target_function: str,
        timezone: str = "UTC",
        emit_to: str | None = None,
    ) -> None:
        super().__init__(name)
        self.trigger = trigger
        self.target_function = target_function
        self.timezone = timezone
        self.emit_to = emit_to
        self.__skim_component__.update(
            {
                "trigger": trigger.model_dump(),
                "trigger_type": type(trigger).__name__.lower(),  # "every" | "cron"
                "target_function": target_function,
                "timezone": timezone,
                "emit_to": emit_to,
            }
        )


# ── App reference ─────────────────────────────────────────────────────────────


class AppRef(ExternalComponent):
    """
    Reference to another deployed Skaal app.

    Calls functions on a remote Skaal app via HTTP/HTTPS POST using
    ``httpx``.  The base URL is resolved from ``base_url`` (literal) or
    ``base_url_env`` (name of the environment variable that holds the URL at
    runtime).

    Usage::

        payments = AppRef("payments", base_url_env="PAYMENTS_SERVICE_URL")
        app.attach(payments)

        result = await payments.charge(amount=100, currency="USD")
        # or explicitly:
        result = await payments.call("charge", amount=100, currency="USD")
    """

    _skim_component_kind = "app-ref"

    def __init__(
        self,
        name: str,
        *,
        base_url: str | None = None,
        base_url_env: str | None = None,
        timeout_ms: int = 30_000,
    ) -> None:
        super().__init__(name, connection_string=base_url, connection_env=base_url_env)
        self.timeout_ms = timeout_ms
        self.__skim_component__["timeout_ms"] = timeout_ms

    def _resolve_base_url(self) -> str:
        if self.connection_string:
            return self.connection_string.rstrip("/")
        if self.connection_env:
            url = os.environ.get(self.connection_env)
            if url:
                return url.rstrip("/")
        raise RuntimeError(
            f"AppRef {self.name!r}: set base_url= or the {self.connection_env!r} env var."
        )

    async def call(self, fn_name: str, **kwargs: Any) -> Any:
        """Call a function on the remote Skaal app via ``POST /{fn_name}``."""
        import httpx

        url = f"{self._resolve_base_url()}/{fn_name}"
        timeout = httpx.Timeout(self.timeout_ms / 1000)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=kwargs)
        if resp.status_code >= 400:
            data = resp.json()
            raise RuntimeError(
                f"AppRef {self.name!r} → {fn_name!r} [{resp.status_code}]: "
                f"{data.get('error', data)}"
            )
        return resp.json()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        async def _proxy(**kwargs: Any) -> Any:
            return await self.call(name, **kwargs)

        return _proxy
