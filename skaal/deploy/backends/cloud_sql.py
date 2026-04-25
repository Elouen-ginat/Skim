from __future__ import annotations

from skaal.deploy.kinds import StorageKind
from skaal.deploy.plugin import BackendPlugin, Wiring

postgres_plugin = BackendPlugin(
    name="cloud-sql-postgres",
    kinds=frozenset({StorageKind.KV, StorageKind.RELATIONAL}),
    wiring=Wiring(
        class_name="PostgresBackend",
        module="postgres_backend",
        env_prefix="SKAAL_DB_DSN",
        uses_namespace=True,
        extra_deps=("cloud-sql-python-connector[asyncpg]>=1.9",),
        requires_vpc=True,
        local_service="postgres",
        local_env_value="postgresql://skaal_user:skaal_pass@postgres/skaal_db",
    ),
    supported_targets=frozenset({"gcp"}),
    local_fallbacks={
        StorageKind.KV: "local-redis",
        StorageKind.RELATIONAL: "sqlite",
    },
)

pgvector_plugin = BackendPlugin(
    name="cloud-sql-pgvector",
    kinds=frozenset({StorageKind.VECTOR}),
    wiring=Wiring(
        class_name="PgVectorBackend",
        module="pgvector_backend",
        env_prefix="SKAAL_DB_DSN",
        uses_namespace=True,
        extra_deps=(
            "cloud-sql-python-connector[asyncpg]>=1.9",
            "langchain-postgres>=0.0.17",
            "psycopg[binary]>=3.3",
        ),
        requires_vpc=True,
        local_service="postgres",
        local_env_value="postgresql://skaal_user:skaal_pass@postgres/skaal_db",
    ),
    supported_targets=frozenset({"gcp"}),
    local_fallbacks={StorageKind.VECTOR: "chroma-local"},
)
