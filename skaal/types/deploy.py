"""Typed deploy resource shapes shared by Pulumi-based generators."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, NamedTuple, Protocol, Required, TypeAlias, TypedDict

TargetName: TypeAlias = Literal[
    "aws",
    "aws-lambda",
    "gcp",
    "gcp-cloudrun",
    "local",
    "local-docker",
]

ConfigOverrides: TypeAlias = dict[str, str]
StackOutputs: TypeAlias = dict[str, str]


class StackProfile(TypedDict, total=False):
    env: dict[str, str]
    invokers: list[str]
    labels: dict[str, str]
    enable_mesh: bool


class DeployMeta(TypedDict, total=False):
    target: Required[TargetName]
    source_module: Required[str]
    app_name: Required[str]
    lambda_architecture: str
    lambda_runtime: str


class RouteSpec(TypedDict, total=False):
    path: str
    target: str
    methods: list[str]


class AuthConfig(TypedDict, total=False):
    provider: Literal["jwt"]
    issuer: str
    audience: str
    header: str
    required: bool


class RateLimitConfig(TypedDict, total=False):
    requests_per_second: float
    burst: int


class GatewayConfig(TypedDict, total=False):
    routes: list[RouteSpec]
    auth: AuthConfig
    rate_limit: RateLimitConfig
    cors_origins: list[str]


class AppLike(Protocol):
    name: str
    _mounts: dict[str, str]
    _wsgi_attribute: str


class BackendWiring(NamedTuple):
    imports: str
    overrides: str


class DockerBuildConfig(TypedDict, total=False):
    context: str
    dockerfile: str
    platform: str


class DockerHealthcheck(TypedDict, total=False):
    interval: str
    retries: int
    startPeriod: str
    tests: list[str]
    timeout: str


class DockerLabel(TypedDict):
    label: str
    value: str


class DockerNetworkAttachment(TypedDict, total=False):
    aliases: list[str]
    name: str


class DockerPortBinding(TypedDict, total=False):
    external: int
    internal: int
    ip: str
    protocol: str


class DockerVolumeMount(TypedDict, total=False):
    containerPath: str
    hostPath: str
    readOnly: bool
    volumeName: str


class DockerImageProperties(TypedDict, total=False):
    build: DockerBuildConfig
    imageName: str
    skipPush: bool


class DockerContainerProperties(TypedDict, total=False):
    command: list[str]
    envs: list[str]
    healthcheck: DockerHealthcheck
    image: str
    labels: list[DockerLabel]
    name: str
    networkMode: str
    networksAdvanced: list[DockerNetworkAttachment]
    ports: list[DockerPortBinding]
    restart: str
    volumes: list[DockerVolumeMount]
    wait: bool
    waitTimeout: int
    workingDir: str


class LocalServiceSpec(TypedDict, total=False):
    command: list[str]
    envs: list[str]
    healthcheck: DockerHealthcheck
    image: str
    labels: list[DockerLabel]
    ports: list[DockerPortBinding]
    volumes: list[DockerVolumeMount]


class PulumiProviderPlugin(TypedDict):
    name: str
    version: str


class PulumiPlugins(TypedDict):
    providers: Required[list[PulumiProviderPlugin]]


class PulumiResourceOptions(TypedDict, total=False):
    dependsOn: list[str]


class PulumiResource(TypedDict, total=False):
    properties: Required[Mapping[str, Any]]
    type: Required[str]
    options: PulumiResourceOptions


class PulumiStack(TypedDict, total=False):
    config: Required[dict[str, Any]]
    name: Required[str]
    outputs: Required[dict[str, Any]]
    plugins: PulumiPlugins
    resources: Required[dict[str, PulumiResource]]
    runtime: Required[str]
    variables: dict[str, Any]
