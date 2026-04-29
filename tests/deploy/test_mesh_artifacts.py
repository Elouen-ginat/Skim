"""Regression tests for precompiled mesh artifact generation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from skaal.deploy.aws import generate_artifacts as generate_aws_artifacts
from skaal.deploy.gcp import generate_artifacts as generate_gcp_artifacts
from skaal.deploy.local import generate_artifacts as generate_local_artifacts
from skaal.plan import PlanFile


def _make_app(name: str = "mesh-app") -> MagicMock:
    app = MagicMock()
    app.name = name
    del app._mounts
    app.__dict__.pop("_mounts", None)
    return app


def _write_source_tree(tmp_path: Path) -> None:
    src_pkg = tmp_path / "examples"
    src_pkg.mkdir()
    (src_pkg / "__init__.py").write_text("", encoding="utf-8")


def test_local_artifacts_skip_mesh_bundle_and_uv_source(tmp_path: Path) -> None:
    output_dir = tmp_path / "artifacts"
    _write_source_tree(tmp_path)

    generate_local_artifacts(
        _make_app(),
        PlanFile(app_name="mesh-app", deploy_config={"port": 8000}),
        output_dir=output_dir,
        source_module="examples.counter",
        stack_profile={"enable_mesh": True},
    )

    pyproject_text = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert "skaal-mesh" in pyproject_text
    assert "[tool.uv.sources]" not in pyproject_text
    assert not (output_dir / "mesh").exists()


def test_gcp_artifacts_skip_mesh_bundle_and_uv_source(tmp_path: Path) -> None:
    output_dir = tmp_path / "artifacts"
    _write_source_tree(tmp_path)

    generate_gcp_artifacts(
        _make_app(),
        PlanFile(app_name="mesh-app"),
        output_dir=output_dir,
        source_module="examples.counter",
        stack_profile={"enable_mesh": True},
    )

    pyproject_text = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert "skaal-mesh" in pyproject_text
    assert "[tool.uv.sources]" not in pyproject_text
    assert not (output_dir / "mesh").exists()


def test_aws_artifacts_record_mesh_runtime_metadata(tmp_path: Path) -> None:
    output_dir = tmp_path / "artifacts"

    generate_aws_artifacts(
        _make_app(),
        PlanFile(
            app_name="mesh-app",
            deploy_config={"runtime": "python3.12", "architecture": "arm64"},
        ),
        output_dir=output_dir,
        source_module="examples.counter",
        stack_profile={"enable_mesh": True},
    )

    pyproject_text = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    meta = json.loads((output_dir / "skaal-meta.json").read_text(encoding="utf-8"))
    pulumi_yaml = (output_dir / "Pulumi.yaml").read_text(encoding="utf-8")

    assert "skaal-mesh" in pyproject_text
    assert meta["lambda_architecture"] == "arm64"
    assert meta["lambda_runtime"] == "python3.12"
    assert "lambdaArchitecture" in pulumi_yaml


def test_generated_dockerfiles_do_not_require_rust_toolchain(tmp_path: Path) -> None:
    output_dir = tmp_path / "artifacts"
    _write_source_tree(tmp_path)
    app = _make_app()
    plan = PlanFile(app_name="mesh-app", deploy_config={"port": 8000})

    generate_local_artifacts(
        app,
        plan,
        output_dir=output_dir,
        source_module="examples.counter",
        stack_profile={"enable_mesh": True},
    )
    local_dockerfile = (output_dir / "Dockerfile").read_text(encoding="utf-8")
    assert "rustup" not in local_dockerfile
    assert "build-essential" not in local_dockerfile
    assert "cargo" not in local_dockerfile

    gcp_output_dir = tmp_path / "gcp-artifacts"
    generate_gcp_artifacts(
        app,
        PlanFile(app_name="mesh-app"),
        output_dir=gcp_output_dir,
        source_module="examples.counter",
        stack_profile={"enable_mesh": True},
    )
    gcp_dockerfile = (gcp_output_dir / "Dockerfile").read_text(encoding="utf-8")
    assert "rustup" not in gcp_dockerfile
    assert "build-essential" not in gcp_dockerfile
    assert "cargo" not in gcp_dockerfile


def test_local_dev_uses_docker_build_stage_when_no_linux_wheel(tmp_path: Path) -> None:
    """When no pre-built Linux wheel exists, a multi-stage Dockerfile is generated
    that compiles the mesh from the bundled Rust source instead of installing from PyPI."""
    output_dir = tmp_path / "artifacts"
    _write_source_tree(tmp_path)

    # Simulate a project with mesh Rust source but no pre-built Linux wheel.
    mesh_dir = tmp_path / "mesh"
    mesh_dir.mkdir()
    (mesh_dir / "Cargo.toml").write_text(
        '[package]\nname = "skaal-mesh"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (mesh_dir / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["maturin"]\nbuild-backend = "maturin"\n', encoding="utf-8"
    )
    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["mesh"]\n', encoding="utf-8")

    generate_local_artifacts(
        _make_app(),
        PlanFile(app_name="mesh-app", deploy_config={"port": 8000}),
        output_dir=output_dir,
        source_module="examples.counter",
        dev=True,
        stack_profile={"enable_mesh": True},
    )

    # skaal-mesh must NOT appear in pyproject.toml — the Dockerfile handles installation.
    pyproject_text = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert "skaal-mesh" not in pyproject_text

    # Mesh Rust source must be bundled into the build context.
    mesh_src = output_dir / "_mesh_src"
    assert mesh_src.is_dir()
    assert (mesh_src / "Cargo.toml").exists()
    assert (mesh_src / "mesh" / "Cargo.toml").exists()

    # Dockerfile must contain a rust build stage and a pip install step.
    dockerfile = (output_dir / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM rust:" in dockerfile
    assert "maturin build" in dockerfile
    assert "COPY --from=mesh-builder" in dockerfile
    assert "pip install" in dockerfile


def test_local_dev_artifacts_bundle_linux_mesh_wheel_when_available(tmp_path: Path) -> None:
    output_dir = tmp_path / "artifacts"
    _write_source_tree(tmp_path)

    wheels_dir = tmp_path / "target" / "wheels"
    wheels_dir.mkdir(parents=True)
    wheel_name = "skaal_mesh-0.2.0-cp311-cp311-manylinux2014_x86_64.whl"
    (wheels_dir / wheel_name).write_text("fake-wheel", encoding="utf-8")

    generate_local_artifacts(
        _make_app(),
        PlanFile(app_name="mesh-app", deploy_config={"port": 8000}),
        output_dir=output_dir,
        source_module="examples.counter",
        dev=True,
        stack_profile={"enable_mesh": True},
    )

    pyproject_text = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    bundled_wheel = output_dir / "_mesh_wheels" / wheel_name

    assert bundled_wheel.exists()
    assert f"skaal-mesh @ file:///app/_mesh_wheels/{wheel_name}" in pyproject_text
