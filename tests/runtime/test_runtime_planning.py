from __future__ import annotations

from pathlib import Path

import pytest

from skaal.runtime._planning import _BINDINGS, _development_storage_binding


def test_binding_table_covers_all_runtime_mode_kind_pairs() -> None:
    assert set(_BINDINGS) == {
        ("memory", "kv"),
        ("memory", "relational"),
        ("memory", "vector"),
        ("sqlite", "kv"),
        ("sqlite", "relational"),
        ("sqlite", "vector"),
        ("redis", "kv"),
        ("redis", "relational"),
        ("redis", "vector"),
        ("postgres", "kv"),
        ("postgres", "relational"),
        ("postgres", "vector"),
    }


def test_redis_relational_binding_is_explicitly_rejected() -> None:
    with pytest.raises(ValueError, match="does not support"):
        _development_storage_binding(
            "relational",
            mode="redis",
            db_path=Path("skaal_local.db"),
            chroma_path=Path("skaal_chroma"),
            redis_url="redis://localhost:6379/0",
            dsn=None,
            min_size=1,
            max_size=5,
        )


def test_redis_binding_requires_url() -> None:
    with pytest.raises(ValueError, match="redis_url is required"):
        _development_storage_binding(
            "kv",
            mode="redis",
            db_path=Path("skaal_local.db"),
            chroma_path=Path("skaal_chroma"),
            redis_url=None,
            dsn=None,
            min_size=1,
            max_size=5,
        )


def test_postgres_binding_requires_dsn() -> None:
    with pytest.raises(ValueError, match="dsn is required"):
        _development_storage_binding(
            "kv",
            mode="postgres",
            db_path=Path("skaal_local.db"),
            chroma_path=Path("skaal_chroma"),
            redis_url=None,
            dsn=None,
            min_size=1,
            max_size=5,
        )
