"""Agent base class — virtual actors with persistent identity."""

from __future__ import annotations

from typing import Any, ClassVar


class AgentMeta(type):
    """Metaclass that registers agent types and their handlers."""

    _registry: ClassVar[dict[str, type]] = {}

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

    __skim_agent__: ClassVar[dict[str, Any]] = {}
    __skim_persistent_fields__: ClassVar[frozenset[str]] = frozenset()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Collect fields annotated as persistent via class variable __persistent__
        cls.__skim_persistent_fields__ = frozenset(
            name for name, annotation in cls.__annotations__.items() if not name.startswith("_")
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
        cls.__skim_agent__ = {"persistent": persistent}  # type: ignore[attr-defined]
        return cls

    return decorator
