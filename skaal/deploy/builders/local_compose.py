"""Local Docker Compose spec builder."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from skaal.deploy._external import DefaultExternalProvisioner
from skaal.deploy._render import render
from skaal.deploy.builders._gateways import (
    adapter_for_component,
)
from skaal.deploy.local_services import COMPOSE_SERVICES
from skaal.deploy.wiring import resolve_backend

if TYPE_CHECKING:
    from skaal.plan import PlanFile


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

    adapter = adapter_for_component(gw_comp)
    if adapter.name != "kong":
        return None

    routes = _gateway_routes(app, gw_comp)
    if not routes:
        routes = [{"path": "/", "target": "app", "methods": ["GET", "POST"]}]

    return adapter.kong_config(
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
    bootstrap_module: str = "main",
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
        adapter = adapter_for_component(gw_comp)
        compose_spec = COMPOSE_SERVICES.get(adapter.compose_service)
        if compose_spec and adapter.compose_service not in services_needed:
            services_needed[adapter.compose_service] = compose_spec
        if adapter.compose_service not in service_dependencies:
            service_dependencies.append(adapter.compose_service)

        routes = _gateway_routes(app, gw_comp)
        app_name = getattr(app, "name", "app") if app is not None else "app"
        app_labels = adapter.app_labels(routes, app_name)

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
        bootstrap_module=bootstrap_module,
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
