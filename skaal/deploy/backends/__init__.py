from skaal.deploy.backends.chroma_local import plugin as chroma_local
from skaal.deploy.backends.cloud_sql import pgvector_plugin as cloud_sql_pgvector
from skaal.deploy.backends.cloud_sql import postgres_plugin as cloud_sql_postgres
from skaal.deploy.backends.dynamodb import plugin as dynamodb
from skaal.deploy.backends.firestore import plugin as firestore
from skaal.deploy.backends.local_map import plugin as local_map
from skaal.deploy.backends.memorystore_redis import plugin as memorystore_redis
from skaal.deploy.backends.rds_postgres import pgvector_plugin as rds_pgvector
from skaal.deploy.backends.rds_postgres import postgres_plugin as rds_postgres
from skaal.deploy.backends.redis_local import plugin as redis_local
from skaal.deploy.backends.sqlite_local import plugin as sqlite

BUILTIN_BACKENDS = (
    chroma_local,
    cloud_sql_pgvector,
    cloud_sql_postgres,
    dynamodb,
    firestore,
    local_map,
    memorystore_redis,
    redis_local,
    rds_pgvector,
    rds_postgres,
    sqlite,
)

__all__ = ["BUILTIN_BACKENDS"]
