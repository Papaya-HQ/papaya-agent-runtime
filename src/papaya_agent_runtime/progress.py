"""Worker progress as structured events, not prose.

A dispatched worker reports where it is with ``ppy progress <task_id> --phase <phase>
--note "..."``. Each report is an event of kind ``worker_progress`` on the task, so:

- the manager's peek is ``ppy task <id>`` (latest phase + note), not a file read;
- ``ppy memory show --repo <name>`` renders the repo's progress log from events;
- the health poller's "last heard" gets a semantic signal, and can tell that a
  worker never posted a plan (``plan_missing``, raised once per task after a grace
  period — see :mod:`papaya_agent_runtime.health`).

The per-repo ``tasks.md`` stays a free-form place for durable follow-ups, backlog, and
tech debt; the running narrative moves here.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from papaya_agent_runtime.state import init_db, store

PHASES = ("plan", "implement", "test", "review", "blocked", "done")

#: A manager's note for a worker that is not a steer (:func:`post_guidance`).
GUIDANCE_EVENT = "progress_guidance"


class ProgressError(Exception):
    pass


def record(
    task_id: int,
    *,
    phase: str,
    note: str = "",
    conn: sqlite3.Connection | None = None,
) -> int:
    conn = conn or init_db()
    if phase not in PHASES:
        raise ProgressError(f"phase must be one of {', '.join(PHASES)}, got {phase!r}")
    task = store.get_task(conn, task_id)
    if task is None:
        raise ProgressError(f"task {task_id} not found")
    ends_at = task["ends_at"] if "ends_at" in task.keys() else "done"  # noqa: SIM118
    if phase == "done" and ends_at == "review":
        raise ProgressError(
            f"task {task_id} was dispatched with --ends-at review; --phase done is refused. "
            "Stop at --phase review so the manager can review and deliver it"
        )
    if phase == "plan" and not note.strip():
        raise ProgressError("a plan report needs a note describing the approach")
    # A worker that says COMPOSE_PROJECT_NAME=... has just told us which stack to
    # tear down when the task ends; record it rather than make anyone re-state it.
    from papaya_agent_runtime import compose

    compose.note_project(task_id, note, conn=conn)
    _observe_timing(conn, task, phase)
    return store.append_event(
        conn,
        kind="worker_progress",
        payload={"task_id": task_id, "phase": phase, "note": note.strip()},
        run_id=task["run_id"],
        task_id=task_id,
    )


def post_guidance(task_id: int, note: str, *, conn: sqlite3.Connection | None = None) -> int:
    """Leave a note for a worker that is not a steer: its next progress report hands it over.

    A check-in's `continue, note <text>` (a reminder to post the plan, say) lands here.
    Nothing interrupts the worker and nothing counts as the manager acting on it.
    """
    own = conn is None
    conn = conn or init_db()
    try:
        task = store.get_task(conn, task_id)
        if task is None:
            raise ProgressError(f"task {task_id} not found")
        return store.append_event(
            conn,
            kind=GUIDANCE_EVENT,
            payload={"task_id": task_id, "note": note.strip(), "by": store.BY_MANAGER},
            run_id=task["run_id"],
            task_id=task_id,
        )
    finally:
        if own:
            conn.close()


def guidance_for(
    task_id: int, report_event_id: int, *, conn: sqlite3.Connection | None = None
) -> list[str]:
    """The notes left since the report before ``report_event_id``, oldest first."""
    own = conn is None
    conn = conn or init_db()
    try:
        previous = conn.execute(
            "SELECT MAX(id) AS id FROM events WHERE task_id = ? AND id < ? "
            f"AND {store.PROGRESS_NOTE}",
            (task_id, report_event_id),
        ).fetchone()
        rows = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = ? AND id > ? AND id < ? "
            "ORDER BY id",
            (task_id, GUIDANCE_EVENT, int(previous["id"] or 0), report_event_id),
        ).fetchall()
    finally:
        if own:
            conn.close()
    notes = []
    for row in rows:
        try:
            note = str(json.loads(row["payload"]).get("note") or "").strip()
        except (TypeError, ValueError, AttributeError):
            continue
        if note:
            notes.append(note)
    return notes


def _parse_stamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _stalled_since(conn: sqlite3.Connection, task_id: int, after_event_id: int) -> bool:
    """Was this worker found stalled since ``after_event_id``: quiet, stopped, or checked in on?"""
    row = conn.execute(
        "SELECT 1 FROM events WHERE id > ? AND ("
        "(task_id = ? AND kind IN ('worker_quiet', 'worker_stopped')) OR "
        "(kind = 'ticket_round' AND json_valid(payload) "
        "AND json_extract(payload, '$.action') = 'checkin' "
        "AND json_extract(payload, '$.trigger') = 'quiet' "
        "AND json_extract(payload, '$.worker_task_id') = ?)) LIMIT 1",
        (after_event_id, task_id, task_id),
    ).fetchone()
    return row is not None


def _observe_timing(conn: sqlite3.Connection, task: sqlite3.Row, phase: str) -> None:
    """Keep the silence this note ends and, for the first note past `plan`, the plan phase.

    Silence is measured from the previous note (from dispatch for the first). An
    interval in which the worker went quiet, stopped, or was checked on for silence
    is kept flagged ``stall``: it is what the silence budget exists to catch, not a
    sample of how long this repository's workers normally go between notes. Time a
    gate of this worker spent queued behind another is left out: that wait is the
    machine's, not the worker's silence.
    """
    from papaya_agent_runtime import budgets, gate

    try:
        now = datetime.now(UTC)
        task_id = int(task["id"])
        created = _parse_stamp(task["created_at"])
        previous = store.progress_events(conn, task_id=task_id)
        last = previous[0] if previous else None
        since = _parse_stamp(last["created_at"]) if last is not None else created
        if since is not None:
            stalled = _stalled_since(conn, task_id, int(last["id"]) if last is not None else 0)
            queued = gate.queued_seconds(conn, task_id, since, now)
            budgets.observe_task(
                task_id,
                budgets.SILENCE,
                max(0.0, (now - since).total_seconds() - queued),
                outcome=budgets.STALL if stalled else "",
                at=now,
                conn=conn,
            )
        past_plan = any(_entry(row)["phase"] != "plan" for row in previous)
        if phase != "plan" and not past_plan and created is not None:
            budgets.observe_task(
                task_id, budgets.PLAN, (now - created).total_seconds(), at=now, conn=conn
            )
    except (sqlite3.Error, ValueError, TypeError):
        return


def _entry(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {}
    return {
        "event_id": row["id"],
        "task_id": row["task_id"],
        "phase": payload.get("phase"),
        "note": payload.get("note", ""),
        "at": row["created_at"],
    }


def latest(task_id: int, *, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    conn = conn or init_db()
    row = store.latest_progress(conn, task_id)
    return _entry(row) if row is not None else None


def history(task_id: int, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    conn = conn or init_db()
    return [_entry(r) for r in store.progress_events(conn, task_id=task_id)]


def render_repo_log(
    repo_name: str, *, conn: sqlite3.Connection | None = None, limit: int = 40
) -> str:
    """The repo's progress log, newest first, rendered from events."""
    conn = conn or init_db()
    repo = store.get_repo(conn, repo_name)
    if repo is None:
        return f"# {repo_name} — progress log\n\n(repo not registered)\n"
    rows = store.progress_events(conn, repo_id=repo["id"])[:limit]
    titles = {
        t["id"]: t["title"]
        for t in conn.execute(
            "SELECT id, title FROM tasks WHERE repo_id = ?", (repo["id"],)
        ).fetchall()
    }
    out = [f"# {repo_name} — progress log (from `ppy progress`, newest first)", ""]
    if not rows:
        out.append("- no progress reported yet")
    for row in rows:
        e = _entry(row)
        title = titles.get(e["task_id"], "?")
        note = f" — {e['note']}" if e["note"] else ""
        out.append(f'- {e["at"]} · task {e["task_id"]} "{title}" · {e["phase"]}{note}')
    return "\n".join(out) + "\n"


def describe(entry: dict[str, Any] | None) -> str:
    if entry is None:
        return "no progress reported"
    note = f" — {entry['note']}" if entry.get("note") else ""
    return f"{entry['phase']}{note} (at {entry['at']})"
