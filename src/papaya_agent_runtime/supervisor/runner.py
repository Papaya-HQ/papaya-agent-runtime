"""Per-task runner guardian.

Owns exactly one worker subprocess: spawns it in its own process group, streams
its normalized event stream to the spool and SQLite, and records a fail-closed
result. It never infers success from a missing process — a worker that exits
without a result event is a failure to be reconciled, not a silent success.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from papaya_agent_runtime import budgets, tool_learning
from papaya_agent_runtime.providers.base import ProviderAdapter, TaskSpec, WorkerResult
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor import autocommit, lifeline
from papaya_agent_runtime.supervisor.spool import EventSpool


def _git(args: list[str], cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


@dataclass
class Finalized:
    """The outcome of the end-of-task auto-commit."""

    head_sha: str | None = None
    committed: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)


def _finalize_worktree(spec: TaskSpec, summary: str) -> Finalized:
    """Ensure completed work is committed; report what was staged and what was not.

    Workers may or may not commit. The manager owns the reviewable commit, so a
    completed task with a dirty worktree is committed here with a generated
    message — but only for the paths that belong on the branch. Evidence and
    receipt directories a worker wrote for the review are held back (see
    ``supervisor.autocommit``); they stay in the worktree and are named in the
    task's event, never silently swept into the commit.
    """
    if not spec.worktree_path:
        return Finalized()
    committed, excluded = autocommit.stage(spec.worktree_path)
    if committed:
        _git(
            [
                "-c",
                "user.name=Papaya Agent Runtime",
                "-c",
                "user.email=manager@ppy.local",
                "commit",
                "-qm",
                f"task {spec.task_id}: {summary or spec.title}",
            ],
            cwd=spec.worktree_path,
        )
    head = _git(["rev-parse", "HEAD"], cwd=spec.worktree_path)
    return Finalized(head_sha=head or None, committed=committed, excluded=excluded)


_TERMINAL_STATUS = {"completed": "worker_done", "blocked": "blocked", "failed": "failed"}


def worker_env(
    base: dict[str, str] | None = None, *, task_values: dict[str, str] | None = None
) -> dict[str, str]:
    """Environment for a worker process: it can call ``ppy``, and it can install things.

    ``ppy`` resolves ``.ppy`` from the cwd unless ``PPY_HOME`` is set, and a worker's cwd
    is its leased worktree — so pin ``PPY_HOME`` to this instance and put the repo's
    ``bin/`` first on PATH. That is what makes ``ppy progress`` work for workers.

    Both directories are created here rather than assumed. Three workers in the
    2026-08-31 window lost roughly ten minutes each before touching the task:
    ``uv``'s default cache was not writable from a Codex sandbox, and writing
    progress needed a sandbox escalation. A cache under ``.ppy`` is inside the
    sandbox's writable root, and it is shared rather than per-task on purpose —
    the cost being paid was re-downloading the same wheels for every dispatch, so
    a cold cache per task would give back nothing.
    """
    from papaya_agent_runtime.manager.launch import repo_root
    from papaya_agent_runtime.paths import ppy_home, uv_cache_dir

    env = dict(base if base is not None else os.environ)
    # The manager's interpreter and database are never authority for a worker.
    # Remove them before applying the task's resolved values so a missing repo
    # setting cannot silently fall back to the manager shell.
    for inherited in ("VIRTUAL_ENV", "DATABASE_URL", "TEST_DATABASE_URL"):
        env.pop(inherited, None)
    home = ppy_home()
    cache = uv_cache_dir()
    for directory in (home, cache):
        with contextlib.suppress(OSError):
            directory.mkdir(parents=True, exist_ok=True)
    env["PPY_HOME"] = str(home)
    env["UV_CACHE_DIR"] = str(cache)
    env.update(task_values or {})
    for cache_key in ("UV_CACHE_DIR", "RUFF_CACHE_DIR", "MYPY_CACHE_DIR"):
        configured = env.get(cache_key)
        if configured:
            with contextlib.suppress(OSError):
                Path(configured).mkdir(parents=True, exist_ok=True)
    bin_dir = os.path.join(repo_root(), "bin")
    env["PATH"] = os.pathsep.join([bin_dir, env.get("PATH", "")])
    return env


class RunnerGuardian:
    def __init__(
        self, adapter: ProviderAdapter, *, on_exit: Callable[[], None] | None = None
    ) -> None:
        self.adapter = adapter
        # Called once the worker process has exited, before its result is recorded:
        # the supervisor hands the execution slot back here, so nothing that reads
        # the recorded terminal status can find the slot still taken.
        self._on_exit = on_exit
        self._proc: subprocess.Popen | None = None
        self.runner_id: str | None = None
        self._interrupt = threading.Event()
        # Set when this runner's session was retired mid-flight (a resume or a
        # steer's interrupt started a newer session for the same task).
        self.superseded = False
        # Set when the supervisor itself is shutting down: the worker was not done,
        # it was stopped, and its session is to be resumed rather than failed.
        self.stopping = False

    def interrupt(self, *, shutdown: bool = False) -> None:
        """Request a cooperative stop of the running worker."""
        if shutdown:
            self.stopping = True
        self._interrupt.set()
        proc = self._proc
        if proc and proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), self.adapter.interrupt_signal())

    def run(
        self,
        spec: TaskSpec,
        *,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> WorkerResult:
        conn = init_db()
        runner_id = uuid.uuid4().hex[:12]
        self.runner_id = runner_id
        store.register_runner(
            conn, runner_id=runner_id, task_id=spec.task_id, provider=self.adapter.name
        )
        spool = EventSpool(spec.run_id, spec.task_id)

        argv = self.adapter.resume(spec) if spec.resume_session_id else self.adapter.start(spec)
        env = worker_env(
            self.adapter.child_env() if hasattr(self.adapter, "child_env") else None,
            task_values=spec.process_env,
        )

        session_started = time.monotonic()
        self._proc = subprocess.Popen(
            argv,
            cwd=spec.worktree_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        # Its own group, so an interrupt reaches the tools it started and never the
        # supervisor; the lifeline takes that group down if the supervisor dies abruptly.
        lifeline.watch_group(self._proc.pid)
        if self._interrupt.is_set():
            # Stopped between being asked to start and starting: honour it now.
            self.interrupt()
        store.update_runner(conn, runner_id, pid=self._proc.pid, status="running")
        store.set_task_status(conn, spec.task_id, "in_progress")

        events = []
        session_seen: str | None = spec.resume_session_id
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            ev = self.adapter.parse_event(raw)
            if ev is None:
                continue
            events.append(ev)
            spool.append(ev.kind, ev.raw)
            store.append_event(
                conn,
                kind=f"worker_{ev.kind}",
                payload=ev.raw,
                run_id=spec.run_id,
                task_id=spec.task_id,
            )
            if ev.session_id:
                session_seen = ev.session_id
                store.upsert_session(
                    conn,
                    task_id=spec.task_id,
                    provider=self.adapter.name,
                    provider_session_id=ev.session_id,
                )
                store.update_runner(conn, runner_id, session_id=ev.session_id)
            denial = self.adapter.live_denial(ev, events)
            if denial is not None:
                # Now, so a steer about it reaches a worker that is still running.
                tool_learning.learn(
                    [denial],
                    task_id=spec.task_id,
                    run_id=spec.run_id,
                    worktree=spec.worktree_path,
                    branch=spec.branch,
                )
            if on_event:
                on_event(ev.kind, ev.raw)

        stderr = self._proc.stderr.read() if self._proc.stderr else ""
        self._proc.wait()
        lifeline.release_group(self._proc.pid)
        exit_code = self._proc.returncode
        session_seconds = time.monotonic() - session_started
        if self._on_exit is not None:
            self._on_exit()

        result = self.adapter.result(events, exit_code)
        from papaya_agent_runtime import deficiencies

        # The turn's own list: a denial already recorded live is not recorded, steered or
        # reported again (`tool_learning.record` deduplicates).
        tool_learning.learn(
            self.adapter.permission_denials(events),
            task_id=spec.task_id,
            run_id=spec.run_id,
            worktree=spec.worktree_path,
            branch=spec.branch,
        )
        if result.usage is not None:
            store.record_usage(
                conn,
                run_id=spec.run_id,
                task_id=spec.task_id,
                provider=result.usage.provider,
                model=result.usage.model or spec.model,
                reasoning=result.usage.reasoning or spec.reasoning,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
            )

        # A runner superseded by a resume (or by a steer's interrupt) is no longer
        # the task's session. Its exit — usually a nonzero code from the interrupt
        # that ended it — must not rewrite the task's status or raise an actionable
        # error, or a live resumed worker reads as `failed` (2026-09-01, codex).
        session_id = result.session_id or session_seen
        if store.is_runner_superseded(conn, runner_id):
            self.superseded = True
            store.update_runner(
                conn,
                runner_id,
                status="superseded",
                exit_code=exit_code if exit_code is not None else -1,
                result_recorded=1,
            )
            store.append_event(
                conn,
                kind="runner_superseded",
                payload={
                    "task_id": spec.task_id,
                    "runner": runner_id,
                    "session_id": session_id,
                    "exit_code": exit_code,
                    "worker_status": result.status,
                    "summary": result.summary,
                },
                run_id=spec.run_id,
                task_id=spec.task_id,
            )
            # Ended by the session that replaced it, not by running its course.
            budgets.observe_task(
                spec.task_id,
                budgets.WORKER_SESSION,
                session_seconds,
                outcome=budgets.KILL,
                conn=conn,
            )
            self._proc = None
            return result

        # A turn ending is not evidence that the gate was finished. Judge it on the
        # evidence *before* the auto-commit below, so the supervisor's own safety
        # commit is never what makes the worker look like it left work unpushed.
        from papaya_agent_runtime import turn_end

        verdict = turn_end.StopVerdict()
        if result.status == "completed":
            verdict = turn_end.why_stopped(conn, spec.task_id)
        elif self.stopping and result.status == "failed":
            # Not a failure: the supervisor stopped it. Recorded the way a turn that
            # ended short is, with its session, so the rounds resume it from there.
            task = store.get_task(conn, spec.task_id)
            verdict = turn_end.StopVerdict(
                stopped=True,
                reasons=["the supervisor shut down while this session was running"],
                expected_phase=(task["ends_at"] if task is not None else None) or "done",
            )

        # For a completed task, guarantee a reviewable commit and record head.
        finalized = None
        if result.status == "completed" and not result.head_sha:
            finalized = _finalize_worktree(spec, result.summary)
            result.head_sha = finalized.head_sha
            # Say out loud what the auto-commit staged and what it held back, so a
            # missing file is a recorded decision rather than a surprise at review.
            store.append_event(
                conn,
                kind="autocommit",
                payload={
                    "task_id": spec.task_id,
                    "head_sha": finalized.head_sha,
                    "committed": finalized.committed,
                    "excluded": finalized.excluded,
                    "summary": (
                        f"auto-committed {len(finalized.committed)} path(s); "
                        f"left {len(finalized.excluded)} out of the commit"
                        + (f": {', '.join(finalized.excluded)}" if finalized.excluded else "")
                    ),
                },
                run_id=spec.run_id,
                task_id=spec.task_id,
            )

        # A worker that filed its done note and left commits behind is missing one
        # thing only its own lease branch can be given, so the harness pushes it —
        # after the auto-commit, so the safety commit goes up with the rest — and
        # judges the turn again on what is true afterwards.
        # ONE push decision for this ending, after the auto-commit so the safety
        # commit goes up with the rest, and before anything reviews this head. Where
        # the repository gates pushes the worker never pushed at all — its rules told
        # it not to — and the runtime pushes only with its own gate green at this
        # exact SHA (issue #83). Anywhere else this is the old rescue, unchanged.
        # Never two pushes, and never a push the gate has not seen.
        verdict, _pushed = turn_end.deliver_after_turn(conn, spec.task_id, verdict)

        task_status = _TERMINAL_STATUS.get(result.status, "failed")
        if verdict.stopped:
            task_status = turn_end.WORKER_STOPPED
            if verdict.background_command:
                deficiencies.record_gate_past_tool_cap(
                    spec.task_id, spec.run_id, verdict.background_command
                )

        # Emit an actionable event for the manager/human loop. Every terminal event
        # carries the session it came from so a late arrival can be attributed.
        if verdict.stopped:
            kind = turn_end.WORKER_STOPPED
            payload = {
                "task_id": spec.task_id,
                "head_sha": result.head_sha,
                "session_id": session_id,
                "summary": verdict.summary,
                "reasons": verdict.reasons,
                "unpushed": verdict.unpushed,
                "phase": verdict.phase,
                "background_command": verdict.background_command,
                "expected_phase": verdict.expected_phase,
                "resume_message": verdict.resume_message(),
            }
        elif result.status == "completed":
            kind = "worker_done"
            payload = {
                "task_id": spec.task_id,
                "head_sha": result.head_sha,
                "summary": result.summary,
                "session_id": session_id,
                "excluded_from_commit": finalized.excluded if finalized else [],
            }
        elif result.status == "blocked":
            kind = "question"
            payload = {
                "task_id": spec.task_id,
                "question": result.question,
                "session_id": session_id,
            }
        else:
            kind = "error"
            payload = {
                "task_id": spec.task_id,
                "summary": result.summary,
                "exit_code": exit_code,
                "session_id": session_id,
                "stderr": stderr[-2000:],
            }

        store.record_turn_result(
            conn,
            run_id=spec.run_id,
            task_id=spec.task_id,
            runner_id=runner_id,
            task_status=task_status,
            kind=kind,
            payload=payload,
            exit_code=exit_code if exit_code is not None else -1,
        )
        if self.stopping and verdict.stopped:
            outcome = budgets.KILL
        elif verdict.stopped:
            outcome = budgets.STALL
        elif exit_code is not None and exit_code < 0:
            outcome = budgets.KILL
        else:
            outcome = result.status
        budgets.observe_task(
            spec.task_id, budgets.WORKER_SESSION, session_seconds, outcome=outcome, conn=conn
        )
        self._proc = None
        return result
