# Skaal — user gaps report

A review of v0.2.0 from the user's point of view: things solo-dev → production users will reasonably want to do, what happens today, where the gap is, and how badly it hurts. Focus is **ergonomics + capability**; observability/security/license/maturity gaps live in [`what_is_needed_for_prod.md`](./what_is_needed_for_prod.md).

Severity legend:

- **P0** — fundamental gap. A common user story can't be expressed without dropping out of the framework, or the framework silently does the wrong thing.
- **P1** — friction. Possible, but the user has to reach around the framework or learn an undocumented escape hatch.
- **P2** — polish. The path works; the experience could be smoother.

Each finding cites file:line evidence verified against current code.

---

## Top of list — start here

If the next implementation pass is one PR, these are the items that buy the most user-visible improvement, ranked by reach × severity:

ADR 014 removed public HTTP routing/streaming from the "what Skaal should build next" list, and ADR 015 landed the first `Store[T]` pagination/index pass (`list_page`, `scan_page`, `query_index`, `SecondaryIndex`). The next coherent pass is now the remaining storage/runtime capability work.

1. **Blob / object storage tier** — there is no `@app.blob` and no S3/GCS backend, so any user with files drops the framework entirely. P0. ([§B.2](#b2-kv-store-and-storage-tiers), [ADR 016](./design/016-blob-storage-tier-implementation-plan.md))
2. **Agent persistent-state save/load** — `__skaal_persistent_fields__` is collected but the runtime never loads or persists it; "fields marked @persistent survive restarts" in the docstring is currently false. P0 correctness gap. ([§B.7](#b7-agents))
3. **`skaal init` / project scaffolding + `skaal dev` watch mode** — the pieces now exist only partially; Skaal still lacks a first-class zero-config `init` → `dev` onboarding path. P0 for adoption. ([§A.1](#a1-cli-zero-config-and-dev-loop), [ADR 020](./design/020-skaal-init-and-dev-implementation-plan.md))
4. **Solver-failure error messages with closest-match suggestions** — today an unsatisfiable plan surfaces as a Z3 stack trace. P0 for first-time users. ([§A.4](#a4-error-messages-and-validation))
5. **Catalog overrides per environment** (dev / staging / prod) without copy-pasting whole TOML files. P1 but hits everyone past the prototype stage. ([§A.5](#a5-catalog-ergonomics), [ADR 022](./design/022-catalog-overrides-implementation-plan.md))
6. **Relational migrations beyond `create_all`** — any team past first deploy will need schema versioning and rollback. P0. ([§B.3](#b3-relational-tier-skaalrelationalpy))
7. **Secret injection at deploy / runtime** — there is still no Skaal-level Secrets Manager / Secret Manager surface. P0. ([§B.6](#b6-compute--functions))
8. **Examples ladder and testing story** — the examples still do not cover agents, schedules, patterns, or test fixtures. P1. ([§A.2](#a2-testing-story), [§A.8](#a8-examples-dont-progress-and-miss-common-patterns))
9. **Backend-native cursor/index optimization** — the new `Store[T]` surface is present, but the built-in backends still materialize pages/index queries rather than mapping to native cursor or secondary-index primitives. P1 scalability gap. ([§B.2](#b2-kv-store-and-storage-tiers))
10. **Per-row TTL / cache semantics** — `retention` still influences planning rather than runtime expiry behavior. P0 for session/cache workloads. ([§B.2](#b2-kv-store-and-storage-tiers))

---

## A. Ergonomics gaps

### A.1. CLI zero-config and dev loop

**What users want:** `pip install skaal && skaal init && skaal dev` — get to "code reload on save, hitting localhost" without reading docs.

**What happens today:**
- `skaal init` now exists and scaffolds a starter project, but the happy path still ends in `skaal run` output rather than a dedicated dev command.
- There is still no first-class `skaal dev` watch-mode. Hot reload exists under `skaal run` (`skaal/cli/run_cmd.py`) via `skaal/cli/_reload.py`, but it is not surfaced as the obvious default entry point.
- `skaal run` requires `MODULE:APP` either as a positional or under `[tool.skaal]` in `pyproject.toml`. The fallback is real and the error message is good, but it is not surfaced in `--help`.
- No tab-completion install path documented (Typer supports it; nothing in the CLI registers it).

**Why it's awkward:** The first-run story is better than the original audit, but it is still split across partially-hidden surfaces. Users can scaffold and they can hot-reload, yet the framework still does not present that as one obvious `skaal init` → `skaal dev` workflow. Compare to `cargo new`, `npm init`, `vite`, or `django-admin startproject`, where the default inner loop is immediately legible.

**Severity:** P0 (adoption). Tracked in [ADR 020](./design/020-skaal-init-and-dev-implementation-plan.md). The remaining work is to turn the existing scaffolding + reload pieces into a first-class `skaal init` → `skaal dev` onboarding path.

---

### A.2. Testing story

**What users want:** Unit-test a `Module` in pytest with in-memory backends, no HTTP server, no temp directories.

**What happens today:**
- `LocalRuntime` (`skaal/runtime/local.py:29-82`) is instantiable from Python, but it is undocumented in the README and has no `pytest` fixture / context-manager helper.
- `skaal/api.py` exposes `run`, `serve_async`, `serve_blocking` but none yield a "started, ready, here is a callable" handle for tests.
- No `tests/` examples in `examples/` — users have nothing to copy.
- No documented in-memory backend for relational/vector tiers in tests; the local KV backend works, but `@app.relational` resolves to SQLite-on-disk by default.
- No mock/double for `Store[T]` — users have to register the class against a runtime they spun up by hand.

**Severity:** P1. A single `from skaal.testing import build_test_app` (or a `pytest_plugin` wired via `pyproject.toml` plugin entry-points) would land this. Add at least one example test under `examples/02_todo_api/tests/`.

---

### A.3. Decorator and constraint-syntax consistency

**What users want:** Pass the same string syntax to every decorator, with validation at decoration time.

**What happens today:**
- `Latency("< 5ms")` is parsed eagerly at construction (`skaal/types/constraints.py:52-83`) with a regex that rejects `"5ms"` (missing operator) — fine.
- `@storage(read_latency=...)` and `@relational(...)` accept either a `Latency` or a `str`. Decorators in `skaal/decorators.py:44-58` coerce strings.
- `@app.function(compute=Compute(latency="< 50ms"))` does **not** pre-coerce: the `Compute` dataclass takes the string straight (`skaal/decorators.py:223-264` flow). The mismatch surfaces only at solve time.
- `Throughput`'s unit regex captures any tail (`(.+)`), so `"> 1000 frobnications/s"` is accepted at parse time and only rejected (if at all) when the solver compares against catalog units.
- Constraint vocabulary is not documented anywhere user-facing: spaces, `<` vs `<=`, valid units, valid `access_pattern` strings, `metric` strings, `durability` enum strings — users learn by reading the source.

**Severity:** P1. Add a single page (`docs/constraints.md`) listing the grammar; tighten the `Throughput` parser to a closed set of units; coerce at every decorator boundary so `solve` only ever sees typed values.

---

### A.4. Error messages and validation

**What users want:** When something is wrong, the message names the file/decorator/constraint and suggests the fix.

**What happens today:**
- The CLI error boundary (`skaal/cli/_errors.py:18-39`) captures and prints exception text, hiding tracebacks unless `-vv`. Most cases are fine.
- An unsatisfiable solve surfaces as a generic exception with no "closest backend" hint or "you asked for `read_latency < 1ms` but the cheapest local backend is `sqlite` at `< 5ms`" guidance. Z3's UNSAT is a black box to a first-time user.
- `AccessPattern("badvalue")` raises a bare `ValueError: 'badvalue' is not a valid AccessPattern` from the enum — no list of legal values, no "did you mean…".
- Forgetting `pip install "skaal[vector]"` and using `@app.vector(...)` raises `ImportError` from inside the decorator — no "install the `vector` extra" message.
- `skaal/api.py` has four `except Exception:  # noqa: BLE001` paths (lines 589, 818, 870, 964) that swallow without logging. (Already noted in the prod-readiness review; keeping it here because the *user* feels these as "it just silently did nothing.")
- Bad TOML in a catalog surfaces as `tomllib.TOMLDecodeError` with no Skaal context.

**Severity:** P0 for the solver-UNSAT case; P1 for the rest. The solver case alone causes more "give up and move on" moments than any other ergonomic issue.

---

### A.5. Catalog ergonomics

**What users want:** Per-environment overrides (dev catalog inherits from base, swaps the storage tier); a way to ask "what backends could I pick?"; templates for `[storage.X.deploy]`.

**What happens today:**
- `skaal catalog` exists (`skaal/cli/catalog_cmd.py`) and prints backends; not advertised in any doc.
- No catalog inheritance / overlay. To run dev vs prod with different latency budgets, users copy `catalogs/local.toml` and edit. Drift is on the user.
- `Catalog.from_raw()` validates `[storage.X.deploy]` (`skaal/catalog/models.py:78-100`), but there is no published schema or "skaal catalog validate" command — users discover required fields only on failure.
- Catalog file lookup fallback chain (`skaal/catalog/loader.py:44-134`) is reasonable but invisible: when it fails, the error names the search list, but users do not know that list existed.

**Severity:** P1 across the board. Tracked in [ADR 022](./design/022-catalog-overrides-implementation-plan.md): a reserved `[skaal] extends = "..."` table for inheritance, `[skaal] remove = [...]` for narrowing, plus `skaal catalog validate <path>` and `skaal catalog sources <path>` subcommands.

---

### A.6. Plan/lock readability and "why this backend?"

**What users want:** Run `skaal plan --explain Storage.Profiles` and see the constraint, the candidate backends, and which constraint each one violated.

**What happens today:**
- `plan.skaal.lock` is JSON. `_print_plan_table` (`skaal/cli/plan_cmd.py:22-45`) renders it readably to stdout and includes a `reason` field per assignment, which is the strongest part of the UX.
- No `--explain`, no per-candidate breakdown, no "fell back from `dynamodb` because region absent in catalog."
- No `skaal diff plan.skaal.lock` integration to compare two plans pretty-printed.

**Severity:** P2 — but high-value the day a user's prod stack picks something different from dev.

---

### A.7. Migration UX

**What users want:** Dry-run the migration, see how many rows, see the planned stage transitions, get progress while shadow-writes catch up.

**What happens today (`skaal/cli/migrate_cmd.py:17-60`):**
- `start / advance / rollback / status` are wired.
- No `--dry-run`. No row-count or ETA. No documentation of the six stages from ADR 004 in the CLI help — users must read `docs/design/004-six-stage-migration.md` to know what `advance` does next.
- Rollback semantics are not explained at the CLI surface.

**Severity:** P1 for solo devs, P0 the first time a team runs a real migration on a system they trust.

---

### A.8. Examples don't progress and miss common patterns

**What users want:** A ladder — hello-world → CRUD → background jobs → events → agents → deploy.

**What happens today (`examples/`):**
- `01_hello_world`, `02_todo_api`, `03_dash_app`, `04_mesh_counter`, `05_task_dashboard` exist.
- No example uses `@app.agent` despite agents being a first-class feature.
- No example uses `@app.schedule`.
- No example uses `EventLog` / `Projection` / `Saga` / `Outbox` (the runtime engines exist — see §C — but users would not know).
- No example with auth, file upload, websockets, or a test file.
- The complexity curve across 01→05 is roughly flat: every example is a small KV/relational app.

**Severity:** P1. Adoption-shaped — users who want to evaluate Skaal for a "real" app cannot find prior art for half its surface area.

---

### A.9. Module mounting and cross-module references

**What users want:** Compose `auth` into `api` without name collisions or guessing how exports work.

**What happens today (`skaal/module.py:629-735`, `skaal/app.py:127-146`):**
- `module.export(...)` returns a `ModuleExport` with `.storage`, `.agents`, `.functions`, `.channels` as plain `dict[str, Any]` — IDE-opaque, not a typed namespace.
- `app.use(module)` namespaces by module name by default; `app.use(module, namespace=None)` merges into root and detects collisions only on already-exported symbols.
- `app.mount(module, prefix=...)` records the prefix but the relationship between the prefix and the URL surface generated by deploy is implicit.
- Cross-module references are by name string in the solver's graph; if module A imports `B.Users`, the wiring works but the user never types it.

**Severity:** P2 today (most users have one module), P0 once Skaal pitches "publish reusable `skaal-<x>` packages on PyPI" — the encapsulation story has to hold.

---

### A.10. Type hints for stored values

**What users want:** `await Counts.get("k")` returns `int` to mypy/Pyright.

**What happens today:**
- `Store[T]` is `Generic[T]` (`skaal/storage.py:55-160`); the methods are `classmethod`s using `T` from the class generic. Static checkers do follow this when the user writes `class Counts(Store[int]): ...`.
- `await` of `Store.list()` returns `list[tuple[str, T]]` — fine.
- The opacity is in `ModuleExport.storage["Users"]` (a plain `dict`) and in the absence of `Latency` literal-string typing, so `read_latency="< 5ms"` is just `str` to checkers.

**Severity:** P2.

---

## B. Capability gaps

### B.1. HTTP/API surface

ADR 014 reframed this surface. Skaal now treats `@app.function()` as compute plus resilience, and the supported path for public HTTP shape is `mount_asgi(...)` / `mount_wsgi(...)` with FastAPI, Starlette, Litestar, Flask, Dash, and similar frameworks. Skaal itself reserves the internal invoke seam (`POST /_skaal/invoke/<qualified_name>`) plus health/index helpers. See `docs/http.md` and `docs/design/014-http-routing-overhaul.md`.

Resolved by that cut: path/method routing, request validation, OpenAPI generation, and SSE/streaming are no longer Skaal-router gaps because the mounted framework owns them. The remaining gaps are around deploy-time policy wiring and first-party guidance.

| Want                                        | Today (`runtime/local.py`)                                                                                                    | Sev    |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ------ |
| Skaal-level auth/cors policy wiring to deploy targets | `APIGateway.auth` exists in `skaal/components.py:191-230`, but deploy targets still do not consistently consume it.           | **P0** |
| First-party auth / middleware examples      | Mounted frameworks can do this today, but the examples/docs stop at CRUD and streaming.                                       | P1     |
| WebSockets                                  | Available through mounted ASGI apps, but there is no Skaal-level deploy/runtime story beyond "bring your own framework."     | P1     |
| File upload / multipart                     | Available through mounted ASGI/WSGI apps; the internal invoke seam remains JSON-only by design.                               | P2     |
| Static asset serving                        | Available through mounted ASGI/WSGI apps or an upstream proxy; Skaal does not add its own asset layer.                        | P2     |

The main remaining user surprise is not path-param routing anymore; it is that Skaal is intentionally not the public HTTP router, so users need to start from the mounted-framework examples rather than from `@app.function()` itself.

---

### B.2. KV `Store` and storage tiers

Update: ADR 015 landed a first coherent `Store[T]` surface for cursor pagination and secondary-index lookup. The remaining gap is mostly backend efficiency and richer storage tiers, not basic expressiveness.

| Want                                       | Today                                                                                                       | Sev    |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | ------ |
| Pagination / cursor on `Store[T]`          | `Store.list_page(...)` and `scan_page(...)` now exist, but the bundled backends still materialize pages rather than using native cursor primitives. | P1 |
| Secondary indexes on `Store[T]`            | `SecondaryIndex(...)` and `Store.query_index(...)` now exist, but the bundled backends still evaluate declared indexes generically rather than provisioning native DB indexes. | P1 |
| Per-row TTL (`@storage(retention=...)`)    | `retention` is parsed and consumed by the solver for catalog matching (`solver/storage.py:78`) but no backend implements per-row expiry — the SQLite/Postgres/Local KV backends have no TTL fields. | **P0** for session stores |
| Blob / object tier                         | No `@app.blob`, no S3 / GCS backend in `skaal/backends/` or `pyproject.toml` plugin entry points. Tracked in [ADR 016](./design/016-blob-storage-tier-implementation-plan.md). | **P0** |
| Cache layer (Redis-as-cache, not KV store) | Redis backend is a KV store; no TTL on `set`, no eviction policy plumbed.                                   | P1     |
| Cross-tier transactions                    | `Store.update()` is atomic within KV; relational session is atomic; no cross-tier primitive. Outbox covers the channel→storage case. | P1 |
| Multi-region replication                   | Solver and deploy are per-region. None.                                                                     | P2     |
| Encryption at rest config                  | Cloud defaults apply; not user-configurable through Skaal.                                                  | P2     |
| Full-text search                           | None; PostgreSQL `tsvector` is reachable via raw SQL only.                                                  | P1     |
| Time-series / graph                        | None.                                                                                                       | P2     |

**Note (correction to the verification agent):** the relational tier *is* now wired through SQLModel — `skaal/relational.py` and `PostgresBackend._ensure_relational_engine` (`backends/postgres_backend.py:95-107`, `242-255`). The KV facade is no longer the only option for SQL-resolving storage. DynamoDB and Firestore remain KV-only.

---

### B.3. Relational tier (`skaal/relational.py`)

| Want                                       | Today                                                                                                       | Sev |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | --- |
| Migrations beyond `create_all` (Alembic)   | `ensure_schema()` calls SQLAlchemy `create_all`. No version table, no diff, no rollback. ADR 004's six-stage flow is for KV migrations, not relational schema migrations. | **P0** for any team past first deploy |
| Joins                                      | Available via the yielded SQLModel `AsyncSession` — works, but undocumented.                                | P2  |
| Raw SQL escape hatch                       | `session.exec(text("..."))` works but is undocumented.                                                      | P2  |
| Read replicas                              | None.                                                                                                       | P1  |

---

### B.4. Vector tier (`skaal/vector.py`)

| Want                                       | Today                                                                                                       | Sev |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | --- |
| Hybrid search (vector + BM25 / keyword)    | `similarity_search` is vector-only.                                                                          | P1  |
| Rich metadata filtering                    | A `filter` dict is passed to the backend; expressiveness is whatever the LangChain adapter supports.        | P1  |
| Re-embedding on model change               | None; changing `__skaal_embeddings__` orphans existing vectors.                                              | P1  |
| Namespaces / collections                   | One per `@app.vector` class; cross-namespace queries require multiple decorators.                            | P2  |

---

### B.5. Channels / events / queues

| Want                                       | Today                                                                                                       | Sev |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | --- |
| Consumer groups                            | `EventLog.subscribe(group=...)` exists (`patterns.py:119-151`); on Redis Streams the backend honours groups (`backends/redis_channel.py:107`). On the local KV-backed log, it's a single offset per group. | P1 |
| Dead-letter queue                          | None. Projection engine swallows handler exceptions silently (`runtime/engines/projection.py:52-56`); the comment promises a "strict-mode" path "in a later phase." | **P0** for production |
| Replay by timestamp                        | Replay is by integer offset (`patterns.py:112-117`); no time index.                                         | P1  |
| Exactly-once                               | `Outbox.delivery` accepts `"exactly-once"` (`patterns.py:316`) but the OutboxEngine guarantees only at-least-once relay; user code must dedupe.                                                       | P1 documented gap |
| Durable subscription cursors across restart | Cursors live in the storage backend the channel resolves to. Local backend = lost on restart; Redis = durable. | P1 conditional |
| Channel-to-channel routing                 | None — user writes a function.                                                                              | P2  |

---

### B.6. Compute / functions

| Want                                       | Today                                                                                                       | Sev |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | --- |
| Long-running / background jobs             | `@app.function` is request/response. `@app.schedule` is recurring. No "fire-and-forget" or "delayed once" primitive — users build it from a Channel + worker function. | **P0** |
| One-shot delayed jobs ("remind me in 1 h") | None; `Cron`/`Every` only.                                                                                   | P1  |
| Streaming response / async generator       | `app.invoke_stream(...)` now covers the mounted-HTTP path, but returning a stream directly from the raw internal invoke route is still not a first-class deploy/runtime primitive. | P1 |
| GPU compute                                | Solver carries `compute_type` ("cpu","gpu","tpu") (`types/compute.py`) but the bundled catalogs (`catalogs/aws.toml`, `catalogs/gcp.toml`) define no GPU instance options. | **P0** for ML |
| Custom container images                    | Deploy generators bundle Python deps; no path to "ship this Dockerfile."                                     | P1  |
| Secret injection at deploy / runtime       | No integration with AWS Secrets Manager / GCP Secret Manager / Pulumi secrets. `connection_env` slot exists in `ExternalComponent` (`components.py:79-100`) but is not consumed. | **P0** |
| Env-specific dependencies                  | None.                                                                                                       | P1  |
| Per-call retry / circuit breaker for *outbound* HTTP | Function-level resilience (`runtime/middleware.py`) wraps the function as a whole, not its outbound HTTP calls. | P1 |
| Returning a stream from `@app.function`    | Supported through `app.invoke_stream(...)` when a mounted ASGI app owns the HTTP response; the internal invoke seam stays JSON-only. The remaining gap is deployment guidance, not local capability. | P1 |
| Scheduled functions invocable via HTTP     | `@app.schedule` functions are also exposed via POST (`local.py:332`). Convenient for testing, awkward for security — they have no auth and their existence isn't advertised in `GET /`. | P1 |

---

### B.7. Agents

| Want                                       | Today                                                                                                       | Sev |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | --- |
| Persistent fields survive restart          | `__skaal_persistent_fields__` is collected on the class (`agent.py:42-64`) but **no runtime code reads it.** Grep shows two hits, both in `agent.py`. The docstring claim is currently false. | **P0** correctness |
| Concurrency control on a single identity   | The runtime does not serialize calls to the same agent; user must add `asyncio.Lock`.                        | **P0** correctness |
| Agent-to-agent calls                       | None; mediate via Channel.                                                                                   | P1  |
| Sharding / placement                       | None.                                                                                                        | P1  |
| Leasing / eviction                         | None; long-running clusters leak.                                                                            | P1  |
| Timers / reminders                         | None — combine Channel + scheduled function.                                                                 | P1  |
| Observe agent state without invoking       | Expose a `get_state` handler manually.                                                                       | P2  |

The persistence gap is the single most surprising one — the README and class docstring both promise it.

---

### B.8. Schedules (`skaal/schedule.py`, `module.schedule`)

| Want                                       | Today                                                                                                       | Sev |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | --- |
| Distributed lock (don't fire on every replica) | None at the framework level. APScheduler-local fires once per process; cloud schedulers fire once per rule but multi-instance Skaal deployments behind a custom invoker don't have a documented contract. | **P0** for multi-replica |
| Overlapping-firing policy                  | Scheduler-defined, not exposed.                                                                              | P1  |
| Missed-fire policy                         | Scheduler-defined, not exposed (`misfire_grace_time` etc.).                                                  | P1  |
| Timezone correctness                       | `timezone="..."` is accepted (`module.py:513`) and passed through; cloud-side honoring is unverified end-to-end. | P1 |
| Dynamic schedules at runtime               | Schedules are decorator-static.                                                                              | P1  |

---

### B.9. Multi-target / multi-env / secrets

| Want                                       | Today                                                                                                       | Sev |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | --- |
| `dev` / `staging` / `prod` with different storage choices | Stack profiles (`api.py:408-423`) carry `env`, `invokers`, `labels`, `enable_mesh` per stack — *not* a backend override. Users must maintain separate catalogs. | **P0** for teams |
| Multi-region                               | One `region` per deploy call; multi-region = scripted multiple calls.                                        | P1  |
| Multi-account                              | Pulumi can; Skaal has no surface for it.                                                                     | P1  |
| Canary / blue-green                        | None.                                                                                                        | P1  |
| Env-specific config (env vars / secrets)   | Pulumi config can be passed; no Skaal-level abstraction for "this app needs `DB_DSN`, fetch from Secrets Manager." | **P0** |
| Feature flags                              | None.                                                                                                        | P2  |

---

### B.10. Inter-service and external integration

| Want                                       | Today                                                                                                       | Sev |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | --- |
| Calling another Skaal app                  | No service-discovery / RPC primitive. Use HTTP.                                                              | P1  |
| External HTTP clients with retry / breaker | The function-scope policies don't cover individual outbound calls; users wire `httpx` + tenacity by hand.    | P1  |
| Publish to a non-Skaal Kafka / SNS         | Channels are Skaal-internal.                                                                                 | P1  |
| GraphQL / gRPC                             | None; mount via ASGI for GraphQL.                                                                            | P2  |

---

### B.11. Frontend / static / TLS

| Want                                       | Today                                                                                                       | Sev |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | --- |
| Static asset serving                       | None native. Dash/Flask works via `mount_wsgi` (`app.py:42-86`); a SPA needs ASGI mount + StaticFiles.       | P1  |
| Custom domain / TLS                        | Not in the deploy surface; configured in Pulumi or cloud console.                                            | P1  |
| CDN / edge functions                       | None.                                                                                                        | P2  |

---

### B.12. Data lifecycle and compliance

| Want                                       | Today                                                                                                       | Sev |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | --- |
| GDPR cascade delete across modules         | None; cascade is on the user.                                                                                | **P0** for privacy-regulated apps |
| Data residency enforcement                 | `residency` is parsed and the solver carries it (`solver/storage.py:74`) but multi-store residency consistency is not enforced. | **P0** for regulated industries |
| Backup / restore / point-in-time recovery  | Cloud-native; not surfaced through Skaal.                                                                    | P1  |
| Export / import to external systems        | None.                                                                                                        | P1  |

---

### B.13. AI / LLM-specific

Skaal markets the vector tier; users will reasonably expect more LLM affordances.

| Want                                       | Today                                                                                                       | Sev |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | --- |
| Streaming token responses                  | Function return is a single JSON value. (Repeated from §B.6.)                                                | **P0** |
| RAG primitive                              | Vector tier exists; users compose retrieval + LLM call by hand.                                              | P1  |
| LLM call abstraction (`@app.llm(...)`)     | None; users call `anthropic` / `openai` SDKs directly.                                                       | P1  |
| Eval harness                               | None.                                                                                                        | P2  |

---

## C. Things the agents flagged as missing that are actually present

The capability review draft over-claimed in a few places. These are present and working — implementation pass should *not* re-do them:

- **Projection / Saga / Outbox runtime engines**: the patterns are not metadata stubs. Real engines exist:
  - `skaal/runtime/engines/projection.py` — `ProjectionEngine` tails an `EventLog` and applies a registered handler, with checkpointing via `subscribe()`.
  - `skaal/runtime/engines/saga.py` — `SagaEngine` + `SagaExecutor`, supporting both coordination strategies.
  - `skaal/runtime/engines/outbox.py` — `OutboxEngine` drains via polling.
  - `skaal/runtime/engines/base.py:24-39` dispatches by isinstance.
  Real gaps in the patterns (silent error swallowing in `projection.py:52-56`, no DLQ, "strict mode" deferred) are listed under §B.5.
- **Per-function resilience policies** (retry, circuit breaker, rate limit, bulkhead) — implemented in `skaal/runtime/middleware.py` and wired in `skaal/runtime/local.py:66-71`. Gap is *outbound* HTTP, not function-level.
- **Request body size limit** — `_MAX_BODY_SIZE = 10 MiB` enforced (`runtime/local.py:14`, 429-440), returns 413.
- **`/health` endpoint** — exists (`runtime/local.py:353, 491, 598, 796, 852`). `/ready` and `/metrics` are still missing — see prod-readiness doc.
- **Structured logging** — present (`skaal/cli/_logging.py` JsonLogFormatter; ~120 log calls across the codebase). Gap is metrics/tracing, not logs.
- **SQLModel-backed relational tier** — present and wired through `skaal/relational.py` + `PostgresBackend._ensure_relational_engine`.

---

## Cross-references

- Operational/security/maturity gaps: [`what_is_needed_for_prod.md`](./what_is_needed_for_prod.md)
- Storage tier rationale: [`new_storage.md`](./new_storage.md)
- Architecture decisions: [`design/`](./design/)
