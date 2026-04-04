"""Tests for skaal.catalog — loader, models, registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from skaal.catalog.loader import load_catalog, load_typed_catalog
from skaal.catalog.models import (
    Catalog,
    ComputeBackendSpec,
    LatencyRange,
    StorageBackendSpec,
)
from skaal.catalog.registry import BackendRegistry

# ── Helpers ───────────────────────────────────────────────────────────────────


def _minimal_raw() -> dict:
    return {
        "storage": {
            "fast-redis": {
                "display_name": "Fast Redis",
                "read_latency": {"min": 0.1, "max": 2.0, "unit": "ms"},
                "write_latency": {"min": 0.1, "max": 5.0, "unit": "ms"},
                "durability": ["ephemeral", "persistent"],
                "access_patterns": ["random-read", "random-write"],
                "cost_per_gb_month": 3.5,
            },
            "slow-postgres": {
                "display_name": "Postgres",
                "read_latency": {"min": 1.0, "max": 50.0, "unit": "ms"},
                "write_latency": {"min": 2.0, "max": 100.0, "unit": "ms"},
                "durability": ["persistent", "durable"],
                "access_patterns": ["random-read", "transactional"],
                "cost_per_gb_month": 0.12,
            },
        },
        "compute": {
            "small-cpu": {
                "display_name": "Small CPU",
                "vcpus": 2,
                "memory_gb": 4.0,
                "compute_types": ["cpu"],
                "cost_per_hour": 0.05,
            },
            "gpu-instance": {
                "display_name": "GPU Instance",
                "vcpus": 8,
                "memory_gb": 32.0,
                "compute_types": ["gpu", "cpu"],
                "cost_per_hour": 2.50,
            },
        },
    }


# ── Model tests ───────────────────────────────────────────────────────────────


def test_latency_range_model():
    lr = LatencyRange(min=0.1, max=5.0, unit="ms")
    assert lr.min == 0.1
    assert lr.max == 5.0
    assert lr.unit == "ms"


def test_storage_backend_spec():
    raw = _minimal_raw()["storage"]["fast-redis"]
    spec = StorageBackendSpec(**raw)
    assert spec.display_name == "Fast Redis"
    assert spec.durability == ["ephemeral", "persistent"]
    assert spec.cost_per_gb_month == 3.5
    assert spec.requires_vpc is False


def test_compute_backend_spec():
    raw = _minimal_raw()["compute"]["gpu-instance"]
    spec = ComputeBackendSpec(**raw)
    assert spec.vcpus == 8
    assert spec.memory_gb == 32.0
    assert "gpu" in spec.compute_types


def test_catalog_from_raw():
    raw = _minimal_raw()
    cat = Catalog.from_raw(raw)
    assert "fast-redis" in cat.storage
    assert "slow-postgres" in cat.storage
    assert "small-cpu" in cat.compute
    assert "gpu-instance" in cat.compute


def test_catalog_from_raw_empty_sections():
    cat = Catalog.from_raw({})
    assert cat.storage == {}
    assert cat.compute == {}
    assert cat.network == {}


# ── Loader tests ──────────────────────────────────────────────────────────────


def test_load_catalog_explicit_path():
    """load_catalog() with explicit path returns a raw dict."""
    p = Path(__file__).parent.parent.parent / "catalogs" / "aws.toml"
    if not p.exists():
        pytest.skip("catalogs/aws.toml not present")
    catalog = load_catalog(p)
    assert "storage" in catalog


def test_load_catalog_missing_path():
    with pytest.raises(FileNotFoundError):
        load_catalog("/nonexistent/path/catalog.toml")


def test_load_typed_catalog(tmp_path):
    """load_typed_catalog() returns a Catalog object."""
    # write a minimal TOML
    lines = [
        "[storage.fast-redis]",
        'display_name = "Fast Redis"',
        'read_latency = { min = 0.1, max = 2.0, unit = "ms" }',
        'write_latency = { min = 0.1, max = 5.0, unit = "ms" }',
        'durability = ["ephemeral", "persistent"]',
        'access_patterns = ["random-read"]',
        "cost_per_gb_month = 3.5",
    ]
    p = tmp_path / "test.toml"
    p.write_text("\n".join(lines))
    cat = load_typed_catalog(p)
    assert isinstance(cat, Catalog)
    assert "fast-redis" in cat.storage
    assert cat.storage["fast-redis"].cost_per_gb_month == 3.5


# ── Registry tests ────────────────────────────────────────────────────────────


@pytest.fixture
def registry() -> BackendRegistry:
    cat = Catalog.from_raw(_minimal_raw())
    return BackendRegistry(cat)


def test_find_storage_no_filter(registry):
    results = registry.find_storage()
    assert len(results) == 2


def test_find_storage_by_durability(registry):
    results = registry.find_storage(durability="durable")
    names = [n for n, _ in results]
    assert "slow-postgres" in names
    assert "fast-redis" not in names


def test_find_storage_by_read_latency(registry):
    # max 3ms → only fast-redis qualifies (min=0.1 ≤ 3)
    results = registry.find_storage(read_latency_max=3.0)
    names = [n for n, _ in results]
    assert "fast-redis" in names
    assert "slow-postgres" not in names


def test_find_storage_by_access_pattern(registry):
    results = registry.find_storage(access_pattern="transactional")
    names = [n for n, _ in results]
    assert "slow-postgres" in names
    assert "fast-redis" not in names


def test_find_storage_sorted_cheapest(registry):
    results = registry.find_storage()
    costs = [spec.cost_per_gb_month for _, spec in results]
    assert costs == sorted(costs)


def test_best_storage_returns_cheapest(registry):
    result = registry.best_storage(durability="persistent")
    assert result is not None
    name, spec = result
    assert name == "slow-postgres"  # cheaper at 0.12/GB


def test_best_storage_no_match(registry):
    result = registry.best_storage(access_pattern="nonexistent-pattern")
    assert result is None


def test_find_compute_by_type(registry):
    results = registry.find_compute(compute_type="gpu")
    names = [n for n, _ in results]
    assert "gpu-instance" in names
    assert "small-cpu" not in names


def test_find_compute_by_memory(registry):
    results = registry.find_compute(memory_min=10.0)
    names = [n for n, _ in results]
    assert "gpu-instance" in names
    assert "small-cpu" not in names


def test_best_compute_cheapest_first(registry):
    result = registry.best_compute(compute_type="cpu")
    assert result is not None
    name, spec = result
    assert name == "small-cpu"  # cheaper


def test_find_compute_any_type(registry):
    results = registry.find_compute(compute_type="any")
    assert len(results) == 2
