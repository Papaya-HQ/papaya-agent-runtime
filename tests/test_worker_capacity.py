"""Hermetic admission tests for the active worker execution bound."""

from __future__ import annotations

import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from conftest import scale
from papaya_agent_runtime import repos
from papaya_agent_runtime.config import MMConfig, WorkerCeiling, save_config
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor import core
from papaya_agent_runtime.supervisor.core import Supervisor, SupervisorError
from papaya_agent_runtime.supervisor.runner import RunnerGuardian
from papaya_agent_runtime.supervisor.server import SupervisorServer


def _bounded_config(limit: int = 1) -> None:
    save_config(
        MMConfig(
            worker=WorkerCeiling("codex", "gpt-5.6-sol", "xhigh", "gpt-5.6-sol", "high", limit)
        )
    )


def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + scale(timeout)
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for worker state")


def _live(task_id: int) -> bool:
    return bool(store.live_runners_for_task(init_db(), task_id))


def _status(task_id: int) -> str:
    return store.get_task(init_db(), task_id)["status"]


def _event_count(task_id: int, kind: str) -> int:
    row = (
        init_db()
        .execute("SELECT COUNT(*) FROM events WHERE task_id = ? AND kind = ?", (task_id, kind))
        .fetchone()
    )
    return int(row[0])


class _CrashingRunner:
    superseded = False

    def __init__(self, runner_id: str) -> None:
        self.runner_id = runner_id
        self.interrupted = False

    def run(self, spec) -> None:
        raise RuntimeError("worker bookkeeping exploded")

    def interrupt(self) -> None:
        self.interrupted = True


class _LockedTransaction:
    """Connection proxy that reproduces a SQLite write-lock failure."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def execute(self, sql, *args):
        if sql == "BEGIN IMMEDIATE":
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _running_crash_task(runner_id: str = "crashed-runner"):
    conn = init_db()
    run_id = store.create_run(conn, "crash bookkeeping")
    task_id = store.add_task(conn, run_id=run_id, title="crash")
    store.set_task_status(conn, task_id, "in_progress")
    store.register_runner(conn, runner_id=runner_id, task_id=task_id, provider="fake")
    store.update_runner(conn, runner_id, pid=2**22 + 12345, status="running")
    return SimpleNamespace(task_id=task_id, run_id=run_id), _CrashingRunner(runner_id)


def test_full_capacity_refuses_dispatch_before_task_lease_or_runner_creation(
    ppy_home, source_repo
) -> None:
    _bounded_config()
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    first = supervisor.dispatch_task(
        repo=added.name, title="hold the slot", instructions="HOLD:0.5", provider="fake"
    )
    _wait_for(lambda: _live(first["task_id"]))
    conn = init_db()
    before = (
        conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM runners").fetchone()[0],
    )

    with pytest.raises(SupervisorError, match="capacity is full"):
        supervisor.dispatch_task(repo=added.name, title="must wait", provider="fake")

    after = (
        conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM runners").fetchone()[0],
    )
    assert after == before
    _wait_for(lambda: not _live(first["task_id"]))
    admitted = supervisor.dispatch_task(repo=added.name, title="now admitted", provider="fake")
    _wait_for(lambda: _status(admitted["task_id"]) == "worker_done")


def test_two_simultaneous_requests_for_the_last_slot_admit_at_most_one(
    ppy_home, source_repo
) -> None:
    _bounded_config()
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    barrier = threading.Barrier(3)
    results: list[dict] = []
    errors: list[str] = []

    def dispatch(title: str) -> None:
        barrier.wait()
        try:
            results.append(
                supervisor.dispatch_task(
                    repo=added.name, title=title, instructions="HOLD:0.5", provider="fake"
                )
            )
        except SupervisorError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=dispatch, args=(f"candidate {i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 1
    assert len(errors) == 1 and "capacity is full" in errors[0]
    assert init_db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    _wait_for(lambda: not _live(results[0]["task_id"]))


def test_resume_at_capacity_and_duplicate_live_resume_have_no_side_effects(
    ppy_home, source_repo
) -> None:
    _bounded_config()
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    stopped = supervisor.dispatch_task(
        repo=added.name, title="needs input", instructions="ASK: which path?", provider="fake"
    )
    _wait_for(lambda: _status(stopped["task_id"]) == "blocked")
    blocker = supervisor.dispatch_task(
        repo=added.name, title="occupy capacity", instructions="HOLD:0.5", provider="fake"
    )
    _wait_for(lambda: _live(blocker["task_id"]))

    before_status = _status(stopped["task_id"])
    before_events = _event_count(stopped["task_id"], "resumed")
    with pytest.raises(SupervisorError, match="capacity is full"):
        supervisor.resume_task(stopped["task_id"], "use /v2")
    assert _status(stopped["task_id"]) == before_status
    assert _event_count(stopped["task_id"], "resumed") == before_events

    blocker_events = _event_count(blocker["task_id"], "resumed")
    with pytest.raises(SupervisorError, match="already has a (pending or )?live execution"):
        supervisor.resume_task(blocker["task_id"], "duplicate")
    assert _event_count(blocker["task_id"], "resumed") == blocker_events
    assert len(store.live_runners_for_task(init_db(), blocker["task_id"])) == 1
    _wait_for(lambda: not _live(blocker["task_id"]))


def test_new_supervisor_counts_live_runner_until_reconciled(ppy_home, source_repo) -> None:
    _bounded_config()
    added = repos.add_repo(source_repo)
    first_supervisor = Supervisor()
    first = first_supervisor.dispatch_task(
        repo=added.name, title="survive restart", instructions="HOLD:0.5", provider="fake"
    )
    _wait_for(lambda: _live(first["task_id"]))

    restarted = Supervisor()
    with pytest.raises(SupervisorError, match="capacity is full"):
        restarted.dispatch_task(repo=added.name, title="not yet", provider="fake")
    _wait_for(lambda: not _live(first["task_id"]))


def test_one_ppy_home_refuses_a_second_live_supervisor(ppy_home) -> None:
    first = SupervisorServer()
    first.start_background()
    try:
        with pytest.raises(RuntimeError, match="one supervisor per PPY_HOME"):
            SupervisorServer().start_background()
    finally:
        first.stop()


def test_slot_releases_after_worker_error_and_launch_failure(
    ppy_home, source_repo, monkeypatch
) -> None:
    _bounded_config()
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    failed = supervisor.dispatch_task(
        repo=added.name, title="provider error", instructions="FAIL", provider="fake"
    )
    _wait_for(lambda: _status(failed["task_id"]) == "failed")

    real_run = RunnerGuardian.run
    calls = 0

    def fail_first_launch(self, spec, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            self.runner_id = "failed-launch"
            store.register_runner(
                init_db(),
                runner_id=self.runner_id,
                task_id=spec.task_id,
                provider=self.adapter.name,
            )
            raise OSError("launch failed")
        return real_run(self, spec, **kwargs)

    monkeypatch.setattr(RunnerGuardian, "run", fail_first_launch)
    launch_failed = supervisor.dispatch_task(repo=added.name, title="launch error", provider="fake")
    _wait_for(lambda: _status(launch_failed["task_id"]) == "failed")
    admitted = supervisor.dispatch_task(repo=added.name, title="after failures", provider="fake")
    _wait_for(lambda: _status(admitted["task_id"]) == "worker_done")


def test_crash_bookkeeping_retries_a_one_shot_locked_write(ppy_home, monkeypatch) -> None:
    spec, runner = _running_crash_task()
    real_init_db = core.init_db
    calls = 0

    def fail_first_write():
        nonlocal calls
        calls += 1
        conn = real_init_db()
        return _LockedTransaction(conn) if calls == 1 else conn

    monkeypatch.setattr(core, "init_db", fail_first_write)
    Supervisor()._run_task(runner, spec)

    conn = real_init_db()
    task = store.get_task(conn, spec.task_id)
    recorded_runner = store.get_runner(conn, runner.runner_id)
    errors = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = 'error'",
        (spec.task_id,),
    ).fetchall()
    assert runner.interrupted is True
    assert task["status"] == "failed"
    assert len(errors) == 1
    assert "worker bookkeeping exploded" in errors[0]["payload"]
    assert recorded_runner["status"] == "failed"
    assert recorded_runner["result_recorded"] == 1


def test_double_crash_bookkeeping_failure_stays_reconcilable(ppy_home, monkeypatch) -> None:
    spec, runner = _running_crash_task()
    real_init_db = core.init_db
    calls = 0

    def fail_both_writes():
        nonlocal calls
        calls += 1
        conn = real_init_db()
        return _LockedTransaction(conn) if calls <= 2 else conn

    monkeypatch.setattr(core, "init_db", fail_both_writes)
    supervisor = Supervisor()
    supervisor._run_task(runner, spec)

    conn = real_init_db()
    assert store.get_task(conn, spec.task_id)["status"] == "in_progress"
    assert store.get_runner(conn, runner.runner_id)["result_recorded"] == 0

    findings = supervisor.reconcile()["reconciled"]
    assert findings == [
        {
            "runner": runner.runner_id,
            "task_id": spec.task_id,
            "action": "orphaned->needs_recovery",
        }
    ]
    assert store.get_task(real_init_db(), spec.task_id)["status"] == "needs_recovery"
    assert _event_count(spec.task_id, "error") == 1


def test_old_runner_crash_bookkeeping_does_not_overwrite_newer_live_runner(ppy_home) -> None:
    spec, runner = _running_crash_task("old-runner")
    conn = init_db()
    store.supersede_runners(conn, spec.task_id, reason="resume")
    store.register_runner(conn, runner_id="new-runner", task_id=spec.task_id, provider="fake")
    store.update_runner(conn, "new-runner", status="running")

    Supervisor()._run_task(runner, spec)

    conn = init_db()
    assert store.get_task(conn, spec.task_id)["status"] == "in_progress"
    assert store.get_runner(conn, "new-runner")["status"] == "running"
    assert store.get_runner(conn, "old-runner")["result_recorded"] == 1
    assert _event_count(spec.task_id, "error") == 0


def test_checkpoint_auto_resume_reacquires_a_released_slot(ppy_home, source_repo) -> None:
    _bounded_config()
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    task = supervisor.dispatch_task(
        repo=added.name, title="checkpoint", instructions="HOLD:0.3", provider="fake"
    )
    _wait_for(lambda: _live(task["task_id"]))
    response = supervisor.steer_task(task["task_id"], "apply after checkpoint")
    assert response["mode"] == "checkpoint_pending"
    _wait_for(lambda: _event_count(task["task_id"], "resumed") == 1)
    _wait_for(lambda: _status(task["task_id"]) == "worker_done")


# --------------------------------------------------------------------------- #
# Execution-owned admission (issue #70)
# --------------------------------------------------------------------------- #


def _blocked_task(supervisor, added) -> int:
    stopped = supervisor.dispatch_task(
        repo=added.name, title="needs input", instructions="ASK: which path?", provider="fake"
    )
    _wait_for(lambda: _status(stopped["task_id"]) == "blocked")
    return stopped["task_id"]


def test_two_resumes_of_one_stopped_task_admit_exactly_one_even_with_room_for_both(
    ppy_home, source_repo, monkeypatch
) -> None:
    """Capacity 2, both resumes held after admission and before binding: the
    task-keyed slot used to let both through and the second bind overwrote the
    first. Now the second is refused at admission with no side effect."""
    _bounded_config(2)
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    task_id = _blocked_task(supervisor, added)
    conn = init_db()

    def event_total() -> int:
        row = conn.execute("SELECT COUNT(*) FROM events WHERE task_id = ?", (task_id,)).fetchone()
        return int(row[0])

    events_before = event_total()
    status_before = _status(task_id)

    real_launch = supervisor._launch_resumed
    gate = threading.Barrier(2, timeout=10)

    def held_launch(conn, execution, task, **kwargs):
        # Admitted, not yet bound: hold here until the contender has been refused.
        gate.wait()
        return real_launch(conn, execution, task, **kwargs)

    monkeypatch.setattr(supervisor, "_launch_resumed", held_launch)
    results: list[dict] = []
    errors: list[str] = []

    def first() -> None:
        results.append(supervisor.resume_task(task_id, "use /v2"))

    winner = threading.Thread(target=first)
    winner.start()
    _wait_for(lambda: any(e["task_id"] == task_id for e in supervisor._active_executions()))
    # The contender arrives while the winner is admitted but unbound.
    try:
        supervisor.resume_task(task_id, "use /v3")
    except SupervisorError as exc:
        errors.append(str(exc))
    assert errors and "pending or live execution" in errors[0]
    assert _status(task_id) == status_before  # the loser wrote nothing
    assert event_total() == events_before
    gate.wait()
    winner.join(timeout=10)
    assert len(results) == 1
    _wait_for(lambda: _event_count(task_id, "resumed") == 1)
    assert len(store.live_runners_for_task(init_db(), task_id)) <= 1
    _wait_for(lambda: _status(task_id) == "worker_done")
    assert _event_count(task_id, "resumed") == 1


@pytest.mark.parametrize(
    "stage",
    ["provisioning", "adapter", "popen", "thread_start"],
)
def test_every_pre_run_failure_releases_the_slot_and_keeps_the_traceback(
    ppy_home, source_repo, stage
) -> None:
    _bounded_config(1)
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()

    class Boom(RuntimeError):
        pass

    def explode(*args, **kwargs):
        raise Boom(f"{stage} failed")

    # Its own patch context: undoing it must not undo the fixtures' environment.
    with pytest.MonkeyPatch.context() as mp:
        if stage == "provisioning":
            from papaya_agent_runtime.worktree import provision

            mp.setattr(provision, "provision_worktree", explode)
        elif stage == "adapter":
            from papaya_agent_runtime.supervisor import core

            mp.setattr(core, "_adapter_for", explode)
        elif stage == "popen":
            import subprocess

            real_popen = subprocess.Popen

            def popen(*args, **kwargs):
                # Only the worker launch starts its own session; git calls do not.
                if kwargs.get("start_new_session"):
                    explode()
                return real_popen(*args, **kwargs)

            mp.setattr(subprocess, "Popen", popen)
        elif stage == "thread_start":
            mp.setattr(threading.Thread, "start", explode)

        if stage == "popen":
            # The failure happens on the runner thread; the dispatch itself returns.
            resp = supervisor.dispatch_task(repo=added.name, title=stage, provider="fake")
            _wait_for(lambda: _status(resp["task_id"]) == "failed")
            row = (
                init_db()
                .execute("SELECT status FROM runners WHERE task_id = ?", (resp["task_id"],))
                .fetchone()
            )
            assert row["status"] == "failed"  # no stale `starting` row eats the slot
        else:
            with pytest.raises(Boom) as raised:
                supervisor.dispatch_task(repo=added.name, title=stage, provider="fake")
            assert raised.value.__traceback__ is not None
        _wait_for(lambda: supervisor._active_executions() == [])

    admitted = supervisor.dispatch_task(repo=added.name, title="after failure", provider="fake")
    _wait_for(lambda: _status(admitted["task_id"]) == "worker_done")


def test_an_old_finalizer_never_releases_a_newer_executions_slot(ppy_home, source_repo) -> None:
    _bounded_config(1)
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    task_id = _blocked_task(supervisor, added)
    # The blocked task's first execution is over. Keep its identity around.
    from papaya_agent_runtime.supervisor.core import _Execution

    stale = _Execution(token="stale-token", task_id=task_id, bound=True)

    supervisor.resume_task(task_id, "HOLD:0.6")
    _wait_for(lambda: _live(task_id))
    [current] = supervisor._active_executions()
    assert current["task_id"] == task_id

    supervisor._release(stale)  # an old finalizer, late
    assert supervisor._active_executions() == [current]
    with pytest.raises(SupervisorError, match="capacity is full"):
        supervisor.dispatch_task(repo=added.name, title="must wait", provider="fake")
    _wait_for(lambda: not _live(task_id))
    assert supervisor._active_executions() == []


def test_restart_counts_a_live_recorded_runner_and_frees_a_dead_one_after_reconcile(
    ppy_home, source_repo
) -> None:
    _bounded_config(1)
    added = repos.add_repo(source_repo)
    conn = init_db()
    run_id = store.create_run(conn, "inherited")
    task_id = store.add_task(conn, run_id=run_id, title="inherited")
    store.set_task_status(conn, task_id, "in_progress")
    # A `running` row whose process is gone, and a pid-less `starting` row: both
    # from a supervisor that died mid-flight.
    store.register_runner(conn, runner_id="dead", task_id=task_id, provider="fake")
    store.update_runner(conn, "dead", pid=2**22 + 12345, status="running")
    store.register_runner(conn, runner_id="pidless", task_id=task_id, provider="fake")

    restarted = Supervisor()
    with pytest.raises(SupervisorError, match="capacity is full"):
        restarted.dispatch_task(repo=added.name, title="not yet", provider="fake")

    findings = restarted.reconcile()["reconciled"]
    assert sorted(f["runner"] for f in findings) == ["dead", "pidless"]
    assert _status(task_id) == "needs_recovery"
    admitted = restarted.dispatch_task(repo=added.name, title="now", provider="fake")
    _wait_for(lambda: _status(admitted["task_id"]) == "worker_done")


def test_a_worktree_awaiting_review_is_not_an_execution(ppy_home, source_repo) -> None:
    _bounded_config(1)
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    done = supervisor.dispatch_task(repo=added.name, title="finished", provider="fake")
    _wait_for(lambda: _status(done["task_id"]) == "worker_done")
    assert supervisor._active_executions() == []
    again = Supervisor().dispatch_task(repo=added.name, title="next", provider="fake")
    _wait_for(lambda: _status(again["task_id"]) == "worker_done")
