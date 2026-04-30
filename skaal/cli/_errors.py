from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import typer

from skaal.errors import SkaalError

P = ParamSpec("P")
R = TypeVar("R")

_LOG = logging.getLogger("skaal.cli")


def _log_error(exc: BaseException) -> None:
    _LOG.error(str(exc), exc_info=_LOG.isEnabledFor(logging.DEBUG))


def cli_error_boundary(func: Callable[P, R]) -> Callable[P, R]:
    @wraps(func)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except typer.Exit:
            raise
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
