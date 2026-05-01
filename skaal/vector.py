"""Typed vector storage surface backed by LangChain vector stores."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from typing import Any, ClassVar, Generic, Sequence, TypeVar, cast, get_args, get_origin

from skaal.storage import _is_pydantic, _primary_key_field

T = TypeVar("T")
_VALID_VECTOR_METRICS = {"cosine", "l2", "inner"}
_TEXT_FIELD_PREFERENCE = ("text", "content", "body", "summary", "title", "description")


class HashEmbeddings:
    """Deterministic zero-dependency embeddings for local development and tests."""

    def __init__(self, dimensions: int = 1536) -> None:
        if dimensions <= 0:
            raise ValueError("HashEmbeddings dimensions must be > 0.")
        self.dimensions = dimensions

    def _embed(self, text: str) -> list[float]:
        tokens = re.findall(r"\w+", text.lower()) or [text.lower()]
        vector = [0.0] * self.dimensions
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            for index in range(0, len(digest), 4):
                chunk = digest[index : index + 4]
                bucket = int.from_bytes(chunk, "big") % self.dimensions
                sign = 1.0 if chunk[0] % 2 == 0 else -1.0
                vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


def _require_langchain_core() -> None:
    """Raise :class:`~skaal.errors.MissingExtraError` if the vector extra is missing."""
    from skaal.errors import MissingExtraError

    try:
        import langchain_core  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised when vector extra missing
        raise MissingExtraError(
            "Vector storage requires the 'vector' extra. "
            "Install it with `pip install 'skaal[vector]'`."
        ) from exc


def _string_like(annotation: Any) -> bool:
    if annotation is str:
        return True
    origin = get_origin(annotation)
    if origin is None:
        return False
    return any(arg is str for arg in get_args(annotation))


def validate_vector_model(store_cls: type) -> None:
    """Raise if *store_cls* is not a concrete ``VectorStore[T]`` model."""
    if not isinstance(store_cls, type) or not issubclass(store_cls, VectorStore):
        raise TypeError('@app.storage(kind="vector") requires a skaal.VectorStore subclass.')

    value_type = getattr(store_cls, "__skaal_value_type__", None)
    if value_type is None or not _is_pydantic(value_type):
        raise TypeError(
            '@app.storage(kind="vector") requires VectorStore[T] where T is a Pydantic model.'
        )

    metric = getattr(store_cls, "__skaal_vector_metric__", "cosine")
    if metric not in _VALID_VECTOR_METRICS:
        raise ValueError(
            f"Unsupported vector metric {metric!r}. Expected one of {sorted(_VALID_VECTOR_METRICS)}."
        )

    dim = getattr(store_cls, "__skaal_vector_dimensions__", 1536)
    if not isinstance(dim, int) or dim <= 0:
        raise ValueError("Vector store dimensions must be a positive integer.")


def is_vector_model(obj: Any) -> bool:
    """Return ``True`` if *obj* is a typed vector store registered with Skaal."""
    return (
        isinstance(obj, type)
        and hasattr(obj, "__skaal_storage__")
        and getattr(obj, "__skaal_storage__", {}).get("kind") == "vector"
    )


def _schema_hints(store_cls: type) -> dict[str, Any]:
    """Extract solver-visible hints from a ``VectorStore`` subclass."""
    validate_vector_model(store_cls)
    vector_store_cls = cast(type[VectorStore[Any]], store_cls)
    value_type = getattr(store_cls, "__skaal_value_type__", None)
    assert value_type is not None
    text_fields = list(vector_store_cls._resolve_text_fields())
    return {
        "model": value_type.__name__,
        "dimensions": getattr(store_cls, "__skaal_vector_dimensions__", 1536),
        "metric": getattr(store_cls, "__skaal_vector_metric__", "cosine"),
        "id_field": getattr(store_cls, "__skaal_key_field__", "id"),
        "text_fields": text_fields,
    }


async def _call_backend(
    backend: Any,
    async_name: str,
    sync_name: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    async_fn = getattr(backend, async_name, None)
    if callable(async_fn):
        return await async_fn(*args, **kwargs)

    sync_fn = getattr(backend, sync_name, None)
    if callable(sync_fn):
        return await asyncio.to_thread(sync_fn, *args, **kwargs)

    raise NotImplementedError(
        f"Vector backend {type(backend).__name__} does not implement {async_name} or {sync_name}."
    )


class VectorStore(Generic[T]):
    """Typed wrapper around a LangChain-compatible vector store backend."""

    __skaal_value_type__: ClassVar[type | None] = None
    __skaal_key_field__: ClassVar[str] = "id"
    __skaal_vector_dimensions__: ClassVar[int] = 1536
    __skaal_vector_metric__: ClassVar[str] = "cosine"
    __skaal_vector_text_fields__: ClassVar[tuple[str, ...] | None] = None
    __skaal_embeddings__: ClassVar[Any | None] = None
    _backend: ClassVar[Any | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for base in getattr(cls, "__orig_bases__", []):
            origin = get_origin(base)
            if origin is VectorStore or (
                isinstance(origin, type) and issubclass(origin, VectorStore)
            ):
                args = get_args(base)
                if args:
                    cls.__skaal_value_type__ = args[0]
                    if "__skaal_key_field__" not in cls.__dict__:
                        cls.__skaal_key_field__ = _primary_key_field(args[0])
                break

    @classmethod
    def wire(cls, backend: Any) -> None:
        """Bind *backend* to this vector store class."""
        configure = getattr(backend, "configure", None)
        if callable(configure):
            configure(
                dimensions=cls.__skaal_vector_dimensions__,
                metric=cls.__skaal_vector_metric__,
                model_type=cls.__skaal_value_type__,
                embeddings=getattr(cls, "__skaal_embeddings__", None),
            )
        cls._backend = backend

    @classmethod
    def _ensure_wired(cls) -> None:
        if cls._backend is None:
            raise NotImplementedError(
                f"{cls.__name__} vector store not wired. Use LocalRuntime or deploy first."
            )

    @classmethod
    def _coerce_item(cls, item: T | dict[str, Any]) -> T:
        validate_vector_model(cls)
        value_type = cls.__skaal_value_type__
        assert value_type is not None
        if isinstance(item, value_type):
            return cast(T, item)
        model_type = cast(Any, value_type)
        if isinstance(item, dict):
            return cast(T, model_type.model_validate(item))
        return cast(T, model_type.model_validate(item))

    @classmethod
    def _extract_id(cls, item: T) -> str:
        key_field = cls.__skaal_key_field__
        if hasattr(item, key_field):
            return str(getattr(item, key_field))
        raise ValueError(
            f"Cannot extract vector id field {key_field!r} from {type(item).__name__}."
        )

    @classmethod
    def _resolve_text_fields(cls) -> tuple[str, ...]:
        configured = getattr(cls, "__skaal_vector_text_fields__", None)
        if configured:
            return tuple(configured)

        value_type = cls.__skaal_value_type__
        if value_type is None or not hasattr(value_type, "model_fields"):
            return tuple()
        model_type = cast(Any, value_type)

        field_names = [
            name
            for name, field in model_type.model_fields.items()
            if name != cls.__skaal_key_field__ and _string_like(field.annotation)
        ]
        preferred = [name for name in _TEXT_FIELD_PREFERENCE if name in field_names]
        remaining = [name for name in field_names if name not in preferred]
        return tuple(preferred + remaining)

    @classmethod
    def _page_content(cls, payload: dict[str, Any]) -> str:
        chunks: list[str] = []
        for field_name in cls._resolve_text_fields():
            value = payload.get(field_name)
            if value:
                chunks.append(str(value))
        if chunks:
            return "\n".join(chunks)
        return json.dumps(payload, sort_keys=True, default=str)

    @classmethod
    def _metadata(cls, payload: dict[str, Any]) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "_skaal_payload": json.dumps(payload, sort_keys=True, default=str),
        }
        for key, value in payload.items():
            if key.startswith("_"):
                continue
            if isinstance(value, (str, int, float, bool)):
                metadata[key] = value
        return metadata

    @classmethod
    def _to_document(cls, item: T) -> Any:
        _require_langchain_core()
        from langchain_core.documents import Document

        payload = cast(Any, item).model_dump(mode="json")
        return Document(
            id=cls._extract_id(item),
            page_content=cls._page_content(payload),
            metadata=cls._metadata(payload),
        )

    @classmethod
    def _from_document(cls, document: Any) -> T:
        validate_vector_model(cls)
        value_type = cls.__skaal_value_type__
        assert value_type is not None
        model_type = cast(Any, value_type)
        metadata = getattr(document, "metadata", {}) or {}
        serialized = metadata.get("_skaal_payload")
        if isinstance(serialized, str):
            return cast(T, model_type.model_validate(json.loads(serialized)))
        return cast(T, model_type.model_validate(metadata))

    @classmethod
    async def add(cls, items: Sequence[T | dict[str, Any]]) -> list[str]:
        cls._ensure_wired()
        assert cls._backend is not None
        typed_items = [cls._coerce_item(item) for item in items]
        documents = [cls._to_document(item) for item in typed_items]
        ids = [cls._extract_id(item) for item in typed_items]
        result = await _call_backend(
            cls._backend, "aadd_documents", "add_documents", documents, ids=ids
        )
        return list(result or ids)

    @classmethod
    async def similarity_search(
        cls,
        query: str,
        *,
        k: int = 4,
        filter: dict[str, Any] | None = None,
    ) -> list[T]:
        cls._ensure_wired()
        assert cls._backend is not None
        kwargs: dict[str, Any] = {"k": k}
        if filter is not None:
            kwargs["filter"] = filter
        documents = await _call_backend(
            cls._backend,
            "asimilarity_search",
            "similarity_search",
            query,
            **kwargs,
        )
        return [cls._from_document(document) for document in documents]

    @classmethod
    async def similarity_search_with_score(
        cls,
        query: str,
        *,
        k: int = 4,
        filter: dict[str, Any] | None = None,
    ) -> list[tuple[T, float]]:
        cls._ensure_wired()
        assert cls._backend is not None
        kwargs: dict[str, Any] = {"k": k}
        if filter is not None:
            kwargs["filter"] = filter
        results = await _call_backend(
            cls._backend,
            "asimilarity_search_with_score",
            "similarity_search_with_score",
            query,
            **kwargs,
        )
        return [(cls._from_document(document), float(score)) for document, score in results]

    @classmethod
    async def delete(cls, ids: Sequence[str] | None = None) -> None:
        cls._ensure_wired()
        assert cls._backend is not None
        payload = list(ids) if ids is not None else None
        await _call_backend(cls._backend, "adelete", "delete", ids=payload)

    @classmethod
    async def close(cls) -> None:
        if cls._backend is not None and hasattr(cls._backend, "close"):
            await cls._backend.close()
