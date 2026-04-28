# Pattern → Engine dispatch — implementation plan

Status: Proposed
Scope: [skaal/patterns.py](../skaal/patterns.py),
[skaal/runtime/engines/](../skaal/runtime/engines/) (`base.py`, `eventlog.py`,
`outbox.py`, `projection.py`, `saga.py`, `__init__.py`).

Replace the `isinstance` ladder in
[start_engines_for](../skaal/runtime/engines/base.py) with a typed registry so
each engine self-binds to its pattern class. New patterns plug in by adding one
file and one decorator — no central edit.

---

## 1. Problem

[skaal/runtime/engines/base.py](../skaal/runtime/engines/base.py) currently
dispatches like this:

```python
for obj in app._collect_all().values():
    if isinstance(obj, EventLog):
        engines.append(EventLogEngine(obj))
    elif isinstance(obj, Projection):
        engines.append(ProjectionEngine(obj))
    elif isinstance(obj, Saga):
        engines.append(SagaEngine(obj))
    elif isinstance(obj, Outbox):
        engines.append(OutboxEngine(obj))
```

Issues:

- `base.py` knows about every engine class — a fan-in coupling that grows
  linearly with each new pattern.
- Adding a pattern requires editing `base.py` *and* importing the new engine
  there, which risks circular imports as the engine modules grow.
- The isinstance order matters silently (subclass-before-parent), and there is
  no compile-time guarantee that an engine accepts the pattern it claims to
  handle.

## 2. Goal

- O(1) lookup by `type(obj)` with a polymorphic fallback.
- Each engine module declares its own pattern binding; `base.py` stops
  importing engine classes.
- Static type checking enforces that the registered factory accepts the
  registered pattern.
- Public surface (`PatternEngine`, `start_engines_for`) is unchanged for
  callers in [_lifecycle.py](../skaal/runtime/_lifecycle.py) and
  [_core.py](../skaal/runtime/_core.py).

## 3. Design — registry decorator

Add a `Pattern` marker protocol to `patterns.py`, a typed registry +
`register_engine` decorator to `base.py`, and one decorator line to each engine
class.

### 3.1 `Pattern` protocol — `skaal/patterns.py`

```python
from typing import ClassVar, Protocol, runtime_checkable

@runtime_checkable
class Pattern(Protocol):
    """Marker every Skaal pattern (EventLog/Projection/Saga/Outbox) satisfies.

    Patterns expose ``__skaal_pattern__`` metadata consumed by the solver;
    this protocol formalises the shared shape so engine dispatch can take
    ``Pattern`` instead of ``Any``.
    """

    __skaal_pattern__: dict[str, object]
```

No behavior change — every existing pattern class already sets
`self.__skaal_pattern__` in its `__init__`. Patterns do not need to inherit
from `Pattern` because `@runtime_checkable` Protocols accept structural
matches, but we *may* attach it as a base class to make `isinstance(obj,
Pattern)` cheap if profiling shows the protocol check is hot.

### 3.2 Registry + decorator — `skaal/runtime/engines/base.py`

Full replacement file:

```python
"""Pattern-engine base protocol and the runtime-side starter helper."""

from __future__ import annotations

from typing import Any, Callable, Protocol, TypeVar, runtime_checkable

from skaal.patterns import Pattern

P = TypeVar("P", bound=Pattern)


@runtime_checkable
class PatternEngine(Protocol):
    """Minimum lifecycle every engine exposes."""

    async def start(self, context: Any) -> None: ...
    async def stop(self) -> None: ...


EngineFactory = Callable[[P], PatternEngine]
"""Anything callable as ``factory(pattern) -> PatternEngine`` — typically the
engine class itself, since ``EngineCls(pattern_obj)`` matches."""


_REGISTRY: dict[type[Pattern], EngineFactory[Any]] = {}


def register_engine(
    pattern_cls: type[P],
) -> Callable[[EngineFactory[P]], EngineFactory[P]]:
    """Decorator: bind an engine factory to a pattern class.

    Usage::

        @register_engine(EventLog)
        class EventLogEngine:
            def __init__(self, log: EventLog[Any]) -> None: ...

    The returned decorator preserves the engine's own type so static checks
    on call sites still see ``EventLogEngine``.
    """

    def _decorate(factory: EngineFactory[P]) -> EngineFactory[P]:
        if pattern_cls in _REGISTRY:
            raise RuntimeError(
                f"engine factory already registered for {pattern_cls.__name__}"
            )
        _REGISTRY[pattern_cls] = factory  # type: ignore[assignment]
        return factory

    return _decorate


def engine_for(obj: Pattern) -> PatternEngine | None:
    """Return a not-yet-started engine for *obj*, or ``None`` if no factory
    is registered for its type (or any of its bases)."""
    factory = _REGISTRY.get(type(obj))
    if factory is not None:
        return factory(obj)
    for cls, fac in _REGISTRY.items():
        if isinstance(obj, cls):
            return fac(obj)
    return None


async def start_engines_for(app: Any, context: Any) -> list[PatternEngine]:
    """Inspect *app*'s collected resources, build the right engine for each
    registered pattern, start them, and return the list so the caller can
    stop them on shutdown.

    *context* is forwarded to every engine's :meth:`start` call.
    """
    # Force decorators to run by importing each engine module exactly once.
    from skaal.runtime.engines import (  # noqa: F401
        eventlog,
        outbox,
        projection,
        saga,
    )

    engines: list[PatternEngine] = [
        eng
        for obj in app._collect_all().values()
        if (eng := engine_for(obj)) is not None
    ]
    for eng in engines:
        await eng.start(context)
    return engines
```

### 3.3 Engine self-registration

Each engine adds one decorator. No other changes to engine internals.

[skaal/runtime/engines/eventlog.py](../skaal/runtime/engines/eventlog.py):

```python
from skaal.patterns import EventLog
from skaal.runtime.engines.base import register_engine

@register_engine(EventLog)
class EventLogEngine:
    def __init__(self, log: EventLog[Any]) -> None: ...
```

[skaal/runtime/engines/projection.py](../skaal/runtime/engines/projection.py):

```python
@register_engine(Projection)
class ProjectionEngine:
    def __init__(self, projection: Projection[Any, Any]) -> None: ...
```

[skaal/runtime/engines/saga.py](../skaal/runtime/engines/saga.py):

```python
@register_engine(Saga)
class SagaEngine:
    def __init__(self, saga: Saga) -> None: ...
```

[skaal/runtime/engines/outbox.py](../skaal/runtime/engines/outbox.py):

```python
@register_engine(Outbox)
class OutboxEngine:
    def __init__(self, outbox: Outbox[Any], poll_interval: float = 0.05) -> None: ...
```

### 3.4 `__init__.py` exports

[skaal/runtime/engines/__init__.py](../skaal/runtime/engines/__init__.py)
gains `register_engine` and `engine_for` so plugin authors outside the package
can register patterns:

```python
from skaal.runtime.engines.base import (
    PatternEngine,
    engine_for,
    register_engine,
    start_engines_for,
)

__all__ = [
    "EventLogEngine",
    "OutboxEngine",
    "PatternEngine",
    "ProjectionEngine",
    "SagaEngine",
    "SagaExecutor",
    "engine_for",
    "register_engine",
    "start_engines_for",
]
```

## 4. Type-hint checklist

| Symbol | Hint | Why |
|---|---|---|
| `P` | `TypeVar("P", bound=Pattern)` | Restricts the registry to pattern classes; preserves the relationship between decorator argument and factory parameter. |
| `EngineFactory[P]` | `Callable[[P], PatternEngine]` | Lets the decorator demand `factory(p: P) -> PatternEngine`. An engine class qualifies because `EngineCls.__init__(self, p: P)` makes `EngineCls` callable as `Callable[[P], EngineCls]`, and `EngineCls` is structurally a `PatternEngine`. |
| `_REGISTRY` | `dict[type[Pattern], EngineFactory[Any]]` | Storage erases `P`; the decorator is the typed entry point. The `# type: ignore[assignment]` inside `_decorate` documents that erasure. |
| `register_engine(pattern_cls: type[P])` | `Callable[[EngineFactory[P]], EngineFactory[P]]` | Identity decorator preserves the engine's own type at the call site (so `EventLogEngine(some_log)` still type-checks as `EventLogEngine`). |
| `engine_for(obj: Pattern) -> PatternEngine \| None` | as written | Public, typed lookup helper for tests/plugins. |
| `start_engines_for(app: Any, context: Any) -> list[PatternEngine]` | unchanged | Public surface preserved; existing call sites keep working. |
| Engine `__init__` params | `EventLog[Any]`, `Projection[Any, Any]`, `Saga`, `Outbox[Any]` | Concrete pattern types make `register_engine(Pattern)` match `EngineFactory[Pattern]` at the decorator site. |

## 5. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Decorator never fires because module not imported. | `start_engines_for` explicitly imports the four built-in engine modules before reading `_REGISTRY`. |
| Plugin pattern in user code never gets dispatched. | Document that third-party patterns must import their engine module at app-startup time (e.g. via `pyproject` entry points or a side-effecting import in the user's `app.py`). |
| Duplicate registration silently overwrites. | `register_engine` raises `RuntimeError` on re-registration. |
| Subclass of a registered pattern matches wrong factory. | Exact-`type` lookup runs first; the `isinstance` fallback hits the first match in insertion order. If we add a pattern hierarchy, swap to `for cls in type(obj).__mro__`. |
| `Pattern` protocol drifts from real attribute layout. | `__skaal_pattern__` is set by every pattern's `__init__`; covered by existing tests that read `pattern_type` from it. |

## 6. Migration steps

1. Add `Pattern` protocol to [skaal/patterns.py](../skaal/patterns.py).
2. Replace [skaal/runtime/engines/base.py](../skaal/runtime/engines/base.py)
   with the §3.2 implementation.
3. Add `@register_engine(...)` to each of `EventLogEngine`, `ProjectionEngine`,
   `SagaEngine`, `OutboxEngine`.
4. Extend [engines/__init__.py](../skaal/runtime/engines/__init__.py) exports.
5. Run the runtime test suite — `start_engines_for` is exercised by
   [_lifecycle.py](../skaal/runtime/_lifecycle.py); existing pattern tests
   already construct each engine and validate lifecycle.
6. (Optional) Add a unit test that asserts `register_engine` rejects a
   duplicate registration.

## 7. Out of scope

- Pattern discovery via entry points (would belong in
  [skaal/plugins.py](../skaal/plugins.py), parallel to the backends/channels
  groups).
- Renaming `_collect_all` or formalising the `app` argument's type — handled
  separately if the runtime audit calls for it.
- Engine ordering (today engines start in iteration order; if start-order
  becomes load-bearing we add a `priority` arg to `register_engine`).
