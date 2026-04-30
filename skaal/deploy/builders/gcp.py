"""GCP Cloud Run Pulumi stack builder."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

from skaal.deploy.backends import DefaultExternalProvisioner, get_handler
from skaal.deploy.builders.apigw import add_gcp_api_gateway
from skaal.deploy.builders.common import resource_slug
from skaal.deploy.config import (
    CloudRunDeployConfig,
    CloudSQLDeployConfig,
    MemorystoreRedisDeployConfig,
)
from skaal.types import AppLike, PulumiStack, StackProfile

if TYPE_CHECKING:
    from skaal.plan import PlanFile


def build_pulumi_stack(
    app: AppLike,
    plan: "PlanFile",
    region: str,
    stack_profile: StackProfile | None = None,
) -> PulumiStack:
    deploy = CloudRunDeployConfig.model_validate(plan.deploy_config)
    needs_vpc = any(get_handler(spec).requires_vpc for spec in plan.storage.values())

    profile_env: dict[str, str] = (stack_profile or {}).get("env") or {}
    profile_invokers: list[str] = (stack_profile or {}).get("invokers") or []
    profile_labels: dict[str, str] = (stack_profile or {}).get("labels") or {}

    config: dict[str, Any] = {
        "gcp:project": {"type": "string"},
        "gcp:region": {"type": "string", "default": region},
        "cloudRunMemory": {"type": "string", "default": deploy.memory},
        "cloudRunCpu": {"type": "string", "default": deploy.cpu},
        "cloudRunConcurrency": {"type": "integer", "default": deploy.concurrency},
        "cloudRunMinInstances": {"type": "integer", "default": deploy.min_instances},
        "cloudRunMaxInstances": {"type": "integer", "default": deploy.max_instances},
    }

    for qualified_name, spec in plan.storage.items():
        class_name = qualified_name.split(".")[-1]
        if spec.backend in ("cloud-sql-postgres", "cloud-sql-pgvector"):
            sql_config = CloudSQLDeployConfig.model_validate(spec.deploy_params)
            config[f"sqlTier{class_name}"] = {"type": "string", "default": sql_config.tier}
            config[f"sqlDeletionProtection{class_name}"] = {
                "type": "boolean",
                "default": sql_config.deletion_protection,
            }
        elif spec.backend == "memorystore-redis":
            redis_config = MemorystoreRedisDeployConfig.model_validate(spec.deploy_params)
            config[f"redisSizeGb{class_name}"] = {
                "type": "integer",
                "default": redis_config.memory_size_gb,
            }

    resources: dict[str, Any] = {
        "repo": {
            "type": "gcp:artifactregistry:Repository",
            "properties": {
                "repositoryId": f"${{pulumi.stack}}-{app.name}",
                "location": "${gcp:region}",
                "format": "DOCKER",
            },
        }
    }
    bucket_outputs: dict[str, str] = {}

    container_envs: list[dict[str, str]] = []
    for qualified_name, spec in plan.storage.items():
        class_name = qualified_name.split(".")[-1]
        handler = get_handler(spec)
        env_var = f"{handler.env_prefix}_{class_name.upper()}"

        if spec.backend == "gcs":
            resource_key = f"{class_name.lower()}-bucket"
            bucket_name = f"${{pulumi.stack}}-{resource_slug(app.name)}-{resource_slug(class_name)}"
            resources[resource_key] = {
                "type": "gcp:storage:Bucket",
                "properties": {
                    "name": bucket_name,
                    "location": "${gcp:region}",
                    "uniformBucketLevelAccess": True,
                },
            }
            container_envs.append({"name": env_var, "value": bucket_name})
            bucket_outputs[class_name.lower()] = bucket_name
        elif spec.backend == "firestore":
            collection = f"{app.name}-{class_name.lower()}"
            container_envs.append({"name": env_var, "value": collection})
        elif spec.backend in ("cloud-sql-postgres", "cloud-sql-pgvector"):
            sql_config = CloudSQLDeployConfig.model_validate(spec.deploy_params)
            resource_key = f"{class_name.lower()}-sql"
            resources[resource_key] = {
                "type": "gcp:sql:DatabaseInstance",
                "properties": {
                    "databaseVersion": sql_config.database_version,
                    "region": "${gcp:region}",
                    "settings": {
                        "tier": f"${{{f'sqlTier{class_name}'}}}",
                        "backupConfiguration": {"enabled": sql_config.backup_enabled},
                    },
                    "deletionProtection": f"${{{f'sqlDeletionProtection{class_name}'}}}",
                },
            }
            resources[f"{class_name.lower()}-db"] = {
                "type": "gcp:sql:Database",
                "properties": {"instance": f"${{{resource_key}.name}}", "name": app.name},
            }
            dsn = f"postgresql://skaal@localhost/{app.name}?host=/cloudsql/${{{resource_key}.connectionName}}"
            container_envs.append({"name": env_var, "value": dsn})
        elif spec.backend == "memorystore-redis":
            redis_config = MemorystoreRedisDeployConfig.model_validate(spec.deploy_params)
            resource_key = f"{class_name.lower()}-redis"
            resources[resource_key] = {
                "type": "gcp:redis:Instance",
                "properties": {
                    "tier": redis_config.tier,
                    "memorySizeGb": f"${{{f'redisSizeGb{class_name}'}}}",
                    "region": "${gcp:region}",
                    "redisVersion": redis_config.redis_version,
                },
            }
            container_envs.append(
                {"name": env_var, "value": f"redis://${{{resource_key}.host}}:6379"}
            )

    existing = {entry["name"] for entry in container_envs}
    for name, source in DefaultExternalProvisioner().env_vars(plan).items():
        if name not in existing:
            container_envs.append({"name": name, "value": source})

    if profile_env:
        existing_idx = {entry["name"]: index for index, entry in enumerate(container_envs)}
        for name, value in profile_env.items():
            entry = {"name": name, "value": value}
            if name in existing_idx:
                container_envs[existing_idx[name]] = entry
            else:
                container_envs.append(entry)

    service_annotations: dict[str, str] = {}
    if needs_vpc:
        resources["vpc-connector"] = {
            "type": "gcp:vpcaccess:Connector",
            "properties": {
                "name": "${pulumi.stack}-connector",
                "region": "${gcp:region}",
                "ipCidrRange": "10.8.0.0/28",
                "network": "default",
            },
        }
        service_annotations = {
            "run.googleapis.com/vpc-access-connector": "${vpc-connector.name}",
            "run.googleapis.com/vpc-access-egress": "private-ranges-only",
        }

    image = f"${{gcp:region}}-docker.pkg.dev/${{gcp:project}}/${{repo.name}}/{app.name}:latest"
    scaling_annotations = {
        "autoscaling.knative.dev/minScale": "${cloudRunMinInstances}",
        "autoscaling.knative.dev/maxScale": "${cloudRunMaxInstances}",
    }
    template_annotations = {**scaling_annotations, **service_annotations}
    template_metadata: dict[str, Any] = {"annotations": template_annotations}
    if profile_labels:
        template_metadata["labels"] = dict(profile_labels)

    resources["cloud-run-service"] = {
        "type": "gcp:cloudrun:Service",
        "properties": {
            "name": f"${{pulumi.stack}}-{app.name}",
            "location": "${gcp:region}",
            "template": {
                "metadata": template_metadata,
                "spec": {
                    "containerConcurrency": "${cloudRunConcurrency}",
                    "containers": [
                        {
                            "image": image,
                            "envs": container_envs,
                            "resources": {
                                "limits": {"memory": "${cloudRunMemory}", "cpu": "${cloudRunCpu}"},
                            },
                        }
                    ],
                },
            },
            "traffics": [{"percent": 100, "latestRevision": True}],
        },
    }

    invoker_members = profile_invokers or ["allUsers"]
    for index, member in enumerate(invoker_members):
        suffix = "" if index == 0 else f"-{index}"
        resources[f"invoker{suffix}"] = {
            "type": "gcp:cloudrun:IamMember",
            "properties": {
                "service": "${cloud-run-service.name}",
                "location": "${gcp:region}",
                "role": "roles/run.invoker",
                "member": member,
            },
        }

    for comp_name, comp in plan.components.items():
        if comp.kind != "schedule-trigger":
            continue
        cfg = comp.config
        trigger_type = cfg.get("trigger_type", "cron")
        target_fn = cfg.get("target_function", comp_name)
        timezone = cfg.get("timezone", "UTC")

        if trigger_type == "cron":
            cron_expr = cfg["trigger"]["expression"]
        else:
            from skaal.schedule import Every

            cron_expr = Every(interval=cfg["trigger"]["interval"]).as_cron_expression()

        body_bytes = json.dumps({"_skaal_trigger": comp_name}).encode()
        resources[f"{comp_name}-scheduler"] = {
            "type": "gcp:cloudscheduler:Job",
            "properties": {
                "name": f"${{pulumi.stack}}-{comp_name}",
                "schedule": cron_expr,
                "timeZone": timezone,
                "region": "${gcp:region}",
                "httpTarget": {
                    "uri": (
                        "${" "cloud-run-service.statuses[0].url" "}" + f"/_skaal/invoke/{target_fn}"
                    ),
                    "httpMethod": "POST",
                    "headers": {"Content-Type": "application/json"},
                    "body": base64.b64encode(body_bytes).decode(),
                    "oidcToken": {
                        "serviceAccountEmail": (
                            "${cloud-run-service.template[0].spec[0]" ".serviceAccountName}"
                        ),
                        "audience": ("${" "cloud-run-service.statuses[0].url" "}"),
                    },
                },
            },
            "options": {"dependsOn": ["${cloud-run-service}"]},
        }

    outputs: dict[str, Any] = {
        "serviceUrl": "${cloud-run-service.statuses[0].url}",
        "imageRepository": "${repo.name}",
        **{f"bucket{k.capitalize()}": value for k, value in bucket_outputs.items()},
    }
    add_gcp_api_gateway(app, plan, resources, outputs)

    return {
        "name": f"skaal-{app.name}",
        "runtime": "yaml",
        "config": config,
        "resources": resources,
        "outputs": outputs,
    }
