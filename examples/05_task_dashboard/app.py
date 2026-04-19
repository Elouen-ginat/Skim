"""
05_task_dashboard — a complex Skaal + Dash example.

Demonstrates most Skaal features in one realistic application:

  Storage
  -------
  - Sessions   Map[str, SessionState]   ephemeral, random-read (Redis / Memorystore)
  - Users      Map[str, User]           persistent, collocated with Tasks
  - Tasks      Map[str, Task]           persistent, random-read
  - Stats      Map[str, StatsView]      persistent, write-heavy (aggregations)

  Patterns
  --------
  - AuditLog   EventLog[AuditEvent]     append-only audit trail of every task mutation
  - StatsSaga  Saga                     3-step: assign task → notify → update stats

  Agent
  -----
  - TaskProcessor   persistent agent that owns a per-user task counter

  Channel
  -------
  - Notifications   Channel[TaskNotification]   pub/sub for real-time alerts

  Scheduling
  ----------
  - Every("5m")   purge expired sessions
  - Cron("0 8 * * *")   generate daily active-task report

  Resilience
  ----------
  - RetryPolicy on create_task  (network glitches during write)
  - CircuitBreaker on assign_task  (protect Stats under load)
  - RateLimitPolicy on create_task  (abuse prevention)
  - Bulkhead on get_dashboard_stats  (isolate slow aggregations)

Run locally:

    pip install dash dash-bootstrap-components
    skaal run examples.05_task_dashboard.app:skaal_app

With persistent SQLite:

    skaal run examples.05_task_dashboard.app:skaal_app --persist

Plan for AWS:

    skaal plan --target aws-lambda examples.05_task_dashboard.app:skaal_app
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, dcc, html
from pydantic import BaseModel, Field

from skaal import (
    App,
    Bulkhead,
    Channel,
    CircuitBreaker,
    EventLog,
    Every,
    Map,
    RateLimitPolicy,
    RetryPolicy,
    Saga,
    SagaStep,
    ScheduleContext,
)
from skaal.agent import Agent
from skaal.decorators import handler
from skaal.types import Persistent, Scale

logger = logging.getLogger(__name__)

# ── Domain models ─────────────────────────────────────────────────────────────

Priority = Literal["low", "medium", "high", "critical"]
TaskStatus = Literal["open", "in_progress", "done", "cancelled"]


class User(BaseModel):
    id: str
    name: str
    email: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Task(BaseModel):
    id: str
    title: str
    description: str = ""
    priority: Priority = "medium"
    status: TaskStatus = "open"
    owner_id: str | None = None
    tags: list[str] = []
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str | None = None


class SessionState(BaseModel):
    session_id: str
    active_user_id: str | None = None
    last_seen: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    filter_status: TaskStatus | None = None


class AuditEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str  # created | assigned | completed | deleted
    task_id: str
    user_id: str | None = None
    payload: dict = {}
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class TaskNotification(BaseModel):
    task_id: str
    message: str
    severity: Literal["info", "warning", "error"] = "info"
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class StatsView(BaseModel):
    """Aggregated dashboard statistics — updated on every mutation."""

    key: str = "global"
    total_tasks: int = 0
    open_tasks: int = 0
    in_progress_tasks: int = 0
    done_tasks: int = 0
    cancelled_tasks: int = 0
    critical_open: int = 0
    last_updated: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── Skaal app declaration ─────────────────────────────────────────────────────

skaal_app = App("task-dashboard")

# ── Storage ───────────────────────────────────────────────────────────────────


@skaal_app.storage(
    read_latency="< 5ms",
    durability="ephemeral",
    access_pattern="random-read",
    # retention is a backend config concern (e.g. Redis TTL), not a solver
    # constraint — local catalogs don't declare it as a selectable property.
)
class Sessions(Map[str, SessionState]):
    """Ephemeral per-browser session state; Redis / Memorystore preferred."""


@skaal_app.storage(
    read_latency="< 10ms",
    durability="persistent",
    access_pattern="random-read",
    collocate_with="task-dashboard.Tasks",
)
class Users(Map[str, User]):
    """Registered users, co-located with Tasks for low-latency joins."""


@skaal_app.storage(
    read_latency="< 10ms",
    write_latency="< 20ms",
    durability="persistent",
    access_pattern="random-read",
)
class Tasks(Map[str, Task]):
    """
    Primary task store.

    Solver selects:
      - SQLite locally (--persist flag)
      - DynamoDB on aws-lambda target
      - Cloud Spanner on gcp target (write_throughput + durability)
    """


@skaal_app.storage(
    read_latency="< 20ms",
    write_latency="< 20ms",
    durability="persistent",
    # "write-heavy" is a cloud-catalog pattern (Spanner, DynamoDB on-demand).
    # Local backends only support "random-write" — functionally equivalent here.
    access_pattern="random-write",
    auto_optimize=True,
)
class Stats(Map[str, StatsView]):
    """Aggregated counters rebuilt on every task mutation."""


# ── Event log ─────────────────────────────────────────────────────────────────


@skaal_app.storage(
    access_pattern="event-log",
    # "durable" (11-nines replicated) is a cloud-only tier — local catalog only
    # has "ephemeral" and "persistent".  Use "persistent" here so the solver can
    # pick local-map or sqlite; on AWS/GCP the catalog maps this to Kinesis/Pub-Sub.
    durability="persistent",
)
class AuditLog(EventLog[AuditEvent]):
    """
    Append-only audit trail of every task lifecycle event.

    Solver maps to:
      - local-map in dev (in-process, no Kafka needed)
      - Amazon Kinesis on AWS
      - Google Pub/Sub on GCP
    """


@skaal_app.storage(
    read_latency="< 5ms",
    durability="persistent",
    access_pattern="random-read",
    collocate_with="task-dashboard.AuditLog",
)
class RecentAuditEvents(Map[str, AuditEvent]):
    """
    CQRS read-model: last 100 audit events materialised into a queryable Map.

    Written synchronously alongside AuditLog so Dash callbacks can read
    the audit trail without an async context.  Keys are ISO timestamps
    (sortable lexicographically), oldest entries pruned when count > 100.
    """


# ── Channel ───────────────────────────────────────────────────────────────────


@skaal_app.channel(throughput="> 200 events/s", durability="persistent")
class Notifications(Channel[TaskNotification]):
    """Real-time task notifications broadcast to all connected workers."""


# ── Agent ─────────────────────────────────────────────────────────────────────


@skaal_app.agent(persistent=True)
class TaskProcessor(Agent):
    """
    Persistent virtual actor — one instance per user.

    Keeps a per-user task tally that survives restarts.
    Receives TaskNotification messages and updates the tally.
    """

    tasks_processed: Persistent[int] = 0
    last_event_type: Persistent[str] = ""

    @handler
    async def handle_notification(self, notification: TaskNotification) -> None:
        self.tasks_processed += 1
        self.last_event_type = notification.message[:40]


# ── Patterns ──────────────────────────────────────────────────────────────────

# 3-step saga: assign task → send notification → update global stats.
# On failure at any step the compensation functions roll back in reverse order.
AssignSaga = skaal_app.pattern(
    Saga(
        name="AssignTaskSaga",
        coordination="compensation",
        steps=[
            SagaStep(
                function="persist_assignment",
                compensate="undo_assignment",
                timeout_ms=5_000,
            ),
            SagaStep(
                function="emit_assignment_notification",
                compensate="suppress_notification",
                timeout_ms=2_000,
            ),
            SagaStep(
                function="increment_stats_assigned",
                compensate="decrement_stats_assigned",
                timeout_ms=3_000,
            ),
        ],
    )
)

# ── Scheduling ────────────────────────────────────────────────────────────────


@skaal_app.schedule(trigger=Every(interval="5m"))
async def purge_expired_sessions() -> None:
    """Remove sessions that have not been active for more than 2 hours."""
    cutoff = datetime.now(timezone.utc)
    entries = await Sessions.list()
    for key, session in entries:
        last = datetime.fromisoformat(session.last_seen)
        if (cutoff - last).total_seconds() > 7200:
            await Sessions.delete(key)


@skaal_app.schedule(trigger=Every(interval="30s"), timezone="UTC")
async def daily_task_report(ctx: ScheduleContext) -> None:
    """
    Generate a daily snapshot of task statistics.

    Appends an AuditEvent with type 'daily_report' and the current stats
    payload so the EventLog captures point-in-time metrics.
    """
    stats = await Stats.get("global") or StatsView()
    await _record_audit(
        AuditEvent(
            event_type="daily_report",
            task_id="__system__",
            payload=stats.model_dump(),
        )
    )


# ── Compute functions ─────────────────────────────────────────────────────────


@skaal_app.function(
    retry=RetryPolicy(max_attempts=3, backoff="exponential", base_delay_ms=50),
    rate_limit=RateLimitPolicy(requests_per_second=100, burst=20, scope="global"),
)
async def create_task(
    title: str,
    description: str = "",
    priority: Priority = "medium",
    tags: list[str] | None = None,
    owner_id: str | None = None,
) -> dict:
    """Create a new task. Retried automatically on transient failures."""
    task_id = str(uuid.uuid4())
    task = Task(
        id=task_id,
        title=title,
        description=description,
        priority=priority,
        tags=tags or [],
        owner_id=owner_id,
    )
    await Tasks.set(task_id, task)

    await _record_audit(
        AuditEvent(
            event_type="created", task_id=task_id, user_id=owner_id, payload=task.model_dump()
        )
    )
    await _refresh_stats()
    return task.model_dump()


@skaal_app.function(
    circuit_breaker=CircuitBreaker(failure_threshold=5, recovery_timeout_ms=30_000),
    # Compute latency/memory are cloud hints; on local the single process handles all
    scale=Scale(instances="auto", strategy="round-robin"),
)
async def assign_task(task_id: str, user_id: str) -> dict:
    """
    Assign a task to a user.

    Uses a CircuitBreaker to protect Stats under write bursts.
    Scale is set to auto with round-robin so the solver can fan this out.
    """
    task = await Tasks.get(task_id)
    if task is None:
        return {"error": f"Task {task_id!r} not found"}

    user = await Users.get(user_id)
    if user is None:
        return {"error": f"User {user_id!r} not found"}

    task.owner_id = user_id
    task.status = "in_progress"
    task.updated_at = datetime.now(timezone.utc).isoformat()
    await Tasks.set(task_id, task)

    await _record_audit(AuditEvent(event_type="assigned", task_id=task_id, user_id=user_id))

    notification = TaskNotification(
        task_id=task_id,
        message=f"Task '{task.title}' assigned to {user.name}",
        severity="info",
    )
    await Notifications.send(notification)
    await _refresh_stats()
    return task.model_dump()


@skaal_app.function(
    retry=RetryPolicy(max_attempts=2, backoff="linear", base_delay_ms=100),
)
async def complete_task(task_id: str, user_id: str | None = None) -> dict:
    """Mark a task as done and stamp the completion time."""
    task = await Tasks.get(task_id)
    if task is None:
        return {"error": f"Task {task_id!r} not found"}

    task.status = "done"
    task.completed_at = datetime.now(timezone.utc).isoformat()
    task.updated_at = task.completed_at
    await Tasks.set(task_id, task)

    await _record_audit(
        AuditEvent(
            event_type="completed", task_id=task_id, user_id=user_id, payload={"title": task.title}
        )
    )
    await _refresh_stats()
    return task.model_dump()


@skaal_app.function()
async def delete_task(task_id: str, user_id: str | None = None) -> dict:
    """Remove a task from the store and log the deletion."""
    task = await Tasks.get(task_id)
    if task is None:
        return {"error": f"Task {task_id!r} not found"}
    await Tasks.delete(task_id)
    await _record_audit(AuditEvent(event_type="deleted", task_id=task_id, user_id=user_id))
    await _refresh_stats()
    return {"ok": True, "deleted": task_id}


@skaal_app.function(
    bulkhead=Bulkhead(max_concurrent_calls=10, max_wait_ms=2_000),
)
async def get_dashboard_stats() -> dict:
    """
    Return aggregated task statistics.

    A Bulkhead prevents slow stats scans from starving other handlers when
    the Tasks store is large.
    """
    stats = await Stats.get("global") or StatsView()
    return stats.model_dump()


@skaal_app.function()
async def list_tasks(status: TaskStatus | None = None) -> dict:
    """List all tasks, optionally filtered by status."""
    entries = await Tasks.list()
    tasks = [t for _, t in entries]
    if status:
        tasks = [t for t in tasks if t.status == status]
    tasks.sort(key=lambda t: t.created_at, reverse=True)
    return {"tasks": [t.model_dump() for t in tasks], "count": len(tasks)}


@skaal_app.function()
async def create_user(name: str, email: str) -> dict:
    """Register a new user. Returns error if email already taken."""
    # O(n) scan is acceptable at demo scale; production would use an index.
    entries = await Users.list()
    for _, u in entries:
        if u.email == email:
            return {"error": f"Email {email!r} already registered"}
    user = User(id=str(uuid.uuid4()), name=name, email=email)
    await Users.set(user.id, user)
    return user.model_dump()


@skaal_app.function()
async def list_users() -> dict:
    """Return all registered users."""
    entries = await Users.list()
    users = [u.model_dump() for _, u in entries]
    return {"users": users, "count": len(users)}


# ── Internal helper (not exposed as a Skaal function) ────────────────────────


async def _refresh_stats() -> None:
    """Recompute and persist the global StatsView from the current Tasks store."""
    entries = await Tasks.list()
    tasks = [t for _, t in entries]
    stats = StatsView(
        total_tasks=len(tasks),
        open_tasks=sum(1 for t in tasks if t.status == "open"),
        in_progress_tasks=sum(1 for t in tasks if t.status == "in_progress"),
        done_tasks=sum(1 for t in tasks if t.status == "done"),
        cancelled_tasks=sum(1 for t in tasks if t.status == "cancelled"),
        critical_open=sum(1 for t in tasks if t.priority == "critical" and t.status == "open"),
    )
    await Stats.set("global", stats)


async def _record_audit(event: AuditEvent) -> None:
    """
    Called from async contexts (scheduler jobs, Skaal HTTP functions).

    Uses the async storage API throughout so every await runs inside the
    caller's event loop — no cross-loop sharing, no _sync_run detour.
    AuditLog.append is best-effort: LocalRuntime only wires Map/Collection
    subclasses so _backend is absent locally.
    """
    # AuditLog (EventLog subclass) is never instantiated as a class — it cannot
    # be called as AuditLog.append(event) without an instance.  In production
    # the deploy target provisions a real stream (Kinesis / Pub-Sub) and wires
    # it correctly; locally we skip it and rely solely on RecentAuditEvents.
    key = f"{event.timestamp}_{event.event_id[:8]}"
    await RecentAuditEvents.set(key, event)
    # Prune to last 100 entries
    all_entries = await RecentAuditEvents.list()
    if len(all_entries) > 100:
        oldest = sorted(k for k, _ in all_entries)[: len(all_entries) - 100]
        for k in oldest:
            await RecentAuditEvents.delete(k)


def _sync_audit(event: AuditEvent) -> None:
    """
    Sync variant used directly by Dash callbacks.

    Writes only to RecentAuditEvents (the readable CQRS projection).
    EventLog.append is async-only; Dash callbacks call this instead of
    _record_audit so audit entries appear in the UI without an event loop.
    Prunes to the last 100 entries on every write.
    """
    key = f"{event.timestamp}_{event.event_id[:8]}"
    RecentAuditEvents.sync_set(key, event)
    # Prune oldest entries so the Map stays bounded
    all_entries = RecentAuditEvents.sync_list()
    if len(all_entries) > 100:
        oldest_keys = sorted(k for k, _ in all_entries)[: len(all_entries) - 100]
        for k in oldest_keys:
            RecentAuditEvents.sync_delete(k)


# ── Saga step implementations ─────────────────────────────────────────────────
# These are referenced by name in AssignSaga.


@skaal_app.function()
async def persist_assignment(task_id: str, user_id: str) -> dict:
    return await assign_task(task_id, user_id)


@skaal_app.function()
async def undo_assignment(task_id: str) -> None:
    task = await Tasks.get(task_id)
    if task:
        task.owner_id = None
        task.status = "open"
        await Tasks.set(task_id, task)


@skaal_app.function()
async def emit_assignment_notification(task_id: str, user_id: str) -> None:
    user = await Users.get(user_id)
    task = await Tasks.get(task_id)
    if user and task:
        await Notifications.send(
            TaskNotification(
                task_id=task_id,
                message=f"[saga] '{task.title}' → {user.name}",
            )
        )


@skaal_app.function()
async def suppress_notification(**_: object) -> None:
    pass  # compensate: nothing to undo for a fire-and-forget notification


@skaal_app.function()
async def increment_stats_assigned(**_: object) -> None:
    await _refresh_stats()


@skaal_app.function()
async def decrement_stats_assigned(**_: object) -> None:
    await _refresh_stats()


# ── Dash layout helpers ───────────────────────────────────────────────────────

PRIORITY_COLORS = {
    "low": "secondary",
    "medium": "primary",
    "high": "warning",
    "critical": "danger",
}

STATUS_COLORS = {
    "open": "light",
    "in_progress": "info",
    "done": "success",
    "cancelled": "secondary",
}


def _stat_card(title: str, value: str | int, color: str = "primary") -> dbc.Card:
    return dbc.Card(
        dbc.CardBody(
            [
                html.H6(title, className="card-subtitle text-muted mb-1"),
                html.H3(str(value), className=f"text-{color} mb-0"),
            ]
        ),
        className="mb-2 shadow-sm",
    )


def _task_row(task: dict, users: list[dict]) -> dbc.ListGroupItem:
    user_map = {u["id"]: u["name"] for u in users}
    owner = user_map.get(task.get("owner_id") or "", "—")
    is_done = task["status"] == "done"

    # Build user options for the assign dropdown
    user_options = [{"label": u["name"], "value": u["id"]} for u in users]

    return dbc.ListGroupItem(
        dbc.Row(
            [
                dbc.Col(html.Strong(task["title"]), width=3),
                dbc.Col(
                    dbc.Badge(task["priority"], color=PRIORITY_COLORS[task["priority"]]), width=1
                ),
                dbc.Col(dbc.Badge(task["status"], color=STATUS_COLORS[task["status"]]), width=2),
                dbc.Col(owner, width=2, className="text-muted small"),
                dbc.Col(task["created_at"][:10], width=1, className="text-muted small"),
                dbc.Col(
                    dbc.InputGroup(
                        [
                            dbc.Select(
                                id={"type": "sel-assign", "index": task["id"]},
                                options=user_options,
                                placeholder="Assign…",
                                size="sm",
                                disabled=is_done or not user_options,
                                style={"maxWidth": "120px"},
                            ),
                            dbc.Button(
                                "→",
                                id={"type": "btn-assign", "index": task["id"]},
                                size="sm",
                                color="info",
                                outline=True,
                                disabled=is_done or not user_options,
                                title="Assign & set In Progress",
                            ),
                            dbc.Button(
                                "✓",
                                id={"type": "btn-done", "index": task["id"]},
                                size="sm",
                                color="success",
                                outline=True,
                                disabled=is_done,
                                title="Mark done",
                            ),
                            dbc.Button(
                                "✕",
                                id={"type": "btn-del", "index": task["id"]},
                                size="sm",
                                color="danger",
                                outline=True,
                                title="Delete",
                            ),
                        ],
                        size="sm",
                    ),
                    width=3,
                ),
            ],
            align="center",
        ),
    )


# ── Dash app ──────────────────────────────────────────────────────────────────

dash_app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.FLATLY],
    suppress_callback_exceptions=True,
)

dash_app.layout = dbc.Container(
    fluid=True,
    children=[
        # ── invisible state ───────────────────────────────────────────────
        dcc.Store(id="session-id", storage_type="session"),
        dcc.Interval(id="boot", interval=1, n_intervals=0, max_intervals=1),
        dcc.Interval(id="poll", interval=10_000, n_intervals=0),  # auto-refresh every 10 s
        dcc.Store(id="action-result"),  # carries last op outcome
        # ── header ────────────────────────────────────────────────────────
        dbc.Row(
            dbc.Col(
                html.H2(
                    ["Task Dashboard ", dbc.Badge("Skaal", color="dark", className="ms-2")],
                    className="my-3",
                )
            )
        ),
        # ── tabs ──────────────────────────────────────────────────────────
        dbc.Tabs(
            id="main-tabs",
            active_tab="tab-board",
            children=[
                # ── Tab 1: Task Board ──────────────────────────────────
                dbc.Tab(
                    label="Task Board",
                    tab_id="tab-board",
                    children=[
                        dbc.Row(
                            [
                                # ── Stats column ──────────────────────────
                                dbc.Col(
                                    [
                                        html.H5("Overview", className="mt-3"),
                                        html.Div(id="stats-cards"),
                                        html.Hr(),
                                        html.H5("Create Task"),
                                        dbc.Input(
                                            id="inp-title",
                                            placeholder="Task title",
                                            className="mb-2",
                                        ),
                                        dbc.Textarea(
                                            id="inp-desc",
                                            placeholder="Description (optional)",
                                            rows=2,
                                            className="mb-2",
                                        ),
                                        dbc.Select(
                                            id="inp-priority",
                                            options=[
                                                {"label": p.capitalize(), "value": p}
                                                for p in ["low", "medium", "high", "critical"]
                                            ],
                                            value="medium",
                                            className="mb-2",
                                        ),
                                        dbc.Input(
                                            id="inp-tags",
                                            placeholder="Tags (comma-separated)",
                                            className="mb-2",
                                        ),
                                        dbc.Button(
                                            "Add Task",
                                            id="btn-create",
                                            color="primary",
                                            className="w-100",
                                        ),
                                        html.Div(id="create-feedback", className="mt-2"),
                                    ],
                                    width=3,
                                ),
                                # ── Task list ─────────────────────────────
                                dbc.Col(
                                    [
                                        dbc.Row(
                                            [
                                                dbc.Col(
                                                    html.H5("Tasks", className="mt-3"), width="auto"
                                                ),
                                                dbc.Col(
                                                    dbc.Select(
                                                        id="filter-status",
                                                        options=[
                                                            {"label": "All", "value": ""},
                                                            {"label": "Open", "value": "open"},
                                                            {
                                                                "label": "In Progress",
                                                                "value": "in_progress",
                                                            },
                                                            {"label": "Done", "value": "done"},
                                                            {
                                                                "label": "Cancelled",
                                                                "value": "cancelled",
                                                            },
                                                        ],
                                                        value="",
                                                        className="mt-2",
                                                    ),
                                                    width=3,
                                                ),
                                            ]
                                        ),
                                        dbc.ListGroup(id="task-list", className="mt-2"),
                                    ],
                                    width=9,
                                ),
                            ]
                        ),
                    ],
                ),
                # ── Tab 2: Users ───────────────────────────────────────
                dbc.Tab(
                    label="Users",
                    tab_id="tab-users",
                    children=dbc.Row(
                        [
                            dbc.Col(
                                [
                                    html.H5("Register User", className="mt-3"),
                                    dbc.Input(id="inp-uname", placeholder="Name", className="mb-2"),
                                    dbc.Input(
                                        id="inp-email",
                                        placeholder="Email",
                                        type="email",
                                        className="mb-2",
                                    ),
                                    dbc.Button(
                                        "Register",
                                        id="btn-register",
                                        color="success",
                                        className="w-100",
                                    ),
                                    html.Div(id="user-feedback", className="mt-2"),
                                ],
                                width=3,
                            ),
                            dbc.Col(
                                [
                                    html.H5("Registered Users", className="mt-3"),
                                    dbc.ListGroup(id="user-list"),
                                ],
                                width=9,
                            ),
                        ]
                    ),
                ),
                # ── Tab 3: Audit Log ───────────────────────────────────
                dbc.Tab(
                    label="Audit Log",
                    tab_id="tab-audit",
                    children=[
                        html.H5("Recent Events", className="mt-3"),
                        html.P(
                            "Last 50 events from the append-only AuditLog EventLog.",
                            className="text-muted",
                        ),
                        dbc.Table(
                            id="audit-table",
                            striped=True,
                            bordered=True,
                            hover=True,
                            responsive=True,
                            size="sm",
                        ),
                    ],
                ),
                # ── Tab 4: System Health ───────────────────────────────
                dbc.Tab(
                    label="System Health",
                    tab_id="tab-health",
                    children=[
                        html.H5("Skaal Infrastructure Status", className="mt-3"),
                        dbc.Alert(
                            "Health data is read from the Skaal Mesh control plane.",
                            color="info",
                            className="mb-3",
                        ),
                        # ── Scheduled jobs panel ───────────────────────
                        dbc.Card(
                            dbc.CardBody(
                                [
                                    html.H6("Scheduled Jobs", className="card-title"),
                                    html.P(
                                        "Cron / interval jobs registered with @app.schedule(). "
                                        "Use 'Run now' to trigger manually without waiting for "
                                        "the next scheduled firing.",
                                        className="text-muted small",
                                    ),
                                    dbc.Row(
                                        [
                                            dbc.Col(
                                                [
                                                    html.Strong("purge_expired_sessions"),
                                                    html.Span(
                                                        " Every 5 min",
                                                        className="text-muted ms-2 small",
                                                    ),
                                                ],
                                                width=7,
                                            ),
                                            dbc.Col(
                                                dbc.Button(
                                                    "Run now",
                                                    id="btn-trigger-purge",
                                                    size="sm",
                                                    color="secondary",
                                                    outline=True,
                                                ),
                                                width=2,
                                            ),
                                        ],
                                        className="mb-2 align-items-center",
                                    ),
                                    dbc.Row(
                                        [
                                            dbc.Col(
                                                [
                                                    html.Strong("daily_task_report"),
                                                    html.Span(
                                                        " Cron 0 8 * * * (08:00 UTC)",
                                                        className="text-muted ms-2 small",
                                                    ),
                                                ],
                                                width=7,
                                            ),
                                            dbc.Col(
                                                dbc.Button(
                                                    "Run now",
                                                    id="btn-trigger-report",
                                                    size="sm",
                                                    color="primary",
                                                    outline=True,
                                                ),
                                                width=2,
                                            ),
                                        ],
                                        className="mb-2 align-items-center",
                                    ),
                                    html.Div(id="schedule-trigger-result", className="mt-2"),
                                ]
                            ),
                            className="mb-3",
                        ),
                        html.Div(id="health-panel"),
                    ],
                ),
            ],
        ),
    ],
)


# ── Callbacks ─────────────────────────────────────────────────────────────────


@callback(
    Output("session-id", "data"),
    Input("boot", "n_intervals"),
    State("session-id", "data"),
)
def init_session(_, existing_id: str | None) -> str:
    """Assign a stable session ID on first load."""
    if existing_id:
        Sessions.sync_update(
            existing_id,
            lambda s: SessionState(
                **(s or SessionState(session_id=existing_id)).model_dump()
                | {"last_seen": datetime.now(timezone.utc).isoformat()}
            ),
        )
        return existing_id
    sid = str(uuid.uuid4())
    Sessions.sync_set(sid, SessionState(session_id=sid))
    return sid


@callback(
    Output("stats-cards", "children"),
    Output("task-list", "children"),
    Input("poll", "n_intervals"),
    Input("action-result", "data"),
    Input("filter-status", "value"),
    State("session-id", "data"),
)
def refresh_board(_, __, filter_status: str, session_id: str | None):
    """Reload the stats cards and task list whenever the poll fires or an action completes."""
    # -- stats --
    stats_raw = Stats.sync_get("global") or StatsView()
    stats_cards = dbc.Row(
        [
            dbc.Col(_stat_card("Total", stats_raw.total_tasks), width=2),
            dbc.Col(_stat_card("Open", stats_raw.open_tasks, "primary"), width=2),
            dbc.Col(_stat_card("In Progress", stats_raw.in_progress_tasks, "info"), width=2),
            dbc.Col(_stat_card("Done", stats_raw.done_tasks, "success"), width=2),
            dbc.Col(_stat_card("Critical Open", stats_raw.critical_open, "danger"), width=2),
        ]
    )

    # -- tasks --
    entries = Tasks.sync_list()
    tasks = [t for _, t in entries]
    if filter_status:
        tasks = [t for t in tasks if t.status == filter_status]
    tasks.sort(key=lambda t: t.created_at, reverse=True)

    users_raw = Users.sync_list()
    users = [u.model_dump() for _, u in users_raw]

    if not tasks:
        task_rows = [dbc.ListGroupItem("No tasks yet. Create one on the left.", color="light")]
    else:
        task_rows = [_task_row(t.model_dump(), users) for t in tasks]

    return stats_cards, task_rows


@callback(
    Output("create-feedback", "children"),
    Output("action-result", "data"),
    Input("btn-create", "n_clicks"),
    State("inp-title", "value"),
    State("inp-desc", "value"),
    State("inp-priority", "value"),
    State("inp-tags", "value"),
    State("session-id", "data"),
    prevent_initial_call=True,
)
def handle_create(n_clicks, title, desc, priority, tags_raw, session_id):
    """Create a new task via sync wrapper and surface feedback."""
    if not title:
        return dbc.Alert("Title is required.", color="warning"), dash.no_update

    tags = [t.strip() for t in (tags_raw or "").split(",") if t.strip()]
    task = Task(
        id=str(uuid.uuid4()),
        title=title,
        description=desc or "",
        priority=priority or "medium",
        tags=tags,
    )
    Tasks.sync_set(task.id, task)
    _sync_audit(AuditEvent(event_type="created", task_id=task.id, payload=task.model_dump()))

    # Rebuild stats synchronously (sync_update on Stats)
    def update_stats(s: StatsView | None) -> StatsView:
        s = s or StatsView()
        s.total_tasks += 1
        s.open_tasks += 1
        if task.priority == "critical":
            s.critical_open += 1
        s.last_updated = datetime.now(timezone.utc).isoformat()
        return s

    Stats.sync_update("global", update_stats)

    return (
        dbc.Alert(f"Created: '{task.title}'", color="success", duration=4000),
        {"op": "create", "task_id": task.id},
    )


@callback(
    Output("action-result", "data", allow_duplicate=True),
    Input({"type": "btn-done", "index": dash.ALL}, "n_clicks"),
    State({"type": "btn-done", "index": dash.ALL}, "id"),
    State("session-id", "data"),
    prevent_initial_call=True,
)
def handle_done(n_clicks_list, ids, session_id):
    """Mark the clicked task as done."""
    ctx = dash.callback_context
    if not ctx.triggered or not any(n for n in n_clicks_list if n):
        return dash.no_update

    triggered_id = ctx.triggered[0]["prop_id"]
    # Extract the task id from the triggered component id
    import json as _json

    raw = triggered_id.rsplit(".", 1)[0]
    task_id = _json.loads(raw)["index"]

    def mark_done(t: Task | None) -> Task:
        if t is None:
            return Task(id=task_id, title="unknown")
        t.status = "done"
        t.completed_at = datetime.now(timezone.utc).isoformat()
        t.updated_at = t.completed_at
        return t

    updated = Tasks.sync_update(task_id, mark_done)
    _sync_audit(
        AuditEvent(
            event_type="completed",
            task_id=task_id,
            payload={"title": updated.title if updated else ""},
        )
    )

    def update_stats(s: StatsView | None) -> StatsView:
        s = s or StatsView()
        if s.open_tasks > 0:
            s.open_tasks -= 1
        elif s.in_progress_tasks > 0:
            s.in_progress_tasks -= 1
        s.done_tasks += 1
        s.last_updated = datetime.now(timezone.utc).isoformat()
        return s

    Stats.sync_update("global", update_stats)
    return {"op": "done", "task_id": task_id}


@callback(
    Output("action-result", "data", allow_duplicate=True),
    Input({"type": "btn-assign", "index": dash.ALL}, "n_clicks"),
    State({"type": "btn-assign", "index": dash.ALL}, "id"),
    State({"type": "sel-assign", "index": dash.ALL}, "value"),
    prevent_initial_call=True,
)
def handle_assign(n_clicks_list, ids, selected_users):
    """Assign the selected user to a task and set its status to in_progress."""
    ctx = dash.callback_context
    if not ctx.triggered or not any(n for n in n_clicks_list if n):
        return dash.no_update

    import json as _json

    triggered_id = ctx.triggered[0]["prop_id"]
    raw = triggered_id.rsplit(".", 1)[0]
    task_id = _json.loads(raw)["index"]

    # Find which index in the id list corresponds to the triggered task
    idx = next((i for i, cid in enumerate(ids) if cid["index"] == task_id), None)
    if idx is None:
        return dash.no_update

    user_id = selected_users[idx] if idx < len(selected_users) else None
    if not user_id:
        return dash.no_update

    user = Users.sync_get(user_id)
    if user is None:
        return dash.no_update

    def assign(t: Task | None) -> Task:
        if t is None:
            return Task(id=task_id, title="unknown")
        t.owner_id = user_id
        t.status = "in_progress"
        t.updated_at = datetime.now(timezone.utc).isoformat()
        return t

    updated = Tasks.sync_update(task_id, assign)
    _sync_audit(
        AuditEvent(
            event_type="assigned",
            task_id=task_id,
            user_id=user_id,
            payload={"user_name": user.name, "title": updated.title if updated else ""},
        )
    )

    def update_stats(s: StatsView | None) -> StatsView:
        s = s or StatsView()
        # Only move from open → in_progress (task may already be in_progress)
        if updated and updated.status == "in_progress":
            s.open_tasks = max(0, s.open_tasks - 1)
            s.in_progress_tasks += 1
        s.last_updated = datetime.now(timezone.utc).isoformat()
        return s

    Stats.sync_update("global", update_stats)
    return {"op": "assign", "task_id": task_id, "user_id": user_id}


@callback(
    Output("action-result", "data", allow_duplicate=True),
    Input({"type": "btn-del", "index": dash.ALL}, "n_clicks"),
    State({"type": "btn-del", "index": dash.ALL}, "id"),
    prevent_initial_call=True,
)
def handle_delete(n_clicks_list, ids):
    """Delete the clicked task and decrement stats."""
    ctx = dash.callback_context
    if not ctx.triggered or not any(n for n in n_clicks_list if n):
        return dash.no_update

    import json as _json

    triggered_id = ctx.triggered[0]["prop_id"]
    raw = triggered_id.rsplit(".", 1)[0]
    task_id = _json.loads(raw)["index"]

    task = Tasks.sync_get(task_id)
    Tasks.sync_delete(task_id)
    _sync_audit(
        AuditEvent(
            event_type="deleted",
            task_id=task_id,
            payload={"title": task.title if task else ""},
        )
    )

    if task:

        def decr(s: StatsView | None) -> StatsView:
            s = s or StatsView()
            s.total_tasks = max(0, s.total_tasks - 1)
            if task.status == "open":
                s.open_tasks = max(0, s.open_tasks - 1)
            elif task.status == "in_progress":
                s.in_progress_tasks = max(0, s.in_progress_tasks - 1)
            elif task.status == "done":
                s.done_tasks = max(0, s.done_tasks - 1)
            if task.priority == "critical" and task.status == "open":
                s.critical_open = max(0, s.critical_open - 1)
            s.last_updated = datetime.now(timezone.utc).isoformat()
            return s

        Stats.sync_update("global", decr)

    return {"op": "delete", "task_id": task_id}


@callback(
    Output("user-list", "children"),
    Output("user-feedback", "children"),
    Input("btn-register", "n_clicks"),
    Input("poll", "n_intervals"),
    State("inp-uname", "value"),
    State("inp-email", "value"),
    prevent_initial_call=False,
)
def handle_user_tab(n_clicks, _, name, email):
    """Register a user or refresh the user list."""
    ctx = dash.callback_context
    feedback = dash.no_update

    if ctx.triggered and ctx.triggered[0]["prop_id"] == "btn-register.n_clicks":
        if not name or not email:
            feedback = dbc.Alert("Name and email required.", color="warning")
        else:
            # Check for duplicates
            existing = [u for _, u in Users.sync_list() if u.email == email]
            if existing:
                feedback = dbc.Alert(f"Email {email!r} already registered.", color="danger")
            else:
                user = User(id=str(uuid.uuid4()), name=name, email=email)
                Users.sync_set(user.id, user)
                feedback = dbc.Alert(f"Registered: {name}", color="success", duration=4000)

    entries = Users.sync_list()
    if not entries:
        user_items = [dbc.ListGroupItem("No users yet.", color="light")]
    else:
        user_items = [
            dbc.ListGroupItem(
                dbc.Row(
                    [
                        dbc.Col(html.Strong(u.name), width=4),
                        dbc.Col(u.email, width=5, className="text-muted"),
                        dbc.Col(u.created_at[:10], width=3, className="text-muted"),
                    ]
                ),
            )
            for _, u in entries
        ]

    return user_items, feedback


@callback(
    Output("audit-table", "children"),
    Input("main-tabs", "active_tab"),
    Input("poll", "n_intervals"),
)
def refresh_audit(active_tab, _):
    """Render the last 50 AuditLog events as an HTML table."""
    if active_tab != "tab-audit":
        return dash.no_update

    # RecentAuditEvents is a CQRS read-model written alongside AuditLog.
    # Keys are ISO timestamps so sort order = chronological order.
    entries = RecentAuditEvents.sync_scan("")
    events = sorted(entries, key=lambda kv: kv[0])[-50:]

    header = html.Thead(
        html.Tr(
            [
                html.Th("Time"),
                html.Th("Type"),
                html.Th("Task ID"),
                html.Th("User"),
                html.Th("Payload"),
            ]
        )
    )

    def _row(kv: tuple) -> html.Tr:
        _, evt = kv
        tid = evt.task_id
        short_tid = tid[:8] + "…" if len(tid) > 8 else tid
        return html.Tr(
            [
                html.Td(evt.timestamp[:19]),
                html.Td(dbc.Badge(evt.event_type, color="info")),
                html.Td(short_tid),
                html.Td(evt.user_id or "—"),
                html.Td(str(evt.payload)[:60]),
            ]
        )

    body = html.Tbody(
        [_row(e) for e in events]
        if events
        else [html.Tr([html.Td("No events yet.", colSpan=5, className="text-center text-muted")])]
    )
    return [header, body]


@callback(
    Output("schedule-trigger-result", "children"),
    Input("btn-trigger-purge", "n_clicks"),
    Input("btn-trigger-report", "n_clicks"),
    prevent_initial_call=True,
)
def trigger_scheduled_job(*_):
    """
    Run a scheduled function on demand using only the sync storage API.

    We deliberately avoid asyncio.run() here.  The storage backends share
    connection pools initialised in the scheduler's background event loop;
    opening a second loop via asyncio.run() causes
    "Future attached to a different loop" errors.
    Using sync_* methods instead spawns a fresh OS thread per call (via
    _sync_run), each with its own event loop, so no loop is shared.
    """
    ctx = dash.callback_context
    if not ctx.triggered:
        return dash.no_update

    trigger_id = ctx.triggered[0]["prop_id"].split(".")[0]

    try:
        if trigger_id == "btn-trigger-purge":
            cutoff = datetime.now(timezone.utc)
            entries = Sessions.sync_list()
            removed = 0
            for key, session in entries:
                last = datetime.fromisoformat(session.last_seen)
                if (cutoff - last).total_seconds() > 7200:
                    Sessions.sync_delete(key)
                    removed += 1
            return dbc.Alert(
                f"purge_expired_sessions completed — {removed} expired session(s) removed.",
                color="success",
                duration=6000,
            )

        if trigger_id == "btn-trigger-report":
            stats = Stats.sync_get("global") or StatsView()
            event = AuditEvent(
                event_type="daily_report",
                task_id="__system__",
                payload=stats.model_dump(),
            )
            _sync_audit(event)
            return dbc.Alert(
                "daily_task_report completed — check the Audit Log tab for the 'daily_report' entry.",
                color="success",
                duration=8000,
            )
    except Exception as exc:
        return dbc.Alert(f"Error: {exc}", color="danger")

    return dash.no_update


@callback(
    Output("health-panel", "children"),
    Input("main-tabs", "active_tab"),
    Input("poll", "n_intervals"),
)
def refresh_health(active_tab, _):
    """
    Try to pull a health snapshot from the Skaal Mesh control plane.

    Falls back to a static summary when the Mesh Rust extension is not built.
    """
    if active_tab != "tab-health":
        return dash.no_update

    try:
        from skaal.mesh import MeshClient

        mesh = MeshClient("task-dashboard")
        snap = mesh.health_snapshot()

        agents = snap.agents or {}
        migrations = snap.migrations or {}
        # channels = snap.channels or {}
        state_keys = snap.state or {}

        panels = [
            dbc.Card(
                dbc.CardBody(
                    [
                        html.H6("App", className="card-subtitle text-muted"),
                        html.P(snap.app, className="mb-0"),
                        dbc.Badge(
                            snap.status, color="success" if snap.status == "ok" else "danger"
                        ),
                    ]
                ),
                className="mb-3",
            ),
            dbc.Row(
                [
                    dbc.Col(
                        dbc.Card(
                            dbc.CardBody(
                                [
                                    html.H6("Registered Agents"),
                                    html.P(f"{len(agents)} agent(s)"),
                                    html.Ul(
                                        [html.Li(f"{k}: {v}") for k, v in list(agents.items())[:10]]
                                    ),
                                ]
                            )
                        ),
                        width=4,
                    ),
                    dbc.Col(
                        dbc.Card(
                            dbc.CardBody(
                                [
                                    html.H6("Active Migrations"),
                                    html.P(f"{len(migrations)} migration(s)"),
                                    html.Ul(
                                        [
                                            html.Li(f"{k}: stage {v}")
                                            for k, v in list(migrations.items())[:5]
                                        ]
                                    ),
                                ]
                            )
                        ),
                        width=4,
                    ),
                    dbc.Col(
                        dbc.Card(
                            dbc.CardBody(
                                [
                                    html.H6("Shared State Keys"),
                                    html.P(f"{len(state_keys)} key(s)"),
                                    html.Ul([html.Li(k) for k in list(state_keys.keys())[:10]]),
                                ]
                            )
                        ),
                        width=4,
                    ),
                ]
            ),
        ]
        return panels

    except Exception as exc:
        return dbc.Alert(
            [
                html.Strong("Mesh not available: "),
                str(exc),
                html.Br(),
                html.Small("Build the extension: maturin develop --manifest-path mesh/Cargo.toml"),
            ],
            color="warning",
        )


# ── Wire Dash into Skaal ──────────────────────────────────────────────────────

skaal_app.mount_wsgi(dash_app.server, attribute="dash_app.server")


# ── Local dev entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio

    from skaal.runtime.local import LocalRuntime

    runtime = LocalRuntime(skaal_app, port=8050)
    asyncio.run(runtime.serve())
