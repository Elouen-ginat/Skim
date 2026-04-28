"""Tests for deploy helper integration points."""

from __future__ import annotations

import subprocess
from pathlib import Path

from skaal.deploy import push


def test_pulumi_env_injects_blank_pulumi_passphrase(monkeypatch):
    monkeypatch.delenv("PULUMI_CONFIG_PASSPHRASE", raising=False)
    env = push._pulumi_env()
    assert env["PULUMI_CONFIG_PASSPHRASE"] == ""


def test_pulumi_env_preserves_existing_pulumi_passphrase(monkeypatch):
    monkeypatch.setenv("PULUMI_CONFIG_PASSPHRASE", "secret")
    env = push._pulumi_env()
    assert env["PULUMI_CONFIG_PASSPHRASE"] == "secret"


def test_pulumi_login_local_uses_file_backend_uri(monkeypatch, tmp_path: Path):
    state_dir = tmp_path / ".pulumi-state"
    calls: list[tuple[list[str], Path | None, bool, bool]] = []

    def fake_run(
        cmd: list[str],
        cwd: Path | None = None,
        capture: bool = False,
        check: bool = True,
    ):
        calls.append((cmd, cwd, capture, check))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(push, "_run", fake_run)
    backend_url = push._pulumi_login_local(state_dir)

    assert state_dir.is_dir()
    expected_url = f"file://{state_dir.resolve().as_posix()}"
    assert backend_url == expected_url
    assert calls == [(["pulumi", "login", expected_url], None, False, True)]


def test_pulumi_stack_select_inits_missing_stack(monkeypatch, tmp_path: Path):
    calls: list[tuple[list[str], Path | None, bool, bool]] = []

    def fake_run(
        cmd: list[str],
        cwd: Path | None = None,
        capture: bool = False,
        check: bool = True,
    ):
        calls.append((cmd, cwd, capture, check))
        if cmd[:3] == ["pulumi", "stack", "select"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="missing")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(push, "_run", fake_run)

    push._pulumi_stack_select_or_init(tmp_path, "local")

    assert calls == [
        (["pulumi", "stack", "select", "local"], tmp_path, True, False),
        (["pulumi", "stack", "init", "local"], tmp_path, False, True),
    ]


def test_pulumi_destroy_passes_yes(monkeypatch, tmp_path: Path):
    calls: list[tuple[list[str], Path | None, bool, bool]] = []

    def fake_run(
        cmd: list[str],
        cwd: Path | None = None,
        capture: bool = False,
        check: bool = True,
    ):
        calls.append((cmd, cwd, capture, check))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(push, "_run", fake_run)

    push._pulumi_destroy(tmp_path, yes=True)

    assert calls == [(["pulumi", "destroy", "--yes"], tmp_path, False, True)]


def test_build_local_image_builds_and_inspects(monkeypatch, tmp_path: Path):
    calls: list[tuple[list[str], Path | None, bool]] = []

    def fake_run(
        cmd: list[str],
        cwd: Path | None = None,
        capture: bool = False,
        check: bool = True,
    ):
        calls.append((cmd, cwd, capture))
        stdout = "sha256:test-image\n" if cmd[:3] == ["docker", "image", "inspect"] else None
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout)

    monkeypatch.setattr(push, "_run", fake_run)

    image_ref = push._build_local_image(tmp_path, "skaal-test:local")

    assert image_ref == "sha256:test-image"
    assert calls == [
        (["docker", "build", "-t", "skaal-test:local", str(tmp_path.resolve())], tmp_path, False),
        (
            ["docker", "image", "inspect", "skaal-test:local", "--format", "{{.Id}}"],
            tmp_path,
            True,
        ),
    ]


def test_run_surfaces_clear_message_for_missing_pulumi(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(push.subprocess, "run", fake_run)

    try:
        push._pulumi_stack_select_or_init(Path("artifacts"), "local")
    except FileNotFoundError as exc:
        assert "Pulumi CLI was not found on PATH" in str(exc)
    else:
        raise AssertionError("Expected FileNotFoundError")


def test_run_surfaces_clear_message_for_missing_external_tool(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(push.subprocess, "run", fake_run)

    try:
        push._run(["docker", "build", "."])
    except FileNotFoundError as exc:
        assert "Required executable was not found on PATH: docker" in str(exc)
    else:
        raise AssertionError("Expected FileNotFoundError")
