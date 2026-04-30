from __future__ import annotations

import base64
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import pulumi
import pulumi.automation as auto
from pulumi.automation import errors as auto_errors

from skaal.deploy.errors import DeployError
from skaal.deploy.pulumi import DeploymentContext, PulumiRunner, RunnerPlan
from skaal.deploy.pulumi.automation import (
    existing_resource_import_id,
    read_stack_spec,
    workspace_options,
)
from skaal.deploy.pulumi.meta import read_meta
from skaal.types import ConfigOverrides, PulumiResource, PulumiStack, TargetName

_EXPR = re.compile(r"\$\{([^}]+)\}")
_FULL_EXPR = re.compile(r"^\$\{([^}]+)\}$")
_PATH_TOKEN = re.compile(r"([^\[\]]+)|\[(\d+)\]")

_RESOURCE_TYPES: dict[str, str] = {
    "aws:apigatewayv2:Api": "aws:apigatewayv2/api:Api",
    "aws:apigatewayv2:Authorizer": "aws:apigatewayv2/authorizer:Authorizer",
    "aws:apigatewayv2:Integration": "aws:apigatewayv2/integration:Integration",
    "aws:apigatewayv2:Route": "aws:apigatewayv2/route:Route",
    "aws:apigatewayv2:Stage": "aws:apigatewayv2/stage:Stage",
    "aws:dynamodb:Table": "aws:dynamodb/table:Table",
    "aws:ec2:SecurityGroup": "aws:ec2/securityGroup:SecurityGroup",
    "aws:events:Rule": "aws:cloudwatch/eventRule:EventRule",
    "aws:events:Target": "aws:cloudwatch/eventTarget:EventTarget",
    "aws:iam:Policy": "aws:iam/policy:Policy",
    "aws:iam:Role": "aws:iam/role:Role",
    "aws:iam:RolePolicyAttachment": "aws:iam/rolePolicyAttachment:RolePolicyAttachment",
    "aws:lambda:Function": "aws:lambda/function:Function",
    "aws:lambda:Permission": "aws:lambda/permission:Permission",
    "aws:rds:Instance": "aws:rds/instance:Instance",
    "docker:Container": "docker:index/container:Container",
    "docker:Network": "docker:index/network:Network",
    "docker:Volume": "docker:index/volume:Volume",
    "gcp:apigateway:Api": "gcp:apigateway/api:Api",
    "gcp:apigateway:ApiConfig": "gcp:apigateway/apiConfig:ApiConfig",
    "gcp:apigateway:Gateway": "gcp:apigateway/gateway:Gateway",
    "gcp:artifactregistry:Repository": "gcp:artifactregistry/repository:Repository",
    "gcp:cloudrun:IamMember": "gcp:cloudrun/iamMember:IamMember",
    "gcp:cloudrun:Service": "gcp:cloudrun/service:Service",
    "gcp:cloudscheduler:Job": "gcp:cloudscheduler/job:Job",
    "gcp:redis:Instance": "gcp:redis/instance:Instance",
    "gcp:sql:Database": "gcp:sql/database:Database",
    "gcp:sql:DatabaseInstance": "gcp:sql/databaseInstance:DatabaseInstance",
    "gcp:vpcaccess:Connector": "gcp:vpcaccess/connector:Connector",
    "random:index:RandomPassword": "random:index/randomPassword:RandomPassword",
}

_INVOKE_TOKENS: dict[str, str] = {
    "aws:ec2:getVpc": "aws:ec2/getVpc:getVpc",
    "aws:ec2:getSubnets": "aws:ec2/getSubnets:getSubnets",
}


class ProgressSink(Protocol):
    def pulumi_output(self, line: str) -> None: ...

    def pulumi_event(self, event: Any) -> None: ...


class _NullProgressSink:
    def pulumi_output(self, line: str) -> None:
        del line

    def pulumi_event(self, event: Any) -> None:
        del event


def _command_diagnostics(exc: BaseException) -> str | None:
    parts: list[str] = []
    for attr in ("stderr", "stdout", "message"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(dict.fromkeys(parts)) or None


class _StackProgram:
    def __init__(self, spec: PulumiStack):
        self._spec = spec
        self._config_schema = spec.get("config", {})
        self._variables: dict[str, Any] = {}
        self._resources: dict[str, pulumi.CustomResource] = {}

    def run(self) -> None:
        for variable_name, expression in self._spec.get("variables", {}).items():
            self._variables[variable_name] = self.resolve_value(expression)

        for logical_name, resource in self._spec["resources"].items():
            props = self.resolve_value(dict(resource.get("properties", {})))
            opts = self.resource_options(resource)
            resource_type = self.normalize_resource_type(resource["type"])
            self._resources[logical_name] = pulumi.CustomResource(
                resource_type,
                logical_name,
                props,
                opts,
            )

        for output_name, output_value in self._spec.get("outputs", {}).items():
            pulumi.export(output_name, self.resolve_value(output_value))

    def normalize_resource_type(self, resource_type: str) -> str:
        return _RESOURCE_TYPES.get(resource_type, resource_type)

    def normalize_invoke_token(self, invoke_token: str) -> str:
        return _INVOKE_TOKENS.get(invoke_token, invoke_token)

    def config_value(self, key: str) -> Any:
        spec = self._config_schema.get(key, {})
        config_type = str(spec.get("type", "string"))
        namespace, bare_key = key.split(":", 1) if ":" in key else ("", key)
        config = pulumi.Config(namespace) if namespace else pulumi.Config()

        getter_name = {
            "boolean": "get_bool",
            "integer": "get_int",
            "number": "get_float",
        }.get(config_type, "get")
        require_name = {
            "boolean": "require_bool",
            "integer": "require_int",
            "number": "require_float",
        }.get(config_type, "require")

        value = getattr(config, getter_name)(bare_key)
        if value is not None:
            return value
        if "default" in spec:
            return spec["default"]
        return getattr(config, require_name)(bare_key)

    def resolve_reference(self, expr: str) -> Any:
        if expr == "pulumi.stack":
            return pulumi.get_stack()
        if expr.startswith("env:"):
            return os.environ[expr.removeprefix("env:")]
        if expr in self._config_schema:
            return self.config_value(expr)

        root, _, remainder = expr.partition(".")
        if root in self._variables:
            value: Any = self._variables[root]
        elif root in self._resources:
            value = self._resources[root]
        elif ":" in expr:
            return self.config_value(expr)
        else:
            raise KeyError(f"Unknown stack reference: {expr}")

        if not remainder:
            return value
        for segment in remainder.split("."):
            value = self.apply_segment(value, segment)
        return value

    def apply_segment(self, value: Any, segment: str) -> Any:
        for attr_name, index in _PATH_TOKEN.findall(segment):
            if attr_name:
                if isinstance(value, dict):
                    value = value[attr_name]
                else:
                    value = getattr(value, attr_name)
                continue
            value = value[int(index)]
        return value

    def resolve_string(self, value: str) -> Any:
        full_match = _FULL_EXPR.match(value)
        if full_match:
            return self.resolve_reference(full_match.group(1))

        matches = list(_EXPR.finditer(value))
        if not matches:
            return value

        parts: list[Any] = []
        cursor = 0
        for match in matches:
            if match.start() > cursor:
                parts.append(value[cursor : match.start()])
            parts.append(self.resolve_reference(match.group(1)))
            cursor = match.end()
        if cursor < len(value):
            parts.append(value[cursor:])
        return self.concat(parts)

    def concat(self, parts: list[Any]) -> Any:
        if not any(isinstance(part, pulumi.Output) for part in parts):
            return "".join(str(part) for part in parts)
        return pulumi.Output.all(*[pulumi.Output.from_input(part) for part in parts]).apply(
            lambda resolved: "".join(str(part) for part in resolved)
        )

    def resolve_intrinsic(self, key: str, value: Any) -> Any:
        if key == "fn::fileArchive":
            return pulumi.FileArchive(str(self.resolve_value(value)))
        if key == "fn::invoke":
            invoke_args = self.resolve_value(value.get("arguments", {}))
            result = pulumi.runtime.invoke_output(
                self.normalize_invoke_token(value["function"]),
                invoke_args,
            )
            return_name = value.get("return")
            if return_name:
                return result[return_name]
            return result
        if key == "fn::join":
            separator, items = value
            resolved_items = [self.resolve_value(item) for item in items]
            if not any(isinstance(item, pulumi.Output) for item in resolved_items):
                return str(separator).join(str(item) for item in resolved_items)
            return pulumi.Output.all(
                *[pulumi.Output.from_input(item) for item in resolved_items]
            ).apply(lambda resolved: str(separator).join(str(item) for item in resolved))
        if key == "fn::toBase64":
            resolved = self.resolve_value(value)
            if isinstance(resolved, pulumi.Output):
                return pulumi.Output.from_input(resolved).apply(
                    lambda text: base64.b64encode(str(text).encode("utf-8")).decode("utf-8")
                )
            return base64.b64encode(str(resolved).encode("utf-8")).decode("utf-8")
        if key == "fn::toJSON":
            resolved = self.resolve_value(value)
            if isinstance(resolved, pulumi.Output):
                return pulumi.Output.json_dumps(resolved)
            return json.dumps(resolved)
        raise ValueError(f"Unsupported Pulumi intrinsic: {key}")

    def resolve_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.resolve_string(value)
        if isinstance(value, list):
            return [self.resolve_value(item) for item in value]
        if isinstance(value, dict):
            if len(value) == 1:
                intrinsic, intrinsic_value = next(iter(value.items()))
                if intrinsic.startswith("fn::"):
                    return self.resolve_intrinsic(intrinsic, intrinsic_value)
            return {key: self.resolve_value(item) for key, item in value.items()}
        return value

    def resource_options(self, resource: PulumiResource) -> pulumi.ResourceOptions | None:
        depends_on: list[pulumi.CustomResource] = []
        options = resource.get("options") or {}
        for dependency in options.get("dependsOn", []):
            match = _FULL_EXPR.match(dependency)
            if not match:
                continue
            logical_name = match.group(1).split(".", 1)[0]
            depends_on.append(self._resources[logical_name])

        opts = pulumi.ResourceOptions(depends_on=depends_on) if depends_on else None
        import_id = existing_resource_import_id(
            resource["type"],
            dict(resource.get("properties", {})),
        )
        if not import_id:
            return opts
        import_opts = pulumi.ResourceOptions(import_=import_id)
        if opts is None:
            return import_opts
        return pulumi.ResourceOptions.merge(opts, import_opts)


def _program_for(spec: PulumiStack) -> Callable[[], None]:
    builder = _StackProgram(spec)
    return builder.run


class AutomationRunner(PulumiRunner):
    def __init__(self, *, progress_sink: ProgressSink | None = None):
        self._progress_sink = progress_sink or _NullProgressSink()

    def deploy(self, plan: RunnerPlan) -> ConfigOverrides:
        context = plan.context
        spec = read_stack_spec(context.artifacts_dir)
        stack = self._create_or_select_stack(context, spec)

        config = dict(plan.config)
        if plan.package is not None:
            package_config = plan.package(context) or {}
            config.update(package_config)

        for key, value in config.items():
            stack.set_config(key, auto.ConfigValue(value=str(value)))

        self._up(stack, context.target)
        if plan.post_up is not None and plan.post_up(
            context,
            lambda key: str(stack.outputs()[key].value),
        ):
            self._up(stack, context.target)
        return {key: str(stack.outputs()[key].value) for key in plan.output_keys}

    def destroy(self, artifacts_dir: Path, *, stack: str, yes: bool) -> None:
        del yes
        spec = read_stack_spec(artifacts_dir)
        target = read_meta(artifacts_dir)["target"]
        try:
            stack_ref = auto.select_stack(
                stack_name=stack,
                project_name=spec["name"],
                program=_program_for(spec),
                opts=workspace_options(artifacts_dir, spec),
            )
            stack_ref.destroy(
                on_output=self._progress_sink.pulumi_output,
                on_event=self._progress_sink.pulumi_event,
            )
        except auto_errors.CommandError as exc:
            raise DeployError(
                target=target,
                phase="destroy",
                message=f"Pulumi destroy failed for target {target!r}.",
                diagnostics=_command_diagnostics(exc),
            ) from exc

    def _create_or_select_stack(self, context: DeploymentContext, spec: PulumiStack) -> auto.Stack:
        try:
            return auto.create_or_select_stack(
                stack_name=context.stack,
                project_name=spec["name"],
                program=_program_for(spec),
                opts=workspace_options(context.artifacts_dir, spec),
            )
        except auto_errors.CommandError as exc:
            raise DeployError(
                target=context.target,
                phase="up",
                message=f"Failed to initialize the Pulumi stack for target {context.target!r}.",
                diagnostics=_command_diagnostics(exc),
            ) from exc

    def _up(self, stack: auto.Stack, target: TargetName) -> None:
        attempts = 3 if target == "local" else 1
        last_error: auto_errors.CommandError | None = None
        for attempt in range(attempts):
            try:
                stack.up(
                    on_output=self._progress_sink.pulumi_output,
                    on_event=self._progress_sink.pulumi_event,
                )
                return
            except auto_errors.CommandError as exc:
                if target == "local" and "i/o timeout" in str(exc) and attempt < attempts - 1:
                    last_error = exc
                    time.sleep(5.0)
                    continue
                raise DeployError(
                    target=target,
                    phase="up",
                    message=f"Pulumi up failed for target {target!r}.",
                    diagnostics=_command_diagnostics(exc),
                ) from exc
            except Exception as exc:  # noqa: BLE001
                raise DeployError(
                    target=target,
                    phase="up",
                    message=f"Pulumi up failed for target {target!r}.",
                    diagnostics=str(exc),
                ) from exc
        if last_error is not None:
            raise DeployError(
                target=target,
                phase="up",
                message=f"Pulumi up failed for target {target!r} after retrying.",
                diagnostics=_command_diagnostics(last_error),
            ) from last_error
