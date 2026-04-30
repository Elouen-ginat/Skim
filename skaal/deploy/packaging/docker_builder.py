from __future__ import annotations

from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any, TypeAlias

import docker

DockerLogChunk: TypeAlias = dict[str, Any]
DockerProgress: TypeAlias = Callable[[DockerLogChunk], None] | None


def build_image(
    *,
    context_dir: Path,
    tag: str,
    progress: DockerProgress = None,
) -> str:
    client = docker.from_env()
    try:
        image, log_stream = client.images.build(
            path=str(context_dir),
            tag=tag,
            rm=True,
            forcerm=True,
            decode=True,
        )
        for chunk in log_stream:
            if progress is not None:
                progress(chunk)
        return image.id
    finally:
        client.close()


def push_image(
    *,
    repository: str,
    tag: str,
    auth_config: dict[str, str] | None = None,
    progress: DockerProgress = None,
) -> None:
    client = docker.from_env()
    try:
        log_stream: str | Generator[DockerLogChunk, None, None] = client.images.push(
            repository,
            tag=tag,
            auth_config=auth_config,
            stream=progress is not None,
            decode=progress is not None,
        )
        if progress is None:
            return
        assert not isinstance(log_stream, str)
        for chunk in log_stream:
            progress(chunk)
    finally:
        client.close()


def login_registry(
    *,
    registry: str,
    username: str,
    password: str,
) -> None:
    client = docker.from_env()
    try:
        client.login(username=username, password=password, registry=registry)
    finally:
        client.close()


def find_network_id(name: str) -> str | None:
    client = docker.from_env()
    try:
        networks = client.networks.list(names=[name])
        if not networks:
            return None
        return networks[0].id
    finally:
        client.close()


def find_volume_name(name: str) -> str | None:
    client = docker.from_env()
    try:
        volumes = client.volumes.list(filters={"name": name})
        if not volumes:
            return None
        return volumes[0].name
    finally:
        client.close()
