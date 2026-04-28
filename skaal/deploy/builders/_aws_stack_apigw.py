from __future__ import annotations

from typing import TYPE_CHECKING, Any

from skaal.deploy.builders._aws_stack_common import _apigw_path, _safe_key

if TYPE_CHECKING:
    from skaal.plan import PlanFile


def _add_apigw_resources(
    app: Any,
    plan: "PlanFile",
    resources: dict[str, Any],
    config: dict[str, Any],  # noqa: ARG001 — reserved for future config entries
) -> None:
    """Populate *resources* with API Gateway v2 Pulumi resources."""
    gw_comp = next(
        (c for c in plan.components.values() if c.kind in ("proxy", "api-gateway")),
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

    def _add_route(route_key: str, res_key: str, extra: dict[str, Any] | None = None) -> None:
        props: dict[str, Any] = {
            "apiId": "${api.id}",
            "routeKey": route_key,
            "target": "integrations/${lambda-integration.id}",
        }
        if extra:
            props.update(extra)
        resources[res_key] = {"type": "aws:apigatewayv2:Route", "properties": props}
        route_resource_keys.append(res_key)

    if gw_comp and gw_comp.config.get("routes"):
        auth_extra: dict[str, Any] = {}
        if authorizer_ref:
            auth_extra = {"authorizerId": authorizer_ref, "authorizationType": "JWT"}

        seen_keys: set[str] = set()
        for route in gw_comp.config["routes"]:
            gw_path = _apigw_path(route["path"])
            methods: list[str] = route.get("methods") or ["GET", "POST"]
            if {"GET", "POST", "PUT", "DELETE", "PATCH"}.issubset({m.upper() for m in methods}):
                methods = ["ANY"]
            for method in methods:
                route_key = f"{method.upper()} {gw_path}"
                if route_key in seen_keys:
                    continue
                seen_keys.add(route_key)
                _add_route(route_key, f"route-{_safe_key(route_key)}", auth_extra or None)
    elif mounts:
        for ns, prefix in mounts.items():
            mount_path = _apigw_path(prefix.rstrip("/") + "/*")
            _add_route(f"ANY {mount_path}", f"route-mount-{_safe_key(ns)}")
    else:
        _add_route("$default", "default-route")

    stage_props: dict[str, Any] = {
        "apiId": "${api.id}",
        "name": "$default",
        "autoDeploy": True,
    }
    if gw_comp:
        rl_cfg = gw_comp.config.get("rate_limit") or {}
        if rl_cfg:
            rps = float(rl_cfg.get("requests_per_second", 1000))
            burst = int(rl_cfg.get("burst", max(1, int(rps * 2))))
            stage_props["defaultRouteSettings"] = {
                "throttlingBurstLimit": burst,
                "throttlingRateLimit": rps,
            }

    resources["default-stage"] = {
        "type": "aws:apigatewayv2:Stage",
        "properties": stage_props,
        "options": {"dependsOn": [f"${{{key}}}" for key in route_resource_keys]},
    }


__all__ = ["_add_apigw_resources", "_apigw_path"]
