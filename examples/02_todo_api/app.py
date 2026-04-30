"""
Todo API — FastAPI mounted over Skaal compute, KV, relational, and vector storage.

For semantic search, install the vector extra first:

    pip install "skaal[vector]"

Run locally:

    pip install "skaal[examples,vector]"
    skaal run examples.todo_api:app

With persistent SQLite:

    skaal run examples.todo_api:app --persist

Deploy to AWS Lambda + DynamoDB:

    skaal plan --target aws-lambda examples.todo_api:app
    skaal deploy --target aws examples.todo_api:app

Try it:

    curl -s localhost:8000/todos \
        -X POST \
        -H "Content-Type: application/json" \
        -d '{"id":"t1","title":"Buy groceries","description":"Milk eggs bread","tags":["home","errands"]}' | jq

    curl -s localhost:8000/todos | jq
    curl -s localhost:8000/todos/t1 | jq
    curl -s localhost:8000/todos/t1/comments \
        -X POST \
        -H "Content-Type: application/json" \
        -d '{"author":"alex","body":"Remember oat milk too"}' | jq
    curl -s 'localhost:8000/todos/search?q=grocery%20list' | jq
    curl -s localhost:8000/todos/t1 -X DELETE | jq
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel
from pydantic import Field as PydanticField
from sqlmodel import Field, SQLModel, select

from skaal import App, Store, VectorStore, open_relational_session

# ── Domain models ──────────────────────────────────────────────────────────────


class Attachment(BaseModel):
    url: str
    name: str
    mime_type: str = "application/octet-stream"


class Todo(BaseModel):
    id: str
    title: str
    description: str = ""
    done: bool = False
    tags: list[str] = PydanticField(default_factory=list)
    attachments: list[Attachment] = PydanticField(default_factory=list)
    created_at: str = PydanticField(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str | None = None


class TodoSearchDocument(BaseModel):
    id: str
    title: str
    content: str
    done: bool = False


Todo.model_rebuild()


class CreateTodoRequest(BaseModel):
    id: str
    title: str
    description: str = ""
    tags: list[str] = PydanticField(default_factory=list)
    attachments: list[Attachment] = PydanticField(default_factory=list)


class CommentRequest(BaseModel):
    author: str
    body: str


# ── App declaration ────────────────────────────────────────────────────────────

app = App("todos")
api = FastAPI(title="Skaal Todo API")


@app.storage(
    read_latency="< 10ms",
    durability="persistent",
    access_pattern="random-read",
)
class Todos(Store[Todo]):
    """
    Persistent todo items.

    Solver selects:
      - DynamoDB on aws-lambda target (serverless, no VPC)
      - ElastiCache Redis on generic target (< 10ms, persistent)
      - SQLite locally with --persist flag
    """


@app.relational(read_latency="< 20ms", durability="persistent")
class Comments(SQLModel, table=True):
    """Structured todo comments stored in the relational tier."""

    __tablename__ = "todo_comments"

    id: int | None = Field(default=None, primary_key=True)
    todo_id: str = Field(index=True)
    author: str
    body: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@app.vector(
    dim=64,
    metric="cosine",
    read_latency="< 30ms",
    durability="persistent",
)
class TodoSearchIndex(VectorStore[TodoSearchDocument]):
    """Semantic search index over todo titles, descriptions, tags, and attachments."""

    __skaal_vector_text_fields__ = ("title", "content")


def _todo_to_search_doc(todo: Todo) -> TodoSearchDocument:
    attachment_names = " ".join(attachment.name for attachment in todo.attachments)
    content_parts = [
        todo.description,
        " ".join(todo.tags),
        attachment_names,
        "done" if todo.done else "open",
    ]
    return TodoSearchDocument(
        id=todo.id,
        title=todo.title,
        content="\n".join(part for part in content_parts if part),
        done=todo.done,
    )


async def _sync_todo_search(todo: Todo) -> None:
    await TodoSearchIndex.delete([todo.id])
    await TodoSearchIndex.add([_todo_to_search_doc(todo)])


async def _comment_rows(todo_id: str) -> list[Comments]:
    async with open_relational_session(Comments) as session:
        result = await session.exec(
            select(Comments).where(Comments.todo_id == todo_id).order_by(Comments.id)
        )
        return list(result.all())


# ── Functions ──────────────────────────────────────────────────────────────────


@app.function()
async def create_todo(
    id: str,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    attachments: list[dict] | None = None,
) -> dict:
    """Create a new todo. Returns error if id already exists."""
    if await Todos.get(id) is not None:
        return {"error": f"Todo {id!r} already exists"}
    todo = Todo(
        id=id,
        title=title,
        description=description,
        tags=tags or [],
        attachments=[Attachment(**a) for a in (attachments or [])],
    )
    await Todos.set(id, todo)
    await _sync_todo_search(todo)
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
    await _sync_todo_search(todo)
    return todo.model_dump()


@app.function()
async def add_attachment(
    id: str, url: str, name: str, mime_type: str = "application/octet-stream"
) -> dict:
    """Attach a file to a todo. Demonstrates nested model mutation."""
    todo = await Todos.get(id)
    if todo is None:
        return {"error": f"Todo {id!r} not found"}
    todo.attachments.append(Attachment(url=url, name=name, mime_type=mime_type))
    await Todos.set(id, todo)
    await _sync_todo_search(todo)
    return todo.model_dump()


@app.function()
async def add_comment(todo_id: str, author: str, body: str) -> dict:
    """Insert a structured comment for a todo using relational storage."""
    if await Todos.get(todo_id) is None:
        return {"error": f"Todo {todo_id!r} not found"}

    async with open_relational_session(Comments) as session:
        comment = Comments(todo_id=todo_id, author=author, body=body)
        session.add(comment)
        await session.commit()
        await session.refresh(comment)
    return comment.model_dump()


@app.function()
async def list_comments(todo_id: str) -> dict:
    """List structured comments for a todo from the relational store."""
    comments = await _comment_rows(todo_id)
    return {"comments": [comment.model_dump() for comment in comments], "count": len(comments)}


@app.function()
async def search_todos(query: str, k: int = 3, done: bool | None = None) -> dict:
    """Semantic todo lookup backed by the vector store."""
    results = await TodoSearchIndex.similarity_search(
        query,
        k=k,
        filter={"done": done} if done is not None else None,
    )
    todos: list[dict] = []
    seen: set[str] = set()
    for result in results:
        if result.id in seen:
            continue
        todo = await Todos.get(result.id)
        if todo is None:
            continue
        seen.add(result.id)
        todos.append(todo.model_dump())
    return {"todos": todos, "count": len(todos)}


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
    await TodoSearchIndex.delete([id])
    return {"ok": True, "deleted": id}


def _raise_for_error(result: dict, *, not_found: bool = False, conflict: bool = False) -> dict:
    if "error" not in result:
        return result
    if conflict:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result["error"])
    if not_found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=result["error"])
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result["error"])


@api.get("/todos")
async def http_list_todos(done: bool | None = Query(default=None)) -> dict:
    return await app.invoke(list_todos, done=done)


@api.get("/todos/{todo_id}")
async def http_get_todo(todo_id: str) -> dict:
    result = await app.invoke(get_todo, id=todo_id)
    return _raise_for_error(result, not_found=True)


@api.post("/todos", status_code=status.HTTP_201_CREATED)
async def http_create_todo(payload: CreateTodoRequest) -> dict:
    result = await app.invoke(
        create_todo,
        **payload.model_dump(mode="json"),
    )
    return _raise_for_error(result, conflict=True)


@api.delete("/todos/{todo_id}")
async def http_delete_todo(todo_id: str) -> dict:
    return await app.invoke(delete_todo, id=todo_id)


@api.post("/todos/{todo_id}/comments", status_code=status.HTTP_201_CREATED)
async def http_add_comment(todo_id: str, payload: CommentRequest) -> dict:
    result = await app.invoke(add_comment, todo_id=todo_id, **payload.model_dump())
    return _raise_for_error(result, not_found=True)


@api.get("/todos/{todo_id}/comments")
async def http_list_comments(todo_id: str) -> dict:
    return await app.invoke(list_comments, todo_id=todo_id)


@api.get("/todos/search")
async def http_search_todos(q: str, k: int = 3, done: bool | None = Query(default=None)) -> dict:
    return await app.invoke(search_todos, query=q, k=k, done=done)


app.mount_asgi(api, attribute="api")
