"""What the supervisor's end-of-task auto-commit is allowed to stage.

A worker that finishes without committing gets its work committed for it, so the
manager always has a reviewable head. That commit used to be a blanket
``git add -A``, which swept in whatever else happened to be lying in the
worktree: on 2026-09-02 a task's evidence directory — screenshots and receipts
written for the review, never meant for the branch — landed in the commit and the
commit had to be rebuilt clean before it could ship.

Two filters now decide what is staged:

1. anything ``.gitignore`` ignores is never a candidate (``git status`` does not
   offer ignored paths, and we never use ``add -f``); and
2. an ordered exclusion list, last match wins, with ``!`` negating — so ``*.png``
   can be excluded while ``docs/`` keeps its diagrams.

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


def changed_paths(worktree: str) -> list[str]:
    """Every path git would offer to a commit, one file per entry, ignored files absent.

    ``-uall`` expands untracked directories so an exclusion can name a single file;
    ``-z`` avoids git's quoting so a path with a space or a newline survives intact.
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
    paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        code, path = record[:2], record[3:]
        paths.append(path)
        if code[0] in ("R", "C") and index < len(records):
            # A rename/copy record is followed by its source path; both belong to
            # the same change and must be staged together.
            paths.append(records[index])
            index += 1
    return paths


def stage(worktree: str, *, rules: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Stage everything eligible; return ``(staged, excluded)``, both repo-relative."""
    staged: list[str] = []
    excluded: list[str] = []
    for path in changed_paths(worktree):
        if is_excluded(path, worktree=worktree, rules=rules):
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
