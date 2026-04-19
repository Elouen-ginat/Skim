# ADR 007 — Schema Versioning and Migrations

**Status:** Accepted
**Date:** 2024-01-01

## Context

Pydantic models stored via `Store[T]` evolve over time.
Stored data may be at an older schema version than the current model.  We need
a safe, explicit way to upgrade stored data on read.

## Decision

Adopt a **decorator-based migration registry**:

1. Models declare their version via `__skaal_version__ = N` (default: 1).
2. Migration functions are registered with `@migrate_from(OldModel, to=NewModel)`.
3. `apply_migrations(data, Model)` chains registered functions to upgrade data
   from `stored_version` to `target_version` before deserialisation.

```python
class UserV2(BaseModel):
    __skaal_version__ = 2
    id: str
    full_name: str = ""

@migrate_from(UserV1, to=UserV2)
def _(old: dict) -> dict:
    return {**old, "full_name": old.get("name", "")}
```

Migrations are stored in `_MIGRATIONS`, a module-level dict keyed by
`(model_name, from_version)`.

## Consequences

**Positive:**
- Migrations are co-located with model definitions.
- `apply_migrations` is deterministic and testable.
- No external migration tool required.

**Negative:**
- Migration registry is process-global; tests must reset it between runs.
- Only forward migrations are supported; rollback requires a separate migration.
- Model rename requires careful coordination of `__name__` strings.
