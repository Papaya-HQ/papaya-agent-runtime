"""Opt-in live test for the real treehouse lease backend.

Run with ``uv run pytest -m live tests/live/test_treehouse_live.py`` on a machine
where the ``treehouse`` binary is installed. It exercises the exact interface the
lease backend depends on (``get --lease --json`` / ``return``) and cleans the
pooled worktree up afterwards.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.worktree import LeaseManager

pytestmark = pytest.mark.live


def _has_treehouse() -> bool:
    return shutil.which("treehouse") is not None


@pytest.mark.skipif(not _has_treehouse(), reason="treehouse not installed")
def test_treehouse_lease_roundtrip(ppy_home, source_repo, monkeypatch) -> None:
    # Explicit backend overrides the conftest git default.
    monkeypatch.setenv("PPY_LEASE_BACKEND", "git")  # prove explicit arg wins
    mgr = LeaseManager(backend="treehouse")
    assert mgr.backend == "treehouse"

    lease = mgr.acquire(repo_path=source_repo, task_id=None)
    try:
        assert os.path.isdir(lease.worktree_path)
        assert lease.base_sha
        # A task branch was created inside the pooled worktree.
        current = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=lease.worktree_path,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert current == lease.branch

        # A real commit lands in the leased worktree.
        with open(os.path.join(lease.worktree_path, "live.txt"), "w", encoding="utf-8") as fh:
            fh.write("treehouse lease\n")
        subprocess.run(["git", "add", "-A"], cwd=lease.worktree_path, check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "live"],
            cwd=lease.worktree_path,
            check=True,
        )
        assert mgr.head_sha(lease) != lease.base_sha

        conn = init_db()
        active = store.active_leases(conn)
        assert any(entry["id"] == lease.id and entry["backend"] == "treehouse" for entry in active)
    finally:
        mgr.release(lease, repo_path=source_repo)
        # Best-effort: remove the now-idle pooled worktree so we leave no state.
        subprocess.run(
            ["treehouse", "destroy", lease.worktree_path, "--yes", "--include-unlanded"],
            capture_output=True,
            text=True,
            check=False,
        )

    conn = init_db()
    assert all(entry["id"] != lease.id for entry in store.active_leases(conn))
