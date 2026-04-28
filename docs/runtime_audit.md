# Runtime submodule — audit & implementation plan

Status: Proposed
Scope: `skaal/runtime/` (LocalRuntime, MeshRuntime, engines, mixins)

This document captures the gaps found in the runtime submodule and a concrete
plan to close them. It is a working roadmap, not an ADR — items 1, 5 and 9
likely deserve their own ADR before the change lands.

---

## 1. Architectural map

| Layer | What it owns | Where |
|---|---|---|
| Declaration | `App`/`Module` collect storage, agents, functions, channels, patterns, schedules | `skaal/app.py`, `skaal/module.py` |
| Solver / plan | `PlanFile` — solved storage→backend assignments | `skaal/plan.py`, `skaal/solver/` |
| Local runtime | mixin stack (Core / Dispatch / Transport / Lifecycle / LocalServer / LocalScheduler) → Starlette + APScheduler + LocalMap/Sqlite/Chroma | `skaal/runtime/local.py` |
| Mesh runtime | smaller mixin stack + `SkaalMesh` PyO3 control plane (Rust) | `skaal/runtime/mesh_runtime.py`, `mesh/src/` |
| Mesh wrapper | `MeshClient` — typed Python surface over `SkaalMesh` (agents, state, migrations, channels, health) | `skaal/mesh/client.py` |
| Pattern engines | EventLog / Outbox / Projection / Saga; started by `start_engines_for(app, runtime)` | `skaal/runtime/engines/` |
| Public entry | `skaal.api.build_runtime` / `serve_async` / `run` | `skaal/api.py` |
| Deploy | Generated handlers (AWS Lambda, Cloud Run, local Docker) all instantiate `LocalRuntime` with the resolved plan | `skaal/deploy/templates/` |

Two facts shape the rest of this document:

- **Every shipping deploy uses `LocalRuntime`.** `MeshRuntime` is only reachable
  via `build_runtime(distributed=True)` and is not used by any generated
  template. Asymmetries between the two runtimes have therefore gone unnoticed.
- **The Rust mesh already owns** `AgentRegistry`, `InMemoryStateStore`,
  `MigrationController`, and `MeshChannel` (`mesh/src/lib.rs:44-50`). The Python
  `runtime/agent_registry.py` and `runtime/state.py` modules are leftover
  scaffolding from before the mesh existed and are not wired into either
  runtime.

---

## 2. Findings

### 2.1 Explicit "later phase" placeholders

- `engines/projection.py:55` — exception swallowing: *"strict-mode will surface
  via an observability hook in a later phase."* Neither strict-mode nor an
  observability hook exists.
- `engines/projection.py:58-61` — `checkpoint_every` block is a literal `pass`:
  *"reserved for snapshotting derived state in future versions."*
  `Projection.checkpoint_every` is parsed and counted but does nothing.
- `engines/eventlog.py:22-34` — engine has no real lifecycle; only fires a
  best-effort `connect()` probe and toggles a `_started` flag that is never
  read elsewhere.

### 2.2 Defined and exported but never wired into a runtime

- **`AgentRegistry` / `AgentRecord` / `AgentStatus`** (`agent_registry.py`) —
  re-exported in `runtime/__init__.py` and exercised only in
  `tests/runtime/test_runtime_modules.py`. Neither runtime instantiates it.
- **`InMemoryStateStore`** (`state.py`) — same story: re-exported, only used in
  tests, never instantiated by any runtime.
- **`Channel` ABC in `runtime/channels.py`** — `RedisStreamChannel` claims
  (in docstring) to "implement" this interface but does not subclass it. Only
  `LocalChannel` extends it. The base class is essentially decorative.
- **`SagaEngine.executor()` method** (`engines/saga.py:150`) — never called;
  sagas are reached via `context.sagas[name]`.

### 2.3 LocalRuntime / MeshRuntime asymmetries

`MeshRuntime` mixes in only Core / Dispatch / HttpTransport / Lifecycle, so
relative to `LocalRuntime` it is missing:

- `_LocalSchedulerMixin` → `@app.schedule(...)` jobs do not run under
  `MeshRuntime`.
- `_LocalServerMixin` → `MeshRuntime._serve_runtime` falls through to the
  inherited `_serve_skaal` only; user `_asgi_app` / `_wsgi_app` is silently
  ignored.
- Path/str inconsistency at `mesh_runtime.py:120-121` (string paths vs
  `Path(...)` in `local.py:97-101`).

### 2.4 Other smells

- `_local_server.py:81-92` (`build_asgi`) exposes the Skaal dispatcher;
  `_serve_wsgi` / `_serve_asgi` (`117-143`) mount the user app at `/` and only
  graft `/health`. So when a user ships a Dash/FastAPI app, the Skaal
  `POST /{function}` endpoints are not reachable. Possibly intentional, but
  not documented.
- `engines/projection.py:43-56` — `_stopping` is checked only after
  `subscribe()` yields. If the source is idle, `stop()` relies on
  `task.cancel()`; the event flag is informational only.
- `engines/outbox.py:30-31` — `setattr(self.outbox, "write", ...)` only
  attaches when `write` is missing; subsequent restarts re-use the earlier
  closure (capturing the original backend). Edge case if backends are
  rewired.
- `_dispatch.py:99-100` — bare `except Exception` returns
  `traceback.format_exc()` in the JSON response. Production deploys go
  through this same dispatcher (`deploy/templates/aws/handler.py:64`), so the
  leak ships.
- `_dispatch.py:33-37` — `_index_payload` / `_health_payload` are no-op
  extension points; only `MeshRuntime` overrides them. Fine, just noting.
- `channels.py:21-22` vs `channels.py:40-46` — base `Channel.subscribe` is
  `def`, `LocalChannel.subscribe` is `async def`. Functionally OK, but the
  override violates the declared signature.
- `_core.py:116-126` — `shutdown` closes engines and backends but never
  closes anything in `_backend_overrides` that did not get installed in
  `_backends`. Leaks a connection if the override targets a storage class
  that was renamed/removed.
- `Module._agents` is collected and surfaced in `_collect_all()`, but
  `_RuntimeCoreMixin._initialize_runtime_state` ignores them. `LocalRuntime`
  cannot actually run an agent today.

---

## 3. Solution shape per issue

### 3.1 `AgentRegistry` + `InMemoryStateStore` — pick one

This is the biggest design call.

- **Option A — delete the Python copies.** Drop the modules, drop their
  re-exports from `runtime/__init__.py`, keep tests only against `MeshClient`.
  Honest, but `LocalRuntime` (the runtime that ships) loses any agent/state
  surface entirely.
- **Option B — promote them to first-class local services.** Introduce a
  small Protocol in `skaal/types/runtime.py`:

  ```python
  class RuntimeServices(Protocol):
      agents: AgentsService     # register/update/list/route
      state:  StateService      # get/set/delete/keys
  ```

  `LocalRuntime` constructs the Python `AgentRegistry` /
  `InMemoryStateStore` and exposes them on `self`. `MeshRuntime` builds
  adapters that proxy to `MeshClient`. Then
  `_RuntimeCoreMixin._initialize_runtime_state` walks `app._collect_all()`
  for `Agent` subclasses (already discoverable via `__skaal_agent__`) and
  registers them.

  **Recommended** — matches the project's "local has parity with prod" claim
  in `docs/design/005-local-runtime-design.md:29-30`.

### 3.2 `MeshRuntime` missing scheduler + ASGI/WSGI mount

The mixin split is the fix: nothing in `_LocalSchedulerMixin`
(`_local_scheduler.py`) or `_LocalServerMixin` (`_local_server.py`) is
local-specific. Rename them to `_SchedulerMixin` / `_StarletteServerMixin`
and add them to the `MeshRuntime` MRO at `mesh_runtime.py:45`. Then port the
same `_serve_runtime` body from `LocalRuntime._serve_runtime`
(`local.py:187-216`) — or hoist that method into a shared mixin too, since
it is identical.

### 3.3 `ProjectionEngine` placeholders

Two patches against `engines/projection.py:39-61`:

- **Strict-mode**: add `strict: bool = False` to `Projection.__init__`
  (`patterns.py:181-188`) and surface it through `__skaal_pattern__`. In
  `_run`, replace the swallowing `continue` with `raise` when `strict`.
- **Checkpoint snapshot**: `Projection.target` already gives access to the
  derived store. Implement the hook by, at the `checkpoint_every` boundary,
  awaiting `target_backend.set(f"__projection__:{handler}:offset", offset)`.
  That is all the comment is asking for.

For observability, define one tiny `RuntimeObserver` Protocol
(`event_handled(name, offset)`, `event_failed(name, offset, exc)`,
`engine_started(name)`, `engine_stopped(name)`) and attach a default stdout
impl to `LocalRuntime`; mesh routes through its existing health snapshot.

### 3.4 `EventLogEngine._started` unused bookkeeping

Either remove it, or feed it into the observer above
(`engine_started` / `engine_stopped`). The probe at
`engines/eventlog.py:25-33` is fine; it is the dead flag that needs to go.

### 3.5 `OutboxEngine` minor robustness

`engines/outbox.py:30-31`: replace the conditional `setattr` with
always-overwrite, and resolve `_backend_of(self.outbox.storage)` inside the
closure on every call rather than capturing once. Cheap fix, removes the
rewired-backend footgun.

### 3.6 `SagaEngine.executor()` dead accessor

Delete the method (`engines/saga.py:150-153`). Sagas are reached via
`context.sagas[name]`; that is the documented and used path.

### 3.7 `Channel` ABC vs `RedisStreamChannel` mismatch

`channel.py:80-97` — `wire_local` instantiates a fresh `LocalChannel` per
user-channel and just wraps `publish/subscribe` into `send/receive`. The
ABC layer in `runtime/channels.py` adds nothing the wire functions do not
already provide; `RedisStreamChannel` does not even inherit from it.

- **Demote**: make the runtime `Channel` ABC's `subscribe` `async def`,
  make `RedisStreamChannel` inherit from it, and keep both in lockstep with
  a shared interface test.
- **Delete**: remove `runtime/channels.py:Channel`, keep `LocalChannel` as
  a concrete in-process queue helper used only by `wire_local`. Drop both
  from `runtime/__init__.py`.

**Recommended**: delete. Less surface, fewer half-truths in the docstrings.

### 3.8 `_dispatch` traceback leaking on 500

`_dispatch.py:99-100`: gate the `traceback.format_exc()` field behind a
`SKAAL_DEBUG` env var (or a `LocalRuntime(..., debug=False)` constructor
flag passed via `_RuntimeCoreMixin`). Production deploys go through this
same dispatcher (`deploy/templates/aws/handler.py:64` calls
`_runtime._dispatch` directly), so this leak ships.

### 3.9 `_serve_with_starlette` hides Skaal routes when a user app is mounted

This is a product decision, not a bug. Today `_local_server.py:40-45`
mounts user app at `/` with only `/health` grafted on top, so
`POST /{function}` is unreachable. Pick:

- Mount Skaal at `/_skaal/*` (namespaced) and user app at `/`.
- Or accept a `skaal_prefix=` kw on `mount_asgi` / `mount_wsgi` and mount
  user app there with Skaal at root.

**Recommended**: the first. Less invasive; document it as an addendum to
`docs/design/005-local-runtime-design.md`.

### 3.10 `MeshRuntime._patch_storage` path-vs-str inconsistency

`mesh_runtime.py:120-121`: copy `LocalRuntime._patch_storage` verbatim, or
extract both to a `_default_local_storage_factories(qname)` helper in
`_planning.py` and call it from both runtimes.

### 3.11 `_RuntimeCoreMixin.shutdown` may leak override-instantiated backends

`_planning.py:30-48` calls `wiring.instantiate(resource_name)` and stores
the result in `_backend_overrides`. `_patch_storage_backends` only adds
those instances to `self._backends` for storage classes the loop touches.
Anything that does not match a registered storage class leaks the
connection. Patch `_core.py:116-126` `shutdown` to also iterate
`self._backend_overrides.values()` and `await close()` on anything not
already closed.

### 3.12 Agents declared but never wired

`Module._agents` is collected, but `_RuntimeCoreMixin._initialize_runtime_state`
never iterates them. Fixing 3.1 (Option B) gives the registry; the second
half is an `AgentDispatcher` that shards calls by `agent_id`
(single-threaded per identity, per `agent.py:23-39`) and hydrates
`Persistent[T]` fields from the state service. This is the largest piece of
work and probably belongs in a follow-up ADR rather than a runtime cleanup
pass.

---

## 4. Suggested order of attack

1. **Cleanups (no design needed)** — 3.5, 3.6, 3.8, 3.10, 3.11. Pure code
   hygiene; can land in one PR.
2. **Mixin re-parenting** — 3.2, plus fixing `_serve_runtime` duplication.
   Net effect: `MeshRuntime` gets parity with `LocalRuntime` for free.
3. **Channel ABC decision** — 3.7. Pick demote-or-delete; either way the
   public surface in `runtime/__init__.py` shrinks.
4. **Observer + projection completeness** — 3.3 and 3.4 together (one
   observer, two engines feeding it). Closes the explicit "later phase"
   comments.
5. **`RuntimeServices` protocol + agent wiring** — 3.1 (Option B) then
   3.12. ADR-worthy: it changes how local and mesh runtimes look from the
   outside, and it touches `start_engines_for(app, context)` (engines start
   using `context.agents` / `context.state`).
6. **ASGI/WSGI mount semantics** — 3.9. Smallest user-facing API change but
   worth a deliberate decision.

---

## 5. Out of scope

- Non-runtime production gaps already tracked in
  `docs/what_is_needed_for_prod.md` (logging, metrics, auth, secrets, real
  per-entity SQL backends).
- Replacing the Rust mesh's in-memory state store with a distributed
  backend (etcd / Redis); see `docs/design/006-rust-mesh-architecture.md`.
- Schema versioning / migration ergonomics; see
  `docs/design/007-schema-versioning.md`.
