"""Isolated git fixtures for live probes.

Probes must never run against the Papaya Agent Runtime repository itself. Each probe
provider gets a throwaway temp git repository (and, where needed, a linked
worktree) that is deleted after the run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass


@dataclass
class Fixture:
    root: str
    worktree: str | None = None

    def cleanup(self) -> None:
        for path in (self.worktree, self.root):
            if path and os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)


def _git(args: list[str], cwd: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def make_repo(prefix: str = "ppy-probe-") -> Fixture:
    """Create an isolated git repo with one commit and a README to edit."""
    root = tempfile.mkdtemp(prefix=prefix)
    _git(["init", "-q", "-b", "main"], cwd=root)
    _git(["config", "user.name", "PPY Probe"], cwd=root)
    _git(["config", "user.email", "probe@papaya-agent-runtime.local"], cwd=root)
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("# Probe fixture\n\nScratch repository for interrupt/resume probes.\n")
    with open(os.path.join(root, "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("initial\n")
    _git(["add", "."], cwd=root)
    _git(["commit", "-q", "-m", "initial"], cwd=root)
    return Fixture(root=root)


def add_worktree(fixture: Fixture, name: str = "wt") -> str:
    """Add a linked worktree to the fixture and return its path."""
    worktree = os.path.join(tempfile.mkdtemp(prefix="ppy-probe-wt-"), name)
    _git(["worktree", "add", "-q", worktree, "HEAD"], cwd=fixture.root)
    fixture.worktree = os.path.dirname(worktree)
    return worktree


def git_dirty(cwd: str) -> bool:
    """Return True if the working tree has uncommitted changes."""
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(proc.stdout.strip())
