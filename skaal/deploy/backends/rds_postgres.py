from __future__ import annotations

from skaal.deploy.backends._postgres import postgres_kv_plugin, postgres_vector_plugin

postgres_plugin = postgres_kv_plugin(
    "rds-postgres",
    target="aws",
    dependency_sets=("postgres-asyncpg",),
)

pgvector_plugin = postgres_vector_plugin(
    "rds-pgvector",
    target="aws",
    dependency_sets=("pgvector-runtime",),
)
