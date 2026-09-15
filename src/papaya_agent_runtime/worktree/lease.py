"""Worktree lease lifecycle.

A lease is an identity-bound, isolated Git worktree branched from a repository's
base clone. Workers only ever touch their leased worktree.

Two backends implement the same ``Lease`` contract:

- ``git`` — the always-available reference backend using plain ``git worktree``
  under ``.ppy/worktree-pools/``.
- ``treehouse`` — the pinned companion (https://github.com/kunchenguid/treehouse),
  which maintains a pool of pre-warmed, reusable worktrees. We use its
  non-interactive lease interface: ``treehouse get --lease --json`` durably
  reserves a pooled worktree and prints ``{path, lease_id, ...}``; ``treehouse
  return <path> --force --if-lease-id <id>`` releases it.

Backend selection: an explicit ``backend`` argument wins; otherwise ``auto``
consults ``PPY_LEASE_BACKEND`` and then prefers ``treehouse`` when its binary is on
PATH, falling back to ``git``. Either way the lease is recorded in SQLite so
recovery and reconcile can find and clean up orphans.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from papaya_agent_runtime.companions import companion_bin, has_companion
from papaya_agent_runtime.paths import worktree_pools_dir
from papaya_agent_runtime.state import init_db, store


class LeaseError(Exception):
    pass


@dataclass
class Lease:
    id: str
    repo_id: int | None
    task_id: int | None
    branch: str
    worktree_path: str
    base_sha: str | None
    backend: str


def _git(args: list[str], cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise LeaseError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


# --------------------------------------------------------------------------- #
# Treehouse backend helpers (pure argv/JSON so they are unit-testable)
# --------------------------------------------------------------------------- #


def _th_acquire_argv(holder: str) -> list[str]:
    return ["treehouse", "get", "--lease", "--json", "--lease-holder", holder]


def _th_return_argv(path: str, lease_id: str) -> list[str]:
    return ["treehouse", "return", path, "--force", "--if-lease-id", lease_id]


def _parse_lease_json(text: str) -> dict:
    """Parse ``treehouse get --lease --json`` stdout into a lease dict."""
    try:
        data = json.loads(text.strip())
    except (ValueError, TypeError) as exc:
        raise LeaseError(f"could not parse treehouse lease JSON: {text!r}") from exc
    if "path" not in data or "lease_id" not in data:
        raise LeaseError(f"treehouse lease JSON missing path/lease_id: {data!r}")
    return data


def _run_treehouse(argv: list[str], cwd: str) -> str:
    # Prefer the provisioned copy under .ppy/tools/bin; fall back to PATH. The argv
    # builders stay pure (argv[0] == "treehouse") so only the executable is swapped.
    resolved = companion_bin(argv[0]) or argv[0]
    proc = subprocess.run(
        [resolved, *argv[1:]], cwd=cwd, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        # Update banners go to stderr; only surface it on failure.
        raise LeaseError(f"{' '.join(argv)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def resolve_backend(backend: str | None) -> str:
    """Resolve a backend name. Explicit wins; else env; else auto-detect.

    ``PPY_LEASE_BACKEND`` lets the runtime prefer git (e.g. the hermetic test
    suite) or force treehouse without code changes.
    """
    if backend not in (None, "auto"):
        if backend not in ("git", "treehouse"):
            raise LeaseError(f"unknown lease backend {backend!r}")
        return backend
    env = os.environ.get("PPY_LEASE_BACKEND")
    if env in ("git", "treehouse"):
        return env
    if env not in (None, "", "auto"):
        raise LeaseError(f"unknown PPY_LEASE_BACKEND {env!r}")
    return "treehouse" if has_companion("treehouse") else "git"


class LeaseManager:
    """Acquire and release worktree leases against a repository base clone."""

    def __init__(self, backend: str | None = None) -> None:
        self.backend = resolve_backend(backend)

    def acquire(
        self,
        *,
        repo_path: str,
        repo_id: int | None = None,
        task_id: int | None = None,
        branch_prefix: str = "ppy",
        branch: str | None = None,
    ) -> Lease:
        """Lease a fresh worktree; a fresh branch unless ``branch`` names the task's own.

        ``branch`` is for a task getting its worktree *back* (issue #58): the
        pull request, the remote, and every record already carry that name, so
        the rebuilt checkout is put on it — the existing local branch when the
        base clone still has one, a new branch at the clone's head otherwise.
        The caller moves it to the right commit afterwards.
        """
        if self.backend == "treehouse":
            return self._acquire_treehouse(repo_path, repo_id, task_id, branch_prefix, branch)
        return self._acquire_git(repo_path, repo_id, task_id, branch_prefix, branch)

    def _acquire_git(
        self,
        repo_path: str,
        repo_id: int | None,
        task_id: int | None,
        branch_prefix: str,
        branch: str | None,
    ) -> Lease:
        lease_id = uuid.uuid4().hex[:12]
        pool = worktree_pools_dir()
        pool.mkdir(parents=True, exist_ok=True)
        worktree_path = str(pool / lease_id)

        base_sha = _git(["rev-parse", "HEAD"], cwd=repo_path)
        if branch and _branch_exists(repo_path, branch):
            _git(["worktree", "add", "-q", worktree_path, branch], cwd=repo_path)
        else:
            branch = branch or f"{branch_prefix}/task-{task_id or 'adhoc'}-{lease_id}"
            _git(["worktree", "add", "-q", "-b", branch, worktree_path, "HEAD"], cwd=repo_path)
        return self._record(lease_id, repo_id, task_id, branch, worktree_path, base_sha)

    def _acquire_treehouse(
        self,
        repo_path: str,
        repo_id: int | None,
        task_id: int | None,
        branch_prefix: str,
        branch: str | None,
    ) -> Lease:
        holder = f"ppy-task-{task_id or 'adhoc'}"
        out = _run_treehouse(_th_acquire_argv(holder), cwd=repo_path)
        data = _parse_lease_json(out)
        worktree_path = data["path"]
        lease_id = data["lease_id"]
        base_sha = _git(["rev-parse", "HEAD"], cwd=worktree_path)
        # Treehouse hands out a pooled checkout; give the task its own branch so
        # delivery can push HEAD:<branch> exactly as with the git backend.
        if branch and _branch_exists(worktree_path, branch):
            _git(["switch", branch], cwd=worktree_path)
        else:
            branch = branch or f"{branch_prefix}/task-{task_id or 'adhoc'}-{lease_id[:12]}"
            _git(["switch", "-c", branch], cwd=worktree_path)
        return self._record(lease_id, repo_id, task_id, branch, worktree_path, base_sha)

    def _record(
        self,
        lease_id: str,
        repo_id: int | None,
        task_id: int | None,
        branch: str,
        worktree_path: str,
        base_sha: str,
    ) -> Lease:
        conn = init_db()
        store.add_lease(
            conn,
            lease_id=lease_id,
            repo_id=repo_id,
            task_id=task_id,
            branch=branch,
            worktree_path=worktree_path,
            base_sha=base_sha,
            backend=self.backend,
        )
        return Lease(lease_id, repo_id, task_id, branch, worktree_path, base_sha, self.backend)

    def release(self, lease: Lease, *, repo_path: str, remove_branch: bool = True) -> None:
        if lease.backend == "treehouse":
            self._release_treehouse(lease)
        else:
            self._release_git(lease, repo_path, remove_branch)
        conn = init_db()
        store.release_lease(conn, lease.id)

    def _release_git(self, lease: Lease, repo_path: str, remove_branch: bool) -> None:
        try:
            _git(["worktree", "remove", "--force", lease.worktree_path], cwd=repo_path)
        except LeaseError:
            _git(["worktree", "prune"], cwd=repo_path)
        if remove_branch:
            with_suppressed_branch_delete(repo_path, lease.branch)

    def _release_treehouse(self, lease: Lease) -> None:
        # Guarded by the lease id so we never return someone else's worktree.
        # cwd is irrelevant to `treehouse return <path>`; use the worktree itself.
        cwd = lease.worktree_path if Path(lease.worktree_path).exists() else os.getcwd()
        with contextlib.suppress(LeaseError):
            _run_treehouse(_th_return_argv(lease.worktree_path, lease.id), cwd=cwd)

    def head_sha(self, lease: Lease) -> str:
        if not Path(lease.worktree_path).exists():
            raise LeaseError(f"worktree {lease.worktree_path} is missing")
        return _git(["rev-parse", "HEAD"], cwd=lease.worktree_path)

    def is_dirty(self, lease: Lease) -> bool:
        out = _git(["status", "--porcelain"], cwd=lease.worktree_path)
        return bool(out.strip())


def _branch_exists(repo_path: str, branch: str) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def with_suppressed_branch_delete(repo_path: str, branch: str) -> None:
    """Delete a lease branch, ignoring the not-found case."""
    with contextlib.suppress(LeaseError):
        _git(["branch", "-D", branch], cwd=repo_path)
