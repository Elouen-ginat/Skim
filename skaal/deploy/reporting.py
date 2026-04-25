"""Deploy reporter — separates presentation from orchestration.

Every target pushes progress messages through a :class:`DeployReporter`
instead of calling ``typer.echo`` directly.  This keeps the business
logic testable (inject a silent reporter in tests) and lets future
callers swap the CLI echoer for structured JSON, a log stream, or a
progress-bar UI.
"""

from __future__ import annotations

from typing import Protocol


class DeployReporter(Protocol):
    """What targets emit during build & push."""

    def info(self, message: str) -> None: ...
    def step(self, message: str) -> None: ...
    def result(self, message: str) -> None: ...


class TyperReporter:
    """Default implementation backed by :mod:`typer`."""

    def __init__(self) -> None:
        import typer  # imported lazily so this module stays import-light

        self._echo = typer.echo

    def info(self, message: str) -> None:
        self._echo(message)

    def step(self, message: str) -> None:
        self._echo(f"==> {message}")

    def result(self, message: str) -> None:
        self._echo(f"\n{message}")


class SilentReporter:
    """Used in tests — drops every message on the floor."""

    def info(self, message: str) -> None: ...
    def step(self, message: str) -> None: ...
    def result(self, message: str) -> None: ...
