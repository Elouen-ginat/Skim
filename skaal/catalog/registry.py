"""Backend registry — query catalog entries by constraint predicates."""

from __future__ import annotations

from typing import Any

from skaal.catalog.models import Catalog, ComputeBackendSpec, StorageBackendSpec


class BackendRegistry:
    """
    Query-able view over a loaded :class:`~skaal.catalog.models.Catalog`.

    Usage::

        from skaal.catalog.loader import load_typed_catalog
        from skaal.catalog.registry import BackendRegistry

        catalog = load_typed_catalog()
        registry = BackendRegistry(catalog)

        candidates = registry.find_storage(read_latency_max=5, durability="persistent")
        instance = registry.find_compute(compute_type="gpu", memory_min=16)
    """

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog

    # ── Storage ───────────────────────────────────────────────────────────────

    def find_storage(
        self,
        *,
        read_latency_max: float | None = None,
        write_latency_max: float | None = None,
        durability: str | None = None,
        access_pattern: str | None = None,
        size_gb: float | None = None,
        region: str | None = None,
    ) -> list[tuple[str, StorageBackendSpec]]:
        """
        Return storage backends that satisfy all given constraints.

        Results are sorted cheapest-first by ``cost_per_gb_month``.
        """
        results: list[tuple[str, StorageBackendSpec]] = []

        for name, spec in self._catalog.storage.items():
            if read_latency_max is not None:
                if spec.read_latency.min > read_latency_max:
                    continue

            if write_latency_max is not None:
                if spec.write_latency.min > write_latency_max:
                    continue

            if durability is not None:
                if durability not in spec.durability:
                    continue

            if access_pattern is not None:
                if access_pattern not in spec.access_patterns:
                    continue

            if size_gb is not None and spec.max_size_gb > 0:
                if spec.max_size_gb < size_gb:
                    continue

            if region is not None:
                if "all" not in spec.regions and region not in spec.regions:
                    continue

            results.append((name, spec))

        results.sort(key=lambda x: x[1].cost_per_gb_month)
        return results

    def best_storage(self, **kwargs: Any) -> tuple[str, StorageBackendSpec] | None:
        """Return the cheapest storage backend matching the given constraints."""
        candidates = self.find_storage(**kwargs)
        return candidates[0] if candidates else None

    # ── Compute ───────────────────────────────────────────────────────────────

    def find_compute(
        self,
        *,
        compute_type: str | None = None,
        memory_min: float | None = None,
        vcpus_min: int | None = None,
        region: str | None = None,
    ) -> list[tuple[str, ComputeBackendSpec]]:
        """
        Return compute backends that satisfy all given constraints.

        Results are sorted cheapest-first by ``cost_per_hour``.
        """
        results: list[tuple[str, ComputeBackendSpec]] = []

        for name, spec in self._catalog.compute.items():
            if compute_type is not None:
                if compute_type != "any" and compute_type not in spec.compute_types:
                    continue

            if memory_min is not None:
                if spec.memory_gb < memory_min:
                    continue

            if vcpus_min is not None:
                if spec.vcpus < vcpus_min:
                    continue

            if region is not None:
                if "all" not in spec.regions and region not in spec.regions:
                    continue

            results.append((name, spec))

        results.sort(key=lambda x: x[1].cost_per_hour)
        return results

    def best_compute(self, **kwargs: Any) -> tuple[str, ComputeBackendSpec] | None:
        """Return the cheapest compute backend matching the given constraints."""
        candidates = self.find_compute(**kwargs)
        return candidates[0] if candidates else None
