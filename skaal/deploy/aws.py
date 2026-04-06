"""AWS Lambda artifact generator."""

from __future__ import annotations

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
    resources["api"] = {
        "type": "aws:apigatewayv2:Api",
        "properties": {
            "name": f"${{pulumi.stack}}-{app.name}-api",
            "protocolType": "HTTP",
        },
    }
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
    resources["default-route"] = {
        "type": "aws:apigatewayv2:Route",
        "properties": {
            "apiId": "${api.id}",
            "routeKey": "$default",
            "target": "integrations/${lambda-integration.id}",
        },
    }
    resources["default-stage"] = {
        "type": "aws:apigatewayv2:Stage",
        "properties": {
            "apiId": "${api.id}",
            "name": "$default",
            "autoDeploy": True,
        },
        "options": {"dependsOn": ["${default-route}"]},
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
