"""Hot-reload supervisor for ``skaal run``.

The supervisor re-execs ``sys.executable -m skaal.cli.main run --no-reload …``
as a child process, watches the configured directories with ``watchfiles``,
and restarts the child on any source change.  Pure subprocess management —
no in-process import shenanigans, so the existing ``api.run`` path is reused
unchanged.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from skaal.types.cli import (
    ChangeStream,
    ChildArgv,
    ReloadDirs,
    ReloadMode,
)

_LOG = logging.getLogger("skaal.cli")
_DEV_ENVS = frozenset({"", "dev", "local", "development"})
_TERM_TIMEOUT_S = 5.0


class _Child(Protocol):
    """Minimal subprocess.Popen-shaped surface used by :func:`supervise`."""

    returncode: int | None

    def poll(self) -> int | None: ...
    def send_signal(self, sig: int) -> None: ...
    def wait(self, timeout: float | None = ...) -> int: ...
    def kill(self) -> None: ...


class _Spawn(Protocol):
    def __call__(self, argv: ChildArgv) -> _Child: ...


class _Watcher(Protocol):
    def __call__(self, dirs: ReloadDirs) -> ChangeStream: ...


def should_auto_reload(*, isatty: bool, skaal_env: str | None) -> bool:
    """Return ``True`` when ``auto`` mode should default to reload-on.

    On when stdout is a TTY and ``SKAAL_ENV`` is unset or names a dev-ish
    environment; off otherwise (CI, Docker, production runs).
    """
    if not isatty:
        return False
    return (skaal_env or "").lower() in _DEV_ENVS


def resolve_reload(mode: ReloadMode) -> bool:
    """Collapse a :class:`ReloadMode` into a concrete on/off decision."""
    if mode == "on":
        return True
    if mode == "off":
        return False
    return should_auto_reload(
        isatty=sys.stdout.isatty(),
        skaal_env=os.environ.get("SKAAL_ENV"),
    )


def default_reload_dirs() -> ReloadDirs:
    """Best-effort project root for the watcher; falls back to cwd."""
    from skaal.settings import find_pyproject

    pyproject = find_pyproject()
    return [pyproject.parent if pyproject else Path.cwd()]


def _watch(dirs: ReloadDirs) -> ChangeStream:
    """Yield change batches from ``watchfiles``; isolated for testability."""
    from watchfiles import PythonFilter, watch

    yield from watch(*dirs, watch_filter=PythonFilter(extra_extensions=(".toml",)))


def _default_spawn(argv: ChildArgv) -> _Child:
    return subprocess.Popen(argv)


def supervise(
    child_argv: ChildArgv,
    reload_dirs: ReloadDirs,
    *,
    spawn: _Spawn = _default_spawn,
    watcher: _Watcher = _watch,
) -> int:
    """Run *child_argv* under a file-watching supervisor.

    Returns the exit code to propagate. ``spawn`` and ``watcher`` are injected
    so unit tests can drive the loop without real subprocesses.
    """
    _LOG.info("watching %d path(s); Ctrl-C to stop", len(reload_dirs))
    child = spawn(child_argv)
    try:
        for changes in watcher(reload_dirs):
            paths = sorted({path for _, path in changes})
            _LOG.info("reload: %s", ", ".join(paths))
            _terminate(child)
            child = spawn(child_argv)
    except KeyboardInterrupt:
        pass
    finally:
        _terminate(child)
    return child.returncode or 0


def _terminate(child: _Child) -> None:
    if child.poll() is not None:
        return
    child.send_signal(signal.SIGTERM)
    try:
        child.wait(timeout=_TERM_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def child_command(argv_tail: Iterable[str]) -> ChildArgv:
    """Build the argv used to relaunch the runtime in non-reload mode."""
    return [sys.executable, "-m", "skaal.cli.main", "run", "--no-reload", *argv_tail]
