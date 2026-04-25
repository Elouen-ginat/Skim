from __future__ import annotations

from skaal.deploy.kinds import StorageKind
from skaal.deploy.plugin import BackendPlugin, Wiring

_LOCAL_POSTGRES_DSN = "postgresql://skaal_user:skaal_pass@postgres/skaal_db"


def _postgres_plugin(
    name: str,
    *,
    target: str,
    kinds: frozenset[StorageKind],
    class_name: str,
    module: str,
    dependency_sets: tuple[str, ...],
    local_fallbacks: dict[StorageKind, str],
) -> BackendPlugin:
    return BackendPlugin(
        name=name,
        kinds=kinds,
        wiring=Wiring(
            class_name=class_name,
            module=module,
            env_prefix="SKAAL_DB_DSN",
            uses_namespace=True,
            dependency_sets=dependency_sets,
            requires_vpc=True,
            local_service="postgres",
            local_env_value=_LOCAL_POSTGRES_DSN,
        ),
        supported_targets=frozenset({target}),
        local_fallbacks=local_fallbacks,
    )


def postgres_kv_plugin(
    name: str,
    *,
    target: str,
    dependency_sets: tuple[str, ...],
) -> BackendPlugin:
    return _postgres_plugin(
        name,
        target=target,
        kinds=frozenset({StorageKind.KV, StorageKind.RELATIONAL}),
        class_name="PostgresBackend",
        module="postgres_backend",
        dependency_sets=dependency_sets,
        local_fallbacks={
            StorageKind.KV: "local-redis",
            StorageKind.RELATIONAL: "sqlite",
        },
    )


def postgres_vector_plugin(
    name: str,
    *,
    target: str,
    dependency_sets: tuple[str, ...],
) -> BackendPlugin:
    return _postgres_plugin(
        name,
        target=target,
        kinds=frozenset({StorageKind.VECTOR}),
        class_name="PgVectorBackend",
        module="pgvector_backend",
        dependency_sets=dependency_sets,
        local_fallbacks={StorageKind.VECTOR: "chroma-local"},
    )


__all__ = ["postgres_kv_plugin", "postgres_vector_plugin"]
