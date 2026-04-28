"""Pulumi Automation API runtime for the local Docker target."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pulumi
import pulumi.automation as auto
import pulumi_docker as docker
import typer

from skaal.deploy.push import _build_local_image

LOCAL_STACK_SPEC = "skaal-local-stack.json"
_EXPR = re.compile(r"^\$\{([^}]+)\}$")


def _resource_slug(name: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        slug = "skaal"
    if not slug[0].isalpha():
        slug = f"skaal-{slug}"
    return slug[:max_len].rstrip("-") or "skaal"


def _local_image_name(app_name: str) -> str:
    return f"skaal-{_resource_slug(app_name)}:local"


def write_local_stack_spec(output_dir: Path, stack: dict[str, Any]) -> Path:
    path = output_dir / LOCAL_STACK_SPEC
    path.write_text(json.dumps(stack, indent=2), encoding="utf-8")
    return path


def _read_local_stack_spec(artifacts_dir: Path) -> dict[str, Any]:
    path = artifacts_dir / LOCAL_STACK_SPEC
    if not path.exists():
        raise FileNotFoundError(
            f"{LOCAL_STACK_SPEC} not found in {artifacts_dir}. Re-run `skaal build` to regenerate the local artifacts."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _pulumi_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PULUMI_CONFIG_PASSPHRASE", "")
    return env


def _local_backend_url(state_dir: Path) -> str:
    state_dir = state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    return f"file://{state_dir.as_posix()}"


def _workspace_options(artifacts_dir: Path, spec: dict[str, Any]) -> auto.LocalWorkspaceOptions:
    return auto.LocalWorkspaceOptions(
        work_dir=str(artifacts_dir),
        env_vars=_pulumi_env(),
        project_settings=auto.ProjectSettings(
            name=spec["name"],
            runtime="python",
            backend=auto.ProjectBackend(url=_local_backend_url(artifacts_dir / ".pulumi-state")),
        ),
    )


def _resolve_expr(expr: str, resources: dict[str, Any], config: pulumi.Config) -> Any:
    if expr == "localImageRef":
        return config.require("localImageRef")

    value: Any = resources[expr.split(".")[0]]
    for part in expr.split(".")[1:]:
        value = getattr(value, part)
    return value


def _resolve_value(value: Any, resources: dict[str, Any], config: pulumi.Config) -> Any:
    if isinstance(value, str):
        match = _EXPR.match(value)
        if match:
            return _resolve_expr(match.group(1), resources, config)
        return value
    if isinstance(value, list):
        return [_resolve_value(item, resources, config) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_value(item, resources, config) for key, item in value.items()}
    return value


def _resource_options(
    options: dict[str, Any] | None, resources: dict[str, Any]
) -> pulumi.ResourceOptions | None:
    depends_on = []
    for dependency in (options or {}).get("dependsOn", []):
        match = _EXPR.match(dependency)
        if not match:
            continue
        depends_on.append(resources[match.group(1)])
    if not depends_on:
        return None
    return pulumi.ResourceOptions(depends_on=depends_on)


def _container_kwargs(
    props: dict[str, Any], resources: dict[str, Any], config: pulumi.Config
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "image": _resolve_value(props["image"], resources, config),
        "name": props.get("name"),
        "command": props.get("command"),
        "envs": props.get("envs"),
        "restart": props.get("restart"),
        "wait": props.get("wait"),
        "wait_timeout": props.get("waitTimeout"),
        "working_dir": props.get("workingDir"),
    }

    if props.get("ports"):
        kwargs["ports"] = [docker.ContainerPortArgs(**port) for port in props["ports"]]
    if props.get("labels"):
        kwargs["labels"] = [docker.ContainerLabelArgs(**label) for label in props["labels"]]
    if props.get("networksAdvanced"):
        kwargs["networks_advanced"] = [
            docker.ContainerNetworksAdvancedArgs(
                name=_resolve_value(network["name"], resources, config),
                aliases=network.get("aliases"),
            )
            for network in props["networksAdvanced"]
        ]
    if props.get("volumes"):
        kwargs["volumes"] = [
            docker.ContainerVolumeArgs(
                container_path=volume.get("containerPath"),
                host_path=_resolve_value(volume.get("hostPath"), resources, config),
                read_only=volume.get("readOnly"),
                volume_name=_resolve_value(volume.get("volumeName"), resources, config),
            )
            for volume in props["volumes"]
        ]
    if props.get("healthcheck"):
        health = props["healthcheck"]
        kwargs["healthcheck"] = docker.ContainerHealthcheckArgs(
            tests=health.get("tests"),
            interval=health.get("interval"),
            timeout=health.get("timeout"),
            retries=health.get("retries"),
            start_period=health.get("startPeriod"),
        )

    return {key: value for key, value in kwargs.items() if value is not None}


def _local_program(spec: dict[str, Any]):
    def program() -> None:
        config = pulumi.Config()
        resources: dict[str, Any] = {}

        for logical_name, resource in spec["resources"].items():
            resource_type = resource["type"]
            props = resource.get("properties", {})
            opts = _resource_options(resource.get("options"), resources)

            if resource_type == "docker:Network":
                resources[logical_name] = docker.Network(
                    logical_name,
                    name=props.get("name"),
                    opts=opts,
                )
            elif resource_type == "docker:Volume":
                resources[logical_name] = docker.Volume(
                    logical_name,
                    name=props.get("name"),
                    opts=opts,
                )
            elif resource_type == "docker:Container":
                resources[logical_name] = docker.Container(
                    logical_name,
                    opts=opts,
                    **_container_kwargs(props, resources, config),
                )
            else:
                raise ValueError(f"Unsupported local Automation resource type: {resource_type}")

        for output_name, output_value in spec.get("outputs", {}).items():
            pulumi.export(output_name, _resolve_value(output_value, resources, config))

    return program


def _create_or_select_stack(artifacts_dir: Path, stack: str) -> tuple[auto.Stack, dict[str, Any]]:
    spec = _read_local_stack_spec(artifacts_dir)
    stack_ref = auto.create_or_select_stack(
        stack_name=stack,
        project_name=spec["name"],
        program=_local_program(spec),
        opts=_workspace_options(artifacts_dir, spec),
    )
    return stack_ref, spec


def _select_stack(artifacts_dir: Path, stack: str) -> tuple[auto.Stack, dict[str, Any]]:
    spec = _read_local_stack_spec(artifacts_dir)
    stack_ref = auto.select_stack(
        stack_name=stack,
        project_name=spec["name"],
        program=_local_program(spec),
        opts=_workspace_options(artifacts_dir, spec),
    )
    return stack_ref, spec


def deploy_local_stack(
    artifacts_dir: Path,
    *,
    stack: str,
    yes: bool,
    app_name: str,
    config_overrides: dict[str, str] | None = None,
) -> str:
    del yes
    typer.echo("==> Building local app image ...")
    image_ref = _build_local_image(artifacts_dir, _local_image_name(app_name))

    stack_ref, _ = _create_or_select_stack(artifacts_dir, stack)
    stack_ref.set_config("localImageRef", auto.ConfigValue(value=image_ref))
    for key, value in (config_overrides or {}).items():
        stack_ref.set_config(key, auto.ConfigValue(value=str(value)))

    typer.echo("==> Starting local Docker stack (pulumi up via Automation API) ...")
    stack_ref.up(on_output=typer.echo)
    return str(stack_ref.outputs()["appUrl"].value)


def destroy_local_stack(artifacts_dir: Path, *, stack: str, yes: bool) -> None:
    del yes
    stack_ref, _ = _select_stack(artifacts_dir, stack)
    typer.echo("==> Stopping local Docker stack (pulumi destroy via Automation API) ...")
    stack_ref.destroy(on_output=typer.echo)
