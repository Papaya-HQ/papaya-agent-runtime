"""What a re-dispatch owes the worker about the attempt before it.

A brief written from the contract alone sends a second worker down the first
one's path; in Middle Manager a rejection of the prior attempt reached the next
worker only through a repo note, mid-task. So a dispatch whose title a closed,
failed or cancelled task in the same repository already carried is a
*re-dispatch*, and the brief lint asks for a ``## Prior attempt`` section
(:func:`papaya_agent_runtime.brief_lint.prior_attempt_findings`).

Derived from the task rows and their events as they are: nothing is recorded
here, and there is no column linking an attempt to the one before it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from papaya_agent_runtime.state import store

#: A prior task in one of these is over; the new dispatch is trying again. Not
#: `delivered`: that work shipped, and doing it again is a new objective.
ENDED_STATUSES = ("closed", "failed", "cancelled")


@dataclass(frozen=True)
class PriorAttempt:
    task_id: int
    title: str
    status: str
    ended_at: str | None
    reason: str | None

    def describe(self) -> str:
        when = f" on {self.ended_at[:10]}" if self.ended_at else ""
        why = f": {self.reason}" if self.reason else ""
        return f'task {self.task_id} "{self.title}" ended {self.status}{when}{why}'


def _normalise(title: str | None) -> str:
    return " ".join((title or "").split()).lower()


def find(conn: sqlite3.Connection, repo_id: int | None, title: str) -> list[sqlite3.Row]:
    """Ended tasks in this repository with this title, however spaced or cased, newest first."""
    wanted = _normalise(title)
    if not repo_id or not wanted:
        return []
    marks = ",".join("?" for _ in ENDED_STATUSES)
    rows = conn.execute(
        f"SELECT * FROM tasks WHERE repo_id = ? AND status IN ({marks}) ORDER BY id DESC",
        (repo_id, *ENDED_STATUSES),
    ).fetchall()
    return [row for row in rows if _normalise(row["title"]) == wanted]


def summarize(conn: sqlite3.Connection, task: sqlite3.Row) -> PriorAttempt:
    """How a prior task ended and why, from its own events."""
    reason = None
    ended_at = None
    row = conn.execute(
        "SELECT payload, created_at FROM events WHERE task_id = ? AND kind IN "
        "('task_closed', 'error', 'status_set') ORDER BY id DESC LIMIT 1",
        (task["id"],),
    ).fetchone()
    if row is not None:
        ended_at = row["created_at"]
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            payload = {}
        if isinstance(payload, dict):
            reason = payload.get("reason") or payload.get("summary") or payload.get("note")
    return PriorAttempt(
        task_id=int(task["id"]),
        title=task["title"],
        status=task["status"],
        ended_at=ended_at or task["updated_at"],
        reason=reason,
    )


def for_repo(conn: sqlite3.Connection, repo: str, title: str) -> PriorAttempt | None:
    """The newest prior attempt at ``title`` in the registered repo ``repo``, or None."""
    row = store.get_repo(conn, repo)
    if row is None:
        return None
    found = find(conn, row["id"], title)
    return summarize(conn, found[0]) if found else None


def describe(conn: sqlite3.Connection, repo: str, title: str) -> str | None:
    """One line naming the prior attempt, for the brief lint; None for a first try."""
    prior = for_repo(conn, repo, title)
    return prior.describe() if prior is not None else None
