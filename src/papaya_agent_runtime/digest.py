"""The delta: what moved, what is newly blocked, what needs the user, since the last check.

Every check used to either dump the whole board or say nothing, which is how the
supervision gap in #72 stayed invisible for a day. The contract now asks that a
heartbeat tick, a team status check and a batch of received events each end in a very
concise summary, with one line for "no change" rather than silence
(`docs/runtime-contract.md`, "Every check ends in a short delta").

One renderer, one record. :func:`check` reads the state now, compares it with what the
last check saw (kept under the run directory, so `ppy status` after a heartbeat tick
compares with that tick and not with the start of time), remembers this one, and
returns the lines. Every surface calls it last: the heartbeat's tick, `ppy status`,
`ppy status --team` and `ppy run`. It reads the ledger and the blockers only; a pull
request's state comes from the surface that already read the forge, so a status check
never pays for one.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from papaya_agent_runtime.state import store

#: The record of what the last check saw, under the run directory.
DIGEST_FILE = "digest.json"

#: Statuses a task entering counts as newly blocked: something stopped and waits.
BLOCKED_STATUSES = ("blocked", "worker_stopped", "failed", "needs_recovery")

#: How many items of one kind a line names before it counts the rest.
NAMED = 4


def _clip(text: object, width: int = 70) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= width else one[: width - 1] + "…"


def _blocked_on_user(value: object) -> bool:
    return str(value or "") == "user" or str(value or "").startswith("user:")


def snapshot(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    prs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """What a check compares: worker task statuses, pull requests, the ledger, blockers.

    ``prs`` is keyed the way the surface keys them (`watch.pr_index`), each with its
    ``state``, ``ci`` and ``mergeable``; a surface that read no forge passes none, and
    the pull requests the last check knew are carried forward unchanged.
    """
    from papaya_agent_runtime import blockers, owed

    now = now or datetime.now(UTC)
    tasks = {
        str(row["id"]): str(row["status"])
        for row in conn.execute(
            f"SELECT id, status FROM tasks WHERE {store.WORKER_TASK} ORDER BY id"
        ).fetchall()
    }
    todos: dict[str, dict[str, Any]] = {}
    for row in store.list_todos(conn, status="open"):
        todos[str(row["id"])] = {
            "text": str(row["text"]),
            "blocked_on": row["blocked_on"] or "",
            "task_id": row["task_id"],
        }
    open_blockers = {
        str(b.get("code") or b.get("title") or i): str(b.get("title") or b.get("code") or "")
        for i, b in enumerate(blockers.current())
    }
    needs: list[str] = []
    for todo_id, todo in todos.items():
        if _blocked_on_user(todo["blocked_on"]):
            where = f" (task {todo['task_id']})" if todo.get("task_id") is not None else ""
            needs.append(f"todo #{todo_id}{where}: {_clip(todo['text'])}")
    for item in owed.overdue(owed.collect(conn, now=now)):
        if item.status in owed.OWED_STATUSES:
            needs.append(f"t{item.task_id} {item.status} {_ago(item.seconds)}, nobody took it up")
    for title in open_blockers.values():
        needs.append(_clip(title))
    return {
        "at": now.isoformat(timespec="seconds"),
        "tasks": tasks,
        "prs": {
            key: {
                "state": e.get("state"),
                "ci": e.get("ci"),
                "mergeable": e.get("mergeable"),
            }
            for key, e in (prs or {}).items()
        }
        if prs is not None
        else None,
        "todos": todos,
        "blockers": open_blockers,
        "needs_user": needs,
    }


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return ""
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"for {minutes}m"
    hours = minutes // 60
    return f"for {hours}h" if hours < 48 else f"for {hours // 24}d"


@dataclass
class Delta:
    """What changed between two checks, in the three lists the contract names."""

    since: str | None
    moved: list[str] = field(default_factory=list)
    newly_blocked: list[str] = field(default_factory=list)
    needs_user: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.moved or self.newly_blocked)


def delta(previous: dict[str, Any] | None, current: dict[str, Any]) -> Delta:
    """Compare two snapshots. With no previous check there is nothing to compare yet."""
    found = Delta(
        since=(previous or {}).get("at"), needs_user=list(current.get("needs_user") or [])
    )
    if previous is None:
        return found
    was_tasks, now_tasks = previous.get("tasks") or {}, current.get("tasks") or {}
    for task_id, status in now_tasks.items():
        before = was_tasks.get(task_id)
        if before == status:
            continue
        found.moved.append(f"t{task_id} {before or 'new'}→{status}")
        if status in BLOCKED_STATUSES:
            found.newly_blocked.append(f"t{task_id} {status}")
    for task_id in was_tasks:
        if task_id not in now_tasks:
            found.moved.append(f"t{task_id} gone")
    was_prs = previous.get("prs")
    now_prs = current.get("prs")
    if was_prs is not None and now_prs is not None:
        for key, entry in now_prs.items():
            before = was_prs.get(key)
            if before is None:
                found.moved.append(f"{key} appeared")
                continue
            for fact in ("state", "ci", "mergeable"):
                if before.get(fact) != entry.get(fact) and entry.get(fact):
                    found.moved.append(
                        f"{key} {fact} {str(before.get(fact) or '?').lower()}→"
                        f"{str(entry.get(fact)).lower()}"
                    )
    was_todos, now_todos = previous.get("todos") or {}, current.get("todos") or {}
    for todo_id in was_todos:
        if todo_id not in now_todos:
            found.moved.append(f"todo #{todo_id} closed")
    for todo_id, todo in now_todos.items():
        before = was_todos.get(todo_id)
        if before is None:
            found.moved.append(f"todo #{todo_id} added")
        elif before.get("blocked_on") != todo.get("blocked_on") and todo.get("blocked_on"):
            found.newly_blocked.append(f"todo #{todo_id} waits on {todo['blocked_on']}")
    was_blockers, now_blockers = previous.get("blockers") or {}, current.get("blockers") or {}
    for code, title in now_blockers.items():
        if code not in was_blockers:
            found.newly_blocked.append(f"blocker: {_clip(title)}")
    for code, title in was_blockers.items():
        if code not in now_blockers:
            found.moved.append(f"blocker cleared: {_clip(title)}")
    return found


def _join(items: list[str]) -> str:
    if len(items) <= NAMED:
        return "; ".join(items)
    return "; ".join(items[:NAMED]) + f"; and {len(items) - NAMED} more"


def _when(stamp: str | None) -> str:
    return f"{stamp[11:16]} UTC" if stamp and len(stamp) >= 16 else "the last check"


def render(found: Delta) -> list[str]:
    """A few lines: what moved, what is newly blocked, what needs the user; or one line."""
    needs = f"needs you: {_join(found.needs_user)}" if found.needs_user else ""
    if found.since is None:
        first = "first check, nothing to compare with yet"
        return [f"{first}; {needs}" if needs else first]
    if not found.changed:
        line = f"no change since {_when(found.since)}"
        return [f"{line}; still {needs}" if needs else line]
    lines = [f"since {_when(found.since)}: moved: {_join(found.moved) or 'nothing'}"]
    if found.newly_blocked:
        lines.append(f"newly blocked: {_join(found.newly_blocked)}")
    if needs:
        lines.append(needs)
    return lines


def line(lines: list[str]) -> str:
    """The digest on one line, for a heartbeat tick a monitor relays line by line."""
    return " | ".join(lines)


# ── the record of the last check ────────────────────────────────────────────


def path() -> Path:
    from papaya_agent_runtime.paths import run_dir

    return run_dir() / DIGEST_FILE


def load() -> dict[str, Any] | None:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def remember(current: dict[str, Any]) -> None:
    try:
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(current, default=str), encoding="utf-8")
    except OSError:
        pass


def check(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    prs: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """The delta since the last check, remembered as this one. Never raises."""
    try:
        previous = load()
        current = snapshot(conn, now=now, prs=prs)
        if current.get("prs") is None and previous is not None:
            # This surface read no forge: the pull requests are as the last check saw them.
            current["prs"] = previous.get("prs")
        found = delta(previous, current)
        remember(current)
        return render(found)
    except Exception as exc:  # noqa: BLE001 - a digest never fails the check it ends
        return [f"no delta: {exc}"]


__all__ = [
    "BLOCKED_STATUSES",
    "DIGEST_FILE",
    "Delta",
    "check",
    "delta",
    "line",
    "load",
    "remember",
    "render",
    "snapshot",
]
