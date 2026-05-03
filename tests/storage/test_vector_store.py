"""Tests for typed vector storage backed by local Chroma."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from skaal import App, VectorStore
from skaal.runtime.local import LocalRuntime


class Article(BaseModel):
    id: str
    title: str
    body: str
    topic: str


@pytest.mark.asyncio
async def test_vector_store_add_search_filter_and_delete(tmp_path) -> None:
    app = App("vector-store")

    @app.storage(
        kind="vector",
        dim=64,
        metric="cosine",
        read_latency="< 25ms",
        durability="persistent",
    )
    class Articles(VectorStore[Article]):
        __skaal_vector_text_fields__ = ("title", "body")

    LocalRuntime.from_sqlite(app, db_path=str(tmp_path / "skaal.db"))
    storage_meta = getattr(Articles, "__skaal_storage__")

    assert storage_meta["kind"] == "vector"
    assert storage_meta["schema"]["dimensions"] == 64
    assert storage_meta["schema"]["metric"] == "cosine"

    await Articles.add(
        [
            Article(
                id="cats-guide",
                title="Cats at home",
                body="Caring for indoor cats and litter routines.",
                topic="pets",
            ),
            Article(
                id="dashboards",
                title="Task dashboards",
                body="Project status, work queues, and reporting widgets.",
                topic="productivity",
            ),
            Article(
                id="dogs-guide",
                title="Dogs and training",
                body="Leash basics, recall drills, and puppy schedules.",
                topic="pets",
            ),
        ]
    )

    filtered = await Articles.similarity_search("cats and litter", k=3, filter={"topic": "pets"})
    assert [item.id for item in filtered]
    assert filtered[0].id == "cats-guide"

    scored = await Articles.similarity_search_with_score("task reporting", k=1)
    assert scored[0][0].id == "dashboards"
    assert isinstance(scored[0][1], float)

    await Articles.delete(["cats-guide"])
    remaining = await Articles.similarity_search("cats and litter", k=3)
    assert all(item.id != "cats-guide" for item in remaining)

    await Articles.close()
