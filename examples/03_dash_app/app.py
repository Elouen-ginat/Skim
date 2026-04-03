"""
Dash app with server-side per-user state via Skaal.

Demonstrates:
  - app.mount_wsgi() to register a WSGI app for deploy
  - Map[str, UserState] as scalable session storage
  - Sessions.sync_get / sync_set — safe in Dash's sync callbacks

Run locally (no Dash install needed to import this as a module):

    pip install dash
    python examples/03_dash_app/app.py

Deploy to GCP Cloud Run (gunicorn + Firestore):

    skaal deploy examples.03_dash_app.app:skaal_app --target gcp

Deploy to AWS Lambda (mangum + DynamoDB):

    skaal deploy examples.03_dash_app.app:skaal_app --target aws

Architecture:
  - Each browser tab gets a unique session_id stored in a dcc.Store component.
  - All mutable state lives in Sessions (backed by Redis/Firestore/DynamoDB).
  - Any Cloud Run / Lambda instance can serve any user — no sticky sessions.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pydantic import BaseModel

from skaal import App, Map


# ── Domain model ──────────────────────────────────────────────────────────────

class UserState(BaseModel):
    session_id: str
    click_count: int = 0
    last_clicked: str = ""
    filter_value: str = "all"


# ── Skaal app — declares storage constraints ──────────────────────────────────

skaal_app = App("dash-demo")


@skaal_app.storage(
    read_latency="< 5ms",
    durability="ephemeral",      # session data; Redis/Memorystore preferred
    retention="30m",             # expire inactive sessions after 30 minutes
    access_pattern="random-read",
)
class Sessions(Map[str, UserState]):
    """Per-user session state, keyed by session ID."""


# ── Dash layout and callbacks ─────────────────────────────────────────────────

try:
    import dash
    from dash import dcc, html, callback, Input, Output, State
    import dash_bootstrap_components as dbc  # type: ignore[import]

    dash_app = dash.Dash(
        __name__,
        external_stylesheets=[dbc.themes.BOOTSTRAP],
        suppress_callback_exceptions=True,
    )

    dash_app.layout = html.Div([
        # Session ID lives in the browser — stateless server side
        dcc.Store(id="session-id", storage_type="session"),
        dcc.Interval(id="init", interval=1, n_intervals=0, max_intervals=1),

        html.H2("Skaal + Dash Demo"),
        html.P("Server-side state per user. Scalable across multiple instances."),

        html.Button("Click me!", id="click-btn", n_clicks=0),
        html.Div(id="output"),
    ])

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
        Output("output", "children"),
        Input("click-btn", "n_clicks"),
        State("session-id", "data"),
        prevent_initial_call=True,
    )
    def handle_click(n_clicks, session_id):
        """Increment this user's click counter in server-side state."""
        if not session_id:
            return "No session yet."

        # sync_get / sync_set — safe in sync Dash callbacks
        state = Sessions.sync_get(session_id) or UserState(session_id=session_id)
        state.click_count += 1
        state.last_clicked = datetime.now(timezone.utc).isoformat()
        Sessions.sync_set(session_id, state)

        return f"Clicks: {state.click_count} — last at {state.last_clicked}"

except ImportError:
    # Dash is optional — skaal_app is importable without it for planning/deploy
    dash_app = None  # type: ignore[assignment]


# ── Tell Skaal which WSGI app to serve at deploy time ─────────────────────────

# "dash_app.server" is the Flask app behind the Dash frontend.
# The deploy generator uses this to produce the correct entry point:
#   - Cloud Run → main.py exposing `application = _user_module.dash_app.server`
#   - Lambda    → handler.py with `Mangum(_user_module.dash_app.server)`
skaal_app.mount_wsgi("dash_app.server")


# ── Local dev entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    from skaal.runtime.local import LocalRuntime

    # Wire Skaal storage with a local in-memory backend for dev
    LocalRuntime(skaal_app)

    if dash_app is not None:
        # Run Dash's dev server (hot reload, debug toolbar)
        dash_app.run(debug=True, port=8050)
    else:
        print("Install dash to run the dev server: pip install dash dash-bootstrap-components")
