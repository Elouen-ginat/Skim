"""Local Docker Compose spec builder."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from skaal.deploy._external import DefaultExternalProvisioner
from skaal.deploy._render import render
from skaal.deploy.backends.local_services import COMPOSE_SERVICES
from skaal.deploy.wiring import resolve_backend

if TYPE_CHECKING:
    from skaal.plan import PlanFile


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


def _gateway_component(plan: "PlanFile") -> Any | None:
    return next(
        (
            component
            for component in plan.components.values()
            if component.kind in ("proxy", "api-gateway")
        ),
        None,
    )


def _gateway_routes(app: Any, gw_comp: Any) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = gw_comp.config.get("routes") or []
    mounts: dict[str, str] = getattr(app, "_mounts", {}) if app is not None else {}
    if not routes and mounts:
        routes = [
            {"path": prefix.rstrip("/") + "/*", "target": ns, "methods": ["GET", "POST"]}
            for ns, prefix in mounts.items()
        ]
    return routes


def build_kong_config(app: Any, plan: "PlanFile", *, app_service_name: str = "app") -> str | None:
    """Build the optional Kong config for a local api-gateway target."""
    gw_comp = _gateway_component(plan)
    if gw_comp is None:
        return None

    implementation = gw_comp.implementation or ("traefik" if gw_comp.kind == "proxy" else "kong")
    if implementation != "kong":
        return None

    routes = _gateway_routes(app, gw_comp)
    if not routes:
        routes = [{"path": "/", "target": "app", "methods": ["GET", "POST"]}]

    return _kong_config(
        routes,
        auth=gw_comp.config.get("auth"),
        rate_limit=gw_comp.config.get("rate_limit"),
        cors_origins=gw_comp.config.get("cors_origins"),
        app_service_name=app_service_name,
    )


def _build_docker_compose(
    plan: "PlanFile",
    port: int,
    source_pkg: str,
    app: Any = None,
    dev: bool = False,
    is_wsgi: bool = False,
    app_service_name: str = "app",
    app_container_name: str = "skaal-app",
) -> str:
    """Build a docker-compose.yml string with the app service and dependencies."""
    services_needed: dict[str, dict[str, Any]] = {}
    service_dependencies: list[str] = []
    env_vars: list[str] = []

    for qname, spec in plan.storage.items():
        class_name = qname.split(".")[-1]
        handler = resolve_backend(spec, target="local")

        if handler.wiring.env_prefix and handler.local_env_value:
            env_var = handler.wiring.env_var(class_name) or ""
            env_vars.append(f"      {env_var}: {handler.local_env_value}")

        if handler.local_service and handler.local_service not in services_needed:
            compose_spec = COMPOSE_SERVICES.get(handler.local_service)
            if compose_spec:
                services_needed[handler.local_service] = compose_spec
            if handler.local_service not in service_dependencies:
                service_dependencies.append(handler.local_service)

    ext_fragment = DefaultExternalProvisioner().compose_fragment(plan)
    if ext_fragment:
        env_vars.append(ext_fragment.rstrip("\n"))

    app_labels = ""
    gw_comp = _gateway_component(plan)
    if gw_comp is not None:
        implementation = gw_comp.implementation or (
            "traefik" if gw_comp.kind == "proxy" else "kong"
        )
        gateway_service = "traefik" if implementation == "traefik" else "kong"
        compose_spec = COMPOSE_SERVICES.get(gateway_service)
        if compose_spec and gateway_service not in services_needed:
            services_needed[gateway_service] = compose_spec
        if gateway_service not in service_dependencies:
            service_dependencies.append(gateway_service)

        routes = _gateway_routes(app, gw_comp)
        if gateway_service == "traefik":
            app_name = getattr(app, "name", "app") if app is not None else "app"
            app_labels = _traefik_labels(routes, app_name)

    additional_services = ""
    if services_needed:
        service_lines: list[str] = []
        for service_name, service_config in services_needed.items():
            service_lines.append(f"  {service_name}:")
            service_lines.append(f"    image: {service_config['image']}")

            if service_config.get("command"):
                service_lines.append("    command:")
                for cmd in service_config["command"]:
                    service_lines.append(f"      - {cmd}")

            if service_config.get("ports"):
                service_lines.append("    ports:")
                for port_mapping in service_config["ports"]:
                    service_lines.append(f"      {port_mapping}")

            if service_config.get("environment"):
                service_lines.append("    environment:")
                for env in service_config["environment"]:
                    service_lines.append(f"      {env}")

            if service_config.get("volumes"):
                service_lines.append("    volumes:")
                for volume in service_config["volumes"]:
                    service_lines.append(f"      {volume}")

            if service_config.get("healthcheck"):
                healthcheck = service_config["healthcheck"]
                service_lines.append("    healthcheck:")
                service_lines.append(f"      test: {healthcheck['test']}")
                service_lines.append(f"      interval: {healthcheck['interval']}")
                service_lines.append(f"      timeout: {healthcheck['timeout']}")
                service_lines.append(f"      retries: {healthcheck['retries']}")
                service_lines.append(f"      start_period: {healthcheck['start_period']}")

            service_lines.append("")
        additional_services = "\n".join(service_lines)

    depends_on_lines = [f"      - {dependency}" for dependency in service_dependencies]
    depends_on_str = "\n".join(depends_on_lines) if depends_on_lines else "      []"
    gunicorn_worker = "" if is_wsgi else " -k uvicorn.workers.UvicornWorker"

    composed = render(
        "local/docker-compose.yml",
        port=str(port),
        source_pkg=source_pkg,
        app_service_name=app_service_name,
        app_container_name=app_container_name,
        service_env_vars="\n".join(env_vars) if env_vars else "      {}",
        service_dependencies=depends_on_str,
        app_labels=app_labels,
        additional_services=additional_services,
        gunicorn_worker=gunicorn_worker,
    )

    if dev:
        composed = composed.replace(
            f"      - ../{source_pkg}:/app/{source_pkg}",
            f"      - ../{source_pkg}:/app/{source_pkg}\n"
            f"      # Mount local skaal source for live library reload (--dev only)\n"
            f"      - ../skaal:/app/skaal",
        )

    return composed
