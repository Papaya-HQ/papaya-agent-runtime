"""Two tasks in one repository editing the same files off the same head.

On 2026-09-06 four tasks (165-168) were dispatched in parallel against one Radar
composition module. Delivery cost three hand-resolved merge conflicts, conflict
markers shipped inside one merge commit, and three extra full CI runs. The
manager's own rule — related changes in one repository are a stack — was not
applied at dispatch, because nothing at dispatch knew what the new brief touched
or what the in-flight siblings were touching (issue #61).

This module answers that at dispatch. It began as a suggestion, like the
migration advisory (:mod:`papaya_agent_runtime.migrations`), but the unattended
``ppy serve`` manager has nobody to read a printed advisory, and this repository
had three tasks editing ``serve.py`` at once (runtime #94). It is now a refusal.
Two edits to one module off one head are fine as long as somebody sequences them:
``--stack-on`` the sibling, or ``--accept-preflight overlap --reason ...`` to
decide it on the record.

What a brief "touches" is read from two places: a ``Touches:`` line (paths,
comma- or space-separated) and any backtick span that names a file or directory
that exists in the repository. The list is recorded on the task under
``task_env`` (key ``touches``), so a later dispatch can compare against it before
the sibling has written a line; once the sibling has commits, its actual diff
against its base counts too.

"In flight" is deliberately narrow (issue #56 is the migration advisory counting
tasks finished a month ago): a non-terminal working status, a lease worktree
that still exists on disk, and a row touched within the last seven days.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from papaya_agent_runtime.state import store

#: Statuses under which a task's branch is still being written or still unmerged
#: on the manager's desk. ``failed`` and ``needs_recovery`` are not: nothing there
#: is heading for a merge until somebody resumes it, at which point it is again.
IN_FLIGHT_STATUSES = ("requested", "in_progress", "worker_done", "worker_stopped", "blocked")

#: A task untouched for longer than this is not in flight, whatever its row says.
IN_FLIGHT_WINDOW = timedelta(days=7)

#: ``task_env`` key the touched-paths list is recorded under.
TOUCHES_KEY = "touches"

_TOUCHES_LINE = re.compile(r"^\s*(?:[-*]\s*)?\**touches\**\s*:\s*(?P<paths>.+?)\s*$", re.I | re.M)
_CODE_SPAN = re.compile(r"`([^`\n]+)`")


@dataclass(frozen=True)
class Overlap:
    """One in-flight task and the paths it shares with the brief being dispatched."""

    task_id: int
    title: str
    status: str
    branch: str | None
    pairs: list[tuple[str, str]]

    @property
    def task_label(self) -> str:
        branch = f"branch {self.branch}" if self.branch else "no branch yet"
        return f'task {self.task_id} "{self.title}" ({self.status}, {branch})'

    def describe(self) -> str:
        shared = ", ".join(
            mine if mine == theirs else f"{mine} (its {theirs})" for mine, theirs in self.pairs
        )
        return f"{self.task_label} touches {shared}"


def _clean(token: str) -> str:
    token = token.strip().strip("`'\"*,;()").strip()
    if token.startswith("./"):
        token = token[2:]
    return token.rstrip("/")


def _looks_like_path(token: str) -> bool:
    return bool(token) and ("/" in token or bool(re.search(r"\.[A-Za-z0-9]{1,8}$", token)))


def touched_paths(brief: str, repo_root: str | None = None) -> list[str]:
    """The repo-relative paths a brief says it touches, in the order first named.

    ``Touches:`` lines are taken at their word — the manager wrote them for this.
    Backtick spans are kept only when they name something that exists under
    ``repo_root``, because a brief quotes commands, flags, and identifiers in
    backticks far more often than files.
    """
    found: list[str] = []

    def add(path: str) -> None:
        if path and path not in found:
            found.append(path)

    for match in _TOUCHES_LINE.finditer(brief or ""):
        for token in re.split(r"[,\s]+", match.group("paths")):
            cleaned = _clean(token)
            if _looks_like_path(cleaned):
                add(cleaned)
    if repo_root and os.path.isdir(repo_root):
        for span in _CODE_SPAN.findall(brief or ""):
            cleaned = _clean(span)
            if not cleaned or cleaned.startswith(("/", "~")) or ".." in cleaned.split("/"):
                continue
            if any(ch in cleaned for ch in " \t|&;$<>"):
                continue
            if os.path.exists(os.path.join(repo_root, cleaned)):
                add(cleaned)
    return found


def record_touches(conn, task_id: int, paths: list[str]) -> None:
    """Keep the list on the task so later dispatches can compare against it."""
    if paths:
        store.set_task_env(conn, task_id, TOUCHES_KEY, json.dumps(paths), source="brief")


def recorded_touches(conn, task_id: int) -> list[str]:
    raw = store.get_task_env(conn, task_id, TOUCHES_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return [str(p) for p in data] if isinstance(data, list) else []


def _changed_paths(task) -> list[str]:
    """Files the task's worktree has changed against its base, when it has any."""
    worktree, base = task["worktree_path"], task["base_sha"]
    if not worktree or not base or not os.path.isdir(worktree):
        return []
    proc = subprocess.run(
        ["git", "-C", worktree, "diff", "--name-only", f"{base}..HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def task_paths(conn, task) -> list[str]:
    """Everything a task is known to touch: what its brief named, plus its diff."""
    paths = recorded_touches(conn, int(task["id"]))
    for path in _changed_paths(task):
        if path not in paths:
            paths.append(path)
    return paths


def _parse_when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def is_in_flight(task, *, now: datetime | None = None) -> bool:
    """A live working status, a lease worktree still on disk, and recent activity."""
    if task["status"] not in IN_FLIGHT_STATUSES:
        return False
    worktree = task["worktree_path"]
    if not worktree or not os.path.isdir(worktree):
        return False
    touched = _parse_when(task["updated_at"])
    if touched is None:
        return False
    return (now or datetime.now(UTC)) - touched <= IN_FLIGHT_WINDOW


def in_flight_tasks(conn, repo_id, *, exclude: set[int] | None = None) -> list:
    """The other tasks in this repository whose branches are still being written."""
    if not repo_id:
        return []
    rows = conn.execute("SELECT * FROM tasks WHERE repo_id = ? ORDER BY id", (repo_id,)).fetchall()
    skip = exclude or set()
    return [row for row in rows if int(row["id"]) not in skip and is_in_flight(row)]


def _related(mine: str, theirs: str) -> bool:
    """Same path, or one is a directory holding the other (a module and a file in it)."""
    return mine == theirs or theirs.startswith(mine + "/") or mine.startswith(theirs + "/")


def overlaps(conn, repo_id, paths: list[str], *, exclude: set[int] | None = None) -> list[Overlap]:
    """Which in-flight tasks share any of ``paths``, and which ones."""
    if not paths:
        return []
    found: list[Overlap] = []
    for row in in_flight_tasks(conn, repo_id, exclude=exclude):
        theirs = task_paths(conn, row)
        pairs = [(mine, other) for mine in paths for other in theirs if _related(mine, other)]
        if pairs:
            found.append(
                Overlap(
                    task_id=int(row["id"]),
                    title=row["title"],
                    status=row["status"],
                    branch=row["branch"],
                    pairs=pairs,
                )
            )
    return found


def _stack_chain(conn, task_id: int | None) -> set[int]:
    """A task and every task it is stacked on: work the new worker starts from."""
    chain: set[int] = set()
    while task_id is not None and int(task_id) not in chain:
        chain.add(int(task_id))
        row = store.get_task(conn, int(task_id))
        task_id = row["stacked_on_task"] if row is not None else None
    return chain


def task_for_branch(conn, repo_id, branch: str | None):
    """The latest task in this repository whose lease branch is ``branch``, if any."""
    if not branch or not repo_id:
        return None
    return conn.execute(
        "SELECT * FROM tasks WHERE repo_id = ? AND branch = ? ORDER BY id DESC LIMIT 1",
        (repo_id, branch),
    ).fetchone()


def dispatch_refusal(
    conn, repo_row, paths: list[str], *, stack_on: int | None = None, base: str | None = None
) -> str | None:
    """Why this dispatch is refused for siblings editing the same files, or None.

    The task being built on — named by ``--stack-on``, or by ``--base`` naming its
    lease branch — and everything under it in its stack are not counted: their
    edits are about to be part of the new worker's starting commit.
    """
    exclude = _stack_chain(conn, stack_on)
    by_branch = task_for_branch(conn, repo_row["id"], base)
    if by_branch is not None:
        exclude |= _stack_chain(conn, int(by_branch["id"]))
    found = overlaps(conn, repo_row["id"], paths, exclude=exclude)
    if not found:
        return None
    lines = [
        "work already in flight in this repository touches files this brief names, and "
        "two branches editing one module off the same head conflict when the second merges."
    ]
    for item in found:
        lines.append(f"  {item.describe()}")
    suggestions = ", ".join(f"--stack-on {item.task_id}" for item in found)
    lines.append(
        f"  To build on that work instead of beside it, dispatch with {suggestions}; to run "
        "beside it anyway, --accept-preflight overlap --reason ..."
    )
    return "\n".join(lines)


__all__ = [
    "IN_FLIGHT_STATUSES",
    "IN_FLIGHT_WINDOW",
    "TOUCHES_KEY",
    "Overlap",
    "dispatch_refusal",
    "in_flight_tasks",
    "is_in_flight",
    "overlaps",
    "record_touches",
    "recorded_touches",
    "task_for_branch",
    "task_paths",
    "touched_paths",
]
