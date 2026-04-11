"""Tests for AWS API Gateway Pulumi resource generation."""

from __future__ import annotations

from unittest.mock import MagicMock

from skaal.components import APIGateway, AuthConfig, Proxy, Route
from skaal.deploy.aws import _add_apigw_resources, _apigw_path
from skaal.plan import ComponentSpec, PlanFile
from skaal.solver.components import encode_component
from skaal.types import RateLimitPolicy

# ── Path conversion ───────────────────────────────────────────────────────────


def test_apigw_path_wildcard():
    assert _apigw_path("/api/*") == "/api/{proxy+}"


def test_apigw_path_root_wildcard():
    assert _apigw_path("/*") == "/{proxy+}"


def test_apigw_path_exact():
    assert _apigw_path("/health") == "/health"


def test_apigw_path_bare_star():
    assert _apigw_path("*") == "/{proxy+}"


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_plan(**component_overrides: ComponentSpec) -> PlanFile:
    return PlanFile(app_name="test-app", components=component_overrides)


def _make_app(name: str = "test-app", mounts: dict | None = None) -> MagicMock:
    app = MagicMock()
    app.name = name
    if mounts:
        app._mounts = mounts
    else:
        # Simulate missing attribute (no mount() calls)
        del app._mounts
        app.__dict__.pop("_mounts", None)
    return app


# ── Default catch-all (no component) ─────────────────────────────────────────


def test_default_route_no_component():
    """Without a proxy/api-gateway component, a $default catch-all is generated."""
    plan = _make_plan()
    app = _make_app()
    resources: dict = {}
    _add_apigw_resources(app, plan, resources, {})

    assert "default-route" in resources
    assert resources["default-route"]["properties"]["routeKey"] == "$default"
    assert "api" in resources
    assert "lambda-integration" in resources
    assert "default-stage" in resources


# ── Mount-derived routes ──────────────────────────────────────────────────────


def test_mount_routes_when_no_component():
    """When app has _mounts but no component, one ANY route per mount is created."""
    plan = _make_plan()
    app = _make_app(mounts={"auth": "/auth", "api": "/v1"})
    resources: dict = {}
    _add_apigw_resources(app, plan, resources, {})

    route_keys = {
        r["properties"]["routeKey"]
        for r in resources.values()
        if r.get("type") == "aws:apigatewayv2:Route"
    }
    assert "ANY /auth/{proxy+}" in route_keys
    assert "ANY /v1/{proxy+}" in route_keys
    assert "$default" not in route_keys


# ── Proxy component: explicit routes ─────────────────────────────────────────


def test_proxy_explicit_routes():
    """Proxy component routes replace the $default catch-all."""
    proxy = Proxy(
        "edge",
        routes=[
            Route("/api/*", target="handle", methods=["GET", "POST"]),
            Route("/health", target="healthz", methods=["GET"]),
        ],
    )
    spec = encode_component("edge", proxy, {}, target="aws-lambda")
    plan = _make_plan(edge=spec)
    app = _make_app()
    resources: dict = {}
    _add_apigw_resources(app, plan, resources, {})

    route_keys = {
        r["properties"]["routeKey"]
        for r in resources.values()
        if r.get("type") == "aws:apigatewayv2:Route"
    }
    assert "GET /api/{proxy+}" in route_keys
    assert "POST /api/{proxy+}" in route_keys
    assert "GET /health" in route_keys
    assert "$default" not in route_keys


# ── APIGateway: JWT authorizer ────────────────────────────────────────────────


def test_api_gateway_jwt_authorizer():
    """APIGateway with JWT auth emits a JWT authorizer and wires it to routes."""
    gw = APIGateway(
        "public",
        routes=[Route("/v1/*", target="fn")],
        auth=AuthConfig(
            provider="jwt",
            issuer="https://auth.example.com",
            audience="my-api",
        ),
    )
    spec = encode_component("public", gw, {}, target="aws-lambda")
    plan = _make_plan(public=spec)
    app = _make_app()
    resources: dict = {}
    _add_apigw_resources(app, plan, resources, {})

    assert "jwt-authorizer" in resources
    auth_res = resources["jwt-authorizer"]
    assert auth_res["type"] == "aws:apigatewayv2:Authorizer"
    assert auth_res["properties"]["authorizerType"] == "JWT"
    assert auth_res["properties"]["jwtConfiguration"]["issuer"] == "https://auth.example.com"
    assert "my-api" in auth_res["properties"]["jwtConfiguration"]["audiences"]

    # Routes should carry authorizerId
    route_resources = [r for r in resources.values() if r.get("type") == "aws:apigatewayv2:Route"]
    assert all("authorizerId" in r["properties"] for r in route_resources)
    assert all(r["properties"]["authorizationType"] == "JWT" for r in route_resources)


# ── APIGateway: CORS ──────────────────────────────────────────────────────────


def test_api_gateway_cors():
    """APIGateway with cors_origins sets corsConfiguration on the API resource."""
    gw = APIGateway(
        "public",
        routes=[Route("/*", target="fn")],
        cors_origins=["https://app.example.com"],
    )
    spec = encode_component("public", gw, {}, target="aws-lambda")
    plan = _make_plan(public=spec)
    app = _make_app()
    resources: dict = {}
    _add_apigw_resources(app, plan, resources, {})

    assert "corsConfiguration" in resources["api"]["properties"]
    cors = resources["api"]["properties"]["corsConfiguration"]
    assert "https://app.example.com" in cors["allowOrigins"]


# ── APIGateway: rate limiting ─────────────────────────────────────────────────


def test_api_gateway_rate_limit():
    """APIGateway with rate_limit adds throttling settings to the stage."""
    gw = APIGateway(
        "public",
        routes=[Route("/*", target="fn")],
        rate_limit=RateLimitPolicy(requests_per_second=50, burst=100),
    )
    spec = encode_component("public", gw, {}, target="aws-lambda")
    plan = _make_plan(public=spec)
    app = _make_app()
    resources: dict = {}
    _add_apigw_resources(app, plan, resources, {})

    stage = resources["default-stage"]
    assert "defaultRouteSettings" in stage["properties"]
    throttle = stage["properties"]["defaultRouteSettings"]
    assert throttle["throttlingRateLimit"] == 50.0
    assert throttle["throttlingBurstLimit"] == 100


# ── Stage dependsOn ───────────────────────────────────────────────────────────


def test_stage_depends_on_routes():
    """Stage dependsOn should reference all created route resources."""
    proxy = Proxy("edge", routes=[Route("/a/*", target="fn"), Route("/b", target="fn2")])
    spec = encode_component("edge", proxy, {}, target="aws-lambda")
    plan = _make_plan(edge=spec)
    app = _make_app()
    resources: dict = {}
    _add_apigw_resources(app, plan, resources, {})

    depends = resources["default-stage"]["options"]["dependsOn"]
    route_keys = {k for k in resources if k.startswith("route-")}
    for rk in route_keys:
        assert f"${{{rk}}}" in depends
