"""Agent base class — virtual actors with persistent identity."""

from __future__ import annotations

import weakref
from typing import Any, ClassVar, get_type_hints


class AgentMeta(type):
    """Metaclass that registers agent types and their handlers."""

    # Use WeakValueDictionary so garbage-collected agent classes are automatically removed.
    # This prevents unbounded growth across test runs and avoids stale class references.
    _registry: ClassVar[weakref.WeakValueDictionary[str, type]] = weakref.WeakValueDictionary()

    def __new__(mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any]) -> type:
        cls = super().__new__(mcs, name, bases, namespace)
        if name != "Agent":
            AgentMeta._registry[name] = cls
        return cls


class Agent(metaclass=AgentMeta):
    """
    Base class for Skaal virtual actors.

    Agents have a persistent identity key and single-threaded execution per identity.
    Fields marked @persistent survive restarts.

    Usage::

        @app.agent(persistent=True)
        class Customer(Agent):
            score: float = 0.0

            @handler
            async def add_score(self, points: float) -> None:
                self.score += points
    """

    __skaal_agent__: ClassVar[dict[str, Any]] = {}
    __skaal_persistent_fields__: ClassVar[frozenset[str]] = frozenset()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Collect fields explicitly marked as persistent via Persistent[T] type annotation
        from skaal.types import Persistent

        # Use get_type_hints to evaluate string annotations (from __future__ import annotations)
        try:
            type_hints = get_type_hints(cls)
        except Exception:
            # If type hints can't be evaluated, fall back to empty
            type_hints = {}

        cls.__skaal_persistent_fields__ = frozenset(
            name
            for name, annotation in type_hints.items()
            if
            (
                # Check if annotation is Persistent[T] or directly is Persistent
                getattr(annotation, "__origin__", None) is Persistent or annotation is Persistent
            )
        )

    @classmethod
    def all_ids(cls) -> list[str]:
        """Return all active agent IDs of this type. Stub — requires runtime mesh."""
        raise NotImplementedError("Agent.all_ids() requires the Skaal runtime mesh.")

    @classmethod
    def query(cls, predicate: Any) -> list["Agent"]:
        """Query agents by predicate. Stub — requires runtime mesh."""
        raise NotImplementedError("Agent.query() requires the Skaal runtime mesh.")


def agent(*, persistent: bool = True) -> Any:
    """Class decorator that registers a class as a Skaal agent."""

    def decorator(cls: type) -> type:
        if not issubclass(cls, Agent):
            # Dynamically make it inherit from Agent
            cls = AgentMeta(cls.__name__, (Agent,), dict(cls.__dict__))
        cls.__skaal_agent__ = {"persistent": persistent}  # type: ignore[attr-defined]
        return cls

    return decorator
