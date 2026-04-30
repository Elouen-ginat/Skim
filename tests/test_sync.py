from __future__ import annotations

import asyncio

import pytest

from skaal import sync_run


def test_sync_run_without_event_loop() -> None:
    assert sync_run(asyncio.sleep(0, result=42)) == 42


@pytest.mark.asyncio
async def test_sync_run_inside_event_loop() -> None:
    assert sync_run(asyncio.sleep(0, result="ok")) == "ok"
