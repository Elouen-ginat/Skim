from __future__ import annotations

import re
from typing import Any, cast

from skaal.backends._spec import BackendSpec, Wiring
from skaal.deploy.kinds import StorageKind
from skaal.vector import HashEmbeddings

_LOCAL_POSTGRES_DSN = "postgresql://skaal_user:skaal_pass@postgres/skaal_db"


def _collection_slug(namespace: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", namespace.lower()).strip("_")
    return slug or "skaal_vectors"


class PgVectorBackend:
    def __init__(
        self,
        dsn: str,
        namespace: str = "default",
        embeddings: Any | None = None,
    ) -> None:
        self.dsn = dsn
        self.namespace = namespace
        self._dimensions = 1536
        self._metric = "cosine"
        self._embeddings = embeddings
        self._store: Any = None

    def configure(
        self,
        *,
        dimensions: int,
        metric: str,
        model_type: type | None = None,
        embeddings: Any | None = None,
    ) -> None:
        self._dimensions = dimensions
        self._metric = metric
        if embeddings is not None:
            self._embeddings = embeddings
        self._store = None

    def _pgvector_dsn(self) -> str:
        if self.dsn.startswith("postgresql+psycopg://"):
            return self.dsn
        if self.dsn.startswith("postgresql+asyncpg://"):
            return "postgresql+psycopg://" + self.dsn[len("postgresql+asyncpg://") :]
        if self.dsn.startswith("postgresql://"):
            return "postgresql+psycopg://" + self.dsn[len("postgresql://") :]
        if self.dsn.startswith("postgres://"):
            return "postgresql+psycopg://" + self.dsn[len("postgres://") :]
        return self.dsn

    def _distance_strategy(self) -> Any:
        try:
            from langchain_postgres.vectorstores import DistanceStrategy
        except ImportError as exc:
            raise ImportError("PGVector storage requires `langchain-postgres`.") from exc

        if self._metric == "l2":
            return DistanceStrategy.EUCLIDEAN
        if self._metric == "inner":
            return DistanceStrategy.MAX_INNER_PRODUCT
        return DistanceStrategy.COSINE

    def _ensure_store(self) -> None:
        if self._store is not None:
            return

        try:
            from langchain_postgres import PGVector
        except ImportError as exc:
            raise ImportError(
                "pgvector storage requires `langchain-postgres` and `psycopg[binary]`."
            ) from exc

        embeddings = self._embeddings or HashEmbeddings(self._dimensions)
        self._store = PGVector(
            embeddings=cast(Any, embeddings),
            connection=self._pgvector_dsn(),
            collection_name=_collection_slug(self.namespace),
            embedding_length=self._dimensions,
            distance_strategy=self._distance_strategy(),
            create_extension=True,
            async_mode=True,
            use_jsonb=True,
        )

    async def aadd_documents(self, documents: list[Any], **kwargs: Any) -> list[str]:
        self._ensure_store()
        return await self._store.aadd_documents(documents, **kwargs)

    async def asimilarity_search(
        self,
        query: str,
        *,
        k: int = 4,
        filter: dict[str, Any] | None = None,
    ) -> list[Any]:
        self._ensure_store()
        kwargs: dict[str, Any] = {"k": k}
        if filter is not None:
            kwargs["filter"] = filter
        return await self._store.asimilarity_search(query, **kwargs)

    async def asimilarity_search_with_score(
        self,
        query: str,
        *,
        k: int = 4,
        filter: dict[str, Any] | None = None,
    ) -> list[tuple[Any, float]]:
        self._ensure_store()
        kwargs: dict[str, Any] = {"k": k}
        if filter is not None:
            kwargs["filter"] = filter
        return await self._store.asimilarity_search_with_score(query, **kwargs)

    async def adelete(self, ids: list[str] | None = None, **kwargs: Any) -> None:
        self._ensure_store()
        await self._store.adelete(ids=ids, **kwargs)

    async def close(self) -> None:
        if self._store is None:
            return
        async_engine = getattr(self._store, "_async_engine", None)
        if async_engine is not None:
            await async_engine.dispose()
        self._store = None

    def __repr__(self) -> str:
        return f"PgVectorBackend(dsn={self.dsn!r}, namespace={self.namespace!r})"


RDS_PGVECTOR_SPEC = BackendSpec(
    name="rds-pgvector",
    kinds=frozenset({StorageKind.VECTOR}),
    wiring=Wiring(
        class_name="PgVectorBackend",
        module="skaal.backends.vector.pgvector",
        env_prefix="SKAAL_DB_DSN",
        uses_namespace=True,
        dependency_sets=("pgvector-runtime",),
        requires_vpc=True,
        local_service="postgres",
        local_env_value=_LOCAL_POSTGRES_DSN,
    ),
    supported_targets=frozenset({"aws"}),
    local_fallbacks={StorageKind.VECTOR: "chroma-local"},
)

CLOUD_SQL_PGVECTOR_SPEC = BackendSpec(
    name="cloud-sql-pgvector",
    kinds=frozenset({StorageKind.VECTOR}),
    wiring=Wiring(
        class_name="PgVectorBackend",
        module="skaal.backends.vector.pgvector",
        env_prefix="SKAAL_DB_DSN",
        uses_namespace=True,
        dependency_sets=("cloud-sql-connector", "pgvector-runtime"),
        requires_vpc=True,
        local_service="postgres",
        local_env_value=_LOCAL_POSTGRES_DSN,
    ),
    supported_targets=frozenset({"gcp"}),
    local_fallbacks={StorageKind.VECTOR: "chroma-local"},
)

__all__ = ["CLOUD_SQL_PGVECTOR_SPEC", "PgVectorBackend", "RDS_PGVECTOR_SPEC"]
