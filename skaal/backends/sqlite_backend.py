"""Compatibility shim for the canonical SqliteBackend backend module."""

from __future__ import annotations

from skaal.backends.kv.sqlite import SqliteBackend

__all__ = ["SqliteBackend"]
