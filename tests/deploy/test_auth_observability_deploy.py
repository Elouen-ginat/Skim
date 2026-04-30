from __future__ import annotations

from skaal import APIGateway, App, AuthConfig, Route
from skaal.deploy.builders.apigw import add_aws_apigw_resources
from skaal.deploy.builders.aws import build_pulumi_stack as build_aws_stack
from skaal.deploy.builders.gcp import build_pulumi_stack as build_gcp_stack
from skaal.deploy.builders.local import build_kong_config
from skaal.plan import ComponentSpec, PlanFile


def _gateway_component_config(
    *, header: str = "Authorization", required: bool = True
) -> dict[str, object]:
    gateway = APIGateway(
        "public-api",
        routes=[Route("/api/*", target="demo.increment")],
        auth=AuthConfig(
            provider="jwt",
            issuer="https://issuer.example.test",
            audience="skaal-tests",
            header=header,
            required=required,
        ),
        cors_origins=["*"],
    )
    return {key: value for key, value in gateway.describe().items() if key not in {"kind", "name"}}


def test_aws_apigw_uses_custom_auth_header_when_required() -> None:
    app = App("demo")
    plan = PlanFile(
        app_name="demo",
        components={
            "public-api": ComponentSpec(
                component_name="public-api",
                kind="api-gateway",
                implementation="aws-apigateway",
                config=_gateway_component_config(header="X-Auth-Token"),
            )
        },
    )
    resources: dict[str, object] = {}

    add_aws_apigw_resources(app, plan, resources, {})

    authorizer = resources["jwt-authorizer"]["properties"]
    assert authorizer["identitySources"] == ["$request.header.X-Auth-Token"]
    assert "X-Auth-Token" in resources["api"]["properties"]["corsConfiguration"]["allowHeaders"]


def test_optional_auth_does_not_force_gateway_authorizer_or_kong_jwt_plugin() -> None:
    app = App("demo")
    plan = PlanFile(
        app_name="demo",
        components={
            "public-api": ComponentSpec(
                component_name="public-api",
                kind="api-gateway",
                implementation="kong",
                config=_gateway_component_config(required=False),
            )
        },
    )
    resources: dict[str, object] = {}

    add_aws_apigw_resources(app, plan, resources, {})

    assert "jwt-authorizer" not in resources
    kong = build_kong_config(app, plan, app_service_name="demo")
    assert kong is not None
    assert "  - name: jwt" not in kong


def test_telemetry_endpoint_env_is_injected_into_cloud_targets() -> None:
    app = App("demo")
    plan = PlanFile(
        app_name="demo",
        components={
            "signoz": ComponentSpec(
                component_name="signoz",
                kind="external-observability",
                provisioned=False,
                connection_env="OTEL_EXPORTER_OTLP_ENDPOINT",
                config={"provider": "signoz"},
            )
        },
    )

    aws_stack = build_aws_stack(app, plan, region="us-east-1")
    aws_env = aws_stack["resources"]["lambda-fn"]["properties"]["environment"]["variables"]
    assert aws_env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "${env:OTEL_EXPORTER_OTLP_ENDPOINT}"

    gcp_stack = build_gcp_stack(app, plan, region="us-central1")
    gcp_envs = gcp_stack["resources"]["cloud-run-service"]["properties"]["template"]["spec"][
        "containers"
    ][0]["envs"]
    assert {
        "name": "OTEL_EXPORTER_OTLP_ENDPOINT",
        "value": "${env:OTEL_EXPORTER_OTLP_ENDPOINT}",
    } in gcp_envs
