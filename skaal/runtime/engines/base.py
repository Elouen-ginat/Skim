"""Pattern-engine base protocol and the runtime-side starter helper."""

from __future__ import annotations

from typing import Any, Callable, Protocol, TypeVar, cast, runtime_checkable

from skaal.patterns import Pattern

P = TypeVar("P", bound=Pattern)


@runtime_checkable
class PatternEngine(Protocol):
    """Minimum lifecycle every engine exposes."""

    async def start(self, context: Any) -> None: ...
    async def stop(self) -> None: ...


EngineFactory = Callable[[P], PatternEngine]


_REGISTRY: dict[type[Pattern], EngineFactory[Any]] = {}


def register_engine(
    pattern_cls: type[P],
) -> Callable[[EngineFactory[P]], EngineFactory[P]]:
    """Bind an engine factory to the pattern class it handles."""

    def _decorate(factory: EngineFactory[P]) -> EngineFactory[P]:
        if pattern_cls in _REGISTRY:
            raise RuntimeError(f"engine factory already registered for {pattern_cls.__name__}")
        _REGISTRY[pattern_cls] = cast(EngineFactory[Any], factory)
        return factory

    return _decorate


def engine_for(obj: Pattern) -> PatternEngine | None:
    """Return a not-yet-started engine for *obj*, or ``None`` if unregistered."""

    factory = _REGISTRY.get(type(obj))
    if factory is not None:
        return factory(obj)

    for cls in type(obj).__mro__[1:]:
        factory = _REGISTRY.get(cast(type[Pattern], cls))
        if factory is not None:
            return factory(obj)

    return None


async def start_engines_for(app: Any, context: Any) -> list[PatternEngine]:
    """Inspect *app*'s collected resources and start the right engine for each
    registered pattern.  Returns the list of started engines so the caller can
    stop them on shutdown.

    *context* is passed through to every engine's :meth:`start` call — the
    runtime uses it to expose the function registry, storage overrides, etc.
    """
    from skaal.runtime.engines import eventlog, outbox, projection, saga  # noqa: F401

    engines: list[PatternEngine] = [
        eng
        for obj in app._collect_all().values()
        if isinstance(obj, Pattern) and (eng := engine_for(obj)) is not None
    ]

    for eng in engines:
        await eng.start(context)
    return engines
