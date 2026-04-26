from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

from skaal.backends._spec import BackendSpec, Wiring
from skaal.deploy.kinds import StorageKind
from skaal.vector import HashEmbeddings


def _collection_slug(namespace: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", namespace.lower()).strip("_")
    return slug or "skaal_vectors"


class ChromaVectorBackend:
    def __init__(
        self,
        path: str | Path = "skaal_chroma",
        namespace: str = "default",
        embeddings: Any | None = None,
    ) -> None:
        self.path = Path(path)
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

    def _collection_metadata(self) -> dict[str, str]:
        metric_map = {"cosine": "cosine", "l2": "l2", "inner": "ip"}
        return {"hnsw:space": metric_map.get(self._metric, "cosine")}

    def _ensure_store(self) -> None:
        if self._store is not None:
            return

        try:
            from langchain_chroma import Chroma
        except ImportError as exc:
            raise ImportError(
                "Chroma vector storage requires `langchain-chroma` and `chromadb`."
            ) from exc

        self.path.mkdir(parents=True, exist_ok=True)
        embeddings = self._embeddings or HashEmbeddings(self._dimensions)
        self._store = Chroma(
            collection_name=_collection_slug(self.namespace),
            embedding_function=cast(Any, embeddings),
            persist_directory=str(self.path),
            collection_metadata=self._collection_metadata(),
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

    async def adelete(self, ids: list[str] | None = None, **kwargs: Any) -> bool | None:
        self._ensure_store()
        return await self._store.adelete(ids=ids, **kwargs)

    async def close(self) -> None:
        self._store = None

    def __repr__(self) -> str:
        return f"ChromaVectorBackend(path={str(self.path)!r}, namespace={self.namespace!r})"


CHROMA_LOCAL_SPEC = BackendSpec(
    name="chroma-local",
    kinds=frozenset({StorageKind.VECTOR}),
    wiring=Wiring(
        class_name="ChromaVectorBackend",
        module="skaal.backends.vector.chroma",
        path_default="/app/data/chroma",
        uses_namespace=True,
        dependency_sets=("chroma-runtime",),
    ),
    supported_targets=frozenset({"local"}),
)

__all__ = ["CHROMA_LOCAL_SPEC", "ChromaVectorBackend"]
