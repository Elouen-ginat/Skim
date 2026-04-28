"""Local Pulumi artifact generator backed by the Docker provider."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from skaal.deploy._backends import _LOCAL_SERVICE_SPECS, build_wiring, get_handler
from skaal.deploy._deps import collect_user_packages
from skaal.deploy._external import DefaultExternalProvisioner
from skaal.deploy._render import render, to_pulumi_yaml, to_pyproject_toml
from skaal.deploy.config import LocalStackDeployConfig
from skaal.deploy.local_automation import write_local_stack_spec
from skaal.deploy.push import write_meta
from skaal.types.deploy import (
    DockerContainerProperties,
    DockerLabel,
    DockerNetworkAttachment,
    DockerPortBinding,
    DockerVolumeMount,
    LocalServiceSpec,
    PulumiResource,
    PulumiStack,
)

if TYPE_CHECKING:
    from skaal.plan import PlanFile


_LOCAL_GITIGNORE = (
    ".pulumi/\n" ".pulumi-state/\n" "__pycache__/\n" ".pytest_cache/\n" ".env\n" ".env.local\n"
)
_LOCAL_DOCKERIGNORE = ".pulumi/\n.pulumi-state/\n__pycache__/\n.pytest_cache/\n"


def _resource_slug(name: str, *, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        slug = "skaal"
    if not slug[0].isalpha():
        slug = f"skaal-{slug}"
    return slug[:max_len].rstrip("-") or "skaal"


def local_image_name(app_name: str) -> str:
    return f"skaal-{_resource_slug(app_name)}:local"


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


def build_kong_config(app: Any, plan: "PlanFile", *, app_service_name: str = "app") -> str | None:
    """Build the optional Kong config for a local api-gateway target."""
    gw_comp = _gateway_component(plan)
    if gw_comp is None:
        return None

    impl = gw_comp.implementation or ("traefik" if gw_comp.kind == "proxy" else "kong")
    if impl != "kong":
        return None

    routes = _gateway_routes(app, gw_comp)
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

    auth = gw_comp.config.get("auth")
    rate_limit = gw_comp.config.get("rate_limit")
    cors_origins = gw_comp.config.get("cors_origins")
    has_plugins = auth or rate_limit or cors_origins
    if has_plugins:
        lines.append("")
        lines.append("plugins:")

    if rate_limit:
        rps = rate_limit.get("requests_per_second", 100)
        lines += [
            "  - name: rate-limiting",
            "    config:",
            f"      minute: {max(1, int(rps * 60))}",
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


def _depends_on(*resource_names: str) -> dict[str, list[str]]:
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


def _app_envs(plan: "PlanFile") -> tuple[list[str], list[str]]:
    envs: dict[str, str] = {}
    service_dependencies: list[str] = []

    for qname, spec in plan.storage.items():
        class_name = qname.split(".")[-1]
        handler = get_handler(spec, local=True)

        if handler.env_prefix and handler.local_env_value:
            env_var = f"{handler.env_prefix}_{class_name.upper()}"
            envs[env_var] = handler.local_env_value

        if handler.local_service and handler.local_service not in service_dependencies:
            service_dependencies.append(handler.local_service)

    for env_name, value in DefaultExternalProvisioner().env_vars(plan).items():
        envs[env_name] = value

    return [f"{name}={value}" for name, value in sorted(envs.items())], service_dependencies


def _app_volumes(output_dir: Path, source_module: str, dev: bool) -> list[DockerVolumeMount]:
    project_root = output_dir.parent.resolve()
    top_pkg = source_module.split(".")[0]
    source_path = project_root / top_pkg
    if not source_path.exists():
        source_path = output_dir / top_pkg

    volumes: list[DockerVolumeMount] = [
        {"containerPath": "/app/data", "volumeName": "${skaal-data.name}"}
    ]
    if source_path.exists():
        volumes.insert(
            0, {"containerPath": f"/app/{top_pkg}", "hostPath": str(source_path.resolve())}
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
        "name": f"skaal-{app_slug}-{service_name}",
        "image": spec["image"],
        "networksAdvanced": [_network_attachment(service_name)],
    }
    if spec.get("command"):
        properties["command"] = list(spec["command"])
    if spec.get("envs"):
        properties["envs"] = list(spec["envs"])
    if spec.get("healthcheck"):
        properties["healthcheck"] = dict(spec["healthcheck"])
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

    resource: PulumiResource = {
        "type": "docker:Container",
        "properties": properties,
    }
    options = _depends_on("skaal-net", *(depends_on or []))
    if options:
        resource["options"] = options
    return resource


def _build_pulumi_stack(
    app: Any,
    plan: "PlanFile",
    *,
    output_dir: Path,
    source_module: str,
    dev: bool = False,
) -> PulumiStack:
    deploy = LocalStackDeployConfig.model_validate(plan.deploy_config)
    app_slug = _resource_slug(app.name)
    app_envs, storage_services = _app_envs(plan)

    resources: dict[str, PulumiResource] = {
        "skaal-net": {
            "type": "docker:Network",
            "properties": {"name": f"skaal-{app_slug}-net"},
        },
        "skaal-data": {
            "type": "docker:Volume",
            "properties": {"name": f"skaal-{app_slug}-data"},
        },
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
            spec=_LOCAL_SERVICE_SPECS[service_name],
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
            spec=_LOCAL_SERVICE_SPECS[gateway_service],
            extra_volumes=extra_volumes,
            depends_on=["app"],
        )

    app_properties: DockerContainerProperties = {
        "name": f"skaal-{app_slug}-app",
        "image": "${localImageRef}",
        "command": _app_command(bool(getattr(app, "_wsgi_attribute", None))),
        "envs": app_envs,
        "networksAdvanced": [_network_attachment("app")],
        "ports": [DockerPortBinding(internal=8000, external=deploy.port)],
        "volumes": _app_volumes(output_dir, source_module, dev),
    }
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


def generate_artifacts(
    app: Any,
    plan: "PlanFile",
    output_dir: Path,
    source_module: str,
    app_var: str = "app",
    dev: bool = False,
) -> list[Path]:
    """Generate local Docker deployment artifacts backed by Pulumi YAML."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    top_pkg = source_module.split(".")[0]
    project_root = output_dir.parent

    generated: list[Path] = []
    backend_imports, backend_overrides = build_wiring(plan, local=True)
    wsgi_attribute: str | None = getattr(app, "_wsgi_attribute", None)
    is_wsgi = bool(wsgi_attribute)

    main_path = output_dir / "main.py"
    if wsgi_attribute:
        main_path.write_text(
            render(
                "local/main_wsgi.py",
                source_module=source_module,
                app_var=app_var,
                wsgi_attribute=wsgi_attribute,
                backend_imports=backend_imports,
                backend_overrides=backend_overrides,
            ),
            encoding="utf-8",
        )
    else:
        main_path.write_text(
            render(
                "local/main.py",
                source_module=source_module,
                app_var=app_var,
                backend_imports=backend_imports,
                backend_overrides=backend_overrides,
            ),
            encoding="utf-8",
        )
    generated.append(main_path)

    skaal_bundle_dir = output_dir / "_skaal"
    skaal_src_dir = project_root / "skaal"
    skaal_root_pyproject = project_root / "pyproject.toml"
    if dev and skaal_src_dir.is_dir() and skaal_root_pyproject.exists():
        skaal_bundle_dir.mkdir(exist_ok=True)
        shutil.copytree(skaal_src_dir, skaal_bundle_dir / "skaal", dirs_exist_ok=True)
        raw = skaal_root_pyproject.read_text(encoding="utf-8")
        raw = raw.replace('path = "mesh"', 'path = "../mesh"')
        (skaal_bundle_dir / "pyproject.toml").write_text(raw, encoding="utf-8")
        for extra in ("LICENSE", "README.md"):
            src = project_root / extra
            if src.exists():
                shutil.copy2(src, skaal_bundle_dir / extra)
        generated.append(skaal_bundle_dir)

    mesh_bundle_dir = output_dir / "mesh"
    mesh_src_dir = project_root / "mesh"
    has_mesh = mesh_src_dir.is_dir() and (mesh_src_dir / "Cargo.toml").exists()
    if has_mesh:
        shutil.copytree(mesh_src_dir, mesh_bundle_dir, dirs_exist_ok=True)
        generated.append(mesh_bundle_dir)

    infra_deps = ["skaal", "gunicorn>=22.0", "apscheduler>=3.10"]
    if not is_wsgi:
        infra_deps += ["uvicorn[standard]>=0.29", "starlette>=0.36"]
    if has_mesh:
        infra_deps.append("skaal-mesh")
    seen_deps: set[str] = set()
    for spec in plan.storage.values():
        for dep in get_handler(spec, local=True).extra_deps:
            if dep not in seen_deps:
                seen_deps.add(dep)
                infra_deps.append(dep)
    user_pkgs = collect_user_packages(source_module)
    deps = list(dict.fromkeys(infra_deps + user_pkgs))
    uv_sources: dict[str, str] = {}
    if dev and skaal_src_dir.is_dir():
        uv_sources["skaal"] = "./_skaal"
    if has_mesh:
        uv_sources["skaal-mesh"] = "./mesh"
    pyproject_path = output_dir / "pyproject.toml"
    pyproject_path.write_text(
        to_pyproject_toml(app.name, deps, uv_sources=uv_sources or None), encoding="utf-8"
    )
    generated.append(pyproject_path)

    gunicorn_worker = "" if is_wsgi else ', "-k", "uvicorn.workers.UvicornWorker"'
    dockerfile_path = output_dir / "Dockerfile"
    dockerfile_path.write_text(
        render("local/Dockerfile", gunicorn_worker=gunicorn_worker), encoding="utf-8"
    )
    generated.append(dockerfile_path)

    src_pkg_dir = project_root / top_pkg
    dst_pkg_dir = output_dir / top_pkg
    if src_pkg_dir.is_dir():
        shutil.copytree(src_pkg_dir, dst_pkg_dir, dirs_exist_ok=True)
        generated.append(dst_pkg_dir)

    kong_config = build_kong_config(app, plan)
    if kong_config is not None:
        kong_path = output_dir / "kong.yml"
        kong_path.write_text(kong_config, encoding="utf-8")
        generated.append(kong_path)

    pulumi_yaml_path = output_dir / "Pulumi.yaml"
    pulumi_yaml_path.write_text(
        to_pulumi_yaml(
            _build_pulumi_stack(
                app,
                plan,
                output_dir=output_dir,
                source_module=source_module,
                dev=dev,
            )
        ),
        encoding="utf-8",
    )
    generated.append(pulumi_yaml_path)

    local_stack_spec_path = write_local_stack_spec(
        output_dir,
        _build_pulumi_stack(
            app,
            plan,
            output_dir=output_dir,
            source_module=source_module,
            dev=dev,
        ),
    )
    generated.append(local_stack_spec_path)

    gitignore_path = output_dir / ".gitignore"
    gitignore_path.write_text(_LOCAL_GITIGNORE, encoding="utf-8")
    generated.append(gitignore_path)

    dockerignore_path = output_dir / ".dockerignore"
    dockerignore_path.write_text(_LOCAL_DOCKERIGNORE, encoding="utf-8")
    generated.append(dockerignore_path)

    meta_path = write_meta(
        output_dir, target="local", source_module=source_module, app_name=app.name
    )
    generated.append(meta_path)

    return generated
