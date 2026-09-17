"""The manager's intent ledger (todos) and the work board rendered from it.

Task *lifecycle* is code-owned in ``tasks``/``events``. What was missing was the
manager's *intent*: acceptance criteria, next steps, what is waiting on whom. That
now lives in the ``todos`` table — structured, code-owned, and read by ``ppy status``,
``ppy handoff``, and the SessionStart hook — so "what's next" no longer depends on a
model remembering to edit a Markdown file.

The Markdown board (``.ppy/memory/tasks.md``) is a **projection**: ``render_board``
builds it from todos + live task states and every todo mutation rewrites it, so the
human-readable view is always current and never the source of truth.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from papaya_agent_runtime import memory
from papaya_agent_runtime.state import init_db, store

IN_FLIGHT = ("requested", "in_progress")
NEEDS_ME = ("worker_done", "worker_stopped", "blocked", "needs_recovery", "failed")

BLOCKERS = ("user", "review", "task", "access")


class TodoError(Exception):
    pass


def _normalize_blocker(blocked_on: str | None) -> str | None:
    if blocked_on is None:
        return None
    value = blocked_on.strip()
    if not value:
        return None
    head = value.split(":", 1)[0]
    if head not in BLOCKERS:
        raise TodoError(
            f"blocked-on must be one of {', '.join(BLOCKERS)} (optionally `task:<id>` / "
            f"`user:<what>`), got {blocked_on!r}"
        )
    if head == "task":
        try:
            int(value.split(":", 1)[1])
        except (IndexError, ValueError) as exc:
            raise TodoError("`task:<id>` needs a numeric task id") from exc
    return value


# --------------------------------------------------------------------------- #
# Ledger operations (each one re-projects the board)
# --------------------------------------------------------------------------- #


def add(
    text: str,
    *,
    run_id: int | None = None,
    task_id: int | None = None,
    blocked_on: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    conn = conn or init_db()
    text = text.strip()
    if not text:
        raise TodoError("a todo needs text")
    if run_id is not None and store.get_run(conn, run_id) is None:
        raise TodoError(f"run {run_id} not found")
    if task_id is not None and store.get_task(conn, task_id) is None:
        raise TodoError(f"task {task_id} not found")
    todo_id = store.add_todo(
        conn, text, run_id=run_id, task_id=task_id, blocked_on=_normalize_blocker(blocked_on)
    )
    write_board(conn)
    return todo_id


def _require(conn: sqlite3.Connection, todo_id: int) -> sqlite3.Row:
    row = store.get_todo(conn, todo_id)
    if row is None:
        raise TodoError(f"todo {todo_id} not found")
    return row


def done(todo_id: int, *, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or init_db()
    _require(conn, todo_id)
    store.update_todo(conn, todo_id, status="done", blocked_on=None)
    write_board(conn)


def drop(todo_id: int, *, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or init_db()
    _require(conn, todo_id)
    store.update_todo(conn, todo_id, status="dropped")
    write_board(conn)


def reopen(todo_id: int, *, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or init_db()
    _require(conn, todo_id)
    store.update_todo(conn, todo_id, status="open", done_at=None)
    write_board(conn)


def block(todo_id: int, on: str, *, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or init_db()
    _require(conn, todo_id)
    store.update_todo(conn, todo_id, blocked_on=_normalize_blocker(on))
    write_board(conn)


def unblock(todo_id: int, *, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or init_db()
    _require(conn, todo_id)
    store.update_todo(conn, todo_id, blocked_on=None)
    write_board(conn)


def edit(todo_id: int, text: str, *, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or init_db()
    _require(conn, todo_id)
    if not text.strip():
        raise TodoError("a todo needs text")
    store.update_todo(conn, todo_id, text=text.strip())
    write_board(conn)


def todo_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "text": row["text"],
        "status": row["status"],
        "blocked_on": row["blocked_on"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "done_at": row["done_at"],
    }


def open_todos(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    conn = conn or init_db()
    return [todo_dict(r) for r in store.list_todos(conn, status="open")]


def next_steps(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Open todos that are not blocked, in the order the manager would do them."""
    return [t for t in open_todos(conn) if not t["blocked_on"]]


def waiting(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Open todos blocked on someone or something."""
    return [t for t in open_todos(conn) if t["blocked_on"]]


def recently_done(conn: sqlite3.Connection | None = None, limit: int = 10) -> list[dict[str, Any]]:
    conn = conn or init_db()
    return [todo_dict(r) for r in store.list_todos(conn, status="done", limit=limit)]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def todo_line(t: dict[str, Any]) -> str:
    refs = []
    if t.get("run_id") is not None:
        refs.append(f"run {t['run_id']}")
    if t.get("task_id") is not None:
        refs.append(f"task {t['task_id']}")
    tail = f" ({', '.join(refs)})" if refs else ""
    if t.get("blocked_on"):
        tail += f" — waiting on {t['blocked_on']}"
    return f"- [#{t['id']}] {t['text']}{tail}"


def _live_tasks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    repos = {r["id"]: r["name"] for r in store.list_repos(conn)}
    marks = ",".join("?" for _ in (*IN_FLIGHT, *NEEDS_ME))
    # Worker tasks only (`store.WORKER_TASK`): a ticket placeholder is not in flight.
    rows = conn.execute(
        f"SELECT t.*, r.objective FROM tasks t JOIN runs r ON r.id = t.run_id "
        f"WHERE t.status IN ({marks}) AND t.{store.WORKER_TASK} ORDER BY t.run_id, t.id",
        (*IN_FLIGHT, *NEEDS_ME),
    ).fetchall()
    out = []
    for row in rows:
        latest = store.latest_progress(conn, row["id"])
        phase = None
        if latest is not None:
            import json

            try:
                phase = json.loads(latest["payload"]).get("phase")
            except (TypeError, ValueError):
                phase = None
        out.append(
            {
                "id": row["id"],
                "run_id": row["run_id"],
                "objective": row["objective"],
                "title": row["title"],
                "repo": repos.get(row["repo_id"]),
                "provider": row["provider"],
                "status": row["status"],
                "phase": phase,
            }
        )
    return out


def render_board(conn: sqlite3.Connection | None = None) -> str:
    conn = conn or init_db()
    todos = open_todos(conn)
    nxt = [t for t in todos if not t["blocked_on"]]
    blocked = [t for t in todos if t["blocked_on"]]
    live = _live_tasks(conn)
    in_flight = [t for t in live if t["status"] in IN_FLIGHT]
    needs_me = [t for t in live if t["status"] in NEEDS_ME]
    done_todos = recently_done(conn)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")

    out = [
        "# Work board (manager)",
        "",
        f"Generated by `ppy board` at {stamp} from `.ppy/state.db` — do not hand-edit; "
        "change it with `ppy todo add|done|block|unblock|drop` (intent) and the task "
        "lifecycle commands (state). `ppy status` / `ppy handoff` read the same source.",
        "",
        "## In flight",
    ]
    if in_flight:
        for t in in_flight:
            who = t["provider"] or "?"
            where = f" ({t['repo']})" if t["repo"] else ""
            phase = f" · phase: {t['phase']}" if t["phase"] else " · no plan posted yet"
            out.append(
                f'- task {t["id"]} "{t["title"]}"{where} [{who}] — {t["status"]}{phase} '
                f'(run {t["run_id"]} "{t["objective"]}")'
            )
    else:
        out.append("- nothing running")
    out += ["", "## Blocked / needs me"]
    if needs_me or blocked:
        for t in needs_me:
            where = f" ({t['repo']})" if t["repo"] else ""
            out.append(f'- task {t["id"]} "{t["title"]}"{where} — {t["status"]}')
        out.extend(todo_line(t) for t in blocked)
    else:
        out.append("- nothing waiting on me")
    out += ["", "## Next"]
    out.extend(todo_line(t) for t in nxt) if nxt else out.append(
        "- (no next steps recorded — `ppy todo add`)"
    )
    out += ["", "## Done (recent)"]
    out.extend(f"- [#{t['id']}] {t['text']}" for t in done_todos) if done_todos else out.append(
        "- nothing yet"
    )
    return "\n".join(out) + "\n"


def write_board(conn: sqlite3.Connection | None = None) -> str:
    """Project the board to ``.ppy/memory/tasks.md`` and return the text."""
    conn = conn or init_db()
    memory.ensure_memory_layout()
    text = render_board(conn)
    memory.board_path().write_text(text)
    return text
