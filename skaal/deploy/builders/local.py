"""Local Docker Pulumi stack builder."""

from __future__ import annotations

import platform
from pathlib import Path
from typing import TYPE_CHECKING, Any

from skaal.deploy.backends import LOCAL_SERVICE_SPECS, get_handler
from skaal.deploy.builders.common import resource_slug
from skaal.deploy.config import LocalStackDeployConfig
from skaal.deploy.secrets import LocalSecretInjector
from skaal.types import (
    AppLike,
    DockerContainerProperties,
    DockerLabel,
    DockerNetworkAttachment,
    DockerPortBinding,
    DockerVolumeMount,
    LocalServiceSpec,
    PulumiResource,
    PulumiResourceOptions,
    PulumiStack,
)

if TYPE_CHECKING:
    from skaal.plan import PlanFile


def local_image_name(app_name: str) -> str:
    return f"skaal-{resource_slug(app_name)}:local"


def _use_health_waits() -> bool:
    return platform.system() != "Windows"


def _use_primary_network_mode() -> bool:
    return platform.system() == "Windows"


def _container_name(app_slug: str, logical_name: str) -> str:
    return f"skaal-{app_slug}-{logical_name}"


def _service_host(app_slug: str, service_name: str) -> str:
    if _use_primary_network_mode():
        return _container_name(app_slug, service_name)
    return service_name


def _gateway_component(plan: "PlanFile") -> Any | None:
    return next(
        (
            component
            for component in plan.components.values()
            if component.kind in ("proxy", "api-gateway")
        ),
        None,
    )


def _gateway_routes(app: AppLike | None, gateway_component: Any) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = gateway_component.config.get("routes") or []
    mounts: dict[str, str] = getattr(app, "_mounts", {}) if app is not None else {}
    if not routes and mounts:
        routes = [
            {"path": prefix.rstrip("/") + "/*", "target": namespace, "methods": ["GET", "POST"]}
            for namespace, prefix in mounts.items()
        ]
    return routes


def _traefik_labels(routes: list[dict[str, Any]], app_name: str) -> list[DockerLabel]:
    labels: list[DockerLabel] = [{"label": "traefik.enable", "value": "true"}]
    if not routes:
        return labels + [
            {"label": f"traefik.http.routers.{app_name}.rule", "value": "PathPrefix(`/`)"},
            {
                "label": f"traefik.http.services.{app_name}.loadbalancer.server.port",
                "value": "8000",
            },
        ]

    for index, route in enumerate(routes):
        path = route["path"].rstrip("*").rstrip("/") or "/"
        router = f"{app_name}-r{index}"
        rule = f"PathPrefix(`{path}`)" if path != "/" else "PathPrefix(`/`)"
        labels.append({"label": f"traefik.http.routers.{router}.rule", "value": rule})
        labels.append(
            {
                "label": f"traefik.http.services.{router}.loadbalancer.server.port",
                "value": "8000",
            }
        )
    return labels


def build_kong_config(
    app: AppLike,
    plan: "PlanFile",
    *,
    app_service_name: str = "app",
) -> str | None:
    gateway_component = _gateway_component(plan)
    if gateway_component is None:
        return None

    implementation = gateway_component.implementation or (
        "traefik" if gateway_component.kind == "proxy" else "kong"
    )
    if implementation != "kong":
        return None

    routes = _gateway_routes(app, gateway_component)
    if not routes:
        routes = [{"path": "/", "target": "app", "methods": ["GET", "POST"]}]

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

    auth = gateway_component.config.get("auth")
    rate_limit = gateway_component.config.get("rate_limit")
    cors_origins = gateway_component.config.get("cors_origins")
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
        origins = ", ".join(f'"{origin}"' for origin in cors_origins)
        auth_header = "Authorization"
        if auth:
            auth_header = str(auth.get("header") or "Authorization")
        lines += [
            "  - name: cors",
            "    config:",
            f"      origins: [{origins}]",
            "      methods: [GET, POST, PUT, DELETE, PATCH, OPTIONS]",
            f"      headers: [Content-Type, Authorization, {auth_header}]",
            "      preflight_continue: false",
        ]

    if auth and auth.get("provider") == "jwt" and auth.get("required", True):
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


def _depends_on(*resource_names: str) -> PulumiResourceOptions:
    deps = [f"${{{resource_name}}}" for resource_name in resource_names if resource_name]
    return {"dependsOn": deps} if deps else {}


def _network_attachment(alias: str) -> DockerNetworkAttachment:
    return {"name": "${skaal-net.name}", "aliases": [alias]}


def _app_command(is_wsgi: bool) -> list[str]:
    command = [
        "uv",
        "run",
        "gunicorn",
        "--bind",
        "0.0.0.0:8000",
        "--workers",
        "1",
        "--timeout",
        "120",
    ]
    if not is_wsgi:
        command += ["-k", "uvicorn.workers.UvicornWorker"]
    command += ["--reload", "main:application"]
    return command


def _app_envs(plan: "PlanFile", *, app_slug: str) -> tuple[list[str], list[str]]:
    envs: dict[str, str] = {}
    service_dependencies: list[str] = []

    for qualified_name, spec in plan.storage.items():
        class_name = qualified_name.split(".")[-1]
        handler = get_handler(spec, local=True)

        if handler.env_prefix and handler.local_env_value:
            env_var = f"{handler.env_prefix}_{class_name.upper()}"
            value = handler.local_env_value
            if handler.local_service:
                value = value.replace(
                    f"://{handler.local_service}:",
                    f"://{_service_host(app_slug, handler.local_service)}:",
                )
            envs[env_var] = value

        if handler.local_service and handler.local_service not in service_dependencies:
            service_dependencies.append(handler.local_service)

    for env_name, value in LocalSecretInjector().env_vars(plan).items():
        envs[env_name] = value

    return [f"{name}={value}" for name, value in sorted(envs.items())], service_dependencies


def _app_volumes(output_dir: Path, source_module: str, dev: bool) -> list[DockerVolumeMount]:
    project_root = output_dir.parent.resolve()
    top_package = source_module.split(".")[0]
    source_path = project_root / top_package
    if not source_path.exists():
        source_path = output_dir / top_package

    volumes: list[DockerVolumeMount] = [
        {"containerPath": "/app/data", "volumeName": "${skaal-data.name}"}
    ]
    if source_path.exists():
        volumes.insert(
            0, {"containerPath": f"/app/{top_package}", "hostPath": str(source_path.resolve())}
        )
    if dev and (project_root / "skaal").is_dir():
        volumes.append(
            {"containerPath": "/app/skaal", "hostPath": str((project_root / "skaal").resolve())}
        )
    return volumes


def _service_container_resource(
    service_name: str,
    *,
    app_slug: str,
    spec: LocalServiceSpec,
    extra_volumes: list[DockerVolumeMount] | None = None,
    depends_on: list[str] | None = None,
) -> PulumiResource:
    properties: DockerContainerProperties = {
        "name": _container_name(app_slug, service_name),
        "image": spec["image"],
    }
    if _use_primary_network_mode():
        properties["networkMode"] = "${skaal-net.name}"
    else:
        properties["networksAdvanced"] = [_network_attachment(service_name)]
    if spec.get("command"):
        properties["command"] = list(spec["command"])
    if spec.get("envs"):
        properties["envs"] = list(spec["envs"])
    healthcheck = spec.get("healthcheck")
    if healthcheck:
        properties["healthcheck"] = healthcheck
        if _use_health_waits():
            properties["wait"] = True
            properties["waitTimeout"] = 120
    if spec.get("labels"):
        properties["labels"] = list(spec["labels"])
    if spec.get("ports"):
        properties["ports"] = list(spec["ports"])
    volumes = list(spec.get("volumes") or [])
    if extra_volumes:
        volumes.extend(extra_volumes)
    if volumes:
        properties["volumes"] = volumes

    resource: PulumiResource = {"type": "docker:Container", "properties": properties}
    options = _depends_on("skaal-net", *(depends_on or []))
    if options:
        resource["options"] = options
    return resource


def build_pulumi_stack(
    app: AppLike,
    plan: "PlanFile",
    *,
    output_dir: Path,
    source_module: str,
    dev: bool = False,
) -> PulumiStack:
    deploy = LocalStackDeployConfig.model_validate(plan.deploy_config)
    app_slug = resource_slug(app.name)
    app_envs, storage_services = _app_envs(plan, app_slug=app_slug)

    resources: dict[str, PulumiResource] = {
        "skaal-net": {"type": "docker:Network", "properties": {"name": f"skaal-{app_slug}-net"}},
        "skaal-data": {"type": "docker:Volume", "properties": {"name": f"skaal-{app_slug}-data"}},
    }

    gateway = _gateway_component(plan)
    gateway_service: str | None = None
    app_labels: list[DockerLabel] = []
    if gateway is not None:
        gateway_service = gateway.implementation or (
            "traefik" if gateway.kind == "proxy" else "kong"
        )
        routes = _gateway_routes(app, gateway)
        if gateway_service == "traefik":
            app_labels = _traefik_labels(routes, app_slug)

    for service_name in storage_services:
        resources[service_name] = _service_container_resource(
            service_name,
            app_slug=app_slug,
            spec=LOCAL_SERVICE_SPECS[service_name],
        )

    if gateway_service is not None:
        extra_volumes: list[DockerVolumeMount] | None = None
        if gateway_service == "kong":
            extra_volumes = [
                {
                    "containerPath": "/kong/config.yml",
                    "hostPath": str((output_dir / "kong.yml").resolve()),
                    "readOnly": True,
                }
            ]
        resources[gateway_service] = _service_container_resource(
            gateway_service,
            app_slug=app_slug,
            spec=LOCAL_SERVICE_SPECS[gateway_service],
            extra_volumes=extra_volumes,
            depends_on=["app"],
        )

    app_properties: DockerContainerProperties = {
        "name": _container_name(app_slug, "app"),
        "image": "${localImageRef}",
        "command": _app_command(bool(getattr(app, "_wsgi_attribute", None))),
        "envs": app_envs,
        "ports": [DockerPortBinding(internal=8000, external=deploy.port)],
        "volumes": _app_volumes(output_dir, source_module, dev),
    }
    if _use_primary_network_mode():
        app_properties["networkMode"] = "${skaal-net.name}"
    else:
        app_properties["networksAdvanced"] = [_network_attachment("app")]
    if app_labels:
        app_properties["labels"] = app_labels

    resources["app"] = {
        "type": "docker:Container",
        "properties": app_properties,
        "options": _depends_on("skaal-data", "skaal-net", *storage_services),
    }

    return {
        "name": f"skaal-{app_slug}",
        "runtime": "yaml",
        "config": {"localImageRef": {"type": "string", "default": local_image_name(app.name)}},
        "resources": resources,
        "outputs": {"appUrl": f"http://localhost:{deploy.port}"},
    }
