from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from skaal.deploy.errors import DeployError
from skaal.deploy.packaging.pip_runner import run_pip


def _artifact_dependencies(artifacts_dir: Path) -> list[str]:
    pyproject_path = artifacts_dir / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = data.get("project", {})
    dependencies = project.get("dependencies", [])
    return [str(dependency) for dependency in dependencies]


def _lambda_platform(lambda_architecture: str) -> str:
    arch = lambda_architecture.lower()
    if arch == "arm64":
        return "manylinux2014_aarch64"
    return "manylinux2014_x86_64"


def _lambda_python_tags(lambda_runtime: str) -> tuple[str, str]:
    if not lambda_runtime.startswith("python3."):
        return "311", "cp311"
    version = lambda_runtime.removeprefix("python")
    major_minor = version.replace(".", "")
    return major_minor, f"cp{major_minor}"


def _diagnostics(exc: subprocess.CalledProcessError) -> str | None:
    parts = [part.strip() for part in (exc.stderr, exc.output) if part and part.strip()]
    return "\n".join(parts) or None


def _run_install(cmd: list[str], *, cwd: Path) -> None:
    try:
        run_pip(cmd, cwd=cwd)
    except FileNotFoundError as exc:
        raise DeployError(
            target="aws",
            phase="package",
            message=f"Required executable was not found on PATH: {cmd[0]}",
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise DeployError(
            target="aws",
            phase="package",
            message="Lambda dependency installation failed.",
            diagnostics=_diagnostics(exc),
        ) from exc


def package_lambda(
    artifacts_dir: Path,
    project_root: Path,
    source_module: str,
    *,
    lambda_architecture: str = "x86_64",
    lambda_runtime: str = "python3.11",
) -> None:
    package_dir = artifacts_dir / "lambda_package"
    if package_dir.exists():
        shutil.rmtree(package_dir)
    package_dir.mkdir()

    dependencies = _artifact_dependencies(artifacts_dir)
    mesh_dependencies = [
        dependency for dependency in dependencies if dependency.startswith("skaal-mesh")
    ]
    base_dependencies = [
        dependency for dependency in dependencies if dependency not in mesh_dependencies
    ]

    if base_dependencies:
        _run_install(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                *base_dependencies,
                "-t",
                str(package_dir),
                "--quiet",
            ],
            cwd=artifacts_dir,
        )

    if mesh_dependencies:
        python_version, abi = _lambda_python_tags(lambda_runtime)
        platform = _lambda_platform(lambda_architecture)
        _run_install(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                *mesh_dependencies,
                "-t",
                str(package_dir),
                "--quiet",
                "--platform",
                platform,
                "--only-binary=:all:",
                "--implementation",
                "cp",
                "--python-version",
                python_version,
                "--abi",
                abi,
                "--no-deps",
            ],
            cwd=artifacts_dir,
        )

    shutil.copy2(artifacts_dir / "handler.py", package_dir / "handler.py")

    top_package = source_module.split(".")[0]
    source_dir = project_root / top_package
    if source_dir.is_dir():
        shutil.copytree(source_dir, package_dir / top_package, dirs_exist_ok=True)
