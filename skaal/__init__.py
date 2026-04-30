"""
Skaal — Infrastructure as Constraints.

Write it once. Scale it with a word.
"""

from skaal import api, types
from skaal._logging import ensure_null_handler as _ensure_null_handler
from skaal.agent import Agent, agent
from skaal.app import App
from skaal.blob import BlobStore
from skaal.channel import Channel
from skaal.components import (
    APIGateway,
    AppRef,
    AuthConfig,
    ExternalObservability,
    ExternalQueue,
    ExternalStorage,
    Proxy,
    Route,
    ScheduleTrigger,
)
from skaal.decorators import (
    blob,
    compute,
    handler,
    relational,
    scale,
    shared,
    storage,
    vector,
)
from skaal.module import Module, ModuleExport
from skaal.patterns import EventLog, Outbox, Projection, Saga, SagaStep
from skaal.relational import ensure_schema as ensure_relational_schema
from skaal.relational import open_session as open_relational_session
from skaal.schedule import Cron, Every, Schedule, ScheduleContext
from skaal.storage import Store
from skaal.sync import run as sync_run
from skaal.types import (
    BeforeInvoke,
    BlobObject,
    Bulkhead,
    CircuitBreaker,
    EngineTelemetrySnapshot,
    InvokeContext,
    Page,
    RateLimitPolicy,
    ReadinessState,
    RetryPolicy,
    SecondaryIndex,
    TelemetryConfig,
)
from skaal.vector import VectorStore

_ensure_null_handler()

__all__ = [
    # Python API namespace (run/plan/build/deploy/...)
    "api",
    # Core
    "App",
    "Module",
    "ModuleExport",
    "Store",
    "BlobStore",
    "VectorStore",
    "Agent",
    "Channel",
    "sync_run",
    # Decorators
    "blob",
    "agent",
    "compute",
    "handler",
    "relational",
    "scale",
    "shared",
    "storage",
    "vector",
    "open_relational_session",
    "ensure_relational_schema",
    # Patterns
    "EventLog",
    "Outbox",
    "Projection",
    "Saga",
    "SagaStep",
    # Components
    "APIGateway",
    "AppRef",
    "AuthConfig",
    "ExternalObservability",
    "ExternalQueue",
    "ExternalStorage",
    "Proxy",
    "Route",
    "ScheduleTrigger",
    # Schedule
    "Cron",
    "Every",
    "Schedule",
    "ScheduleContext",
    "BlobObject",
    "BeforeInvoke",
    "Page",
    # Resilience types
    "Bulkhead",
    "CircuitBreaker",
    "EngineTelemetrySnapshot",
    "InvokeContext",
    "RateLimitPolicy",
    "ReadinessState",
    "RetryPolicy",
    "SecondaryIndex",
    "TelemetryConfig",
    # Type namespace
    "types",
]

__version__ = "0.1.0"
