"""
Skaal — Infrastructure as Constraints.

Write it once. Scale it with a word.
"""

from skaal import types
from skaal.agent import Agent, agent
from skaal.app import App
from skaal.channel import Channel
from skaal.components import (
    APIGateway,
    AuthConfig,
    ExternalObservability,
    ExternalQueue,
    ExternalStorage,
    Proxy,
    Route,
)
from skaal.decorators import (
    compute,
    handler,
    scale,
    shared,
    storage,
)
from skaal.module import Module, ModuleExport
from skaal.patterns import EventLog, Outbox, Projection, Saga, SagaStep
from skaal.storage import Collection, Map
from skaal.types import (
    Bulkhead,
    CircuitBreaker,
    RateLimitPolicy,
    RetryPolicy,
)

__all__ = [
    # Core
    "App",
    "Module",
    "ModuleExport",
    "Map",
    "Collection",
    "Agent",
    "Channel",
    # Decorators
    "agent",
    "compute",
    "handler",
    "scale",
    "shared",
    "storage",
    # Patterns
    "EventLog",
    "Outbox",
    "Projection",
    "Saga",
    "SagaStep",
    # Components
    "APIGateway",
    "AuthConfig",
    "ExternalObservability",
    "ExternalQueue",
    "ExternalStorage",
    "Proxy",
    "Route",
    # Resilience types
    "Bulkhead",
    "CircuitBreaker",
    "RateLimitPolicy",
    "RetryPolicy",
    # Type namespace
    "types",
]

__version__ = "0.1.0"
