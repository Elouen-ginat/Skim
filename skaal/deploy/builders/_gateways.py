from __future__ import annotations

from typing import Any, Protocol


def _traefik_labels(routes: list[dict[str, Any]], app_name: str) -> str:
    """Generate docker-compose label lines that wire Traefik routing rules."""
    if not routes:
        return (
            "    labels:\n"
            f'      - "traefik.enable=true"\n'
            f'      - "traefik.http.routers.{app_name}.rule=PathPrefix(`/`)"\n'
            f'      - "traefik.http.services.{app_name}.loadbalancer.server.port=8000"\n'
        )

    label_lines: list[str] = ["    labels:", '      - "traefik.enable=true"']
    for index, route in enumerate(routes):
        path = route["path"].rstrip("*").rstrip("/") or "/"
        router = f"{app_name}-r{index}"
        rule = f"PathPrefix(`{path}`)" if path != "/" else "PathPrefix(`/`)"
        label_lines.append(f'      - "traefik.http.routers.{router}.rule={rule}"')
        label_lines.append(
            f'      - "traefik.http.services.{router}.loadbalancer.server.port=8000"'
        )
    return "\n".join(label_lines) + "\n"


def _kong_config(
    routes: list[dict[str, Any]],
    auth: dict[str, Any] | None,
    rate_limit: dict[str, Any] | None,
    cors_origins: list[str] | None,
    app_service_name: str = "app",
) -> str:
    """Generate a Kong DB-less declarative configuration (kong.yml)."""
    lines: list[str] = [
        '_format_version: "3.0"',
        "_transform: true",
        "",
        "services:",
        "  - name: skaal-app",
        f"    url: http://{app_service_name}:8000",
        "    routes:",
    ]

    for index, route in enumerate(routes):
        path = route["path"].rstrip("*").rstrip("/") or "/"
        methods = route.get("methods") or ["GET", "POST"]
        lines.append(f"      - name: route-{index}")
        lines.append("        paths:")
        lines.append(f"          - {path}")
        lines.append("        methods:")
        for method in methods:
            lines.append(f"          - {method.upper()}")

    has_plugins = auth or rate_limit or cors_origins
    if has_plugins:
        lines.append("")
        lines.append("plugins:")

    if rate_limit:
        requests_per_second = rate_limit.get("requests_per_second", 100)
        lines += [
            "  - name: rate-limiting",
            "    config:",
            f"      minute: {max(1, int(requests_per_second * 60))}",
            "      policy: local",
        ]

    if cors_origins:
        origins_str = ", ".join(f'"{origin}"' for origin in cors_origins)
        lines += [
            "  - name: cors",
            "    config:",
            f"      origins: [{origins_str}]",
            "      methods: [GET, POST, PUT, DELETE, PATCH, OPTIONS]",
            "      headers: [Content-Type, Authorization]",
            "      preflight_continue: false",
        ]

    if auth and auth.get("provider") == "jwt":
        lines += [
            "  - name: jwt",
            "    config:",
            "      uri_param_names: []",
            "      cookie_names: []",
            "      # Configure consumers and JWT credentials separately",
        ]
        if auth.get("issuer"):
            lines.append(f"      # Issuer: {auth['issuer']}")

    return "\n".join(lines) + "\n"


class GatewayAdapter(Protocol):
    name: str
    compose_service: str

    def app_labels(self, routes: list[dict[str, Any]], app_name: str) -> str: ...

    def kong_config(
        self,
        routes: list[dict[str, Any]],
        *,
        auth: dict[str, Any] | None,
        rate_limit: dict[str, Any] | None,
        cors_origins: list[str] | None,
        app_service_name: str,
    ) -> str | None: ...


class _TraefikGateway:
    name = "traefik"
    compose_service = "traefik"

    def app_labels(self, routes: list[dict[str, Any]], app_name: str) -> str:
        return _traefik_labels(routes, app_name)

    def kong_config(
        self,
        routes: list[dict[str, Any]],
        *,
        auth: dict[str, Any] | None,
        rate_limit: dict[str, Any] | None,
        cors_origins: list[str] | None,
        app_service_name: str,
    ) -> str | None:
        return None


class _KongGateway:
    name = "kong"
    compose_service = "kong"

    def app_labels(self, routes: list[dict[str, Any]], app_name: str) -> str:
        return ""

    def kong_config(
        self,
        routes: list[dict[str, Any]],
        *,
        auth: dict[str, Any] | None,
        rate_limit: dict[str, Any] | None,
        cors_origins: list[str] | None,
        app_service_name: str,
    ) -> str | None:
        return _kong_config(
            routes,
            auth=auth,
            rate_limit=rate_limit,
            cors_origins=cors_origins,
            app_service_name=app_service_name,
        )


_ADAPTERS: dict[str, GatewayAdapter] = {
    "traefik": _TraefikGateway(),
    "kong": _KongGateway(),
}


def adapter_for_component(gateway_component: Any) -> GatewayAdapter:
    """Resolve the gateway adapter for a proxy or api-gateway component."""

    implementation = (
        getattr(gateway_component, "implementation", None)
        or gateway_component.config.get("implementation")
        or ("traefik" if gateway_component.kind == "proxy" else "kong")
    )
    adapter = _ADAPTERS.get(implementation)
    if adapter is None:
        raise ValueError(f"unsupported gateway implementation {implementation!r}")
    return adapter
