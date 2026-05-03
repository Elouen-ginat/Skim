"""Tests for vector storage backend selection."""

from __future__ import annotations

from pydantic import BaseModel

from skaal import App, VectorStore
from skaal.catalog.loader import load_catalog
from skaal.solver.solver import solve


class Passage(BaseModel):
    id: str
    title: str
    content: str


def _make_vector_app() -> App:
    app = App("vector-solver")

    @app.storage(kind="vector", dim=64, read_latency="< 60ms", durability="persistent")
    class Knowledge(VectorStore[Passage]):
        __skaal_vector_text_fields__ = ("title", "content")

    return app


def test_solve_local_vector_uses_chroma() -> None:
    app = _make_vector_app()
    catalog = load_catalog(target="local")

    plan = solve(app, catalog, target="local")
    spec = plan.storage["vector-solver.Knowledge"]

    assert spec.kind == "vector"
    assert spec.backend == "chroma-local"


def test_solve_gcp_vector_uses_cloud_sql_pgvector() -> None:
    app = _make_vector_app()
    catalog = load_catalog(target="gcp")

    plan = solve(app, catalog, target="gcp")
    spec = plan.storage["vector-solver.Knowledge"]

    assert spec.kind == "vector"
    assert spec.backend == "cloud-sql-pgvector"


def test_solve_aws_vector_uses_rds_pgvector() -> None:
    app = _make_vector_app()
    catalog = load_catalog(target="aws")

    plan = solve(app, catalog, target="aws")
    spec = plan.storage["vector-solver.Knowledge"]

    assert spec.kind == "vector"
    assert spec.backend == "rds-pgvector"
