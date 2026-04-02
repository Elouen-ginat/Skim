"""
Todo API — demonstrates typed Map storage with nested Pydantic models.

Run locally:

    skaal run examples.todo_api:app

With persistent SQLite:

    skaal run examples.todo_api:app --persist

Deploy to AWS Lambda + DynamoDB:

    skaal plan --target aws-lambda examples.todo_api:app
    skaal deploy --target aws examples.todo_api:app

Try it:

    curl -s localhost:8000/create_todo \\
      -d '{"id":"t1","title":"Buy groceries","tags":["home","errands"]}' | jq

    curl -s localhost:8000/complete_todo -d '{"id":"t1"}' | jq
    curl -s localhost:8000/list_todos | jq
    curl -s localhost:8000/get_todo -d '{"id":"t1"}' | jq
    curl -s localhost:8000/delete_todo -d '{"id":"t1"}' | jq
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from skaal import App, Map


# ── Domain models ──────────────────────────────────────────────────────────────

class Attachment(BaseModel):
    url: str
    name: str
    mime_type: str = "application/octet-stream"


class Todo(BaseModel):
    id: str
    title: str
    done: bool = False
    tags: list[str] = []
    attachments: list[Attachment] = []   # nested model inside a list
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    completed_at: str | None = None


# ── App declaration ────────────────────────────────────────────────────────────

app = App("todos")


@app.storage(
    read_latency="< 10ms",
    durability="persistent",
    access_pattern="random-read",
)
class Todos(Map[str, Todo]):
    """
    Persistent todo items.

    Solver selects:
      - DynamoDB on aws-lambda target (serverless, no VPC)
      - ElastiCache Redis on generic target (< 10ms, persistent)
      - SQLite locally with --persist flag
    """


# ── Functions ──────────────────────────────────────────────────────────────────

@app.function()
async def create_todo(
    id: str,
    title: str,
    tags: list[str] | None = None,
    attachments: list[dict] | None = None,
) -> dict:
    """Create a new todo. Returns error if id already exists."""
    if await Todos.get(id) is not None:
        return {"error": f"Todo {id!r} already exists"}
    todo = Todo(
        id=id,
        title=title,
        tags=tags or [],
        attachments=[Attachment(**a) for a in (attachments or [])],
    )
    await Todos.set(id, todo)
    return todo.model_dump()


@app.function()
async def get_todo(id: str) -> dict:
    """Fetch a single todo by id."""
    todo = await Todos.get(id)
    return todo.model_dump() if todo else {"error": f"Todo {id!r} not found"}


@app.function()
async def complete_todo(id: str) -> dict:
    """Mark a todo as done."""
    todo = await Todos.get(id)
    if todo is None:
        return {"error": f"Todo {id!r} not found"}
    todo.done = True
    todo.completed_at = datetime.now(timezone.utc).isoformat()
    await Todos.set(id, todo)
    return todo.model_dump()


@app.function()
async def add_attachment(id: str, url: str, name: str, mime_type: str = "application/octet-stream") -> dict:
    """Attach a file to a todo. Demonstrates nested model mutation."""
    todo = await Todos.get(id)
    if todo is None:
        return {"error": f"Todo {id!r} not found"}
    todo.attachments.append(Attachment(url=url, name=name, mime_type=mime_type))
    await Todos.set(id, todo)
    return todo.model_dump()


@app.function()
async def list_todos(done: bool | None = None) -> dict:
    """List all todos, optionally filtered by done status."""
    entries = await Todos.list()
    todos = [v for _, v in entries]
    if done is not None:
        todos = [t for t in todos if t.done == done]
    return {"todos": [t.model_dump() for t in todos], "count": len(todos)}


@app.function()
async def delete_todo(id: str) -> dict:
    """Delete a todo by id."""
    await Todos.delete(id)
    return {"ok": True, "deleted": id}
