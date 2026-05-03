"""Tests for ADR 022 — `[skaal] extends` / `remove` resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from skaal.catalog.loader import load_catalog, load_catalog_with_sources
from skaal.errors import CatalogError
from skaal.types import CatalogSource


def _write(p: Path, body: str) -> Path:
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def base_catalog(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "base.toml",
        """
[storage.sqlite]
display_name = "SQLite"
read_latency  = { min = 0.1, max = 5.0, unit = "ms" }
write_latency = { min = 0.5, max = 20.0, unit = "ms" }
durability    = ["persistent"]
storage_kinds = ["kv"]
access_patterns = ["random-read", "random-write"]
cost_per_gb_month = 0.0

[storage.redis]
display_name = "Redis"
read_latency  = { min = 0.1, max = 2.0, unit = "ms" }
write_latency = { min = 0.1, max = 5.0, unit = "ms" }
durability    = ["ephemeral"]
storage_kinds = ["kv"]
access_patterns = ["random-read", "random-write"]
cost_per_gb_month = 1.0
""",
    )


def test_extends_chains_via_relative_path(tmp_path: Path, base_catalog: Path) -> None:
    """Child file overlays cleanly on a relative-path parent."""
    child = _write(
        tmp_path / "dev.toml",
        """
[skaal]
extends = "base.toml"

[storage.sqlite]
display_name = "SQLite (dev override)"
read_latency  = { min = 0.1, max = 50.0, unit = "ms" }
write_latency = { min = 0.5, max = 100.0, unit = "ms" }
durability    = ["persistent"]
storage_kinds = ["kv"]
access_patterns = ["random-read", "random-write"]
cost_per_gb_month = 0.0
""",
    )
    merged = load_catalog(child)
    assert merged["storage"]["sqlite"]["display_name"] == "SQLite (dev override)"
    # Parent's redis is preserved untouched.
    assert merged["storage"]["redis"]["display_name"] == "Redis"
    # Reserved table is stripped.
    assert "skaal" not in merged


def test_extends_chains_three_levels(tmp_path: Path, base_catalog: Path) -> None:
    mid = _write(
        tmp_path / "mid.toml",
        """
[skaal]
extends = "base.toml"

[storage.sqlite]
display_name = "SQLite (mid)"
read_latency  = { min = 0.1, max = 5.0, unit = "ms" }
write_latency = { min = 0.5, max = 20.0, unit = "ms" }
durability    = ["persistent"]
storage_kinds = ["kv"]
access_patterns = ["random-read"]
cost_per_gb_month = 0.0
""",
    )
    leaf = _write(
        tmp_path / "leaf.toml",
        """
[skaal]
extends = "mid.toml"
""",
    )
    source = load_catalog_with_sources(leaf)
    chain = source.chain()
    assert [n.path for n in chain] == [base_catalog.resolve(), mid.resolve(), leaf.resolve()]


def test_remove_drops_a_parent_backend(tmp_path: Path, base_catalog: Path) -> None:
    child = _write(
        tmp_path / "lean.toml",
        """
[skaal]
extends = "base.toml"
remove = ["storage.redis"]
""",
    )
    merged = load_catalog(child)
    assert "redis" not in merged["storage"]
    assert "sqlite" in merged["storage"]


def test_remove_warns_and_continues_on_missing_entry(
    tmp_path: Path, base_catalog: Path, caplog: pytest.LogCaptureFixture
) -> None:
    child = _write(
        tmp_path / "lean.toml",
        """
[skaal]
extends = "base.toml"
remove = ["storage.nonexistent"]
""",
    )
    with caplog.at_level("WARNING", logger="skaal.catalog"):
        merged = load_catalog(child)
    assert "nothing to remove" in caplog.text
    assert "sqlite" in merged["storage"]


def test_remove_requires_dotted_path(tmp_path: Path, base_catalog: Path) -> None:
    child = _write(
        tmp_path / "bad.toml",
        """
[skaal]
extends = "base.toml"
remove = ["storage_sqlite"]
""",
    )
    with pytest.raises(CatalogError) as exc:
        load_catalog(child)
    assert "dotted" in str(exc.value)


def test_extends_cycle_raises_catalog_error(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.toml", '[skaal]\nextends = "b.toml"\n')
    _write(tmp_path / "b.toml", '[skaal]\nextends = "a.toml"\n')
    with pytest.raises(CatalogError) as exc:
        load_catalog(a)
    assert "circular extends" in str(exc.value)


def test_extends_missing_parent_raises_catalog_error(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.toml", '[skaal]\nextends = "missing.toml"\n')
    with pytest.raises(CatalogError) as exc:
        load_catalog(child)
    assert "does not resolve" in str(exc.value)


def test_extends_must_be_string(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.toml", "[skaal]\nextends = 7\n")
    with pytest.raises(CatalogError):
        load_catalog(child)


def test_load_catalog_with_sources_returns_typed_chain(tmp_path: Path, base_catalog: Path) -> None:
    child = _write(
        tmp_path / "dev.toml",
        '[skaal]\nextends = "base.toml"\nremove = ["storage.redis"]\n',
    )
    source = load_catalog_with_sources(child)
    assert isinstance(source, CatalogSource)
    assert source.removes == ("storage.redis",)
    assert source.parent is not None
    assert source.parent.path == base_catalog.resolve()


def test_no_extends_no_skaal_table_works(tmp_path: Path, base_catalog: Path) -> None:
    """Files without [skaal] still load — back-compat with all existing catalogs."""
    merged = load_catalog(base_catalog)
    assert "sqlite" in merged["storage"]
    assert "skaal" not in merged
