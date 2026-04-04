"""Root test configuration for pytest."""

import pytest


@pytest.fixture(autouse=True)
def reset_migration_registry():
    """Ensure the migration registry is clean before each test."""
    from skaal.types.schema import _MIGRATIONS

    snapshot = dict(_MIGRATIONS)
    yield
    _MIGRATIONS.clear()
    _MIGRATIONS.update(snapshot)
