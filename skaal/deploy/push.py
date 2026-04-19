"""Cross-platform artifact packaging and Pulumi deployment.

``package_and_push()`` is the single public entry point used by ``skaal deploy``.
It reads ``skaal-meta.json`` from the artifacts directory, resolves the target
via :func:`~skaal.deploy.registry.get_target`, and delegates all platform-specific
logic to the target's :meth:`~skaal.deploy.target.DeployTarget.package_and_push` method.

The private helpers in this module (``_package_aws``, ``_pulumi_*``,
``_build_push_image``) are the low-level subprocess layer.  They are called by
the target adapter classes in :mod:`skaal.deploy.registry`.

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

from skaal.errors import SkaalDeployError


def _uv_or_pip() -> list[str]:
    """Return the base install command: ``uv pip install`` if uv is in PATH,
    otherwise ``python -m pip install``."""
    if shutil.which("uv") is not None:
        return ["uv", "pip", "install"]
    return [sys.executable, "-m", "pip", "install"]


# ── Metadata helpers ──────────────────────────────────────────────────────────

META_FILE = "skaal-meta.json"


class DeployCommandError(SkaalDeployError):
    """A required external deployment command failed."""

    def __init__(
        self,
        *,
        stage: str,
        command: list[str],
        cwd: Path | None,
        returncode: int | None = None,
        output: str | None = None,
        recovery_hint: str | None = None,
    ) -> None:
        self.stage = stage
        self.command = [str(part) for part in command]
        self.cwd = cwd
        self.returncode = returncode
        self.output = output.strip() if output else ""
        self.recovery_hint = recovery_hint
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        lines = [f"Deployment step failed: {self.stage}"]
        lines.append(f"  Command: {_format_command(self.command)}")
        if self.cwd is not None:
            lines.append(f"  Working directory: {self.cwd}")
        if self.returncode is not None:
            lines.append(f"  Exit code: {self.returncode}")
        if self.output:
            lines.append("  Output:")
            lines.extend(f"    {line}" for line in self.output.splitlines())
        if self.recovery_hint:
            lines.append(f"  Recovery: {self.recovery_hint}")
        return "\n".join(lines)

    def with_recovery_hint(self, recovery_hint: str) -> "DeployCommandError":
        hint = recovery_hint
        if self.recovery_hint and recovery_hint not in self.recovery_hint:
            hint = f"{self.recovery_hint} {recovery_hint}".strip()
        return DeployCommandError(
            stage=self.stage,
            command=self.command,
            cwd=self.cwd,
            returncode=self.returncode,
            output=self.output,
            recovery_hint=hint,
        )


def _format_command(cmd: list[str]) -> str:
    return subprocess.list2cmdline([str(part) for part in cmd])


def _combine_output(stdout: str | None, stderr: str | None) -> str:
    parts = [part.strip() for part in (stdout, stderr) if part and part.strip()]
    return "\n".join(parts)


def _print_output(stdout: str | None, stderr: str | None) -> None:
    if stdout:
        sys.stdout.write(stdout)
        if not stdout.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
    if stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith("\n"):
            sys.stderr.write("\n")
        sys.stderr.flush()


def _missing_tool_hint(tool: str) -> str:
    return f"Install {tool!r} and ensure it is available on PATH before retrying the deploy."


def _pulumi_stack_missing(output: str) -> bool:
    text = output.lower()
    return (
        "no stack named" in text
        or "could not find stack" in text
        or ("stack" in text and "not found" in text)
        or ("stack" in text and "does not exist" in text)
    )


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


# ── Subprocess helper ─────────────────────────────────────────────────────────


def _run(
    cmd: list[str],
    cwd: Path | None = None,
    *,
    stage: str,
    capture: bool = False,
    check: bool = True,
    recovery_hint: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a deploy command and wrap failures with stage-aware context."""
    try:
        result = subprocess.run(
            [str(part) for part in cmd],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise DeployCommandError(
            stage=stage,
            command=cmd,
            cwd=cwd,
            output=f"Executable {cmd[0]!r} was not found on PATH.",
            recovery_hint=recovery_hint or _missing_tool_hint(str(cmd[0])),
        ) from exc

    if not capture:
        _print_output(result.stdout, result.stderr)

    if check and result.returncode != 0:
        raise DeployCommandError(
            stage=stage,
            command=cmd,
            cwd=cwd,
            returncode=result.returncode,
            output=_combine_output(result.stdout, result.stderr),
            recovery_hint=recovery_hint,
        )

    return result


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


# ── Pulumi helpers ────────────────────────────────────────────────────────────


def _pulumi_stack_select_or_init(artifacts_dir: Path, stack: str) -> None:
    """Select *stack* if it exists, otherwise initialise it."""
    result = _run(
        ["pulumi", "stack", "select", stack],
        cwd=artifacts_dir,
        stage=f"select Pulumi stack {stack}",
        capture=True,
        check=False,
        recovery_hint=(
            "Run `pulumi login` for the correct backend and verify the stack name before retrying."
        ),
    )
    if result.returncode == 0:
        return

    output = _combine_output(result.stdout, result.stderr)
    if not _pulumi_stack_missing(output):
        raise DeployCommandError(
            stage=f"select Pulumi stack {stack}",
            command=["pulumi", "stack", "select", stack],
            cwd=artifacts_dir,
            returncode=result.returncode,
            output=output,
            recovery_hint=(
                "Run `pulumi login` for the correct backend and verify the stack name before retrying."
            ),
        )

    _run(
        ["pulumi", "stack", "init", stack],
        cwd=artifacts_dir,
        stage=f"initialize Pulumi stack {stack}",
        recovery_hint=(
            "If the stack already exists elsewhere, select the correct Pulumi backend and rerun "
            "the deploy instead of creating a new stack."
        ),
    )


def _pulumi_config_set(artifacts_dir: Path, config: dict[str, str]) -> None:
    for key, value in config.items():
        _run(
            ["pulumi", "config", "set", key, value],
            cwd=artifacts_dir,
            stage=f"set Pulumi config {key}",
            recovery_hint=(
                "Check the config key name and ensure the selected Pulumi stack is writable."
            ),
        )


def _pulumi_up(
    artifacts_dir: Path,
    yes: bool,
    *,
    stage: str = "apply Pulumi stack",
    recovery_hint: str | None = None,
) -> None:
    cmd = ["pulumi", "up"]
    if yes:
        cmd.append("--yes")
    _run(
        cmd,
        cwd=artifacts_dir,
        stage=stage,
        recovery_hint=recovery_hint
        or "Inspect the pending changes with `pulumi preview`, then retry the deploy.",
    )


def _pulumi_output(artifacts_dir: Path, output_name: str) -> str:
    result = _run(
        ["pulumi", "stack", "output", output_name],
        cwd=artifacts_dir,
        stage=f"read Pulumi output {output_name}",
        capture=True,
        recovery_hint=(
            "Run `pulumi stack output` manually to confirm the stack completed and the output name is correct."
        ),
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
    _run(
        ["gcloud", "auth", "configure-docker", f"{region}-docker.pkg.dev", "--quiet"],
        stage="configure Docker for Artifact Registry",
        recovery_hint=(
            "Authenticate with `gcloud auth login` and confirm the Artifact Registry API is enabled."
        ),
    )

    _run(
        ["docker", "build", "-t", image, str(artifacts_dir)],
        stage="build Cloud Run container image",
        recovery_hint=(
            "Check that Docker is running and that the generated artifact directory builds locally."
        ),
    )
    _run(
        ["docker", "push", image],
        stage="push container image to Artifact Registry",
        recovery_hint=(
            "Verify Docker credentials for Artifact Registry and confirm the target repository exists."
        ),
    )


# ── Public entry point ────────────────────────────────────────────────────────


def package_and_push(
    artifacts_dir: Path,
    *,
    stack: str = "dev",
    region: str | None = None,
    gcp_project: str | None = None,
    yes: bool = True,
    config_overrides: dict[str, str] | None = None,
    runtime_options: dict[str, Any] | None = None,
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
        runtime_options:  Optional deploy-time behavior flags passed through to
                  the target adapter, e.g. local compose detach/log settings.

    Returns:
        Dict of Pulumi stack outputs (e.g. ``{"apiUrl": "https://..."}``)
        — empty dict for the local target.
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
        runtime_options=runtime_options,
    )
