"""AWS Lambda artifact generator."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from skaal.deploy._backends import build_wiring_aws
from skaal.deploy._deps import collect_user_packages
from skaal.deploy._render import render, to_pulumi_yaml, to_pyproject_toml
from skaal.deploy.config import DynamoDBDeployConfig, LambdaDeployConfig
from skaal.deploy.push import write_meta

if TYPE_CHECKING:
    from skaal.plan import PlanFile

# IAM managed policy ARN — stable AWS value, never changes.
_LAMBDA_BASIC_EXEC_POLICY = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"

# DynamoDB actions granted to the Lambda execution role.
_DYNAMODB_ACTIONS = [
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:DeleteItem",
    "dynamodb:Scan",
    "dynamodb:Query",
]


# ── API Gateway helpers ───────────────────────────────────────────────────────


def _apigw_path(path: str) -> str:
    """Convert a Skaal wildcard path to API Gateway v2 format.

    ``/api/*``  → ``/api/{proxy+}``
    ``/health`` → ``/health``
    """
    if path in ("/*", "*"):
        return "/{proxy+}"
    if path.endswith("/*"):
        return path[:-2] + "/{proxy+}"
    if path.endswith("*"):
        return path[:-1] + "{proxy+}"
    return path


def _safe_key(route_key: str) -> str:
    """Stable Pulumi resource key derived from an API Gateway route key."""
    return re.sub(r"[^a-zA-Z0-9-]", "-", route_key).strip("-")


def _add_apigw_resources(
    app: Any,
    plan: "PlanFile",
    resources: dict[str, Any],
    config: dict[str, Any],  # noqa: ARG001 — reserved for future config entries
) -> None:
    """Populate *resources* with API Gateway v2 Pulumi resources.

    - Reads proxy / api-gateway components from ``plan.components``.
    - Generates per-route resources (instead of a single ``$default`` catch-all)
      when explicit ``Route`` specs are present.
    - Falls back to mount-prefix catch-all routes from ``app._mounts``.
    - Final fallback: ``$default`` catch-all (original behaviour).
    - Adds a JWT authorizer when ``auth.provider == "jwt"``.
    - Sets CORS on the API resource when ``cors_origins`` is present.
    - Sets stage throttling when ``rate_limit`` is present.
    """
    gw_comp = next(
        (c for c in plan.components.values() if c.kind in ("proxy", "api-gateway")),
        None,
    )
    mounts: dict[str, str] = getattr(app, "_mounts", {})

    # ── API resource (with optional CORS) ─────────────────────────────────────
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

    # ── Lambda permission + integration ───────────────────────────────────────
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

    # ── JWT authorizer ────────────────────────────────────────────────────────
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

    # ── Routes ────────────────────────────────────────────────────────────────
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
            # Collapse to ANY when all common verbs are covered
            if {"GET", "POST", "PUT", "DELETE", "PATCH"}.issubset(
                {m.upper() for m in methods}
            ):
                methods = ["ANY"]
            for method in methods:
                rk = f"{method.upper()} {gw_path}"
                if rk in seen_keys:
                    continue
                seen_keys.add(rk)
                _add_route(rk, f"route-{_safe_key(rk)}", auth_extra or None)

    elif mounts:
        # Auto-generate prefix catch-all routes from app.mount() calls
        for ns, prefix in mounts.items():
            mount_path = _apigw_path(prefix.rstrip("/") + "/*")
            _add_route(f"ANY {mount_path}", f"route-mount-{_safe_key(ns)}")

    else:
        # Original fallback: single $default catch-all
        _add_route("$default", "default-route")

    # ── Stage (with optional throttling) ─────────────────────────────────────
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
        "options": {"dependsOn": [f"${{{k}}}" for k in route_resource_keys]},
    }


# ── Pulumi YAML stack builder ─────────────────────────────────────────────────


def _build_pulumi_stack(app: Any, plan: "PlanFile") -> dict[str, Any]:
    """Return the Pulumi stack as a plain Python dict.

    All provisioning parameters come from ``plan.storage[qname].deploy_params``
    and ``plan.deploy_config``, which were sourced from the catalog's
    ``[storage/compute.X.deploy]`` sections when the plan was solved.

    User-overridable parameters (Lambda memory, timeout) are exposed as
    Pulumi ``config:`` entries with catalog-derived defaults so that
    ``pulumi config set lambdaMemoryMb 512`` works without re-planning.
    """
    deploy = LambdaDeployConfig.model_validate(plan.deploy_config)

    # ── Pulumi config (user-overridable) ──────────────────────────────────────
    config: dict[str, Any] = {
        "aws:region": {"type": "string", "default": "us-east-1"},
        "lambdaMemoryMb": {"type": "integer", "default": deploy.memory_mb},
        "lambdaTimeout": {"type": "integer", "default": deploy.timeout},
        "lambdaRuntime": {"type": "string", "default": deploy.runtime},
    }

    resources: dict[str, Any] = {}
    env_vars: dict[str, str] = {}
    table_outputs: dict[str, str] = {}

    # ── DynamoDB tables — one per storage class ───────────────────────────────
    for qname, spec in plan.storage.items():
        class_name = qname.split(".")[-1]
        resource_key = f"{class_name.lower()}-table"
        d = DynamoDBDeployConfig.model_validate(spec.deploy_params)

        resources[resource_key] = {
            "type": "aws:dynamodb:Table",
            "properties": {
                "name": f"${{pulumi.stack}}-{class_name.lower()}",
                "hashKey": d.hash_key,
                "billingMode": d.billing_mode,
                "attributes": [{"name": d.hash_key, "type": d.hash_key_type}],
                "tags": {"skaal-app": app.name, "skaal-storage": qname},
            },
        }
        env_vars[f"SKAAL_TABLE_{class_name.upper()}"] = f"${{{resource_key}.name}}"
        table_outputs[class_name.lower()] = f"${{{resource_key}.name}}"

    # ── IAM role ──────────────────────────────────────────────────────────────
    resources["lambda-role"] = {
        "type": "aws:iam:Role",
        "properties": {
            "name": f"${{pulumi.stack}}-{app.name}-role",
            "assumeRolePolicy": {
                "fn::toJSON": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "lambda.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }
            },
        },
    }
    resources["lambda-basic-exec"] = {
        "type": "aws:iam:RolePolicyAttachment",
        "properties": {
            "role": "${lambda-role.name}",
            "policyArn": _LAMBDA_BASIC_EXEC_POLICY,
        },
    }
    resources["dynamodb-policy"] = {
        "type": "aws:iam:Policy",
        "properties": {
            "name": f"${{pulumi.stack}}-{app.name}-dynamodb",
            "policy": {
                "fn::toJSON": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": _DYNAMODB_ACTIONS,
                            "Resource": "*",
                        }
                    ],
                }
            },
        },
    }
    resources["lambda-dynamodb-attach"] = {
        "type": "aws:iam:RolePolicyAttachment",
        "properties": {
            "role": "${lambda-role.name}",
            "policyArn": "${dynamodb-policy.arn}",
        },
    }

    # ── Lambda function ───────────────────────────────────────────────────────
    lambda_props: dict[str, Any] = {
        "name": f"${{pulumi.stack}}-{app.name}",
        "runtime": "${lambdaRuntime}",
        "handler": "handler.handler",
        "role": "${lambda-role.arn}",
        "code": {"fn::fileArchive": "./lambda_package"},
        "timeout": "${lambdaTimeout}",
        "memorySize": "${lambdaMemoryMb}",
        "environment": {"variables": env_vars},
    }
    if deploy.reserved_concurrency >= 0:
        lambda_props["reservedConcurrentExecutions"] = "${lambdaReservedConcurrency}"
        config["lambdaReservedConcurrency"] = {
            "type": "integer",
            "default": deploy.reserved_concurrency,
        }

    resources["lambda-fn"] = {
        "type": "aws:lambda:Function",
        "properties": lambda_props,
    }

    # ── HTTP API Gateway v2 ───────────────────────────────────────────────────
    _add_apigw_resources(app, plan, resources, config)

    # ── EventBridge rules for schedule triggers ───────────────────────────────
    for comp_name, comp in plan.components.items():
        if comp.kind != "schedule-trigger":
            continue
        cfg = comp.config
        trigger_type = cfg.get("trigger_type", "cron")
        target_fn = cfg.get("target_function", comp_name)

        if trigger_type == "cron":
            from skaal.schedule import Cron

            schedule_expr = Cron(expression=cfg["trigger"]["expression"]).as_aws_expression()
        else:
            from skaal.schedule import Every

            schedule_expr = Every(interval=cfg["trigger"]["interval"]).as_rate_expression()

        rule_key = f"{comp_name}-rule"
        target_key = f"{comp_name}-target"
        permission_key = f"{comp_name}-permission"

        resources[rule_key] = {
            "type": "aws:events:Rule",
            "properties": {
                "name": f"${{pulumi.stack}}-{comp_name}",
                "scheduleExpression": schedule_expr,
                "isEnabled": True,
            },
            "options": {"dependsOn": ["${lambda-fn}"]},
        }
        resources[target_key] = {
            "type": "aws:events:Target",
            "properties": {
                "rule": f"${{{rule_key}.name}}",
                "arn": "${lambda-fn.arn}",
                "input": json.dumps(
                    {"_skaal_trigger": comp_name, "target_function": target_fn}
                ),
            },
        }
        resources[permission_key] = {
            "type": "aws:lambda:Permission",
            "properties": {
                "action": "lambda:InvokeFunction",
                "function": "${lambda-fn.name}",
                "principal": "events.amazonaws.com",
                "sourceArn": f"${{{rule_key}.arn}}",
            },
        }

    # Also grant EventBridge the necessary IAM action on the Lambda role
    if any(c.kind == "schedule-trigger" for c in plan.components.values()):
        resources["events-invoke-policy"] = {
            "type": "aws:iam:Policy",
            "properties": {
                "name": f"${{pulumi.stack}}-{app.name}-events-invoke",
                "policy": {
                    "fn::toJSON": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": ["lambda:InvokeFunction"],
                                "Resource": "${lambda-fn.arn}",
                            }
                        ],
                    }
                },
            },
        }
        resources["events-invoke-attach"] = {
            "type": "aws:iam:RolePolicyAttachment",
            "properties": {
                "role": "${lambda-role.name}",
                "policyArn": "${events-invoke-policy.arn}",
            },
        }

    outputs: dict[str, str] = {
        "apiUrl": "${default-stage.invokeUrl}",
        "lambdaArn": "${lambda-fn.arn}",
        **{f"table{k.capitalize()}": v for k, v in table_outputs.items()},
    }

    return {
        "name": f"skaal-{app.name}",
        "runtime": "yaml",
        "config": config,
        "resources": resources,
        "outputs": outputs,
    }


# ── Public entry point ─────────────────────────────────────────────────────────


def generate_artifacts(
    app: Any,
    plan: "PlanFile",
    output_dir: Path,
    source_module: str,
    app_var: str = "app",
) -> list[Path]:
    """Generate Lambda + Pulumi YAML deployment artifacts.

    Writes into *output_dir*:

    - ``handler.py``   — Lambda entry point (rendered from template)
    - ``pyproject.toml`` — Python dependencies for the Lambda package
    - ``Pulumi.yaml``  — Complete Pulumi stack (YAML runtime)
    - ``skaal-meta.json`` — Target metadata consumed by ``skaal deploy``

    All provisioning parameters (runtime, memory, timeout, DynamoDB billing
    mode, hash key) come from ``plan.deploy_config`` and
    ``plan.storage[*].deploy_params``.  Override at deploy time with::

        pulumi config set lambdaMemoryMb 512
        pulumi config set lambdaTimeout 60

    Args:
        app:           The Skaal App instance.
        plan:          The solved PlanFile (``plan.skaal.lock``).
        output_dir:    Directory to write files into (created if absent).
        source_module: Python module path, e.g. ``"examples.counter"``.
        app_var:       Variable name of the App in the module, e.g. ``"app"``.

    Returns:
        List of generated :class:`~pathlib.Path` objects.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    generated: list[Path] = []
    backend_imports, backend_overrides = build_wiring_aws(plan)
    wsgi_attribute: str | None = getattr(app, "_wsgi_attribute", None)

    # ── handler.py ────────────────────────────────────────────────────────────
    handler_path = output_dir / "handler.py"
    if wsgi_attribute:
        handler_path.write_text(
            render(
                "aws/handler_wsgi.py",
                source_module=source_module,
                app_var=app_var,
                wsgi_attribute=wsgi_attribute,
                backend_imports=backend_imports,
                backend_overrides=backend_overrides,
            )
        )
    else:
        handler_path.write_text(
            render(
                "aws/handler.py",
                app_name=app.name,
                source_module=source_module,
                app_var=app_var,
                backend_imports=backend_imports,
                backend_overrides=backend_overrides,
            )
        )
    generated.append(handler_path)

    # ── pyproject.toml ────────────────────────────────────────────────────────
    user_pkgs = collect_user_packages(source_module)
    base_deps = ["skaal[aws]"]
    if wsgi_attribute:
        base_deps.append("mangum>=0.17")
    deps = list(dict.fromkeys(base_deps + user_pkgs))
    pyproject_path = output_dir / "pyproject.toml"
    pyproject_path.write_text(to_pyproject_toml(app.name, deps))
    generated.append(pyproject_path)

    # ── Pulumi.yaml ───────────────────────────────────────────────────────────
    pulumi_yaml_path = output_dir / "Pulumi.yaml"
    pulumi_yaml_path.write_text(to_pulumi_yaml(_build_pulumi_stack(app, plan)))
    generated.append(pulumi_yaml_path)

    # ── skaal-meta.json ───────────────────────────────────────────────────────
    meta_path = write_meta(output_dir, target="aws", source_module=source_module, app_name=app.name)
    generated.append(meta_path)

    return generated
