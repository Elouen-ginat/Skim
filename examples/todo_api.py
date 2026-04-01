"""
Todo API — a simple Skim app demonstrating persistent storage and CRUD functions.

Run locally:

    skaal run examples.todo_api:app

Then try:

    curl -s http://localhost:8000/ | jq
    curl -s http://localhost:8000/create_todo -d '{"id": "1", "title": "Buy milk"}' | jq
    curl -s http://localhost:8000/get_todo -d '{"id": "1"}' | jq
    curl -s http://localhost:8000/complete_todo -d '{"id": "1"}' | jq
    curl -s http://localhost:8000/list_todos | jq
    curl -s http://localhost:8000/delete_todo -d '{"id": "1"}' | jq
"""

from __future__ import annotations

from skaal import App

app = App("todos")


@app.storage(read_latency="< 10ms", durability="persistent", access_pattern="random-read")
class Todos:
    """Todo items. Solver picks DynamoDB on Lambda, Redis elsewhere."""


@app.function()
async def create_todo(id: str, title: str, done: bool = False) -> dict:
    """Create a new todo item."""
    await Todos.set(id, {"id": id, "title": title, "done": done})
    return {"ok": True, "id": id}


@app.function()
async def get_todo(id: str) -> dict:
    """Get a todo item by id."""
    item = await Todos.get(id)
    return item if item is not None else {"error": "not found"}


@app.function()
async def complete_todo(id: str) -> dict:
    """Mark a todo item as done."""
    item = await Todos.get(id)
    if item is None:
        return {"error": "not found"}
    item["done"] = True
    await Todos.set(id, item)
    return item


@app.function()
async def list_todos() -> dict:
    """Return all todo items."""
    entries = await Todos.list()
    return {"todos": [v for _, v in entries]}


@app.function()
async def delete_todo(id: str) -> dict:
    """Delete a todo item by id."""
    await Todos.delete(id)
    return {"ok": True, "deleted": id}
