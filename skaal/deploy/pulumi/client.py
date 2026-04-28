"""Subprocess layer for Pulumi operations and generic deploy commands.

:class:`PulumiClient` wraps a single artifact directory and exposes the
few Pulumi operations deploy needs: stack select/init, config set, up,
output.  :func:`run_command` is the generic fallback used for ``docker``
/ ``gcloud`` / ``uv`` invocations.  Both funnel through :func:`_run`
which converts ``subprocess`` failures into a typed
:class:`DeployCommandError` carrying stage context and a recovery hint.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Sequence

from skaal.errors import SkaalDeployError


class DeployCommandError(SkaalDeployError):
    """A required external deploy command failed."""

    def __init__(
        self,
        *,
        stage: str,
        command: Sequence[str],
        cwd: Path | None,
        returncode: int | None = None,
        output: str | None = None,
        recovery_hint: str | None = None,
    ) -> None:
        self.stage = stage
        self.command = [str(part) for part in command]
        self.cwd = cwd
        self.returncode = returncode
        self.output = (output or "").strip()
        self.recovery_hint = recovery_hint
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        lines = [f"Deployment step failed: {self.stage}"]
        lines.append(f"  Command: {subprocess.list2cmdline(self.command)}")
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

    def with_recovery_hint(self, hint: str) -> "DeployCommandError":
        merged = hint if not self.recovery_hint else f"{self.recovery_hint} {hint}".strip()
        return DeployCommandError(
            stage=self.stage,
            command=self.command,
            cwd=self.cwd,
            returncode=self.returncode,
            output=self.output,
            recovery_hint=merged,
        )


# ── Core runner ──────────────────────────────────────────────────────────────


def run_command(
    cmd: Sequence[str],
    *,
    stage: str,
    cwd: Path | None = None,
    capture: bool = False,
    check: bool = True,
    recovery_hint: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *cmd*; raise :class:`DeployCommandError` on failure."""
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
        _echo(result.stdout, result.stderr)

    if check and result.returncode != 0:
        raise DeployCommandError(
            stage=stage,
            command=cmd,
            cwd=cwd,
            returncode=result.returncode,
            output=_combine(result.stdout, result.stderr),
            recovery_hint=recovery_hint,
        )
    return result


def _echo(stdout: str | None, stderr: str | None) -> None:
    for stream, content in ((sys.stdout, stdout), (sys.stderr, stderr)):
        if not content:
            continue
        stream.write(content)
        if not content.endswith("\n"):
            stream.write("\n")
        stream.flush()


def _combine(stdout: str | None, stderr: str | None) -> str:
    parts = [p.strip() for p in (stdout, stderr) if p and p.strip()]
    return "\n".join(parts)


def _missing_tool_hint(tool: str) -> str:
    return f"Install {tool!r} and ensure it is available on PATH before retrying the deploy."


# ── Pulumi wrapper ───────────────────────────────────────────────────────────


class PulumiClient:
    """Convenience wrapper bound to one artifacts directory."""

    def __init__(self, artifacts_dir: Path) -> None:
        self.artifacts_dir = artifacts_dir

    # Stack management ------------------------------------------------------

    def select_or_init_stack(self, stack: str) -> None:
        result = run_command(
            ["pulumi", "stack", "select", stack],
            cwd=self.artifacts_dir,
            stage=f"select Pulumi stack {stack}",
            capture=True,
            check=False,
            recovery_hint=("Run `pulumi login` for the correct backend and verify the stack name."),
        )
        if result.returncode == 0:
            return

        output = _combine(result.stdout, result.stderr)
        if not _stack_missing(output):
            raise DeployCommandError(
                stage=f"select Pulumi stack {stack}",
                command=["pulumi", "stack", "select", stack],
                cwd=self.artifacts_dir,
                returncode=result.returncode,
                output=output,
                recovery_hint=(
                    "Run `pulumi login` for the correct backend and verify the stack name."
                ),
            )
        run_command(
            ["pulumi", "stack", "init", stack],
            cwd=self.artifacts_dir,
            stage=f"initialize Pulumi stack {stack}",
            recovery_hint=(
                "If the stack already exists elsewhere, select the correct Pulumi backend "
                "and rerun the deploy instead of creating a new stack."
            ),
        )

    def config_set(self, entries: dict[str, str]) -> None:
        for key, value in entries.items():
            run_command(
                ["pulumi", "config", "set", key, value],
                cwd=self.artifacts_dir,
                stage=f"set Pulumi config {key}",
                recovery_hint=(
                    "Check the config key name and ensure the selected Pulumi stack is writable."
                ),
            )

    def up(
        self,
        *,
        yes: bool,
        stage: str = "apply Pulumi stack",
        recovery_hint: str | None = None,
    ) -> None:
        cmd = ["pulumi", "up"]
        if yes:
            cmd.append("--yes")
        run_command(
            cmd,
            cwd=self.artifacts_dir,
            stage=stage,
            recovery_hint=(
                recovery_hint
                or "Inspect the pending changes with `pulumi preview`, then retry the deploy."
            ),
        )

    def output(self, name: str) -> str:
        result = run_command(
            ["pulumi", "stack", "output", name],
            cwd=self.artifacts_dir,
            stage=f"read Pulumi output {name}",
            capture=True,
            recovery_hint=(
                "Run `pulumi stack output` to confirm the stack completed and the name is correct."
            ),
        )
        return result.stdout.strip()


def _stack_missing(output: str) -> bool:
    text = output.lower()
    return (
        "no stack named" in text
        or "could not find stack" in text
        or ("stack" in text and "not found" in text)
        or ("stack" in text and "does not exist" in text)
    )
