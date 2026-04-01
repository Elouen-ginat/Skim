"""ShadowBackend — routes reads/writes based on migration stage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from skaal.backends.base import StorageBackend


@dataclass
class DiscrepancyRecord:
    key: str
    source_value: Any
    target_value: Any
    timestamp: str  # ISO format


class ShadowBackend:
    """
    Routes reads/writes to source and/or target backends based on migration stage.

    Stage 1 (shadow_write):
      - Reads: source only
      - Writes: BOTH source and target

    Stage 2 (shadow_read):
      - Reads: source (authoritative), also reads target and logs discrepancies
      - Writes: BOTH

    Stage 3 (dual_read):
      - Reads: target first; fall back to source if key missing
      - Writes: BOTH

    Stage 4 (new_primary):
      - Reads: target only
      - Writes: target only (source gets no new writes)

    Stage 5 (cleanup):
      - Reads: target only
      - Writes: target only
      - Note: caller should drain source at this stage
    """

    def __init__(
        self,
        source: StorageBackend,
        target: StorageBackend,
        stage: int,
    ) -> None:
        self.source = source
        self.target = target
        self.stage = stage
        self.discrepancies: list[DiscrepancyRecord] = []

    async def get(self, key: str) -> Any | None:
        if self.stage == 1:
            return await self.source.get(key)

        elif self.stage == 2:
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

        elif self.stage == 3:
            target_val = await self.target.get(key)
            if target_val is not None:
                return target_val
            return await self.source.get(key)

        elif self.stage in (4, 5):
            return await self.target.get(key)

        else:
            raise ValueError(f"Invalid migration stage: {self.stage}")

    async def set(self, key: str, value: Any) -> None:
        if self.stage in (1, 2, 3):
            await self.source.set(key, value)
            await self.target.set(key, value)
        elif self.stage in (4, 5):
            await self.target.set(key, value)
        else:
            raise ValueError(f"Invalid migration stage: {self.stage}")

    async def delete(self, key: str) -> None:
        if self.stage in (1, 2, 3):
            await self.source.delete(key)
            await self.target.delete(key)
        elif self.stage in (4, 5):
            await self.target.delete(key)
        else:
            raise ValueError(f"Invalid migration stage: {self.stage}")

    async def list(self) -> list[tuple[str, Any]]:
        if self.stage in (1, 2):
            return await self.source.list()
        elif self.stage == 3:
            target_items = await self.target.list()
            if target_items:
                return target_items
            return await self.source.list()
        elif self.stage in (4, 5):
            return await self.target.list()
        else:
            raise ValueError(f"Invalid migration stage: {self.stage}")

    async def scan(self, prefix: str = "") -> list[tuple[str, Any]]:
        if self.stage in (1, 2):
            return await self.source.scan(prefix)
        elif self.stage == 3:
            target_items = await self.target.scan(prefix)
            if target_items:
                return target_items
            return await self.source.scan(prefix)
        elif self.stage in (4, 5):
            return await self.target.scan(prefix)
        else:
            raise ValueError(f"Invalid migration stage: {self.stage}")

    async def close(self) -> None:
        await self.source.close()
        await self.target.close()

    def __repr__(self) -> str:
        return (
            f"ShadowBackend(stage={self.stage}, "
            f"source={self.source!r}, target={self.target!r})"
        )
