from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import typer

from skaal.errors import SkaalError, UnsatisfiableConstraints

P = ParamSpec("P")
R = TypeVar("R")

_LOG = logging.getLogger("skaal.cli")


def _log_error(exc: BaseException) -> None:
    _LOG.error(str(exc), exc_info=_LOG.isEnabledFor(logging.DEBUG))


def _print_unsat(exc: UnsatisfiableConstraints) -> None:
    """Render an UNSAT diagnosis through Rich (or plain stderr without TTY)."""
    if exc.diagnosis is None:
        _log_error(exc)
        return
    from rich.console import Console

    from skaal.solver.explain import render_diagnosis

    use_rich = sys.stderr.isatty()
    Console(stderr=True, force_terminal=use_rich).print(
        render_diagnosis(exc.diagnosis, rich=use_rich)
    )


def cli_error_boundary(func: Callable[P, R]) -> Callable[P, R]:
    @wraps(func)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except typer.Exit:
            raise
        except UnsatisfiableConstraints as exc:
            _print_unsat(exc)
            raise typer.Exit(exc.exit_code) from exc
        except SkaalError as exc:
            _log_error(exc)
            raise typer.Exit(getattr(exc, "exit_code", 1)) from exc
        except (FileNotFoundError, ValueError, ModuleNotFoundError, AttributeError) as exc:
            _log_error(exc)
            raise typer.Exit(1) from exc
        except Exception as exc:  # noqa: BLE001
            _log_error(exc)
            raise typer.Exit(1) from exc

    return wrapped
