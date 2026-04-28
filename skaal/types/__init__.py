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
    DockerBuildConfig,
    DockerContainerProperties,
    DockerHealthcheck,
    DockerImageProperties,
    DockerLabel,
    DockerNetworkAttachment,
    DockerPortBinding,
    DockerVolumeMount,
    LocalServiceSpec,
    PulumiPlugins,
    PulumiProviderPlugin,
    PulumiResource,
    PulumiResourceOptions,
    PulumiStack,
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
    # deploy
    "DockerBuildConfig",
    "DockerContainerProperties",
    "DockerHealthcheck",
    "DockerImageProperties",
    "DockerLabel",
    "DockerNetworkAttachment",
    "DockerPortBinding",
    "DockerVolumeMount",
    "LocalServiceSpec",
    "PulumiPlugins",
    "PulumiProviderPlugin",
    "PulumiResource",
    "PulumiResourceOptions",
    "PulumiStack",
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
