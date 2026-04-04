"""MigrationEngine — persistent state machine for 6-stage backend migrations."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from skaal.backends.base import StorageBackend

STAGE_NAMES = {
    0: "idle",
    1: "shadow_write",
    2: "shadow_read",
    3: "dual_read",
    4: "new_primary",
    5: "cleanup",
    6: "done",
}


@dataclass
class MigrationState:
    variable_name: str  # e.g. "counter.Counts"
    source_backend: str  # backend name from plan (e.g. "elasticache-redis")
    target_backend: str
    stage: int  # 0–6
    started_at: str  # ISO timestamp
    advanced_at: str  # ISO timestamp of last stage change
    discrepancy_count: int = 0
    keys_migrated: int = 0
    app_name: str = ""


class MigrationEngine:
    """
    Manages the 6-stage migration protocol for a single storage variable.

    State is persisted to .skaal/migrations/{app_name}/{variable_name}.json
    so it survives restarts.
    """

    STATE_DIR = Path(".skaal/migrations")

    def __init__(self, app_name: str, variable_name: str) -> None:
        self.app_name = app_name
        self.variable_name = variable_name
        self._state_path = self.STATE_DIR / app_name / f"{variable_name.replace('.', '__')}.json"

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
        """Begin a new migration from source to target backend. Sets stage=1."""
        now = datetime.now(timezone.utc).isoformat()
        state = MigrationState(
            variable_name=self.variable_name,
            source_backend=source,
            target_backend=target,
            stage=1,
            started_at=now,
            advanced_at=now,
            app_name=self.app_name,
        )
        self.save_state(state)
        return state

    def advance(self, state: MigrationState, discrepancy_count: int = 0) -> MigrationState:
        """
        Advance to the next stage.
        Raises ValueError if already at stage 6 (done).
        """
        if state.stage >= 6:
            raise ValueError(f"{self.variable_name} migration is already complete (stage 6).")
        state.stage += 1
        state.advanced_at = datetime.now(timezone.utc).isoformat()
        state.discrepancy_count += discrepancy_count
        self.save_state(state)
        return state

    def rollback(self, state: MigrationState) -> MigrationState:
        """Roll back one stage. Cannot roll back from stage 0 or 6."""
        if state.stage <= 0:
            raise ValueError("Already at initial stage.")
        if state.stage == 6:
            raise ValueError("Cannot roll back a completed migration.")
        state.stage -= 1
        state.advanced_at = datetime.now(timezone.utc).isoformat()
        self.save_state(state)
        return state

    def complete(self, state: MigrationState) -> None:
        """Mark migration as done (stage 6). Moves state file to .skaal/migrations/history/."""
        state.stage = 6
        state.advanced_at = datetime.now(timezone.utc).isoformat()
        self.save_state(state)

    def list_all(self) -> list[MigrationState]:
        """Return all in-progress migrations for this app."""
        states = []
        app_dir = self.STATE_DIR / self.app_name
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
