"""Worker reflections: the team's voice in the manager's performance review.

A worker that has finished (or given up on) a task files two short notes with
``ppy reflect <task_id> --self "..." --manager "..."``:

- **self** — its own assessment: what it learned, what went well or badly, what it
  would do differently, what would have made the task easier;
- **manager** — its assessment of the manager: was the brief clear and complete, was
  the scope right, did steering and review help or hurt, what should change.

Each reflection is an event of kind ``worker_reflection`` on the task, so it is
durable, attributable, and visible to the manager (``ppy reflect <task_id>`` shows
them). The assessment evidence packet (:mod:`papaya_agent_runtime.assessments`) carries
every reflection filed since the previous review, so the workers' learnings and
their view of the manager are first-class input to the next cycle — not something
the manager has to remember to ask for.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from papaya_agent_runtime.state import init_db, store

KIND = "worker_reflection"
MAX_NOTE_CHARS = 4000


class ReflectionError(Exception):
    pass


def _clean(text: str | None, label: str) -> str:
    value = (text or "").strip()
    if len(value) > MAX_NOTE_CHARS:
        raise ReflectionError(f"{label} note must be at most {MAX_NOTE_CHARS} characters")
    return value


def record(
    task_id: int,
    *,
    self_note: str | None = None,
    manager_note: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """File a reflection on ``task_id``. At least one of the two notes is required."""
    conn = conn or init_db()
    task = store.get_task(conn, task_id)
    if task is None:
        raise ReflectionError(f"task {task_id} not found")
    self_text = _clean(self_note, "self")
    manager_text = _clean(manager_note, "manager")
    if not (self_text or manager_text):
        raise ReflectionError("a reflection needs --self and/or --manager text")
    return store.append_event(
        conn,
        kind=KIND,
        payload={"task_id": task_id, "self": self_text, "manager": manager_text},
        run_id=task["run_id"],
        task_id=task_id,
    )


def _entry(row: sqlite3.Row, titles: dict[int, str] | None = None) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {}
    task_id = row["task_id"]
    return {
        "event_id": row["id"],
        "task_id": task_id,
        "run_id": row["run_id"],
        "title": (titles or {}).get(task_id),
        "self": payload.get("self", ""),
        "manager": payload.get("manager", ""),
        "at": row["created_at"],
    }


def history(task_id: int, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Every reflection filed on one task, oldest first."""
    conn = conn or init_db()
    rows = conn.execute(
        "SELECT * FROM events WHERE task_id = ? AND kind = ? ORDER BY id ASC", (task_id, KIND)
    ).fetchall()
    return [_entry(r) for r in rows]


def since(
    start_iso: str, *, conn: sqlite3.Connection | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Reflections filed at or after ``start_iso``, oldest first, with task titles."""
    conn = conn or init_db()
    rows = conn.execute(
        "SELECT * FROM events WHERE kind = ? AND created_at >= ? ORDER BY id ASC LIMIT ?",
        (KIND, start_iso, limit),
    ).fetchall()
    task_ids = sorted({int(r["task_id"]) for r in rows if r["task_id"] is not None})
    titles: dict[int, str] = {}
    if task_ids:
        marks = ",".join("?" for _ in task_ids)
        titles = {
            int(t["id"]): t["title"]
            for t in conn.execute(
                f"SELECT id, title FROM tasks WHERE id IN ({marks})", task_ids
            ).fetchall()
        }
    return [_entry(r, titles) for r in rows]


def render(entries: list[dict[str, Any]], *, heading: str | None = None) -> str:
    """Human-readable reflections, one block per entry."""
    out: list[str] = []
    if heading:
        out += [heading, ""]
    if not entries:
        out.append("- no reflections filed")
        return "\n".join(out) + "\n"
    for e in entries:
        title = f' "{e["title"]}"' if e.get("title") else ""
        out.append(f"- {e['at']} · task {e['task_id']}{title}")
        if e["self"]:
            out.append(f"  self: {e['self']}")
        if e["manager"]:
            out.append(f"  manager: {e['manager']}")
    return "\n".join(out) + "\n"
