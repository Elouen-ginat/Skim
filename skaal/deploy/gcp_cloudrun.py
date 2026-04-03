"""GCP Cloud Run artifact generator — thin orchestrator over deploy/templates/gcp/."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

from skaal.deploy._render import render, to_pulumi_yaml

if TYPE_CHECKING:
    from skaal.plan import PlanFile


# ── Per-backend metadata ───────────────────────────────────────────────────────

_BACKENDS: dict[str, dict[str, str]] = {
    "firestore": {
        "class": "FirestoreBackend",
        "import": "from skaal.backends.firestore_backend import FirestoreBackend",
        "env_prefix": "SKAAL_COLLECTION",
    },
    "cloud-sql-postgres": {
        "class": "PostgresBackend",
        "import": "from skaal.backends.postgres_backend import PostgresBackend",
        "env_prefix": "SKAAL_DB_DSN",
    },
    "memorystore-redis": {
        "class": "RedisBackend",
        "import": "from skaal.backends.redis_backend import RedisBackend",
        "env_prefix": "SKAAL_REDIS_URL",
    },
}

_REQUIRES_VPC = {"cloud-sql-postgres", "memorystore-redis"}


def _backend_meta(backend_name: str) -> dict[str, str]:
    return _BACKENDS.get(backend_name, _BACKENDS["firestore"])


def _env_var(backend_name: str, class_name: str) -> str:
    return f"{_backend_meta(backend_name)['env_prefix']}_{class_name.upper()}"


def _constructor(backend_name: str, class_name: str, env_var: str) -> str:
    cls = _backend_meta(backend_name)["class"]
    if backend_name in ("cloud-sql-postgres", "memorystore-redis"):
        return f'{cls}(os.environ["{env_var}"], namespace="{class_name}")'
    return f'{cls}(os.environ["{env_var}"])'


# ── Wiring helpers (backend_imports / backend_overrides for entry-point template)

def _build_wiring(plan: "PlanFile") -> tuple[str, str]:
    """
    Return ``(backend_imports, backend_overrides)`` template variables.

    backend_imports  — one import line per unique backend class used.
    backend_overrides — ``"ClassName": BackendInstance(...)`` lines, indented
                        to fit inside a dict literal with 8-space indent.
    """
    seen: set[str] = set()
    import_lines: list[str] = []
    override_lines: list[str] = []

    for qname, spec in plan.storage.items():
        class_name = qname.split(".")[-1]
        meta = _backend_meta(spec.backend)
        env_var = _env_var(spec.backend, class_name)

        if meta["import"] not in seen:
            seen.add(meta["import"])
            import_lines.append(meta["import"])

        ctor = _constructor(spec.backend, class_name, env_var)
        override_lines.append(f'        "{class_name}": {ctor},')

    return "\n".join(import_lines), "\n".join(override_lines)


# ── Pulumi YAML stack builder ─────────────────────────────────────────────────

def _build_pulumi_stack(
    app: Any, plan: "PlanFile", region: str
) -> dict[str, Any]:
    """
    Return the Pulumi stack as a plain Python dict.

    The dict is serialised to ``Pulumi.yaml`` by ``to_pulumi_yaml()``.
    All provisioning parameters come from ``plan.storage[qname].deploy_params``
    and ``plan.deploy_config`` — values sourced from the catalog's
    ``[storage/compute.X.deploy]`` sections when the plan was solved.

    User-overridable parameters (Cloud Run memory/CPU, SQL tier, Redis size)
    are exposed as Pulumi ``config:`` entries with catalog-derived defaults so
    that ``pulumi config set cloudRunMemory 1Gi`` works without re-planning.
    """
    deploy = plan.deploy_config  # Cloud Run deploy params from catalog

    needs_vpc = any(s.backend in _REQUIRES_VPC for s in plan.storage.values())

    # ── Pulumi config (user-overridable) ──────────────────────────────────────
    config: dict[str, Any] = {
        "gcp:project": {"type": "string"},
        "gcp:region": {"type": "string", "default": region},
        "cloudRunMemory": {"type": "string", "default": deploy.get("memory", "512Mi")},
        "cloudRunCpu": {"type": "string", "default": deploy.get("cpu", "1000m")},
        "cloudRunConcurrency": {
            "type": "integer",
            "default": int(deploy.get("concurrency", 80)),
        },
    }

    # Per-storage overridable params
    for qname, spec in plan.storage.items():
        class_name = qname.split(".")[-1]
        d = spec.deploy_params
        if spec.backend == "cloud-sql-postgres":
            config[f"sqlTier{class_name}"] = {
                "type": "string",
                "default": d.get("tier", "db-f1-micro"),
            }
        elif spec.backend == "memorystore-redis":
            config[f"redisSizeGb{class_name}"] = {
                "type": "integer",
                "default": int(d.get("memory_size_gb", 1)),
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
        env_var = _env_var(spec.backend, class_name)
        d = spec.deploy_params

        if spec.backend == "firestore":
            collection = f"{app.name}-{class_name.lower()}"
            # Firestore is serverless — no Pulumi resource needed, just pass
            # the collection name as a static env var.
            container_envs.append({"name": env_var, "value": collection})

        elif spec.backend == "cloud-sql-postgres":
            resource_key = f"{class_name.lower()}-sql"
            db_key = f"{class_name.lower()}-db"
            resources[resource_key] = {
                "type": "gcp:sql:DatabaseInstance",
                "properties": {
                    "databaseVersion": d.get("database_version", "POSTGRES_16"),
                    "region": "${gcp:region}",
                    "settings": {
                        "tier": f"${{{f'sqlTier{class_name}'}}}",
                        "backupConfiguration": {
                            "enabled": d.get("backup_enabled", "true") == "true",
                        },
                    },
                    "deletionProtection": d.get("deletion_protection", "false") == "true",
                },
            }
            resources[db_key] = {
                "type": "gcp:sql:Database",
                "properties": {
                    "instance": f"${{{resource_key}.name}}",
                    "name": app.name,
                },
            }
            # DSN uses Cloud SQL socket path; the Cloud SQL Auth Proxy handles auth.
            dsn = (
                f"postgresql://skaal@localhost/{app.name}"
                f"?host=/cloudsql/${{{resource_key}.connectionName}}"
            )
            container_envs.append({"name": env_var, "value": dsn})

        elif spec.backend == "memorystore-redis":
            resource_key = f"{class_name.lower()}-redis"
            resources[resource_key] = {
                "type": "gcp:redis:Instance",
                "properties": {
                    "tier": d.get("tier", "BASIC"),
                    "memorySizeGb": f"${{{f'redisSizeGb{class_name}'}}}",
                    "region": "${gcp:region}",
                    "redisVersion": d.get("redis_version", "REDIS_7_0"),
                },
            }
            container_envs.append({
                "name": env_var,
                "value": f"redis://${{{resource_key}.host}}:6379",
            })

    # ── VPC connector (required by Cloud SQL and Memorystore) ─────────────────
    service_annotations: dict[str, str] = {}
    if needs_vpc:
        resources["vpc-connector"] = {
            "type": "gcp:vpcaccess:Connector",
            "properties": {
                "name": f"${{pulumi.stack}}-connector",
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
    image = (
        f"${{gcp:region}}-docker.pkg.dev/${{gcp:project}}/${{repo.name}}/{app.name}:latest"
    )
    template: dict[str, Any] = {
        "spec": {
            "containerConcurrency": "${cloudRunConcurrency}",
            "containers": [{
                "image": image,
                "envs": container_envs,
                "resources": {
                    "limits": {
                        "memory": "${cloudRunMemory}",
                        "cpu": "${cloudRunCpu}",
                    },
                },
            }],
        },
    }
    if service_annotations:
        template["metadata"] = {"annotations": service_annotations}

    resources["cloud-run-service"] = {
        "type": "gcp:cloudrun:Service",
        "properties": {
            "name": f"${{pulumi.stack}}-{app.name}",
            "location": "${gcp:region}",
            "template": template,
            "traffics": [{"percent": 100, "latestRevision": True}],
        },
    }

    # Allow public (unauthenticated) access — restrict via IAM if needed
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
    """
    Generate Cloud Run + Pulumi YAML deployment artifacts.

    Writes into *output_dir*:

    - ``main.py``     — Cloud Run entry point (rendered from template)
    - ``Dockerfile``  — Container build spec (rendered from template)
    - ``requirements.txt`` — Python dependencies
    - ``Pulumi.yaml`` — Complete Pulumi stack (YAML runtime, no Python SDK needed)
    - ``README.md``   — Step-by-step deployment guide

    All provisioning parameters (Cloud Run memory/CPU, SQL tier, Redis version)
    come from ``plan.deploy_config`` and ``plan.storage[*].deploy_params``,
    which are populated by the solver from the catalog's
    ``[compute.cloud-run.deploy]`` and ``[storage.X.deploy]`` sections.
    Override any of them at deploy time with::

        pulumi config set cloudRunMemory 1Gi
        pulumi config set sqlTierItems db-g1-small

    Args:
        app:           The Skaal App instance.
        plan:          The solved PlanFile (``plan.skaal.lock``).
        output_dir:    Directory to write files into (created if absent).
        source_module: Python module path, e.g. ``"examples.counter"``.
        app_var:       Variable name of the App in the module, e.g. ``"app"``.
        region:        Default GCP region, e.g. ``"us-central1"``.

    Returns:
        List of generated :class:`~pathlib.Path` objects.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    generated: list[Path] = []
    backend_imports, backend_overrides = _build_wiring(plan)

    # ── main.py ───────────────────────────────────────────────────────────────
    main_path = output_dir / "main.py"
    main_path.write_text(render(
        "gcp/main.py",
        app_name=app.name,
        source_module=source_module,
        app_var=app_var,
        backend_imports=backend_imports,
        backend_overrides=backend_overrides,
    ))
    generated.append(main_path)

    # ── Dockerfile ────────────────────────────────────────────────────────────
    dockerfile_path = output_dir / "Dockerfile"
    dockerfile_path.write_text(render("gcp/Dockerfile"))
    generated.append(dockerfile_path)

    # ── requirements.txt ──────────────────────────────────────────────────────
    deps = ["skaal[gcp]", "uvicorn>=0.29"]
    for spec in plan.storage.values():
        if spec.backend == "cloud-sql-postgres":
            deps.append("cloud-sql-python-connector[asyncpg]>=1.9")
    requirements_path = output_dir / "requirements.txt"
    requirements_path.write_text("\n".join(dict.fromkeys(deps)) + "\n")
    generated.append(requirements_path)

    # ── Pulumi.yaml ───────────────────────────────────────────────────────────
    pulumi_yaml_path = output_dir / "Pulumi.yaml"
    pulumi_yaml_path.write_text(to_pulumi_yaml(_build_pulumi_stack(app, plan, region)))
    generated.append(pulumi_yaml_path)

    # ── README.md ─────────────────────────────────────────────────────────────
    resource_lines = [
        f"- `{spec.backend}` backend for `{qname.split('.')[-1]}`"
        for qname, spec in plan.storage.items()
    ]
    resource_lines += ["- Cloud Run service", "- Artifact Registry repository"]

    readme_path = output_dir / "README.md"
    readme_path.write_text(textwrap.dedent(f"""\
        # Deploy {app.name} to GCP (Cloud Run)

        Generated by `skaal deploy`.

        ## Prerequisites

        - GCP project with billing enabled
        - `gcloud` CLI authenticated (`gcloud auth application-default login`)
        - Docker installed
        - Pulumi CLI: `pip install pulumi`

        ## Deploy

        ```bash
        PROJECT=your-gcp-project
        REGION={region}

        # 1. Provision infrastructure (creates Artifact Registry repo + storage)
        pulumi stack init dev
        pulumi config set gcp:project $PROJECT
        pulumi config set gcp:region $REGION
        pulumi up

        # 2. Build and push the container image
        REPO=$(pulumi stack output imageRepository)
        gcloud auth configure-docker $REGION-docker.pkg.dev
        docker build -t $REGION-docker.pkg.dev/$PROJECT/$REPO/{app.name}:latest .
        docker push $REGION-docker.pkg.dev/$PROJECT/$REPO/{app.name}:latest

        # 3. Re-run `pulumi up` to deploy the image to Cloud Run
        pulumi up
        ```

        ## Customise without re-planning

        ```bash
        pulumi config set cloudRunMemory 1Gi     # default from catalog
        pulumi config set cloudRunCpu 2000m
        pulumi config set cloudRunConcurrency 40
        # Per-storage overrides (replace Items with your class name):
        pulumi config set sqlTierItems db-g1-small
        pulumi config set redisSizeGbCache 2
        ```

        ## Resources

        {chr(10).join(resource_lines)}

        ## Notes

        - The Cloud Run service is public (unauthenticated). Add IAM bindings to restrict access.
        - Firestore collections are created automatically on first write.
        - Use Workload Identity or `GOOGLE_APPLICATION_CREDENTIALS` for auth in CI.
    """))
    generated.append(readme_path)

    return generated
