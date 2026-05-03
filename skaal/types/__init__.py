"""skaal.types — all constraint and schema types.

Re-exports everything so existing code using ``from skaal.types import X``
continues to work without change.
"""

from skaal.types.blob import BlobObject
from skaal.types.catalog import CatalogRaw, CatalogSource
from skaal.types.cli import ChangeBatch, ChangeStream, ChildArgv, ReloadDirs, ReloadMode
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
from skaal.types.observability import (
    EngineTelemetrySnapshot,
    HeaderMap,
    ReadinessState,
    TelemetryConfig,
    TelemetryExporter,
)
from skaal.types.relational import (
    RelationalMigrationOp,
    RelationalMigrationPlan,
    RelationalMigrationStatus,
    RelationalMigrationStep,
    RelationalRevision,
)
from skaal.types.schema import apply_migrations, migrate_from
from skaal.types.solver import (
    CandidateReport,
    Diagnosis,
    RelaxSuggestion,
    ResourceKind,
    Violation,
)
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
    # blob
    "BlobObject",
    # catalog (pre-validation layer; see skaal.catalog.models for the typed Catalog)
    "CatalogRaw",
    "CatalogSource",
    # cli
    "ChangeBatch",
    "ChangeStream",
    "ChildArgv",
    "ReloadDirs",
    "ReloadMode",
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
    "TelemetryConfig",
    "TelemetryExporter",
    "EngineTelemetrySnapshot",
    "ReadinessState",
    "HeaderMap",
    # invoke
    "BeforeInvoke",
    "InvokeContext",
    "StreamFn",
    # solver / diagnostics
    "CandidateReport",
    "Diagnosis",
    "RelaxSuggestion",
    "ResourceKind",
    "Violation",
    # storage
    "Page",
    "SecondaryIndex",
    # relational migrations
    "RelationalMigrationOp",
    "RelationalMigrationPlan",
    "RelationalMigrationStatus",
    "RelationalMigrationStep",
    "RelationalRevision",
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
    "AgentsService",
    "AsyncClosable",
    "BackendOverrides",
    "ConstructorKwargs",
    "DispatchResult",
    "MeshClient",
    "RuntimeObserver",
    "RuntimeApp",
    "RuntimeCallable",
    "RuntimeInstance",
    "RuntimeInvoker",
    "RuntimeKwargs",
    "RuntimeMode",
    "RuntimePayload",
    "RuntimePlanSource",
    "RuntimeServices",
    "RuntimeWireParams",
    "StateService",
    "StorageClassMap",
    "StorageKindName",
    "SupportsAsyncSend",
]
