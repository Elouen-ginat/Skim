"""
Skaal — Infrastructure as Constraints.

Write it once. Scale it with a word.
"""

from skaal.app import App
from skaal.module import Module, ModuleExport
from skaal.storage import Collection, Map
from skaal.agent import Agent, agent
from skaal.channel import Channel
from skaal.decorators import (
    compute,
    deploy,
    handler,
    scale,
    shared,
    storage,
)
from skaal.patterns import EventLog, Outbox, Projection, Saga, SagaStep
from skaal.components import (
    APIGateway,
    AuthConfig,
    ExternalObservability,
    ExternalQueue,
    ExternalStorage,
    Proxy,
    Route,
)
from skaal.types import (
    Bulkhead,
    CircuitBreaker,
    RateLimitPolicy,
    RetryPolicy,
)
from skaal import types

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
    "deploy",
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
