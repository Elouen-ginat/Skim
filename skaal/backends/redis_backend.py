"""Compatibility shim for the canonical RedisBackend backend module."""

from __future__ import annotations

from skaal.backends.kv.redis import RedisBackend

__all__ = ["RedisBackend"]
