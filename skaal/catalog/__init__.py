"""skaal.catalog — catalog loading, models, and backend registry."""

from skaal.catalog.loader import load_catalog, load_typed_catalog
from skaal.catalog.models import Catalog, ComputeBackendSpec, NetworkSpec, StorageBackendSpec
from skaal.catalog.registry import BackendRegistry

__all__ = [
    "BackendRegistry",
    "Catalog",
    "ComputeBackendSpec",
    "NetworkSpec",
    "StorageBackendSpec",
    "load_catalog",
    "load_typed_catalog",
]
