"""Skim storage backends — pluggable key-value stores."""

from __future__ import annotations

from skaal.backends.base import StorageBackend
from skaal.backends.dynamodb_backend import DynamoBackend
from skaal.backends.redis_backend import RedisBackend

__all__ = ["StorageBackend", "RedisBackend", "DynamoBackend"]
