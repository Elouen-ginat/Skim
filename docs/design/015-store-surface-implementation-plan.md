# ADR 015 — Store Surface Implementation Plan

**Status:** Proposed
**Date:** 2026-04-30
**Related:** [user_gaps.md §B.2](../user_gaps.md), [ADR 014](014-http-routing-overhaul.md), [new_storage.md](../new_storage.md)

## Goal

Land the next post-HTTP implementation pass on the `Store[T]` surface.

This pass should solve the two highest-reach remaining P0s together:

1. pagination on `Store[T]`
2. secondary indexes on `Store[T]`

These are the next most common reasons a user outgrows Skaal immediately after the HTTP story is made workable with FastAPI mounts. A CRUD API that can route requests but cannot page through rows or look up by anything except primary key is still not viable for a non-toy app.

## Why this is next

After ADR 014, the HTTP routing problem is no longer the bottleneck. The next bottleneck is the storage API shape:

- `Store.list()` returns the entire dataset.
- `Store.scan(prefix)` is the only lookup other than `get(key)`.
- there is no supported way to say "query users by email" or "show the next 50 rows".

These are not three separate product problems. They are one missing storage surface. The right next move is to fix that surface directly instead of starting a broader grab-bag milestone.

## Scope

This pass includes:

- a typed paginated listing API for `Store[T]`
- declared secondary indexes on `@app.storage(...)`
- a query API over declared indexes
- local, SQLite, Postgres, Redis, DynamoDB, and Firestore support for the new surface
- docs and examples updated to use the new API

This pass does **not** include:

- blob/object storage
- per-row TTL enforcement
- full-text search
- generated convenience methods like `find_by_email(...)`
- relational migrations

Those are real gaps, but they should remain separate work. Mixing them into this pass would turn one coherent API cutover into several unrelated backend projects.

## Current facts

The current `Store[T]` surface is intentionally small:

- `get(key)`
- `set(key, value)`
- `delete(key)`
- `list()`
- `scan(prefix="")`

The current backend protocol in `skaal/backends/base.py` matches that shape. There is no cursor concept, no page object, and no index declaration or query contract.

That is good news. The work is localized:

- `skaal/storage.py` owns the user-facing API
- `skaal/backends/base.py` owns the backend contract
- the storage decorators own the metadata surface
- built-in backends own the mechanics

## Decision

Add one explicit, typed `Store` query surface instead of trying to stretch `scan(prefix)` into several meanings.

The user model should be:

- `list_page(...)` when they want a cursor-based walk over the primary key order
- `scan_page(prefix=...)` when they want cursor-based prefix traversal
- `query_index(...)` when they want secondary lookup by declared index

Indexes must be declared, named metadata. They should not be inferred from every model field.

## Public API to add

### 1. Pagination types

Add `skaal/types/storage.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    items: list[T]
    next_cursor: str | None
    has_more: bool
```

Re-export it from `skaal.types.__init__` and top-level `skaal`.

The cursor is opaque. Users pass it back untouched.

### 2. Index declaration type

Add a small storage index metadata type in the same module:

```python
@dataclass(frozen=True)
class SecondaryIndex:
    name: str
    partition_key: str
    sort_key: str | None = None
    unique: bool = False
```

This is intentionally small. It covers the 80% case:

- unique lookup by `email`
- grouping by `org_id`
- grouped ordering by `org_id + created_at`

Do not add filter expressions, projections, or arbitrary query operators in v1.

### 3. Decorator metadata

Extend `@app.storage(...)` and the underlying decorator to accept:

```python
indexes: list[SecondaryIndex] | None = None
```

Store the declarations in `__skaal_storage__["indexes"]` so the runtime, solver, and deploy layers see the same metadata.

### 4. Store methods

Add to `Store[T]`:

```python
@classmethod
async def list_page(cls, *, limit: int = 100, cursor: str | None = None) -> Page[tuple[str, T]]: ...

@classmethod
async def scan_page(
    cls,
    prefix: str = "",
    *,
    limit: int = 100,
    cursor: str | None = None,
) -> Page[tuple[str, T]]: ...

@classmethod
async def query_index(
    cls,
    index_name: str,
    key: Any,
    *,
    limit: int = 100,
    cursor: str | None = None,
) -> Page[T]: ...
```

Keep `list()` and `scan()` as eager convenience helpers implemented on top of the paged methods.

## Backend contract changes

Extend `StorageBackend` with three methods:

```python
async def list_page(self, *, limit: int, cursor: str | None) -> Page[tuple[str, Any]]: ...

async def scan_page(
    self,
    prefix: str,
    *,
    limit: int,
    cursor: str | None,
) -> Page[tuple[str, Any]]: ...

async def query_index(
    self,
    index_name: str,
    key: Any,
    *,
    limit: int,
    cursor: str | None,
) -> Page[Any]: ...
```

Implementation rule: `list()` and `scan()` stay in the protocol for now, but built-in backends should implement them by draining the paged methods so there is one canonical traversal path.

## Cursor contract

Do not expose backend-native cursors directly.

Use one internal cursor codec owned by Skaal that serializes a small JSON payload and base64-encodes it. Each backend can decode only the fields it needs.

The cursor payload should be additive and backend-tagged, for example:

```json
{
  "backend": "sqlite",
  "mode": "list",
  "last_key": "user_0042"
}
```

This keeps the public API stable while allowing different backends to use different resume tokens internally.

## Index semantics

The v1 contract should be intentionally narrow:

- `partition_key` is required
- `sort_key` is optional
- `query_index(...)` matches exactly on `partition_key`
- ordering within the page is by `sort_key` when present, otherwise by primary key
- cursor resumes from the last item seen in that ordered sequence

Do not add range predicates in this pass. If the user needs "created_at > X" they can still use relational storage. The point of this pass is to make the common KV cases first-class, not to build DynamoDB's whole query model into Skaal.

## Backend implementation rules

### LocalMap

- Keep an in-memory secondary map per declared index.
- Update indexes on `set` and `delete`.
- Implement pagination over sorted primary keys and sorted index entries.

### SQLite / Postgres

- Materialize KV rows into one table if not already present.
- Create secondary SQL indexes for declared `SecondaryIndex` metadata.
- Use `ORDER BY` + `LIMIT` with resume based on last seen key or `(partition_key, sort_key, pk)` tuple.

### Redis

- Keep the existing key-value payload as source of truth.
- Use sorted sets or namespaced sets to maintain secondary index membership.
- Cursor should be based on score/member resume, not a full key dump.

### DynamoDB / Firestore

- Use native secondary indexes where available.
- Treat missing deploy-time index resources as a planning error, not a silent runtime fallback.
- The deploy/build layers must emit the required index resources from `__skaal_storage__["indexes"]`.

## Solver and deploy implications

This pass should **not** change backend selection logic yet.

But it must surface enough metadata for later planning work:

- `__skaal_storage__["indexes"]` should appear in the solved metadata
- deploy targets should provision declared indexes for backends that require infra resources
- missing index support in a target should fail clearly during plan/build, not after deploy

The solver should remain conservative in v1: index declarations do not change the chosen backend, they change whether the chosen backend is considered deployable.

## Docs and examples

Update:

- `docs/user_gaps.md` top-of-list section to point here as the next implementation pass
- `README.md` or `docs/http.md` examples where they currently imply eager `list()` over the whole dataset
- `examples/02_todo_api/app.py` to use paginated listing in at least one endpoint

Add a focused storage doc showing:

```python
@app.storage(
    read_latency="< 10ms",
    durability="persistent",
    indexes=[
        SecondaryIndex("by_email", partition_key="email", unique=True),
        SecondaryIndex("by_org_created", partition_key="org_id", sort_key="created_at"),
    ],
)
class Users(Store[User]):
    pass

page = await Users.list_page(limit=50, cursor=cursor)
matches = await Users.query_index("by_email", "alice@example.com")
```

## Tests

Add coverage for:

- `Store.list_page()` first page, middle page, final page
- `Store.scan_page(prefix=...)` preserving prefix semantics with cursors
- `Store.query_index()` on unique and non-unique indexes
- index maintenance after overwrite and delete
- cursor stability across backends for the same visible ordering
- deploy/build failures when a backend needs explicit index resources and they are missing
- eager `list()` and `scan()` preserving old behavior by draining paged reads

## Direct cutover sequence

1. Add `Page` and `SecondaryIndex` types and re-export them.
2. Extend storage decorators to collect `indexes=` metadata.
3. Extend `StorageBackend` with paged/query methods.
4. Implement `Store.list_page`, `scan_page`, and `query_index`.
5. Update `LocalMap`, SQLite, Postgres, and Redis backends.
6. Update DynamoDB and Firestore plus deploy/build resource generation.
7. Convert `list()` and `scan()` to drain paged methods.
8. Update examples and docs.
9. Land backend-specific tests before expanding scope.

## Non-goals for the follow-up pass

The implementation immediately after this one should not be "more storage" by default. Once pagination and indexes land, the next candidate passes can be evaluated cleanly between:

- blob/object storage
- agent persistence correctness
- `skaal init` / `skaal dev`
- solver error-message overhaul

That prioritization should happen after this surface is stable, not while it is still being cut over.

## Acceptance criteria

- Users can page through `Store[T]` without loading the whole dataset.
- Users can declare and query secondary indexes on `Store[T]`.
- Built-in backends implement the same visible pagination and query semantics.
- Deploy/build fails clearly when declared indexes require infra that was not provisioned.
- Existing eager `list()` and `scan()` calls still work unchanged.
- At least one example demonstrates paginated listing plus secondary lookup.
