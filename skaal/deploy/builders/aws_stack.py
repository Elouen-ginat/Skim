"""AWS Lambda Pulumi stack builder."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from skaal.deploy._external import DefaultExternalProvisioner
from skaal.deploy.config import DynamoDBDeployConfig, LambdaDeployConfig, RDSPostgresDeployConfig
from skaal.deploy.wiring import resolve_backend

if TYPE_CHECKING:
    from skaal.plan import PlanFile

# IAM managed policy ARN — stable AWS value, never changes.
_LAMBDA_BASIC_EXEC_POLICY = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
_LAMBDA_VPC_EXEC_POLICY = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"

# DynamoDB actions granted to the Lambda execution role.
_DYNAMODB_ACTIONS = [
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:DeleteItem",
    "dynamodb:Scan",
    "dynamodb:Query",
]


def _resource_slug(name: str, *, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        slug = "skaal"
    if not slug[0].isalpha():
        slug = f"skaal-{slug}"
    return slug[:max_len].rstrip("-") or "skaal"


def _database_name(app_name: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "_", app_name.lower()).strip("_")
    if not name:
        name = "skaal"
    if not name[0].isalpha():
        name = f"skaal_{name}"
    return name[:63]


def _apigw_path(path: str) -> str:
    """Convert a Skaal wildcard path to API Gateway v2 format."""
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


def _build_pulumi_stack(app: Any, plan: "PlanFile", region: str = "us-east-1") -> dict[str, Any]:
    """Return the Pulumi stack as a plain Python dict."""
    deploy = LambdaDeployConfig.model_validate(plan.deploy_config)

    config: dict[str, Any] = {
        "aws:region": {"type": "string", "default": region},
        "lambdaMemoryMb": {"type": "integer", "default": deploy.memory_mb},
        "lambdaTimeout": {"type": "integer", "default": deploy.timeout},
        "lambdaRuntime": {"type": "string", "default": deploy.runtime},
    }

    variables: dict[str, Any] = {}
    resources: dict[str, Any] = {}
    env_vars: dict[str, str] = {}
    table_outputs: dict[str, str] = {}
    db_outputs: dict[str, str] = {}
    has_dynamodb = any(
        resolve_backend(spec, target="aws").plugin.name == "dynamodb"
        for spec in plan.storage.values()
    )
    needs_vpc = any(
        resolve_backend(spec, target="aws").requires_vpc for spec in plan.storage.values()
    )
    vpc_id_ref: str | None = None
    subnet_ids_ref: str | list[str] | None = None

    if needs_vpc:
        if deploy.vpc_id:
            vpc_id_ref = deploy.vpc_id
        else:
            variables["selectedVpcId"] = {
                "fn::invoke": {
                    "function": "aws:ec2:getVpc",
                    "arguments": {"default": True},
                    "return": "id",
                }
            }
            vpc_id_ref = "${selectedVpcId}"

        if deploy.subnet_ids:
            subnet_ids_ref = list(deploy.subnet_ids)
        else:
            subnet_filter = deploy.vpc_id or "${selectedVpcId}"
            variables["selectedSubnetIds"] = {
                "fn::invoke": {
                    "function": "aws:ec2:getSubnets",
                    "arguments": {
                        "filters": [{"name": "vpc-id", "values": [subnet_filter]}],
                    },
                    "return": "ids",
                }
            }
            subnet_ids_ref = "${selectedSubnetIds}"

        resources["rds-subnet-group"] = {
            "type": "aws:rds:SubnetGroup",
            "properties": {
                "name": f"${{pulumi.stack}}-{app.name}-db-subnets",
                "subnetIds": subnet_ids_ref,
                "tags": {"skaal-app": app.name},
            },
        }
        resources["lambda-sg"] = {
            "type": "aws:ec2:SecurityGroup",
            "properties": {
                "name": f"${{pulumi.stack}}-{app.name}-lambda",
                "description": "Lambda security group for Skaal app access to VPC resources",
                "vpcId": vpc_id_ref,
                "egress": [
                    {
                        "protocol": "-1",
                        "fromPort": 0,
                        "toPort": 0,
                        "cidrBlocks": ["0.0.0.0/0"],
                    }
                ],
                "tags": {"skaal-app": app.name},
            },
        }

    for qname, spec in plan.storage.items():
        class_name = qname.split(".")[-1]
        handler = resolve_backend(spec, target="aws")
        env_var = handler.wiring.env_var(class_name) or ""
        resource_slug = _resource_slug(class_name)

        if spec.backend == "dynamodb":
            resource_key = f"{resource_slug}-table"
            config_model = DynamoDBDeployConfig.model_validate(spec.deploy_params)

            resources[resource_key] = {
                "type": "aws:dynamodb:Table",
                "properties": {
                    "name": f"${{pulumi.stack}}-{resource_slug}",
                    "hashKey": config_model.hash_key,
                    "billingMode": config_model.billing_mode,
                    "attributes": [
                        {"name": config_model.hash_key, "type": config_model.hash_key_type}
                    ],
                    "tags": {"skaal-app": app.name, "skaal-storage": qname},
                },
            }
            env_vars[env_var] = f"${{{resource_key}.name}}"
            table_outputs[class_name.lower()] = f"${{{resource_key}.name}}"
            continue

        if spec.backend in ("rds-postgres", "rds-pgvector"):
            rds = RDSPostgresDeployConfig.model_validate(spec.deploy_params)
            config[f"dbInstanceClass{class_name}"] = {
                "type": "string",
                "default": rds.instance_class,
            }
            config[f"dbAllocatedStorageGb{class_name}"] = {
                "type": "integer",
                "default": rds.allocated_storage_gb,
            }
            config[f"dbMaxAllocatedStorageGb{class_name}"] = {
                "type": "integer",
                "default": rds.max_allocated_storage_gb,
            }
            config[f"dbDeletionProtection{class_name}"] = {
                "type": "boolean",
                "default": rds.deletion_protection,
            }

            password_key = f"{resource_slug}-db-password"
            db_sg_key = f"{resource_slug}-db-sg"
            db_key = f"{resource_slug}-db"
            database_name = _database_name(app.name)

            resources[password_key] = {
                "type": "random:index:RandomPassword",
                "properties": {
                    "length": 24,
                    "special": False,
                    "upper": True,
                    "lower": True,
                    "numeric": True,
                },
            }
            resources[db_sg_key] = {
                "type": "aws:ec2:SecurityGroup",
                "properties": {
                    "name": f"${{pulumi.stack}}-{resource_slug}-db",
                    "description": f"Postgres access for {class_name}",
                    "vpcId": vpc_id_ref,
                    "ingress": [
                        {
                            "protocol": "tcp",
                            "fromPort": rds.port,
                            "toPort": rds.port,
                            "securityGroups": ["${lambda-sg.id}"],
                        }
                    ],
                    "egress": [
                        {
                            "protocol": "-1",
                            "fromPort": 0,
                            "toPort": 0,
                            "cidrBlocks": ["0.0.0.0/0"],
                        }
                    ],
                    "tags": {"skaal-app": app.name, "skaal-storage": qname},
                },
            }
            resources[db_key] = {
                "type": "aws:rds:Instance",
                "properties": {
                    "identifier": f"${{pulumi.stack}}-{resource_slug}",
                    "dbName": database_name,
                    "engine": "postgres",
                    "engineVersion": rds.engine_version,
                    "instanceClass": f"${{{f'dbInstanceClass{class_name}'}}}",
                    "allocatedStorage": f"${{{f'dbAllocatedStorageGb{class_name}'}}}",
                    "maxAllocatedStorage": f"${{{f'dbMaxAllocatedStorageGb{class_name}'}}}",
                    "storageType": rds.storage_type,
                    "backupRetentionPeriod": rds.backup_retention_days,
                    "deletionProtection": f"${{{f'dbDeletionProtection{class_name}'}}}",
                    "username": rds.username,
                    "password": f"${{{password_key}.result}}",
                    "port": rds.port,
                    "manageMasterUserPassword": False,
                    "publiclyAccessible": rds.publicly_accessible,
                    "skipFinalSnapshot": rds.skip_final_snapshot,
                    "storageEncrypted": True,
                    "applyImmediately": True,
                    "dbSubnetGroupName": "${rds-subnet-group.name}",
                    "vpcSecurityGroupIds": [f"${{{db_sg_key}.id}}"],
                    "tags": {"skaal-app": app.name, "skaal-storage": qname},
                },
            }
            env_vars[env_var] = (
                f"postgresql://{rds.username}:${{{password_key}.result}}"
                f"@${{{db_key}.address}}:{rds.port}/{database_name}"
            )
            db_outputs[class_name.lower()] = f"${{{db_key}.address}}"
            continue

        raise ValueError(
            f"AWS deploy target does not yet support provisioning backend {spec.backend!r} "
            f"for {qname!r}."
        )

    for name, source in DefaultExternalProvisioner().env_vars(plan).items():
        env_vars.setdefault(name, source)

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
    lambda_depends_on = ["${lambda-basic-exec}"]
    if has_dynamodb:
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
        lambda_depends_on.append("${lambda-dynamodb-attach}")

    if needs_vpc:
        resources["lambda-vpc-access-attach"] = {
            "type": "aws:iam:RolePolicyAttachment",
            "properties": {
                "role": "${lambda-role.name}",
                "policyArn": _LAMBDA_VPC_EXEC_POLICY,
            },
        }
        lambda_depends_on.append("${lambda-vpc-access-attach}")

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
    if needs_vpc:
        lambda_props["vpcConfig"] = {
            "subnetIds": subnet_ids_ref,
            "securityGroupIds": ["${lambda-sg.id}"],
            "vpcId": vpc_id_ref,
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
        "options": {"dependsOn": lambda_depends_on},
    }

    _add_apigw_resources(app, plan, resources, config)

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
                "input": json.dumps({"_skaal_trigger": comp_name, "target_function": target_fn}),
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

    if any(component.kind == "schedule-trigger" for component in plan.components.values()):
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
        **{f"table{k.capitalize()}": value for k, value in table_outputs.items()},
        **{f"dbEndpoint{k.capitalize()}": value for k, value in db_outputs.items()},
    }

    stack = {
        "name": f"skaal-{app.name}",
        "runtime": "yaml",
        "config": config,
        "resources": resources,
        "outputs": outputs,
    }
    if variables:
        stack["variables"] = variables
    return stack
