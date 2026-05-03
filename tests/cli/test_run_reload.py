"""Tests for ``skaal run`` hot-reload supervisor."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from skaal.cli import _reload


@pytest.mark.parametrize(
    ("isatty", "skaal_env", "expected"),
    [
        (True, None, True),
        (True, "dev", True),
        (True, "local", True),
        (True, "development", True),
        (True, "production", False),
        (True, "staging", False),
        (False, None, False),
        (False, "dev", False),
    ],
)
def test_should_auto_reload_matrix(isatty: bool, skaal_env: str | None, expected: bool) -> None:
    assert _reload.should_auto_reload(isatty=isatty, skaal_env=skaal_env) is expected


def test_resolve_reload_explicit_modes_short_circuit() -> None:
    assert _reload.resolve_reload("on") is True
    assert _reload.resolve_reload("off") is False


def test_child_command_uses_no_reload_flag() -> None:
    cmd = _reload.child_command(["mod:app", "--port", "8000"])
    assert cmd[1:] == ["-m", "skaal.cli.main", "run", "--no-reload", "mod:app", "--port", "8000"]


class _FakeChild:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.signals: list[Any] = []
        self.killed = False
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sig: Any) -> None:
        self.signals.append(sig)
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_supervise_restarts_child_on_change(tmp_path: Path) -> None:
    spawned: list[_FakeChild] = []

    def fake_spawn(argv: list[str]) -> _FakeChild:
        child = _FakeChild(argv)
        spawned.append(child)
        return child

    def fake_watcher(_dirs: list[Path]) -> Any:
        yield {(1, str(tmp_path / "app.py"))}

    rc = _reload.supervise(
        ["python", "-m", "skaal.cli.main", "run", "--no-reload", "mod:app"],
        [tmp_path],
        spawn=fake_spawn,
        watcher=fake_watcher,
    )
    assert rc == 0
    assert len(spawned) == 2
    assert spawned[0].signals  # original got SIGTERM


def test_supervise_handles_keyboard_interrupt(tmp_path: Path) -> None:
    spawned: list[_FakeChild] = []

    def fake_spawn(argv: list[str]) -> _FakeChild:
        child = _FakeChild(argv)
        spawned.append(child)
        return child

    def fake_watcher(_dirs: list[Path]) -> Any:
        raise KeyboardInterrupt
        yield  # pragma: no cover  - generator marker

    rc = _reload.supervise(
        ["python"], [tmp_path], spawn=fake_spawn, watcher=fake_watcher
    )
    assert rc == 0
    assert len(spawned) == 1
    assert spawned[0].signals  # SIGTERM on shutdown


def test_run_command_reload_off_invokes_api_run(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from skaal.cli.main import app as cli_app

    runner = CliRunner()
    with mock.patch("skaal.api.run") as mock_run:
        runner.invoke(
            cli_app,
            ["run", "mod:app", "--no-reload", "--port", "9999"],
        )
    assert mock_run.called
    kwargs = mock_run.call_args.kwargs
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9999
    assert kwargs["redis"] is None
    assert kwargs["persist"] is False
