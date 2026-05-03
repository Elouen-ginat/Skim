"""Tests for AWS Lambda packaging helpers."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from skaal.deploy.packaging.lambda_pkg import package_lambda


def test_package_aws_uses_platform_specific_mesh_wheel(tmp_path: Path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    (artifacts_dir / "pyproject.toml").write_text(
        """
[project]
name = "demo"
version = "0.1.0"
dependencies = [
    "skaal[runtime,aws]",
    "skaal-mesh",
]
""".strip(),
        encoding="utf-8",
    )
    (artifacts_dir / "handler.py").write_text("def handler(event, context): return {}\n")
    src_pkg = tmp_path / "examples"
    src_pkg.mkdir()
    (src_pkg / "__init__.py").write_text("", encoding="utf-8")

    with mock.patch("skaal.deploy.packaging.lambda_pkg.run_pip") as fake_run:
        package_lambda(
            artifacts_dir,
            tmp_path,
            "examples.counter",
            lambda_architecture="arm64",
            lambda_runtime="python3.12",
        )

    install_commands = [call.args[0] for call in fake_run.call_args_list]
    assert any("skaal[runtime,aws]" in cmd for cmd in install_commands)

    mesh_cmd = next(cmd for cmd in install_commands if "skaal-mesh" in cmd)
    assert "--platform" in mesh_cmd
    assert "manylinux2014_aarch64" in mesh_cmd
    assert "--only-binary=:all:" in mesh_cmd
    assert "--python-version" in mesh_cmd
    assert "312" in mesh_cmd
