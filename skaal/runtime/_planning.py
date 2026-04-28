from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypedDict

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


class BindParams(TypedDict, total=False):
    db_path: Path
    chroma_path: Path
    redis_url: str | None
    dsn: str | None
    min_size: int
    max_size: int


Binder = Callable[[BindParams], tuple[str, RuntimeWireParams]]

_BINDINGS: dict[tuple[RuntimeMode, StorageKindName], Binder] = {}


def _binding(mode: RuntimeMode, kind: StorageKindName) -> Callable[[Binder], Binder]:
    """Register a development storage binding.

    Example:
        @_binding("memory", "kv")
        def _memory_kv(_params: BindParams) -> tuple[str, RuntimeWireParams]:
            ...
    """

    def _decorate(fn: Binder) -> Binder:
        _BINDINGS[(mode, kind)] = fn
        return fn

    return _decorate


@_binding("memory", "kv")
def _memory_kv(_params: BindParams) -> tuple[str, RuntimeWireParams]:
    return "local-map", {
        "class_name": "LocalMap",
        "module": "skaal.backends.kv.local_map",
        "env_prefix": None,
        "path_default": None,
        "uses_namespace": False,
    }


@_binding("memory", "relational")
def _memory_relational(params: BindParams) -> tuple[str, RuntimeWireParams]:
    return "sqlite", {
        "env_prefix": None,
        "module": "skaal.backends.kv.sqlite",
        "path_default": str(params["db_path"]),
        "uses_namespace": True,
    }


@_binding("memory", "vector")
def _memory_vector(_params: BindParams) -> tuple[str, RuntimeWireParams]:
    return "chroma-local", {
        "module": "skaal.backends.vector.chroma",
        "path_default": "skaal_chroma",
        "uses_namespace": True,
    }


@_binding("sqlite", "kv")
def _sqlite_kv(params: BindParams) -> tuple[str, RuntimeWireParams]:
    return "sqlite", {
        "env_prefix": None,
        "module": "skaal.backends.kv.sqlite",
        "path_default": str(params["db_path"]),
        "uses_namespace": True,
    }


@_binding("sqlite", "relational")
def _sqlite_relational(params: BindParams) -> tuple[str, RuntimeWireParams]:
    return "sqlite", {
        "env_prefix": None,
        "module": "skaal.backends.kv.sqlite",
        "path_default": str(params["db_path"]),
        "uses_namespace": True,
    }


@_binding("sqlite", "vector")
def _sqlite_vector(params: BindParams) -> tuple[str, RuntimeWireParams]:
    return "chroma-local", {
        "module": "skaal.backends.vector.chroma",
        "path_default": str(params["chroma_path"]),
        "uses_namespace": True,
    }


def _unsupported_redis_model() -> ValueError:
    return ValueError(
        "LocalRuntime.from_redis() does not support @app.relational or @app.vector models."
    )


@_binding("redis", "kv")
def _redis_kv(params: BindParams) -> tuple[str, RuntimeWireParams]:
    redis_url = params.get("redis_url")
    if not redis_url:
        raise ValueError("redis_url is required when mode='redis'.")
    return "local-redis", {
        "env_prefix": None,
        "connection_value": redis_url,
        "module": "skaal.backends.kv.redis",
        "uses_namespace": True,
    }


@_binding("redis", "relational")
def _redis_relational(_params: BindParams) -> tuple[str, RuntimeWireParams]:
    raise _unsupported_redis_model()


@_binding("redis", "vector")
def _redis_vector(_params: BindParams) -> tuple[str, RuntimeWireParams]:
    raise _unsupported_redis_model()


def _postgres_params(params: BindParams) -> str:
    dsn = params.get("dsn")
    if not dsn:
        raise ValueError("dsn is required when mode='postgres'.")
    return dsn


@_binding("postgres", "kv")
def _postgres_kv(params: BindParams) -> tuple[str, RuntimeWireParams]:
    return "rds-postgres", {
        "env_prefix": None,
        "connection_value": _postgres_params(params),
        "module": "skaal.backends.kv.postgres",
        "uses_namespace": True,
        "constructor_kwargs": {
            "min_size": params["min_size"],
            "max_size": params["max_size"],
        },
    }


@_binding("postgres", "relational")
def _postgres_relational(params: BindParams) -> tuple[str, RuntimeWireParams]:
    return "rds-postgres", {
        "env_prefix": None,
        "connection_value": _postgres_params(params),
        "module": "skaal.backends.kv.postgres",
        "uses_namespace": True,
        "constructor_kwargs": {
            "min_size": params["min_size"],
            "max_size": params["max_size"],
        },
    }


@_binding("postgres", "vector")
def _postgres_vector(params: BindParams) -> tuple[str, RuntimeWireParams]:
    return "rds-pgvector", {
        "env_prefix": None,
        "connection_value": _postgres_params(params),
        "module": "skaal.backends.vector.pgvector",
        "uses_namespace": True,
    }


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
            kind = "kv"

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
    binder = _BINDINGS.get((mode, kind))
    if binder is None:
        raise ValueError(
            f"No development binding for mode={mode!r} and kind={kind!r}. "
            f"Supported pairs: {sorted(_BINDINGS)}"
        )
    return binder(
        {
            "db_path": db_path,
            "chroma_path": chroma_path,
            "redis_url": redis_url,
            "dsn": dsn,
            "min_size": min_size,
            "max_size": max_size,
        }
    )
