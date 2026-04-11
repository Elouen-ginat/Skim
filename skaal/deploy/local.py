"""Local Docker Compose artifact generator."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from skaal.deploy._backends import _COMPOSE_SERVICES, build_wiring, get_handler
from skaal.deploy._deps import collect_user_packages
from skaal.deploy._render import render, to_pyproject_toml
from skaal.deploy.config import LocalStackDeployConfig
from skaal.deploy.push import write_meta

if TYPE_CHECKING:
    from skaal.plan import PlanFile


# ── Local gateway helpers ─────────────────────────────────────────────────────


def _traefik_labels(routes: list[dict[str, Any]], app_name: str) -> str:
    """Generate docker-compose label lines that wire Traefik routing rules.

    Each route produces a Traefik router + service label pair.  The app
    container retains its direct port binding so it is reachable without
    Traefik during development.
    """
    if not routes:
        return (
            "    labels:\n"
            f'      - "traefik.enable=true"\n'
            f'      - "traefik.http.routers.{app_name}.rule=PathPrefix(`/`)"\n'
            f'      - "traefik.http.services.{app_name}.loadbalancer.server.port=8000"\n'
        )

    label_lines: list[str] = ["    labels:", '      - "traefik.enable=true"']
    for i, route in enumerate(routes):
        path = route["path"].rstrip("*").rstrip("/") or "/"
        router = f"{app_name}-r{i}"
        # PathPrefix strips trailing /* so /api/* → PathPrefix(`/api`)
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
) -> str:
    """Generate a Kong DB-less declarative configuration (kong.yml)."""
    lines: list[str] = [
        '_format_version: "3.0"',
        "_transform: true",
        "",
        "services:",
        "  - name: skaal-app",
        "    url: http://app:8000",
        "    routes:",
    ]

    for i, route in enumerate(routes):
        path = route["path"].rstrip("*").rstrip("/") or "/"
        methods = route.get("methods") or ["GET", "POST"]
        lines.append(f"      - name: route-{i}")
        lines.append("        paths:")
        lines.append(f"          - {path}")
        lines.append("        methods:")
        for m in methods:
            lines.append(f"          - {m.upper()}")

    # Global plugins
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
        origins_str = ", ".join(f'"{o}"' for o in cors_origins)
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


# ── Docker Compose YAML builder ───────────────────────────────────────────────


def _build_docker_compose(
    plan: "PlanFile", port: int, source_pkg: str, app: Any = None, dev: bool = False
) -> str:
    """Build a ``docker-compose.yml`` string with the app service and any
    required storage backend services.

    Cloud backends (e.g. firestore, cloud-sql-postgres) are automatically
    resolved to their local equivalents via :func:`~skaal.deploy._backends.get_handler`
    with ``local=True``.

    When *plan.components* includes a proxy or api-gateway component, the
    matching local gateway service (Traefik or Kong) is added automatically.
    """
    services_needed: dict[str, dict[str, Any]] = {}
    service_dependencies: list[str] = []
    env_vars: list[str] = []

    for qname, spec in plan.storage.items():
        class_name = qname.split(".")[-1]
        handler = get_handler(spec, local=True)

        if handler.env_prefix and handler.local_env_value:
            env_var = f"{handler.env_prefix}_{class_name.upper()}"
            env_vars.append(f"      {env_var}: {handler.local_env_value}")

        if handler.local_service and handler.local_service not in services_needed:
            compose_spec = _COMPOSE_SERVICES.get(handler.local_service)
            if compose_spec:
                services_needed[handler.local_service] = compose_spec
            if handler.local_service not in service_dependencies:
                service_dependencies.append(handler.local_service)

    # ── Proxy / API-gateway services ──────────────────────────────────────────
    app_labels = ""
    gw_comp = next(
        (c for c in plan.components.values() if c.kind in ("proxy", "api-gateway")),
        None,
    )
    if gw_comp is not None:
        impl = gw_comp.implementation or ("traefik" if gw_comp.kind == "proxy" else "kong")
        gateway_svc = "traefik" if impl == "traefik" else "kong"
        compose_spec = _COMPOSE_SERVICES.get(gateway_svc)
        if compose_spec and gateway_svc not in services_needed:
            services_needed[gateway_svc] = compose_spec
        if gateway_svc not in service_dependencies:
            service_dependencies.append(gateway_svc)

        routes: list[dict[str, Any]] = gw_comp.config.get("routes") or []
        mounts: dict[str, str] = getattr(app, "_mounts", {}) if app is not None else {}
        if not routes and mounts:
            routes = [
                {"path": prefix.rstrip("/") + "/*", "target": ns, "methods": ["GET", "POST"]}
                for ns, prefix in mounts.items()
            ]

        if gateway_svc == "traefik":
            app_name = getattr(app, "name", "app") if app is not None else "app"
            app_labels = _traefik_labels(routes, app_name)

    # Re-build additional_services after gateway was potentially added
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
                for vol in service_config["volumes"]:
                    service_lines.append(f"      {vol}")

            if service_config.get("healthcheck"):
                hc = service_config["healthcheck"]
                service_lines.append("    healthcheck:")
                service_lines.append(f"      test: {hc['test']}")
                service_lines.append(f"      interval: {hc['interval']}")
                service_lines.append(f"      timeout: {hc['timeout']}")
                service_lines.append(f"      retries: {hc['retries']}")
                service_lines.append(f"      start_period: {hc['start_period']}")

            service_lines.append("")
        additional_services = "\n".join(service_lines)

    depends_on_lines = [f"      - {dep}" for dep in service_dependencies]
    depends_on_str = "\n".join(depends_on_lines) if depends_on_lines else "      []"

    composed = render(
        "local/docker-compose.yml",
        port=str(port),
        source_pkg=source_pkg,
        service_env_vars="\n".join(env_vars) if env_vars else "      {}",
        service_dependencies=depends_on_str,
        app_labels=app_labels,
        additional_services=additional_services,
    )

    if dev:
        # In --dev mode, append a volume mount for the local skaal source so
        # live edits are picked up without rebuilding the image.
        # PYTHONPATH=/app (set in Dockerfile) makes /app/skaal take precedence
        # over the PyPI-installed package in site-packages.
        composed = composed.replace(
            f"      - ../{source_pkg}:/app/{source_pkg}",
            f"      - ../{source_pkg}:/app/{source_pkg}\n"
            f"      # Mount local skaal source for live library reload (--dev only)\n"
            f"      - ../skaal:/app/skaal",
        )

    return composed


# ── Public entry point ─────────────────────────────────────────────────────────


def generate_artifacts(
    app: Any,
    plan: "PlanFile",
    output_dir: Path,
    source_module: str,
    app_var: str = "app",
    dev: bool = False,
) -> list[Path]:
    """Generate Docker Compose deployment artifacts.

    Writes into *output_dir*:

    - ``main.py``            — App entry point (rendered from template)
    - ``Dockerfile``         — Container build spec
    - ``docker-compose.yml`` — Service orchestration (app + storage backends)
    - ``pyproject.toml``     — Python dependencies
    - ``.gitignore``         — Ignores runtime data files
    - ``skaal-meta.json``    — Target metadata consumed by ``skaal deploy``

    Cloud backends in the plan (e.g. ``firestore``, ``cloud-sql-postgres``)
    are transparently mapped to their local Docker equivalents
    (``postgres``, ``redis``) so the same plan works everywhere.

    Args:
        app:           The Skaal App instance.
        plan:          The solved PlanFile (``plan.skaal.lock``).
        output_dir:    Directory to write files into (created if absent).
        source_module: Python module path, e.g. ``"examples.counter"``.
        app_var:       Variable name of the App in the module.
        dev:           Bundle the local skaal source into the artifact so the
                       Docker image uses the working copy instead of PyPI.

    Returns:
        List of generated :class:`~pathlib.Path` objects.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    top_pkg = source_module.split(".")[0]
    project_root = output_dir.parent

    generated: list[Path] = []
    backend_imports, backend_overrides = build_wiring(plan, local=True)
    deploy_config = LocalStackDeployConfig.model_validate(plan.deploy_config)
    wsgi_attribute: str | None = getattr(app, "_wsgi_attribute", None)

    # ── main.py ───────────────────────────────────────────────────────────────
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

    # ── skaal source bundle (--dev only) ──────────────────────────────────────
    skaal_bundle_dir = output_dir / "_skaal"
    skaal_src_dir = project_root / "skaal"
    skaal_root_pyproject = project_root / "pyproject.toml"
    if dev and skaal_src_dir.is_dir() and skaal_root_pyproject.exists():
        skaal_bundle_dir.mkdir(exist_ok=True)
        shutil.copytree(skaal_src_dir, skaal_bundle_dir / "skaal", dirs_exist_ok=True)
        shutil.copy2(skaal_root_pyproject, skaal_bundle_dir / "pyproject.toml")
        for extra in ("LICENSE", "README.md"):
            src = project_root / extra
            if src.exists():
                shutil.copy2(src, skaal_bundle_dir / extra)
        generated.append(skaal_bundle_dir)

    # ── pyproject.toml ────────────────────────────────────────────────────────
    # uvicorn[standard] is needed for the UvicornWorker class used in the Dockerfile CMD.
    infra_deps = ["skaal", "gunicorn>=22.0", "uvicorn[standard]>=0.29", "starlette>=0.36"]
    seen_deps: set[str] = set()
    for spec in plan.storage.values():
        for dep in get_handler(spec, local=True).extra_deps:
            if dep not in seen_deps:
                seen_deps.add(dep)
                infra_deps.append(dep)
    user_pkgs = collect_user_packages(source_module)
    deps = list(dict.fromkeys(infra_deps + user_pkgs))
    uv_sources = {"skaal": "./_skaal"} if dev and skaal_src_dir.is_dir() else None
    pyproject_path = output_dir / "pyproject.toml"
    pyproject_path.write_text(
        to_pyproject_toml(app.name, deps, uv_sources=uv_sources), encoding="utf-8"
    )
    generated.append(pyproject_path)

    # ── Dockerfile ────────────────────────────────────────────────────────────
    dockerfile_path = output_dir / "Dockerfile"
    dockerfile_path.write_text(render("local/Dockerfile"), encoding="utf-8")
    generated.append(dockerfile_path)

    # ── source package ────────────────────────────────────────────────────────
    src_pkg_dir = project_root / top_pkg
    dst_pkg_dir = output_dir / top_pkg
    if src_pkg_dir.is_dir():
        shutil.copytree(src_pkg_dir, dst_pkg_dir, dirs_exist_ok=True)
        generated.append(dst_pkg_dir)

    # ── kong.yml (DB-less config for Kong api-gateway) ────────────────────────
    gw_comp = next(
        (c for c in plan.components.values() if c.kind in ("proxy", "api-gateway")),
        None,
    )
    if gw_comp is not None:
        impl = gw_comp.implementation or ("traefik" if gw_comp.kind == "proxy" else "kong")
        if impl == "kong":
            routes: list[dict[str, Any]] = gw_comp.config.get("routes") or []
            mounts: dict[str, str] = getattr(app, "_mounts", {})
            if not routes and mounts:
                routes = [
                    {
                        "path": prefix.rstrip("/") + "/*",
                        "target": ns,
                        "methods": ["GET", "POST"],
                    }
                    for ns, prefix in mounts.items()
                ]
            if not routes:
                routes = [{"path": "/", "target": "app", "methods": ["GET", "POST"]}]
            kong_path = output_dir / "kong.yml"
            kong_path.write_text(
                _kong_config(
                    routes,
                    auth=gw_comp.config.get("auth"),
                    rate_limit=gw_comp.config.get("rate_limit"),
                    cors_origins=gw_comp.config.get("cors_origins"),
                ),
                encoding="utf-8",
            )
            generated.append(kong_path)

    # ── docker-compose.yml ────────────────────────────────────────────────────
    compose_path = output_dir / "docker-compose.yml"
    compose_path.write_text(
        _build_docker_compose(plan, deploy_config.port, source_pkg=top_pkg, app=app, dev=dev),
        encoding="utf-8",
    )
    generated.append(compose_path)

    # ── .gitignore ────────────────────────────────────────────────────────────
    gitignore_path = output_dir / ".gitignore"
    gitignore_path.write_text(
        "# Local data\n"
        "data/\n"
        "*.db\n"
        "*.sqlite3\n"
        "__pycache__/\n"
        ".pytest_cache/\n"
        ".env\n"
        ".env.local\n",
        encoding="utf-8",
    )
    generated.append(gitignore_path)

    # ── skaal-meta.json ───────────────────────────────────────────────────────
    meta_path = write_meta(
        output_dir, target="local", source_module=source_module, app_name=app.name
    )
    generated.append(meta_path)

    return generated
