"""Tests for the 6-stage migration engine and ShadowBackend."""

from __future__ import annotations

import json

import pytest

from skaal.backends.local_backend import LocalMap
from skaal.migrate.engine import MigrationEngine, MigrationState, copy_all
from skaal.migrate.shadow import DiscrepancyRecord, ShadowBackend


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_backends():
    """Return a fresh (source, target) pair of LocalMap backends."""
    return LocalMap(), LocalMap()


# ── ShadowBackend — Stage 1 (shadow_write) ────────────────────────────────────

@pytest.mark.asyncio
async def test_stage1_writes_to_both():
    source, target = _make_backends()
    shadow = ShadowBackend(source, target, stage=1)

    await shadow.set("key", "value")

    assert await source.get("key") == "value"
    assert await target.get("key") == "value"


@pytest.mark.asyncio
async def test_stage1_reads_from_source():
    source, target = _make_backends()
    await source.set("key", "from_source")
    await target.set("key", "from_target")

    shadow = ShadowBackend(source, target, stage=1)
    result = await shadow.get("key")
    assert result == "from_source"


@pytest.mark.asyncio
async def test_stage1_delete_both():
    source, target = _make_backends()
    await source.set("k", 1)
    await target.set("k", 1)
    shadow = ShadowBackend(source, target, stage=1)

    await shadow.delete("k")
    assert await source.get("k") is None
    assert await target.get("k") is None


# ── ShadowBackend — Stage 2 (shadow_read) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_stage2_discrepancy_detected():
    source, target = _make_backends()
    await source.set("key", "old_value")
    await target.set("key", "new_value")

    shadow = ShadowBackend(source, target, stage=2)
    result = await shadow.get("key")

    # Returns source value
    assert result == "old_value"
    # But records the discrepancy
    assert len(shadow.discrepancies) == 1
    d = shadow.discrepancies[0]
    assert d.key == "key"
    assert d.source_value == "old_value"
    assert d.target_value == "new_value"


@pytest.mark.asyncio
async def test_stage2_no_discrepancy_when_equal():
    source, target = _make_backends()
    await source.set("key", "same")
    await target.set("key", "same")

    shadow = ShadowBackend(source, target, stage=2)
    await shadow.get("key")

    assert len(shadow.discrepancies) == 0


@pytest.mark.asyncio
async def test_stage2_writes_to_both():
    source, target = _make_backends()
    shadow = ShadowBackend(source, target, stage=2)

    await shadow.set("x", 99)
    assert await source.get("x") == 99
    assert await target.get("x") == 99


# ── ShadowBackend — Stage 3 (dual_read) ───────────────────────────────────────

@pytest.mark.asyncio
async def test_stage3_reads_target_first():
    source, target = _make_backends()
    await source.set("key", "source_val")
    await target.set("key", "target_val")

    shadow = ShadowBackend(source, target, stage=3)
    assert await shadow.get("key") == "target_val"


@pytest.mark.asyncio
async def test_stage3_falls_back_to_source():
    source, target = _make_backends()
    await source.set("key", "source_only")
    # target does NOT have this key

    shadow = ShadowBackend(source, target, stage=3)
    assert await shadow.get("key") == "source_only"


@pytest.mark.asyncio
async def test_stage3_writes_to_both():
    source, target = _make_backends()
    shadow = ShadowBackend(source, target, stage=3)
    await shadow.set("z", "dual")
    assert await source.get("z") == "dual"
    assert await target.get("z") == "dual"


# ── ShadowBackend — Stage 4 (new_primary) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_stage4_reads_target_only():
    source, target = _make_backends()
    await source.set("key", "source_val")
    await target.set("key", "target_val")

    shadow = ShadowBackend(source, target, stage=4)
    assert await shadow.get("key") == "target_val"


@pytest.mark.asyncio
async def test_stage4_writes_target_only():
    source, target = _make_backends()
    shadow = ShadowBackend(source, target, stage=4)
    await shadow.set("new", "data")

    assert await target.get("new") == "data"
    assert await source.get("new") is None


@pytest.mark.asyncio
async def test_stage4_delete_target_only():
    source, target = _make_backends()
    await source.set("del", "x")
    await target.set("del", "x")

    shadow = ShadowBackend(source, target, stage=4)
    await shadow.delete("del")

    assert await target.get("del") is None
    assert await source.get("del") == "x"  # source unchanged


# ── ShadowBackend — Stage 5 (cleanup) ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_stage5_reads_target_only():
    source, target = _make_backends()
    await source.set("k", "source")
    await target.set("k", "target")

    shadow = ShadowBackend(source, target, stage=5)
    assert await shadow.get("k") == "target"


@pytest.mark.asyncio
async def test_stage5_writes_target_only():
    source, target = _make_backends()
    shadow = ShadowBackend(source, target, stage=5)
    await shadow.set("s5", "only_target")

    assert await target.get("s5") == "only_target"
    assert await source.get("s5") is None


# ── ShadowBackend — list/scan routing ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_shadow_list_stage1():
    source, target = _make_backends()
    await source.set("a", 1)
    await source.set("b", 2)
    await target.set("a", 1)

    shadow = ShadowBackend(source, target, stage=1)
    items = await shadow.list()
    assert len(items) == 2  # from source


@pytest.mark.asyncio
async def test_shadow_close_closes_both():
    source, target = _make_backends()
    shadow = ShadowBackend(source, target, stage=1)
    # close() should not raise — LocalMap has a no-op close
    await shadow.close()


# ── MigrationEngine ────────────────────────────────────────────────────────────

def test_engine_start_creates_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    engine = MigrationEngine("myapp", "counter.Counts")
    state = engine.start("redis", "dynamodb")

    assert state.stage == 1
    assert state.source_backend == "redis"
    assert state.target_backend == "dynamodb"
    assert state.app_name == "myapp"
    assert engine._state_path.exists()


def test_engine_load_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    engine = MigrationEngine("myapp", "counter.Counts")
    assert engine.load_state() is None  # no state yet

    engine.start("redis", "dynamodb")
    state = engine.load_state()
    assert state is not None
    assert state.stage == 1


def test_engine_advance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    engine = MigrationEngine("myapp", "var")
    state = engine.start("a", "b")
    assert state.stage == 1

    state = engine.advance(state)
    assert state.stage == 2

    state = engine.advance(state)
    assert state.stage == 3

    # Reload from disk to verify persistence
    reloaded = engine.load_state()
    assert reloaded.stage == 3


def test_engine_advance_at_stage6_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    engine = MigrationEngine("myapp", "var")
    state = engine.start("a", "b")
    # Manually advance to stage 6
    state.stage = 6
    with pytest.raises(ValueError, match="already complete"):
        engine.advance(state)


def test_engine_rollback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    engine = MigrationEngine("myapp", "var")
    state = engine.start("a", "b")
    state = engine.advance(state)  # stage 2
    assert state.stage == 2

    state = engine.rollback(state)
    assert state.stage == 1

    # Verify on disk
    reloaded = engine.load_state()
    assert reloaded.stage == 1


def test_engine_rollback_at_stage0_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    engine = MigrationEngine("myapp", "var")
    state = engine.start("a", "b")
    state.stage = 0
    with pytest.raises(ValueError, match="initial stage"):
        engine.rollback(state)


def test_engine_rollback_at_stage6_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    engine = MigrationEngine("myapp", "var")
    state = engine.start("a", "b")
    state.stage = 6
    with pytest.raises(ValueError, match="completed"):
        engine.rollback(state)


def test_engine_complete(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    engine = MigrationEngine("myapp", "var")
    state = engine.start("a", "b")
    engine.complete(state)

    reloaded = engine.load_state()
    assert reloaded.stage == 6


def test_engine_list_all(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    e1 = MigrationEngine("myapp", "var1")
    e1.start("a", "b")

    e2 = MigrationEngine("myapp", "var2")
    e2.start("c", "d")

    # Use any engine with the same app_name to list all
    engine = MigrationEngine("myapp", "__probe__")
    states = engine.list_all()
    assert len(states) == 2
    var_names = {s.variable_name for s in states}
    assert var_names == {"var1", "var2"}


# ── copy_all ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_copy_all():
    source = LocalMap()
    target = LocalMap()

    await source.set("a", 1)
    await source.set("b", "two")
    await source.set("c", {"x": 3})

    count = await copy_all(source, target)
    assert count == 3

    assert await target.get("a") == 1
    assert await target.get("b") == "two"
    assert await target.get("c") == {"x": 3}


@pytest.mark.asyncio
async def test_copy_all_empty():
    source = LocalMap()
    target = LocalMap()
    count = await copy_all(source, target)
    assert count == 0
