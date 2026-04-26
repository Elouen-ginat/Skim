"""Compatibility shim for the canonical PostgresBackend backend module."""

from __future__ import annotations

from skaal.backends.kv.postgres import PostgresBackend

__all__ = ["PostgresBackend"]
