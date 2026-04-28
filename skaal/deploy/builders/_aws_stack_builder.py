from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from skaal.deploy._external import DefaultExternalProvisioner
from skaal.deploy.builders._aws_stack_apigw import _add_apigw_resources
from skaal.deploy.builders._aws_stack_common import (
    DYNAMODB_ACTIONS,
    LAMBDA_BASIC_EXEC_POLICY,
    LAMBDA_VPC_EXEC_POLICY,
    _database_name,
    _resource_slug,
)
from skaal.deploy.builders._schedule import _load_schedule
from skaal.deploy.config import DynamoDBDeployConfig, LambdaDeployConfig, RDSPostgresDeployConfig
from skaal.deploy.wiring import resolve_backend

if TYPE_CHECKING:
    from skaal.plan import PlanFile


@dataclass(slots=True)
class _AWSStackContext:
    app: Any
    plan: "PlanFile"
    deploy: LambdaDeployConfig
    config: dict[str, Any]
    variables: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, Any] = field(default_factory=dict)
    env_vars: dict[str, str] = field(default_factory=dict)
    table_outputs: dict[str, str] = field(default_factory=dict)
    db_outputs: dict[str, str] = field(default_factory=dict)
    has_dynamodb: bool = False
    needs_vpc: bool = False
    vpc_id_ref: str | None = None
    subnet_ids_ref: str | list[str] | None = None


def _build_base_config(deploy: LambdaDeployConfig, region: str) -> dict[str, Any]:
    return {
        "aws:region": {"type": "string", "default": region},
        "lambdaMemoryMb": {"type": "integer", "default": deploy.memory_mb},
        "lambdaTimeout": {"type": "integer", "default": deploy.timeout},
        "lambdaRuntime": {"type": "string", "default": deploy.runtime},
    }


def _new_context(app: Any, plan: "PlanFile", region: str) -> _AWSStackContext:
    deploy = LambdaDeployConfig.model_validate(plan.deploy_config)
    return _AWSStackContext(
        app=app,
        plan=plan,
        deploy=deploy,
        config=_build_base_config(deploy, region),
        has_dynamodb=any(
            resolve_backend(spec, target="aws").plugin.name == "dynamodb"
            for spec in plan.storage.values()
        ),
        needs_vpc=any(
            resolve_backend(spec, target="aws").requires_vpc for spec in plan.storage.values()
        ),
    )


def _configure_vpc(ctx: _AWSStackContext) -> None:
    if not ctx.needs_vpc:
        return

    if ctx.deploy.vpc_id:
        ctx.vpc_id_ref = ctx.deploy.vpc_id
    else:
        ctx.variables["selectedVpcId"] = {
            "fn::invoke": {
                "function": "aws:ec2:getVpc",
                "arguments": {"default": True},
                "return": "id",
            }
        }
        ctx.vpc_id_ref = "${selectedVpcId}"

    if ctx.deploy.subnet_ids:
        ctx.subnet_ids_ref = list(ctx.deploy.subnet_ids)
    else:
        subnet_filter = ctx.deploy.vpc_id or "${selectedVpcId}"
        ctx.variables["selectedSubnetIds"] = {
            "fn::invoke": {
                "function": "aws:ec2:getSubnets",
                "arguments": {
                    "filters": [{"name": "vpc-id", "values": [subnet_filter]}],
                },
                "return": "ids",
            }
        }
        ctx.subnet_ids_ref = "${selectedSubnetIds}"

    ctx.resources["rds-subnet-group"] = {
        "type": "aws:rds:SubnetGroup",
        "properties": {
            "name": f"${{pulumi.stack}}-{ctx.app.name}-db-subnets",
            "subnetIds": ctx.subnet_ids_ref,
            "tags": {"skaal-app": ctx.app.name},
        },
    }
    ctx.resources["lambda-sg"] = {
        "type": "aws:ec2:SecurityGroup",
        "properties": {
            "name": f"${{pulumi.stack}}-{ctx.app.name}-lambda",
            "description": "Lambda security group for Skaal app access to VPC resources",
            "vpcId": ctx.vpc_id_ref,
            "egress": [
                {
                    "protocol": "-1",
                    "fromPort": 0,
                    "toPort": 0,
                    "cidrBlocks": ["0.0.0.0/0"],
                }
            ],
            "tags": {"skaal-app": ctx.app.name},
        },
    }


def _add_dynamodb_resource(
    ctx: _AWSStackContext,
    *,
    qname: str,
    class_name: str,
    env_var: str,
    resource_slug: str,
    deploy_params: dict[str, Any],
) -> None:
    resource_key = f"{resource_slug}-table"
    config_model = DynamoDBDeployConfig.model_validate(deploy_params)

    ctx.resources[resource_key] = {
        "type": "aws:dynamodb:Table",
        "properties": {
            "name": f"${{pulumi.stack}}-{resource_slug}",
            "hashKey": config_model.hash_key,
            "billingMode": config_model.billing_mode,
            "attributes": [{"name": config_model.hash_key, "type": config_model.hash_key_type}],
            "tags": {"skaal-app": ctx.app.name, "skaal-storage": qname},
        },
    }
    ctx.env_vars[env_var] = f"${{{resource_key}.name}}"
    ctx.table_outputs[class_name.lower()] = f"${{{resource_key}.name}}"


def _add_rds_resource(
    ctx: _AWSStackContext,
    *,
    qname: str,
    class_name: str,
    env_var: str,
    resource_slug: str,
    backend: str,
    deploy_params: dict[str, Any],
) -> None:
    if backend not in ("rds-postgres", "rds-pgvector"):
        raise ValueError(
            f"AWS deploy target does not yet support provisioning backend {backend!r} "
            f"for {qname!r}."
        )

    rds = RDSPostgresDeployConfig.model_validate(deploy_params)
    ctx.config[f"dbInstanceClass{class_name}"] = {
        "type": "string",
        "default": rds.instance_class,
    }
    ctx.config[f"dbAllocatedStorageGb{class_name}"] = {
        "type": "integer",
        "default": rds.allocated_storage_gb,
    }
    ctx.config[f"dbMaxAllocatedStorageGb{class_name}"] = {
        "type": "integer",
        "default": rds.max_allocated_storage_gb,
    }
    ctx.config[f"dbDeletionProtection{class_name}"] = {
        "type": "boolean",
        "default": rds.deletion_protection,
    }

    password_key = f"{resource_slug}-db-password"
    db_sg_key = f"{resource_slug}-db-sg"
    db_key = f"{resource_slug}-db"
    database_name = _database_name(ctx.app.name)

    ctx.resources[password_key] = {
        "type": "random:index:RandomPassword",
        "properties": {
            "length": 24,
            "special": False,
            "upper": True,
            "lower": True,
            "numeric": True,
        },
    }
    ctx.resources[db_sg_key] = {
        "type": "aws:ec2:SecurityGroup",
        "properties": {
            "name": f"${{pulumi.stack}}-{resource_slug}-db",
            "description": f"Postgres access for {class_name}",
            "vpcId": ctx.vpc_id_ref,
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
            "tags": {"skaal-app": ctx.app.name, "skaal-storage": qname},
        },
    }
    ctx.resources[db_key] = {
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
            "tags": {"skaal-app": ctx.app.name, "skaal-storage": qname},
        },
    }
    ctx.env_vars[env_var] = (
        f"postgresql://{rds.username}:${{{password_key}.result}}"
        f"@${{{db_key}.address}}:{rds.port}/{database_name}"
    )
    ctx.db_outputs[class_name.lower()] = f"${{{db_key}.address}}"


def _add_storage_resources(ctx: _AWSStackContext) -> None:
    for qname, spec in ctx.plan.storage.items():
        class_name = qname.split(".")[-1]
        handler = resolve_backend(spec, target="aws")
        env_var = handler.wiring.env_var(class_name) or ""
        resource_slug = _resource_slug(class_name)

        if spec.backend == "dynamodb":
            _add_dynamodb_resource(
                ctx,
                qname=qname,
                class_name=class_name,
                env_var=env_var,
                resource_slug=resource_slug,
                deploy_params=spec.deploy_params,
            )
            continue

        _add_rds_resource(
            ctx,
            qname=qname,
            class_name=class_name,
            env_var=env_var,
            resource_slug=resource_slug,
            backend=spec.backend,
            deploy_params=spec.deploy_params,
        )


def _add_lambda_resources(ctx: _AWSStackContext) -> None:
    for name, source in DefaultExternalProvisioner().env_vars(ctx.plan).items():
        ctx.env_vars.setdefault(name, source)

    ctx.resources["lambda-role"] = {
        "type": "aws:iam:Role",
        "properties": {
            "name": f"${{pulumi.stack}}-{ctx.app.name}-role",
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
    ctx.resources["lambda-basic-exec"] = {
        "type": "aws:iam:RolePolicyAttachment",
        "properties": {
            "role": "${lambda-role.name}",
            "policyArn": LAMBDA_BASIC_EXEC_POLICY,
        },
    }
    lambda_depends_on = ["${lambda-basic-exec}"]
    if ctx.has_dynamodb:
        ctx.resources["dynamodb-policy"] = {
            "type": "aws:iam:Policy",
            "properties": {
                "name": f"${{pulumi.stack}}-{ctx.app.name}-dynamodb",
                "policy": {
                    "fn::toJSON": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": DYNAMODB_ACTIONS,
                                "Resource": "*",
                            }
                        ],
                    }
                },
            },
        }
        ctx.resources["lambda-dynamodb-attach"] = {
            "type": "aws:iam:RolePolicyAttachment",
            "properties": {
                "role": "${lambda-role.name}",
                "policyArn": "${dynamodb-policy.arn}",
            },
        }
        lambda_depends_on.append("${lambda-dynamodb-attach}")

    if ctx.needs_vpc:
        ctx.resources["lambda-vpc-access-attach"] = {
            "type": "aws:iam:RolePolicyAttachment",
            "properties": {
                "role": "${lambda-role.name}",
                "policyArn": LAMBDA_VPC_EXEC_POLICY,
            },
        }
        lambda_depends_on.append("${lambda-vpc-access-attach}")

    lambda_props: dict[str, Any] = {
        "name": f"${{pulumi.stack}}-{ctx.app.name}",
        "runtime": "${lambdaRuntime}",
        "handler": "handler.handler",
        "role": "${lambda-role.arn}",
        "code": {"fn::fileArchive": "./lambda_package"},
        "timeout": "${lambdaTimeout}",
        "memorySize": "${lambdaMemoryMb}",
        "environment": {"variables": ctx.env_vars},
    }
    if ctx.needs_vpc:
        lambda_props["vpcConfig"] = {
            "subnetIds": ctx.subnet_ids_ref,
            "securityGroupIds": ["${lambda-sg.id}"],
            "vpcId": ctx.vpc_id_ref,
        }
    if ctx.deploy.reserved_concurrency >= 0:
        lambda_props["reservedConcurrentExecutions"] = "${lambdaReservedConcurrency}"
        ctx.config["lambdaReservedConcurrency"] = {
            "type": "integer",
            "default": ctx.deploy.reserved_concurrency,
        }

    ctx.resources["lambda-fn"] = {
        "type": "aws:lambda:Function",
        "properties": lambda_props,
        "options": {"dependsOn": lambda_depends_on},
    }


def _add_schedule_resources(ctx: _AWSStackContext) -> None:
    for comp_name, comp in ctx.plan.components.items():
        if comp.kind != "schedule-trigger":
            continue
        cfg = comp.config
        target_fn = cfg.get("target_function", comp_name)
        schedule_expr = _load_schedule(cfg).to_aws_expression()

        rule_key = f"{comp_name}-rule"
        target_key = f"{comp_name}-target"
        permission_key = f"{comp_name}-permission"

        ctx.resources[rule_key] = {
            "type": "aws:events:Rule",
            "properties": {
                "name": f"${{pulumi.stack}}-{comp_name}",
                "scheduleExpression": schedule_expr,
                "isEnabled": True,
            },
            "options": {"dependsOn": ["${lambda-fn}"]},
        }
        ctx.resources[target_key] = {
            "type": "aws:events:Target",
            "properties": {
                "rule": f"${{{rule_key}.name}}",
                "arn": "${lambda-fn.arn}",
                "input": json.dumps({"_skaal_trigger": comp_name, "target_function": target_fn}),
            },
        }
        ctx.resources[permission_key] = {
            "type": "aws:lambda:Permission",
            "properties": {
                "action": "lambda:InvokeFunction",
                "function": "${lambda-fn.name}",
                "principal": "events.amazonaws.com",
                "sourceArn": f"${{{rule_key}.arn}}",
            },
        }

    if any(component.kind == "schedule-trigger" for component in ctx.plan.components.values()):
        ctx.resources["events-invoke-policy"] = {
            "type": "aws:iam:Policy",
            "properties": {
                "name": f"${{pulumi.stack}}-{ctx.app.name}-events-invoke",
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
        ctx.resources["events-invoke-attach"] = {
            "type": "aws:iam:RolePolicyAttachment",
            "properties": {
                "role": "${lambda-role.name}",
                "policyArn": "${events-invoke-policy.arn}",
            },
        }


def _build_outputs(ctx: _AWSStackContext) -> dict[str, str]:
    return {
        "apiUrl": "${default-stage.invokeUrl}",
        "lambdaArn": "${lambda-fn.arn}",
        **{f"table{k.capitalize()}": value for k, value in ctx.table_outputs.items()},
        **{f"dbEndpoint{k.capitalize()}": value for k, value in ctx.db_outputs.items()},
    }


def _build_pulumi_stack(app: Any, plan: "PlanFile", region: str = "us-east-1") -> dict[str, Any]:
    """Return the Pulumi stack as a plain Python dict."""
    ctx = _new_context(app, plan, region)
    _configure_vpc(ctx)
    _add_storage_resources(ctx)
    _add_lambda_resources(ctx)
    _add_apigw_resources(app, plan, ctx.resources, ctx.config)
    _add_schedule_resources(ctx)

    stack = {
        "name": f"skaal-{app.name}",
        "runtime": "yaml",
        "config": ctx.config,
        "resources": ctx.resources,
        "outputs": _build_outputs(ctx),
    }
    if ctx.variables:
        stack["variables"] = ctx.variables
    return stack


__all__ = ["_build_pulumi_stack"]
