"""Tests for deploy subprocess orchestration helpers."""

from __future__ import annotations

import subprocess

import pytest

from skaal.deploy import push
from skaal.deploy.registry import LocalDockerComposeTarget


def test_run_wraps_missing_tool_with_deploy_context(tmp_path) -> None:
    """Missing executables should surface as DeployCommandError with recovery help."""
    with pytest.raises(push.DeployCommandError) as exc_info:
        push._run(["skaal-tool-that-does-not-exist"], cwd=tmp_path, stage="probe deploy tool")

    message = str(exc_info.value)
    assert "Deployment step failed: probe deploy tool" in message
    assert "not found on PATH" in message
    assert "Recovery:" in message


def test_pulumi_stack_select_or_init_initializes_missing_stack(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing stack should trigger `pulumi stack init`, not fail immediately."""
    calls: list[tuple[list[str], str, bool, bool]] = []

    def _fake_run(
        cmd: list[str],
        cwd=None,
        *,
        stage: str,
        capture: bool = False,
        check: bool = True,
        recovery_hint: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, stage, capture, check))
        if cmd[:3] == ["pulumi", "stack", "select"]:
            return subprocess.CompletedProcess(cmd, 255, "", "error: no stack named dev")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(push, "_run", _fake_run)

    push._pulumi_stack_select_or_init(tmp_path, "dev")

    assert calls[0][0] == ["pulumi", "stack", "select", "dev"]
    assert calls[1][0] == ["pulumi", "stack", "init", "dev"]


def test_pulumi_stack_select_or_init_preserves_real_select_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-missing-stack failures should not be turned into stack init attempts."""
    calls: list[list[str]] = []

    def _fake_run(
        cmd: list[str],
        cwd=None,
        *,
        stage: str,
        capture: bool = False,
        check: bool = True,
        recovery_hint: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 255, "", "backend is offline")

    monkeypatch.setattr(push, "_run", _fake_run)

    with pytest.raises(push.DeployCommandError) as exc_info:
        push._pulumi_stack_select_or_init(tmp_path, "dev")

    assert calls == [["pulumi", "stack", "select", "dev"]]
    assert "backend is offline" in str(exc_info.value)


def test_local_target_detach_and_follow_logs(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Local deploy runtime options should switch compose into detached log-follow mode."""
    calls: list[list[str]] = []

    def _fake_run(
        cmd: list[str],
        cwd=None,
        *,
        stage: str,
        capture: bool = False,
        check: bool = True,
        recovery_hint: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(push, "_run", _fake_run)

    LocalDockerComposeTarget().package_and_push(
        tmp_path,
        stack="dev",
        region=None,
        gcp_project=None,
        yes=True,
        project_root=tmp_path,
        source_module="examples.counter",
        app_name="demo",
        runtime_options={"detach": True, "follow_logs": True},
    )

    assert calls == [
        ["docker", "compose", "up", "--build", "--detach"],
        ["docker", "compose", "logs", "--follow"],
    ]


def test_local_target_attached_mode_ignores_follow_logs(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attached mode should keep the single blocking `docker compose up` behavior."""
    calls: list[list[str]] = []

    def _fake_run(
        cmd: list[str],
        cwd=None,
        *,
        stage: str,
        capture: bool = False,
        check: bool = True,
        recovery_hint: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(push, "_run", _fake_run)

    LocalDockerComposeTarget().package_and_push(
        tmp_path,
        stack="dev",
        region=None,
        gcp_project=None,
        yes=True,
        project_root=tmp_path,
        source_module="examples.counter",
        app_name="demo",
        runtime_options={"detach": False, "follow_logs": True},
    )

    assert calls == [["docker", "compose", "up", "--build"]]
