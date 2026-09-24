"""What the supervisor's end-of-task auto-commit is allowed to stage.

A worker that finishes without committing gets its work committed for it, so the
manager always has a reviewable head. That commit used to be a blanket
``git add -A``, which swept in whatever else happened to be lying in the
worktree: on 2026-09-02 a task's evidence directory — screenshots and receipts
written for the review, never meant for the branch — landed in the commit and the
commit had to be rebuilt clean before it could ship.

Three filters now decide what is staged:

1. anything ``.gitignore`` ignores is never a candidate (``git status`` does not
   offer ignored paths, and we never use ``add -f``);
2. an ordered exclusion list, last match wins, with ``!`` negating — so ``*.png``
   can be excluded while ``docs/`` keeps its diagrams; and
3. build and environment artifacts (:data:`ARTIFACTS`) the repository does not
   already track. A repository with no ``.gitignore`` has nothing to stop
   ``__pycache__/`` or a fresh ``uv.lock``: on 2026-09-23 both landed on a lease
   branch after the worker had pushed it clean, and review sent the worker back to
   remove files nobody wrote. A lockfile the repository tracks is not untracked, so
   a change to it is committed like any other.

Whatever is held back is reported, not silently dropped: it stays in the worktree
and is named in the task's ``autocommit`` event.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path

EXCLUDE_ENV = "PPY_AUTOCOMMIT_EXCLUDE"

# Ordered; later rules win. A leading "!" keeps a path the earlier rules excluded.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    "evidence/",  # review evidence: captures, receipts, gate output
    ".ppy-evidence/",  # the in-worktree receipt directory the environment block pins
    "receipts/",
    "*.png",  # screenshots a worker took to prove its work
    "!docs/",  # …except the diagrams and images that belong to the repo
    "/private/tmp/",  # anything actually living in scratch space
)

#: What building, testing or installing leaves behind. Held back only while the
#: repository does not track it (see :func:`is_artifact`), and never replaced by
#: ``PPY_AUTOCOMMIT_EXCLUDE``: no configuration makes a stray ``.pyc`` the branch's.
ARTIFACTS: tuple[str, ...] = (
    "__pycache__/",
    "*.pyc",
    ".venv/",
    "node_modules/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    "dist/",
    "build/",
    "*.egg-info/",
    ".ppy-evidence/",
    ".mm-evidence/",
    "uv.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
)


def exclude_rules() -> list[str]:
    """The active exclusion rules. ``PPY_AUTOCOMMIT_EXCLUDE`` replaces the defaults."""
    raw = os.environ.get(EXCLUDE_ENV)
    if raw is None:
        return list(DEFAULT_EXCLUDES)
    return [part.strip() for part in raw.split(",") if part.strip()]


def _matches(path: str, resolved: str, pattern: str) -> bool:
    if pattern.startswith("/"):
        # A location rule: where the path really lives, symlinks resolved.
        root = pattern.rstrip("/")
        return resolved == root or resolved.startswith(root + os.sep)
    if pattern.endswith("/"):
        directory = pattern.rstrip("/")
        return directory in Path(path).parts[:-1]
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(os.path.basename(path), pattern)


def is_excluded(path: str, *, worktree: str, rules: list[str] | None = None) -> bool:
    """Should ``path`` (repo-relative) be held back from the auto-commit?"""
    rules = exclude_rules() if rules is None else rules
    resolved = os.path.realpath(os.path.join(worktree, path))
    excluded = False
    for rule in rules:
        negated = rule.startswith("!")
        pattern = rule[1:] if negated else rule
        if pattern and _matches(path, resolved, pattern):
            excluded = not negated
    return excluded


def artifact_root(path: str) -> str | None:
    """The artifact ``path`` belongs to, or ``None`` when it is not one.

    A directory rule answers with the directory (``pkg/__pycache__/``), so a
    ``.venv`` of ten thousand files is named once, not ten thousand times.
    """
    parts = Path(path).parts
    for rule in ARTIFACTS:
        if rule.endswith("/"):
            directory = rule.rstrip("/")
            for depth, part in enumerate(parts[:-1]):
                if fnmatch.fnmatchcase(part, directory):
                    return "/".join(parts[: depth + 1]) + "/"
        elif fnmatch.fnmatchcase(os.path.basename(path), rule):
            return path
    return None


def is_artifact(path: str) -> bool:
    """Is ``path`` (repo-relative) a build or environment artifact by name?"""
    return artifact_root(path) is not None


def changed_paths(worktree: str) -> list[str]:
    """Every path git would offer to a commit, one file per entry, ignored files absent."""
    return [path for _code, path in changed_entries(worktree)]


def changed_entries(worktree: str) -> list[tuple[str, str]]:
    """``(status code, path)`` for every path git would offer to a commit.

    ``-uall`` expands untracked directories so an exclusion can name a single file;
    ``-z`` avoids git's quoting so a path with a space or a newline survives intact.
    The code is git's two-letter porcelain status; ``??`` is untracked.
    """
    proc = subprocess.run(
        ["git", "-C", worktree, "status", "--porcelain", "-z", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []
    records = [r for r in proc.stdout.split("\0") if r]
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        code, path = record[:2], record[3:]
        entries.append((code, path))
        if code[0] in ("R", "C") and index < len(records):
            # A rename/copy record is followed by its source path; both belong to
            # the same change and must be staged together.
            entries.append((code, records[index]))
            index += 1
    return entries


def stage(worktree: str, *, rules: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Stage everything eligible; return ``(staged, excluded)``, both repo-relative.

    An untracked artifact is named in ``excluded`` by its root (see
    :func:`artifact_root`), once, however many files it holds.
    """
    staged: list[str] = []
    excluded: list[str] = []
    for code, path in changed_entries(worktree):
        root = artifact_root(path) if code == "??" else None
        if root is not None:
            if root not in excluded:
                excluded.append(root)
        elif is_excluded(path, worktree=worktree, rules=rules):
            excluded.append(path)
        else:
            staged.append(path)
    if staged:
        # `add --` also stages deletions, so a removed file is still committed.
        subprocess.run(
            ["git", "-C", worktree, "add", "--", *staged],
            capture_output=True,
            text=True,
            check=False,
        )
    return staged, excluded
