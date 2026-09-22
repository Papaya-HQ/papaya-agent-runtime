"""Running without Papaya: a mode, not a blocker.

A machine with no Papaya connection is still a manager. It registers and onboards
repositories, briefs, dispatches, reviews, delivers and follows what it delivered,
exactly as a connected one does. Two things differ, and this module owns both:

- **The invitation.** Once per session (and once per `ppy serve` start) the runtime
  says, in one plain line, that it is better with Papaya, and offers to set it up:
  `ppy papaya connect` installs the client if it is missing and the person clicks
  Approve. An offer, not a nag: said once, never repeated on later commands, never a
  comment, and `PPY_QUIET_INVITE=1` or config ``papaya.invite = false`` turns even
  that line off.
- **The ticket steps.** A task with no Papaya work item behind it (created locally)
  has no item to set a status on, comment on, or write acceptance criteria to. Those
  steps are skipped silently, and the fact is recorded on the task as one
  :data:`TICKET_STEP_SKIPPED` event per phase, so the ledger says what a connected
  ticket would have had and nothing reports it as a failure.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

#: The one line. Kept to one line on purpose: a host shows the last thing said.
INVITE_LINE = (
    "Running without Papaya. It's better with it: tickets, comments and the team's "
    "record flow in and out by themselves. `ppy papaya connect` sets it up: it installs "
    "the client if needed and you click Approve in your browser. https://trypapaya.ai"
)

#: Set to anything but ``0``/``false``/empty to silence the invitation.
QUIET_ENV = "PPY_QUIET_INVITE"

#: The event a skipped ticket step leaves on a task.
TICKET_STEP_SKIPPED = "ticket_step_skipped"

#: Why a step was skipped.
NO_WORK_ITEM = "no Papaya work item: the task was created locally"
NOT_CONNECTED = "not connected to Papaya"

#: The steps a ticket gets at each phase of a task's life, and a local task does not.
#: Mirrors what `ppy serve`'s ticket runner and rounds do on a work item.
DISPATCHED = "dispatched"
REVIEWED = "reviewed"
DELIVERED = "delivered"
MERGED = "merged"
TICKET_STEPS: dict[str, tuple[str, ...]] = {
    DISPATCHED: (
        "write acceptance criteria on the work item",
        "set the work item to in_progress",
        "comment that a worker was dispatched",
    ),
    REVIEWED: ("comment the review report on the work item",),
    DELIVERED: (
        "comment the pull request on the work item",
        "set the work item to in_review",
    ),
    MERGED: (
        "comment that the pull request merged",
        "set the work item to done",
    ),
}


def connected() -> bool:
    """Is this machine pinned to a Papaya agent? Local files only; never raises."""
    from papaya_agent_runtime import papaya

    try:
        return papaya.identity() is not None
    except Exception:  # noqa: BLE001 - an unreadable connection is no connection
        return False


def invite_enabled() -> bool:
    """Whether the invitation may be said at all."""
    quiet = os.environ.get(QUIET_ENV, "").strip().lower()
    if quiet and quiet not in ("0", "false", "no"):
        return False
    from papaya_agent_runtime.config import ConfigError, load_config
    from papaya_agent_runtime.paths import config_path

    if not config_path().exists():
        return True
    try:
        return bool(load_config().papaya.invite)
    except (ConfigError, OSError, TypeError):
        return True


def invitation() -> str | None:
    """The line to say, or ``None`` when connected or silenced."""
    if connected() or not invite_enabled():
        return None
    return INVITE_LINE


def say_invitation(stream: Any, *, prefix: str = "") -> bool:
    """Print the invitation once to ``stream`` if it applies. Never raises."""
    try:
        line = invitation()
        if line is None:
            return False
        print(f"{prefix}{line}", file=stream, flush=True)
    except Exception:  # noqa: BLE001 - an invitation must never be why a command failed
        return False
    return True


# ── ticket steps a local task does not have ─────────────────────────────────


def has_work_item(conn: sqlite3.Connection, task_id: int) -> bool:
    """Is there a Papaya work item behind this task, or behind the run it was filed in?

    A worker dispatched for a ticket is filed in the ticket's run, and the ticket
    runner does the item's steps; only a run with no Papaya event at all is local.
    """
    from papaya_agent_runtime.papaya_events import PAPAYA_EVENT_KEY, PAPAYA_EVENT_METADATA

    row = conn.execute(
        "SELECT 1 FROM task_env JOIN tasks ON tasks.id = task_env.task_id "
        "WHERE task_env.key IN (?, ?) AND tasks.run_id = "
        "(SELECT run_id FROM tasks WHERE id = ?) LIMIT 1",
        (PAPAYA_EVENT_KEY, PAPAYA_EVENT_METADATA, task_id),
    ).fetchone()
    return row is not None


def record_skipped(
    conn: sqlite3.Connection,
    task_id: int,
    phase: str,
    *,
    steps: tuple[str, ...] | list[str] | None = None,
    reason: str = NO_WORK_ITEM,
) -> None:
    """Record, on the task, the ticket steps this phase skipped. Commits; never raises."""
    from papaya_agent_runtime.state import store

    try:
        task = store.get_task(conn, task_id)
        if task is None:
            return
        store.append_event(
            conn,
            kind=TICKET_STEP_SKIPPED,
            payload={
                "task_id": task_id,
                "phase": phase,
                "steps": list(steps if steps is not None else TICKET_STEPS.get(phase, ())),
                "reason": reason,
            },
            run_id=task["run_id"],
            task_id=task_id,
        )
    except Exception:  # noqa: BLE001 - bookkeeping of a skip must never fail the step itself
        return


def skip_if_local(conn: sqlite3.Connection, task_id: int, phase: str) -> bool:
    """For a local task, record the ticket steps ``phase`` would have had. Never raises."""
    try:
        if has_work_item(conn, task_id):
            return False
    except Exception:  # noqa: BLE001 - an unreadable task is not worth failing a phase over
        return False
    record_skipped(conn, task_id, phase)
    return True


__all__ = [
    "DELIVERED",
    "DISPATCHED",
    "INVITE_LINE",
    "MERGED",
    "NOT_CONNECTED",
    "NO_WORK_ITEM",
    "QUIET_ENV",
    "REVIEWED",
    "TICKET_STEPS",
    "TICKET_STEP_SKIPPED",
    "connected",
    "has_work_item",
    "invitation",
    "invite_enabled",
    "record_skipped",
    "say_invitation",
    "skip_if_local",
]
