"""GCP Cloud Run artifact generator."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from skaal.deploy._backends import build_wiring, get_handler
from skaal.deploy._deps import collect_user_packages
from skaal.deploy._render import render, to_pulumi_yaml, to_pyproject_toml
from skaal.deploy.config import (
    CloudRunDeployConfig,
    CloudSQLDeployConfig,
    MemorystoreRedisDeployConfig,
)
from skaal.deploy.push import write_meta

if TYPE_CHECKING:
    from skaal.plan import PlanFile


# ── Pulumi YAML stack builder ─────────────────────────────────────────────────


def _build_pulumi_stack(app: Any, plan: "PlanFile", region: str) -> dict[str, Any]:
    """Return the Pulumi stack as a plain Python dict.

    All provisioning parameters come from ``plan.storage[qname].deploy_params``
    and ``plan.deploy_config``, sourced from the catalog's
    ``[storage/compute.X.deploy]`` sections when the plan was solved.

    User-overridable parameters are exposed as Pulumi ``config:`` entries so
    that ``pulumi config set cloudRunMemory 1Gi`` works without re-planning.
    """
    deploy = CloudRunDeployConfig.model_validate(plan.deploy_config)
    needs_vpc = any(get_handler(s).requires_vpc for s in plan.storage.values())

    # ── Pulumi config (user-overridable) ──────────────────────────────────────
    config: dict[str, Any] = {
        "gcp:project": {"type": "string"},
        "gcp:region": {"type": "string", "default": region},
        "cloudRunMemory": {"type": "string", "default": deploy.memory},
        "cloudRunCpu": {"type": "string", "default": deploy.cpu},
        "cloudRunConcurrency": {"type": "integer", "default": deploy.concurrency},
        "cloudRunMinInstances": {"type": "integer", "default": deploy.min_instances},
        "cloudRunMaxInstances": {"type": "integer", "default": deploy.max_instances},
    }

    # Per-storage overridable params validated via typed config models.
    for qname, spec in plan.storage.items():
        class_name = qname.split(".")[-1]
        if spec.backend == "cloud-sql-postgres":
            sql_cfg = CloudSQLDeployConfig.model_validate(spec.deploy_params)
            config[f"sqlTier{class_name}"] = {"type": "string", "default": sql_cfg.tier}
        elif spec.backend == "memorystore-redis":
            redis_cfg = MemorystoreRedisDeployConfig.model_validate(spec.deploy_params)
            config[f"redisSizeGb{class_name}"] = {
                "type": "integer",
                "default": redis_cfg.memory_size_gb,
            }

    resources: dict[str, Any] = {}

    # ── Artifact Registry repo ────────────────────────────────────────────────
    resources["repo"] = {
        "type": "gcp:artifactregistry:Repository",
        "properties": {
            "repositoryId": f"${{pulumi.stack}}-{app.name}",
            "location": "${gcp:region}",
            "format": "DOCKER",
        },
    }

    # ── Per-storage resources ─────────────────────────────────────────────────
    container_envs: list[dict[str, str]] = []

    for qname, spec in plan.storage.items():
        class_name = qname.split(".")[-1]
        handler = get_handler(spec)
        env_var = f"{handler.env_prefix}_{class_name.upper()}"

        if spec.backend == "firestore":
            collection = f"{app.name}-{class_name.lower()}"
            container_envs.append({"name": env_var, "value": collection})

        elif spec.backend == "cloud-sql-postgres":
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
                    "deletionProtection": sql_cfg.deletion_protection,
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

    # ── VPC connector (required by Cloud SQL and Memorystore) ─────────────────
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

    # ── Cloud Run service ─────────────────────────────────────────────────────
    image = f"${{gcp:region}}-docker.pkg.dev/${{gcp:project}}/${{repo.name}}/{app.name}:latest"
    scaling_annotations = {
        "autoscaling.knative.dev/minScale": "${cloudRunMinInstances}",
        "autoscaling.knative.dev/maxScale": "${cloudRunMaxInstances}",
    }
    template_annotations = {**scaling_annotations, **service_annotations}

    template: dict[str, Any] = {
        "metadata": {"annotations": template_annotations},
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

    resources["public-invoker"] = {
        "type": "gcp:cloudrun:IamMember",
        "properties": {
            "service": "${cloud-run-service.name}",
            "location": "${gcp:region}",
            "role": "roles/run.invoker",
            "member": "allUsers",
        },
    }

    return {
        "name": f"skaal-{app.name}",
        "runtime": "yaml",
        "config": config,
        "resources": resources,
        "outputs": {
            "serviceUrl": "${cloud-run-service.statuses[0].url}",
            "imageRepository": "${repo.name}",
        },
    }


# ── Public entry point ─────────────────────────────────────────────────────────


def generate_artifacts(
    app: Any,
    plan: "PlanFile",
    output_dir: Path,
    source_module: str,
    app_var: str = "app",
    region: str = "us-central1",
) -> list[Path]:
    """Generate Cloud Run + Pulumi YAML deployment artifacts.

    Writes into *output_dir*:

    - ``main.py``        — Cloud Run entry point (rendered from template)
    - ``Dockerfile``     — Container build spec (rendered from template)
    - ``pyproject.toml`` — Python dependencies
    - ``Pulumi.yaml``    — Complete Pulumi stack (YAML runtime)
    - ``skaal-meta.json`` — Target metadata consumed by ``skaal deploy``

    All provisioning parameters (Cloud Run memory/CPU, SQL tier, Redis version)
    come from ``plan.deploy_config`` and ``plan.storage[*].deploy_params``.
    Override at deploy time with::

        pulumi config set cloudRunMemory 1Gi
        pulumi config set sqlTierItems db-g1-small

    Args:
        app:           The Skaal App instance.
        plan:          The solved PlanFile (``plan.skaal.lock``).
        output_dir:    Directory to write files into (created if absent).
        source_module: Python module path, e.g. ``"examples.counter"``.
        app_var:       Variable name of the App in the module, e.g. ``"app"``.
        region:        Default GCP region (default: ``"us-central1"``).

    Returns:
        List of generated :class:`~pathlib.Path` objects.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    generated: list[Path] = []
    backend_imports, backend_overrides = build_wiring(plan)
    wsgi_attribute: str | None = getattr(app, "_wsgi_attribute", None)

    # ── main.py ───────────────────────────────────────────────────────────────
    main_path = output_dir / "main.py"
    if wsgi_attribute:
        main_path.write_text(
            render(
                "gcp/main_wsgi.py",
                source_module=source_module,
                app_var=app_var,
                wsgi_attribute=wsgi_attribute,
                backend_imports=backend_imports,
                backend_overrides=backend_overrides,
            )
        )
    else:
        main_path.write_text(
            render(
                "gcp/main.py",
                app_name=app.name,
                source_module=source_module,
                app_var=app_var,
                backend_imports=backend_imports,
                backend_overrides=backend_overrides,
            )
        )
    generated.append(main_path)

    # ── Dockerfile ────────────────────────────────────────────────────────────
    dockerfile_path = output_dir / "Dockerfile"
    dockerfile_path.write_text(
        render("gcp/Dockerfile_wsgi") if wsgi_attribute else render("gcp/Dockerfile")
    )
    generated.append(dockerfile_path)

    # ── pyproject.toml ────────────────────────────────────────────────────────
    infra_deps: list[str] = ["skaal[gcp]", "gunicorn>=22.0" if wsgi_attribute else "uvicorn>=0.29"]
    for spec in plan.storage.values():
        infra_deps.extend(get_handler(spec).extra_deps)
    user_pkgs = collect_user_packages(source_module)
    deps = list(dict.fromkeys(infra_deps + user_pkgs))
    pyproject_path = output_dir / "pyproject.toml"
    pyproject_path.write_text(to_pyproject_toml(app.name, deps))
    generated.append(pyproject_path)

    # ── Pulumi.yaml ───────────────────────────────────────────────────────────
    pulumi_yaml_path = output_dir / "Pulumi.yaml"
    pulumi_yaml_path.write_text(to_pulumi_yaml(_build_pulumi_stack(app, plan, region)))
    generated.append(pulumi_yaml_path)

    # ── skaal-meta.json ───────────────────────────────────────────────────────
    meta_path = write_meta(output_dir, target="gcp", source_module=source_module, app_name=app.name)
    generated.append(meta_path)

    return generated
