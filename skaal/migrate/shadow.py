"""ShadowBackend — routes reads/writes based on migration stage."""

from __future__ import annotations

import builtins
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from skaal.backends.base import StorageBackend
from skaal.migrate.engine import MigrationStage


@dataclass
class DiscrepancyRecord:
    key: str
    source_value: Any
    target_value: Any
    timestamp: str  # ISO format


class ShadowBackend:
    """
    Routes reads/writes to source and/or target backends based on migration stage.

    SHADOW_WRITE (1):
      - Reads: source only
      - Writes: BOTH source and target

    SHADOW_READ (2):
      - Reads: source (authoritative), also reads target and logs discrepancies
      - Writes: BOTH

    DUAL_READ (3):
      - Reads: target first; fall back to source if key missing
      - Writes: BOTH

    NEW_PRIMARY (4):
      - Reads: target only
      - Writes: target only (source gets no new writes)

    CLEANUP (5):
      - Reads: target only
      - Writes: target only
      - Note: caller should drain source at this stage
    """

    def __init__(
        self,
        source: StorageBackend,
        target: StorageBackend,
        stage: MigrationStage,
    ) -> None:
        self.source = source
        self.target = target
        self.stage = MigrationStage(stage)
        self.discrepancies: list[DiscrepancyRecord] = []

    async def get(self, key: str) -> Any | None:
        if self.stage == MigrationStage.SHADOW_WRITE:
            return await self.source.get(key)

        elif self.stage == MigrationStage.SHADOW_READ:
            source_val = await self.source.get(key)
            target_val = await self.target.get(key)
            if source_val != target_val:
                self.discrepancies.append(
                    DiscrepancyRecord(
                        key=key,
                        source_value=source_val,
                        target_value=target_val,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )
                )
            return source_val

        elif self.stage == MigrationStage.DUAL_READ:
            target_val = await self.target.get(key)
            if target_val is not None:
                return target_val
            return await self.source.get(key)

        elif self.stage in (MigrationStage.NEW_PRIMARY, MigrationStage.CLEANUP):
            return await self.target.get(key)

        else:
            raise ValueError(f"Invalid migration stage: {self.stage}")

    async def set(self, key: str, value: Any) -> None:
        if self.stage in (
            MigrationStage.SHADOW_WRITE,
            MigrationStage.SHADOW_READ,
            MigrationStage.DUAL_READ,
        ):
            await self.source.set(key, value)
            await self.target.set(key, value)
        elif self.stage in (MigrationStage.NEW_PRIMARY, MigrationStage.CLEANUP):
            await self.target.set(key, value)
        else:
            raise ValueError(f"Invalid migration stage: {self.stage}")

    async def delete(self, key: str) -> None:
        if self.stage in (
            MigrationStage.SHADOW_WRITE,
            MigrationStage.SHADOW_READ,
            MigrationStage.DUAL_READ,
        ):
            await self.source.delete(key)
            await self.target.delete(key)
        elif self.stage in (MigrationStage.NEW_PRIMARY, MigrationStage.CLEANUP):
            await self.target.delete(key)
        else:
            raise ValueError(f"Invalid migration stage: {self.stage}")

    async def list(self) -> builtins.list[tuple[str, Any]]:
        if self.stage in (MigrationStage.SHADOW_WRITE, MigrationStage.SHADOW_READ):
            return await self.source.list()
        elif self.stage == MigrationStage.DUAL_READ:
            target_items = await self.target.list()
            if target_items:
                return target_items
            return await self.source.list()
        elif self.stage in (MigrationStage.NEW_PRIMARY, MigrationStage.CLEANUP):
            return await self.target.list()
        else:
            raise ValueError(f"Invalid migration stage: {self.stage}")

    async def scan(self, prefix: str = "") -> builtins.list[tuple[str, Any]]:
        if self.stage in (MigrationStage.SHADOW_WRITE, MigrationStage.SHADOW_READ):
            return await self.source.scan(prefix)
        elif self.stage == MigrationStage.DUAL_READ:
            target_items = await self.target.scan(prefix)
            if target_items:
                return target_items
            return await self.source.scan(prefix)
        elif self.stage in (MigrationStage.NEW_PRIMARY, MigrationStage.CLEANUP):
            return await self.target.scan(prefix)
        else:
            raise ValueError(f"Invalid migration stage: {self.stage}")

    async def increment_counter(self, key: str, delta: int = 1) -> int:
        """Atomically increment a counter, routing to the active backend(s).

        Stages SHADOW_WRITE–DUAL_READ: increment source (authoritative) and mirror to target.
        Stages NEW_PRIMARY–CLEANUP: increment target only.
        """
        if self.stage in (
            MigrationStage.SHADOW_WRITE,
            MigrationStage.SHADOW_READ,
            MigrationStage.DUAL_READ,
        ):
            new_value = await self.source.increment_counter(key, delta)
            # Mirror to target so it stays in sync; the source value is canonical.
            await self.target.set(key, new_value)
            return new_value
        elif self.stage in (MigrationStage.NEW_PRIMARY, MigrationStage.CLEANUP):
            return await self.target.increment_counter(key, delta)
        else:
            raise ValueError(f"Invalid migration stage: {self.stage}")

    async def atomic_update(self, key: str, fn: Callable[[Any], Any]) -> Any:
        """Atomically read-modify-write, routing to the active backend(s).

        Stages SHADOW_WRITE–DUAL_READ: update source (authoritative) and mirror the result to target.
        Stages NEW_PRIMARY–CLEANUP: update target only.
        """
        if self.stage in (
            MigrationStage.SHADOW_WRITE,
            MigrationStage.SHADOW_READ,
            MigrationStage.DUAL_READ,
        ):
            result = await self.source.atomic_update(key, fn)
            await self.target.set(key, result)
            return result
        elif self.stage in (MigrationStage.NEW_PRIMARY, MigrationStage.CLEANUP):
            return await self.target.atomic_update(key, fn)
        else:
            raise ValueError(f"Invalid migration stage: {self.stage}")

    async def close(self) -> None:
        await self.source.close()
        await self.target.close()

    def __repr__(self) -> str:
        return (
            f"ShadowBackend(stage={self.stage.name}, "
            f"source={self.source!r}, target={self.target!r})"
        )
