"""
Typed Pydantic models for deployment provisioning configuration.

These models live at the intersection of catalog and infrastructure:
- The catalog TOML's ``[storage/compute.X.deploy]`` sections are parsed into
  these models at catalog load time, giving early validation with clear errors.
- Deploy generators call the same factories to get typed attribute access
  instead of raw ``dict.get(key, default)`` calls.
- The solver never imports this module — deploy config is invisible to
  constraint solving.

Adding a new backend
--------------------
1. Subclass ``StorageDeployConfig`` (or ``ComputeDeployConfig``).
2. Add validators as needed.
3. Register the class in ``_STORAGE_CONFIGS`` (or ``_COMPUTE_CONFIGS``).
The factories below will route to it automatically.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# ── Base classes ──────────────────────────────────────────────────────────────

class StorageDeployConfig(BaseModel):
    """Base for storage backend deploy configs.  Unknown fields are ignored
    so that custom catalog entries don't break on forward-compat keys."""
    model_config = {"extra": "ignore"}


class ComputeDeployConfig(BaseModel):
    """Base for compute backend deploy configs."""
    model_config = {"extra": "ignore"}


# ── AWS storage ───────────────────────────────────────────────────────────────

class DynamoDBDeployConfig(StorageDeployConfig):
    billing_mode: Literal["PAY_PER_REQUEST", "PROVISIONED"] = "PAY_PER_REQUEST"
    hash_key: str = "pk"
    hash_key_type: Literal["S", "N", "B"] = "S"

    @field_validator("hash_key")
    @classmethod
    def _hash_key_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("hash_key must not be empty")
        return v


# ── GCP storage ───────────────────────────────────────────────────────────────

class FirestoreDeployConfig(StorageDeployConfig):
    """Firestore is serverless — no provisioning parameters required."""


class CloudSQLDeployConfig(StorageDeployConfig):
    database_version: str = "POSTGRES_16"
    tier: str = "db-f1-micro"
    backup_enabled: bool = True
    deletion_protection: bool = False

    @field_validator("database_version")
    @classmethod
    def _valid_db_version(cls, v: str) -> str:
        v = v.upper()
        known_prefixes = ("POSTGRES_", "MYSQL_", "SQLSERVER_")
        if not any(v.startswith(p) for p in known_prefixes):
            raise ValueError(
                f"Unrecognised database_version {v!r}. "
                "Expected one of POSTGRES_X, MYSQL_X, SQLSERVER_X."
            )
        return v

    @field_validator("tier")
    @classmethod
    def _valid_tier_format(cls, v: str) -> str:
        if not v.startswith("db-"):
            raise ValueError(
                f"Cloud SQL tier must start with 'db-', got {v!r}. "
                "Example: 'db-f1-micro', 'db-g1-small'."
            )
        return v


class MemorystoreRedisDeployConfig(StorageDeployConfig):
    redis_version: str = "REDIS_7_0"
    tier: Literal["BASIC", "STANDARD_HA"] = "BASIC"
    memory_size_gb: int = Field(default=1, ge=1, le=300)

    @field_validator("redis_version")
    @classmethod
    def _valid_redis_version(cls, v: str) -> str:
        v = v.upper()
        if not re.match(r"^REDIS_\d+_\d+$", v):
            raise ValueError(
                f"redis_version must match REDIS_<major>_<minor>, got {v!r}. "
                "Example: 'REDIS_7_0'."
            )
        return v


# ── AWS compute ───────────────────────────────────────────────────────────────

_LAMBDA_RUNTIMES = {
    "python3.8", "python3.9", "python3.10", "python3.11", "python3.12",
    "nodejs18.x", "nodejs20.x", "java11", "java17", "java21",
    "ruby3.2", "provided.al2", "provided.al2023",
}


class LambdaDeployConfig(ComputeDeployConfig):
    runtime: str = "python3.11"
    timeout: int = Field(
        default=30, ge=1, le=900,
        description="Function timeout in seconds. Lambda maximum is 900 (15 min).",
    )
    memory_mb: int = Field(
        default=256, ge=128, le=10240,
        description="Memory allocated to the function in MB. Must be a multiple of 64.",
    )

    @field_validator("runtime")
    @classmethod
    def _valid_runtime(cls, v: str) -> str:
        if v not in _LAMBDA_RUNTIMES:
            raise ValueError(
                f"Unknown Lambda runtime {v!r}. "
                f"Known values: {sorted(_LAMBDA_RUNTIMES)}."
            )
        return v

    @field_validator("memory_mb")
    @classmethod
    def _multiple_of_64(cls, v: int) -> int:
        if v % 64 != 0:
            raise ValueError(
                f"Lambda memory_mb must be a multiple of 64, got {v}. "
                "Valid examples: 128, 256, 512, 1024."
            )
        return v


# ── GCP compute ───────────────────────────────────────────────────────────────

_MEMORY_RE = re.compile(r"^\d+(\.\d+)?(Ki|Mi|Gi|K|M|G)$")
_CPU_RE = re.compile(r"^\d+(\.\d+)?m?$")


class CloudRunDeployConfig(ComputeDeployConfig):
    memory: str = "512Mi"
    cpu: str = "1000m"
    concurrency: int = Field(
        default=80, ge=1, le=1000,
        description="Max concurrent requests per container instance.",
    )

    @field_validator("memory")
    @classmethod
    def _valid_memory(cls, v: str) -> str:
        if not _MEMORY_RE.match(v):
            raise ValueError(
                f"memory must be a Kubernetes quantity like '512Mi' or '1Gi', got {v!r}."
            )
        return v

    @field_validator("cpu")
    @classmethod
    def _valid_cpu(cls, v: str) -> str:
        if not _CPU_RE.match(v):
            raise ValueError(
                f"cpu must be like '1000m' or '2', got {v!r}."
            )
        return v


# ── Registries + factories ────────────────────────────────────────────────────

_STORAGE_CONFIGS: dict[str, type[StorageDeployConfig]] = {
    "dynamodb": DynamoDBDeployConfig,
    "firestore": FirestoreDeployConfig,
    "cloud-sql-postgres": CloudSQLDeployConfig,
    "memorystore-redis": MemorystoreRedisDeployConfig,
}

_COMPUTE_CONFIGS: dict[str, type[ComputeDeployConfig]] = {
    # Canonical catalog keys
    "lambda": LambdaDeployConfig,
    "cloud-run": CloudRunDeployConfig,
    # Deploy-target aliases used by the solver / CLI
    "aws-lambda": LambdaDeployConfig,
    "aws": LambdaDeployConfig,
    "gcp-cloudrun": CloudRunDeployConfig,
    "gcp": CloudRunDeployConfig,
}


def storage_deploy_config(
    backend_name: str, params: dict[str, Any]
) -> StorageDeployConfig:
    """
    Parse and validate *params* as the deploy config for *backend_name*.

    Returns an instance of the registered subclass (e.g.
    :class:`DynamoDBDeployConfig` for ``"dynamodb"``).  Falls back to the
    base :class:`StorageDeployConfig` for unknown backends so that custom
    catalog entries forward-compat gracefully.

    Raises:
        ValueError: If the params fail model validation, with a message that
            names the backend and lists the offending fields.
    """
    cls = _STORAGE_CONFIGS.get(backend_name, StorageDeployConfig)
    try:
        return cls.model_validate(params)
    except Exception as exc:
        raise ValueError(
            f"Invalid [storage.{backend_name}.deploy] configuration: {exc}"
        ) from exc


def compute_deploy_config(
    target: str, params: dict[str, Any]
) -> ComputeDeployConfig:
    """
    Parse and validate *params* as the deploy config for *target*.

    Returns an instance of the registered subclass (e.g.
    :class:`LambdaDeployConfig` for ``"lambda"``/``"aws-lambda"``/``"aws"``).

    Raises:
        ValueError: If the params fail model validation.
    """
    cls = _COMPUTE_CONFIGS.get(target, ComputeDeployConfig)
    try:
        return cls.model_validate(params)
    except Exception as exc:
        raise ValueError(
            f"Invalid [compute.{target}.deploy] configuration: {exc}"
        ) from exc
