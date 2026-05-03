"""Agent base class — virtual actors with persistent identity."""

from __future__ import annotations

import weakref
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, ClassVar, get_args, get_origin, get_type_hints


def _is_class_var(annotation: Any) -> bool:
    return get_origin(annotation) is ClassVar


def _is_persistent_annotation(annotation: Any) -> bool:
    from skaal.types import Persistent

    origin = get_origin(annotation)
    if origin is Persistent or annotation is Persistent:
        return True
    if origin is None:
        return False
    if (
        str(origin) != "<class 'typing.Annotated'>"
        and getattr(origin, "__qualname__", "") != "Annotated"
    ):
        return False
    return any(meta is Persistent for meta in get_args(annotation)[1:])


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
    __skaal_state_defaults__: ClassVar[dict[str, Any]] = {}
    __skaal_state_fields__: ClassVar[frozenset[str]] = frozenset()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        try:
            type_hints = get_type_hints(cls, include_extras=True)
        except TypeError:
            type_hints = get_type_hints(cls)
        except Exception:
            type_hints = {}

        state_fields: set[str] = set()
        persistent_fields: set[str] = set()
        state_defaults: dict[str, Any] = {}
        for name, annotation in type_hints.items():
            if _is_class_var(annotation) or name.startswith("__skaal_"):
                continue
            state_fields.add(name)
            if hasattr(cls, name):
                state_defaults[name] = getattr(cls, name)
            if _is_persistent_annotation(annotation):
                persistent_fields.add(name)

        cls.__skaal_state_fields__ = frozenset(state_fields)
        cls.__skaal_state_defaults__ = state_defaults
        cls.__skaal_persistent_fields__ = frozenset(persistent_fields)

    def _load_state(self, state: Mapping[str, Any] | None) -> None:
        for field_name in self.__class__.__skaal_state_fields__:
            if field_name in self.__class__.__skaal_state_defaults__:
                setattr(
                    self,
                    field_name,
                    deepcopy(self.__class__.__skaal_state_defaults__[field_name]),
                )
            elif field_name in self.__dict__:
                del self.__dict__[field_name]

        if state is None:
            return

        for field_name in self.__class__.__skaal_persistent_fields__:
            if field_name in state:
                setattr(self, field_name, deepcopy(state[field_name]))

    def _serialize_state(self) -> dict[str, Any]:
        return {
            field_name: deepcopy(getattr(self, field_name))
            for field_name in self.__class__.__skaal_persistent_fields__
            if hasattr(self, field_name)
        }


def agent(*, persistent: bool = True) -> Any:
    """Class decorator that registers a class as a Skaal agent."""

    def decorator(cls: type) -> type:
        if not issubclass(cls, Agent):
            # Dynamically make it inherit from Agent
            cls = AgentMeta(cls.__name__, (Agent,), dict(cls.__dict__))
        cls.__skaal_agent__ = {"persistent": persistent}  # type: ignore[attr-defined]
        return cls

    return decorator
