"""Types describing relational schema migrations.

These shapes are returned by the :mod:`skaal.migrate.relational` runner and
are the public surface of ``skaal migrate relational ...`` results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal


class RelationalMigrationOp(StrEnum):
    """One DDL operation in a planned upgrade or downgrade."""

    CREATE_TABLE = "create_table"
    DROP_TABLE = "drop_table"
    ADD_COLUMN = "add_column"
    DROP_COLUMN = "drop_column"
    ALTER_COLUMN = "alter_column"
    CREATE_INDEX = "create_index"
    DROP_INDEX = "drop_index"
    OTHER = "other"


@dataclass(frozen=True)
class RelationalMigrationStep:
    """One row in a dry-run plan."""

    op: RelationalMigrationOp
    table: str | None
    detail: str
    sql: str


@dataclass(frozen=True)
class RelationalRevision:
    """One Alembic revision present in ``versions/``."""

    revision_id: str
    down_revision: str | None
    message: str
    created_at: datetime
    is_head: bool
    is_applied: bool


@dataclass(frozen=True)
class RelationalMigrationPlan:
    """Output of ``upgrade --dry-run`` / ``downgrade --dry-run`` / ``check``."""

    backend_name: str
    direction: Literal["upgrade", "downgrade"]
    from_revision: str | None
    to_revision: str
    steps: list[RelationalMigrationStep] = field(default_factory=list)
    is_empty: bool = False


@dataclass(frozen=True)
class RelationalMigrationStatus:
    """Output of ``current`` and post-execution result of ``upgrade``/``downgrade``."""

    backend_name: str
    current_revision: str | None
    head_revision: str | None
    pending: list[RelationalRevision] = field(default_factory=list)
    applied: list[RelationalRevision] = field(default_factory=list)

    @property
    def is_at_head(self) -> bool:
        return self.current_revision == self.head_revision and self.head_revision is not None


__all__ = [
    "RelationalMigrationOp",
    "RelationalMigrationPlan",
    "RelationalMigrationStatus",
    "RelationalMigrationStep",
    "RelationalRevision",
]
