# Skaal — exhaustive dispatch refactor plan

Status: Proposed
Scope: every place in the lib where a chain of `if/elif` branches on a tag
(string, class, kind) does what a registry, polymorphic method, or lookup
table would do better.

This is the umbrella plan; the [pattern→engine sub-plan](engine_dispatch.md)
is one of the items below (§1) and is referenced rather than re-described.

The project already has clean registry/Protocol designs in
[backends/_registry.py](../skaal/backends/_registry.py) and
[deploy/targets/base.py](../skaal/deploy/targets/base.py) — the work below
brings the rest of the library up to the same bar.

---

## Inventory

| #  | Location | Symptom | Severity |
|----|----------|---------|----------|
| 1  | [skaal/runtime/engines/base.py:31-39](../skaal/runtime/engines/base.py) | `isinstance` ladder over 4 pattern classes | high |
| 2  | [skaal/solver/solver.py:338-485](../skaal/solver/solver.py) | `ptype == "..."` ladder over the same 4 patterns | high |
| 3  | [skaal/runtime/_planning.py:103-179](../skaal/runtime/_planning.py) | nested `(mode, kind)` if-chain — a 2D table as control flow | high |
| 4  | [skaal/deploy/builders/_aws_stack_builder.py:384-399](../skaal/deploy/builders/_aws_stack_builder.py) and [gcp_stack.py:353-366](../skaal/deploy/builders/gcp_stack.py) | duplicated `trigger_type == "cron"` branch; ignores `Cron`/`Every` polymorphism that already exists | medium |
| 5  | [skaal/module.py:598-613](../skaal/module.py) | `bucket_name == "storage"/"agents"/"functions"/"channels"` → 4 parallel locals | low (mechanical) |
| 6  | [skaal/deploy/builders/local_compose.py:133, 187-200](../skaal/deploy/builders/local_compose.py) | `gw_comp.kind == "proxy"` and `implementation == "traefik"` checks repeated; gateway impl knowledge fanned out | low |
| 7  | [skaal/runtime/middleware.py:142-151](../skaal/runtime/middleware.py) | `_RateLimiter._key` switches on `scope` string with three cases | low |
| 8  | [skaal/runtime/engines/outbox.py:90-97](../skaal/runtime/engines/outbox.py) | `delivery == "at-least-once"` vs else — two-arm post-delivery hook | trivial |

Items 1-3 are the load-bearing ones (each gates the addition of new patterns
or new storage modes). Items 4-6 cluster around the same anti-pattern
(string-tag dispatch in deploy code). Items 7-8 are minor and listed for
completeness — fix only if touched anyway.

Out of scope, but worth noting: existing **good** designs we should mirror —
[BackendRegistry](../skaal/backends/_registry.py) (registry + `register` method),
[Target](../skaal/deploy/targets/base.py) (Protocol composition with `builder`
and `deployer`), [Schedule](../skaal/schedule.py) (`as_aws_expression()` /
`as_cron_expression()` polymorphism — currently bypassed by item 4).

---

## 1. Engine dispatch

See [engine_dispatch.md](engine_dispatch.md). Plan: `Pattern` marker protocol
in `skaal/patterns.py`, typed `register_engine` decorator + `_REGISTRY` in
`engines/base.py`, one decorator line per engine class.

Type hints at a glance:

```python
P = TypeVar("P", bound=Pattern)
EngineFactory = Callable[[P], PatternEngine]
_REGISTRY: dict[type[Pattern], EngineFactory[Any]] = {}
def register_engine(pattern_cls: type[P]) -> Callable[[EngineFactory[P]], EngineFactory[P]]: ...
```

This plan is the **prerequisite** for item 2 — the two should share their
`Pattern` protocol.

---

## 2. Solver pattern dispatch

### 2.1 Where

[skaal/solver/solver.py:338-485](../skaal/solver/solver.py), inside
`build_pattern_specs` (or whichever function holds the loop):

```python
if ptype == "event-log":
    ...                          # ~30 lines: backend selection + PatternSpec build
elif ptype == "projection":
    ...                          # ~47 lines: source/target resolution, co-locate, PatternSpec
elif ptype == "saga":
    ...                          # ~34 lines: function-name validation, PatternSpec
elif ptype == "outbox":
    ...                          # ~30 lines: borrow-backend, PatternSpec
```

### 2.2 Why this shape is wrong

- Adding a 5th pattern means: (a) new class in `skaal/patterns.py`, (b) new
  `Literal` in `PatternType`, (c) new engine in `engines/`, (d) new branch
  here. (a)+(c) become one decorator after item 1; this branch stays.
- Each branch closes over `storage_specs`, `storage_backends`, `target`,
  `registered_functions`, `all_resources` — there is no shared shape. A
  registry value must accept that shared context bag.

### 2.3 Plan — `register_pattern_solver` decorator

Create `skaal/solver/_pattern_solvers.py`:

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from skaal.patterns import Pattern
from skaal.plan import PatternSpec, PatternType, StorageSpec


@dataclass(frozen=True)
class PatternSolveContext:
    qname: str
    pattern_meta: dict[str, Any]
    all_resources: dict[str, Any]
    storage_specs: dict[str, StorageSpec]            # mutable — solvers may co-locate
    storage_backends: dict[str, Any]
    registered_functions: set[str]
    target: str | None


PatternSolver = Callable[[PatternSolveContext], PatternSpec]
"""Computes the PatternSpec for a single declared pattern instance."""


_REGISTRY: dict[PatternType, PatternSolver] = {}


def register_pattern_solver(pattern_type: PatternType) -> Callable[[PatternSolver], PatternSolver]:
    def _decorate(fn: PatternSolver) -> PatternSolver:
        if pattern_type in _REGISTRY:
            raise RuntimeError(f"solver already registered for pattern_type {pattern_type!r}")
        _REGISTRY[pattern_type] = fn
        return fn
    return _decorate


def solve_pattern(ctx: PatternSolveContext) -> PatternSpec | None:
    """Look up the registered solver by ``ctx.pattern_meta['pattern_type']``."""
    ptype = ctx.pattern_meta.get("pattern_type")
    fn = _REGISTRY.get(ptype) if isinstance(ptype, str) else None
    return fn(ctx) if fn is not None else None
```

Then move each branch's body into a top-level function in a per-pattern
module, e.g. `skaal/solver/patterns/event_log.py`:

```python
@register_pattern_solver("event-log")
def solve_event_log(ctx: PatternSolveContext) -> PatternSpec:
    pattern_constraints = _storage_constraints_from_pattern(ctx.pattern_meta)
    ...
    return PatternSpec(
        pattern_name=ctx.qname,
        pattern_type="event-log",
        backend=backend_name or None,
        reason=reason,
        config={...},
    )
```

The solver loop collapses to:

```python
for qname, obj in all_resources.items():
    pattern_meta = getattr(obj, "__skaal_pattern__", None)
    if not isinstance(pattern_meta, dict):
        continue
    ctx = PatternSolveContext(
        qname=qname,
        pattern_meta=pattern_meta,
        all_resources=all_resources,
        storage_specs=storage_specs,
        storage_backends=storage_backends,
        registered_functions=registered_functions,
        target=target,
    )
    spec = solve_pattern(ctx)
    if spec is not None:
        pattern_specs[qname] = spec
```

### 2.4 Type hints

| Symbol | Hint | Note |
|---|---|---|
| `PatternSolveContext` | frozen `@dataclass` | Immutable except `storage_specs` (its dict is shared) — comment that explicitly. |
| `PatternSolver` | `Callable[[PatternSolveContext], PatternSpec]` | Single-argument simplifies registration; bag carries the closure. |
| `_REGISTRY` | `dict[PatternType, PatternSolver]` | Keyed by the existing `PatternType = Literal["event-log","projection","saga","outbox"]` from `skaal/plan.py:14`. |
| `register_pattern_solver(pattern_type: PatternType)` | returns `Callable[[PatternSolver], PatternSolver]` | The `Literal` argument means typos fail at type-check time. |
| `solve_pattern(ctx) -> PatternSpec \| None` | `None` when `pattern_type` is unknown — preserves today's silent-skip behavior. |

### 2.5 Risks

- **Solver functions must run in the right order to observe side-effects on
  `storage_specs`.** Today projection mutates `storage_specs` (force
  co-location). The registry preserves insertion order for `dict`; document
  that ordering = registration order, and register `event-log` first so
  `projection` sees its source. Or split into two passes (collect → mutate)
  to remove the implicit ordering — a cleanup worth doing while we're here.
- **`PatternType` Literal stays the source of truth** — `register_pattern_solver`
  takes it directly so static checkers reject `register_pattern_solver("evt-log")`.

### 2.6 Migration

1. Add `skaal/solver/_pattern_solvers.py` with the dataclass + decorator + registry.
2. Add `skaal/solver/patterns/__init__.py` that imports
   `event_log`, `projection`, `saga`, `outbox` (forces decorators to run).
3. Move each branch body into its own file; keep helpers
   (`_storage_constraints_from_pattern`, `_resolve_resource_qname`) where they
   are or move them under `skaal/solver/_pattern_solvers.py`.
4. Replace the if/elif loop in `solver.py` with the 8-line version above.
5. Existing solver tests (which assert on `PatternSpec` outputs) cover behavior.

---

## 3. Runtime planning — `(mode, kind)` matrix

### 3.1 Where

[skaal/runtime/_planning.py:103-179](../skaal/runtime/_planning.py),
`_development_storage_binding`. The function dispatches on
`mode ∈ {"memory", "sqlite", "redis", "postgres"}` × `kind ∈ {"kv", "relational",
"vector"}` via 60+ lines of nested ifs, with several silent fall-throughs (e.g.
`postgres` is the implicit default reached only by falling off the bottom).

### 3.2 Why this shape is wrong

- It is a **lookup table pretending to be control flow**. The (mode, kind)
  coverage matrix is impossible to skim.
- Side-conditions (e.g. `redis_url is None` raises only inside the `redis`
  branch; `dsn is None` only inside the `postgres` branch) are mixed in.
- Adding a new mode (e.g. `"duckdb"`) requires editing the chain.

### 3.3 Plan — table-driven binder

```python
from typing import Callable, TypedDict

class BindParams(TypedDict, total=False):
    db_path: Path
    chroma_path: Path
    redis_url: str | None
    dsn: str | None
    min_size: int
    max_size: int

Binder = Callable[[BindParams], tuple[str, RuntimeWireParams]]

_BINDINGS: dict[tuple[RuntimeMode, StorageKindName], Binder] = {}

def _binding(mode: RuntimeMode, kind: StorageKindName) -> Callable[[Binder], Binder]:
    def _decorate(fn: Binder) -> Binder:
        _BINDINGS[(mode, kind)] = fn
        return fn
    return _decorate


@_binding("memory", "kv")
def _memory_kv(_: BindParams) -> tuple[str, RuntimeWireParams]:
    return "local-map", {
        "class_name": "LocalMap",
        "module": "skaal.backends.kv.local_map",
        "env_prefix": None,
        "path_default": None,
        "uses_namespace": False,
    }

@_binding("memory", "relational")
def _memory_relational(p: BindParams) -> tuple[str, RuntimeWireParams]:
    return "sqlite", {
        "env_prefix": None,
        "module": "skaal.backends.kv.sqlite",
        "path_default": str(p["db_path"]),
        "uses_namespace": True,
    }

# … one binder per cell …


def _development_storage_binding(
    kind: StorageKindName,
    *,
    mode: RuntimeMode,
    db_path: Path,
    chroma_path: Path,
    redis_url: str | None,
    dsn: str | None,
    min_size: int,
    max_size: int,
) -> tuple[str, RuntimeWireParams]:
    binder = _BINDINGS.get((mode, kind))
    if binder is None:
        raise ValueError(
            f"No development binding for mode={mode!r} × kind={kind!r}. "
            f"Supported pairs: {sorted(_BINDINGS)}"
        )
    return binder({
        "db_path": db_path,
        "chroma_path": chroma_path,
        "redis_url": redis_url,
        "dsn": dsn,
        "min_size": min_size,
        "max_size": max_size,
    })
```

Validation that today happens inside branches (e.g. `redis_url is None →
raise`) moves to the relevant binder, where it belongs.

### 3.4 Type hints

| Symbol | Hint | Note |
|---|---|---|
| `BindParams` | `TypedDict(total=False)` | All keys optional from the binder's view; the call site always passes all of them. |
| `Binder` | `Callable[[BindParams], tuple[str, RuntimeWireParams]]` | `RuntimeWireParams` already exists in [skaal/types/runtime.py](../skaal/types/runtime.py). |
| `_BINDINGS` | `dict[tuple[RuntimeMode, StorageKindName], Binder]` | `RuntimeMode` and `StorageKindName` are existing `Literal` types — typos fail at type-check time. |
| `_binding(mode, kind)` | returns identity decorator | Mirrors `register_engine` style. |

### 3.5 Risks

- **Coverage gap surfaces as `ValueError` instead of silent fall-through.**
  This is the point — today `mode="postgres", kind="vector"` returns the
  `rds-pgvector` branch by accident; an explicit binder makes the matrix
  inspectable. Add a unit test that asserts every `(mode, kind)` combo the
  CLI accepts is registered.
- **Mode-specific validation timing**: today `"redis without redis_url"`
  raises before the lookup; in the new shape it raises inside the binder.
  Same UX, different stack.

### 3.6 Migration

1. Define `_BINDINGS`, `BindParams`, `Binder`, `_binding` at module top.
2. Convert each branch body into a binder function. Keep the existing dict
   literals verbatim to keep diffs small.
3. Replace `_development_storage_binding` body with the 4-line lookup.
4. Add coverage test: `assert set(_BINDINGS) == {(m, k) for m in RuntimeMode_values for k in StorageKindName_values if (m, k) is supported}`.

---

## 4. Schedule trigger dispatch — AWS + GCP duplicated

### 4.1 Where

- [_aws_stack_builder.py:392-399](../skaal/deploy/builders/_aws_stack_builder.py)
- [gcp_stack.py:361-366](../skaal/deploy/builders/gcp_stack.py)

Both look like:

```python
trigger_type = cfg.get("trigger_type", "cron")
if trigger_type == "cron":
    expr = Cron(expression=cfg["trigger"]["expression"]).as_aws_expression()
else:
    expr = Every(interval=cfg["trigger"]["interval"]).as_rate_expression()
```

### 4.2 Why this shape is wrong

[skaal/schedule.py](../skaal/schedule.py) **already** defines `Cron` and
`Every` with `as_aws_expression()` / `as_rate_expression()` /
`as_cron_expression()` polymorphic methods. The builders re-discriminate
because the plan stores `cfg["trigger"]` as a dict, throwing away the type.

### 4.3 Plan

Two complementary changes:

**A. Persist the typed schedule, not a dict.** When the planner writes a
`schedule-trigger` component, store the `Schedule` model directly (Pydantic
already serialises). Re-validate on read into a `Schedule = Cron | Every`
discriminated union via `pydantic.TypeAdapter`:

```python
from pydantic import TypeAdapter
from skaal.schedule import Schedule  # = Cron | Every

_schedule_adapter: TypeAdapter[Schedule] = TypeAdapter(Schedule)

def _load_schedule(cfg: dict[str, Any]) -> Schedule:
    return _schedule_adapter.validate_python(cfg["trigger"])
```

**B. Add `to_aws_expression()` / `to_gcp_expression()` shared methods on
`Schedule`** — they already exist piecewise; promote them to a common
interface so callers do `schedule.to_aws_expression()` regardless of subtype:

```python
class _ScheduleProto(Protocol):
    def to_aws_expression(self) -> str: ...
    def to_gcp_expression(self) -> str: ...

class Cron(BaseModel):
    expression: str
    def to_aws_expression(self) -> str:
        min_, hr, dom, mon, dow = self.expression.split()
        return f"cron({min_} {hr} {dom} {mon} {dow} *)"
    def to_gcp_expression(self) -> str:
        return self.expression  # GCP accepts standard 5-field cron

class Every(BaseModel):
    interval: str
    def to_aws_expression(self) -> str:    # was as_rate_expression
        ...
    def to_gcp_expression(self) -> str:    # was as_cron_expression
        ...
```

The AWS builder collapses to:

```python
schedule = _load_schedule(cfg)
schedule_expr = schedule.to_aws_expression()
```

…and the GCP builder, symmetrically, calls `to_gcp_expression()`.

### 4.4 Type hints

- `Schedule = Cron | Every` already exists in [schedule.py:169](../skaal/schedule.py#L169).
- `TypeAdapter[Schedule]` for validation; Pydantic infers the discriminator
  from the field shape (`expression` vs `interval`). If we want it explicit,
  add a `kind: Literal["cron","every"]` field to both models and use
  `pydantic.Discriminator("kind")`.
- Do **not** define a `Protocol` for `to_aws_expression()` etc. — `Schedule`
  is a closed `Cron | Every` union, so a union of concrete types gives mypy
  exhaustiveness checking for free.

### 4.5 Risks

- **Backwards compatibility of the plan file**: existing `plan.json` files
  store `trigger` as `{"expression": "..."}` (Cron) or `{"interval": "..."}`
  (Every) — the Pydantic discriminator will accept both shapes already, so
  the change is read-compatible. Verify with a fixture from the testdata.
- **Renaming `as_aws_expression` → `to_aws_expression`** is invasive across
  callers (only two). Keep the old names as one-line aliases for one release
  if external code uses them; otherwise just rename.

### 4.6 Migration

1. Add `to_aws_expression` / `to_gcp_expression` methods on both schedule
   models (delete or alias the older names).
2. Add `_schedule_adapter` + `_load_schedule` in a shared helper —
   [skaal/deploy/builders/_schedule.py](../skaal/deploy/builders/_schedule.py)
   (new file).
3. Replace the trigger blocks in both builders with `_load_schedule(cfg).to_<cloud>_expression()`.
4. Test: assert that an `Every(interval="5m")` round-trips through
   `_schedule_adapter.dump_python` → `validate_python`, and that the resulting
   `to_aws_expression()` matches the current output.

---

## 5. Module export bucket dispatch

### 5.1 Where

[skaal/module.py:598-613](../skaal/module.py)

```python
exp_storage: dict[str, Any] = {}
exp_agents: dict[str, Any] = {}
exp_functions: dict[str, Any] = {}
exp_channels: dict[str, Any] = {}
...
if bucket_name == "storage":   exp_storage[sym_name] = sym
elif bucket_name == "agents":  exp_agents[sym_name] = sym
elif bucket_name == "functions": exp_functions[sym_name] = sym
elif bucket_name == "channels":  exp_channels[sym_name] = sym
```

### 5.2 Plan — single dict-of-dicts

```python
from collections import defaultdict
from typing import Literal

BucketName = Literal["storage", "agents", "functions", "channels"]

exports: dict[BucketName, dict[str, Any]] = defaultdict(dict)
...
exports[bucket_name][sym_name] = sym  # type: ignore[index]   # bucket_name is str at this point
```

If the four locals are read individually elsewhere (verify; likely just
returned at the bottom), unpack at the end:

```python
return {
    "storage": exports["storage"],
    "agents": exports["agents"],
    "functions": exports["functions"],
    "channels": exports["channels"],
}
```

### 5.3 Type hints

- `BucketName = Literal["storage", "agents", "functions", "channels"]` — keep
  near the top of `module.py`; reuse if `registered.items()` is also typed.
- `dict[BucketName, dict[str, Any]]` — typed dispatch is enforced statically.

### 5.4 Risks / migration

Mechanical, ~5 minute change. Existing module export tests cover it.

---

## 6. Local-compose gateway implementation resolution

### 6.1 Where

[skaal/deploy/builders/local_compose.py:133, 187-200](../skaal/deploy/builders/local_compose.py):

```python
implementation = gw_comp.implementation or ("traefik" if gw_comp.kind == "proxy" else "kong")
gateway_service = "traefik" if implementation == "traefik" else "kong"
...
if gateway_service == "traefik":
    app_labels = _traefik_labels(routes, app_name)
```

### 6.2 Why this shape is wrong

- Three branches on the same `implementation`/`gateway_service` flag — the
  knowledge of "what does each gateway need" is fanned out.
- Adding a third gateway (e.g. `nginx`) means editing all three sites.

### 6.3 Plan — gateway adapter Protocol

Define one adapter per gateway in
[skaal/deploy/builders/_gateways.py](../skaal/deploy/builders/_gateways.py):

```python
from typing import Protocol

class GatewayAdapter(Protocol):
    name: str                                    # service name in compose
    compose_service: str                         # key into COMPOSE_SERVICES
    def app_labels(self, routes: list[dict[str, Any]], app_name: str) -> str: ...
    def kong_config(self, routes: list[dict[str, Any]], **opts: Any) -> str | None: ...

class _Traefik:
    name = "traefik"
    compose_service = "traefik"
    def app_labels(self, routes, app_name): return _traefik_labels(routes, app_name)
    def kong_config(self, routes, **opts): return None

class _Kong:
    name = "kong"
    compose_service = "kong"
    def app_labels(self, routes, app_name): return ""
    def kong_config(self, routes, **opts): return _kong_config(routes, **opts)

_ADAPTERS: dict[str, GatewayAdapter] = {"traefik": _Traefik(), "kong": _Kong()}

def adapter_for(gw_comp: Component) -> GatewayAdapter:
    impl = gw_comp.implementation or ("traefik" if gw_comp.kind == "proxy" else "kong")
    return _ADAPTERS[impl]
```

Call sites become:

```python
adapter = adapter_for(gw_comp)
services_needed.setdefault(adapter.compose_service, COMPOSE_SERVICES[adapter.compose_service])
app_labels = adapter.app_labels(routes, app_name)
```

### 6.4 Type hints

- `GatewayAdapter(Protocol)` — structural; concrete adapters can be plain
  classes, no inheritance.
- `_ADAPTERS: dict[str, GatewayAdapter]` — invariant; lookups via the
  resolution function.

### 6.5 Risks

Low. Behavior is preserved per-call. The default-resolution logic
(`"traefik" if kind == "proxy" else "kong"`) lives in **one** place
(`adapter_for`) instead of two.

---

## 7. RateLimiter scope dispatch

### 7.1 Where

[skaal/runtime/middleware.py:142-151](../skaal/runtime/middleware.py),
`_RateLimiter._key`:

```python
if scope == "global":     return "__global__"
if scope == "per-client": return str(kwargs.get("client_id") or kwargs.get("client") or "__anon__")
if scope.startswith("per-key:"):
    arg = scope.split(":", 1)[1]
    return str(kwargs.get(arg, "__missing__"))
return "__global__"
```

### 7.2 Plan — small dict + parsed prefix

The `per-key:<arg>` form is parametric, so a flat dict doesn't fit. Replace
with a small dispatch table for the fixed cases plus the existing parametric
branch:

```python
_KEYERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "global":     lambda _kw: "__global__",
    "per-client": lambda kw: str(kw.get("client_id") or kw.get("client") or "__anon__"),
}

def _key(self, kwargs: dict[str, Any]) -> str:
    scope = self.policy.scope
    keyer = _KEYERS.get(scope)
    if keyer is not None:
        return keyer(kwargs)
    if scope.startswith("per-key:"):
        return str(kwargs.get(scope.split(":", 1)[1], "__missing__"))
    return "__global__"
```

### 7.3 Type hints

`Callable[[dict[str, Any]], str]` for keyers. `scope` is already a string;
no extra `Literal` because `per-key:*` is open-ended.

### 7.4 Verdict

**Optional.** Three cases is the threshold where a registry starts to pay
off; today it's exactly at the line. Do this only if a fourth scope is
imminent — otherwise leave it.

---

## 8. Outbox delivery branch

[skaal/runtime/engines/outbox.py:90-97](../skaal/runtime/engines/outbox.py) —
`if self.outbox.delivery == "at-least-once": delete(key) else: row["delivered"]
= True; set(key, row)`. Two arms, hot path inside a relay loop. **Not worth
refactoring** — leave it.

---

## Execution order

Recommended sequencing — each step keeps the lib green between commits:

1. **Item 1** (engines) — establishes the `Pattern` protocol and the registry idiom.
2. **Item 2** (solver) — reuses `Pattern` from #1; biggest payoff (new
   patterns plug in via two decorators total).
3. **Item 3** (planning matrix) — independent; swap `_development_storage_binding`.
4. **Item 5** (module buckets) — trivial; bundle with whatever PR touches `module.py` next.
5. **Item 4** (schedule polymorphism) — independent; ship with item 3 if convenient.
6. **Item 6** (gateway adapter) — independent; only if `nginx` or another gateway is on the roadmap.
7. **Items 7-8** — skip unless they get touched for unrelated reasons.

## Acceptance bar — applies to every item

- No behavior change observable in existing tests.
- New code uses `Literal`/Protocols/`TypedDict` so static checkers reject
  unknown tags at the call site, not at runtime.
- A duplicate `register_*` call raises `RuntimeError` (mirrors
  [BackendRegistry.register](../skaal/backends/_registry.py#L18)).
- Each registry is documented at its definition site with one example of how
  to add a new entry.

## Out of scope

- Entry-point / plugin discovery for any of these registries (would parallel
  [skaal/plugins.py](../skaal/plugins.py); deferable until a third-party
  pattern/binding actually exists).
- Renaming `_collect_all` or formalising the `app` argument's type.
- Replacing `is_relational_model` / `is_vector_model` predicate dispatch in
  [_planning.py:74-81](../skaal/runtime/_planning.py#L74-L81) — those are
  feature checks on classes, not tag dispatches; the predicate form is
  appropriate.
