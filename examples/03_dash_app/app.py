"""Dash app that demonstrates Skaal's KV, relational, and vector storage tiers.

Demonstrates:
    - app.mount_wsgi() to register a WSGI app for deploy
    - Store[UserState] for scalable per-user session state
    - @app.storage(kind="relational", ...) for persistent note history
    - @app.storage(kind="vector", ...) for semantic search over those notes
    - Sessions.sync_update(...) in Dash's sync callbacks
    - open_relational_session(...) and VectorStore APIs wrapped with asyncio.run(...)

Run locally:

    pip install dash dash-bootstrap-components "skaal[vector]"
    python examples/03_dash_app/app.py

Deploy to GCP Cloud Run:

    skaal deploy examples.03_dash_app.app:skaal_app --target gcp

Deploy to AWS Lambda:

    skaal deploy examples.03_dash_app.app:skaal_app --target aws

Architecture:
  - Each browser tab gets a unique session_id stored in a dcc.Store component.
  - Short-lived UI state lives in Sessions.
  - Longer-lived notes are stored in SessionNotes (relational).
  - Semantic lookup runs against SessionNoteIndex (vector) with per-session filtering.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, cast

import dash
import dash_bootstrap_components as dbc  # type: ignore[import]
from dash import Input, Output, State, callback, dcc, html, no_update
from pydantic import BaseModel
from sqlalchemy import desc
from sqlmodel import Field, SQLModel, select

from skaal import App, Store, VectorStore, open_relational_session

# ── Domain model ──────────────────────────────────────────────────────────────


class UserState(BaseModel):
    session_id: str
    click_count: int = 0
    last_clicked: str = ""
    filter_value: str = "all"


class SessionNoteDocument(BaseModel):
    id: str
    session_id: str
    title: str
    content: str


# ── Skaal app — declares storage constraints ──────────────────────────────────

skaal_app = App("dash-demo")


@skaal_app.storage(
    read_latency="< 5ms",
    durability="ephemeral",  # session data; Redis/Memorystore preferred
    access_pattern="random-read",
)
class Sessions(Store[UserState]):
    """Per-user session state, keyed by session ID."""


@skaal_app.storage(kind="relational", read_latency="< 20ms", durability="persistent")
class SessionNotes(SQLModel, table=True):
    """Persistent note history for each Dash session."""

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)
    title: str
    body: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@skaal_app.storage(
    kind="vector",
    dim=64,
    metric="cosine",
    read_latency="< 30ms",
    durability="persistent",
)
class SessionNoteIndex(VectorStore[SessionNoteDocument]):
    """Semantic search index over note titles and bodies."""

    __skaal_vector_text_fields__ = ("title", "content")


def _run_async(coro: Any) -> Any:
    """Bridge Dash's sync callbacks to Skaal's async relational/vector APIs."""
    return asyncio.run(coro)


async def _persist_note(session_id: str, title: str, body: str) -> SessionNotes:
    async with open_relational_session(SessionNotes) as session:
        note = SessionNotes(session_id=session_id, title=title, body=body)
        session.add(note)
        await session.commit()
        await session.refresh(note)

    assert note.id is not None
    doc_id = f"note:{note.id}"
    await SessionNoteIndex.delete([doc_id])
    await SessionNoteIndex.add(
        [
            SessionNoteDocument(
                id=doc_id,
                session_id=session_id,
                title=title,
                content=body,
            )
        ]
    )
    return note


async def _load_notes(session_id: str, limit: int = 5) -> list[SessionNotes]:
    async with open_relational_session(SessionNotes) as session:
        notes_model = cast(Any, SessionNotes)
        result = await session.exec(
            select(SessionNotes)
            .where(notes_model.session_id == session_id)
            .order_by(desc(notes_model.id))
            .limit(limit)
        )
        return list(result.all())


async def _search_notes(session_id: str, query: str, limit: int = 3) -> list[SessionNoteDocument]:
    return await SessionNoteIndex.similarity_search(
        query,
        k=limit,
        filter={"session_id": session_id},
    )


def _render_note_list(notes: list[SessionNotes]) -> Any:
    if not notes:
        return html.Div("No notes saved yet.", className="text-muted")

    return dbc.ListGroup(
        [
            dbc.ListGroupItem(
                [
                    html.Div(note.title, className="fw-semibold"),
                    html.Small(note.created_at, className="text-muted d-block"),
                    html.Div(note.body, className="mt-2"),
                ]
            )
            for note in notes
        ],
        flush=True,
    )


def _render_search_results(results: list[SessionNoteDocument]) -> Any:
    if not results:
        return html.Div("No semantic matches yet.", className="text-muted")

    return dbc.ListGroup(
        [
            dbc.ListGroupItem(
                [
                    html.Div(result.title, className="fw-semibold"),
                    html.Div(result.content[:180], className="mt-2 text-muted"),
                ]
            )
            for result in results
        ],
        flush=True,
    )


# ── Dash layout and callbacks ─────────────────────────────────────────────────

dash_app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    suppress_callback_exceptions=True,
)

dash_app.layout = html.Div(
    dbc.Container(
        [
            dcc.Store(id="session-id", storage_type="session"),
            dcc.Interval(id="init", interval=1, n_intervals=0, max_intervals=1),
            html.H2("Skaal + Dash Storage Demo"),
            html.P(
                "KV session state, relational note history, and semantic search behind one Dash UI.",
                className="text-muted",
            ),
            dbc.Row(
                [
                    dbc.Col(
                        dbc.Card(
                            dbc.CardBody(
                                [
                                    html.H4("Session State", className="card-title"),
                                    html.P(
                                        "Short-lived click state lives in Sessions.",
                                        className="text-muted",
                                    ),
                                    html.Button(
                                        "Click me!",
                                        id="click-btn",
                                        n_clicks=0,
                                        className="btn btn-primary",
                                    ),
                                    html.Div(id="output", className="mt-3 fw-semibold"),
                                    html.Div(
                                        id="session-summary", className="mt-2 text-secondary small"
                                    ),
                                ]
                            )
                        ),
                        md=4,
                    ),
                    dbc.Col(
                        dbc.Card(
                            dbc.CardBody(
                                [
                                    html.H4("Relational Notes", className="card-title"),
                                    html.P(
                                        "Persist notes per session in a real relational table.",
                                        className="text-muted",
                                    ),
                                    dbc.Input(
                                        id="note-title",
                                        placeholder="Release checklist",
                                        type="text",
                                    ),
                                    dcc.Textarea(
                                        id="note-body",
                                        placeholder="Write a note to save in SessionNotes...",
                                        style={
                                            "width": "100%",
                                            "height": "120px",
                                            "marginTop": "0.75rem",
                                        },
                                    ),
                                    html.Button(
                                        "Save note",
                                        id="save-note-btn",
                                        n_clicks=0,
                                        className="btn btn-dark mt-3",
                                    ),
                                    html.Div(
                                        id="note-status", className="mt-3 text-secondary small"
                                    ),
                                    html.Div(id="recent-notes", className="mt-3"),
                                ]
                            )
                        ),
                        md=4,
                    ),
                    dbc.Col(
                        dbc.Card(
                            dbc.CardBody(
                                [
                                    html.H4("Vector Search", className="card-title"),
                                    html.P(
                                        "Search those notes semantically through SessionNoteIndex.",
                                        className="text-muted",
                                    ),
                                    dbc.Input(
                                        id="search-query",
                                        placeholder="Find the checklist about deploys",
                                        type="text",
                                    ),
                                    html.Button(
                                        "Search notes",
                                        id="search-btn",
                                        n_clicks=0,
                                        className="btn btn-secondary mt-3",
                                    ),
                                    html.Div(id="search-results", className="mt-3"),
                                ]
                            )
                        ),
                        md=4,
                    ),
                ],
                className="g-4 mt-1",
            ),
        ],
        fluid=True,
        className="py-4",
    )
)


@callback(
    Output("session-id", "data"),
    Input("init", "n_intervals"),
    State("session-id", "data"),
)
def init_session(_, existing_id):
    """Assign a session ID on first load if none exists."""
    if existing_id:
        return existing_id
    session_id = str(uuid.uuid4())
    # sync_set is safe in Dash callbacks — no event loop conflict
    Sessions.sync_set(session_id, UserState(session_id=session_id))
    return session_id


@callback(
    Output("session-summary", "children"),
    Output("recent-notes", "children"),
    Input("session-id", "data"),
)
def load_session_data(session_id):
    """Display the session handle and its most recent relational notes."""
    if not session_id:
        return "Creating session...", html.Div("Waiting for session...", className="text-muted")

    notes = _run_async(_load_notes(session_id))
    return f"Session {session_id[:8]} is using Skaal-backed storage.", _render_note_list(notes)


@callback(
    Output("output", "children"),
    Input("click-btn", "n_clicks"),
    State("session-id", "data"),
    prevent_initial_call=True,
)
def handle_click(n_clicks, session_id):
    """Increment this user's click counter in server-side state."""
    if not session_id:
        return "No session yet."

    # sync_update performs the read-modify-write atomically — no race condition
    # even when Dash fires concurrent callbacks (rapid clicks, multiple workers).
    def increment(state: UserState | None) -> UserState:
        if state is None:
            state = UserState(session_id=session_id)
        state.click_count += 1
        state.last_clicked = datetime.now(timezone.utc).isoformat()
        return state

    state = Sessions.sync_update(session_id, increment)
    return f"Clicks: {state.click_count} — last at {state.last_clicked}"


@callback(
    Output("note-status", "children"),
    Output("recent-notes", "children", allow_duplicate=True),
    Output("note-title", "value"),
    Output("note-body", "value"),
    Input("save-note-btn", "n_clicks"),
    State("session-id", "data"),
    State("note-title", "value"),
    State("note-body", "value"),
    prevent_initial_call=True,
)
def save_note(_, session_id, title, body):
    """Persist a relational note and refresh the note list."""
    if not session_id:
        return "No session yet.", no_update, no_update, no_update

    title = (title or "").strip()
    body = (body or "").strip()
    notes = _run_async(_load_notes(session_id))
    if not title or not body:
        return "Enter both a note title and body.", _render_note_list(notes), no_update, no_update

    note = _run_async(_persist_note(session_id, title, body))
    notes = _run_async(_load_notes(session_id))
    return f"Saved note #{note.id}.", _render_note_list(notes), "", ""


@callback(
    Output("search-results", "children"),
    Input("search-btn", "n_clicks"),
    State("session-id", "data"),
    State("search-query", "value"),
    prevent_initial_call=True,
)
def search_notes(_, session_id, query):
    """Search saved notes semantically using the vector store."""
    if not session_id:
        return html.Div("No session yet.", className="text-muted")

    query = (query or "").strip()
    if not query:
        return html.Div("Enter a search query first.", className="text-muted")

    results = _run_async(_search_notes(session_id, query))
    return _render_search_results(results)


# ── Tell Skaal which WSGI app to serve ────────────────────────────────────────

# dash_app.server is the Flask app behind the Dash frontend.
#
# - wsgi_app=... gives LocalRuntime the real callable for `skaal run`
#   (serves via uvicorn + WSGIMiddleware so the Dash UI loads in the browser).
# - attribute=... gives deploy generators the Python path to use in the
#   generated main.py / handler.py entry-point files.
skaal_app.mount_wsgi(
    dash_app.server if dash_app is not None else None,
    attribute="dash_app.server",
)


# ── Local dev entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio

    from skaal.runtime.local import LocalRuntime

    if dash_app is None:
        print("Install dash to run: pip install dash dash-bootstrap-components")
    else:
        # LocalRuntime wires storage AND serves the Dash UI via uvicorn.
        # Requires: pip install uvicorn starlette
        runtime = LocalRuntime(skaal_app, port=8050)
        asyncio.run(runtime.serve())
