"""Tests for local Docker Compose gateway (Traefik / Kong) generation."""

from __future__ import annotations

from unittest.mock import MagicMock

from skaal.components import APIGateway, Proxy, Route
from skaal.deploy.builders.local_compose import _build_docker_compose, _kong_config, _traefik_labels
from skaal.plan import PlanFile
from skaal.solver.components import encode_component

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_app(name: str = "test-app", mounts: dict | None = None) -> MagicMock:
    app = MagicMock()
    app.name = name
    if mounts:
        app._mounts = mounts
    else:
        del app._mounts
        app.__dict__.pop("_mounts", None)
    return app


def _empty_plan(components: dict | None = None) -> PlanFile:
    return PlanFile(app_name="test-app", components=components or {})


# ── Traefik labels ────────────────────────────────────────────────────────────


def test_traefik_labels_no_routes_default():
    """No routes → single catch-all PathPrefix(/) label."""
    labels = _traefik_labels([], "myapp")
    assert "traefik.enable=true" in labels
    assert "PathPrefix(`/`)" in labels


def test_traefik_labels_per_route():
    routes = [
        {"path": "/api/*", "target": "fn", "methods": ["GET"]},
        {"path": "/health", "target": "hc", "methods": ["GET"]},
    ]
    labels = _traefik_labels(routes, "myapp")
    assert "PathPrefix(`/api`)" in labels
    assert "PathPrefix(`/health`)" in labels


# ── Kong config ───────────────────────────────────────────────────────────────


def test_kong_config_basic():
    routes = [{"path": "/v1/*", "target": "fn", "methods": ["GET", "POST"]}]
    cfg = _kong_config(routes, auth=None, rate_limit=None, cors_origins=None)
    assert "_format_version" in cfg
    assert "skaal-app" in cfg
    assert "/v1" in cfg
    assert "GET" in cfg
    assert "POST" in cfg


def test_kong_config_rate_limit():
    routes = [{"path": "/", "target": "fn", "methods": ["GET"]}]
    cfg = _kong_config(
        routes,
        auth=None,
        rate_limit={"requests_per_second": 2, "burst": 4, "scope": "global"},
        cors_origins=None,
    )
    assert "rate-limiting" in cfg
    assert "minute: 120" in cfg  # 2 rps × 60


def test_kong_config_cors():
    routes = [{"path": "/", "target": "fn", "methods": ["GET"]}]
    cfg = _kong_config(
        routes,
        auth=None,
        rate_limit=None,
        cors_origins=["https://app.example.com"],
    )
    assert "cors" in cfg
    assert "https://app.example.com" in cfg


def test_kong_config_jwt_auth():
    routes = [{"path": "/", "target": "fn", "methods": ["GET"]}]
    cfg = _kong_config(
        routes,
        auth={"provider": "jwt", "issuer": "https://auth.example.com"},
        rate_limit=None,
        cors_origins=None,
    )
    assert "jwt" in cfg
    assert "https://auth.example.com" in cfg


def test_kong_config_uses_custom_upstream_service_name():
    routes = [{"path": "/", "target": "fn", "methods": ["GET"]}]
    cfg = _kong_config(
        routes,
        auth=None,
        rate_limit=None,
        cors_origins=None,
        app_service_name="backend",
    )

    assert "url: http://backend:8000" in cfg


# ── Docker Compose without gateway ───────────────────────────────────────────


def test_compose_no_gateway():
    plan = _empty_plan()
    app = _make_app()
    compose = _build_docker_compose(plan, port=8000, source_pkg="myapp", app=app)
    assert "skaal-app" in compose
    assert "traefik" not in compose
    assert "kong" not in compose


# ── Docker Compose with Proxy → Traefik ──────────────────────────────────────


def test_compose_proxy_adds_traefik():
    proxy = Proxy("edge", routes=[Route("/api/*", target="fn")])
    spec = encode_component("edge", proxy, {}, target="local")
    plan = _empty_plan(components={"edge": spec})
    app = _make_app()
    compose = _build_docker_compose(plan, port=8000, source_pkg="myapp", app=app)

    assert "traefik" in compose
    assert "traefik.enable=true" in compose


def test_compose_proxy_traefik_route_labels():
    proxy = Proxy(
        "edge",
        routes=[Route("/api/*", target="fn"), Route("/health", target="hc")],
    )
    spec = encode_component("edge", proxy, {}, target="local")
    plan = _empty_plan(components={"edge": spec})
    app = _make_app()
    compose = _build_docker_compose(plan, port=8000, source_pkg="myapp", app=app)

    assert "PathPrefix(`/api`)" in compose
    assert "PathPrefix(`/health`)" in compose


# ── Docker Compose with APIGateway → Kong ────────────────────────────────────


def test_compose_api_gateway_adds_kong():
    gw = APIGateway("public", routes=[Route("/v1/*", target="fn")])
    spec = encode_component("public", gw, {}, target="local")
    plan = _empty_plan(components={"public": spec})
    app = _make_app()
    compose = _build_docker_compose(plan, port=8000, source_pkg="myapp", app=app)

    assert "kong" in compose
    assert "traefik" not in compose


# ── Mount routes wired to Traefik ─────────────────────────────────────────────


def test_compose_mount_routes_to_traefik():
    """app._mounts drive Traefik labels when Proxy has no explicit routes."""
    proxy = Proxy("edge", routes=[])
    spec = encode_component("edge", proxy, {}, target="local")
    plan = _empty_plan(components={"edge": spec})
    app = _make_app(mounts={"payments": "/pay"})
    compose = _build_docker_compose(plan, port=8000, source_pkg="myapp", app=app)

    assert "PathPrefix(`/pay`)" in compose


def test_compose_respects_custom_app_service_names():
    plan = _empty_plan()
    app = _make_app(name="demo")
    compose = _build_docker_compose(
        plan,
        port=8000,
        source_pkg="myapp",
        app=app,
        app_service_name="backend",
        app_container_name="demo-local",
    )

    assert "\n  backend:\n" in compose
    assert "container_name: demo-local" in compose
