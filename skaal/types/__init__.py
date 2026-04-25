"""skaal.types — all constraint and schema types.

Re-exports everything so existing code using ``from skaal.types import X``
continues to work without change.
"""

from skaal.types.compute import (
    Bulkhead,
    CircuitBreaker,
    Compute,
    ComputeType,
    RateLimitPolicy,
    RetryPolicy,
    Scale,
    ScaleStrategy,
)
from skaal.types.constraints import (
    AccessPattern,
    Consistency,
    DecommissionPolicy,
    Durability,
    Latency,
    Persistent,
    Throughput,
)
from skaal.types.runtime import (
    AsyncClosable,
    BackendFactory,
    BackendOverrides,
    ConstructorKwargs,
    DispatchResult,
    MeshClient,
    RuntimeApp,
    RuntimeCallable,
    RuntimeInstance,
    RuntimeInvoker,
    RuntimeKwargs,
    RuntimeMode,
    RuntimePayload,
    RuntimePlanSource,
    RuntimeWireParams,
    StorageClassMap,
    StorageKindName,
    SupportsAsyncSend,
)
from skaal.types.schema import apply_migrations, migrate_from

__all__ = [
    # constraints
    "AccessPattern",
    "Consistency",
    "DecommissionPolicy",
    "Durability",
    "Latency",
    "Persistent",
    "Throughput",
    # compute
    "Bulkhead",
    "CircuitBreaker",
    "Compute",
    "ComputeType",
    "RateLimitPolicy",
    "RetryPolicy",
    "Scale",
    "ScaleStrategy",
    # schema
    "apply_migrations",
    "migrate_from",
    # runtime
    "AsyncClosable",
    "BackendFactory",
    "BackendOverrides",
    "ConstructorKwargs",
    "DispatchResult",
    "MeshClient",
    "RuntimeApp",
    "RuntimeCallable",
    "RuntimeInstance",
    "RuntimeInvoker",
    "RuntimeKwargs",
    "RuntimeMode",
    "RuntimePayload",
    "RuntimePlanSource",
    "RuntimeWireParams",
    "StorageClassMap",
    "StorageKindName",
    "SupportsAsyncSend",
]
