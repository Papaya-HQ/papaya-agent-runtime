"""Supervisor business logic (transport-independent).

Owns dispatch, status, actionable-wait, and reconcile. The socket server is a
thin transport around this class; tests drive it directly. Runners execute in
background threads; SQLite is the durable coordination point so state survives a
supervisor restart.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from papaya_agent_runtime import decisions, lifecycle
from papaya_agent_runtime.config import ConfigError, load_config
from papaya_agent_runtime.providers.base import TaskSpec
from papaya_agent_runtime.providers.claude import ClaudeAdapter, effective_allowed_tools
from papaya_agent_runtime.providers.codex import CodexAdapter
from papaya_agent_runtime.providers.fake import FakeProvider
from papaya_agent_runtime.router import CeilingError, WorkerProfile, choose_worker, enforce_ceiling
from papaya_agent_runtime.schemas import SchemaError, validate_task
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.runner import RunnerGuardian
from papaya_agent_runtime.worktree import Lease, LeaseError, LeaseManager


def _advisory_failed(conn, kind: str, exc: Exception) -> None:
    """Record that an advisory could not be computed, instead of hiding it.

    A dispatch advisory is a suggestion, so it must never fail a dispatch — but a
    silent `except` also means a crash inside one looks exactly like "nothing to
    report". That is how a migration matcher that raised on every call shipped
    unnoticed: every advisory returned None and every dispatch looked clean. The
    catch stays; the silence does not.
    """
    with contextlib.suppress(Exception):
        store.append_event(
            conn,
            kind="advisory_failed",
            payload={
                "advisory": kind,
                "error": f"{type(exc).__name__}: {exc}",
                "summary": (
                    f"the {kind} advisory could not be computed for this dispatch "
                    f"({type(exc).__name__}: {exc}). The dispatch went ahead — an advisory "
                    "is a suggestion — but nothing was checked, so treat a clean dispatch "
                    "as unknown rather than clear until this is fixed."
                ),
            },
        )


class SupervisorError(Exception):
    pass


@dataclass
class _Execution:
    """One admitted worker execution, from admission to the provider process's exit.

    The token is the whole identity. A reservation used to be keyed by the
    calling thread and a bound slot by the task (issue #70): two resumes of one
    stopped task could both reserve before either bound, the second binding
    overwrote the first, and a finalizer for an old session released whatever
    slot the task held *now*. Every path that gives a slot back names the
    execution it is giving back, so an old finalizer can never release a newer
    execution's slot, and an execution knows its task from admission — before
    any branch sync, status write, or runner creation — so a duplicate resume
    is refused with no side effects at all.
    """

    token: str
    task_id: int | None
    #: Set once the execution has a runner thread; until then it is pending.
    bound: bool = False
    #: Which capacity admitted it: :data:`LANE_TICKET` (`worker.max_concurrent`) or
    #: :data:`LANE_RECONCILE` (`worker.reconcile_slots`, pull request fixes only).
    lane: str = "ticket"


#: A ticket's work: its first dispatch and every run before it is delivered.
LANE_TICKET = "ticket"
#: Fixing a delivered pull request. Never a first dispatch; never a ticket slot.
LANE_RECONCILE = "reconcile"


def _adapter_for(provider: str):
    if provider == "fake":
        return FakeProvider()
    if provider == "claude":
        return ClaudeAdapter()
    if provider == "codex":
        return CodexAdapter()
    raise SupervisorError(f"unknown provider {provider!r}")


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _record_runner_crash(conn, runner_id: str | None, spec: TaskSpec, exc: Exception) -> None:
    """Atomically expose a crashed runner, without overwriting a newer session.

    ``result_recorded`` is the reconciliation boundary: once true, a dead runner
    is no longer eligible for recovery. Keep it in the same transaction as the
    task failure and actionable error so a failed write leaves the runner visibly
    unreconciled rather than silently stranding the task in progress.
    """
    with contextlib.suppress(Exception):
        conn.rollback()
    try:
        conn.execute("BEGIN IMMEDIATE")
        runner_row = store.get_runner(conn, runner_id) if runner_id is not None else None
        current_session = runner_row is None or not runner_row["superseded_at"]
        if current_session:
            newer_live = conn.execute(
                """
                SELECT 1 FROM runners
                WHERE task_id = ? AND id IS NOT ?
                  AND status IN ('starting', 'running')
                  AND superseded_at IS NULL
                LIMIT 1
                """,
                (spec.task_id, runner_id),
            ).fetchone()
            current_session = newer_live is None

        if current_session:
            now = datetime.now(UTC).isoformat()
            seq = store.next_seq(conn, spec.run_id)
            payload = json.dumps(
                {
                    "task_id": spec.task_id,
                    "runner": runner_id,
                    "summary": f"runner crashed: {exc!r}",
                }
            )
            conn.execute(
                """
                INSERT INTO events (run_id, task_id, seq, kind, payload, created_at)
                VALUES (?, ?, ?, 'error', ?, ?)
                """,
                (spec.run_id, spec.task_id, seq, payload, now),
            )
            conn.execute(
                "UPDATE tasks SET status = 'failed', updated_at = ? WHERE id = ?",
                (now, spec.task_id),
            )

        if runner_row is not None and runner_row["status"] in ("starting", "running"):
            conn.execute(
                """
                UPDATE runners
                SET status = 'failed', exit_code = -1, result_recorded = 1
                WHERE id = ?
                """,
                (runner_id,),
            )
        conn.commit()
    except Exception:
        with contextlib.suppress(Exception):
            conn.rollback()
        raise


def _start_from_branch(
    worktree_path: str,
    branch: str,
    remote: str = "origin",
    local_source: str | None = None,
) -> str:
    """Move a fresh worktree onto ``branch`` and return the resulting HEAD.

    The branch is looked for in three places, in order: on ``remote`` (the repo's
    forge — for a repo registered from a local path, ``origin`` is that local clone
    and the branch a stack is built on lives on the forge instead); as a local ref
    of the base clone, which every lease worktree shares; and in ``local_source``,
    the lease worktree of the task being stacked on, fetched as a peer.

    The last two are what let a layer start from a parent that has not pushed
    yet. On 2026-09-05 (issue #57) ``--stack-on`` was refused two minutes after
    the parent was dispatched, because only the forge was ever asked — while the
    parent's lease branch sat in the base clone the whole time. Which tip the
    child starts from is recorded as its ``base_sha`` either way; the push comes
    later, with the parent's own delivery.
    """
    import subprocess
    from pathlib import Path

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", worktree_path, *args], capture_output=True, text=True, check=False
        )

    fetched = git("fetch", "--quiet", remote, branch)
    if fetched.returncode == 0:
        target, source = "FETCH_HEAD", f"{remote}/{branch}"
    elif git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
        target, source = f"refs/heads/{branch}", f"the local branch {branch}"
    elif (
        local_source
        and Path(local_source).is_dir()
        and (git("fetch", "--quiet", local_source, branch).returncode == 0)
    ):
        target, source = "FETCH_HEAD", f"{branch} in the lease worktree {local_source}"
    else:
        raise SupervisorError(
            f"cannot start from {branch!r}: git fetch {remote} {branch} failed: "
            f"{fetched.stderr.strip()[:200]}; and no local branch {branch!r} exists in the "
            "base clone either"
        )
    reset = git("reset", "--hard", "--quiet", target)
    if reset.returncode != 0:
        raise SupervisorError(
            f"cannot start from {branch!r}: git reset --hard onto {source} failed: "
            f"{reset.stderr.strip()[:200]}"
        )
    head = git("rev-parse", "HEAD")
    return head.stdout.strip()


def _lease_worktree_for_branch(conn, branch: str) -> str | None:
    """The lease worktree of the task whose branch this is, if any is recorded."""
    row = conn.execute(
        "SELECT worktree_path FROM tasks WHERE branch = ? ORDER BY id DESC LIMIT 1", (branch,)
    ).fetchone()
    return row["worktree_path"] if row is not None and row["worktree_path"] else None


class Supervisor:
    def __init__(self, *, session_resumable=None) -> None:
        #: Whether a delivered task's provider session can be picked up again to fix
        #: its pull request: ``(task row, session id) -> bool``. When it cannot, the
        #: lane runs a fresh reconciler session on the same task instead.
        self._session_resumable = session_resumable or (lambda _task, session: bool(session))
        self._lock = threading.Lock()
        self._threads: dict[int, threading.Thread] = {}
        self._runners: dict[int, RunnerGuardian] = {}
        self._leases: dict[int, object] = {}
        #: Every admitted execution, pending or bound, by token.
        self._executions: dict[str, _Execution] = {}
        # Event identities currently crossing the task-row creation boundary.
        # The durable copy lives in task_env; this closes the same-supervisor
        # race before that row exists.
        self._papaya_event_claims: set[str] = set()
        # Question fingerprints already auto-answered per task; a fingerprint that
        # reappears after its stored answer is escalated instead of looping.
        self._auto_answered: dict[int, set[str]] = {}
        # Every thread this supervisor started, so ``close`` can wait for all of
        # them — ``_threads`` keeps only the latest per task.
        self._started: list[threading.Thread] = []
        self._closed = False

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #
    def dispatch_task(
        self,
        *,
        repo: str,
        title: str,
        instructions: str = "",
        provider: str | None = None,
        run_id: int | None = None,
        model: str | None = None,
        reasoning: str | None = None,
        base: str | None = None,
        stack_on: int | None = None,
        ends_at: str = "done",
        papaya_event_key: str | None = None,
        papaya_event_metadata: str | None = None,
    ) -> dict:
        if not papaya_event_key:
            return self._dispatch_task_once(
                repo=repo,
                title=title,
                instructions=instructions,
                provider=provider,
                run_id=run_id,
                model=model,
                reasoning=reasoning,
                base=base,
                stack_on=stack_on,
                ends_at=ends_at,
            )

        from papaya_agent_runtime import papaya_events

        with self._lock:
            existing = papaya_events.find_existing_task(init_db(), papaya_event_key)
            if existing is not None:
                return {
                    "run_id": existing["run_id"],
                    "task_id": existing["id"],
                    "lease_id": existing["lease_id"],
                    "branch": existing["branch"],
                    "worktree_path": existing["worktree_path"],
                    "ends_at": existing["ends_at"],
                    "deduplicated": True,
                }
            if papaya_event_key in self._papaya_event_claims:
                raise SupervisorError(
                    "this Papaya event is already being dispatched; retry in a moment"
                )
            self._papaya_event_claims.add(papaya_event_key)

        try:
            return self._dispatch_task_once(
                repo=repo,
                title=title,
                instructions=instructions,
                provider=provider,
                run_id=run_id,
                model=model,
                reasoning=reasoning,
                base=base,
                stack_on=stack_on,
                ends_at=ends_at,
                papaya_event_key=papaya_event_key,
                papaya_event_metadata=papaya_event_metadata,
            )
        finally:
            with self._lock:
                self._papaya_event_claims.discard(papaya_event_key)

    def _dispatch_task_once(
        self,
        *,
        repo: str,
        title: str,
        instructions: str = "",
        provider: str | None = None,
        run_id: int | None = None,
        model: str | None = None,
        reasoning: str | None = None,
        base: str | None = None,
        stack_on: int | None = None,
        ends_at: str = "done",
        papaya_event_key: str | None = None,
        papaya_event_metadata: str | None = None,
    ) -> dict:
        # A caller that names no provider gets the configured worker provider. It
        # used to get `fake`, which on 2026-09-04 sent a real task to a stub worker
        # that pushed a branch to a live GitHub remote (issue #49). Imported here,
        # not at module scope, so the hermetic suite can substitute its own default.
        from papaya_agent_runtime.config import default_worker_provider

        provider = provider or default_worker_provider()
        conn = init_db()
        repo_row = store.get_repo(conn, repo)
        if repo_row is None:
            raise SupervisorError(f"repo {repo!r} is not registered (ppy repo add)")
        from papaya_agent_runtime import environment

        try:
            ends_at = environment.validate_ends_at(ends_at)
        except environment.RepoEnvironmentError as exc:
            raise SupervisorError(str(exc)) from exc

        # A stack is a chain of tasks. Naming the task this one builds on derives
        # the starting branch from its lease, so the manager never has to look up
        # (or misremember) a branch name.
        if stack_on is not None:
            parent = store.get_task(conn, stack_on)
            if parent is None:
                raise SupervisorError(f"cannot stack on task {stack_on}: no such task")
            if not parent["branch"]:
                raise SupervisorError(
                    f"cannot stack on task {stack_on}: it has no branch yet "
                    "(it never got a worktree, so there is nothing to build on)"
                )
            if base and base != parent["branch"]:
                raise SupervisorError(
                    f"--stack-on {stack_on} means starting from {parent['branch']!r}, but "
                    f"--base says {base!r}; pass one or the other"
                )
            base = parent["branch"]

        # Whether another task in this repository is already adding a database
        # migration. Read before this task's row exists, so nothing has to filter
        # it back out, and never allowed to fail a dispatch: it is a suggestion.
        migration_advisory = None
        try:
            from papaya_agent_runtime import migrations as _migrations

            migration_advisory = _migrations.dispatch_advisory(conn, repo_row, stack_on=stack_on)
        except Exception as exc:  # noqa: BLE001 - an advisory never costs the dispatch
            migration_advisory = None
            _advisory_failed(conn, "migration", exc)

        # Whether another task in flight here already touches the files this brief
        # names. Four parallel tasks on one module cost three hand-resolved
        # conflicts on 2026-09-06 (issue #61); the sibling is named and --stack-on
        # suggested. Also a suggestion, never a refusal.
        overlap_advisory = None
        touched: list[str] = []
        try:
            from papaya_agent_runtime import overlap as _overlap

            touched = _overlap.touched_paths(instructions, repo_row["local_path"])
            overlap_advisory = _overlap.dispatch_advisory(
                conn, repo_row, touched, stack_on=stack_on
            )
        except Exception as exc:  # noqa: BLE001 - an advisory never costs the dispatch
            overlap_advisory = None
            _advisory_failed(conn, "overlap", exc)

        # Validate the task packet contract before doing any work.
        packet = {
            "title": title,
            "repo": repo,
            "instructions": instructions,
            "provider": provider,
            "model": model,
            "reasoning": reasoning,
            "run_id": run_id,
        }
        try:
            validate_task(packet)
        except SchemaError as exc:
            raise SupervisorError(f"invalid task packet: {exc}") from exc

        # Enforce the hard worker ceiling for real providers (runtime, not prompt).
        if provider in ("claude", "codex"):
            model, reasoning = self._resolve_and_enforce(provider, model, reasoning)
        if provider == "claude":
            # A Claude worker with no allowed tools has no shell: it cannot run the
            # suite, commit, or even report progress. That used to happen silently
            # whenever the supervisor was started without PPY_CLAUDE_ALLOWED_TOOLS.
            allowed, source = effective_allowed_tools()
            if not allowed:
                raise SupervisorError(
                    f"claude worker would have no tools at all ({source} is empty) — it could "
                    "not run a command, commit, or report progress. Restore the profile with "
                    "`ppy config claude --reset`, or add a pattern with "
                    "`ppy config claude --allow 'Bash(git:*)'`."
                )

        execution = self._admit(conn, limit=self._max_concurrent(provider), task_id=None)
        try:
            return self._launch_dispatched(
                conn,
                execution,
                repo_row=repo_row,
                repo=repo,
                title=title,
                instructions=instructions,
                provider=provider,
                run_id=run_id,
                model=model,
                reasoning=reasoning,
                base=base,
                stack_on=stack_on,
                touched=touched,
                migration_advisory=migration_advisory,
                overlap_advisory=overlap_advisory,
                ends_at=ends_at,
                papaya_event_key=papaya_event_key,
                papaya_event_metadata=papaya_event_metadata,
            )
        except BaseException:
            # Whatever failed — the lease, the branch, provisioning, the adapter's
            # argv, the thread — this execution is over and its slot goes back.
            self._release(execution)
            raise

    def _launch_dispatched(
        self,
        conn,
        execution: _Execution,
        *,
        repo_row,
        repo: str,
        title: str,
        instructions: str,
        provider: str,
        run_id: int | None,
        model: str | None,
        reasoning: str | None,
        base: str | None,
        stack_on: int | None,
        touched: list[str],
        migration_advisory: str | None,
        overlap_advisory: str | None,
        ends_at: str,
        papaya_event_key: str | None,
        papaya_event_metadata: str | None,
    ) -> dict:
        if run_id is None:
            run_id = store.create_run(conn, title)
            store.set_run_status(conn, run_id, "running")

        task_id = store.add_task(
            conn,
            run_id=run_id,
            title=title,
            repo_id=repo_row["id"],
            provider=provider,
            model=model,
            reasoning=reasoning,
            ends_at=ends_at,
        )
        if papaya_event_key:
            from papaya_agent_runtime.papaya_events import (
                PAPAYA_EVENT_KEY,
                PAPAYA_EVENT_METADATA,
            )

            store.set_task_env(
                conn,
                task_id,
                PAPAYA_EVENT_KEY,
                papaya_event_key,
                source="papaya_event",
            )
            if papaya_event_metadata:
                store.set_task_env(
                    conn,
                    task_id,
                    PAPAYA_EVENT_METADATA,
                    papaya_event_metadata,
                    source="papaya_event",
                )

        # The task row exists first because the lease's branch name carries the
        # task id. If no worktree can be leased, the row must not linger as a
        # `requested` task with no runner: mark it failed with the reason, and
        # surface that reason to the caller instead of a bare "dispatch failed".
        lease_mgr = LeaseManager()
        try:
            lease = lease_mgr.acquire(
                repo_path=repo_row["local_path"],
                repo_id=repo_row["id"],
                task_id=task_id,
            )
        except LeaseError as exc:
            store.set_task_status(conn, task_id, "failed")
            store.append_event(
                conn,
                kind="error",
                payload={"task_id": task_id, "summary": f"no worktree lease: {exc}"},
                run_id=run_id,
                task_id=task_id,
            )
            raise SupervisorError(f"task {task_id}: no worktree lease: {exc}") from exc
        base_sha = lease.base_sha
        if base:
            # Stacked work starts from the exact tip of the branch it builds on, not
            # from the repo's base. A worker cannot be redirected after it starts,
            # and a wrong starting commit cost a dispatch on 2026-08-30.
            try:
                from papaya_agent_runtime import repos as _repos

                base_sha = _start_from_branch(
                    lease.worktree_path,
                    base,
                    _repos.upstream_remote(repo_row),
                    local_source=_lease_worktree_for_branch(conn, base),
                )
            except SupervisorError as exc:
                store.set_task_status(conn, task_id, "failed")
                store.append_event(
                    conn,
                    kind="error",
                    payload={"task_id": task_id, "summary": str(exc)},
                    run_id=run_id,
                    task_id=task_id,
                )
                with self._lock:
                    self._leases[task_id] = (lease, repo_row["local_path"])
                with contextlib.suppress(Exception):
                    self.release_task_lease(task_id, remove_branch=True)
                raise
        store.update_task_fields(
            conn,
            task_id,
            base_sha=base_sha,
            branch=lease.branch,
            ends_at=ends_at,
            worktree_path=lease.worktree_path,
            lease_id=lease.id,
            stacked_on=base,
            stacked_on_task=stack_on,
        )
        # What this brief says it touches, kept for the next dispatch to compare
        # against before this worker has written a line.
        with contextlib.suppress(Exception):
            from papaya_agent_runtime import overlap as _overlap

            _overlap.record_touches(conn, task_id, touched)
        store.append_event(
            conn,
            kind="dispatched",
            payload={
                "task_id": task_id,
                "title": title,
                "provider": provider,
                "lease": lease.id,
                "stacked_on": base,
                "stacked_on_task": stack_on,
            },
            run_id=run_id,
            task_id=task_id,
        )
        # A local task has no work item for its brief's criteria, status or comment.
        from papaya_agent_runtime import standalone

        standalone.skip_if_local(conn, task_id, standalone.DISPATCHED)

        # Give the worktree whatever head start this repo has configured — a reused
        # virtualenv from the base clone, a provision command — before the worker
        # starts. Opt-in per repo; a repo with nothing configured does nothing here,
        # and a hook that fails costs the head start, never the dispatch.
        from papaya_agent_runtime.worktree.provision import provision_worktree

        provision_worktree(
            repo_row,
            lease.worktree_path,
            task_id=task_id,
            run_id=run_id,
            conn=conn,
        )

        # Point the worker at this repo's shared memory (learnings + progress log) so
        # it uses and updates it — which is also how the manager follows progress
        # without interrupting. Seeding is idempotent; files already exist from add.
        from papaya_agent_runtime import memory

        memory.seed_repo_memory(repo)

        # The facts about this repository's environment that cost workers cycles
        # when briefs carried them by hand (issue #60): the evidence directory
        # inside the worktree, the local gate versus CI's full suite, a private
        # database stack assigned to this task, and the push-hook policy. Settled
        # here, recorded on the task, and rendered into the worker's prompt.
        from papaya_agent_runtime import environment

        prepared = environment.prepare(
            conn,
            repo_row,
            task_id=task_id,
            worktree=lease.worktree_path,
            branch=lease.branch,
            ends_at=ends_at,
        )
        if prepared.compose_project:
            store.append_event(
                conn,
                kind="environment_assigned",
                payload={
                    "task_id": task_id,
                    "compose_project": prepared.compose_project,
                    "db_port": prepared.db_port,
                    "evidence_path": prepared.evidence_path,
                    "summary": (
                        f"task {task_id} owns compose project {prepared.compose_project}"
                        + (f" on port {prepared.db_port}" if prepared.db_port else "")
                        + "; the block in its brief carries the override recipe"
                    ),
                },
                run_id=run_id,
                task_id=task_id,
            )

        spec = TaskSpec(
            task_id=task_id,
            title=title,
            instructions=instructions,
            worktree_path=lease.worktree_path,
            base_sha=base_sha or "",
            provider=provider,
            model=model,
            reasoning=reasoning,
            run_id=run_id,
            branch=lease.branch,
            memory_preamble=memory.worker_context(repo, task_id=task_id, ends_at=ends_at),
            environment=prepared.block,
            process_env=prepared.process_env or {},
            denied_tools=environment.denied_tools(repo_row),
        )
        adapter = _adapter_for(provider)
        runner = RunnerGuardian(adapter, on_exit=lambda: self._release(execution))

        with self._lock:
            self._runners[task_id] = runner
            self._leases[task_id] = (lease, repo_row["local_path"])

        thread = threading.Thread(
            target=self._run_task, args=(runner, spec), kwargs={"execution": execution}, daemon=True
        )
        with self._lock:
            self._threads[task_id] = thread
        self._bind(execution, task_id)
        self._start(thread)

        return {
            "run_id": run_id,
            "task_id": task_id,
            "lease_id": lease.id,
            "branch": lease.branch,
            "worktree_path": lease.worktree_path,
            "migration_advisory": migration_advisory,
            "compose_project": prepared.compose_project,
            "db_port": prepared.db_port,
            "evidence_path": prepared.evidence_path,
            "overlap_advisory": overlap_advisory,
            "ends_at": ends_at,
        }

    def _resolve_and_enforce(
        self, provider: str, model: str | None, reasoning: str | None
    ) -> tuple[str, str]:
        """Resolve worker profile and enforce the ceiling (runtime, not prompt).

        Omitted choices use the configured worker defaults. Every real execution
        therefore reaches the provider with a concrete, persisted profile rather
        than inheriting an account or CLI default.
        """
        try:
            config = load_config()
        except ConfigError as exc:
            raise SupervisorError(f"run `ppy setup` first: {exc}") from exc

        default = choose_worker(config)
        model = model or default.model
        reasoning = reasoning or default.reasoning

        try:
            enforce_ceiling(config, WorkerProfile(provider, model, reasoning))
        except CeilingError as exc:
            raise SupervisorError(f"worker ceiling: {exc}") from exc
        return model, reasoning

    def _max_concurrent(self, provider: str) -> int:
        """Read the configured active-execution bound.

        Hermetic fake-provider tests historically require no config; they receive
        the schema default. Real providers remain fail-closed in profile resolution.
        """
        try:
            return load_config().worker.max_concurrent
        except ConfigError as exc:
            from papaya_agent_runtime.paths import config_path

            if provider == "fake" and not config_path().exists():
                from papaya_agent_runtime.config import WorkerCeiling

                return WorkerCeiling().max_concurrent
            raise SupervisorError(f"run `ppy setup` first: {exc}") from exc

    def _reconcile_slots(self) -> int:
        """The reconcile lane's size, `worker.reconcile_slots`; the default without config."""
        from papaya_agent_runtime.config import WorkerCeiling

        try:
            return load_config().worker.reconcile_slots
        except ConfigError:
            return WorkerCeiling().reconcile_slots

    def _admit(
        self, conn, *, limit: int, task_id: int | None, lane: str = LANE_TICKET
    ) -> _Execution:
        """Atomically admit one execution, or refuse with nothing changed.

        Normal operation uses one manager-owned :class:`Supervisor` per
        ``PPY_HOME`` (enforced by the server's owner lock). The short mutex here
        protects admission inside that process; this is not a distributed
        scheduler. Persisted starting/running rows from an earlier supervisor
        are counted conservatively until reconciliation marks them otherwise.
        A resume names its task at admission, so a second resume of the same
        task — pending or live — is refused here, before any side effect.

        The two lanes are counted apart. ``limit`` bounds ``lane``: a ticket
        execution counts ticket executions and every persisted row this process
        does not own; a reconcile execution counts only reconcile executions, so a
        pull request fix is admitted with every ticket slot busy, and a ticket is
        never refused because the lane is.
        """
        with self._lock:
            if self._closed:
                raise SupervisorError("the supervisor is closing; it admits no new executions")
            live = store.live_runners(conn)
            if task_id is not None:
                if any(e.task_id == task_id for e in self._executions.values()):
                    raise SupervisorError(
                        f"task {task_id} already has a pending or live execution; "
                        "refusing a duplicate resume"
                    )
                if any(row["task_id"] == task_id for row in live):
                    raise SupervisorError(
                        f"task {task_id} already has a live execution; refusing a duplicate resume"
                    )

            # A bound execution and its runner row describe the same process, so
            # count that pair once. Extra live rows for the same task remain
            # counted: they are still consuming provider capacity.
            bound_tasks = [e.task_id for e in self._executions.values() if e.bound]
            persisted = len(live)
            for active_task in bound_tasks:
                if any(row["task_id"] == active_task for row in live):
                    persisted -= 1
            if lane == LANE_RECONCILE:
                active = sum(1 for e in self._executions.values() if e.lane == LANE_RECONCILE)
                if active >= limit:
                    raise SupervisorError(
                        f"the reconcile lane is full ({active}/{limit} fixing a pull request); "
                        "retry after that fix ends"
                    )
            else:
                ticket = sum(1 for e in self._executions.values() if e.lane != LANE_RECONCILE)
                active = ticket + persisted
                if active >= limit:
                    raise SupervisorError(
                        f"worker capacity is full ({active}/{limit} active); "
                        "retry after a worker exits"
                    )
            execution = _Execution(token=uuid.uuid4().hex[:12], task_id=task_id, lane=lane)
            self._executions[execution.token] = execution
            return execution

    def _bind(self, execution: _Execution, task_id: int) -> None:
        """Attach the runner thread's identity: from here the slot is this task's process."""
        with self._lock:
            if self._executions.get(execution.token) is not execution:
                raise SupervisorError("execution slot was released before worker launch")
            execution.task_id = task_id
            execution.bound = True

    def _release(self, execution: _Execution | None) -> None:
        """Give this execution's slot back. Idempotent; never touches another execution."""
        if execution is None:
            return
        with self._lock:
            if self._executions.get(execution.token) is execution:
                del self._executions[execution.token]

    def _active_executions(self) -> list[dict]:
        with self._lock:
            return [
                {"token": e.token, "task_id": e.task_id, "bound": e.bound}
                for e in self._executions.values()
            ]

    def resume_task(
        self,
        task_id: int,
        message: str | None = None,
        *,
        continuation: dict | None = None,
        ends_at: str | None = None,
        by: str | None = None,
    ) -> dict:
        """Resume a task's provider session (blocked-worker resume / checkpoint steer).

        ``continuation`` names what this resume delivers — the queued steer
        events, or the stored answer — and is written into the ``resumed`` event
        before the runner thread starts. That event is the durable proof of
        consumption (issue #71): a steer or answer counts as delivered when, and
        only when, a launch carrying it was recorded. ``by`` says who asked
        (`store.BY_PERSON` or `store.BY_MANAGER`), and is written there too.
        """
        if by:
            continuation = {**(continuation or {}), "by": by}
        conn = init_db()
        task = store.get_task(conn, task_id)
        if task is None:
            raise SupervisorError(f"task {task_id} not found")
        from papaya_agent_runtime import environment

        resolved_ends_at = ends_at or (
            task["ends_at"] if "ends_at" in task.keys() else "done"  # noqa: SIM118
        )
        try:
            resolved_ends_at = environment.validate_ends_at(resolved_ends_at)
        except environment.RepoEnvironmentError as exc:
            raise SupervisorError(str(exc)) from exc
        from papaya_agent_runtime.config import default_worker_provider

        provider = task["provider"] or default_worker_provider()
        model, reasoning = task["model"], task["reasoning"]
        if provider in ("claude", "codex"):
            model, reasoning = self._resolve_and_enforce(provider, model, reasoning)
        # Admission names the task: a second resume of this task is refused right
        # here, before the worktree is rebuilt or synced, before any status write.
        # A task that was ever delivered only runs again to fix its pull request, and
        # that is the reconcile lane's work, never a ticket slot's.
        from papaya_agent_runtime import reconcile

        if reconcile.is_reconciliation(conn, task_id):
            execution = self._admit(
                conn, limit=self._reconcile_slots(), task_id=task_id, lane=LANE_RECONCILE
            )
        else:
            execution = self._admit(conn, limit=self._max_concurrent(provider), task_id=task_id)
        try:
            return self._launch_resumed(
                conn,
                execution,
                task,
                provider=provider,
                model=model,
                reasoning=reasoning,
                message=message,
                continuation=continuation,
                ends_at=resolved_ends_at,
            )
        except BaseException:
            self._release(execution)
            raise

    def _launch_resumed(
        self,
        conn,
        execution: _Execution,
        task,
        *,
        provider: str,
        model: str | None,
        reasoning: str | None,
        message: str | None,
        continuation: dict | None = None,
        ends_at: str = "done",
    ) -> dict:
        task_id = int(task["id"])
        sess = conn.execute(
            "SELECT * FROM sessions WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        session_id = sess["provider_session_id"] if sess else None
        # A task whose turn ended mid-gate is resumed with what was cut short, not
        # with silence: a bare resume used to hand the worker no reason at all, and
        # a worker that does not know what it left unfinished repeats the ending.
        if message is None:
            from papaya_agent_runtime import turn_end

            recorded = turn_end.latest_stop_verdict(conn, task_id)
            if recorded is not None and task["status"] == turn_end.WORKER_STOPPED:
                message = recorded.resume_message()
        # The worktree may be gone: a failed worker's pristine lease is handed
        # back, and a prune can take a slot. The task is still resumable, so it
        # gets its checkout back before anything tries to run in it (issue #58).
        rebuilt = self._ensure_worktree(conn, task)
        if rebuilt is not None:
            task = store.get_task(conn, task_id)
        lifecycle.require_live_lease(conn, task_id, error_type=SupervisorError)
        # A cascade may have rewritten this task's branch while it was stopped.
        # Resuming a worker into a worktree that is behind its own remote ends in
        # a force-push over the cascade, so the worktree is moved onto the remote
        # first — or the resume is refused when both sides hold commits.
        from papaya_agent_runtime import stacks

        try:
            stacks.sync_worktree_with_remote(task_id)
        except stacks.StackError as exc:
            raise SupervisorError(str(exc)) from exc
        # Any runner still attached to the old session is retired here: it is no
        # longer the task's worker, and its (usually nonzero) exit must not land on
        # the task after this resume. Then say the truth about the task *now* —
        # it is being worked again, not `failed` from the session we just replaced.
        superseded = store.supersede_runners(conn, task_id, reason="resume")
        store.set_task_status(conn, task_id, "in_progress")
        store.update_task_fields(conn, task_id, model=model, reasoning=reasoning, ends_at=ends_at)
        task = store.get_task(conn, task_id)

        # A continuation replaces the worker's instructions for the turn. It
        # carries the brief's Goals, Intent, In scope and Out of scope with it, so
        # a steer or answer never silently erases a boundary (issue #77).
        from papaya_agent_runtime import environment

        finish = environment.finish_instruction(task_id, ends_at)
        terminal_message = (
            f"{message}\n\nTerminal instruction: {finish}"
            if message
            else f"Terminal instruction: {finish}"
        )
        packet, scope_preserved = _self_contained(conn, task, terminal_message)
        repo_row = conn.execute("SELECT * FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
        process_env = (
            environment.task_process_env(conn, repo_row, task_id) if repo_row is not None else {}
        )
        # A pull request fix resumes the session that delivered it, which holds the
        # diff, the brief and the review. When that session cannot be resumed, or its
        # worktree had to be rebuilt, a fresh reconciler session starts on the same
        # task — same branch, same worktree — from a brief scoped to the pull request.
        reconciler = execution.lane == LANE_RECONCILE and (
            rebuilt is not None or not self._session_resumable(task, session_id)
        )
        if reconciler:
            from papaya_agent_runtime import reconcile

            brief = reconcile.reconciler_brief(conn, task, message)
            prepared = (
                environment.prepare(
                    conn,
                    repo_row,
                    task_id=task_id,
                    worktree=task["worktree_path"] or "",
                    branch=task["branch"],
                    ends_at=ends_at,
                )
                if repo_row is not None
                else None
            )
            spec = TaskSpec(
                task_id=task_id,
                title=f"Reconcile: {task['title']}",
                instructions=f"{brief}\nTerminal instruction: {finish}",
                worktree_path=task["worktree_path"] or "",
                base_sha=task["base_sha"] or "",
                provider=provider,
                model=model,
                reasoning=reasoning,
                run_id=task["run_id"],
                branch=task["branch"],
                environment=prepared.block if prepared is not None else None,
                process_env=(prepared.process_env if prepared is not None else None) or process_env,
                denied_tools=environment.denied_tools(repo_row),
            )
            session_id = None
        else:
            spec = TaskSpec(
                task_id=task_id,
                title=task["title"],
                instructions=packet or "",
                worktree_path=task["worktree_path"] or "",
                base_sha=task["base_sha"] or "",
                provider=provider,
                model=model,
                reasoning=reasoning,
                run_id=task["run_id"],
                branch=task["branch"],
                resume_session_id=session_id,
                steer_message=packet,
                process_env=process_env,
                denied_tools=environment.denied_tools(repo_row),
            )
        adapter = _adapter_for(spec.provider)
        runner = RunnerGuardian(adapter, on_exit=lambda: self._release(execution))
        with self._lock:
            self._runners[task_id] = runner
        thread = threading.Thread(
            target=self._run_task, args=(runner, spec), kwargs={"execution": execution}, daemon=True
        )
        with self._lock:
            self._threads[task_id] = thread
        store.append_event(
            conn,
            kind="resumed",
            payload={
                "task_id": task_id,
                "session_id": session_id,
                "message": message,
                "superseded_runners": superseded,
                "status": "in_progress",
                "scope_preserved": scope_preserved,
                "ends_at": ends_at,
                "lane": execution.lane,
                "reconciler": reconciler,
                **(continuation or {}),
            },
            run_id=task["run_id"],
            task_id=task_id,
        )
        self._bind(execution, task_id)
        self._start(thread)
        return {
            "task_id": task_id,
            "resumed_session": session_id,
            "status": "in_progress",
            "ends_at": ends_at,
            "superseded_runners": superseded,
            "worktree_rebuilt": rebuilt,
            "lane": execution.lane,
            "reconciler": reconciler,
        }

    def _ensure_worktree(self, conn, task) -> dict | None:
        """Give a task whose checkout is gone a new one, on its own branch, at its own head.

        Returns what was done, or None when the worktree was there all along. The
        rebuilt checkout starts from the task's branch on the forge, or in the
        base clone, or — when the branch was never pushed and the pristine
        release deleted it — from the task's recorded base commit. Task 158
        (2026-09-06) had no commits and lost its slot to a prune; `ppy resume`
        then crashed twice and the session had to be thrown away.
        """
        from pathlib import Path

        task_id = int(task["id"])
        path = task["worktree_path"]
        if path and Path(path).is_dir():
            return None
        repo_row = conn.execute("SELECT * FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
        if repo_row is None:
            raise SupervisorError(
                f"task {task_id}: its worktree {path or '(none)'} is gone and the task has no "
                "registered repository to rebuild one from"
            )
        import subprocess

        from papaya_agent_runtime import repos as _repos
        from papaya_agent_runtime.worktree.lease import _branch_exists

        branch = task["branch"]
        local = repo_row["local_path"]
        # Decided before the lease exists: leasing on a branch name creates that
        # branch when the clone lacks it, and a branch we just made is no evidence
        # of where the task's commits are.
        kept_locally = bool(branch) and _branch_exists(local, branch)
        try:
            lease = LeaseManager().acquire(
                repo_path=local, repo_id=repo_row["id"], task_id=task_id, branch=branch
            )
        except LeaseError as exc:
            raise SupervisorError(
                f"task {task_id}: its worktree is gone and no new one could be leased: {exc}"
            ) from exc

        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["git", "-C", lease.worktree_path, *args],
                capture_output=True,
                text=True,
                check=False,
            )

        from papaya_agent_runtime import reconcile

        started_from = None
        if reconcile.is_reconciliation(conn, task_id):
            # A delivered task's work is its pull request. The forge's head is the
            # truth — a local branch can be stale after a rebase pushed from elsewhere
            # — and the base commit is never an answer (PAP-222, 2026-09-17).
            remote = _repos.upstream_remote(repo_row)
            for refspec, label in reconcile.pr_head_sources(conn, task):
                fetched = git("fetch", "--quiet", remote, refspec).returncode == 0
                if fetched and git("reset", "--hard", "--quiet", "FETCH_HEAD").returncode == 0:
                    head = git("rev-parse", "HEAD").stdout.strip()
                    started_from = f"{label} at {head[:8]} (from {remote})"
                    break
            if started_from is None:
                head = git("rev-parse", "HEAD").stdout.strip() if kept_locally else ""
                if not kept_locally or head == (task["base_sha"] or lease.base_sha):
                    raise SupervisorError(
                        f"task {task_id}: its worktree is gone and its pull request head could "
                        f"not be fetched from {remote}; refusing to rebuild it at the base commit"
                    )
                started_from = (
                    f"branch {branch} at {head[:8]} (kept in the base clone; the pull request "
                    f"head could not be fetched from {remote})"
                )
        elif kept_locally:
            head = git("rev-parse", "HEAD").stdout.strip()
            started_from = f"branch {branch} at {head[:8]} (kept in the base clone)"
        elif branch:
            remote = _repos.upstream_remote(repo_row)
            fetched = git("fetch", "--quiet", remote, branch).returncode == 0
            if fetched and git("reset", "--hard", "--quiet", "FETCH_HEAD").returncode == 0:
                head = git("rev-parse", "HEAD").stdout.strip()
                started_from = f"branch {branch} at {head[:8]} (from {remote})"
        if started_from is None:
            base = task["base_sha"] or lease.base_sha
            reset = git("reset", "--hard", "--quiet", base)
            if reset.returncode != 0:
                raise SupervisorError(
                    f"task {task_id}: rebuilt a worktree but could not move it onto the base "
                    f"commit {base[:8]}: {reset.stderr.strip()[:200]}"
                )
            started_from = f"base commit {base[:8]} (nothing of this task's was ever pushed)"
        store.update_task_fields(
            conn,
            task_id,
            worktree_path=lease.worktree_path,
            lease_id=lease.id,
            branch=lease.branch,
        )
        with self._lock:
            self._leases[task_id] = (lease, repo_row["local_path"])
        payload = {
            "task_id": task_id,
            "previous_worktree": path,
            "worktree_path": lease.worktree_path,
            "lease": lease.id,
            "branch": lease.branch,
            "started_from": started_from,
            "summary": (
                f"worktree {path or '(none)'} was gone; leased {lease.worktree_path} and "
                f"started it from {started_from}"
            ),
        }
        store.append_event(
            conn, kind="worktree_rebuilt", payload=payload, run_id=task["run_id"], task_id=task_id
        )
        return payload

    def steer_task(
        self, task_id: int, message: str, *, delivery: str = "append", by: str | None = None
    ) -> dict:
        """Steer a task.

        ``by`` is who steered — a person at a session (`store.BY_PERSON`) or `ppy
        serve` (`store.BY_MANAGER`) — and rides on every event the steer writes, so
        the rounds can tell a person's direction from their own and leave it alone.

        Interrupt steering is offered only when the provider/version proves it
        (capability-gated). Otherwise steering is checkpoint-at-completion: the
        message is queued and delivered by resuming the session once the current
        turn ends. Queued messages are additive — every one since the last
        delivery goes into that resume, in the order sent — unless ``delivery``
        is ``"replace"``, which makes this message supersede everything queued
        before it. On 2026-09-07 three additive messages were queued and only the
        last survived (issue #75); the response now says what is queued.
        """
        if delivery not in ("append", "replace"):
            raise SupervisorError(f"delivery must be 'append' or 'replace', not {delivery!r}")
        conn = init_db()
        task = store.get_task(conn, task_id)
        if task is None:
            raise SupervisorError(f"task {task_id} not found")
        provider = task["provider"] or "fake"
        if provider in ("claude", "codex"):
            # A steer that interrupts a turn promises to resume it. Revalidate
            # first so lowering the ceiling cannot destroy a currently running
            # allowed-at-launch session and only then discover it may not resume.
            self._resolve_and_enforce(provider, task["model"], task["reasoning"])
        adapter = _adapter_for(provider)
        can_interrupt = getattr(adapter, "supports_interrupt_steer", lambda: False)()

        # A steer only reaches a worker that is actually mid-turn. A finished,
        # delivered, blocked, or failed task — or one whose runner process has
        # already exited — has no turn to interrupt and no checkpoint coming, so
        # the only way the message can land is by resuming the session with it.
        # (Queueing it "for the checkpoint" of a turn that already ended is how a
        # steer silently vanished on 2026-08-31.)
        if not self._has_live_turn(conn, task):
            return {
                "mode": "resume",
                "note": "no live worker turn; resumed the session with the steer",
                **self.resume_task(task_id, message, by=by),
            }

        if can_interrupt:
            return self._interrupt_steer(conn, task, message, by=by)

        # Checkpoint steering: queue the message; the checkpoint delivers the queue.
        event_id = store.append_event(
            conn,
            kind="steer",
            payload={
                "task_id": task_id,
                "mode": "checkpoint_pending",
                "message": message,
                "delivery": delivery,
                **({"by": by} if by else {}),
            },
            run_id=task["run_id"],
            task_id=task_id,
        )
        pending = self._pending_checkpoint_steers(conn, task_id)
        included, superseded = _split_replaced(pending)
        queue = [
            {
                "event": eid,
                "delivery": payload.get("delivery", "append"),
                "preview": _preview(payload.get("message")),
                "will_be": "delivered" if (eid, payload) in included else "superseded",
            }
            for eid, payload in pending
        ]
        if delivery == "replace":
            what = (
                f"queued as a replacement: it supersedes the {len(superseded)} message(s) "
                "queued before it, which will not be delivered"
                if superseded
                else "queued as a replacement (nothing earlier was queued)"
            )
        else:
            what = (
                f"queued; {len(included)} message(s) will be delivered together, in the "
                "order sent, when the current turn ends"
                if len(included) > 1
                else "queued; delivered when the current turn ends"
            )
        return {
            "mode": "checkpoint_pending",
            "task_id": task_id,
            "steer_event": event_id,
            "delivery": delivery,
            "queue": queue,
            "note": (
                f"provider does not support mid-flight steer; {what}. If capacity or the "
                "ceiling refuses that resume, the queue stays and is retried when a slot "
                "frees or on reconcile"
            ),
        }

    # How long the auto-resume waits for an interrupted worker process to exit
    # before resuming anyway (the resume is by session id, so it is safe either way).
    INTERRUPT_DRAIN_SECONDS = 60.0

    def _interrupt_steer(self, conn, task, message: str, *, by: str | None = None) -> dict:
        """Interrupt the live turn and resume it with the steer, staying in_progress.

        The interrupted process exits nonzero and its adapter reports ``failed``.
        Before the signal goes out the runner is marked superseded, so that exit is
        recorded against the retired session instead of stamping the task — a steer
        is a redirection, not a failure (2026-09-02, task 78). The resume carrying
        the steer text is fired as soon as the old process is gone.
        """
        task_id = int(task["id"])
        with self._lock:
            runner = self._runners.get(task_id)
        superseded = store.supersede_runners(conn, task_id, reason="steer-interrupt")
        store.set_task_status(conn, task_id, "in_progress")
        if runner is not None:
            runner.interrupt()
        store.append_event(
            conn,
            kind="steer",
            payload={
                "task_id": task_id,
                "mode": "interrupt_resume",
                "message": message,
                "superseded_runners": superseded,
                "status": "in_progress",
                **({"by": by} if by else {}),
            },
            run_id=task["run_id"],
            task_id=task_id,
        )
        thread = threading.Thread(
            target=self._resume_after_interrupt,
            args=(task_id, message, [r["runner"] for r in superseded]),
            kwargs={"by": by} if by else {},
            daemon=True,
        )
        self._start(thread)
        return {
            "mode": "interrupt_resume",
            "task_id": task_id,
            "status": "in_progress",
            "superseded_runners": superseded,
            "note": (
                "interrupt delivered; the session is being resumed with the steer — "
                "the task stays in_progress"
            ),
        }

    def _resume_after_interrupt(
        self, task_id: int, message: str, runner_ids: list[str], *, by: str | None = None
    ) -> None:
        """Wait for the interrupted worker process to die, then resume with the steer."""
        deadline = time.monotonic() + self.INTERRUPT_DRAIN_SECONDS
        while time.monotonic() < deadline:
            conn = init_db()
            pending = False
            for runner_id in runner_ids:
                row = store.get_runner(conn, runner_id)
                if row is None or row["result_recorded"]:
                    continue
                if _pid_alive(row["pid"]):
                    pending = True
            if not pending:
                break
            time.sleep(0.1)
        conn = init_db()
        task = store.get_task(conn, task_id)
        run_id = task["run_id"] if task else None
        # The interrupt has retired the worker; from here the steer is a queued
        # message like any other, delivered by the resume that carries it.
        queued = store.append_event(
            conn,
            kind="steer",
            payload={
                "task_id": task_id,
                "mode": "checkpoint_pending",
                "message": message,
                "delivery": "append",
                "after": "interrupt",
                **({"by": by} if by else {}),
            },
            run_id=run_id,
            task_id=task_id,
        )
        try:
            self.resume_task(task_id, message, continuation={"steer_events": [int(queued or 0)]})
        except Exception as exc:  # noqa: BLE001 - a failed resume is visible in events
            # No worker is running now, and saying `in_progress` would claim one
            # is. The message stays queued; the next free slot retries it.
            store.set_task_status(conn, task_id, "worker_stopped")
            self._defer_continuation(
                conn,
                task_id=task_id,
                run_id=run_id,
                kind="steer",
                reason=str(exc),
                steer_events=[int(queued or 0)],
            )
            store.append_event(
                conn,
                kind="error",
                payload={
                    "task_id": task_id,
                    "summary": (
                        f"steer interrupt landed but the resume was refused: {exc}. The task "
                        "is worker_stopped with the steer queued; it is retried when a slot "
                        "frees, or resume it by hand"
                    ),
                },
                run_id=run_id,
                task_id=task_id,
            )

    NO_LIVE_TURN_STATUSES = (
        "worker_done",
        "worker_stopped",
        "delivered",
        "closed",
        "cancelled",
        "blocked",
        "failed",
        "needs_recovery",
    )

    def _has_live_turn(self, conn, task) -> bool:
        """True only while a worker process is actually running a turn for the task."""
        if task["status"] in self.NO_LIVE_TURN_STATUSES:
            return False
        runner = conn.execute(
            "SELECT status FROM runners WHERE task_id = ? ORDER BY started_at DESC LIMIT 1",
            (task["id"],),
        ).fetchone()
        return bool(runner) and runner["status"] in ("starting", "running")

    def _pending_checkpoint_steers(self, conn, task_id: int) -> list[tuple[int, dict]]:
        """Queued checkpoint steers not yet carried by a launch, oldest first.

        The boundary is the newest event that proves delivery: a ``resumed``
        event naming ``steer_events`` (written as part of the launch itself), or
        a ``steer_applied`` from before that record existed.
        """
        rows = conn.execute(
            "SELECT id, kind, payload FROM events WHERE task_id = ? "
            "AND kind IN ('steer', 'steer_applied', 'resumed') ORDER BY id DESC",
            (task_id,),
        ).fetchall()
        pending: list[tuple[int, dict]] = []
        for row in rows:
            payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else {}
            if row["kind"] == "steer_applied":
                break
            if row["kind"] == "resumed":
                if payload.get("steer_events"):
                    break
                continue
            if payload.get("mode") == "checkpoint_pending":
                pending.append((int(row["id"]), payload))
        pending.reverse()
        return pending

    def _apply_pending_steer(self, spec: TaskSpec) -> None:
        """Deliver the checkpoint-queued steers now that the worker's turn has ended.

        ``steer_task`` promises delivery at the checkpoint; this is that
        checkpoint. Every queued message since the last delivery goes into one
        resume, in order — unless a message was queued to replace, in which case
        it supersedes what came before it (issue #75). Consumption is the
        launch: a refused resume leaves every message queued and records why
        (issue #71).
        """
        conn = init_db()
        self._deliver_pending_steers(conn, spec.task_id, spec.run_id)

    def _deliver_pending_steers(self, conn, task_id: int, run_id: int | None) -> dict | None:
        pending = self._pending_checkpoint_steers(conn, task_id)
        if not pending:
            return None
        included, superseded = _split_replaced(pending)
        message = _compose_checkpoint(included)
        ids = [event_id for event_id, _ in included]
        dropped = [event_id for event_id, _ in superseded]
        try:
            self.resume_task(
                task_id,
                message,
                continuation={"steer_events": ids, "superseded_steer_events": dropped},
            )
        except Exception as exc:  # noqa: BLE001 - the refusal is recorded, not raised
            return self._defer_continuation(
                conn,
                task_id=task_id,
                run_id=run_id,
                kind="steer",
                reason=str(exc),
                steer_events=ids,
                superseded_steer_events=dropped,
            )
        applied = {
            "task_id": task_id,
            "steer_event": ids[-1],
            "steer_events": ids,
            "superseded_steer_events": dropped,
            "composed": len(ids) > 1,
        }
        store.append_event(
            conn, kind="steer_applied", payload=applied, run_id=run_id, task_id=task_id
        )
        return applied

    def _defer_continuation(
        self, conn, *, task_id: int, run_id: int | None, kind: str, reason: str, **details
    ) -> dict:
        """Record that a continuation could not launch and stays pending.

        Recorded once per distinct reason: a retry that fails the same way
        appends nothing, so a full pool does not fill the log, while a newly
        lowered ceiling shows up the first time it refuses.
        """
        last = _latest_deferred(conn, task_id)
        payload = {"task_id": task_id, "kind": kind, "reason": reason, **details}
        if last is not None and last.get("kind") == kind and last.get("reason") == reason:
            return payload
        payload["summary"] = (
            f"{kind} for task {task_id} could not be delivered ({reason}); it stays pending "
            "and is retried when a worker slot frees or on reconcile"
        )
        store.append_event(
            conn, kind="continuation_deferred", payload=payload, run_id=run_id, task_id=task_id
        )
        return payload

    def retry_deferred_continuations(self) -> list[dict]:
        """Try once, now, to launch every continuation a refusal left pending.

        Called whenever a slot frees, from ``reconcile``, and on the server's
        health tick (which is how a pending continuation survives a supervisor
        restart). Each pending continuation is attempted at most once per call;
        one that is refused again simply waits for the next call. Nothing here
        bypasses admission or the ceiling — that is the point.
        """
        conn = init_db()
        attempted: list[dict] = []
        for task_id, deferred in _pending_deferred(conn):
            task = store.get_task(conn, task_id)
            if task is None or task["status"] in ("delivered", "closed", "cancelled"):
                continue
            kind = deferred.get("kind")
            outcome: dict | None = None
            if kind == "steer":
                outcome = self._deliver_pending_steers(conn, task_id, task["run_id"])
            elif kind == "answer":
                outcome = self._deliver_stored_answer(
                    conn,
                    task_id=task_id,
                    run_id=task["run_id"],
                    question=str(deferred.get("question") or ""),
                    answer=str(deferred.get("answer") or ""),
                    decision_id=deferred.get("decision_id"),
                    scope=deferred.get("scope"),
                )
            if outcome is not None:
                attempted.append({"task_id": task_id, "kind": kind, **outcome})
        return attempted

    def _deliver_stored_answer(
        self,
        conn,
        *,
        task_id: int,
        run_id: int | None,
        question: str,
        answer: str,
        decision_id,
        scope,
    ) -> dict:
        """Resume a blocked task with a stored decision's answer; consumed only on launch."""
        continuation = {"answered_question": question, "decision_id": decision_id}
        try:
            self.resume_task(task_id, answer, continuation=continuation)
        except Exception as exc:  # noqa: BLE001 - recorded, and retried later
            return self._defer_continuation(
                conn,
                task_id=task_id,
                run_id=run_id,
                kind="answer",
                reason=str(exc),
                question=question,
                answer=answer,
                decision_id=decision_id,
                scope=scope,
            )
        self._auto_answered.setdefault(task_id, set()).add(decisions.fingerprint(question))
        payload = {
            "task_id": task_id,
            "question": question,
            "answer": answer,
            "decision_id": decision_id,
            "scope": scope,
        }
        store.append_event(
            conn, kind="auto_answered", payload=payload, run_id=run_id, task_id=task_id
        )
        return payload

    def _run_task(
        self, runner: RunnerGuardian, spec: TaskSpec, execution: _Execution | None = None
    ) -> None:
        try:
            try:
                result = runner.run(spec)
            finally:
                # The slot covers the provider process only. Follow-up bookkeeping,
                # review waits, and checkpoint auto-resume do not retain it. It is
                # released by identity: this execution's, never "the task's". The
                # runner already released it when the process exited; this covers
                # a run that never got that far.
                self._release(execution)
        except Exception as exc:  # noqa: BLE001 - record, never crash the supervisor
            runner.interrupt()
            # A lock error here may be the reason ``runner.run`` escaped. Retry
            # once through a fresh connection. If both attempts fail, the atomic
            # recorder leaves result_recorded false, so dead-runner reconciliation
            # can turn the durable runner row into needs_recovery plus an error.
            for _attempt in range(2):
                conn = None
                try:
                    conn = init_db()
                    _record_runner_crash(conn, runner.runner_id, spec, exc)
                    break
                except Exception:  # noqa: BLE001 - reconciliation owns a double failure
                    pass
                finally:
                    if conn is not None:
                        with contextlib.suppress(Exception):
                            conn.close()
            from papaya_agent_runtime import deficiencies

            deficiencies.record_exception(
                "the supervisor's worker thread", exc, task_id=spec.task_id, run_id=spec.run_id
            )
        else:
            if runner.superseded:
                # A retired session's ending is not this task's checkpoint.
                return
            if result.status == "blocked" and result.question:
                self._maybe_auto_answer(spec, result.question)
            else:
                self._apply_pending_steer(spec)
        # A task that failed without touching its worktree has nothing worth
        # keeping; hand the slot back so the pool cannot fill up with dead leases.
        with contextlib.suppress(Exception):  # lease hygiene never breaks worker completion
            self._release_lease_if_pristine(spec.task_id)
        # This is a cheap deterministic due check, not a model call. It makes
        # run-count and failure-triggered reviews durable immediately.
        try:
            from papaya_agent_runtime.assessments import ensure_due

            ensure_due()
        except Exception:  # noqa: BLE001 - maintenance never breaks worker completion
            pass
        # This execution's slot is free: a continuation that a full pool refused
        # earlier gets its one try now.
        with contextlib.suppress(Exception):
            self.retry_deferred_continuations()

    def _release_lease_if_pristine(self, task_id: int) -> None:
        """Release a failed task's lease when its worktree holds no work.

        A failed worker that never committed or edited anything (a rejected model
        name, a crashed launch) leaves a pristine worktree. Keeping that lease
        only starves later dispatches. A worktree with uncommitted edits or new
        commits is kept for inspection and ``ppy resume``.
        """
        conn = init_db()
        task = store.get_task(conn, task_id)
        if task is None or task["status"] != "failed":
            return
        with self._lock:
            entry = self._leases.get(task_id)
        if entry is None:
            return
        lease, repo_path = entry
        assert isinstance(lease, Lease)
        mgr = LeaseManager(lease.backend)
        try:
            pristine = not mgr.is_dirty(lease) and mgr.head_sha(lease) == (lease.base_sha or "")
        except LeaseError:
            # Missing or unreadable worktree: nothing to preserve.
            pristine = True
        if not pristine:
            return
        try:
            self.release_task_lease(task_id, remove_branch=True)
        except LeaseError:
            return
        store.append_event(
            conn,
            kind="lease_released",
            payload={"task_id": task_id, "lease": lease.id, "reason": "failed, pristine"},
            run_id=task["run_id"],
            task_id=task_id,
        )

    def _maybe_auto_answer(self, spec: TaskSpec, question: str) -> None:
        """Resolve a blocking question from durable decisions, or leave it for a human.

        A routine question already answered by a scoped decision is resumed
        autonomously. A question that reappears after we applied its stored answer
        is left actionable so the human is not looped.
        """
        conn = init_db()
        fp = decisions.fingerprint(question)
        answered = self._auto_answered.setdefault(spec.task_id, set())
        if fp in answered:
            store.append_event(
                conn,
                kind="blocked",
                payload={
                    "task_id": spec.task_id,
                    "question": question,
                    "summary": "recurring question after auto-answer; escalating to human",
                },
                run_id=spec.run_id,
                task_id=spec.task_id,
            )
            return
        context = _task_context(conn, spec.task_id)
        try:
            match = decisions.find_matching(
                conn, question, run_id=spec.run_id, task_id=spec.task_id, context=context
            )
        except decisions.StaleDecision as stale:
            # A prior answer exists but the premise changed: ask for the delta.
            store.append_event(
                conn,
                kind="blocked",
                payload={
                    "task_id": spec.task_id,
                    "question": question,
                    "summary": "premise changed since a prior decision; confirmation needed",
                    "prior_decision_id": stale.decision.id,
                    "prior_answer": stale.decision.answer,
                },
                run_id=spec.run_id,
                task_id=spec.task_id,
            )
            return
        if match is None:
            return  # leave the `question` event actionable for a human
        # The fingerprint is remembered, and `auto_answered` written, only once
        # the resume carrying the answer has launched; a refusal leaves the
        # question actionable and the answer pending for retry.
        self._deliver_stored_answer(
            conn,
            task_id=spec.task_id,
            run_id=spec.run_id,
            question=question,
            answer=match.answer,
            decision_id=match.id,
            scope=match.scope,
        )

    def answer_question(
        self,
        task_id: int,
        answer: str,
        *,
        scope: str = "run",
        rationale: str | None = None,
        by: str | None = None,
    ) -> dict:
        """Record the user's answer as a durable decision and resume the task."""
        conn = init_db()
        task = store.get_task(conn, task_id)
        if task is None:
            raise SupervisorError(f"task {task_id} not found")
        question = _last_question(conn, task_id)
        if question is None:
            raise SupervisorError(f"task {task_id} has no pending question")
        decision_id = decisions.record_decision(
            conn,
            question=question,
            answer=answer,
            scope=scope,
            run_id=task["run_id"],
            task_id=task_id,
            rationale=rationale,
            context=_task_context(conn, task_id),
        )
        resumed = self.resume_task(task_id, answer, by=by)
        return {"task_id": task_id, "decision_id": decision_id, **resumed}

    # ------------------------------------------------------------------ #
    # Status and wait
    # ------------------------------------------------------------------ #
    def task_status(self, task_id: int) -> dict:
        conn = init_db()
        row = store.get_task(conn, task_id)
        if row is None:
            raise SupervisorError(f"task {task_id} not found")
        return dict(row)

    def run_status(self, run_id: int) -> dict:
        conn = init_db()
        run = store.get_run(conn, run_id)
        if run is None:
            raise SupervisorError(f"run {run_id} not found")
        tasks = [dict(t) for t in store.list_tasks(conn, run_id)]
        actionable = [
            {"seq": e["seq"], "kind": e["kind"], "payload": e["payload"]}
            for e in store.actionable_events(conn, run_id)
        ]
        return {
            "run": dict(run),
            "tasks": tasks,
            "actionable": actionable,
            "usage": store.usage_totals(conn, run_id),
        }

    @staticmethod
    def _all_terminal(conn, run_id: int) -> bool:
        tasks = store.list_tasks(conn, run_id)
        if not tasks:
            return False
        terminal = {"worker_done", "blocked", "failed", "delivered", "closed", "cancelled"}
        return all(t["status"] in terminal for t in tasks)

    def wait_actionable(self, run_id: int, timeout: float = 30.0, after_seq: int = 0) -> dict:
        """Block until the run has actionable events newer than ``after_seq`` (a cursor).

        Without the cursor a caller keeps seeing the same finished worker forever;
        watchers keyed on status strings then produce false "done" ticks. Pass the
        highest ``seq`` already handled to hear only what is new.

        The statuses are read before the events. A worker's turn ends as one commit
        (status and event together, :func:`store.record_turn_result`), so a run seen
        terminal already has its event in the next read. Read the other way round,
        a turn that ended between the two reads answered "all terminal, nothing new"
        and lost the finished worker, which a busy machine did about one wait in 20.
        """
        deadline = time.monotonic() + timeout
        while True:
            conn = init_db()
            try:
                all_terminal = self._all_terminal(conn, run_id)
                actionable = [
                    e for e in store.actionable_events(conn, run_id) if int(e["seq"]) > after_seq
                ]
            finally:
                conn.close()
            if actionable or all_terminal:
                return {
                    "run_id": run_id,
                    "actionable": [
                        {"seq": e["seq"], "kind": e["kind"], "payload": e["payload"]}
                        for e in actionable
                    ],
                    "all_terminal": all_terminal,
                }
            if time.monotonic() > deadline:
                return {
                    "run_id": run_id,
                    "actionable": [],
                    "all_terminal": False,
                    "timed_out": True,
                }
            time.sleep(0.1)

    # ------------------------------------------------------------------ #
    # Reconcile
    # ------------------------------------------------------------------ #
    def close_dead_runners(self, *, grace_s: float = 0.0, source: str = "supervisor") -> list:
        """Close runner rows with no process behind them, except this process's own.

        An execution this supervisor admitted records its own end, so its task is
        skipped; every other live row is judged by its pid and heartbeat
        (:mod:`papaya_agent_runtime.supervisor.dead_runners`). Closing a row is what
        gives its slot back: admission counts live rows.
        """
        from papaya_agent_runtime.supervisor import dead_runners

        with self._lock:
            own = {e.task_id for e in self._executions.values() if e.task_id is not None}
        conn = init_db()
        try:
            return dead_runners.close_dead_runners(
                conn, grace_s=grace_s, skip_tasks=own, source=source
            )
        finally:
            conn.close()

    def reconcile(self) -> dict:
        """Find half-alive runners and mark them for recovery (fail-closed)."""
        conn = init_db()
        findings = []
        for row in store.live_runners(conn):
            pid = row["pid"]
            if not _pid_alive(pid):
                # Process is gone but no result was recorded: never assume success.
                store.update_runner(conn, row["id"], status="orphaned")
                task = store.get_task(conn, row["task_id"])
                if task and task["status"] in ("in_progress", "requested", "dispatched"):
                    store.set_task_status(conn, row["task_id"], "needs_recovery")
                    store.append_event(
                        conn,
                        kind="error",
                        payload={
                            "task_id": row["task_id"],
                            "summary": "runner process vanished without a result; "
                            "marked needs_recovery",
                        },
                        run_id=task["run_id"] if task else None,
                        task_id=row["task_id"],
                    )
                findings.append(
                    {
                        "runner": row["id"],
                        "task_id": row["task_id"],
                        "action": "orphaned->needs_recovery",
                    }
                )
        # Recovery may have freed slots, and a restart has no other trigger.
        retried = self.retry_deferred_continuations()
        return {"reconciled": findings, "continuations": retried}

    def release_task_lease(self, task_id: int, *, remove_branch: bool = False) -> None:
        with self._lock:
            entry = self._leases.pop(task_id, None)
        if entry is None:
            return
        lease, repo_path = entry
        LeaseManager(lease.backend).release(lease, repo_path=repo_path, remove_branch=remove_branch)

    def shutdown(self) -> None:
        """Stop every running worker as the supervisor goes: recorded stopped, not failed."""
        with self._lock:
            runners = list(self._runners.values())
        for runner in runners:
            runner.interrupt(shutdown=True)

    def _start(self, thread: threading.Thread) -> None:
        thread.start()
        with self._lock:
            self._started.append(thread)

    def close(self, timeout: float = 30.0) -> list[threading.Thread]:
        """Stop every worker this supervisor started and wait for its bookkeeping to end.

        Admission is refused from here on, so a continuation that would have started
        another worker is deferred instead. Returns the threads still alive at the
        deadline — empty when the supervisor is truly done.

        A supervisor's threads find the database through ``PPY_HOME`` each time they
        touch it. One left running after its owner moved on (a test ending, most
        often) finishes its bookkeeping against whichever instance that variable
        names by then: on a slow CI runner, the next test's, whose task ids restart
        at 1 (task 259).
        """
        with self._lock:
            self._closed = True
        deadline = time.monotonic() + timeout
        while True:
            self.shutdown()
            with self._lock:
                alive = [t for t in self._started if t.is_alive()]
            if not alive or time.monotonic() >= deadline:
                return alive
            for thread in alive:
                thread.join(timeout=max(0.0, min(0.2, deadline - time.monotonic())))


def _self_contained(conn, task, message: str | None) -> tuple[str | None, bool]:
    """The message plus the brief's standing scope, and whether any scope was found.

    A bare resume (no message) sends nothing new, so nothing is appended: the
    session already holds the brief. A brief archived without the four sections
    — from before they were required — is passed through unchanged.
    """
    if not message:
        return message, False
    try:
        from papaya_agent_runtime import brief_lint, pr_body

        scope = brief_lint.standing_scope(pr_body.archived_brief(conn, task))
    except Exception:  # noqa: BLE001 - a missing archive never blocks a resume
        scope = None
    if not scope:
        return message, False
    return f"{message}\n\n{scope}", True


def _preview(message: object, width: int = 72) -> str:
    text = " ".join(str(message or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _split_replaced(pending: list[tuple[int, dict]]) -> tuple[list, list]:
    """Everything after the last replacing message is included; the rest is superseded."""
    cut = 0
    for index, (_event_id, payload) in enumerate(pending):
        if payload.get("delivery") == "replace":
            cut = index
    return pending[cut:], pending[:cut]


def _compose_checkpoint(included: list[tuple[int, dict]]) -> str:
    """One message a worker can act on from several queued ones, in the order sent."""
    if len(included) == 1:
        return str(included[0][1].get("message") or "")
    parts = [
        f"{len(included)} messages were queued while your last turn ran. They are given in "
        "the order they were sent; each later one supplements the earlier ones unless it "
        "says otherwise."
    ]
    for number, (event_id, payload) in enumerate(included, start=1):
        parts.append(
            f"--- message {number} of {len(included)} (steer event {event_id}) ---\n"
            f"{payload.get('message') or ''}"
        )
    return "\n\n".join(parts)


def _latest_deferred(conn, task_id: int) -> dict | None:
    row = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = 'continuation_deferred' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload"])
    except (ValueError, TypeError):
        return None


def _pending_deferred(conn) -> list[tuple[int, dict]]:
    """Tasks whose newest deferred continuation has not been followed by a launch."""
    rows = conn.execute(
        "SELECT task_id, MAX(id) AS last FROM events WHERE kind = 'continuation_deferred' "
        "GROUP BY task_id"
    ).fetchall()
    out: list[tuple[int, dict]] = []
    for row in rows:
        launched = conn.execute(
            "SELECT 1 FROM events WHERE task_id = ? AND kind = 'resumed' AND id > ? LIMIT 1",
            (row["task_id"], row["last"]),
        ).fetchone()
        if launched is not None:
            continue
        payload_row = conn.execute(
            "SELECT payload FROM events WHERE id = ?", (row["last"],)
        ).fetchone()
        try:
            payload = json.loads(payload_row["payload"])
        except (ValueError, TypeError):
            continue
        out.append((int(row["task_id"]), payload))
    return out


def _task_context(conn, task_id: int) -> str | None:
    """A compact premise fingerprint for a task's decisions.

    Uses the task's repo base SHA: if the repository state a decision was made
    under has moved, the decision is treated as premise-changed on reuse. This is
    a conservative proxy for "relevant repository facts changed".
    """
    row = conn.execute("SELECT base_sha FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    base = row["base_sha"]
    return f"repo@{base[:12]}" if base else None


def _last_question(conn, task_id: int) -> str | None:
    import json

    row = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind IN ('question','blocked') "
        "ORDER BY seq DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload"]).get("question")
    except (ValueError, KeyError, TypeError):
        return None
