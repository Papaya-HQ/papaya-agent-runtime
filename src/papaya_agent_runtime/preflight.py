"""Dispatch preflight: the checks a manager kept in its head, moved into the tool.

Two dispatches were lost on 2026-08-30/31 to conditions ``ppy`` could have caught
before creating a task: a brief read from a file that no longer existed (the
worker started with empty instructions) and a worktree checkout that died on a
full disk. ``ppy dispatch --brief <file>`` now refuses an empty brief, refuses to
dispatch onto a nearly full disk, and archives the brief it sent next to the
instance state so the exact packet a worker received is always recoverable.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from papaya_agent_runtime.paths import ppy_home

MIN_FREE_GB_ENV = "PPY_MIN_FREE_GB"
DEFAULT_MIN_FREE_GB = 5.0

#: A brief's first Markdown heading, however many "#" marks it carries.
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*$")
#: A task title is a one-line label, not a paragraph; longer headings are cut here.
TITLE_CAP = 120


class PreflightError(RuntimeError):
    """A dispatch was refused before any task state was created."""


def read_brief(path: str | Path) -> str:
    """Return the brief's text, refusing a missing or empty file plainly."""
    brief_path = Path(path).expanduser()
    if not brief_path.is_file():
        raise PreflightError(
            f"brief file not found: {brief_path} — nothing was dispatched. "
            "Write the brief to a durable location (for example .ppy/briefs/<repo>/) first."
        )
    text = brief_path.read_text(encoding="utf-8")
    if not text.strip():
        raise PreflightError(
            f"brief file is empty: {brief_path} — refusing to dispatch a worker with no "
            "instructions (this is how an empty-brief worker was sent out on 2026-08-30)."
        )
    return text


def title_from_brief(text: str) -> str | None:
    """The brief's first Markdown heading, shaped into a task title.

    A brief already opens with the outcome it wants, written carefully; a title
    typed at the prompt is the same sentence typed again, worse. Leading ``#``
    marks and surrounding whitespace go, internal whitespace collapses to single
    spaces, and the result is capped so a title stays a label. Returns ``None``
    when the brief has no heading at all, which the caller reports plainly.
    """
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match is None:
            continue
        heading = " ".join(match.group(1).split())
        if heading:
            return heading[:TITLE_CAP]
    return None


def min_free_gb() -> float:
    """The free-space floor a dispatch requires, in gigabytes (env-overridable)."""
    raw = os.environ.get(MIN_FREE_GB_ENV)
    if not raw:
        return DEFAULT_MIN_FREE_GB
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_MIN_FREE_GB


def check_disk(path: str | Path | None = None, *, floor_gb: float | None = None) -> float:
    """Return free gigabytes at ``path`` (the instance root by default) or refuse.

    A worker needs room for a worktree checkout and a dependency install — several
    gigabytes on a real repository. Dispatching below the floor produces a
    half-created checkout that also blocks the pool until someone removes it.
    """
    target = Path(path) if path is not None else ppy_home()
    probe = target if target.exists() else target.parent
    usage = shutil.disk_usage(probe)
    free_gb = usage.free / 1e9
    floor = min_free_gb() if floor_gb is None else floor_gb
    if free_gb < floor:
        raise PreflightError(
            f"only {free_gb:.1f} GB free on the volume holding {probe} (floor {floor:g} GB; "
            f"set {MIN_FREE_GB_ENV} to change it) — free space before dispatching; a "
            f"worktree checkout plus a dependency install needs several GB. {reclaim_hint()}"
        )
    return free_gb


def reclaim_hint() -> str:
    """Point a refused dispatch at the space it already owns.

    Most of the time the disk is full of the harness's own finished worktrees —
    20 GB of delivered tasks on 2026-09-02 — so a refusal that only says "free
    space" sends the manager hunting when one command would do it.
    """
    from papaya_agent_runtime.worktree.reclaim import human_bytes, reclaimable_bytes

    freeable = reclaimable_bytes()
    if freeable <= 0:
        return (
            "`ppy worktree prune` has nothing to reclaim right now; "
            "`ppy worktree list` shows what each slot is holding."
        )
    return (
        f"`ppy worktree prune` would reclaim {human_bytes(freeable)} from the worktrees of "
        "finished tasks (`ppy worktree list` shows what each slot is holding)."
    )


def archived_brief_path(repo: str, task_id: int) -> Path:
    """Where a task's brief is kept, whether or not it has been written yet."""
    return ppy_home() / "briefs" / repo / f"task-{task_id}.md"


def archive_brief(repo: str, task_id: int, text: str) -> Path:
    """Keep the exact brief a task received under the instance's briefs directory."""
    target = archived_brief_path(repo, task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target
