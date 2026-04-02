"""
Counter — a simple Skim app demonstrating storage and functions.

Run locally:

    skaal run examples.counter:app

Then try:

    curl -s http://localhost:8000/ | jq
    curl -s http://localhost:8000/increment -d '{"name": "hits"}' | jq
    curl -s http://localhost:8000/increment -d '{"name": "hits", "by": 5}' | jq
    curl -s http://localhost:8000/get_count -d '{"name": "hits"}' | jq
    curl -s http://localhost:8000/list_counts | jq
    curl -s http://localhost:8000/reset -d '{"name": "hits"}' | jq
"""

from skaal import App

app = App("counter")


@app.storage(read_latency="< 5ms", durability="ephemeral")
class Counts:
    """Named integer counters. Backed by LocalMap in local mode."""


@app.function()
async def increment(name: str, by: int = 1) -> dict:
    """Increment counter ``name`` by ``by`` (default 1). Returns new value."""
    current = await Counts.get(name) or 0
    new_value = current + by
    await Counts.set(name, new_value)
    return {"name": name, "value": new_value}


@app.function()
async def get_count(name: str) -> dict:
    """Return the current value of counter ``name``."""
    value = await Counts.get(name) or 0
    return {"name": name, "value": value}


@app.function()
async def reset(name: str) -> dict:
    """Reset counter ``name`` to zero."""
    await Counts.delete(name)
    return {"name": name, "value": 0}


@app.function()
async def list_counts() -> dict:
    """Return all counters and their values."""
    entries = await Counts.list()
    return {"counts": dict(entries)}
