"""Typed deploy resource shapes shared by Pulumi-based generators."""

from __future__ import annotations

from typing import Any, TypedDict


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
    providers: list[PulumiProviderPlugin]


class PulumiResourceOptions(TypedDict, total=False):
    dependsOn: list[str]


class PulumiResource(TypedDict, total=False):
    options: PulumiResourceOptions
    properties: dict[str, Any]
    type: str


class PulumiStack(TypedDict, total=False):
    config: dict[str, Any]
    name: str
    outputs: dict[str, Any]
    plugins: PulumiPlugins
    resources: dict[str, PulumiResource]
    runtime: str
