"""Storage kinds used by the deploy backend registry."""

from __future__ import annotations

from enum import Enum


class StorageKind(str, Enum):
    """The categories a backend can claim."""

    KV = "kv"
    RELATIONAL = "relational"
    VECTOR = "vector"
    BLOB = "blob"
    STREAM = "stream"

    @classmethod
    def parse(cls, value: str) -> "StorageKind":
        try:
            return cls(value)
        except ValueError as exc:
            known = ", ".join(k.value for k in cls)
            raise ValueError(f"unknown storage kind {value!r} (known: {known})") from exc
