"""Schema versioning and migration definitions for Skaal models.

Every Pydantic model stored via Map[K, V] or Collection[T] can declare
a schema version and a migration function that upgrades stored data from
the previous version.

Usage::

    from skaal.types.schema import versioned, migrate_from

    class UserV2(BaseModel):
        __skaal_version__ = 2

        id: str
        name: str
        full_name: str = ""   # new in v2 (renamed from 'name')

    @migrate_from(UserV1, to=UserV2)
    def migrate_user_v1_to_v2(old: dict) -> dict:
        return {**old, "full_name": old.get("name", "")}
"""

from __future__ import annotations

from typing import Any, Callable


# Registry: (source_model_name, source_version) -> migration_fn
_MIGRATIONS: dict[tuple[str, int], Callable[[dict], dict]] = {}


def migrate_from(
    source: type,
    *,
    to: type,
) -> Callable[[Callable[[dict], dict]], Callable[[dict], dict]]:
    """
    Register a migration function from ``source`` model version to ``to`` version.

    The decorated function receives the raw ``dict`` from storage and must
    return a ``dict`` compatible with the ``to`` model.

    Example::

        @migrate_from(UserV1, to=UserV2)
        def _(old: dict) -> dict:
            return {**old, "full_name": old.pop("name", "")}
    """
    source_version = getattr(source, "__skaal_version__", 1)
    source_name = source.__name__

    def decorator(fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
        _MIGRATIONS[(source_name, source_version)] = fn
        return fn

    return decorator


def apply_migrations(data: dict, model: type) -> dict:
    """
    Upgrade *data* to the current version of *model* by running registered
    migration functions in sequence.

    If no migrations are registered or the data is already current, returns
    *data* unchanged.
    """
    target_version = getattr(model, "__skaal_version__", 1)
    stored_version = data.get("__skaal_version__", 1)

    if stored_version >= target_version:
        return data

    current = dict(data)
    model_name = model.__name__

    for v in range(stored_version, target_version):
        fn = _MIGRATIONS.get((model_name, v))
        if fn is not None:
            current = fn(current)

    current["__skaal_version__"] = target_version
    return current


def list_migrations() -> list[tuple[str, int]]:
    """Return all registered migration keys as ``(model_name, from_version)``."""
    return sorted(_MIGRATIONS.keys())
