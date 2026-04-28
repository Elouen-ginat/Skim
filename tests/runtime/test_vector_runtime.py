"""Runtime wiring tests for vector stores."""

from __future__ import annotations

from pydantic import BaseModel

from skaal import App, VectorStore
from skaal.runtime.local import LocalRuntime


class Note(BaseModel):
    id: str
    text: str


def _make_vector_app() -> App:
    app = App("vector-runtime")

    @app.vector(dim=32, read_latency="< 20ms", durability="persistent")
    class Notes(VectorStore[Note]):
        pass

    return app


def test_from_sqlite_uses_chroma_for_vector_store(tmp_path) -> None:
    from skaal.backends.vector.chroma import ChromaVectorBackend

    rt = LocalRuntime.from_sqlite(_make_vector_app(), db_path=str(tmp_path / "runtime.db"))

    backend = rt._backends["vector-runtime.Notes"]
    assert isinstance(backend, ChromaVectorBackend)


def test_from_postgres_uses_pgvector_for_vector_store() -> None:
    from skaal.backends.vector.pgvector import PgVectorBackend

    rt = LocalRuntime.from_postgres(
        _make_vector_app(),
        dsn="postgresql://user:pass@localhost/vector_db",
    )

    backend = rt._backends["vector-runtime.Notes"]
    assert isinstance(backend, PgVectorBackend)
    assert backend._pgvector_dsn() == "postgresql+psycopg://user:pass@localhost/vector_db"
