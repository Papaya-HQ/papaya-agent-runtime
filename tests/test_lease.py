"""Hermetic tests for worktree leases (git backend) and treehouse helpers."""

from __future__ import annotations

import os

import pytest

from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.worktree import LeaseManager
from papaya_agent_runtime.worktree.lease import (
    LeaseError,
    _parse_lease_json,
    _th_acquire_argv,
    _th_return_argv,
    resolve_backend,
)


def test_acquire_and_release(ppy_home, source_repo) -> None:
    mgr = LeaseManager(backend="git")
    # task_id=None keeps this a pure lease test (no FK to a task row).
    lease = mgr.acquire(repo_path=source_repo, task_id=None)
    assert os.path.isdir(lease.worktree_path)
    assert lease.branch.startswith("ppy/task-adhoc-")
    assert lease.base_sha

    conn = init_db()
    assert len(store.active_leases(conn)) == 1

    # The worktree is a real checkout of the base commit.
    assert os.path.exists(os.path.join(lease.worktree_path, "README.md"))
    assert mgr.head_sha(lease) == lease.base_sha
    assert mgr.is_dirty(lease) is False

    mgr.release(lease, repo_path=source_repo)
    assert not os.path.isdir(lease.worktree_path)
    assert len(store.active_leases(conn)) == 0


def test_treehouse_argv_helpers() -> None:
    assert _th_acquire_argv("ppy-task-7") == [
        "treehouse",
        "get",
        "--lease",
        "--json",
        "--lease-holder",
        "ppy-task-7",
    ]
    assert _th_return_argv("/pool/wt", "abc123") == [
        "treehouse",
        "return",
        "/pool/wt",
        "--force",
        "--if-lease-id",
        "abc123",
    ]


def test_parse_lease_json_shape() -> None:
    payload = (
        '{"path":"/Users/x/.treehouse/repo-ab/1/repo",'
        '"lease_id":"663f81d6432aeba95af3f44d31cb8f4f",'
        '"lease_holder":"ppy-task-1","leased_at":"2026-08-29T07:23:58-07:00"}'
    )
    data = _parse_lease_json(payload)
    assert data["path"].endswith("/repo")
    assert data["lease_id"] == "663f81d6432aeba95af3f44d31cb8f4f"


def test_parse_lease_json_rejects_garbage() -> None:
    with pytest.raises(LeaseError):
        _parse_lease_json("not json")
    with pytest.raises(LeaseError):
        _parse_lease_json('{"lease_id":"x"}')  # missing path


def test_resolve_backend_precedence(monkeypatch) -> None:
    # Explicit wins over env.
    monkeypatch.setenv("PPY_LEASE_BACKEND", "git")
    assert resolve_backend("treehouse") == "treehouse"
    # Env wins over auto-detect.
    assert resolve_backend("auto") == "git"
    assert resolve_backend(None) == "git"
    monkeypatch.setenv("PPY_LEASE_BACKEND", "treehouse")
    assert resolve_backend(None) == "treehouse"
    monkeypatch.setenv("PPY_LEASE_BACKEND", "nonsense")
    with pytest.raises(LeaseError):
        resolve_backend(None)
