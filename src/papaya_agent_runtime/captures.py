"""The receipts a worker filed, pulled back out of its reports and listed for review.

The rule the third performance review produced: never approve a change to something
visual without opening the worker's captures. ``ppy review show`` printed the report
and the diffstat, so the paths the worker named stayed buried in prose and a reviewer
could approve on the strength of a sentence like "screenshots are in
docs/evidence/task-104/".

This module reads those paths back out of the reports and turns them into a listing —
every named file or directory with its size, every missing one marked "(not found)"
rather than failing the command, and every image printed once more on a line of its
own so the reviewer can open it without retyping a path.

A path counts as evidence when the worker wrote it as an absolute path, when it sits
under one of the repo-relative roots the evidence contract names
(:data:`EVIDENCE_ROOTS`), or when any directory along it calls itself receipts,
evidence, captures, or screenshots.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

#: Repo-relative roots the brief's evidence contract already names, and the
#: in-worktree directory the environment block pins (issue #60).
EVIDENCE_ROOTS = ("docs/generated/", "docs/evidence/", ".ppy-evidence/")
#: Words that make a directory a receipts directory whatever else it is called.
EVIDENCE_WORDS = ("receipt", "evidence", "capture", "screenshot")
#: Files worth handing the reviewer as an openable path.
IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif", ".pdf"}
)
#: How many entries of one directory to print before summarising the rest.
MAX_DIR_ENTRIES = 40

# Prose separators: quotes, backticks, brackets, and the punctuation that ends a
# sentence rather than a filename. Markdown links fall apart into their target here.
_SPLIT = re.compile(r"[\s`'\"()\[\]<>|{}]+")
_TRAILING = ".,;:!?*_"


def _clean(token: str) -> str:
    token = token.strip().rstrip(_TRAILING).lstrip("-*")
    while token.startswith("./"):
        token = token[2:]
    return token.rstrip("/")


def _is_evidence(token: str) -> bool:
    if "/" not in token or "://" in token or token.startswith("//"):
        return False
    if token.startswith(("/", "~/")):
        return True
    if token.startswith(EVIDENCE_ROOTS):
        return True
    segments = token.split("/")[:-1] if "." in token.rsplit("/", 1)[-1] else token.split("/")
    return any(word in segment.lower() for segment in segments for word in EVIDENCE_WORDS)


def capture_paths(notes: Iterable[str]) -> list[str]:
    """The evidence paths named across ``notes``, in the order they were written.

    Duplicates collapse, so a path repeated in a plan note and again in the done
    note is listed once.
    """
    found: list[str] = []
    for note in notes:
        for raw in _SPLIT.split(note or ""):
            token = _clean(raw)
            if token and _is_evidence(token) and token not in found:
                found.append(token)
    return found


def _resolve(token: str, root: Path | None) -> Path:
    path = Path(token).expanduser()
    if path.is_absolute() or root is None:
        return path
    return root / path


def _size(path: Path) -> str:
    from papaya_agent_runtime.worktree.reclaim import human_bytes

    try:
        return human_bytes(path.stat().st_size)
    except OSError:
        return "size unavailable"


def render(paths: Iterable[str], root: str | Path | None = None) -> str:
    """A reviewer-facing listing of ``paths``, or "" when the reports named none."""
    paths = list(paths)
    if not paths:
        return ""
    base = Path(root).expanduser() if root else None
    lines = ["captures and receipts named in the worker's reports:"]
    images: list[str] = []

    for token in paths:
        target = _resolve(token, base)
        if target.is_dir():
            entries = sorted(target.iterdir(), key=lambda p: p.name)
            lines.append(f"  {token} — directory, {len(entries)} item(s)")
            for entry in entries[:MAX_DIR_ENTRIES]:
                if entry.is_dir():
                    lines.append(f"      {entry.name}/ — directory")
                    continue
                lines.append(f"      {entry.name}  {_size(entry)}")
                if entry.suffix.lower() in IMAGE_SUFFIXES:
                    images.append(str(entry))
            if len(entries) > MAX_DIR_ENTRIES:
                lines.append(f"      … and {len(entries) - MAX_DIR_ENTRIES} more")
        elif target.is_file():
            lines.append(f"  {token} — {_size(target)}")
            if target.suffix.lower() in IMAGE_SUFFIXES:
                images.append(str(target))
        else:
            # A path that has gone (or was never written) is worth saying out loud;
            # it is never worth failing the review over.
            lines.append(f"  {token} — (not found)")

    if images:
        lines.append("")
        lines.append("open these before approving anything visual (one path per line):")
        lines.extend(images)
    return "\n".join(lines)


__all__ = [
    "EVIDENCE_ROOTS",
    "EVIDENCE_WORDS",
    "IMAGE_SUFFIXES",
    "capture_paths",
    "render",
]
