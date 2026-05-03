"""Tests for the typed objects in :mod:`skaal.types.relational`."""

from __future__ import annotations

from datetime import datetime, timezone

from skaal.types.relational import (
    RelationalMigrationOp,
    RelationalMigrationPlan,
    RelationalMigrationStatus,
    RelationalMigrationStep,
    RelationalRevision,
)


def _make_revision(rev_id: str, *, applied: bool = False, head: bool = False) -> RelationalRevision:
    return RelationalRevision(
        revision_id=rev_id,
        down_revision=None,
        message="msg",
        created_at=datetime.now(timezone.utc),
        is_head=head,
        is_applied=applied,
    )


def test_op_enum_values_are_stable() -> None:
    assert RelationalMigrationOp.CREATE_TABLE.value == "create_table"
    assert RelationalMigrationOp.ALTER_COLUMN.value == "alter_column"


def test_status_is_at_head_true_when_current_matches_head() -> None:
    status = RelationalMigrationStatus(
        backend_name="sqlite",
        current_revision="abc",
        head_revision="abc",
    )
    assert status.is_at_head is True


def test_status_is_at_head_false_when_no_head() -> None:
    status = RelationalMigrationStatus(
        backend_name="sqlite",
        current_revision=None,
        head_revision=None,
    )
    assert status.is_at_head is False


def test_status_is_at_head_false_when_pending() -> None:
    status = RelationalMigrationStatus(
        backend_name="sqlite",
        current_revision="abc",
        head_revision="def",
        pending=[_make_revision("def", head=True)],
    )
    assert status.is_at_head is False


def test_plan_is_empty_default() -> None:
    plan = RelationalMigrationPlan(
        backend_name="sqlite",
        direction="upgrade",
        from_revision=None,
        to_revision="head",
    )
    assert plan.steps == []
    assert plan.is_empty is False


def test_step_is_frozen() -> None:
    step = RelationalMigrationStep(
        op=RelationalMigrationOp.CREATE_TABLE,
        table="users",
        detail="users",
        sql="CREATE TABLE users (id INTEGER);",
    )
    try:
        step.table = "x"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("RelationalMigrationStep should be frozen")
