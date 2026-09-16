"""A runner row is truth or it is closed.

Worker capacity counts ``starting``/``running`` runner rows (`Supervisor._admit`).
A row whose process is gone and that nobody closed holds a slot for as long as
nobody looks: on 2026-09-16 a worker whose supervisor was stopped by hand kept
one of two slots for three hours, through two supervisor starts, because the
start reconciled only a live previous supervisor's workers and the rounds only
looked at workers under held tickets.

:func:`close_dead_runners` is the one place such a row is closed. It runs at every
supervisor start (fresh, adopted or retired) and on every manager round, whatever
the ticket. A row is dead when its recorded pid is not alive, or when it has no
pid and has not been heard from for `supervisor.dead_after` (ten minutes). It is
marked ``exited``; a task that was still in flight becomes ``worker_stopped`` with
a note naming the cause, and its provider session stays on the task, so a resume
picks it up where it stopped. Each closed row is one log line.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from papaya_agent_runtime.state import store

log = logging.getLogger("papaya_agent_runtime.supervisor")

#: `supervisor.dead_after` when the config cannot be read.
DEFAULT_DEAD_AFTER_SECONDS = 600

#: Task statuses a dead runner leaves as `worker_stopped`; anything else already ended.
IN_FLIGHT = ("requested", "dispatched", "in_progress")

#: The event kind a closed runner's task gets (`turn_end.WORKER_STOPPED`).
WORKER_STOPPED = "worker_stopped"


@dataclass(frozen=True)
class Closed:
    runner_id: str
    task_id: int
    cause: str
    #: Whether the task was moved to `worker_stopped` (it was still in flight).
    stopped: bool

    def line(self) -> str:
        tail = "task recorded worker_stopped, session kept" if self.stopped else "task had ended"
        return (
            f"closed runner {self.runner_id} of task {self.task_id}: {self.cause}; "
            f"slot released, {tail}"
        )


def dead_after_seconds() -> int:
    from papaya_agent_runtime.config import ConfigError, load_config

    try:
        return int(load_config().supervisor.dead_after)
    except (ConfigError, OSError, ValueError):
        return DEFAULT_DEAD_AFTER_SECONDS


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parse(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _last_heard(conn: sqlite3.Connection, row: sqlite3.Row) -> datetime | None:
    """The newest of the row's heartbeat, its start, and its task's newest event."""
    event = conn.execute(
        "SELECT MAX(created_at) AS at FROM events WHERE task_id = ?", (row["task_id"],)
    ).fetchone()
    stamps = [_parse(row["heartbeat_at"]), _parse(row["started_at"]), _parse(event["at"])]
    known = [s for s in stamps if s is not None]
    return max(known) if known else None


def _cause(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    now: datetime,
    dead_after: timedelta,
    grace: timedelta,
    alive: Callable[[int | None], bool],
) -> str | None:
    pid = row["pid"]
    heard = _last_heard(conn, row)
    quiet = (now - heard) if heard is not None else None
    if pid:
        if alive(int(pid)):
            return None
        # Between a process exiting and its runner recording the result there is a
        # moment where the pid is gone and the row still says running.
        if grace and quiet is not None and quiet < grace:
            return None
        return f"its process (pid {pid}) is gone"
    if quiet is None or quiet >= dead_after:
        minutes = int(quiet.total_seconds() // 60) if quiet is not None else None
        heard_text = f"for {minutes} minutes" if minutes is not None else "at all"
        return f"it has no process and has not been heard from {heard_text}"
    return None


def close_dead_runners(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    dead_after_s: float | None = None,
    grace_s: float = 0.0,
    skip_tasks: Iterable[int] = (),
    alive: Callable[[int | None], bool] | None = None,
    source: str = "supervisor",
) -> list[Closed]:
    """Close every live runner row with no process behind it. Never raises for one row.

    ``skip_tasks`` are tasks whose execution this process owns (its own runner
    thread records their end); ``grace_s`` is how long a pid may have been gone
    before the row counts as dead, for callers that share a home with a live
    supervisor.
    """
    now = now or datetime.now(UTC)
    dead_after = timedelta(
        seconds=dead_after_s if dead_after_s is not None else dead_after_seconds()
    )
    grace = timedelta(seconds=grace_s)
    alive = alive or pid_alive
    skipped = set(skip_tasks)
    closed: list[Closed] = []
    for row in store.live_runners(conn):
        task_id = int(row["task_id"])
        if task_id in skipped:
            continue
        try:
            cause = _cause(conn, row, now=now, dead_after=dead_after, grace=grace, alive=alive)
            if cause is None:
                continue
            task = store.get_task(conn, task_id)
            in_flight = task is not None and task["status"] in IN_FLIGHT
            # The session itself stays in `sessions`, untouched, which is what a resume reads.
            session = row["session_id"]
            if not session:
                found = conn.execute(
                    "SELECT provider_session_id FROM sessions WHERE task_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                session = found["provider_session_id"] if found else None
            payload = {
                "task_id": task_id,
                "summary": f"worker stopped before done: its runner was closed because {cause}",
                "reasons": [f"runner {row['id']} was closed because {cause}"],
                "runner_id": row["id"],
                "session_id": session,
                "source": source,
            }
            done = store.close_dead_runner(
                conn,
                runner_id=str(row["id"]),
                run_id=int(task["run_id"]) if task is not None else None,
                task_id=task_id,
                task_status=WORKER_STOPPED if in_flight else None,
                kind=WORKER_STOPPED if in_flight else "runner_closed",
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 - one bad row must not keep the rest open
            log.warning("[supervisor] could not close runner %s: %s", row["id"], exc)
            continue
        if not done:
            continue
        entry = Closed(str(row["id"]), task_id, cause, in_flight)
        log.info("[%s] %s", source, entry.line())
        closed.append(entry)
    return closed


__all__ = ["Closed", "close_dead_runners", "dead_after_seconds", "pid_alive"]
