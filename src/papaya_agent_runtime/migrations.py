"""Two tasks in one repository adding a migration off the same head.

On 2026-09-04 two independent backend tasks each added an Alembic revision whose
``down_revision`` was the same commit's head. Nothing in `ppy` noticed: the second
to merge would have left the repository with two Alembic heads and a red
migration-graph test. The manager caught it by reading both diffs by hand and
steering one worker onto the other's branch.

The harness can see it earlier, at two moments and from two angles:

- **At dispatch**, before the second worker has written a line: another task in
  this repository is in flight and its worktree already adds a migration. That is
  a *suggestion* — start from its branch with ``--stack-on`` — not a refusal,
  because two migrations off one head are fine as long as somebody sequences them.
- **At `ppy review show`**, when both diffs exist and the question is sharper: do
  these two added migrations name the *same* ``down_revision``? That is no longer
  a guess about what a worker might do, it is the two-heads condition itself.

Neither one refuses anything. Alembic is never run, nothing is rebased: the
manager decides what to do with what it is told.

What counts as a task's migration is decided by its *commits*, never by whatever
happens to be on disk at its recorded worktree path. Pool slots are recycled:
on 2026-09-07 three finished tasks still pointed at slot 3, which by then held
task 170's checkout, and the review flag reported task 170's own migration as
three phantom competitors (issue #74). So a task's inventory is read from its
lease worktree only while an active lease of *that task* owns the path and the
checkout is on the task's branch; otherwise from the task's branch in the base
clone; and a task whose branch exists nowhere contributes nothing and is named
as unverifiable rather than silently passed.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

#: A task in one of these may still put a migration on the graph: a worker is
#: writing it, or it is written and waiting for review or delivery. ``failed``
#: and ``needs_recovery`` are deliberately absent — that work is not about to
#: merge, so it must not push a new dispatch onto its branch (issue #56). Note
#: this is a narrower question than "may the worktree be reclaimed": a failed
#: task keeps its worktree for ``ppy resume`` (issue #58) while contributing
#: nothing here.
IN_FLIGHT_STATUSES = ("in_progress", "worker_done", "worker_stopped", "blocked")

#: A task in an in-flight status that nothing has touched for this long, and
#: that has no live runner, is treated as abandoned rather than in flight. Tasks
#: 2–5 in the 2026-09-05 false positive were a month old.
IN_FLIGHT_WINDOW = timedelta(days=7)

# Where Alembic revisions live in a repository laid out the ordinary way. A repo
# that keeps them somewhere else sets its own with
# `ppy repo set <name> --migrations-glob <glob>`; the pattern is matched against
# the whole repo-relative path, so `**` spans any number of directories.
DEFAULT_MIGRATIONS_GLOB = "**/alembic/versions/*.py"

# `down_revision = "abc123"` on one line, for a file too broken to parse.
_DOWN_REVISION_LINE = re.compile(
    r"^\s*down_revision\s*(?::[^=]+)?=\s*(?P<value>.+?)\s*(?:#.*)?$", re.MULTILINE
)


@dataclass(frozen=True)
class AddedMigration:
    """A migration file one task adds, and the revision it builds on."""

    task_id: int
    title: str
    status: str
    branch: str | None
    path: str
    down_revision: str | None

    @property
    def task_label(self) -> str:
        """The task and where its work lives, named the way `ppy stack` names a layer."""
        branch = f"branch {self.branch}" if self.branch else "no branch yet"
        return f'task {self.task_id} "{self.title}" ({self.status}, {branch})'

    def describe(self) -> str:
        """The task, its branch, and its migration, in one clause."""
        return f"{self.task_label} adds {self.path}"


def _git(worktree: str, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", worktree, *args], capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout


def _segment_regex(part: str) -> str:
    """One path segment of a glob as a regular expression.

    `*` and `?` stop at a separator, so a pattern cannot silently reach into a
    subdirectory it did not name. `[...]` classes pass through, with a leading
    `!` spelled the way a shell spells it.
    """
    out: list[str] = []
    index = 0
    while index < len(part):
        char = part[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            close = index + 1
            if close < len(part) and part[close] in "!^":
                close += 1
            if close < len(part) and part[close] == "]":
                close += 1
            while close < len(part) and part[close] != "]":
                close += 1
            if close >= len(part):  # unterminated class: a literal bracket
                out.append(re.escape(char))
            else:
                body = part[index + 1 : close]
                if body[:1] in ("!", "^"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                index = close + 1
                continue
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


@lru_cache(maxsize=256)
def _glob_regex(glob: str) -> re.Pattern[str] | None:
    """A glob compiled to a whole-path pattern, or None when it is malformed.

    Hand-rolled rather than `PurePosixPath.full_match`, which only exists on
    Python 3.13 — this project supports 3.12, and on 3.12 that call raised
    `AttributeError` from inside a broad `except`, so every migration-collision
    advisory silently reported nothing instead of failing loudly. One
    implementation means the version CI runs is the version that ships.
    """
    parts = glob.split("/")
    pieces: list[str] = []
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == "**":
            # Zero or more whole segments; the separator is part of the group, so
            # `**/a` matches a bare `a` as well as `x/y/a`.
            pieces.append(".*" if last else "(?:[^/]+/)*")
        else:
            pieces.append(_segment_regex(part) + ("" if last else "/"))
    try:
        return re.compile("".join(pieces) + r"\Z")
    except re.error:
        return None


def matches(path: str, glob: str) -> bool:
    """Does a repo-relative path match this repo's migrations glob?"""
    if not glob:
        return False
    pattern = _glob_regex(glob)
    if pattern is None:
        return False
    return pattern.match(path.lstrip("/")) is not None


def glob_for_repo(repo_row) -> str:
    """A registered repo's migrations glob, falling back to the default."""
    if repo_row is None:
        return DEFAULT_MIGRATIONS_GLOB
    keys = repo_row.keys()
    configured = repo_row["migrations_glob"] if "migrations_glob" in keys else None  # noqa: SIM118
    cleaned = str(configured).strip() if configured else ""
    return cleaned or DEFAULT_MIGRATIONS_GLOB


def glob_for_repo_id(conn, repo_id) -> str:
    if not repo_id:
        return DEFAULT_MIGRATIONS_GLOB
    row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    return glob_for_repo(row)


def parse_down_revision(source: str) -> str | None:
    """The ``down_revision`` an Alembic revision file declares, as one string.

    Alembic writes either a single revision id or — for a migration that merges
    two heads — a tuple of them; ``None`` marks the first revision in a branch and
    is returned as the string ``"None"``, because two tasks each adding a *first*
    migration collide exactly as two off a shared parent do. ``None`` from here
    means "no ``down_revision`` could be read at all", which is not a collision.
    """
    unset = object()
    value: object = unset
    try:
        tree = ast.parse(source)
    except SyntaxError:
        match = _DOWN_REVISION_LINE.search(source)
        if match is None:
            return None
        try:
            value = ast.literal_eval(match.group("value"))
        except (ValueError, SyntaxError):
            return None
    else:
        for node in tree.body:
            targets = (
                [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
            )
            named = any(isinstance(t, ast.Name) and t.id == "down_revision" for t in targets)
            if not named or getattr(node, "value", None) is None:
                continue
            try:
                value = ast.literal_eval(node.value)  # type: ignore[arg-type]
            except (ValueError, SyntaxError):
                return None
            break
    if value is unset:
        return None
    if isinstance(value, tuple | list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _down_revision_at(git_dir: str, ref: str, path: str) -> str | None:
    """Read the revision file as the task's commit has it, not as the disk has it."""
    rc, out = _git(git_dir, "show", f"{ref}:{path}")
    return parse_down_revision(out) if rc == 0 else None


@dataclass(frozen=True)
class Unverifiable:
    """A task whose migrations could not be inventoried, and why."""

    task_id: int
    title: str
    status: str
    branch: str | None
    reason: str

    def describe(self) -> str:
        branch = f"branch {self.branch}" if self.branch else "no branch"
        return f'task {self.task_id} "{self.title}" ({self.status}, {branch}): {self.reason}'


def _active_lease_owns(conn, task) -> bool:
    """Is the task's recorded worktree path held, right now, by a lease of this task?"""
    lease_id, path = task["lease_id"], task["worktree_path"]
    if not path:
        return False
    row = conn.execute(
        "SELECT id FROM leases WHERE task_id = ? AND worktree_path = ? AND status = 'active'",
        (int(task["id"]), path),
    ).fetchone()
    if row is None:
        return False
    return not lease_id or row["id"] == lease_id


def task_commits(conn, task) -> tuple[str, str] | str:
    """Where this task's own commits can be read: ``(git_dir, ref)``, or the reason not.

    Two places are trusted, in order. The lease worktree, while an active lease
    of this task owns the path and the checkout is on the task's branch — that
    is the one moment a path proves ownership. Failing that, the task's branch
    as a ref of the base clone, which every lease worktree shares refs with and
    which outlives the slot. A recorded path that fails the first test is never
    read: it is somebody else's checkout now, or nobody's.
    """
    branch = task["branch"]
    if not branch:
        return "no branch was ever leased"
    worktree = task["worktree_path"]
    if worktree and _active_lease_owns(conn, task) and Path(worktree).is_dir():
        rc, out = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
        if rc == 0 and out.strip() == branch:
            return worktree, "HEAD"
    repo = conn.execute("SELECT local_path FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
    local = repo["local_path"] if repo is not None else None
    if local and os.path.isdir(local):
        rc, _ = _git(local, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        if rc == 0:
            return local, f"refs/heads/{branch}"
    return f"branch {branch} exists neither in an owned worktree nor in the base clone"


def added_migrations(conn, task, *, glob: str | None = None) -> list[AddedMigration]:
    """The migration files this task's commits add, relative to its own base.

    Added files only (``--diff-filter=A``): editing an existing revision is not
    what puts a second head on the graph. A task with no base, or whose commits
    cannot be located (see :func:`task_commits`), contributes nothing rather than
    raising — this is an advisory, and it never gets to be the reason a dispatch
    failed. Callers that must say so use :func:`inventory` instead.
    """
    found, _unverifiable = inventory(conn, task, glob=glob)
    return found


def inventory(
    conn, task, *, glob: str | None = None
) -> tuple[list[AddedMigration], Unverifiable | None]:
    """This task's added migrations, or the reason they could not be read."""
    base = task["base_sha"]
    located = task_commits(conn, task)
    if isinstance(located, str) or not base:
        reason = located if isinstance(located, str) else "no base commit recorded"
        return [], Unverifiable(
            task_id=int(task["id"]),
            title=task["title"],
            status=task["status"],
            branch=task["branch"],
            reason=reason,
        )
    git_dir, ref = located
    if glob is None:
        glob = glob_for_repo_id(conn, task["repo_id"])
    rc, out = _git(git_dir, "diff", "--name-only", "--diff-filter=A", f"{base}..{ref}")
    if rc != 0:
        return [], Unverifiable(
            task_id=int(task["id"]),
            title=task["title"],
            status=task["status"],
            branch=task["branch"],
            reason=f"git diff {base[:8]}..{ref} failed",
        )
    found: list[AddedMigration] = []
    for line in out.splitlines():
        path = line.strip()
        if not path or not matches(path, glob):
            continue
        found.append(
            AddedMigration(
                task_id=int(task["id"]),
                title=task["title"],
                status=task["status"],
                branch=task["branch"],
                path=path,
                down_revision=_down_revision_at(git_dir, ref, path),
            )
        )
    return found, None


def _touched_recently(task, *, now: datetime) -> bool:
    stamp = task["updated_at"] or task["created_at"]
    try:
        touched = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return False
    if touched.tzinfo is None:
        touched = touched.replace(tzinfo=UTC)
    return now - touched <= IN_FLIGHT_WINDOW


def _has_live_runner(conn, task_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM runners WHERE task_id = ? AND status IN ('starting', 'running') LIMIT 1",
        (task_id,),
    ).fetchone()
    return row is not None


def _sibling_tasks(conn, repo_id, *, exclude: set[int], include_delivered: bool) -> list:
    """Other tasks in this repository whose migration could still collide.

    In flight means a status in :data:`IN_FLIGHT_STATUSES` and either a live
    runner or a touch within :data:`IN_FLIGHT_WINDOW`. ``include_delivered`` adds
    a task that is delivered but not merged, with no window: its pull request is
    open, its migration has not landed, and it is exactly as able to produce a
    second head as one still being written.
    """
    if not repo_id:
        return []
    rows = conn.execute("SELECT * FROM tasks WHERE repo_id = ? ORDER BY id", (repo_id,)).fetchall()
    now = datetime.now(UTC)
    kept = []
    for row in rows:
        task_id = int(row["id"])
        if task_id in exclude:
            continue
        status = row["status"]
        if status == "delivered":
            if include_delivered and not row["merged_at"]:
                kept.append(row)
            continue
        if status not in IN_FLIGHT_STATUSES:
            continue
        if _touched_recently(row, now=now) or _has_live_runner(conn, task_id):
            kept.append(row)
    return kept


def in_flight_migrations(
    conn, repo_id, *, exclude: set[int] | None = None, include_delivered: bool = False
) -> list[AddedMigration]:
    """Every migration the other live tasks in this repository already add."""
    found, _unverifiable = in_flight_inventory(
        conn, repo_id, exclude=exclude, include_delivered=include_delivered
    )
    return found


def in_flight_inventory(
    conn, repo_id, *, exclude: set[int] | None = None, include_delivered: bool = False
) -> tuple[list[AddedMigration], list[Unverifiable]]:
    """The in-flight migrations, and the in-flight tasks whose commits could not be read."""
    glob = glob_for_repo_id(conn, repo_id)
    found: list[AddedMigration] = []
    unverifiable: list[Unverifiable] = []
    for row in _sibling_tasks(
        conn, repo_id, exclude=exclude or set(), include_delivered=include_delivered
    ):
        added, missing = inventory(conn, row, glob=glob)
        found.extend(added)
        if missing is not None:
            unverifiable.append(missing)
    return found, unverifiable


def dispatch_advisory(conn, repo_row, *, stack_on: int | None = None) -> str | None:
    """What to tell the manager, at dispatch, about migrations already in flight.

    A task this dispatch is stacking on is not reported: its migration is about to
    be part of the new worker's starting commit, which is the whole point of
    ``--stack-on`` and the opposite of a second head.
    """
    exclude = {int(stack_on)} if stack_on else set()
    others = in_flight_migrations(conn, repo_row["id"], exclude=exclude)
    if not others:
        return None
    lines = [
        "migration advisory: work already in flight in this repository adds a database "
        "migration, and a second one started from the same head leaves two Alembic heads "
        "when both merge."
    ]
    for other in others:
        parent = f" (down_revision {other.down_revision!r})" if other.down_revision else ""
        lines.append(f"  {other.describe()}{parent}")
    suggestions = ", ".join(f"--stack-on {other.task_id}" for other in _unique_tasks(others))
    lines.append(
        "  Nothing was refused. To build on that work instead of beside it, dispatch with "
        f"{suggestions}."
    )
    return "\n".join(lines)


def _unique_tasks(found: list[AddedMigration]) -> list[AddedMigration]:
    seen: set[int] = set()
    out = []
    for item in found:
        if item.task_id in seen:
            continue
        seen.add(item.task_id)
        out.append(item)
    return out


def review_conflicts(conn, task) -> list[tuple[AddedMigration, AddedMigration]]:
    """Pairs of (this task's migration, another task's) that share a parent revision.

    This is the two-heads condition itself, not a guess about one: both diffs
    exist, both name a ``down_revision``, and the two are the same string.
    """
    conflicts, _unverifiable = review_findings(conn, task)
    return conflicts


def review_findings(
    conn, task
) -> tuple[list[tuple[AddedMigration, AddedMigration]], list[Unverifiable]]:
    """The collisions, and the in-flight tasks that could not be checked against.

    An unverifiable sibling is reported only when this task adds a migration at
    all: with nothing on this side, there is nothing for the other side to
    collide with, whatever it holds.
    """
    glob = glob_for_repo_id(conn, task["repo_id"])
    mine = [m for m in added_migrations(conn, task, glob=glob) if m.down_revision]
    if not mine:
        return [], []
    others, unverifiable = in_flight_inventory(
        conn, task["repo_id"], exclude={int(task["id"])}, include_delivered=True
    )
    conflicts = [
        (m, other) for m in mine for other in others if other.down_revision == m.down_revision
    ]
    return conflicts, unverifiable


def review_flag(conn, task) -> str | None:
    """The lines `ppy review show` prints above the diffstat, or None when clear."""
    conflicts, unverifiable = review_findings(conn, task)
    if not conflicts and not unverifiable:
        return None
    lines: list[str] = []
    if conflicts:
        lines.append(
            "MIGRATION COLLISION: this change adds a migration off a revision another "
            "unmerged task in this repository also builds on. Both merging leaves two "
            "Alembic heads."
        )
        for mine, other in conflicts:
            lines.append(
                f"  {mine.path} here and {other.path} on {other.task_label} both declare "
                f"down_revision {mine.down_revision!r}."
            )
        lines.append(
            "  Sequence them before delivering — rebase one onto the other, or give the "
            "later one the earlier revision as its parent."
        )
    if unverifiable:
        lines.append(
            "MIGRATION CHECK INCOMPLETE: this change adds a migration, and the commits of "
            "these in-flight tasks could not be read, so they were not checked against it."
        )
        for item in unverifiable:
            lines.append(f"  {item.describe()}")
    return "\n".join(lines)
