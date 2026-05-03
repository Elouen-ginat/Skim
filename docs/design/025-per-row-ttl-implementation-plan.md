# ADR 025 — Per-row TTL & Cache Semantics Implementation Plan

**Status:** Proposed
**Date:** 2026-05-03
**Related:** [user_gaps.md §B.2](../user_gaps.md#b2-kv-store-and-storage-tiers), [ADR 015](015-store-surface-implementation-plan.md), [ADR 022](022-catalog-overrides-implementation-plan.md)

## Goal

Make `retention=` on `@app.storage` actually expire rows at runtime, and give users a per-call `ttl=` knob on the `Store[T]` write API.

Today, `retention` is a string that the solver consumes as a *categorical* compatibility key against a `retention = [...]` enum slot in the catalog spec ([`skaal/solver/storage.py:75-90`](../../skaal/solver/storage.py#L75-L90)). No backend (`LocalMap`, `SqliteBackend`, `PostgresBackend`, `RedisBackend`, `DynamoBackend`, `FirestoreBackend`) writes an expiry, reads an expiry, or sweeps anything. The cataloged backends do not even declare a `retention` slot, so the check is dead code: any constraint other than `retention=None` is silently ignored. This is the §B.2 P0 gap against session/cache workloads.

This pass closes the gap by replacing the dead categorical check with two real surfaces:

1. **Class-level retention** — `@app.storage(retention="30m")` becomes a backend capability requirement *and* the default per-row TTL applied on writes for that store.
2. **Per-call TTL** — `await Sessions.set(key, value, ttl="15m")` overrides the default for one entry. Same coverage on `add`, `update`, and the relevant sync wrappers.

## Why this is next

The remaining P0 storage gap from the top-of-list ranking after ADRs 016 (blob), 018 (agent persistence), 020 (init/dev), 021 (solver UNSAT), 022 (catalog overrides), 023 (relational migrations), and 024 (secrets) all landed.

Items 8 (examples ladder) and 9 (backend-native cursor/index optimization) are P1; #10 per-row TTL is the only remaining P0 storage capability gap and is the smallest coherent code-only pass that closes a documented-but-broken promise (`retention=` is in the public decorator signature today and silently does nothing). It also unlocks two recurring user stories — sessions and short-lived caches — that today force users to either add Redis-by-hand or run a periodic sweep job.

It pairs naturally with the new `Store[T]` surface from ADR 015: TTL is a property of values stored under a key, so the right place to introduce it is on the same writes that already flow through `Store.set` / `Store.add` / `Store.update`.

## Scope

This pass includes:

- A `Duration` value type and a `TTL` runtime value type under `skaal/types/`.
- A `Retention` type (a class-level retention declaration that wraps a `Duration` plus a small set of named policies).
- Decorator coercion: `retention="30m"` parses to `Retention(Duration(30, "m"))` at decoration time and is rejected eagerly on bad input.
- `Store.set`, `Store.add`, `Store.update`, and their `sync_*` siblings gain a keyword-only `ttl: TTL | str | float | None = None` parameter.
- `StorageBackend` Protocol extended with a `ttl: float | None` keyword on `set` and `atomic_update`. A new optional `set_many` is **not** in scope.
- All bundled KV backends (`LocalMap`, `SqliteBackend`, `PostgresBackend`, `RedisBackend`, `DynamoBackend`, `FirestoreBackend`) honour TTL at write time and filter expired rows at read time.
- Solver replaces the dead `retention = [...]` enum check with a capability check: a backend must advertise `supports_ttl = true` (and optionally `max_ttl_seconds`) to satisfy any declared `retention=`. Backends that do not advertise it are excluded with a clear UNSAT diagnosis.
- Catalogs (`local.toml`, `aws.toml`, `gcp.toml`) declare `supports_ttl` (and optionally `max_ttl_seconds`) on every storage backend that has, or can have, native TTL.
- One example under `examples/02_todo_api/` — or a new `examples/06_session_cache/` mini-app — exercises both class-level retention and per-call `ttl=`.
- Tests covering: parser, capability check, per-backend write+expiry behaviour, default-from-class-retention path, override path, "expired keys do not appear in `list`/`scan`/`get`", and "negative or zero TTL is rejected at the seam".

This pass does **not** include:

- Eviction policies (LRU, LFU, max-size) — separate concern, not what `retention` claims.
- A new "cache" `kind` distinct from `kv`. Per-row TTL is a property a `kv` backend either has or does not; introducing a fourth kind would force users to choose between TTL and indexes.
- TTL on `Collection[T]` / relational tier rows. SQLModel rows already have `created_at` / `updated_at` patterns; row-level expiry there is a separate ADR.
- TTL on blobs. S3/GCS lifecycle policies are bucket-scoped and are a separate ADR.
- TTL on `EventLog` retention. The pattern's `retention=` already has different semantics (truncate by age) and is wired through `pattern_meta["storage"]["retention"]` ([`skaal/patterns.py:88`](../../skaal/patterns.py#L88)); this plan does not change that path.

## Design

### Types (`skaal/types/`)

All new types live in a new module `skaal/types/duration.py` and are re-exported from `skaal/types/__init__.py`. They are *not* added to `constraints.py` — `Latency`, `Throughput`, `Durability`, `AccessPattern` are constraint *predicates* used at solve time; `Duration` and `TTL` are concrete *values* used both at solve time (for capability checks) and at runtime (passed to backends). Mixing them dilutes the constraint surface.

#### `Duration`

```python
@dataclass(frozen=True)
class Duration:
    """Parsed duration value. ``Duration("30m")`` or ``Duration(30, "m")``."""

    seconds: float
    expr: str  # canonical string form, e.g. "30m"

    @classmethod
    def parse(cls, value: str | int | float | Duration) -> Duration: ...

    def __str__(self) -> str: ...
```

- Accepted unit suffixes: `ms`, `s`, `m`, `h`, `d`, `w`. Bare ints/floats are rejected — users must pick a unit, matching the `Latency`/`Throughput` style.
- Invalid inputs raise `ValueError` with a "did you mean…" hint, mirroring `_StrictStrEnum._missing_` ([`skaal/types/constraints.py:14-25`](../../skaal/types/constraints.py#L14-L25)).
- Negative or zero durations raise at parse time. The runtime never sees a non-positive `Duration`.

#### `TTL`

```python
@dataclass(frozen=True)
class TTL:
    """Runtime per-call TTL: either a relative duration or an absolute deadline."""

    seconds: float          # always relative-to-now at call time
    expr: str | None        # original user input, for logs

    @classmethod
    def coerce(cls, value: TTL | Duration | str | int | float | None) -> TTL | None: ...
```

- `TTL` is what backends receive. The `coerce` classmethod is the single seam used by `Store.set` / `add` / `update` so the backend protocol can stay typed at `float | None` (seconds) — the parsed `Duration` is collapsed to seconds before the backend call.
- `int` / `float` inputs are interpreted as **seconds**, not as "raw with no unit" — this is the only place we accept unitless numerics, and it is consistent with every Python TTL API in the ecosystem (`redis-py`, `cachetools`).
- `None` propagates as `None` (no TTL at this call; the backend should fall back to the class-level default).

#### `Retention`

```python
@dataclass(frozen=True)
class Retention:
    """Class-level retention policy declared via @app.storage(retention=...)."""

    duration: Duration | None    # None ⇔ NEVER
    policy: Literal["expire", "never"]

    @classmethod
    def parse(cls, value: Retention | Duration | str | None) -> Retention | None: ...

    @property
    def default_ttl_seconds(self) -> float | None: ...
```

- `retention="30m"` → `Retention(duration=Duration("30m"), policy="expire")`.
- `retention="never"` → `Retention(duration=None, policy="never")`. Useful as an explicit override when a parent module's defaults set retention.
- `retention=None` (the default) means "no retention declared", distinct from `"never"`. The solver only constrains backends when `Retention.policy == "expire"`.

### Decorator changes (`skaal/decorators.py`)

`_build_storage_metadata` already coerces enums via `_coerce_enum`. Add a sibling `_coerce_retention` and call it for the `retention` slot:

```python
def _coerce_retention(value: Retention | str | None) -> Retention | None:
    if value is None or isinstance(value, Retention):
        return value
    return Retention.parse(value)
```

The metadata stored on the class becomes a `Retention` instance, not a raw string. `solver/storage.py` and `solver/solver.py` read it through a single helper so downstream code never has to handle both shapes.

### `Store[T]` API (`skaal/storage.py`)

```python
@classmethod
async def set(
    cls,
    key: str,
    value: T,
    *,
    ttl: TTL | Duration | str | float | None = None,
) -> None: ...
```

- `ttl` is `TTL.coerce`-ed and forwarded as `float | None` seconds to `cls._backend.set(...)`.
- When the caller passes `ttl=None`, the backend may apply its class-level default (see "Default TTL flow" below).
- `add`, `update`, and the corresponding `sync_*` wrappers gain the same parameter.
- `update`'s atomic-callable path forwards `ttl` to `atomic_update` so a read-modify-write under the backend lock can also refresh the row's expiry.

`update`'s value-replacement path (passing a concrete value) is just `set` under the hood today — that stays.

### Default TTL flow

The class-level retention is plumbed through `Store.wire` once, not on every call:

1. `@app.storage(retention="30m")` populates `__skaal_storage__["retention"]: Retention`.
2. When `Store.wire(backend)` runs, it sets `backend._skaal_default_ttl = retention.default_ttl_seconds` (or `None`).
3. Each backend reads `self._skaal_default_ttl` inside `set` / `atomic_update` *only* when the call-site `ttl` is `None`.
4. `ttl=0` is rejected. The escape hatch for "this one row should never expire even though the class declared retention" is `ttl=TTL.never()` → translates to a sentinel that suppresses default application.

This avoids a second public field on `Store` and keeps the runtime config coupled to the backend handle the rest of the API already targets.

### `StorageBackend` Protocol (`skaal/backends/base.py`)

Two methods change:

```python
async def set(
    self,
    key: str,
    value: Any,
    *,
    ttl: float | None = None,
) -> None: ...

async def atomic_update(
    self,
    key: str,
    fn: Callable[[Any], Any],
    *,
    ttl: float | None = None,
) -> Any: ...
```

These are additive in spirit (default `None` keeps every existing call-site working without source changes), and because we have no backwards-compatibility shim policy the older `set(key, value)` shape simply goes away — every bundled backend updates in this PR. Out-of-tree backends will get a single-line type error and a one-line fix.

`get`, `list`, `list_page`, `scan`, `scan_page`, `query_index`, `delete`, `increment_counter`, `close` do not change shape. They do change behaviour: expired entries must not appear in any read path. Each backend implements that as part of its own read-path.

### Backend implementations

| Backend | Storage of expiry | Write path | Read path | Sweep |
|---|---|---|---|---|
| `LocalMap` | parallel `dict[str, float]` of unix-epoch deadlines | `set(..., ttl=)` records `now + ttl` | `get`/`list`/`scan` filter by `deadline > now`, drop expired keys lazily | Opportunistic on every read; no background task |
| `SqliteBackend` | new nullable `expires_at REAL` column on `kv`, indexed | `INSERT … expires_at = ?` | `WHERE (expires_at IS NULL OR expires_at > strftime('%s','now'))` on every read | Opportunistic `DELETE` of expired rows on `set` + a `TODO` for explicit `vacuum_expired()` (out of scope) |
| `PostgresBackend` | new nullable `expires_at TIMESTAMPTZ` column on `skaal_kv`, indexed | `INSERT … expires_at = NOW() + interval` | `WHERE (expires_at IS NULL OR expires_at > NOW())` on every read | Same opportunistic-on-write strategy as SQLite |
| `RedisBackend` | native `EX` argument on `SET` | `SET full_key value EX ttl` (or `SET … KEEPTTL` on touch-only writes) | Native — Redis omits expired keys from `GET`/`SCAN` | Native |
| `DynamoBackend` | DynamoDB TTL attribute on the table (`expires_at`, type `N`) | `put_item` with the attribute set | DynamoDB drops expired items eventually but not immediately — read path filters in-process by the same attribute as a belt-and-braces guard | Native (eventual) plus the in-process guard |
| `FirestoreBackend` | TTL policy on a single field (`expires_at`, type `Timestamp`) | `set` writes the field; backend uses Firestore's TTL feature | Same in-process guard as DynamoDB | Native (eventual) plus the in-process guard |

The "in-process guard" for DynamoDB and Firestore is unavoidable: AWS docs guarantee deletion within 48 h of expiry; without the guard, expired keys would leak to user code in the meantime.

`SqliteBackend` and `PostgresBackend` need a schema migration. The schema-migration registry already knows how to apply DDL (`skaal/migrate/`); a one-step migration adds the column. The `CREATE TABLE` paths in `connect()` are updated for fresh installs.

The new column is **not** populated by index-bucket helper rows in DynamoDB / Redis index buckets — those are infrastructural and never expire. The TTL is only set when the call originated from a user-facing `Store.set` / `Store.update`.

### Solver changes (`skaal/solver/storage.py`)

Replace `_check_retention` with a capability check:

```python
def _check_retention(value: Any, spec: dict[str, Any]) -> bool:
    if value is None or value.policy != "expire":
        return True
    if not spec.get("supports_ttl", False):
        return False
    seconds = value.duration.seconds
    cap = spec.get("max_ttl_seconds")
    return cap is None or seconds <= cap
```

`_CONSTRAINT_FORMATTERS["retention"]` updates to render a human-readable form: `"retention=30m (per-row TTL)"`.

The diagnostics path in `skaal/solver/diagnostics.py:79` (`"retention": _categorical_offered`) needs a parallel update so the closest-match table reports "supports per-row TTL: yes/no" rather than a list of enum values that no backend ever declared. This keeps ADR 021's UX coherent.

The solver layer that reads `__skaal_storage__["retention"]` (`skaal/solver/solver.py` plus `_pattern_solvers.py`) updates to expect a `Retention | None` value rather than a raw string. The `EventLog` pattern path keeps its existing string-shaped `pattern_meta["storage"]["retention"]` because that retention has different semantics (truncate by age, applied by the engine, not the backend). They share parsing through `Duration.parse` but stay structurally distinct.

### Catalog changes

Each storage table that points at a backend with native TTL gets `supports_ttl = true`; backends with a maximum (e.g. AWS DynamoDB has no upper bound; Firestore has no upper bound; Redis has practical 64-bit microsecond max) get `max_ttl_seconds = …` only when meaningful.

Concretely:

- `local-map`, `sqlite`, `local-redis`, `dynamodb`, `firestore`, `aws-redis`, `gcp-redis` — `supports_ttl = true`.
- `postgres` — `supports_ttl = true` after the `expires_at` migration lands.
- Cloud backends with no TTL story (none in the bundled catalogs at present) — omit the key, which defaults to `false`.

The catalog model in `skaal/catalog/models.py` learns the new field as an optional boolean / int.

### Tests

Under `tests/storage/`:

- `test_ttl_types.py` — `Duration.parse`, `TTL.coerce`, `Retention.parse`, and the rejection paths.
- `test_ttl_local.py` — `LocalMap` write+read+expiry, default-from-retention, override on `set`, expiry visible to `list`/`scan`, `update` refreshes TTL.
- `test_ttl_sqlite.py` and `test_ttl_postgres.py` (skipped without local Postgres) — same matrix plus schema-migration round-trip.
- `test_ttl_redis.py` (skipped without local Redis) — native EX path; expired keys disappear from `list_page` even when the key index is stale.
- `test_ttl_solver.py` — capability check both ways (sat with `supports_ttl=true`, unsat with explicit "no backend supports per-row TTL" diagnosis).
- `tests/storage/test_backend_contract.py` (existing) gains a `@parametrize`-driven block that runs the same write+expire+read against every wired backend so any future backend automatically inherits the contract.

The autouse `reset_migration_registry` fixture in `tests/conftest.py` does not need changes; the new schema migration registers and unregisters cleanly under it.

### Files touched

- `skaal/types/duration.py` (new) — `Duration`, `TTL`, `Retention`.
- `skaal/types/__init__.py` — re-exports the three new types.
- `skaal/decorators.py` — `_coerce_retention`, integrated into `_build_storage_metadata`.
- `skaal/storage.py` — `ttl` keyword on `set`/`add`/`update` + sync siblings; default TTL plumbing on `wire`.
- `skaal/backends/base.py` — Protocol updates for `set` / `atomic_update`.
- `skaal/backends/local_backend.py` — expiries dict + lazy filter.
- `skaal/backends/sqlite_backend.py` — `expires_at` column, write path, read filters, schema migration.
- `skaal/backends/postgres_backend.py` — `expires_at` column, write path, read filters, schema migration.
- `skaal/backends/redis_backend.py` — pass `EX` through `SET` and `atomic_update`'s `MULTI` block.
- `skaal/backends/dynamodb_backend.py` — write the TTL attribute; read-path filter.
- `skaal/backends/firestore_backend.py` — write the TTL field; read-path filter.
- `skaal/solver/storage.py` — capability-shaped `_check_retention`; updated formatter and selection-reason text.
- `skaal/solver/diagnostics.py` — diagnostic for the new check.
- `skaal/solver/solver.py`, `skaal/solver/_pattern_solvers.py` — read `Retention` from metadata instead of raw string.
- `skaal/catalog/models.py` — optional `supports_ttl: bool` and `max_ttl_seconds: int | None`.
- `skaal/catalog/data/local.toml`, `aws.toml`, `gcp.toml` — declare the new keys per backend.
- `skaal/migrate/` — register the new DDL migration for the KV `expires_at` column.
- `tests/storage/test_ttl_*.py` (new), `tests/storage/test_backend_contract.py` (extended), `tests/solver/test_storage_solver.py` (extended).
- `docs/user_gaps.md` — strike item #10 from the top-of-list, mark §B.2 row "Per-row TTL" as resolved.
- `examples/06_session_cache/` (new short example) — covers the §A.8 "no agents/schedules/patterns example" gap from the same direction; uses `@app.storage(retention=...)` plus `ttl=` overrides for a token store.

## Migration / compatibility

Per repo policy, no backwards-compatibility shims:

- The `set(key, value)` Protocol grows a keyword-only `ttl=None` argument. Any out-of-tree backend that defined `set` with `**kwargs` keeps working; one that pinned a positional-only signature gets a one-line type fix.
- The existing dead `retention = [...]` enum slot in any user-authored TOML is rejected by `catalog/models.py` validation with a clear message that the slot has been replaced by `supports_ttl`.
- Stored rows in `kv` / `skaal_kv` tables predating this change have a `NULL` `expires_at`, which the read-path treats as "never expires". Existing data is therefore unaffected. The schema migration adds the column with `NULL` default; no row rewrite required.

## Open questions

- **Sweep cadence.** The opportunistic-on-write sweep is cheap but lets expired rows accumulate during read-only windows. A `vacuum_expired()` method on each backend, plus an `EveryEngine` job in local runtime, is a candidate follow-up but is left out here to keep the PR shape tight.
- **`TTL.never()` ergonomics.** Choosing between a sentinel object and a `Literal["never"]` string is open. The sentinel is more typeable; the string is more grep-able. First cut goes with `TTL.never()`; revisit if user feedback prefers the string.
- **Per-call `expires_at` (absolute deadline).** The `TTL` type carries `seconds` only. If users need "expire at 23:59 UTC tonight" we add an alternate constructor `TTL.at(datetime)`; the backend Protocol does not need to change because we still hand it `seconds = expires_at - now()` at call time. Deferred.
- **Relational-tier TTL.** Out of scope here. The right shape is a `__skaal_relational_ttl__` column policy that extends the migration engine — large enough to deserve its own ADR.
