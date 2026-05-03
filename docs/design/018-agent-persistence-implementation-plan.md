# ADR 018 — Agent Persistence & Local Invocation Implementation Plan

**Status:** Proposed
**Date:** 2026-04-30
**Related:** [user_gaps.md §B.7](../user_gaps.md#b7-agents), [skaal/agent.py](../../skaal/agent.py), [ADR 015](015-store-surface-implementation-plan.md), [ADR 017](017-production-runtime-baseline-implementation-plan.md)

## Goal

Make `@app.agent(persistent=True)` actually persistent, and give agents a first-class local-runtime invocation surface.

This pass closes two coupled gaps from `user_gaps.md`:

1. **Persistence is a lie today.** `__skaal_persistent_fields__` is collected on the class ([agent.py:42-64](../../skaal/agent.py#L42-L64)) but no runtime code reads or writes it. The `Agent` docstring claims "Fields marked @persistent survive restarts" — currently false.
2. **Agents have no local-runtime invocation path.** `LocalRuntime` ([skaal/runtime/local.py](../../skaal/runtime/local.py)) does not register, route, or instantiate agents. Users who declare an agent in an example cannot call it without bypassing the framework.

These two ship together because (a) persistence is meaningless without an invocation seam that materializes the agent instance, and (b) an invocation seam without persistence reproduces the same broken contract.

## Why this is next

ADR 016 covers blob storage, ADR 017 covers production runtime baseline (auth, /ready, OTel). The remaining P0s in the user-gaps top list are split between adoption ergonomics (`skaal init`/`dev`, solver-error UX) and correctness gaps. Agent persistence is the **only correctness P0** with no plan and no parallel work; it is also the smallest coherent code-only pass that closes a documented-but-broken promise.

The companion P0 in §B.7 — single-identity concurrency control — is in scope here because it is unavoidable once an invocation seam exists (concurrent calls to the same identity must serialize against the persisted state).

## Scope

This pass includes:

- A persistent-state load/save protocol invoked around handler dispatch.
- An `AgentStore` abstraction backed by an existing `Store[T]` so persistence rides on the storage tier already chosen by the solver — no new backend.
- A local invocation surface: `LocalRuntime` exposes `POST /_skaal/agents/<AgentName>/<identity>/<handler>` (mirrors the existing `_skaal/invoke/...` shape from ADR 014).
- Per-identity serialization via an `asyncio.Lock` keyed by `(AgentName, identity)`.
- Extension of the `Persistent[T]` annotation handling so non-persistent fields are reset on each load.
- Tests covering: round-trip persistence across runtime restart, two concurrent calls to the same identity, two concurrent calls to different identities, `persistent=False` agents (no storage write), and unknown-identity creation.

This pass does **not** include:

- Distributed/mesh agent placement, sharding, leasing, or eviction (§B.7 P1 items — separate plan).
- Agent-to-agent calls or timers/reminders (§B.7 P1).
- HTTP auth on the agent route — inherits from ADR 017's `APIGateway.auth` once that lands; until then the route is unauthenticated, same as `/_skaal/invoke/*`.
- Mesh-runtime parity. `MeshRuntime` continues to forward to the Rust mesh; this plan only covers the Python local path. A follow-up will mirror the contract through gRPC.

## Design

### Persistence contract

Every agent class is associated with a single `Store[bytes]` (or `Store[str]` carrying JSON) named `__skaal_agents__<AgentName>`. The store is solver-resolved like any other; `@app.agent(persistent=True)` declares an implicit `@app.storage` requirement with `access_pattern="key-value"` and `read_latency` defaulted from the agent decorator.

For a call to identity `id`:

1. Acquire the per-`(AgentName, id)` lock.
2. `raw = await store.get(id)`; if present, JSON-deserialize and `setattr` each field in `__skaal_persistent_fields__` onto the instance. Fields outside the persistent set get their class-level default.
3. Run the handler.
4. Build `state = {f: getattr(inst, f) for f in __skaal_persistent_fields__}`; `await store.set(id, json.dumps(state))`.
5. Release the lock.

Step 4 runs even if the handler raises — partial state writes are explicitly out of scope (single handler = single transaction). On exception, the *pre-call* state is re-serialized so a failed handler cannot half-mutate persistent fields. (This is cheap and avoids a "did it save?" debugging cliff.)

### Invocation seam

`LocalRuntime` gains:

- `_agent_classes: dict[str, type[Agent]]` populated from `app._agents` at startup.
- `_agent_locks: dict[tuple[str, str], asyncio.Lock]` lazy-allocated per identity.
- A request handler matching `POST /_skaal/agents/{name}/{identity}/{handler}` with JSON body `{"args": [...], "kwargs": {...}}`.
- A direct Python helper `await runtime.invoke_agent(name, identity, handler, *args, **kwargs)` for tests and in-process callers.

Identity is a string. Non-string identities (UUIDs, ints) are coerced via `str(...)` at the seam — agents must not rely on identity type round-tripping.

### `Persistent[T]` typing

`Persistent[T]` already exists in `skaal.types` and is detected by `Agent.__init_subclass__`. No change to the public surface. The detector is extended to also recognize `Annotated[T, Persistent]` so users can stack it with other markers without the ordering hazard.

### Concurrency

Per-identity lock guarantees serial execution of handlers against the same `(AgentName, identity)` within a single runtime process. Cross-process serialization is **not** provided in this pass — multi-replica deployments must wait for the mesh-runtime mirror. This is documented prominently next to the lock; users running multi-replica deploys of persistent agents today already have no guarantees, so this does not regress anything.

## Files touched

- `skaal/agent.py` — `Persistent` detector tightened; add `_serialize_state` / `_load_state` helpers on `Agent`.
- `skaal/runtime/local.py` — agent route, `invoke_agent`, lock map, store-resolution wiring.
- `skaal/runtime/agent_registry.py` — extend `AgentRecord` with `last_persisted_at` (cheap; useful for debug).
- `skaal/module.py` — `@module.agent` declares the implicit storage requirement so the solver provisions the agent state store.
- `skaal/solver/solver.py` — accept the implicit requirement (no algorithmic change; it's just another `StorageRequirement`).
- `tests/runtime/test_agent_persistence.py` (new) — covers the five test scenarios from §Scope.
- `tests/runtime/test_agent_invocation.py` (new) — covers the HTTP and Python invocation paths.
- `examples/06_agent_counter/` (new) — first example using `@app.agent`; closes the §A.8 gap for agents.
- `docs/agents.md` (new short page) — documents the persistence contract, lock semantics, and the multi-replica caveat.

## Migration / compatibility

No public API changes. Existing agent classes (currently non-functional at runtime) gain a working invocation path — observable behavior changes from "404" to "works." The `Agent` docstring stops being a lie.

## Open questions

- **Custom serialization.** First cut uses `json.dumps` with default. Pydantic models and dataclasses will need a hook before the next milestone — flagged but deferred.
- **State size limit.** No per-call cap in this pass. The runtime's existing `_MAX_BODY_SIZE` (10 MiB) bounds inputs but not the persisted blob. A 1 MiB soft limit with a warning log is a candidate for a follow-up.
- **Eviction of in-memory instances.** This pass keeps the instance alive only for the duration of the call; every call rehydrates from the store. Caching live instances across calls is a future optimization once distributed placement is on the table.
