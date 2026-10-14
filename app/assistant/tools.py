"""Tools Claude can call. Every query is scoped to the calling user's id."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..db import get_conn

_LOCAL_TIME = {
    "type": "string",
    "description": "Local date-time in the user's timezone, ISO 8601 without offset, e.g. 2026-10-14T09:00",
}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


# Order and content must stay fixed: changing tools mid-conversation invalidates replayed thinking.
TOOLS = [
    _tool(
        "add_note",
        "Save a note: something the user wants to remember that isn't a task (an idea, a fact, "
        "something they did).",
        {"content": {"type": "string", "description": "The note, written as a complete sentence."}},
        ["content"],
    ),
    _tool(
        "search_notes",
        "Find the user's saved notes. Omit query to get the most recent notes.",
        {
            "query": {"type": "string", "description": "Words to search for."},
            "limit": {"type": "integer", "description": "Max results (default 10)."},
        },
        [],
    ),
    _tool(
        "add_task",
        "Add something the user needs to do. Use set_reminder as well if they want to be reminded at a time.",
        {
            "title": {"type": "string", "description": "Short imperative title, e.g. 'Call the vet'."},
            "notes": {"type": "string", "description": "Extra details."},
            "due_at": _LOCAL_TIME,
        },
        ["title"],
    ),
    _tool(
        "list_tasks",
        "List the user's tasks with their ids.",
        {"status": {"type": "string", "enum": ["open", "done", "all"], "description": "Default open."}},
        [],
    ),
    _tool(
        "complete_task",
        "Mark a task done. If the user said how it went, pass that as outcome; it helps with similar tasks later.",
        {
            "task_id": {"type": "integer"},
            "outcome": {"type": "string", "description": "How it went: time taken, problems, what worked."},
        },
        ["task_id"],
    ),
    _tool(
        "set_reminder",
        "Schedule a spoken reminder at a specific local time. Resolve relative times ('tomorrow morning', "
        "'in 2 hours') against the current time given in the user's message.",
        {
            "message": {"type": "string", "description": "What to say when the reminder fires."},
            "remind_at": _LOCAL_TIME,
            "task_id": {"type": "integer", "description": "Related task id, if any."},
        },
        ["message", "remind_at"],
    ),
    _tool("list_reminders", "List the user's upcoming reminders with their ids.", {}, []),
    _tool(
        "cancel_reminder",
        "Cancel an upcoming reminder.",
        {"reminder_id": {"type": "integer"}},
        ["reminder_id"],
    ),
]


class ToolError(Exception):
    """Raised for bad input; the message goes back to Claude as an error tool_result."""


class ToolContext:
    def __init__(self, user_id: int, tz: str):
        self.user_id = user_id
        self.tz = ZoneInfo(tz)

    def parse_local(self, value: str) -> datetime:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            raise ToolError(f"Invalid date-time '{value}'; use ISO 8601 like 2026-10-14T09:00") from None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        return dt.astimezone(timezone.utc)

    def fmt(self, dt: datetime | None) -> str | None:
        if dt is None:
            return None
        return dt.astimezone(self.tz).strftime("%a %b %-d %Y, %-I:%M %p")


def run_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"Unknown tool '{name}'")
    return json.dumps(handler(ctx, **args), default=str)


def _add_note(ctx: ToolContext, content: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO notes (user_id, content) VALUES (%s, %s) RETURNING id", (ctx.user_id, content)
        ).fetchone()
    return {"saved": True, "note_id": row["id"]}


def _search_notes(ctx: ToolContext, query: str | None = None, limit: int = 10) -> dict:
    limit = max(1, min(limit, 50))
    with get_conn() as conn:
        if query:
            rows = conn.execute(
                "SELECT id, content, created_at FROM notes WHERE user_id = %s "
                "AND to_tsvector('english', content) @@ websearch_to_tsquery('english', %s) "
                "ORDER BY created_at DESC LIMIT %s",
                (ctx.user_id, query, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, content, created_at FROM notes WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                (ctx.user_id, limit),
            ).fetchall()
    return {"notes": [{"id": r["id"], "content": r["content"], "saved": ctx.fmt(r["created_at"])} for r in rows]}


def _add_task(ctx: ToolContext, title: str, notes: str = "", due_at: str | None = None) -> dict:
    due = ctx.parse_local(due_at) if due_at else None
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO tasks (user_id, title, notes, due_at) VALUES (%s, %s, %s, %s) RETURNING id",
            (ctx.user_id, title, notes, due),
        ).fetchone()
    return {"task_id": row["id"], "due": ctx.fmt(due)}


def _list_tasks(ctx: ToolContext, status: str = "open") -> dict:
    where = "" if status == "all" else "AND status = %(status)s"
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, title, notes, status, due_at, outcome, completed_at FROM tasks "
            f"WHERE user_id = %(uid)s {where} ORDER BY status, due_at NULLS LAST, created_at LIMIT 100",
            {"uid": ctx.user_id, "status": status},
        ).fetchall()
    return {
        "tasks": [
            {
                "id": r["id"], "title": r["title"], "notes": r["notes"] or None, "status": r["status"],
                "due": ctx.fmt(r["due_at"]), "outcome": r["outcome"], "completed": ctx.fmt(r["completed_at"]),
            }
            for r in rows
        ]
    }


def _complete_task(ctx: ToolContext, task_id: int, outcome: str | None = None) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = now(), outcome = COALESCE(%s, outcome) "
            "WHERE id = %s AND user_id = %s RETURNING title",
            (outcome, task_id, ctx.user_id),
        ).fetchone()
        if row:
            conn.execute(
                "DELETE FROM reminders WHERE task_id = %s AND user_id = %s AND fired_at IS NULL",
                (task_id, ctx.user_id),
            )
    if row is None:
        raise ToolError(f"No task with id {task_id}")
    return {"completed": row["title"]}


def _set_reminder(ctx: ToolContext, message: str, remind_at: str, task_id: int | None = None) -> dict:
    when = ctx.parse_local(remind_at)
    if when < datetime.now(timezone.utc) - timedelta(minutes=1):
        raise ToolError(f"{ctx.fmt(when)} is in the past")
    with get_conn() as conn:
        if task_id is not None and not conn.execute(
            "SELECT 1 FROM tasks WHERE id = %s AND user_id = %s", (task_id, ctx.user_id)
        ).fetchone():
            raise ToolError(f"No task with id {task_id}")
        row = conn.execute(
            "INSERT INTO reminders (user_id, task_id, message, remind_at) VALUES (%s, %s, %s, %s) RETURNING id",
            (ctx.user_id, task_id, message, when),
        ).fetchone()
    return {"reminder_id": row["id"], "when": ctx.fmt(when)}


def _list_reminders(ctx: ToolContext) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, message, remind_at, task_id FROM reminders "
            "WHERE user_id = %s AND fired_at IS NULL ORDER BY remind_at LIMIT 100",
            (ctx.user_id,),
        ).fetchall()
    return {
        "reminders": [
            {"id": r["id"], "message": r["message"], "when": ctx.fmt(r["remind_at"]), "task_id": r["task_id"]}
            for r in rows
        ]
    }


def _cancel_reminder(ctx: ToolContext, reminder_id: int) -> dict:
    with get_conn() as conn:
        deleted = conn.execute(
            "DELETE FROM reminders WHERE id = %s AND user_id = %s AND fired_at IS NULL",
            (reminder_id, ctx.user_id),
        ).rowcount
    if not deleted:
        raise ToolError(f"No upcoming reminder with id {reminder_id}")
    return {"cancelled": reminder_id}


_HANDLERS = {
    "add_note": _add_note,
    "search_notes": _search_notes,
    "add_task": _add_task,
    "list_tasks": _list_tasks,
    "complete_task": _complete_task,
    "set_reminder": _set_reminder,
    "list_reminders": _list_reminders,
    "cancel_reminder": _cancel_reminder,
}
