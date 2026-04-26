"""Compatibility shim for the canonical PgVectorBackend backend module."""

from __future__ import annotations

from skaal.backends.vector.pgvector import PgVectorBackend

__all__ = ["PgVectorBackend"]
