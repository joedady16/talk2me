"""Conversation loop: user text -> Claude (with tools) -> spoken reply.

The API history for each conversation is stored verbatim in conversations.api_messages and only
ever appended to. That keeps the prompt-cache prefix stable and keeps replayed thinking blocks
valid (editing earlier turns, or rebuilding `system`/`tools`, would invalidate them).
"""

import logging
from datetime import datetime
from functools import lru_cache

import anthropic
from psycopg.types.json import Jsonb

from ..auth import User
from ..config import settings
from ..db import get_conn
from .tools import TOOLS, ToolContext, ToolError, run_tool

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8

# Frozen text: per-turn details (current time) go in the user message instead, so this stays cacheable.
SYSTEM_PROMPT = """\
You are Talk2Me, a personal voice assistant. The user talks to you to capture notes, tasks and \
reminders, and to ask what they have on their plate.

Your replies are read aloud by a text-to-speech voice, so:
- Keep replies short: usually one to three sentences.
- Write plain conversational sentences. No markdown, bullet points, emoji, or URLs.
- Say dates and times the way a person would ("Tuesday at 9 AM", "in two hours").

Each user message begins with the current local date and time in square brackets. Use it to \
resolve relative times like "tomorrow morning" or "next Friday", and pass times to tools in the \
user's local time. If a time is ambiguous ("remind me later"), ask a brief follow-up question \
instead of guessing.

Use the tools to act rather than describing what you would do. Look up ids with list_tasks or \
list_reminders instead of guessing them. After an action, confirm it briefly. When the user \
finishes a task, ask how it went if they haven't said, and record the answer as the outcome."""


@lru_cache(maxsize=1)
def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(timeout=90.0)


class AssistantUnavailable(Exception):
    pass


def _open_conversation(conn, user_id: int) -> dict:
    """Return the user's active conversation (row-locked), starting a new one after inactivity."""
    conv = conn.execute(
        "SELECT id, api_messages FROM conversations "
        "WHERE user_id = %s AND ended_at IS NULL AND last_activity_at > now() - make_interval(mins => %s) "
        "ORDER BY last_activity_at DESC LIMIT 1 FOR UPDATE",
        (user_id, settings.conversation_idle_minutes),
    ).fetchone()
    if conv:
        return conv
    conn.execute(
        "UPDATE conversations SET ended_at = last_activity_at WHERE user_id = %s AND ended_at IS NULL", (user_id,)
    )
    return conn.execute(
        "INSERT INTO conversations (user_id) VALUES (%s) RETURNING id, api_messages", (user_id,)
    ).fetchone()


def _execute_tools(ctx: ToolContext, tool_uses: list) -> list[dict]:
    results = []
    for block in tool_uses:
        try:
            content, is_error = run_tool(ctx, block.name, block.input), False
        except (ToolError, TypeError) as exc:
            content, is_error = f"Error: {exc}", True
        except Exception:
            log.exception("Tool %s failed", block.name)
            content, is_error = "Error: the tool failed unexpectedly.", True
        log.info("tool %s(%s) -> %s", block.name, block.input, content[:200])
        results.append({"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error})
    return results


def respond(user: User, text: str) -> str:
    if not settings.anthropic_configured:
        raise AssistantUnavailable("Set ANTHROPIC_API_KEY in .env to enable the assistant")

    ctx = ToolContext(user.id, user.timezone)
    client = _client()
    with get_conn() as conn:
        # The row lock serializes concurrent turns for the same user so history stays append-only.
        conv = _open_conversation(conn, user.id)
        history = conv["api_messages"]
        stamp = datetime.now(ctx.tz).strftime("%A %b %-d %Y, %-I:%M %p %Z")
        new_messages: list[dict] = [{"role": "user", "content": f"[{stamp}] {text}"}]
        reply = ""

        for _ in range(MAX_TOOL_ROUNDS):
            response = client.beta.messages.create(
                model=settings.claude_model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=history + new_messages,
                output_config={"effort": settings.claude_effort},
                cache_control={"type": "ephemeral"},
                # Re-run on Anthropic's recommended model if a safety classifier declines.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if response.stop_reason == "refusal":
                reply = "Sorry, I can't help with that one."
                break

            new_messages.append({"role": "assistant", "content": [b.to_dict() for b in response.content]})
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                reply = " ".join(b.text for b in response.content if b.type == "text").strip()
                break
            new_messages.append({"role": "user", "content": _execute_tools(ctx, tool_uses)})
        else:
            reply = "Sorry, that took more steps than I expected. Could you try again?"

        reply = reply or "Done."
        conn.execute(
            "UPDATE conversations SET api_messages = api_messages || %s, last_activity_at = now() WHERE id = %s",
            (Jsonb(new_messages), conv["id"]),
        )
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s, 'user', %s), (%s, 'assistant', %s)",
            (conv["id"], text, conv["id"], reply),
        )
    return reply


def current_transcript(user_id: int) -> list[dict]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT m.role, m.content, m.created_at FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE c.user_id = %s AND c.ended_at IS NULL "
            "AND c.last_activity_at > now() - make_interval(mins => %s) ORDER BY m.id",
            (user_id, settings.conversation_idle_minutes),
        ).fetchall()


def end_conversation(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE conversations SET ended_at = now() WHERE user_id = %s AND ended_at IS NULL", (user_id,))
