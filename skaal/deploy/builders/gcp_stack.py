"""GCP Cloud Run Pulumi stack builder."""

from __future__ import annotations

import base64
import json
import re
from typing import TYPE_CHECKING, Any

from skaal.deploy._external import DefaultExternalProvisioner
from skaal.deploy.config import (
    CloudRunDeployConfig,
    CloudSQLDeployConfig,
    MemorystoreRedisDeployConfig,
)
from skaal.deploy.wiring import resolve_backend

if TYPE_CHECKING:
    from skaal.plan import PlanFile


def _gcp_openapi_path(path: str) -> str:
    """Convert a Skaal wildcard path to OpenAPI 2.0 path format used by GCP."""
    if path in ("/*", "*"):
        return "/{proxy}"
    if path.endswith("/*"):
        return path[:-2] + "/{proxy}"
    if path.endswith("*"):
        return path[:-1] + "{proxy}"
    return path


def _build_openapi_spec(
    app_name: str,
    routes: list[dict[str, Any]],
    auth: dict[str, Any] | None,
    cloud_run_url_ref: str,
) -> dict[str, Any]:
    """Build the OpenAPI doc as a deploy-time Pulumi interpolation."""
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
        openapi_path = _gcp_openapi_path(route["path"])
        safe_operation = re.sub(r"[^a-z0-9_]", "_", openapi_path.lower()).strip("_") or "root"
        has_proxy = "{proxy}" in openapi_path

        parts.append(f'  "{openapi_path}":\n')
        for method in route.get("methods") or ["get", "post"]:
            lower_method = method.lower()
            parts.append(f"    {lower_method}:\n")
            parts.append(f'      operationId: "{safe_operation}_{lower_method}"\n')
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


def _add_gcp_api_gateway(
    app: Any,
    plan: "PlanFile",
    resources: dict[str, Any],
    outputs: dict[str, Any],
) -> None:
    """Add GCP API Gateway resources when a proxy / api-gateway component exists."""
    gw_comp = next(
        (c for c in plan.components.values() if c.kind in ("proxy", "api-gateway")),
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
                {"path": prefix.rstrip("/") + "/*", "target": ns, "methods": ["GET", "POST"]}
                for ns, prefix in mounts.items()
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


def _build_pulumi_stack(
    app: Any,
    plan: "PlanFile",
    region: str,
    stack_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the Pulumi stack as a plain Python dict."""
    deploy = CloudRunDeployConfig.model_validate(plan.deploy_config)
    needs_vpc = any(
        resolve_backend(spec, target="gcp").requires_vpc for spec in plan.storage.values()
    )

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

    for qname, spec in plan.storage.items():
        class_name = qname.split(".")[-1]
        if spec.backend in ("cloud-sql-postgres", "cloud-sql-pgvector"):
            sql_cfg = CloudSQLDeployConfig.model_validate(spec.deploy_params)
            config[f"sqlTier{class_name}"] = {"type": "string", "default": sql_cfg.tier}
            config[f"sqlDeletionProtection{class_name}"] = {
                "type": "boolean",
                "default": sql_cfg.deletion_protection,
            }
        elif spec.backend == "memorystore-redis":
            redis_cfg = MemorystoreRedisDeployConfig.model_validate(spec.deploy_params)
            config[f"redisSizeGb{class_name}"] = {
                "type": "integer",
                "default": redis_cfg.memory_size_gb,
            }

    resources: dict[str, Any] = {}
    resources["repo"] = {
        "type": "gcp:artifactregistry:Repository",
        "properties": {
            "repositoryId": f"${{pulumi.stack}}-{app.name}",
            "location": "${gcp:region}",
            "format": "DOCKER",
        },
    }

    container_envs: list[dict[str, str]] = []
    for qname, spec in plan.storage.items():
        class_name = qname.split(".")[-1]
        handler = resolve_backend(spec, target="gcp")
        env_var = handler.wiring.env_var(class_name) or ""

        if spec.backend == "firestore":
            collection = f"{app.name}-{class_name.lower()}"
            container_envs.append({"name": env_var, "value": collection})
        elif spec.backend in ("cloud-sql-postgres", "cloud-sql-pgvector"):
            sql_cfg = CloudSQLDeployConfig.model_validate(spec.deploy_params)
            resource_key = f"{class_name.lower()}-sql"
            resources[resource_key] = {
                "type": "gcp:sql:DatabaseInstance",
                "properties": {
                    "databaseVersion": sql_cfg.database_version,
                    "region": "${gcp:region}",
                    "settings": {
                        "tier": f"${{{f'sqlTier{class_name}'}}}",
                        "backupConfiguration": {"enabled": sql_cfg.backup_enabled},
                    },
                    "deletionProtection": f"${{sqlDeletionProtection{class_name}}}",
                },
            }
            resources[f"{class_name.lower()}-db"] = {
                "type": "gcp:sql:Database",
                "properties": {
                    "instance": f"${{{resource_key}.name}}",
                    "name": app.name,
                },
            }
            dsn = (
                f"postgresql://skaal@localhost/{app.name}"
                f"?host=/cloudsql/${{{resource_key}.connectionName}}"
            )
            container_envs.append({"name": env_var, "value": dsn})
        elif spec.backend == "memorystore-redis":
            redis_cfg = MemorystoreRedisDeployConfig.model_validate(spec.deploy_params)
            resource_key = f"{class_name.lower()}-redis"
            resources[resource_key] = {
                "type": "gcp:redis:Instance",
                "properties": {
                    "tier": redis_cfg.tier,
                    "memorySizeGb": f"${{{f'redisSizeGb{class_name}'}}}",
                    "region": "${gcp:region}",
                    "redisVersion": redis_cfg.redis_version,
                },
            }
            container_envs.append(
                {
                    "name": env_var,
                    "value": f"redis://${{{resource_key}.host}}:6379",
                }
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
        connector_ref = deploy.vpc_connector_name
        if connector_ref is None:
            resources["vpc-connector"] = {
                "type": "gcp:vpcaccess:Connector",
                "properties": {
                    "name": "${pulumi.stack}-connector",
                    "region": "${gcp:region}",
                    "ipCidrRange": deploy.vpc_connector_cidr,
                    "network": deploy.vpc_connector_network,
                },
            }
            connector_ref = "${vpc-connector.name}"
        service_annotations = {
            "run.googleapis.com/vpc-access-connector": connector_ref,
            "run.googleapis.com/vpc-access-egress": deploy.vpc_connector_egress,
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

    template: dict[str, Any] = {
        "metadata": template_metadata,
        "spec": {
            "containerConcurrency": "${cloudRunConcurrency}",
            "containers": [
                {
                    "image": image,
                    "envs": container_envs,
                    "resources": {
                        "limits": {
                            "memory": "${cloudRunMemory}",
                            "cpu": "${cloudRunCpu}",
                        },
                    },
                }
            ],
        },
    }

    resources["cloud-run-service"] = {
        "type": "gcp:cloudrun:Service",
        "properties": {
            "name": f"${{pulumi.stack}}-{app.name}",
            "location": "${gcp:region}",
            "template": template,
            "traffics": [{"percent": 100, "latestRevision": True}],
        },
    }

    if profile_invokers:
        invoker_members = profile_invokers
    elif deploy.allow_public_invoker:
        invoker_members = ["allUsers"]
    else:
        invoker_members = []

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
                    "uri": ("${" "cloud-run-service.statuses[0].url" "}" + f"/{target_fn}"),
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
    }
    _add_gcp_api_gateway(app, plan, resources, outputs)

    return {
        "name": f"skaal-{app.name}",
        "runtime": "yaml",
        "config": config,
        "resources": resources,
        "outputs": outputs,
    }
