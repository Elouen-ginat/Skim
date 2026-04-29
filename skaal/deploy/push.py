"""Cross-platform artifact packaging and Pulumi deployment.

``package_and_push()`` is the single public entry point used by ``skaal deploy``.
It reads ``skaal-meta.json`` from the artifacts directory, resolves the target
via :func:`~skaal.deploy.registry.get_target`, and delegates all platform-specific
logic to the target's :meth:`~skaal.deploy.target.DeployTarget.package_and_push` method.

The private helpers in this module (``_package_aws``, ``_pulumi_*``,
``_build_push_image``) are the low-level subprocess layer. They are called by
the target adapter classes in :mod:`skaal.deploy.registry`.

All I/O uses :mod:`pathlib` and :mod:`shutil` — no shell syntax, works on
Windows, macOS, and Linux. External tools (``pulumi``, ``docker``,
``gcloud``) are invoked via :mod:`subprocess` using their unquoted name so
the OS PATH resolver handles platform differences automatically.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any


def _uv_or_pip() -> list[str]:
    """Return the base install command: ``uv pip install`` if uv is in PATH,
    otherwise ``python -m pip install``."""
    if shutil.which("uv") is not None:
        return ["uv", "pip", "install"]
    return [sys.executable, "-m", "pip", "install"]


# ── Metadata helpers ──────────────────────────────────────────────────────────

META_FILE = "skaal-meta.json"


def write_meta(
    output_dir: Path,
    target: str,
    source_module: str,
    app_name: str,
    extra_fields: dict[str, Any] | None = None,
) -> Path:
    """Write ``skaal-meta.json`` into *output_dir*; return the path."""
    meta: dict[str, str] = {
        "target": target,
        "source_module": source_module,
        "app_name": app_name,
    }
    if extra_fields:
        meta.update(extra_fields)
    path = output_dir / META_FILE
    path.write_text(json.dumps(meta, indent=2))
    return path


def read_meta(artifacts_dir: Path) -> dict[str, Any]:
    """Load ``skaal-meta.json`` from *artifacts_dir*; raise if missing."""
    path = artifacts_dir / META_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"{META_FILE} not found in {artifacts_dir}. "
            "Re-run `skaal build` to regenerate the artifacts."
        )
    return json.loads(path.read_text())


# ── Subprocess helper ─────────────────────────────────────────────────────────


def _run(
    cmd: list[str], cwd: Path | None = None, capture: bool = False, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a command, raising CalledProcessError on non-zero exit."""
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            check=check,
            capture_output=capture,
            text=True if capture else None,
            env=_pulumi_env(),
        )
    except FileNotFoundError as exc:
        tool = cmd[0]
        if tool == "pulumi":
            message = "Pulumi CLI was not found on PATH. Install `pulumi` and retry `skaal deploy`."
        else:
            message = f"Required executable was not found on PATH: {tool}"
        raise FileNotFoundError(message) from exc


def _pulumi_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PULUMI_CONFIG_PASSPHRASE", "")
    return env


# ── AWS helpers ───────────────────────────────────────────────────────────────


def _artifact_dependencies(artifacts_dir: Path) -> list[str]:
    pyproject_path = artifacts_dir / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = data.get("project", {})
    dependencies = project.get("dependencies", [])
    return [str(dep) for dep in dependencies]


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


def _package_aws(
    artifacts_dir: Path,
    project_root: Path,
    source_module: str,
    *,
    lambda_architecture: str = "x86_64",
    lambda_runtime: str = "python3.11",
) -> None:
    """
    Build ``lambda_package/`` inside *artifacts_dir*.

     Steps (all Python, no shell):
      1. Delete and recreate ``lambda_package/``.
        2. Install runtime deps from ``pyproject.toml`` into the target dir.
            ``skaal-mesh`` is installed with a Lambda-compatible manylinux wheel.
      3. Copy ``handler.py`` into the package root.
      4. Copy the top-level source package (e.g. ``examples/``) from the
         project root so Lambda can import the user's app.
    """
    pkg_dir = artifacts_dir / "lambda_package"
    if pkg_dir.exists():
        shutil.rmtree(pkg_dir)
    pkg_dir.mkdir()

    dependencies = _artifact_dependencies(artifacts_dir)
    mesh_deps = [dep for dep in dependencies if dep.startswith("skaal-mesh")]
    base_deps = [dep for dep in dependencies if dep not in mesh_deps]

    if base_deps:
        _run(
            [sys.executable, "-m", "pip", "install", *base_deps, "-t", str(pkg_dir), "--quiet"],
            cwd=artifacts_dir,
        )

    if mesh_deps:
        python_version, abi = _lambda_python_tags(lambda_runtime)
        platform = _lambda_platform(lambda_architecture)
        _run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                *mesh_deps,
                "-t",
                str(pkg_dir),
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

    # Entry point.
    shutil.copy2(artifacts_dir / "handler.py", pkg_dir / "handler.py")

    # User source package — copy the top-level directory.
    top_pkg = source_module.split(".")[0]
    src = project_root / top_pkg
    if src.is_dir():
        shutil.copytree(src, pkg_dir / top_pkg, dirs_exist_ok=True)


# ── Pulumi helpers ────────────────────────────────────────────────────────────


def _pulumi_stack_select_or_init(artifacts_dir: Path, stack: str) -> None:
    """Select *stack* if it exists, otherwise initialise it."""
    result = _run(
        ["pulumi", "stack", "select", stack],
        cwd=artifacts_dir,
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        _run(["pulumi", "stack", "init", stack], cwd=artifacts_dir)


def _pulumi_stack_select(artifacts_dir: Path, stack: str) -> None:
    """Select an existing Pulumi *stack*."""
    _run(["pulumi", "stack", "select", stack], cwd=artifacts_dir)


def _pulumi_local_backend_url(state_dir: Path) -> str:
    """Build a Pulumi filestate backend URL for *state_dir*.

    Pulumi's Windows backend parser rejects the RFC-style ``file:///C:/...`` URI
    emitted by :meth:`pathlib.Path.as_uri`. It accepts ``file://C:/...`` instead.
    """
    posix_path = state_dir.resolve().as_posix()
    return f"file://{posix_path}"


def _pulumi_login_local(state_dir: Path) -> str:
    """Prepare the local filestate backend rooted at *state_dir* and return its URL."""
    resolved_state_dir = state_dir.resolve()
    resolved_state_dir.mkdir(parents=True, exist_ok=True)
    backend_url = _pulumi_local_backend_url(resolved_state_dir)
    _run(["pulumi", "login", backend_url])
    return backend_url


def _pulumi_config_set(artifacts_dir: Path, config: dict[str, str]) -> None:
    for key, value in config.items():
        _run(["pulumi", "config", "set", key, value], cwd=artifacts_dir)


def _pulumi_up(artifacts_dir: Path, yes: bool) -> None:
    cmd = ["pulumi", "up"]
    if yes:
        cmd.append("--yes")
    _run(cmd, cwd=artifacts_dir)


def _pulumi_destroy(artifacts_dir: Path, yes: bool) -> None:
    cmd = ["pulumi", "destroy"]
    if yes:
        cmd.append("--yes")
    _run(cmd, cwd=artifacts_dir)


def _pulumi_output(artifacts_dir: Path, output_name: str) -> str:
    result = _run(
        ["pulumi", "stack", "output", output_name],
        cwd=artifacts_dir,
        capture=True,
    )
    return result.stdout.strip()


def _build_local_image(artifacts_dir: Path, image_name: str) -> str:
    _run(["docker", "build", "-t", image_name, str(artifacts_dir.resolve())], cwd=artifacts_dir)
    result = _run(
        ["docker", "image", "inspect", image_name, "--format", "{{.Id}}"],
        cwd=artifacts_dir,
        capture=True,
    )
    return result.stdout.strip()


# ── GCP helpers ───────────────────────────────────────────────────────────────


def _build_push_image(
    artifacts_dir: Path,
    project: str,
    region: str,
    repo: str,
    app_name: str,
) -> None:
    """Build the Docker image and push it to Artifact Registry."""
    image = f"{region}-docker.pkg.dev/{project}/{repo}/{app_name}:latest"

    # Authenticate Docker with the registry (idempotent).
    _run(["gcloud", "auth", "configure-docker", f"{region}-docker.pkg.dev", "--quiet"])

    _run(["docker", "build", "-t", image, str(artifacts_dir)])
    _run(["docker", "push", image])


# ── Public entry point ────────────────────────────────────────────────────────


def package_and_push(
    artifacts_dir: Path,
    *,
    stack: str = "dev",
    region: str | None = None,
    gcp_project: str | None = None,
    yes: bool = True,
    config_overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Package the app and deploy it using Pulumi.

    Reads ``skaal-meta.json`` from *artifacts_dir* to determine the target
    platform, then delegates to the appropriate
    :class:`~skaal.deploy.target.DeployTarget` adapter.

    Args:
        artifacts_dir:    Path to the artifacts directory produced by ``skaal build``.
        stack:            Pulumi stack name (default: ``"dev"``).
        region:           Cloud region override.
        gcp_project:      GCP project ID (required for GCP target).
        yes:              Pass ``--yes`` to ``pulumi up`` (non-interactive).
        config_overrides: Extra ``pulumi config set`` key/value pairs applied
                          after the core project/region config.

    Returns:
        Dict of Pulumi stack outputs (e.g. ``{"apiUrl": "https://..."}``).
    """
    from skaal.deploy.registry import get_target

    artifacts_dir = Path(artifacts_dir).resolve()
    meta = read_meta(artifacts_dir)

    return get_target(meta["target"]).package_and_push(
        artifacts_dir,
        stack=stack,
        region=region,
        gcp_project=gcp_project,
        yes=yes,
        project_root=artifacts_dir.parent,
        source_module=meta["source_module"],
        app_name=meta["app_name"],
        config_overrides=config_overrides,
    )


def destroy_stack(
    artifacts_dir: Path,
    *,
    stack: str = "dev",
    yes: bool = True,
) -> None:
    """Destroy resources for an existing Pulumi stack.

    Reads ``skaal-meta.json`` from *artifacts_dir* to determine the target
    platform, then delegates to the appropriate target adapter.
    """
    from skaal.deploy.registry import get_target

    artifacts_dir = Path(artifacts_dir).resolve()
    meta = read_meta(artifacts_dir)

    get_target(meta["target"]).destroy_stack(
        artifacts_dir,
        stack=stack,
        yes=yes,
    )
