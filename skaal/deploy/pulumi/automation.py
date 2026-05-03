"""Shared Pulumi Automation helpers for deploy runners."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pulumi.automation as auto

from skaal.deploy.packaging.docker_builder import find_network_id, find_volume_name
from skaal.deploy.pulumi.env import local_backend_url, pulumi_env
from skaal.types import PulumiStack

STACK_SPEC = "skaal-stack.json"


def write_stack_spec(output_dir: Path, stack: PulumiStack) -> Path:
    path = output_dir / STACK_SPEC
    path.write_text(json.dumps(stack, indent=2), encoding="utf-8")
    return path


def read_stack_spec(artifacts_dir: Path) -> PulumiStack:
    path = artifacts_dir / STACK_SPEC
    if not path.exists():
        raise FileNotFoundError(
            f"{STACK_SPEC} not found in {artifacts_dir}. Re-run `skaal build` to regenerate the deploy artifacts."
        )
    return cast(PulumiStack, json.loads(path.read_text(encoding="utf-8")))


def workspace_options(artifacts_dir: Path, spec: PulumiStack) -> auto.LocalWorkspaceOptions:
    state_dir = (artifacts_dir / ".pulumi-state").resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    return auto.LocalWorkspaceOptions(
        work_dir=str(artifacts_dir),
        env_vars=pulumi_env(),
        project_settings=auto.ProjectSettings(
            name=spec["name"],
            runtime="python",
            backend=auto.ProjectBackend(url=local_backend_url(state_dir)),
        ),
    )


def existing_resource_import_id(resource_type: str, properties: dict[str, Any]) -> str | None:
    if resource_type == "docker:Network":
        name = properties.get("name")
        return find_network_id(name) if isinstance(name, str) else None
    if resource_type == "docker:Volume":
        name = properties.get("name")
        return find_volume_name(name) if isinstance(name, str) else None
    return None
