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
from skaal.types.deploy import (
    AppLike,
    AuthConfig,
    BackendWiring,
    ConfigOverrides,
    DeployMeta,
    DockerBuildConfig,
    DockerContainerProperties,
    DockerHealthcheck,
    DockerImageProperties,
    DockerLabel,
    DockerNetworkAttachment,
    DockerPortBinding,
    DockerVolumeMount,
    GatewayConfig,
    LocalServiceSpec,
    PulumiPlugins,
    PulumiProviderPlugin,
    PulumiResource,
    PulumiResourceOptions,
    PulumiStack,
    RateLimitConfig,
    RouteSpec,
    StackOutputs,
    StackProfile,
    TargetName,
)
from skaal.types.invoke import BeforeInvoke, InvokeContext, StreamFn
from skaal.types.schema import apply_migrations, migrate_from
from skaal.types.storage import Page, SecondaryIndex

__all__ = [
    # constraints
    "AccessPattern",
    "Consistency",
    "DecommissionPolicy",
    "Durability",
    "Latency",
    "Persistent",
    "Throughput",
    # deploy
    "AppLike",
    "AuthConfig",
    "BackendWiring",
    "ConfigOverrides",
    "DeployMeta",
    "DockerBuildConfig",
    "DockerContainerProperties",
    "DockerHealthcheck",
    "DockerImageProperties",
    "DockerLabel",
    "DockerNetworkAttachment",
    "DockerPortBinding",
    "DockerVolumeMount",
    "GatewayConfig",
    "LocalServiceSpec",
    "PulumiPlugins",
    "PulumiProviderPlugin",
    "PulumiResource",
    "PulumiResourceOptions",
    "PulumiStack",
    "RateLimitConfig",
    "RouteSpec",
    "StackOutputs",
    "StackProfile",
    "TargetName",
    # invoke
    "BeforeInvoke",
    "InvokeContext",
    "StreamFn",
    # storage
    "Page",
    "SecondaryIndex",
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
]
