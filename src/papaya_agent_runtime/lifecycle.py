"""Task lifecycle bookkeeping: close, repair a status, release a stuck lease.

These are the three things the manager was doing with ad-hoc Python one-liners
against `.ppy/state.db` on 2026-09-01/02 — abandoning a task the user cancelled,
correcting a status the runtime had wrong, and handing back a slot whose lease
outlived its worker. A one-liner writes a status with no event behind it, so the
next reader has no idea why the state changed; every operation here records the
reason as a durable event.

Everything works from the DB, so none of it needs a running supervisor.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from papaya_agent_runtime import compose
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.worktree import Lease, LeaseError, LeaseManager

# Every status a task may legitimately hold. `ppy task set-status` validates
# against this so a typo cannot invent a status that no reader understands.
TASK_STATUSES = (
    "requested",
    "in_progress",
    "worker_done",
    "worker_stopped",
    "blocked",
    "failed",
    "needs_recovery",
    "delivered",
    "closed",
    "cancelled",
)

# Statuses that mean "this task is over": no worker, no nagging, safe to reclaim.
TERMINAL_STATUSES = ("delivered", "closed", "cancelled")


class LifecycleError(Exception):
    """A lifecycle command was refused; the state is unchanged."""


def require_live_lease(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    error_type: type[Exception] = LifecycleError,
) -> sqlite3.Row:
    """Return the task's current active lease, or refuse with its release record.

    The task row is authoritative about lease identity. Falling back to any lease
    attached to the task is unsafe: a returned pool slot can already belong to a
    different task even though the old task still names its path and branch.
    """
    task = store.get_task(conn, task_id)
    if task is None:
        raise error_type(f"task {task_id} not found")
    lease_id = task["lease_id"]
    lease = (
        conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
        if lease_id
        else None
    )
    if (
        lease is not None
        and lease["status"] == "active"
        and lease["task_id"] == task_id
        and isinstance(lease["worktree_path"], str)
        and lease["worktree_path"] == task["worktree_path"]
        and lease["branch"] == task["branch"]
        and Path(lease["worktree_path"]).is_dir()
    ):
        return lease
    released = conn.execute(
        "SELECT id, payload FROM events WHERE task_id = ? AND kind = 'lease_released' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    event = f"lease_released event {released['id']}" if released else "no active lease record"
    raise error_type(
        f"task {task_id} has no live worktree lease ({event}); refusing to use its recorded "
        "path because a released lease identity may now belong to another task"
    )


def _task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    task = store.get_task(conn, task_id)
    if task is None:
        raise LifecycleError(f"task {task_id} not found")
    return task


def _lease_row(conn: sqlite3.Connection, task: sqlite3.Row) -> sqlite3.Row | None:
    """The task's lease row: the one it recorded, else any lease still held for it."""
    lease_id = task["lease_id"]
    if lease_id:
        row = conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
        if row is not None:
            return row
    return conn.execute(
        "SELECT * FROM leases WHERE task_id = ? AND status = 'active' "
        "ORDER BY created_at DESC LIMIT 1",
        (task["id"],),
    ).fetchone()


def release_task_lease(
    task_id: int, *, reason: str = "released by hand", remove_branch: bool = False
) -> dict:
    """Hand a task's worktree slot back, recording why.

    A worker that died without finishing leaves its lease `active` forever, and the
    pool fills up until someone reaches into SQLite. This is that, with an event.
    The branch is kept by default: a released slot must never silently destroy
    commits that were never pushed.
    """
    conn = init_db()
    task = _task(conn, task_id)
    row = _lease_row(conn, task)
    if row is None:
        raise LifecycleError(f"task {task_id} has no lease on record")
    if row["status"] != "active":
        return {
            "task_id": task_id,
            "lease": row["id"],
            "released": False,
            "note": f"lease {row['id']} was already released",
        }

    lease = Lease(
        id=row["id"],
        repo_id=row["repo_id"],
        task_id=row["task_id"],
        branch=row["branch"],
        worktree_path=row["worktree_path"],
        base_sha=row["base_sha"],
        backend=row["backend"],
    )
    repo = conn.execute("SELECT * FROM repos WHERE id = ?", (row["repo_id"],)).fetchone()
    repo_path = repo["local_path"] if repo else ""
    try:
        LeaseManager(lease.backend).release(lease, repo_path=repo_path, remove_branch=remove_branch)
    except LeaseError as exc:
        raise LifecycleError(f"could not release lease {lease.id}: {exc}") from exc

    store.append_event(
        conn,
        kind="lease_released",
        payload={
            "task_id": task_id,
            "lease": lease.id,
            "reason": reason,
            "worktree_path": lease.worktree_path,
            "branch": lease.branch,
            "removed_branch": remove_branch,
        },
        run_id=task["run_id"],
        task_id=task_id,
    )
    return {
        "task_id": task_id,
        "lease": lease.id,
        "released": True,
        "worktree_path": lease.worktree_path,
        "branch": lease.branch,
        "note": f"released lease {lease.id} ({lease.worktree_path})",
    }


def close_task(task_id: int, reason: str) -> dict:
    """End a task for a reason that is not delivery, and give its slot back.

    Superseded, duplicated, overtaken by the user's own change, dropped on a scope
    call — all of these used to leave a task sitting in `worker_done` forever,
    holding a worktree and nagging the heartbeat.
    """
    if not reason or not reason.strip():
        raise LifecycleError("a close needs a reason — it is the only record of why")
    conn = init_db()
    task = _task(conn, task_id)
    previous = task["status"]
    store.set_task_status(conn, task_id, "closed")
    lease_note = None
    try:
        lease_note = release_task_lease(task_id, reason=f"task closed: {reason.strip()}")
    except LifecycleError as exc:
        lease_note = {"released": False, "note": str(exc)}
    # A closed task's compose stack is nobody's working state; it was holding a
    # Docker network and a volume until someone noticed.
    torn_down = compose.teardown_for_task(task_id, trigger="task close", conn=conn)
    store.append_event(
        conn,
        kind="task_closed",
        payload={
            "task_id": task_id,
            "reason": reason.strip(),
            "previous_status": previous,
            "lease_released": bool(lease_note and lease_note.get("released")),
            "compose_project": (torn_down or {}).get("project"),
        },
        run_id=task["run_id"],
        task_id=task_id,
    )
    return {
        "task_id": task_id,
        "status": "closed",
        "previous_status": previous,
        "reason": reason.strip(),
        "lease": lease_note,
        "compose": torn_down,
    }


def set_task_status(task_id: int, status: str, note: str | None = None) -> dict:
    """Repair a task's status by hand, leaving a record of who changed what and why."""
    if status not in TASK_STATUSES:
        raise LifecycleError(f"unknown status {status!r} — allowed: {', '.join(TASK_STATUSES)}")
    conn = init_db()
    task = _task(conn, task_id)
    previous = task["status"]
    store.set_task_status(conn, task_id, status)
    store.append_event(
        conn,
        kind="status_set",
        payload={
            "task_id": task_id,
            "from": previous,
            "to": status,
            "note": (note or "").strip() or None,
        },
        run_id=task["run_id"],
        task_id=task_id,
    )
    return {"task_id": task_id, "from": previous, "to": status, "note": note}
