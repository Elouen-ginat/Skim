"""MigrationEngine — persistent state machine for 6-stage backend migrations."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from skaal.backends.base import StorageBackend


_BASE_STATE_DIR = Path(".skaal/migrations")


class MigrationKind(StrEnum):
    """The kind of migration ``skaal migrate`` is operating on.

    ``DATA`` is the existing 6-stage backend swap (KV data movement).
    ``RELATIONAL`` is Alembic-driven DDL versioning of SQLModel tables.
    """

    DATA = "data"
    RELATIONAL = "relational"


def state_dir(app_name: str, kind: MigrationKind) -> Path:
    """Return the on-disk root for *kind* of migration state for *app_name*."""
    return _BASE_STATE_DIR / app_name / kind.value


class MigrationStage(IntEnum):
    """The seven stages of a backend migration."""

    IDLE = 0
    SHADOW_WRITE = 1  # Writes go to both; reads from source only.
    SHADOW_READ = 2  # Reads from source (authoritative) + target (discrepancy log).
    DUAL_READ = 3  # Reads from target first; fall back to source if missing.
    NEW_PRIMARY = 4  # Reads and writes to target only; source receives nothing.
    CLEANUP = 5  # Drain source; target is sole owner.
    DONE = 6  # Migration complete.


@dataclass
class MigrationState:
    variable_name: str  # e.g. "counter.Counts"
    source_backend: str  # backend name from plan (e.g. "elasticache-redis")
    target_backend: str
    stage: MigrationStage
    started_at: str  # ISO timestamp
    advanced_at: str  # ISO timestamp of last stage change
    discrepancy_count: int = 0
    keys_migrated: int = 0
    app_name: str = ""

    def __post_init__(self) -> None:
        # Coerce int loaded from JSON back to the enum.
        if not isinstance(self.stage, MigrationStage):
            self.stage = MigrationStage(self.stage)


class MigrationEngine:
    """
    Manages the 6-stage migration protocol for a single storage variable.

    State is persisted to .skaal/migrations/{app_name}/data/{variable_name}.json
    so it survives restarts.
    """

    def __init__(self, app_name: str, variable_name: str) -> None:
        self.app_name = app_name
        self.variable_name = variable_name
        self._state_path = (
            state_dir(app_name, MigrationKind.DATA) / f"{variable_name.replace('.', '__')}.json"
        )

    def load_state(self) -> MigrationState | None:
        """Load migration state from disk. Returns None if no migration in progress."""
        if not self._state_path.exists():
            return None
        data = json.loads(self._state_path.read_text())
        return MigrationState(**data)

    def save_state(self, state: MigrationState) -> None:
        """Persist state to .skaal/migrations/..."""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(dataclasses.asdict(state), indent=2))

    def start(self, source: str, target: str) -> MigrationState:
        """Begin a new migration from source to target backend. Sets stage=SHADOW_WRITE."""
        now = datetime.now(timezone.utc).isoformat()
        state = MigrationState(
            variable_name=self.variable_name,
            source_backend=source,
            target_backend=target,
            stage=MigrationStage.SHADOW_WRITE,
            started_at=now,
            advanced_at=now,
            app_name=self.app_name,
        )
        self.save_state(state)
        return state

    def advance(
        self,
        state: MigrationState,
        discrepancy_count: int = 0,
        keys_migrated: int = 0,
    ) -> MigrationState:
        """
        Advance to the next stage.
        Raises ValueError if already at DONE.

        Args:
            discrepancy_count: Number of read discrepancies detected this stage
                               (relevant at SHADOW_READ).
            keys_migrated:     Number of keys bulk-copied this stage (returned
                               by :func:`copy_all` at stage transition).
        """
        if state.stage == MigrationStage.DONE:
            raise ValueError(f"{self.variable_name} migration is already complete (DONE).")
        state.stage = MigrationStage(state.stage + 1)
        state.advanced_at = datetime.now(timezone.utc).isoformat()
        state.discrepancy_count += discrepancy_count
        state.keys_migrated += keys_migrated
        self.save_state(state)
        return state

    def rollback(self, state: MigrationState) -> MigrationState:
        """Roll back one stage. Cannot roll back from IDLE or DONE."""
        if state.stage == MigrationStage.IDLE:
            raise ValueError("Already at initial stage.")
        if state.stage == MigrationStage.DONE:
            raise ValueError("Cannot roll back a completed migration.")
        state.stage = MigrationStage(state.stage - 1)
        state.advanced_at = datetime.now(timezone.utc).isoformat()
        self.save_state(state)
        return state

    def complete(self, state: MigrationState) -> None:
        """Mark migration as done (DONE). Moves state file to .skaal/migrations/history/."""
        state.stage = MigrationStage.DONE
        state.advanced_at = datetime.now(timezone.utc).isoformat()
        self.save_state(state)

    def list_all(self) -> list[MigrationState]:
        """Return all in-progress migrations for this app."""
        states = []
        app_dir = state_dir(self.app_name, MigrationKind.DATA)
        if not app_dir.exists():
            return []
        for path in app_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                states.append(MigrationState(**data))
            except Exception:  # noqa: BLE001
                pass
        return states


async def copy_all(source: StorageBackend, target: StorageBackend) -> int:
    """
    Copy all key-value pairs from source to target.

    Returns the number of keys copied. Used at stage transition to
    pre-populate the target backend.
    """
    items = await source.list()
    for key, value in items:
        await target.set(key, value)
    return len(items)
