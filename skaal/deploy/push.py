"""Artifact metadata and packaging helpers for deploy targets."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from skaal.deploy.pulumi import run_command


def _uv_or_pip() -> list[str]:
    """Return the base install command: ``uv pip install`` if uv is in PATH,
    otherwise ``python -m pip install``."""
    if shutil.which("uv") is not None:
        return ["uv", "pip", "install"]
    return [sys.executable, "-m", "pip", "install"]


# ── Metadata helpers ────────────────────────────────────────────────────────

META_FILE = "skaal-meta.json"


def write_meta(output_dir: Path, target: str, source_module: str, app_name: str) -> Path:
    """Write ``skaal-meta.json`` into *output_dir*; return the path."""
    meta: dict[str, str] = {
        "target": target,
        "source_module": source_module,
        "app_name": app_name,
    }
    path = output_dir / META_FILE
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return path


def read_meta(artifacts_dir: Path) -> dict[str, Any]:
    """Load ``skaal-meta.json`` from *artifacts_dir*; raise if missing."""
    path = artifacts_dir / META_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"{META_FILE} not found in {artifacts_dir}. "
            "Re-run `skaal build` to regenerate the artifacts."
        )
    return json.loads(path.read_text(encoding="utf-8"))


# ── AWS helpers ───────────────────────────────────────────────────────────────


def _package_aws(artifacts_dir: Path, project_root: Path, source_module: str) -> None:
    """
    Build ``lambda_package/`` inside *artifacts_dir*.

    Steps (all Python, no shell):
      1. Delete and recreate ``lambda_package/``.
      2. Install deps from ``pyproject.toml`` using ``uv pip install``
         (falls back to ``pip`` if uv is not in PATH).
      3. Copy ``handler.py`` into the package root.
      4. Copy the top-level source package (e.g. ``examples/``) from the
         project root so Lambda can import the user's app.
    """
    pkg_dir = artifacts_dir / "lambda_package"
    if pkg_dir.exists():
        shutil.rmtree(pkg_dir)
    pkg_dir.mkdir()

    # Install deps from pyproject.toml into the Lambda package directory.
    installer = _uv_or_pip()
    target_flag = "--target" if installer[0] == "uv" else "-t"
    run_command(
        [*installer, ".", target_flag, str(pkg_dir), "--quiet"],
        cwd=artifacts_dir,
        stage="install Lambda dependencies",
        recovery_hint=(
            "Check that the generated artifact pyproject.toml is valid and that the Python "
            "packaging toolchain is installed in the active environment."
        ),
    )

    # Entry point.
    shutil.copy2(artifacts_dir / "handler.py", pkg_dir / "handler.py")

    # User source package — copy the top-level directory.
    top_pkg = source_module.split(".")[0]
    src = project_root / top_pkg
    if src.is_dir():
        shutil.copytree(src, pkg_dir / top_pkg, dirs_exist_ok=True)


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
    run_command(
        ["gcloud", "auth", "configure-docker", f"{region}-docker.pkg.dev", "--quiet"],
        stage="configure Docker for Artifact Registry",
        recovery_hint=(
            "Authenticate with `gcloud auth login` and confirm the Artifact Registry API is enabled."
        ),
    )

    run_command(
        ["docker", "build", "-t", image, str(artifacts_dir)],
        stage="build Cloud Run container image",
        recovery_hint=(
            "Check that Docker is running and that the generated artifact directory builds locally."
        ),
    )
    run_command(
        ["docker", "push", image],
        stage="push container image to Artifact Registry",
        recovery_hint=(
            "Verify Docker credentials for Artifact Registry and confirm the target repository exists."
        ),
    )
