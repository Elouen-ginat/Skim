# Skaal Simplification Report

_Date: 2026-04-30_

Analysis of `skaal/` (~5.8k LOC of core, ~21.5k LOC including all submodules) to identify
where complexity can be reduced via consolidation, third-party library adoption, and
philosophy shifts. Findings grouped as **Quick wins**, **Medium refactors**, and
**Philosophy shifts**, with concrete file:line references.

---

## TL;DR

| Opportunity | Files | LOC saved | Priority |
|---|---|---|---|
| Unify serialization helpers | [storage.py:72-101](../../skaal/storage.py#L72-L101), `backends/*` | ~30 | Quick |
| Consolidate cursor validation | [sqlite_backend.py:20-31](../../skaal/backends/sqlite_backend.py#L20-L31), [postgres_backend.py:21-32](../../skaal/backends/postgres_backend.py#L21-L32) | ~15 | Quick |
| Generic decorator metadata helper | [decorators.py](../../skaal/decorators.py) | ~40 | Quick |
| Replace custom retry/breaker stack with Tenacity + pybreaker | [runtime/middleware.py:40-212](../../skaal/runtime/middleware.py#L40-L212) | ~150 | Medium |
| Extract `BaseRuntime` from Local + Mesh | [runtime/local.py](../../skaal/runtime/local.py), [runtime/mesh_runtime.py](../../skaal/runtime/mesh_runtime.py) | ~250 | Medium |
| Collapse backend factory classmethods | [runtime/local.py:180-331](../../skaal/runtime/local.py#L180-L331) | ~80 | Medium |
| Use SQLModel directly for relational | [relational.py](../../skaal/relational.py), backends | ~200 | Philosophy |
| Adopt APScheduler/Arq for scheduling | [schedule.py](../../skaal/schedule.py) | ~100 | Philosophy |
| Replace pattern engines with LangGraph or Temporal | [runtime/engines/](../../skaal/runtime/engines/) | ~300 | Philosophy |
| fsspec for blob backends | `skaal/backends/*blob*` | ~150 | Philosophy |
| Drop entry-point plugin layer | [plugins.py](../../skaal/plugins.py) | ~80 | Philosophy |
| Collapse `@storage`/`@blob`/`@relational`/`@vector` to one | [decorators.py](../../skaal/decorators.py) | ~60 | API |

**Estimated total: ~1,500 LOC (≈ 26% of core).**

---

## Quick wins

### 1. Unify Pydantic serialization helpers
`_serialize` / `_deserialize` in [storage.py:72-101](../../skaal/storage.py#L72-L101) is duplicated in
[postgres_backend.py:35-36](../../skaal/backends/postgres_backend.py#L35-L36) (`_decode_jsonb`) and
re-implemented inline in Redis/DynamoDB backends as `json.loads(...) → model_validate(...)`.
Move to a single `skaal/serialization.py` and import everywhere.

### 2. Consolidate cursor validation
Identical `_validate_cursor()` exists at [sqlite_backend.py:20-31](../../skaal/backends/sqlite_backend.py#L20-L31)
and [postgres_backend.py:21-32](../../skaal/backends/postgres_backend.py#L21-L32). The
encode/decode helpers are already in `storage.py` — finish the job and delete both copies.

### 3. Generic decorator metadata helper
[decorators.py](../../skaal/decorators.py) defines 10+ decorators (`@storage`, `@blob`, `@relational`,
`@vector`, `@compute`, `@scale`, `@handler`, `@shared`, …) that all do the same three things:
normalize string→enum, build a metadata dict, attach `__skaal_*__` to the class. A single
`_apply_metadata(cls, kind, **kw)` helper turns each decorator into a 3-line wrapper.

---

## Medium refactors

### 4. Replace the custom resilience stack with Tenacity + pybreaker
[runtime/middleware.py:40-212](../../skaal/runtime/middleware.py#L40-L212) reimplements:

- `_Breaker` (43-81): manual circuit-breaker state machine
- `_Bulkhead` (86-108): `asyncio.Semaphore` wrapper
- `_TokenBucket` / `_RateLimiter` (113-169): token-bucket rate limiter
- `_with_retry()` (194-211): exponential backoff with jitter

[Tenacity](https://github.com/jd/tenacity) covers retry/backoff (async-native), and
[pybreaker](https://github.com/danielfm/pybreaker) covers circuit breaking. Keep only the
`ResilientInvoker` orchestration shell. **~150 LOC removed**, plus battle-tested behavior
under concurrency.

### 5. Extract a `BaseRuntime` from Local and Mesh
[runtime/local.py](../../skaal/runtime/local.py) (1212 lines) and
[runtime/mesh_runtime.py](../../skaal/runtime/mesh_runtime.py) (540 lines) share ~90% of their
structure. Direct duplicates:

- `_patch_storage()` — local.py:96-180 ↔ mesh_runtime.py:126-170 (nearly identical)
- `_collect_functions()` — local.py:355-375 ↔ mesh_runtime.py:179-189
- `_public_functions()` — local.py:381-386 ↔ mesh_runtime.py:191-196
- `_invocation_target()` — local.py:388-393 ↔ mesh_runtime.py:198-203
- `readiness_state` property and the entire HTTP dispatch loop

A `BaseRuntime` ABC owning HTTP/auth/engines/storage-patching, with `_route_agent` and
`_dispatch_function` as the only abstract hooks, would remove ~250 LOC and make a third
runtime variant trivial.

### 6. Collapse the per-backend factory classmethods
[runtime/local.py:180-331](../../skaal/runtime/local.py#L180-L331) has five near-identical
classmethods (`from_sqlite`, `from_postgres`, `from_redis`, `from_dynamodb`, `from_firestore`).
Replace with a single `from_backend(app, name, **config)` that dispatches via
`plugins.get_backend(name)`. Each cloud-specific helper becomes a 2-line wrapper if kept at all.

---

## Philosophy shifts

### 7. SQLModel as the relational primitive
[relational.py](../../skaal/relational.py) (90 lines) is a thin wrapper over
SQLAlchemy/SQLModel that adds little. Postgres/SQLite backends each re-derive engines and
session management. Make `@app.relational()` validate that the class **is** a `SQLModel` and
expose the session directly. Users get a real ORM (Alembic, async sessions, full query DSL)
and Skaal sheds ~200 LOC of wrapper. The opinionation cost is low — anyone using relational
storage already knows SQLAlchemy.

### 8. Adopt APScheduler or Arq for scheduling
[schedule.py](../../skaal/schedule.py) defines `Every` / `Cron` Pydantic models but has no
runtime — local dev relies on mocks, prod relies on EventBridge/Cloud Scheduler with
hand-rolled cron→native conversions in each deploy builder. APScheduler validates cron
syntax, handles timezones/DST, and gives local users a real scheduler. The cloud builders
still need their own translation, but the local story stops being a stub.

### 9. Replace the pattern engines with LangGraph or Temporal
[runtime/engines/](../../skaal/runtime/engines/) ships four parallel engines (EventLog,
Projection, Saga, Outbox) with the same lifecycle (`start(context)`, `stop()`, polling loop,
retry/backoff). They are all instances of the same problem: subscribe to changes, transform,
write results. Either:

- **LangGraph** for in-process workflow DSL (lightweight),
- **Temporal** for distributed sagas with replay/versioning/observability built in.

Either choice eliminates ~300 LOC and gives users tooling Skaal will never out-compete.

### 10. fsspec for blob backends
The three blob backends (file/S3/GCS) under `skaal/backends/` each reimplement
`put_bytes`/`get_bytes`/`put_file` with bespoke key normalization.
[fsspec](https://filesystem-spec.readthedocs.io/) gives a unified async filesystem interface
and adds Azure, HDFS, MinIO, Hugging Face, etc. for free. ~150 LOC saved and the test story
gets simpler (memory FS is built in).

### 11. Drop the in-process plugin registry
[plugins.py](../../skaal/plugins.py) (221 lines) has three discovery layers:
in-process dict → entry-points → builtins. Most users never register a backend at runtime.
Either keep entry-points only (low cost) or hardcode the builtins and let advanced users
subclass directly. ~80 LOC of indirection gone.

### 12. Collapse storage decorators
[decorators.py](../../skaal/decorators.py) exposes `@storage`, `@blob`, `@relational`,
`@vector` as four sibling decorators that already overlap (`@storage(kind="blob")` works).
Make `@storage(kind=...)` the single entry point with subtype validators; keep `@compute`,
`@scale`, `@handler`, `@shared` since those are orthogonal concerns. Smaller mental model,
~60 LOC removed.

---

## Suggested sequencing

1. **Week 1 — Quick wins.** Serialization, cursor, decorator helper. Risk: low. Immediate
   clarity boost; preps later refactors.
2. **Week 2 — Medium refactors.** Tenacity migration first (isolated), then
   `BaseRuntime` extraction. The runtime work is the largest pure-LOC win and pays
   dividends every time a new transport variant is added.
3. **Week 3+ — Philosophy.** Each shift here needs a design decision and a user-facing
   migration note. Recommended order:
   - SQLModel (smallest user impact, most leverage)
   - fsspec (largely transparent to users)
   - APScheduler (improves local dev story)
   - Workflow engine adoption (largest design call)
   - Plugin layer simplification (last; depends on which extension points survive)

Each phase should ship with: passing test suite, a short migration note in `docs/`, and a
deprecation shim if the public API changes.
