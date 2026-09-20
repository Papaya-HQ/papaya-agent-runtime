"""Keeping one file a worker already has as evidence, without letting it read anything else.

A worker's long command output does not reach it. Claude Code writes a large tool
result to a file under its own session directory and shows the worker a preview, so the
only way to keep a full build or test log as a receipt is to copy that file. Workers
worked this out and tried it: issue #127 is ten refused `cp` commands, every one of them
a worker copying ITS OWN saved output into its evidence directory, filed as a profile
gap because `cp` reaches outside the worktree.

The refusals were right. `cp` takes any path, and a worker with `cp` could put
`~/.ssh/id_rsa` into a pull request body — the same hole review found in `--note-file`
before PR #124 confined it. So the answer is not a wider tool profile but a narrower
command: :func:`add` copies exactly ONE file, and only from somewhere that is already
the task's own.

**The confinement rule.** After strict resolution — symlinks followed, so a link out of
either place lands outside and is refused by the same check — the source must be either

- inside the named task's own worktree, or
- ``<claude projects>/<project>/<session id>/tool-results/<file>``, where the session id
  is one the supervisor recorded FOR THAT TASK.

and in both cases a regular file, owned by the user running this, no larger than
:data:`MAX_BYTES`. The task is the one named on the command line, never the working
directory: a worker's cwd is not evidence of whose task it is.

Pinning the session id is what makes the second place safe. The project directory name
is derivable from the worktree path (`/` and `.` both become `-`), but it is not relied
on: a session id is a UUID the supervisor wrote down against this task, so another
task's sessions, and another project's tool-results, are refused whatever they are
called. A task that was resumed has several — `sessions` keeps only the newest, and the
rest are recoverable from its events — so :func:`session_ids` reads both.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import stat
from pathlib import Path

#: The largest file that may be kept. A receipt is a log, not an artefact; anything
#: larger is a build output that belongs in the repository or nowhere.
MAX_BYTES = 8 * 1024 * 1024

#: The directory Claude Code writes a large tool result into, under the session.
TOOL_RESULTS = "tool-results"

#: A session id as the supervisor records it. Anything else in the record — a crafted
#: payload, a truncated write — is not a directory name this will ever look for.
_SESSION_ID = re.compile(r"\A[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")

#: What a receipt may be called: one bare filename, nothing that could traverse.
_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class EvidenceError(Exception):
    """A refusal, in the words the worker is told it in."""


def claude_projects_root() -> Path:
    """Where Claude Code keeps one directory per project. The test seam for all of this."""
    home = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return Path(home).expanduser() / "projects"


def session_ids(conn: sqlite3.Connection, task_id: int) -> set[str]:
    """Every provider session the supervisor recorded for this task.

    `sessions` holds one row per task and is UPDATEd on resume, so it carries only the
    newest; a resumed task's earlier sessions live on in its events, which is where the
    tool-results a worker wants to keep were written. Both are read, and only ids of the
    recorded shape are returned.
    """
    found: set[str] = set()
    rows = conn.execute(
        "SELECT provider_session_id AS s FROM sessions WHERE task_id = ? "
        "UNION SELECT DISTINCT json_extract(payload, '$.session_id') AS s FROM events "
        "WHERE task_id = ?",
        (task_id, task_id),
    ).fetchall()
    for row in rows:
        value = str(row["s"] or "").strip()
        if _SESSION_ID.match(value):
            found.add(value)
    return found


def _receipt_name(source: Path, given: str | None) -> str:
    """The destination's bare filename, or a refusal saying why it is not one."""
    name = (given if given is not None else source.name).strip()
    if not _NAME.match(name):
        raise EvidenceError(
            f"{name!r} is not a name a receipt may have: one filename of letters, digits, "
            "dots, dashes and underscores, starting with a letter or digit — no directory "
            "separators, no `..`, at most 128 characters"
        )
    return name


def _resolved(source: str | Path) -> Path:
    """``source`` with every symlink followed, or a refusal naming what it is."""
    try:
        return Path(source).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise EvidenceError(f"there is no file at {source}: {exc}") from exc


def _allowed(resolved: Path, worktree: Path, sessions: set[str]) -> bool:
    """Is this resolved path the task's own? See the module docstring for the rule."""
    if resolved.is_relative_to(worktree):
        return True
    parent = resolved.parent
    if parent.name != TOOL_RESULTS or parent.parent.name not in sessions:
        return False
    # Exactly <projects>/<one component>/<session>/tool-results/<file>: a deeper path
    # that merely happens to contain those names is not the session's own directory.
    return parent.parent.parent.parent == claude_projects_root().expanduser().resolve()


def _checked_file(resolved: Path) -> int:
    """The size of a source that may be copied, or a refusal saying why it may not."""
    info = os.stat(resolved)  # the resolved path, so a link to a device is seen as one
    if not stat.S_ISREG(info.st_mode):
        raise EvidenceError(
            f"{resolved} is not a regular file — a receipt is one file, never a directory, "
            "a device or a pipe"
        )
    if info.st_uid != os.getuid():
        raise EvidenceError(f"{resolved} is not yours — only your own files may be kept")
    if info.st_size > MAX_BYTES:
        raise EvidenceError(
            f"{resolved} is {info.st_size} bytes, over the {MAX_BYTES} byte limit for a receipt"
        )
    return int(info.st_size)


def _task(conn: sqlite3.Connection, task_id: int) -> tuple[Path, Path]:
    """``(worktree, evidence directory)`` for a task, both resolved."""
    from papaya_agent_runtime import environment
    from papaya_agent_runtime.state import store

    task = store.get_task(conn, task_id)
    if task is None:
        raise EvidenceError(f"task {task_id} does not exist")
    where = str(task["worktree_path"] or "")
    if not where or not Path(where).is_dir():
        raise EvidenceError(f"task {task_id} has no worktree, so it has nowhere to keep evidence")
    worktree = Path(where).resolve()
    row = (
        conn.execute("SELECT * FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
        if task["repo_id"] is not None
        else None
    )
    default = environment.DEFAULT_EVIDENCE_DIR
    directory = environment.for_repo(row).evidence_dir if row is not None else default
    return worktree, worktree / directory


def add(
    task_id: int,
    source: str | Path,
    *,
    name: str | None = None,
    force: bool = False,
    conn: sqlite3.Connection | None = None,
) -> Path:
    """Copy one file the named task already owns into its evidence directory.

    Returns where it landed. Raises :class:`EvidenceError`, with the reason by name, for
    anything the module docstring's rule refuses. Nothing here is best-effort: a refusal
    must be a refusal, or the confinement is decoration.
    """
    from papaya_agent_runtime.state import db

    own = conn is None
    conn = conn or db.init_db()
    try:
        worktree, evidence_dir = _task(conn, task_id)
        sessions = session_ids(conn, task_id)
    finally:
        if own:
            conn.close()

    resolved = _resolved(source)
    if not _allowed(resolved, worktree, sessions):
        raise EvidenceError(
            f"{resolved} is not task {task_id}'s to keep. A receipt may come from that task's "
            f"own worktree ({worktree}) or from the `{TOOL_RESULTS}` directory of a Claude "
            f"session recorded for it, and from nowhere else"
        )
    _checked_file(resolved)
    target = evidence_dir / _receipt_name(resolved, name)
    if target.exists() and not force:
        raise EvidenceError(f"{target} is already there; pass --force to replace it")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(resolved, target)
    return target


def summary(task_id: int, target: Path) -> str:
    """What the command prints when it kept a receipt."""
    try:
        size = target.stat().st_size
    except OSError:  # pragma: no cover - written a moment ago
        size = 0
    return f"task {task_id}: kept {size} bytes as {target}"


__all__ = [
    "MAX_BYTES",
    "TOOL_RESULTS",
    "EvidenceError",
    "add",
    "claude_projects_root",
    "session_ids",
    "summary",
]
