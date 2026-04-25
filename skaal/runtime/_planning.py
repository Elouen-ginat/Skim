from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from skaal.deploy.wiring import resolve_backend
from skaal.plan import PlanFile, StorageSpec
from skaal.types.runtime import (
    BackendOverrides,
    RuntimeApp,
    RuntimeMode,
    RuntimePlanSource,
    RuntimeWireParams,
    StorageKindName,
)


def coerce_runtime_plan(value: RuntimePlanSource) -> PlanFile:
    if isinstance(value, PlanFile):
        return value
    if isinstance(value, Mapping):
        return PlanFile.model_validate(dict(value))

    path = Path(value)
    if not path.exists():
        raise FileNotFoundError(f"Runtime plan not found at {path}.")
    return PlanFile.read(path)


def build_backend_overrides(
    app: RuntimeApp,
    plan: RuntimePlanSource,
    *,
    target: str | None = None,
) -> BackendOverrides:
    resolved_plan = coerce_runtime_plan(plan)
    overrides: BackendOverrides = {}

    for qname, obj in app._collect_all().items():
        if not (isinstance(obj, type) and hasattr(obj, "__skaal_storage__")):
            continue
        spec = resolved_plan.storage.get(qname)
        if spec is None:
            continue
        resource_name = qname.split(".")[-1]
        overrides[qname] = resolve_backend(spec, target=target).wiring.instantiate(resource_name)

    return overrides


def build_development_plan(
    app: RuntimeApp,
    *,
    mode: RuntimeMode,
    db_path: str | Path = "skaal_local.db",
    redis_url: str | None = None,
    dsn: str | None = None,
    min_size: int = 1,
    max_size: int = 5,
) -> PlanFile:
    from skaal.relational import is_relational_model
    from skaal.storage import Store
    from skaal.vector import VectorStore, is_vector_model

    storage: dict[str, StorageSpec] = {}
    resolved_db_path = Path(db_path)
    chroma_path = resolved_db_path.parent / f"{resolved_db_path.stem}_chroma"

    for qname, obj in app._collect_all().items():
        if not (isinstance(obj, type) and hasattr(obj, "__skaal_storage__")):
            continue

        kind: StorageKindName
        if is_relational_model(obj):
            kind = "relational"
        elif is_vector_model(obj) or issubclass(obj, VectorStore):
            kind = "vector"
        elif issubclass(obj, Store):
            kind = "kv"
        else:
            continue

        backend, wire_params = _development_storage_binding(
            kind,
            mode=mode,
            db_path=resolved_db_path,
            chroma_path=chroma_path,
            redis_url=redis_url,
            dsn=dsn,
            min_size=min_size,
            max_size=max_size,
        )
        storage[qname] = StorageSpec(
            variable_name=qname,
            backend=backend,
            kind=kind,
            wire_params=dict(wire_params),
        )

    return PlanFile(app_name=app.name, deploy_target="local", storage=storage)


def _development_storage_binding(
    kind: StorageKindName,
    *,
    mode: RuntimeMode,
    db_path: Path,
    chroma_path: Path,
    redis_url: str | None,
    dsn: str | None,
    min_size: int,
    max_size: int,
) -> tuple[str, RuntimeWireParams]:
    if mode == "memory":
        if kind == "kv":
            return "local-map", {
                "class_name": "LocalMap",
                "module": "local_backend",
                "env_prefix": None,
                "path_default": None,
                "uses_namespace": False,
            }
        if kind == "relational":
            return "sqlite", {
                "env_prefix": None,
                "path_default": str(db_path),
                "uses_namespace": True,
            }
        return "chroma-local", {
            "path_default": "skaal_chroma",
            "uses_namespace": True,
        }

    if mode == "sqlite":
        if kind == "vector":
            return "chroma-local", {
                "path_default": str(chroma_path),
                "uses_namespace": True,
            }
        return "sqlite", {
            "env_prefix": None,
            "path_default": str(db_path),
            "uses_namespace": True,
        }

    if mode == "redis":
        if kind != "kv":
            raise ValueError(
                "LocalRuntime.from_redis() does not support @app.relational or @app.vector models."
            )
        if not redis_url:
            raise ValueError("redis_url is required when mode='redis'.")
        return "local-redis", {
            "env_prefix": None,
            "connection_value": redis_url,
            "uses_namespace": True,
        }

    if not dsn:
        raise ValueError("dsn is required when mode='postgres'.")
    if kind == "vector":
        return "rds-pgvector", {
            "env_prefix": None,
            "connection_value": dsn,
            "uses_namespace": True,
        }
    return "rds-postgres", {
        "env_prefix": None,
        "connection_value": dsn,
        "uses_namespace": True,
        "constructor_kwargs": {"min_size": min_size, "max_size": max_size},
    }
