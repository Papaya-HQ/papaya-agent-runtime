"""Lifecycle hook ingestion.

Provider harnesses can be configured to call ``ppy hook <event>`` at turn
boundaries (e.g. Claude Code Stop / SessionStart). The hook reads a JSON payload
on stdin, records a durable lifecycle event against the task/run, and prints a
JSON acknowledgement. This is the supervision seam for interactive managers; the
per-task runner already normalizes worker streams without hooks.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from typing import Any

from papaya_agent_runtime.state import init_db, store

LIFECYCLE_EVENTS = (
    "session_start",
    "stop",
    "session_end",
    "notification",
    "pre_tool",
    "post_tool",
)


def _normalize(event: str) -> str:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", event.strip())
    return snake.lower().replace("-", "_")


def handle_hook(event: str, payload: dict[str, Any]) -> dict:
    """Record a lifecycle hook and return provider-compatible steering."""
    normalized = _normalize(event)
    conn = init_db()
    task_id = payload.get("task_id")
    run_id = payload.get("run_id")
    if task_id is not None and run_id is None:
        task = store.get_task(conn, int(task_id))
        run_id = task["run_id"] if task else None
    store.append_event(
        conn,
        kind=f"hook_{normalized}",
        payload={"event": normalized, **payload},
        run_id=int(run_id) if run_id is not None else None,
        task_id=int(task_id) if task_id is not None else None,
    )
    # Hook stdout is a provider-facing protocol, not an acknowledgement channel.
    # Keep the durable event metadata in the database, but emit only fields that
    # Claude Code and Codex both understand. Unknown top-level keys make both
    # harnesses report the hook result as invalid JSON output.
    result: dict[str, Any] = {}

    # Codex and Claude both accept additional context at session start and a
    # blocking continuation at Stop. The stop-active guard is supplied by the
    # harness and prevents the continuation from recursively triggering itself.
    if normalized == "session_start":
        context = session_start_context(conn)
        if context:
            result["hookSpecificOutput"] = {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
    elif normalized == "stop" and not payload.get("stop_hook_active", False):
        reason = stop_block_reason(conn, payload)
        if reason:
            result.update({"decision": "block", "reason": reason})
    return result


def session_start_context(conn) -> str | None:
    """What a (re)starting manager needs to know before its first reply.

    Fires on a fresh session *and* after a compaction, so it carries the durable
    pickup context: open todos, live work, team health — plus any due assessment.
    """
    from papaya_agent_runtime import assessments, handoff

    parts: list[str] = []
    with contextlib.suppress(Exception):  # a hook must never break the harness
        parts.append(handoff.render_session_context(handoff.collect(conn)))
    assessment = assessments.hook_context(conn)
    if assessment:
        parts.append(assessment)
    return "\n\n".join(p for p in parts if p) or None


def stop_block_reason(conn, payload: dict[str, Any] | None = None) -> str | None:
    """Refuse to stop while work is open and no next step is recorded, or while the
    reply about to go out refers to things by internal label.

    These are the places a prompt-level habit becomes a hard rule. With tasks in
    flight or waiting on the manager, an empty todo ledger means "what's next" would
    be lost to the next compaction. And a reply full of "1B", "§5", "rev3" offloads
    the reader's context onto the user — the harness hands the reply back once, with
    the labels named, so it goes out described plainly. Both are skipped for
    framework-development sessions (``PPY_DEV``); the ledger gate also when nothing is
    open. All reasons found are returned together so one bounce covers everything.
    """
    from papaya_agent_runtime import assessments, board, plain_language

    assessment = assessments.hook_context(conn)
    cycle = assessments.latest_cycle(conn)
    if assessment and cycle is not None and cycle["status"] == "ready":
        return assessment
    if os.environ.get("PPY_DEV"):
        return None
    reasons: list[str] = []
    open_tasks = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status IN "
        "('requested','in_progress','worker_done','worker_stopped','blocked',"
        "'needs_recovery','failed')"
    ).fetchone()[0]
    if open_tasks and not board.open_todos(conn):
        reasons.append(
            f"{open_tasks} task(s) are open but no next step is recorded. Before stopping, "
            'write down what happens next so it survives compaction: `ppy todo add "..."` '
            "for each next step (use `--blocked-on user|review|task:<id>` for anything "
            "waiting on someone). Then reply to the user as you were going to."
        )
    with contextlib.suppress(Exception):  # a hook must never break the harness
        plain = plain_language.stop_reason_for(payload or {})
        if plain:
            reasons.append(plain)
    return "\n\n".join(reasons) or None


def handle_hook_stdin(event: str, raw: str) -> dict:
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"_raw": raw[:2000]}
    if not isinstance(payload, dict):
        payload = {"_raw": raw[:2000]}
    return handle_hook(event, payload)
