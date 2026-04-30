"""CLI-facing type aliases."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeAlias

if TYPE_CHECKING:
    from watchfiles import Change

ReloadMode: TypeAlias = Literal["auto", "on", "off"]
"""User-facing reload selector. ``auto`` resolves at runtime via TTY/env."""

ChildArgv: TypeAlias = list[str]
"""Argv passed to the child Python process when supervising hot-reload."""

ReloadDirs: TypeAlias = list[Path]
"""Filesystem roots watched by the reload supervisor."""

ChangeBatch: TypeAlias = "set[tuple[Change, str]]"
"""One batch of filesystem changes as yielded by ``watchfiles.watch``."""

ChangeStream: TypeAlias = Iterator[ChangeBatch]
"""Iterator of change batches; the supervisor consumes one of these."""

__all__ = [
    "ChangeBatch",
    "ChangeStream",
    "ChildArgv",
    "ReloadDirs",
    "ReloadMode",
]
