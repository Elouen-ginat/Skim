"""Tests for GCP API Gateway Pulumi resource generation."""

from __future__ import annotations

from unittest.mock import MagicMock

from skaal.app import App
from skaal.components import APIGateway, AuthConfig, Proxy, Route
from skaal.deploy.builders.gcp_stack import (
    _add_gcp_api_gateway,
    _build_pulumi_stack,
    _gcp_openapi_path,
)
from skaal.plan import PlanFile, StorageSpec
from skaal.solver.components import encode_component

# ── Path conversion ───────────────────────────────────────────────────────────


def test_gcp_openapi_path_wildcard():
    assert _gcp_openapi_path("/api/*") == "/api/{proxy}"


def test_gcp_openapi_path_root_wildcard():
    assert _gcp_openapi_path("/*") == "/{proxy}"


def test_gcp_openapi_path_exact():
    assert _gcp_openapi_path("/health") == "/health"


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_app(name: str = "my-app", mounts: dict | None = None) -> MagicMock:
    app = MagicMock()
    app.name = name
    if mounts:
        app._mounts = mounts
    else:
        del app._mounts
        app.__dict__.pop("_mounts", None)
    return app


# ── No component — no gateway resources ──────────────────────────────────────


def test_no_gateway_resources_when_no_component():
    plan = PlanFile(app_name="my-app")
    app = _make_app()
    resources: dict = {}
    outputs: dict = {}
    _add_gcp_api_gateway(app, plan, resources, outputs)

    assert resources == {}
    assert outputs == {}


# ── Proxy component: gateway resources emitted ───────────────────────────────


def test_proxy_emits_api_gateway_resources():
    proxy = Proxy("edge", routes=[Route("/api/*", target="fn")])
    spec = encode_component("edge", proxy, {}, target="gcp")
    plan = PlanFile(app_name="my-app", components={"edge": spec})
    app = _make_app()
    resources: dict = {}
    outputs: dict = {}
    _add_gcp_api_gateway(app, plan, resources, outputs)

    assert "api-gateway-api" in resources
    assert "api-gateway-config" in resources
    assert "api-gateway-gateway" in resources
    assert "gatewayUrl" in outputs


def test_gateway_api_config_has_openapi_doc():
    proxy = Proxy("edge", routes=[Route("/v1/*", target="fn")])
    spec = encode_component("edge", proxy, {}, target="gcp")
    plan = PlanFile(app_name="my-app", components={"edge": spec})
    app = _make_app()
    resources: dict = {}
    outputs: dict = {}
    _add_gcp_api_gateway(app, plan, resources, outputs)

    cfg = resources["api-gateway-config"]
    assert cfg["type"] == "gcp:apigateway:ApiConfig"
    docs = cfg["properties"]["openapiDocuments"]
    assert len(docs) == 1
    contents = docs[0]["document"]["contents"]
    # contents is a fn::toBase64 / fn::join structure — not a bare string
    assert isinstance(contents, dict)
    assert "fn::toBase64" in contents


def test_openapi_includes_cloud_run_url_ref():
    """The OpenAPI spec parts must reference the Cloud Run service URL."""
    proxy = Proxy("edge", routes=[Route("/*", target="fn")])
    spec = encode_component("edge", proxy, {}, target="gcp")
    plan = PlanFile(app_name="my-app", components={"edge": spec})
    app = _make_app()
    resources: dict = {}
    outputs: dict = {}
    _add_gcp_api_gateway(app, plan, resources, outputs)

    parts = resources["api-gateway-config"]["properties"]["openapiDocuments"][0]["document"][
        "contents"
    ]["fn::toBase64"]["fn::join"][1]
    # The Cloud Run URL interpolation should appear somewhere in the parts list
    assert "${cloud-run-service.statuses[0].url}" in parts


# ── APIGateway with JWT auth ──────────────────────────────────────────────────


def test_openapi_jwt_security_definition():
    gw = APIGateway(
        "public",
        routes=[Route("/v1/*", target="fn")],
        auth=AuthConfig(
            provider="jwt",
            issuer="https://auth.example.com",
            audience="my-api",
        ),
    )
    spec = encode_component("public", gw, {}, target="gcp")
    plan = PlanFile(app_name="my-app", components={"public": spec})
    app = _make_app()
    resources: dict = {}
    outputs: dict = {}
    _add_gcp_api_gateway(app, plan, resources, outputs)

    parts = resources["api-gateway-config"]["properties"]["openapiDocuments"][0]["document"][
        "contents"
    ]["fn::toBase64"]["fn::join"][1]
    joined = "".join(str(p) for p in parts if isinstance(p, str))
    assert "securityDefinitions" in joined
    assert "https://auth.example.com" in joined


# ── Fallback to mount routes ──────────────────────────────────────────────────


def test_mount_routes_fallback():
    """When component has no routes but app has _mounts, use mount prefixes."""
    proxy = Proxy("edge", routes=[])
    spec = encode_component("edge", proxy, {}, target="gcp")
    plan = PlanFile(app_name="my-app", components={"edge": spec})
    app = _make_app(mounts={"auth": "/auth"})
    resources: dict = {}
    outputs: dict = {}
    _add_gcp_api_gateway(app, plan, resources, outputs)

    parts = resources["api-gateway-config"]["properties"]["openapiDocuments"][0]["document"][
        "contents"
    ]["fn::toBase64"]["fn::join"][1]
    joined = "".join(str(p) for p in parts if isinstance(p, str))
    assert "/auth" in joined


# ── Gateway depends on Cloud Run ─────────────────────────────────────────────


def test_api_gateway_config_depends_on_cloud_run():
    proxy = Proxy("edge", routes=[Route("/api/*", target="fn")])
    spec = encode_component("edge", proxy, {}, target="gcp")
    plan = PlanFile(app_name="my-app", components={"edge": spec})
    app = _make_app()
    resources: dict = {}
    outputs: dict = {}
    _add_gcp_api_gateway(app, plan, resources, outputs)

    assert "${cloud-run-service}" in resources["api-gateway-config"]["options"]["dependsOn"]


def test_cloud_run_private_service_omits_public_invoker() -> None:
    app = App(name="demo")
    plan = PlanFile(
        app_name="demo",
        deploy_target="gcp",
        deploy_config={"allow_public_invoker": False},
    )

    stack = _build_pulumi_stack(app, plan, region="us-central1")

    assert "invoker" not in stack["resources"]


def test_cloud_run_uses_configured_vpc_connector_values() -> None:
    app = App(name="demo")
    plan = PlanFile(
        app_name="demo",
        deploy_target="gcp",
        deploy_config={
            "vpc_connector_network": "shared-vpc",
            "vpc_connector_cidr": "10.42.0.0/28",
        },
        storage={
            "demo.User": StorageSpec(
                variable_name="demo.User",
                backend="cloud-sql-postgres",
                kind="relational",
                deploy_params={},
                wire_params={
                    "class_name": "PostgresBackend",
                    "module": "postgres_backend",
                    "env_prefix": "SKAAL_DB_DSN",
                    "uses_namespace": True,
                    "requires_vpc": True,
                },
            ),
        },
    )

    stack = _build_pulumi_stack(app, plan, region="us-central1")
    props = stack["resources"]["vpc-connector"]["properties"]

    assert props["network"] == "shared-vpc"
    assert props["ipCidrRange"] == "10.42.0.0/28"


def test_cloud_run_can_reuse_existing_vpc_connector() -> None:
    app = App(name="demo")
    plan = PlanFile(
        app_name="demo",
        deploy_target="gcp",
        deploy_config={
            "vpc_connector_name": "projects/demo/locations/us-central1/connectors/shared",
            "vpc_connector_egress": "all-traffic",
        },
        storage={
            "demo.User": StorageSpec(
                variable_name="demo.User",
                backend="cloud-sql-postgres",
                kind="relational",
                deploy_params={},
                wire_params={
                    "class_name": "PostgresBackend",
                    "module": "postgres_backend",
                    "env_prefix": "SKAAL_DB_DSN",
                    "uses_namespace": True,
                    "requires_vpc": True,
                },
            ),
        },
    )

    stack = _build_pulumi_stack(app, plan, region="us-central1")

    assert "vpc-connector" not in stack["resources"]
    annotations = stack["resources"]["cloud-run-service"]["properties"]["template"]["metadata"][
        "annotations"
    ]
    assert annotations["run.googleapis.com/vpc-access-connector"] == (
        "projects/demo/locations/us-central1/connectors/shared"
    )
    assert annotations["run.googleapis.com/vpc-access-egress"] == "all-traffic"
