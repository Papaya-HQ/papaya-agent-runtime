"""What the manager owes its workers right now, whichever way the runtime is running.

A worker that finished, stopped, failed or asked a question is waiting on the manager.
`ppy serve` used to act on that only for a held Papaya ticket, and an interactive
session only when it happened to look. On 2026-09-17 two workers dispatched by hand
crashed on their first denied command and nobody heard for eight hours, a finished
worker waited fourteen hours for its review, and a stopped one twenty-one: none of
them had a held ticket, and no heartbeat was running in the session that dispatched
them.

This module is the one answer to "what is owed", read the same way by every surface:

- the heartbeat (`ppy watch`) lists every owed task, failures included;
- the session hooks put the list in front of an interactive manager when it starts,
  and refuse to let a turn end while an owed task has no next step recorded against it
  or no heartbeat is running to hear the next one;
- `ppy status --team` lists it under "waiting on a person";
- readiness raises each one nobody took up within :data:`GRACE_SECONDS` as a problem a
  person owns, which `ppy serve`'s blocker watch reports to them, so a machine with no
  interactive session still tells somebody.

A task whose Papaya ticket is still being worked belongs to `ppy serve`'s ticket runner
and rounds, which already act on it; it is listed, marked with its ticket, and never
raised as a person's problem. Everything here reads; nothing writes.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Worker statuses that mean the manager owes the task its next move.
OWED_STATUSES = ("worker_done", "worker_stopped", "blocked", "needs_recovery", "failed")

#: Statuses of a worker that is still running (or about to).
RUNNING_STATUSES = ("requested", "in_progress")

#: How long a task may sit owed before it is a person's problem, not a turn's next step.
GRACE_SECONDS = 15 * 60.0

#: A ticket in one of these phases is no longer being worked by `ppy serve`.
TICKET_ENDED = frozenset(
    {"released", "handed_back", "stalled", "declined", "handed_over", "done", "needs_a_person"}
)

#: The readiness problem code for owed work nobody has taken up.
PROBLEM_CODE = "worker_waiting_on_manager"

#: The file `ppy watch` keeps while it runs, so a hook can tell a heartbeat is armed.
WATCH_PID_FILE = "watch.pid"


@dataclass(frozen=True)
class Owed:
    """One task waiting on the manager, said the way a person reads it."""

    task_id: int
    status: str
    title: str
    repo: str | None
    #: Why it is owed, in one line: the error, the stop reason, the question, the done note.
    reason: str
    #: The command that takes it up.
    next_step: str
    #: Seconds since the task last changed.
    seconds: float | None
    #: The ticket task `ppy serve` is working it under, when that ticket is still live.
    ticket_task_id: int | None = None

    @property
    def serve_owns(self) -> bool:
        return self.ticket_task_id is not None

    def line(self) -> str:
        where = f" ({self.repo})" if self.repo else ""
        ticket = f" [ticket task {self.ticket_task_id}]" if self.ticket_task_id else ""
        return (
            f"worker task {self.task_id}{where} {_WORDS.get(self.status, self.status)}: "
            f"{self.reason} — next: {self.next_step}{ticket}"
        )

    def public(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "title": self.title,
            "repo": self.repo,
            "reason": self.reason,
            "next_step": self.next_step,
            "seconds": self.seconds,
            "ticket_task_id": self.ticket_task_id,
        }


_WORDS = {
    "worker_done": "finished, waiting on review",
    "worker_stopped": "stopped",
    "blocked": "is asking a question",
    "needs_recovery": "lost its process",
    "failed": "failed",
}

_NEXT = {
    "worker_done": "`ppy review {id}`",
    "worker_stopped": "read `ppy task show {id}`, then `ppy resume {id}` or `ppy task close {id}`",
    "blocked": '`ppy answer {id} --answer "..."`',
    "needs_recovery": "`ppy resume {id}`",
    "failed": "read `ppy task show {id}`, then `ppy resume {id}` or `ppy task close {id}`",
}

_EMPTY_REASON = {
    "worker_done": "no done note was filed",
    "worker_stopped": "no reason was recorded",
    "blocked": "the question is in its progress log",
    "needs_recovery": "its runner process is gone",
    "failed": "no error was recorded",
}

#: Event kinds whose payload says why, newest first, per status.
_REASON_EVENTS = {
    "failed": ("error", "worker_result"),
    "worker_stopped": ("worker_stopped",),
    "needs_recovery": ("error", "worker_stopped"),
}


def _clip(text: object, width: int = 180) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= width else one[: width - 1] + "…"


def _payload(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        value = json.loads(row["payload"])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _newest_event(conn: sqlite3.Connection, task_id: int, kinds: tuple[str, ...]) -> sqlite3.Row:
    marks = ",".join("?" for _ in kinds)
    return conn.execute(
        f"SELECT payload FROM events WHERE task_id = ? AND kind IN ({marks}) "
        "ORDER BY id DESC LIMIT 1",
        (task_id, *kinds),
    ).fetchone()


def _reason(conn: sqlite3.Connection, task_id: int, status: str) -> str:
    kinds = _REASON_EVENTS.get(status)
    if kinds:
        payload = _payload(_newest_event(conn, task_id, kinds))
        said = payload.get("summary") or payload.get("reason") or payload.get("detail")
        if said:
            return _clip(said)
    note = _payload(_newest_event(conn, task_id, ("worker_progress",)))
    if note.get("note"):
        return _clip(note["note"])
    return _EMPTY_REASON.get(status, "")


def _seconds(now: datetime, stamp: object) -> float | None:
    try:
        then = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return max(0.0, (now - then).total_seconds())


def _live_tickets(conn: sqlite3.Connection) -> dict[int, int]:
    """``{run id: ticket task id}`` for every ticket `ppy serve` is still working."""
    from papaya_agent_runtime import papaya_events

    rows = conn.execute(
        "SELECT tasks.id, tasks.run_id, tasks.phase FROM tasks "
        "JOIN task_env ON task_env.task_id = tasks.id "
        "WHERE task_env.key = ? ORDER BY tasks.id DESC",
        (papaya_events.PAPAYA_EVENT_METADATA,),
    ).fetchall()
    live: dict[int, int] = {}
    for row in rows:
        run_id = int(row["run_id"])
        if run_id in live or row["phase"] in TICKET_ENDED:
            continue
        live[run_id] = int(row["id"])
    return live


def collect(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[Owed]:
    """Every worker task waiting on the manager, oldest first."""
    now = now or datetime.now(UTC)
    marks = ",".join("?" for _ in OWED_STATUSES)
    rows = conn.execute(
        "SELECT t.id, t.run_id, t.status, t.title, t.updated_at, r.name AS repo "
        "FROM tasks t LEFT JOIN repos r ON r.id = t.repo_id "
        f"WHERE t.phase IS NULL AND t.status IN ({marks}) ORDER BY t.updated_at, t.id",
        OWED_STATUSES,
    ).fetchall()
    tickets = _live_tickets(conn) if rows else {}
    owed = []
    for row in rows:
        task_id, status = int(row["id"]), str(row["status"])
        reason, next_step = _reason(conn, task_id, status), _NEXT[status].format(id=task_id)
        if status in ("worker_done", "worker_stopped"):
            # The gate follow-up serve's runner makes on the same worker (`supervision`).
            from papaya_agent_runtime import supervision

            followup = supervision.gate_followup(
                task_id, stopped=status == "worker_stopped", detail=reason
            )
            reason = f"{reason} [{followup.line}]"
            if followup.action == supervision.STEER:
                next_step = f"send it back: `ppy followup {task_id} --send` ({followup.line})"
            elif followup.action == supervision.PERSON:
                next_step = f"decide on it: {followup.line} (`ppy task show {task_id}`)"
        owed.append(
            Owed(
                task_id=task_id,
                status=status,
                title=str(row["title"] or ""),
                repo=row["repo"],
                reason=reason,
                next_step=next_step,
                seconds=_seconds(now, row["updated_at"]),
                ticket_task_id=tickets.get(int(row["run_id"])),
            )
        )
    return owed


def running_count(conn: sqlite3.Connection) -> int:
    """Worker tasks still running or about to, with no live ticket of their own."""
    marks = ",".join("?" for _ in RUNNING_STATUSES)
    rows = conn.execute(
        f"SELECT run_id FROM tasks WHERE phase IS NULL AND status IN ({marks})",
        RUNNING_STATUSES,
    ).fetchall()
    if not rows:
        return 0
    tickets = _live_tickets(conn)
    return sum(1 for row in rows if int(row["run_id"]) not in tickets)


def untracked(conn: sqlite3.Connection, owed: list[Owed]) -> list[Owed]:
    """The owed tasks with no live ticket and no open todo recorded against the task."""
    tracked = {
        int(row["task_id"])
        for row in conn.execute(
            "SELECT task_id FROM todos WHERE status = 'open' AND task_id IS NOT NULL"
        ).fetchall()
    }
    return [item for item in owed if not item.serve_owns and item.task_id not in tracked]


def overdue(owed: list[Owed], grace_seconds: float = GRACE_SECONDS) -> list[Owed]:
    """The owed tasks no live ticket covers that have waited past the grace."""
    return [
        item
        for item in owed
        if not item.serve_owns and item.seconds is not None and item.seconds >= grace_seconds
    ]


# ── the heartbeat's own liveness ────────────────────────────────────────────


def watch_pid_path() -> Path:
    from papaya_agent_runtime.paths import run_dir

    return run_dir() / WATCH_PID_FILE


def mark_watch_running(pid: int | None = None) -> None:
    """Record that a heartbeat is running in this process. Never raises."""
    try:
        path = watch_pid_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(pid or os.getpid()))
    except OSError:
        pass


def clear_watch_mark(pid: int | None = None) -> None:
    """Forget this process's heartbeat mark, leaving another process's alone."""
    try:
        path = watch_pid_path()
        if path.read_text().strip() == str(pid or os.getpid()):
            path.unlink()
    except (OSError, ValueError):
        pass


def watch_running() -> bool:
    """Is a `ppy watch` heartbeat alive on this machine right now?"""
    try:
        pid = int(watch_pid_path().read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
