from __future__ import annotations

from skaal.deploy.backends._postgres import postgres_kv_plugin, postgres_vector_plugin

postgres_plugin = postgres_kv_plugin(
    "cloud-sql-postgres",
    target="gcp",
    dependency_sets=("cloud-sql-connector",),
)

pgvector_plugin = postgres_vector_plugin(
    "cloud-sql-pgvector",
    target="gcp",
    dependency_sets=("cloud-sql-connector", "pgvector-runtime"),
)
