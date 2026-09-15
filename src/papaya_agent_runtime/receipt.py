"""Run a task command and retain its output plus one auditable result line."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from papaya_agent_runtime import environment
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.runner import worker_env


class ReceiptError(Exception):
    """A receipt command could not be started safely."""


@dataclass(frozen=True)
class ReceiptResult:
    output_path: str
    ledger_path: str
    exit_code: int
    elapsed: float
    head_sha: str
    result_line: str


def _slug(command: list[str]) -> str:
    words = [Path(command[0]).name, *command[1:3]]
    slug = re.sub(r"[^a-z0-9]+", "-", "-".join(words).lower()).strip("-")
    return (slug or "command")[:64].rstrip("-")


def _available_path(directory: Path, slug: str) -> Path:
    candidate = directory / f"{slug}.txt"
    suffix = 2
    while candidate.exists():
        candidate = directory / f"{slug}-{suffix}.txt"
        suffix += 1
    return candidate


def _head(worktree: str) -> str:
    proc = subprocess.run(
        ["git", "-C", worktree, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def run(
    task_id: int,
    command: list[str],
    *,
    monotonic: Callable[[], float] = time.monotonic,
    utc_now: Callable[[], datetime] | None = None,
) -> ReceiptResult:
    """Execute ``command`` in the task worktree, teeing combined output to evidence."""
    if not command:
        raise ReceiptError("pass a command after --")
    conn = init_db()
    task = store.get_task(conn, task_id)
    if task is None:
        raise ReceiptError(f"task {task_id} not found")
    worktree = task["worktree_path"]
    if not worktree or not Path(worktree).is_dir():
        raise ReceiptError(f"task {task_id} has no accessible worktree")
    repo_row = conn.execute("SELECT * FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
    if repo_row is None:
        raise ReceiptError(f"task {task_id} has no registered repository")

    evidence_dir = Path(environment.evidence_path_for(repo_row, worktree) or "")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    output_path = _available_path(evidence_dir, _slug(command))
    values = environment.task_process_env(conn, repo_row, task_id)
    child_env = worker_env(dict(os.environ), task_values=values)

    started = monotonic()
    try:
        proc = subprocess.Popen(
            command,
            cwd=worktree,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise ReceiptError(f"could not run {shlex.join(command)}: {exc}") from exc
    assert proc.stdout is not None
    with output_path.open("w", encoding="utf-8") as output:
        for line in proc.stdout:
            output.write(line)
            output.flush()
            sys.stdout.write(line)
            sys.stdout.flush()
    exit_code = proc.wait()
    elapsed = max(0.0, monotonic() - started)
    stamped = (utc_now or (lambda: datetime.now(UTC)))().astimezone(UTC).isoformat()
    command_text = shlex.join(command)
    head_sha = _head(worktree)
    result_line = (
        f"{stamped} | {command_text} | exit={exit_code} | {elapsed:.3f}s | head={head_sha}"
    )
    ledger = evidence_dir / "receipts.txt"
    with ledger.open("a", encoding="utf-8") as receipts:
        receipts.write(result_line + "\n")
    return ReceiptResult(
        output_path=str(output_path),
        ledger_path=str(ledger),
        exit_code=exit_code,
        elapsed=elapsed,
        head_sha=head_sha,
        result_line=result_line,
    )
