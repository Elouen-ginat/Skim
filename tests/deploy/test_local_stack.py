"""Tests for local Pulumi Docker stack generation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from skaal.components import APIGateway, Proxy, Route
from skaal.deploy.builders.local import build_pulumi_stack
from skaal.deploy.targets.local import generate_artifacts
from skaal.plan import PlanFile, StorageSpec
from skaal.solver.components import encode_component


def _make_app(name: str = "test-app", mounts: dict[str, str] | None = None) -> MagicMock:
    app = MagicMock()
    app.name = name
    if mounts:
        app._mounts = mounts
    else:
        del app._mounts
        app.__dict__.pop("_mounts", None)
    return app


def test_local_stack_emits_core_resources_and_storage_envs(tmp_path: Path):
    plan = PlanFile(
        app_name="test-app",
        storage={
            "example.Cache": StorageSpec(
                variable_name="cache",
                backend="memorystore-redis",
                kind="kv",
            )
        },
        deploy_config={"port": 8123},
    )

    stack = build_pulumi_stack(
        _make_app(),
        plan,
        output_dir=tmp_path / "artifacts",
        source_module="examples.counter",
    )

    assert "plugins" not in stack
    assert stack["config"] == {
        "localImageRef": {"type": "string", "default": "skaal-test-app:local"}
    }
    assert stack["outputs"] == {"appUrl": "http://localhost:8123"}

    resources = stack["resources"]
    assert resources["skaal-net"]["type"] == "docker:Network"
    assert resources["skaal-data"]["type"] == "docker:Volume"
    assert "app-image" not in resources
    assert resources["redis"]["type"] == "docker:Container"

    app_resource = resources["app"]
    assert app_resource["type"] == "docker:Container"
    assert app_resource["properties"]["image"] == "${localImageRef}"
    assert app_resource["properties"]["ports"] == [{"internal": 8000, "external": 8123}]
    if "networksAdvanced" in app_resource["properties"]:
        assert {"name": "${skaal-net.name}", "aliases": ["app"]} in app_resource["properties"][
            "networksAdvanced"
        ]
        assert "SKAAL_REDIS_URL_CACHE=redis://redis:6379" in app_resource["properties"]["envs"]
    else:
        assert app_resource["properties"]["networkMode"] == "${skaal-net.name}"
        assert (
            "SKAAL_REDIS_URL_CACHE=redis://skaal-test-app-redis:6379"
            in app_resource["properties"]["envs"]
        )
    assert "${redis}" in app_resource["options"]["dependsOn"]
    assert "${app-image}" not in app_resource["options"]["dependsOn"]
    assert {"containerPath": "/app/data", "volumeName": "${skaal-data.name}"} in app_resource[
        "properties"
    ]["volumes"]


def test_local_stack_proxy_adds_traefik_and_labels(tmp_path: Path):
    proxy = Proxy("edge", routes=[Route("/api/*", target="fn")])
    spec = encode_component("edge", proxy, {}, target="local")
    plan = PlanFile(app_name="test-app", components={"edge": spec})

    stack = build_pulumi_stack(
        _make_app(),
        plan,
        output_dir=tmp_path / "artifacts",
        source_module="examples.counter",
    )

    resources = stack["resources"]
    assert resources["traefik"]["type"] == "docker:Container"
    labels = resources["app"]["properties"]["labels"]
    assert {"label": "traefik.enable", "value": "true"} in labels
    assert {
        "label": "traefik.http.routers.test-app-r0.rule",
        "value": "PathPrefix(`/api`)",
    } in labels


def test_local_stack_api_gateway_adds_kong_mount(tmp_path: Path):
    gateway = APIGateway("public", routes=[Route("/v1/*", target="fn")])
    spec = encode_component("public", gateway, {}, target="local")
    output_dir = tmp_path / "artifacts"

    stack = build_pulumi_stack(
        _make_app(),
        PlanFile(app_name="test-app", components={"public": spec}),
        output_dir=output_dir,
        source_module="examples.counter",
    )

    kong_resource = stack["resources"]["kong"]
    assert kong_resource["type"] == "docker:Container"
    assert {
        "containerPath": "/kong/config.yml",
        "hostPath": str((output_dir / "kong.yml").resolve()),
        "readOnly": True,
    } in kong_resource["properties"]["volumes"]
    assert "${app}" in kong_resource["options"]["dependsOn"]


def test_generate_artifacts_writes_dockerignore(tmp_path: Path):
    output_dir = tmp_path / "artifacts"
    src_pkg = tmp_path / "examples"
    src_pkg.mkdir()
    (src_pkg / "__init__.py").write_text("", encoding="utf-8")

    app = _make_app()
    plan = PlanFile(app_name="test-app", deploy_config={"port": 8000})

    generated = generate_artifacts(
        app,
        plan,
        output_dir=output_dir,
        source_module="examples.counter",
    )

    dockerignore_path = output_dir / ".dockerignore"
    local_spec_path = output_dir / "skaal-stack.json"
    assert dockerignore_path in generated
    assert local_spec_path in generated
    assert dockerignore_path.read_text(encoding="utf-8") == (
        ".pulumi/\n.pulumi-state/\n__pycache__/\n.pytest_cache/\n"
    )
    assert '"resources"' in local_spec_path.read_text(encoding="utf-8")
    generated_pyproject = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert '"skaal[serve,runtime]"' in generated_pyproject
    assert '"apscheduler>=3.10' not in generated_pyproject


def test_generate_artifacts_includes_declared_module_build_dependencies(tmp_path: Path):
    output_dir = tmp_path / "artifacts"
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.skaal.build.modules.\"examples.07_file_upload_api.app\"]
dependencies = [\"python-multipart>=0.0.5\"]
""".strip(),
        encoding="utf-8",
    )
    app = _make_app(name="file-upload-api")
    plan = PlanFile(app_name="file-upload-api", deploy_config={"port": 8000})

    generate_artifacts(
        app,
        plan,
        output_dir=output_dir,
        source_module="examples.07_file_upload_api.app",
    )

    pyproject = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert '"python-multipart>=0.0.5"' in pyproject


def test_local_stack_omits_provider_waits_for_healthchecks_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    plan = PlanFile(
        app_name="test-app",
        storage={
            "example.Cache": StorageSpec(
                variable_name="cache",
                backend="memorystore-redis",
                kind="kv",
            )
        },
        deploy_config={"port": 8123},
    )

    monkeypatch.setattr("skaal.deploy.builders.local.platform.system", lambda: "Windows")

    stack = build_pulumi_stack(
        _make_app(),
        plan,
        output_dir=tmp_path / "artifacts",
        source_module="examples.counter",
    )

    redis_props = stack["resources"]["redis"]["properties"]
    assert "healthcheck" in redis_props
    assert "wait" not in redis_props
    assert "waitTimeout" not in redis_props
    assert redis_props["networkMode"] == "${skaal-net.name}"
    assert "networksAdvanced" not in redis_props


def test_local_stack_uses_container_names_for_service_hosts_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    plan = PlanFile(
        app_name="test-app",
        storage={
            "example.Cache": StorageSpec(
                variable_name="cache",
                backend="memorystore-redis",
                kind="kv",
            )
        },
        deploy_config={"port": 8123},
    )

    monkeypatch.setattr("skaal.deploy.builders.local.platform.system", lambda: "Windows")

    stack = build_pulumi_stack(
        _make_app(),
        plan,
        output_dir=tmp_path / "artifacts",
        source_module="examples.counter",
    )

    app_props = stack["resources"]["app"]["properties"]
    assert app_props["networkMode"] == "${skaal-net.name}"
    assert "SKAAL_REDIS_URL_CACHE=redis://skaal-test-app-redis:6379" in app_props["envs"]
