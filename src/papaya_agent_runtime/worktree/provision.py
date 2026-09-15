"""Getting a freshly leased worktree ready before the worker sees it.

Three separate workers in the 2026-08-31 window recorded the same thing as the
largest remaining cost per dispatch: roughly ten minutes before touching the
actual task, spent creating a virtualenv the base clone already had, re-filling a
dependency cache from scratch, and escalating out of the sandbox to write
anywhere. That is a setup problem being solved once per task by whoever happened
to be dispatched.

Two of the three are answered by the worker's environment (see
``supervisor.runner.worker_env``: a writable ``UV_CACHE_DIR`` and ``PPY_HOME``
under ``.ppy``). The third is answered here, and is opt-in per repository, because
what a repo needs installed is a fact about that repo:

- ``ppy repo provision <name> --command "..."`` runs that command in the new
  worktree before the worker starts, and records an event with its exit status;
- ``ppy repo provision <name> --reuse-venv backend/.venv`` links the virtualenv
  that already exists in the base clone into the same place in the worktree.

A repository with neither configured behaves exactly as it did before: nothing
runs, nothing is linked, and no event is recorded.

Nothing here may fail a dispatch. A provision hook that exits non-zero has cost
the worker the head start, not the task — the worker can still install what it
needs — so the exit status is recorded and the dispatch continues.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from papaya_agent_runtime.state import init_db, store

#: How long a provision hook may run. A real dependency install is minutes, not
#: seconds, but a hook that hangs must not hold a dispatch open forever.
TIMEOUT_ENV = "PPY_PROVISION_TIMEOUT"
DEFAULT_TIMEOUT = 900

#: How much of a hook's output is worth keeping on the event.
OUTPUT_TAIL = 2000


@dataclass
class ProvisionResult:
    """What provisioning did for one worktree. Empty when nothing was configured."""

    repo: str
    worktree_path: str
    task_id: int | None = None
    venv: dict = field(default_factory=dict)
    hook: dict = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.venv or self.hook)


def timeout_seconds() -> int:
    raw = os.environ.get(TIMEOUT_ENV)
    try:
        return max(1, int(raw)) if raw else DEFAULT_TIMEOUT
    except ValueError:
        return DEFAULT_TIMEOUT


def _run(command: str, cwd: str, env: dict[str, str] | None) -> subprocess.CompletedProcess:
    """Run a configured hook. Its own shell, because it is a shell command."""
    return subprocess.run(
        command,
        shell=True,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds(),
    )


def _git_path(worktree: str, relative: str) -> Path | None:
    proc = subprocess.run(
        ["git", "-C", worktree, "rev-parse", "--git-path", relative],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    path = Path(proc.stdout.strip())
    return path if path.is_absolute() else Path(worktree) / path


def exclude_locally(worktree: str, relative: str) -> bool:
    """Hide a provisioned path from git in this worktree only.

    The linked virtualenv is not part of the branch, and the end-of-task
    auto-commit stages whatever git offers it. Writing the path into the
    worktree's own exclude file keeps it out of ``git status`` without touching a
    ``.gitignore`` the repository owns.
    """
    info = _git_path(worktree, "info/exclude")
    if info is None:
        return False
    try:
        info.parent.mkdir(parents=True, exist_ok=True)
        existing = info.read_text() if info.exists() else ""
        line = f"/{relative.strip('/')}"
        if line in existing.splitlines():
            return True
        prefix = "" if existing.endswith("\n") or not existing else "\n"
        info.write_text(f"{existing}{prefix}{line}\n")
    except OSError:
        return False
    return True


def link_venv(base_clone: str, worktree: str, relative: str) -> dict:
    """Point the worktree at the virtualenv the base clone already has.

    A symlink is the whole point — the win is not copying gigabytes of installed
    packages per task — but a filesystem that refuses one gets a copy rather than
    nothing. The venv's own absolute paths keep resolving to the base clone,
    which is what makes this work at all.
    """
    result: dict = {"relative": relative, "linked": False, "how": "", "detail": ""}
    source = Path(base_clone) / relative
    dest = Path(worktree) / relative
    if not source.is_dir():
        result["detail"] = f"the base clone has no {relative} to reuse"
        return result
    if dest.exists() or dest.is_symlink():
        result["detail"] = f"{relative} is already in the worktree; left it alone"
        return result
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.symlink_to(source, target_is_directory=True)
        result.update(linked=True, how="symlinked", detail=f"{relative} -> {source}")
    except OSError:
        try:
            shutil.copytree(source, dest, symlinks=True)
        except OSError as exc:
            result["detail"] = f"could not reuse {relative}: {exc}"
            return result
        result.update(linked=True, how="copied", detail=f"{relative} copied from {source}")
    exclude_locally(worktree, relative)
    return result


def run_hook(command: str, worktree: str, *, env: dict[str, str] | None = None) -> dict:
    """Run the repo's provision command in the new worktree, and say how it went."""
    result: dict = {"command": command, "ran": False, "exit_code": None, "seconds": 0.0}
    started = time.monotonic()
    try:
        proc = _run(command, worktree, env)
    except subprocess.TimeoutExpired:
        result["seconds"] = round(time.monotonic() - started, 1)
        result["detail"] = f"provision hook timed out after {timeout_seconds()}s"
        return result
    except (OSError, subprocess.SubprocessError) as exc:
        result["seconds"] = round(time.monotonic() - started, 1)
        result["detail"] = f"provision hook could not be run: {exc}"
        return result
    output = f"{proc.stdout or ''}{proc.stderr or ''}".strip()
    result.update(
        ran=True,
        exit_code=proc.returncode,
        seconds=round(time.monotonic() - started, 1),
        output=output[-OUTPUT_TAIL:],
        detail=(
            f"provision hook succeeded in {round(time.monotonic() - started, 1)}s"
            if proc.returncode == 0
            else f"provision hook exited {proc.returncode}; the worker starts anyway"
        ),
    )
    return result


def provision_worktree(
    repo_row: sqlite3.Row | dict,
    worktree_path: str,
    *,
    task_id: int | None = None,
    run_id: int | None = None,
    env: dict[str, str] | None = None,
    conn: sqlite3.Connection | None = None,
) -> ProvisionResult:
    """Prepare a leased worktree per its repo's configuration. Never raises."""
    name = repo_row["name"]
    result = ProvisionResult(repo=name, worktree_path=worktree_path, task_id=task_id)
    try:
        keys = repo_row.keys() if hasattr(repo_row, "keys") else []
        venv = repo_row["provision_venv"] if "provision_venv" in keys else None
        command = repo_row["provision_command"] if "provision_command" in keys else None
        if venv:
            result.venv = link_venv(repo_row["local_path"], worktree_path, venv)
        if command:
            from papaya_agent_runtime.supervisor.runner import worker_env

            result.hook = run_hook(command, worktree_path, env=env or worker_env())
        if not result.configured:
            return result
        conn = conn or init_db()
        store.append_event(
            conn,
            kind="worktree_provisioned",
            payload={
                "task_id": task_id,
                "repo": name,
                "worktree_path": worktree_path,
                "venv": result.venv or None,
                "hook": result.hook or None,
            },
            run_id=run_id,
            task_id=task_id,
        )
    except Exception as exc:  # noqa: BLE001 - a head start is never worth a failed dispatch
        result.hook = {
            "command": None,
            "ran": False,
            "exit_code": None,
            "detail": f"provisioning skipped: {exc}",
        }
    return result


def describe(result: ProvisionResult | None) -> list[str]:
    """Lines a command can print after provisioning. Empty when nothing was set up."""
    if result is None or not result.configured:
        return []
    lines = []
    if result.venv:
        lines.append(f"venv: {result.venv.get('detail')}")
    if result.hook:
        lines.append(f"hook: {result.hook.get('detail')}")
    return lines
