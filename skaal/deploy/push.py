"""Cross-platform artifact packaging and Pulumi deployment.

``package_and_push()`` is the single entry point used by ``skaal push``.
It reads ``skaal-meta.json`` from the artifacts directory to discover the
target platform and source package, then:

  AWS  — installs deps into ``lambda_package/``, copies source, runs ``pulumi up``.
  GCP  — runs ``pulumi up`` (provisions infra), builds + pushes the Docker image,
          then runs ``pulumi up`` again (deploys the new image to Cloud Run).

All I/O uses :mod:`pathlib` and :mod:`shutil` — no shell syntax, works on
Windows, macOS, and Linux.  External tools (``pulumi``, ``docker``,
``gcloud``) are invoked via :mod:`subprocess` using their unquoted name so
the OS PATH resolver handles platform differences automatically.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
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


def write_meta(output_dir: Path, target: str, source_module: str, app_name: str) -> Path:
    """Write ``skaal-meta.json`` into *output_dir*; return the path."""
    meta: dict[str, str] = {
        "target": target,
        "source_module": source_module,
        "app_name": app_name,
    }
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

def _run(cmd: list[str], cwd: Path | None = None, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a command, raising CalledProcessError on non-zero exit."""
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        capture_output=capture,
        text=True if capture else None,
    )


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
    _run(
        [*installer, ".", target_flag, str(pkg_dir), "--quiet"],
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
    result = subprocess.run(
        ["pulumi", "stack", "select", stack],
        cwd=artifacts_dir,
        capture_output=True,
    )
    if result.returncode != 0:
        _run(["pulumi", "stack", "init", stack], cwd=artifacts_dir)


def _pulumi_config_set(artifacts_dir: Path, config: dict[str, str]) -> None:
    for key, value in config.items():
        _run(["pulumi", "config", "set", key, value], cwd=artifacts_dir)


def _pulumi_up(artifacts_dir: Path, yes: bool) -> None:
    cmd = ["pulumi", "up"]
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
) -> dict[str, str]:
    """
    Package the app and deploy it using Pulumi.

    Reads ``skaal-meta.json`` from *artifacts_dir* to determine the target
    platform, source module, and app name.  All steps are cross-platform.

    Args:
        artifacts_dir: Path to the generated artifacts directory.
        stack:         Pulumi stack name (default: ``"dev"``).
        region:        Cloud region override.  For AWS defaults to
                       ``us-east-1``; for GCP defaults to ``us-central1``.
        gcp_project:   GCP project ID (required for GCP target).
        yes:           Pass ``--yes`` to ``pulumi up`` (non-interactive).

    Returns:
        Dict of Pulumi stack outputs (e.g. ``{"apiUrl": "https://..."}``)
        — empty dict for GCP until outputs are implemented.
    """
    artifacts_dir = Path(artifacts_dir).resolve()
    meta = read_meta(artifacts_dir)

    target: str = meta["target"]
    source_module: str = meta["source_module"]
    app_name: str = meta["app_name"]
    project_root = artifacts_dir.parent

    if target == "local":
        print("==> Starting local stack (docker compose up --build) ...")
        _run(["docker", "compose", "up", "--build"], cwd=artifacts_dir)
        return {}

    # Cloud targets need Pulumi stack initialisation.
    _pulumi_stack_select_or_init(artifacts_dir, stack)

    if target == "aws":
        aws_region = region or "us-east-1"
        _pulumi_config_set(artifacts_dir, {"aws:region": aws_region})

        print("==> Packaging Lambda ...")
        _package_aws(artifacts_dir, project_root, source_module)

        print("==> Deploying (pulumi up) ...")
        _pulumi_up(artifacts_dir, yes=yes)

        api_url = _pulumi_output(artifacts_dir, "apiUrl")
        print(f"\nApp URL: {api_url}")
        return {"apiUrl": api_url}

    elif target == "gcp":
        gcp_region = region or "us-central1"

        if not gcp_project:
            raise ValueError(
                "GCP project is required for --target=gcp. "
                "Pass --gcp-project PROJECT or set SKAAL_GCP_PROJECT."
            )

        _pulumi_config_set(artifacts_dir, {
            "gcp:project": gcp_project,
            "gcp:region": gcp_region,
        })

        # Phase 1: provision infrastructure (Artifact Registry + storage).
        print("==> Provisioning infrastructure (pulumi up) ...")
        _pulumi_up(artifacts_dir, yes=yes)

        # Phase 2: build and push the container image.
        repo = _pulumi_output(artifacts_dir, "imageRepository")
        print(f"==> Building and pushing image to {repo} ...")
        _build_push_image(artifacts_dir, gcp_project, gcp_region, repo, app_name)

        # Phase 3: deploy the new image to Cloud Run.
        print("==> Deploying image to Cloud Run (pulumi up) ...")
        _pulumi_up(artifacts_dir, yes=yes)

        service_url = _pulumi_output(artifacts_dir, "serviceUrl")
        print(f"\nApp URL: {service_url}")
        return {"serviceUrl": service_url}

    else:
        raise ValueError(f"Unknown deploy target {target!r}. Expected 'aws', 'gcp', or 'local'.")
