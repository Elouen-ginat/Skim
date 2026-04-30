from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from skaal.deploy.builders.common import safe_key
from skaal.types import AppLike

if TYPE_CHECKING:
    from skaal.plan import PlanFile


def aws_apigw_path(path: str) -> str:
    if path in ("/*", "*"):
        return "/{proxy+}"
    if path.endswith("/*"):
        return path[:-2] + "/{proxy+}"
    if path.endswith("*"):
        return path[:-1] + "{proxy+}"
    return path


def gcp_openapi_path(path: str) -> str:
    if path in ("/*", "*"):
        return "/{proxy}"
    if path.endswith("/*"):
        return path[:-2] + "/{proxy}"
    if path.endswith("*"):
        return path[:-1] + "{proxy}"
    return path


def add_aws_apigw_resources(
    app: AppLike,
    plan: "PlanFile",
    resources: dict[str, Any],
    config: dict[str, Any],
) -> None:
    gw_comp = next(
        (
            component
            for component in plan.components.values()
            if component.kind in ("proxy", "api-gateway")
        ),
        None,
    )
    mounts: dict[str, str] = getattr(app, "_mounts", {})

    api_props: dict[str, Any] = {
        "name": f"${{pulumi.stack}}-{app.name}-api",
        "protocolType": "HTTP",
    }
    cors_origins = gw_comp.config.get("cors_origins") if gw_comp else None
    if cors_origins:
        api_props["corsConfiguration"] = {
            "allowOrigins": cors_origins,
            "allowMethods": ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
            "allowHeaders": ["Content-Type", "Authorization"],
        }
    resources["api"] = {"type": "aws:apigatewayv2:Api", "properties": api_props}

    resources["api-invoke-permission"] = {
        "type": "aws:lambda:Permission",
        "properties": {
            "action": "lambda:InvokeFunction",
            "function": "${lambda-fn.arn}",
            "principal": "apigateway.amazonaws.com",
            "sourceArn": "${api.executionArn}/*/*",
        },
    }
    resources["lambda-integration"] = {
        "type": "aws:apigatewayv2:Integration",
        "properties": {
            "apiId": "${api.id}",
            "integrationType": "AWS_PROXY",
            "integrationUri": "${lambda-fn.invokeArn}",
            "payloadFormatVersion": "2.0",
        },
    }

    authorizer_ref: str | None = None
    if gw_comp:
        auth_cfg = gw_comp.config.get("auth") or {}
        if auth_cfg.get("provider") == "jwt":
            jwt_conf: dict[str, Any] = {"issuer": auth_cfg.get("issuer", "")}
            audience = auth_cfg.get("audience")
            if audience:
                jwt_conf["audiences"] = [audience]
            resources["jwt-authorizer"] = {
                "type": "aws:apigatewayv2:Authorizer",
                "properties": {
                    "apiId": "${api.id}",
                    "authorizerType": "JWT",
                    "identitySources": ["$request.header.Authorization"],
                    "jwtConfiguration": jwt_conf,
                    "name": f"{app.name}-jwt",
                },
            }
            authorizer_ref = "${jwt-authorizer.id}"

    route_resource_keys: list[str] = []

    def add_route(route_key: str, resource_key: str, extra: dict[str, Any] | None = None) -> None:
        props: dict[str, Any] = {
            "apiId": "${api.id}",
            "routeKey": route_key,
            "target": "integrations/${lambda-integration.id}",
        }
        if extra:
            props.update(extra)
        resources[resource_key] = {"type": "aws:apigatewayv2:Route", "properties": props}
        route_resource_keys.append(resource_key)

    if gw_comp and gw_comp.config.get("routes"):
        auth_extra: dict[str, Any] = {}
        if authorizer_ref:
            auth_extra = {"authorizerId": authorizer_ref, "authorizationType": "JWT"}

        seen_keys: set[str] = set()
        for route in gw_comp.config["routes"]:
            gateway_path = aws_apigw_path(route["path"])
            methods: list[str] = route.get("methods") or ["GET", "POST"]
            if {"GET", "POST", "PUT", "DELETE", "PATCH"}.issubset(
                {method.upper() for method in methods}
            ):
                methods = ["ANY"]
            for method in methods:
                route_key = f"{method.upper()} {gateway_path}"
                if route_key in seen_keys:
                    continue
                seen_keys.add(route_key)
                add_route(route_key, f"route-{safe_key(route_key)}", auth_extra or None)
    elif mounts:
        for namespace, prefix in mounts.items():
            mount_path = aws_apigw_path(prefix.rstrip("/") + "/*")
            add_route(f"ANY {mount_path}", f"route-mount-{safe_key(namespace)}")
    else:
        add_route("$default", "default-route")

    stage_props: dict[str, Any] = {
        "apiId": "${api.id}",
        "name": "$default",
        "autoDeploy": True,
    }
    if gw_comp:
        rate_limit = gw_comp.config.get("rate_limit") or {}
        if rate_limit:
            requests_per_second = float(rate_limit.get("requests_per_second", 1000))
            burst = int(rate_limit.get("burst", max(1, int(requests_per_second * 2))))
            stage_props["defaultRouteSettings"] = {
                "throttlingBurstLimit": burst,
                "throttlingRateLimit": requests_per_second,
            }

    resources["default-stage"] = {
        "type": "aws:apigatewayv2:Stage",
        "properties": stage_props,
        "options": {"dependsOn": [f"${{{key}}}" for key in route_resource_keys]},
    }


def _build_openapi_spec(
    app_name: str,
    routes: list[dict[str, Any]],
    auth: dict[str, Any] | None,
    cloud_run_url_ref: str,
) -> dict[str, Any]:
    parts: list[Any] = [
        'swagger: "2.0"\n',
        f'info:\n  title: "{app_name} API"\n  description: "Managed by Skaal"\n  version: "1.0.0"\n',
        "schemes:\n  - https\n",
        'produces:\n  - "application/json"\n',
        "x-google-backend:\n  address: ",
        cloud_run_url_ref,
        "\npaths:\n",
    ]

    for route in routes:
        openapi_path = gcp_openapi_path(route["path"])
        safe_operation = re.sub(r"[^a-z0-9_]", "_", openapi_path.lower()).strip("_") or "root"
        has_proxy = "{proxy}" in openapi_path

        parts.append(f'  "{openapi_path}":\n')
        for method in route.get("methods") or ["get", "post"]:
            method_name = method.lower()
            parts.append(f"    {method_name}:\n")
            parts.append(f'      operationId: "{safe_operation}_{method_name}"\n')
            if has_proxy:
                parts.append(
                    "      parameters:\n"
                    "        - in: path\n"
                    "          name: proxy\n"
                    "          required: true\n"
                    "          type: string\n"
                )
            parts.append('      responses:\n        "200":\n          description: "Success"\n')

    if auth and auth.get("provider") == "jwt":
        issuer = auth.get("issuer") or ""
        audience = auth.get("audience") or ""
        parts.append(
            "securityDefinitions:\n"
            "  jwt:\n"
            '    authorizationUrl: ""\n'
            '    flow: "implicit"\n'
            '    type: "oauth2"\n'
        )
        if issuer:
            parts.append(f'    x-google-issuer: "{issuer}"\n')
            parts.append(f'    x-google-jwks_uri: "{issuer}/.well-known/jwks.json"\n')
        if audience:
            parts.append(f'    x-google-audiences: "{audience}"\n')
        parts.append("security:\n  - jwt: []\n")

    return {"fn::toBase64": {"fn::join": ["", parts]}}


def add_gcp_api_gateway(
    app: AppLike,
    plan: "PlanFile",
    resources: dict[str, Any],
    outputs: dict[str, Any],
) -> None:
    gw_comp = next(
        (
            component
            for component in plan.components.values()
            if component.kind in ("proxy", "api-gateway")
        ),
        None,
    )
    if gw_comp is None:
        return

    routes: list[dict[str, Any]] = gw_comp.config.get("routes") or []
    auth: dict[str, Any] | None = gw_comp.config.get("auth")

    if not routes:
        mounts: dict[str, str] = getattr(app, "_mounts", {})
        if mounts:
            routes = [
                {"path": prefix.rstrip("/") + "/*", "target": namespace, "methods": ["GET", "POST"]}
                for namespace, prefix in mounts.items()
            ]
        else:
            routes = [{"path": "/*", "target": "app", "methods": ["GET", "POST"]}]

    cloud_run_url = "${cloud-run-service.statuses[0].url}"
    openapi_contents = _build_openapi_spec(app.name, routes, auth, cloud_run_url)

    resources["api-gateway-api"] = {
        "type": "gcp:apigateway:Api",
        "properties": {
            "apiId": f"${{pulumi.stack}}-{app.name}",
        },
    }
    resources["api-gateway-config"] = {
        "type": "gcp:apigateway:ApiConfig",
        "properties": {
            "api": "${api-gateway-api.apiId}",
            "apiConfigId": f"${{pulumi.stack}}-{app.name}-cfg",
            "openapiDocuments": [
                {
                    "document": {
                        "path": "spec.yaml",
                        "contents": openapi_contents,
                    }
                }
            ],
        },
        "options": {"dependsOn": ["${cloud-run-service}"]},
    }
    resources["api-gateway-gateway"] = {
        "type": "gcp:apigateway:Gateway",
        "properties": {
            "gatewayId": f"${{pulumi.stack}}-{app.name}-gw",
            "apiConfig": "${api-gateway-config.id}",
            "region": "${gcp:region}",
        },
    }
    outputs["gatewayUrl"] = "${api-gateway-gateway.defaultHostname}"
