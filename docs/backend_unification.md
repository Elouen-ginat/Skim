# Backend code — unification plan

Status: Proposed
Scope: `skaal/backends/`, `skaal/deploy/backends/`, `skaal/runtime/`,
`skaal/plugins.py`, `skaal/channel.py`, plus the entry-points block in
`pyproject.toml`.

This document describes the duplication that has accumulated between three
"backend" surfaces in the codebase, and a step-by-step plan to consolidate
them behind one canonical contract.

---

## 1. Where backend code lives today

| Location | What it owns | Public surface |
|---|---|---|
| `skaal/backends/*_backend.py` | Concrete async classes that satisfy `StorageBackend` (LocalMap, SqliteBackend, RedisBackend, PostgresBackend, ChromaVectorBackend, PgVectorBackend, DynamoBackend, FirestoreBackend) | `from skaal.backends import RedisBackend`, etc. |
| `skaal/backends/redis_channel.py` | `RedisStreamChannel` — a pub/sub channel impl, not a `StorageBackend` | imported by `skaal.channel.wire_redis()` |
| `skaal/backends/__init__.py` | Re-exports above; `__getattr__` legacy shim that proxies legacy names through `skaal.plugins.get_backend(...)` | `from skaal.backends import …` |
| `skaal/plugins.py` | Generic plugin registry: 3 entry-point groups (`skaal.backends`, `skaal.channels`, `skaal.catalogs`) + in-process registration | `register_runtime_backend`, `get_backend`, `get_channel`, `register_catalog` |
| `pyproject.toml` `[project.entry-points."skaal.backends"]` | Names → class import paths: `local`, `sqlite`, `redis`, `postgres`, `chroma`, `pgvector`, `dynamodb`, `firestore` | discovered by `skaal.plugins` |
| `skaal/deploy/backends/*.py` | `BackendPlugin` records: name, kinds, wiring (class_name, module, env_prefix, path_default, dep sets, requires_vpc, local_service, …), supported_targets, local_fallbacks | `BUILTIN_BACKENDS` tuple |
| `skaal/deploy/registry.py` | `BackendRegistry` and `TargetRegistry`; eagerly populated at import time from `BUILTIN_BACKENDS` and `BUILTIN_TARGETS` | `register_backend_plugin`, `resolve_backend_plugin` |
| `skaal/deploy/plugin.py` | `Wiring` dataclass + `BackendPlugin` dataclass + `resolve_wiring` | imported by every deploy backend file |
| `skaal/deploy/wiring.py` | `resolve_backend(spec, target)` → `ResolvedBackend(plugin, wiring)`; `build_runtime_wiring(plan, target)` for codegen | called by runtime planning + deploy templates |
| `skaal/deploy/backends/local_services.py` | `COMPOSE_SERVICES` dict (Postgres / Redis / Traefik / Kong sidecar definitions) | consumed by `local_compose` builder |
| `skaal/deploy/dependencies.py` + `data/dependency_sets.toml` | Dep-set name → pip requirements list, used by Wiring's `dependency_sets` | resolved during artifact build |
| `skaal/runtime/_planning.py` | `build_backend_overrides(app, plan, target)` (uses deploy `resolve_backend`); `_default_local_storage_factories()` (direct imports of `LocalMap` / `SqliteBackend` / `ChromaVectorBackend`); `_development_storage_binding(kind, mode, …)` (returns deploy-plugin name strings) | LocalRuntime + MeshRuntime |
| `skaal/runtime/local.py`, `mesh_runtime.py` | Call `_patch_storage_backends(store_factory, vector_factory, relational_factory)` with the three factories above | runtime construction |
| `skaal/runtime/_core.py` | `_patch_storage_backends(...)` walks `app._collect_all()`, picks override-or-factory, and calls `cls.wire(backend)` / `wire_relational_model(obj, backend)` / `VectorStore.wire(backend)` | runtime construction |
| `skaal/storage.py`, `skaal/relational.py`, `skaal/vector.py` | Per-storage-kind `.wire(backend)` entry points that store the backend on the class | storage decorator API |
| `skaal/channel.py` | User-facing `Channel[T]`; `wire_local` / `wire_redis` (instantiates `LocalChannel` / `RedisStreamChannel`) | discovered via `skaal.channels` entry-point group |
| `skaal/runtime/channels.py` | A separate `Channel` ABC + `LocalChannel` (publish/subscribe topics) used only inside `wire_local` | re-exported but functionally redundant |
| `skaal/catalog/data/*.toml` | Catalogs reference deploy-plugin names (`local-map`, `sqlite`, `rds-postgres`, …) | consumed by the solver |

---

## 2. The three problems

### 2.1 Two parallel registries with different naming

There are two registries, each authoritative for a different consumer:

- **`skaal.plugins`** — discovers entry-point group `skaal.backends`. Names
  are short class identifiers: `local`, `sqlite`, `redis`, `postgres`,
  `chroma`, `pgvector`, `dynamodb`, `firestore`. Maps directly to a Python
  class.
- **`skaal.deploy.registry.backend_registry`** — eagerly populated from
  `BUILTIN_BACKENDS`. Names are deploy-context strings: `local-map`,
  `sqlite`, `local-redis`, `memorystore-redis`, `chroma-local`,
  `rds-postgres`, `rds-pgvector`, `cloud-sql-postgres`,
  `cloud-sql-pgvector`, `dynamodb`, `firestore`. Maps to a `BackendPlugin`
  with kinds, supported_targets, fallbacks, and `Wiring` (which itself
  knows how to import the backend class via
  `import_module(f"skaal.backends.{module}")`).

Consequences:

- A name like `"sqlite"` exists in both registries and refers to the same
  thing — but a name like `"redis"` (entry-points) and `"local-redis"`
  (deploy) are the same Python class with two different identities.
- Catalog TOML uses deploy-plugin names. User code that does
  `from skaal.backends import RedisBackend` resolves through the
  entry-point registry. The two never cross-validate.
- Third-party storage backends installed via the `skaal.backends`
  entry-point group are invisible to deploy (no `BackendPlugin`, no
  fallbacks, no `Wiring`).
- `Wiring.instantiate` does its own `import_module` and bypasses
  `skaal.plugins.get_backend` entirely.

### 2.2 Backend metadata is split across two trees

Per backend, the implementation lives in `skaal/backends/<x>_backend.py`
(class) and the metadata lives in `skaal/deploy/backends/<x>.py`
(`BackendPlugin`). Adding a new backend means editing two trees, plus
`pyproject.toml` (entry-point row) and possibly
`skaal/deploy/data/dependency_sets.toml`.

`_postgres.py` is a partial workaround: a `postgres_kv_plugin` /
`postgres_vector_plugin` factory shared by `rds_postgres.py`,
`cloud_sql.py`. The same idea can apply to `redis_local.py` /
`memorystore_redis.py` and `chroma_local.py` (a future hosted Chroma).

### 2.3 The runtime has a third path for "no plan"

`runtime/_planning._default_local_storage_factories` directly imports
`LocalMap`, `SqliteBackend`, `ChromaVectorBackend` and is the only path used
when the runtime is constructed without a plan (the "memory mode" default
in `LocalRuntime.__init__`). Every other path goes through deploy's
`resolve_backend(...).wiring.instantiate(...)`.

That means there are three resolution paths:

1. `runtime_plan=...` → `build_backend_overrides` → `resolve_backend` → `Wiring.instantiate` → import.
2. `backend_overrides=...` → user gives concrete instances.
3. neither → `_default_local_storage_factories` → direct imports.

Path 3 bypasses both the plugin registry and the deploy registry. It cannot
be overridden by entry-point plugins, ignores `local_fallbacks`, and is
the reason `_planning.py` still needs hard-coded class imports.

### 2.4 Channels are filed under storage backends

`skaal/backends/redis_channel.py` does not implement the `StorageBackend`
protocol; it is a Redis-Streams pub/sub implementation. It is in the
storage-backends folder only because nothing else fit.

A second `LocalChannel`/`Channel` ABC pair exists in
`skaal/runtime/channels.py` that is functionally redundant with
`skaal/channel.py` (`wire_local`). See the runtime audit
(`docs/runtime_audit.md` § 3.7) for the deletion vs demote decision.

---

## 3. Target architecture

Goal: **one backend = one module = one record**.

```
skaal/
├── backends/
│   ├── __init__.py              # discovery via skaal.plugins; small surface
│   ├── base.py                  # StorageBackend Protocol (unchanged)
│   ├── _registry.py             # the single BackendRegistry (moved from deploy)
│   ├── _spec.py                 # BackendSpec (renamed BackendPlugin) + Wiring
│   ├── kv/
│   │   ├── local_map.py         # LocalMap class + spec(s) ("local-map")
│   │   ├── sqlite.py            # SqliteBackend + spec(s) ("sqlite")
│   │   ├── redis.py             # RedisBackend + specs ("local-redis", "memorystore-redis")
│   │   ├── postgres.py          # PostgresBackend + specs ("rds-postgres", "cloud-sql-postgres")
│   │   ├── dynamodb.py          # DynamoBackend + spec ("dynamodb")
│   │   └── firestore.py         # FirestoreBackend + spec ("firestore")
│   ├── vector/
│   │   ├── chroma.py            # ChromaVectorBackend + spec ("chroma-local")
│   │   └── pgvector.py          # PgVectorBackend + specs ("rds-pgvector", "cloud-sql-pgvector")
│   └── channels/
│       ├── local.py             # LocalChannel + wire_local
│       └── redis.py             # RedisStreamChannel + wire_redis
└── deploy/
    └── backends/                # DELETED — folded into skaal/backends/*
```

### 3.1 One spec dataclass per backend module

Today: `skaal/backends/redis_backend.py` (class) plus
`skaal/deploy/backends/redis_local.py` and
`skaal/deploy/backends/memorystore_redis.py` (two specs).

After: `skaal/backends/kv/redis.py` exposes:

```python
class RedisBackend: ...                  # the StorageBackend impl

LOCAL_REDIS = BackendSpec(
    name="local-redis",
    kinds={StorageKind.KV},
    impl=RedisBackend,                   # direct class reference, no string lookup
    wiring=Wiring(
        env_prefix="SKAAL_REDIS_URL",
        uses_namespace=True,
        local_service="redis",
        local_env_value="redis://redis:6379",
        dependency_sets=("redis-runtime",),
    ),
    supported_targets={"local"},
)

MEMORYSTORE_REDIS = BackendSpec(
    name="memorystore-redis",
    kinds={StorageKind.KV},
    impl=RedisBackend,
    wiring=replace(LOCAL_REDIS.wiring, requires_vpc=True),
    supported_targets={"gcp"},
    local_fallbacks={StorageKind.KV: "local-redis"},
)
```

Key changes vs today's `BackendPlugin`:

- `class_name` + `module` strings → `impl: type[StorageBackend]` direct
  reference. `Wiring.import_statement` becomes
  `f"from {impl.__module__} import {impl.__name__}"` derived at codegen
  time.
- The class **and** all its deploy contexts ship in the same file.

### 3.2 One registry, three views

`skaal/backends/_registry.py` keeps a single `BackendRegistry` that holds
`BackendSpec` objects. It exposes three accessors:

```python
def get_spec(name: str) -> BackendSpec               # full record
def get_impl(name: str) -> type[StorageBackend]      # spec.impl
def resolve(spec: StorageSpec, *, target: str) -> BackendSpec
```

`skaal.plugins` becomes a thin facade over this registry for the existing
public API:

```python
def get_backend(name: str) -> type[StorageBackend]:
    return _registry.get_impl(name)
```

Entry-point plugins register a `BackendSpec` (not just a class), via a
single new group `skaal.backend_specs`. The legacy `skaal.backends`
group keeps working for one release with a compatibility shim that wraps
the bare class in a minimal spec (kinds=`{KV}`, supported_targets=`{}`).

### 3.3 Single resolution path in the runtime

Replace `_default_local_storage_factories` with a one-liner that builds a
default plan via the registry:

```python
def _default_local_plan(app: RuntimeApp) -> PlanFile:
    # Picks "local-map" / "chroma-local" / "sqlite" by storage kind
    # from the registry's "local" target.
    return build_development_plan(app, mode="memory")
```

`LocalRuntime.__init__` collapses to the existing two paths:

1. `runtime_plan=...` (or default plan from `_default_local_plan`) →
   `build_backend_overrides` → `BackendSpec.instantiate(resource_name)`.
2. `backend_overrides=...` → user gives concrete instances.

No more direct imports of `LocalMap` / `SqliteBackend` / `ChromaVectorBackend`
in `runtime/`.

### 3.4 Channels are first-class, not parked in `backends/`

Move:

- `skaal/runtime/channels.py:LocalChannel` → `skaal/backends/channels/local.py`
- `skaal/backends/redis_channel.py` → `skaal/backends/channels/redis.py`
- `skaal/channel.py:wire_local` / `wire_redis` → live next to their classes

Delete `skaal/runtime/channels.py:Channel` (the redundant ABC) per
`docs/runtime_audit.md` § 3.7.

`skaal.channels` entry-point group keeps its semantics (a wire function),
but the registered targets now live in `skaal/backends/channels/`.

### 3.5 Compose sidecars stay in deploy

`local_services.py:COMPOSE_SERVICES` is deploy-only build-output config
(YAML for `docker-compose.yml`). It does *not* belong in
`skaal/backends/` because it has no runtime counterpart. Keep it under
`skaal/deploy/local_services.py` (one level up from the deleted
`deploy/backends/` folder) and reference services by name from
`Wiring.local_service`.

---

## 4. Implementation steps

### Step 0 — non-breaking prep (1 PR)

- Add `skaal/backends/_registry.py` and `skaal/backends/_spec.py` as
  thin re-exports of `skaal.deploy.plugin.BackendPlugin` /
  `skaal.deploy.registry.backend_registry`. No behavior change. Updates
  internal imports in `runtime/_planning.py` to use the new path.
- Goal: callers stop seeing the registry as deploy-only.

### Step 1 — kill the runtime's third path (1 PR)

- Delete `_default_local_storage_factories` from
  `skaal/runtime/_planning.py`.
- Make `LocalRuntime.__init__` (and `MeshRuntime.__init__`) always build
  a plan when neither `runtime_plan` nor `backend_overrides` is given —
  they already call `build_development_plan(app, mode="memory")`, so this
  is removing the manual factory wiring in `_patch_storage` and letting
  `build_backend_overrides` produce the instances.
- Net diff: `runtime/local.py` and `runtime/mesh_runtime.py` lose
  `_patch_storage` overrides; `_RuntimeCoreMixin._patch_storage_backends`
  consumes the override map directly.
- Tests: `tests/runtime/test_local.py`, `test_mesh_runtime.py`,
  `test_local_runtime_extras.py` already cover memory-mode startup —
  they should pass unchanged.

### Step 2 — direct class references in `BackendSpec` (1 PR)

- Add `impl: type[StorageBackend] | None = None` to `Wiring` (or to a
  new `BackendSpec` wrapping `Wiring`).
- For each existing plugin in `skaal/deploy/backends/*.py`, populate
  `impl=...` alongside the legacy `class_name` / `module` strings.
- `Wiring.instantiate` prefers `impl` when present; falls back to the
  string-based lookup for backwards compatibility.
- `Wiring.import_statement` becomes `f"from {impl.__module__} import
  {impl.__name__}"` when `impl` is set.

This is the safest groundwork before the file moves: deploy templates
keep generating identical code, and the registry can resolve classes
without `import_module`.

### Step 3 — fold deploy backend files into `skaal/backends/` (1 PR per kind, or 1 big PR)

For each backend:

1. Move the implementation class into the new folder layout
   (`skaal/backends/kv/redis.py`, `skaal/backends/vector/chroma.py`, etc.).
   Keep the legacy module path as a compat re-export for one release:

   ```python
   # skaal/backends/redis_backend.py  (compat shim, removed in vNext)
   from skaal.backends.kv.redis import RedisBackend  # noqa: F401
   ```

2. Move the matching `BackendSpec` (or specs) into the same file. Delete
   the corresponding `skaal/deploy/backends/<x>.py`.

3. Replace the import in `skaal/deploy/backends/__init__.py:BUILTIN_BACKENDS`
   with the new locations. Once all backends moved, delete
   `skaal/deploy/backends/` and update `skaal/deploy/registry.py` to
   import `BUILTIN_BACKENDS` from `skaal.backends`.

Order suggestion: do `local_map` and `sqlite` first (smallest, used by
every test), then KV (`redis`, `dynamodb`, `firestore`), then relational
(`postgres`), then vector (`chroma`, `pgvector`).

### Step 4 — unify the registries (1 PR)

- `skaal.plugins.get_backend(name)` becomes
  `_backend_registry.get_impl(name)`.
- Add a `skaal.backend_specs` entry-point group; built-in registrations
  via `pyproject.toml`:

  ```toml
  [project.entry-points."skaal.backend_specs"]
  local-map = "skaal.backends.kv.local_map:LOCAL_MAP_SPEC"
  sqlite    = "skaal.backends.kv.sqlite:SQLITE_SPEC"
  ...
  ```

- Keep `[project.entry-points."skaal.backends"]` for one release with a
  deprecation warning logged on use; document that third-party packages
  should migrate to `skaal.backend_specs`.
- `skaal.deploy.registry.backend_registry` becomes an alias for
  `skaal.backends._registry.backend_registry`. Eventually removed in a
  later cleanup.

### Step 5 — relocate channels (1 PR)

- Create `skaal/backends/channels/` with `local.py` and `redis.py`.
- Move `skaal/backends/redis_channel.py` and the runtime-side
  `LocalChannel` into the new folder. Move `wire_local` / `wire_redis`
  next to their classes.
- Update `pyproject.toml`'s `skaal.channels` entries:

  ```toml
  [project.entry-points."skaal.channels"]
  local = "skaal.backends.channels.local:wire_local"
  redis = "skaal.backends.channels.redis:wire_redis"
  ```

- Delete `skaal/runtime/channels.py` (its `Channel` ABC was decorative;
  see `docs/runtime_audit.md` § 3.7). Drop re-exports from
  `skaal/runtime/__init__.py`.

### Step 6 — final cleanup (1 PR)

- Delete legacy compat shims from Step 3 (`skaal/backends/redis_backend.py`
  re-export and friends).
- Delete `skaal/deploy/registry.py:backend_registry` alias.
- Remove the legacy `skaal.backends` entry-point group from
  `pyproject.toml` (announce in CHANGELOG; major-version bump).
- Update `docs/new_storage.md` and `docs/design/003-catalog-toml-format.md`
  to point at the new layout.

---

## 5. Migration impact

| Affected surface | Impact |
|---|---|
| User code: `from skaal.backends import RedisBackend` | Works through Step 5 via compat shim. Removed in Step 6 (one release later). |
| Catalog TOMLs (`skaal/catalog/data/*.toml`, user catalogs) | No change — still reference deploy-plugin names. The names are unchanged because Step 3 keeps the same `BackendSpec.name`s. |
| Deploy templates (`skaal/deploy/templates/aws/handler.py`, `gcp/main.py`, `local/main.py`) | No change — they consume `LocalRuntime` only. Step 1 changes how `LocalRuntime` resolves no-plan defaults but the public constructor signature is unchanged. |
| Third-party `skaal.backends` entry-point plugins | Continue to work through Step 5. Step 4 introduces `skaal.backend_specs` as the recommended replacement; deprecation warning. |
| Tests | `tests/runtime/test_runtime_modules.py`, `tests/runtime/test_local.py` need import-path updates after Steps 3 / 5. |
| `MeshClient` channel/state/agent calls | Untouched — runtime services (`_services.py`) already consume `MeshClient`, not the backend registry. |

---

## 6. What this does *not* fix

- Per-entity SQL schema vs the current `skaal_kv(ns, key, value JSONB)`
  shim in `PostgresBackend` (`docs/what_is_needed_for_prod.md`). That is a
  separate workstream.
- Backend-level retry / circuit-breaker / pool tuning. The `Wiring.constructor_kwargs`
  hook is enough to plumb pool sizes today.
- The agent dispatcher / `Persistent[T]` hydration covered in
  `docs/runtime_audit.md` § 3.12.

---

## 7. Suggested order of attack

1. Step 0 (prep) — non-breaking re-exports.
2. Step 1 — single resolution path in the runtime. Removes the obvious
   duplication and is fully backwards-compatible.
3. Step 2 — direct class references in `BackendSpec`. Sets up Step 3.
4. Step 3 — file moves, kind by kind. Each kind is a small reviewable PR.
5. Step 5 — channels, after all storage backends moved.
6. Step 4 — registry unification. Done after the file moves so
   `skaal.backend_specs` registrations point at their final location.
7. Step 6 — drop compat shims, bump major version.
