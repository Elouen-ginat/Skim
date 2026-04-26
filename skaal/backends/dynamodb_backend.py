"""Compatibility shim for the canonical DynamoBackend backend module."""

from __future__ import annotations

from skaal.backends.kv.dynamodb import DynamoBackend

__all__ = ["DynamoBackend"]
