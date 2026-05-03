from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    items: list[T]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True)
class SecondaryIndex:
    name: str
    partition_key: str
    sort_key: str | None = None
    unique: bool = False
