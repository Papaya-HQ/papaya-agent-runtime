"""Heavy gates run one at a time per repository, and wait for the memory they need.

2026-09-17 00:05 UTC: two backend workers ran `make verify` at the same time under the
supervisor (1.3 GB and 0.9 GB resident), the machine ran low on memory, and other
processes were killed. The suite takes sixteen minutes; two at once bought nothing.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import make_git_repo, wait_until
from papaya_agent_runtime import budgets, gate, progress, repos, rounds, serve, team
from papaya_agent_runtime.state import init_db, store


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _task(tmp_path: Path, repo_id: int, name: str) -> int:
    worktree = Path(make_git_repo(tmp_path / name))
    conn = init_db()
    try:
        run_id = store.create_run(conn, name)
        task_id = store.add_task(conn, run_id=run_id, title=name, repo_id=repo_id)
        store.update_task_fields(conn, task_id, worktree_path=str(worktree), status="in_progress")
    finally:
        conn.close()
    return task_id


def _repo(tmp_path: Path, name: str) -> int:
    conn = init_db()
    try:
        repo_id = store.add_repo(
            conn,
            name=name,
            origin=f"https://github.com/acme/{name}.git",
            local_path=str(Path(make_git_repo(tmp_path / f"{name}-base"))),
            default_branch="main",
            base_sha="a" * 40,
        )
    finally:
        conn.close()
    repos.set_settings(name, local_gate="make test", full_suite_command="make verify")
    return repo_id


@pytest.fixture
def world(tmp_path, ppy_home):
    """Two tasks in `app`, one in `api`, each with its own worktree."""
    app = _repo(tmp_path, "app")
    api = _repo(tmp_path, "api")
    return {
        "app1": _task(tmp_path, app, "app1"),
        "app2": _task(tmp_path, app, "app2"),
        "api1": _task(tmp_path, api, "api1"),
    }


class HeldRunner:
    """A gate that runs until the test lets it finish."""

    def __init__(self) -> None:
        self.started: list[tuple[int | None, bool]] = []
        self._release: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def _event(self, key: str) -> threading.Event:
        with self._lock:
            return self._release.setdefault(key, threading.Event())

    def release(self, spec_key: str) -> None:
        self._event(spec_key).set()

    def __call__(self, spec: gate.GateSpec, *, on_progress, on_process) -> gate.GateResult:
        self.started.append((spec.task_id, spec.full))
        self._event(spec.key).wait(10)
        return gate.GateResult(
            repo=spec.repo,
            command=spec.command,
            full=spec.full,
            exit_code=0,
            duration_seconds=1.0,
            summary="1 passed",
            head_sha=spec.head_sha,
            output_path="",
            started_at="",
            finished_at="",
            task_id=spec.task_id,
        )


class GatesClient:
    """The supervisor socket, answered by a `Gates` in this process."""

    def __init__(self, gates: gate.Gates) -> None:
        self.gates = gates

    def ping(self) -> dict:
        return {"ok": True}

    def gate_start(self, *, task_id, repo, full) -> dict:
        spec = gate.resolve(task_id=task_id, repo=repo, full=full)
        return {"ok": True, **self.gates.start(spec)}

    def gate_wait(self, key: str, timeout: float = 60.0) -> dict:
        return {"ok": True, **self.gates.wait(key, min(timeout, 0.01))}


def _gates(runner, clock, **seams) -> gate.Gates:
    defaults = {
        "settings": lambda: gate.GateSettings(),
        "learned": lambda _repo, _full: None,
        "free_memory": lambda: None,
        "needed_memory": lambda _repo, _settings: None,
        "poll": 0.01,
    }
    defaults.update(seams)
    return gate.Gates(runner=runner, clock=clock, **defaults)


def _events(task_id: int, kind: str) -> list[dict]:
    conn = init_db()
    try:
        rows = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]
    finally:
        conn.close()


def _observations(task_id: int, kind: str) -> list[float]:
    conn = init_db()
    try:
        rows = conn.execute(
            "SELECT seconds FROM repo_observations WHERE task_id = ? AND kind = ? ORDER BY id",
            (task_id, kind),
        ).fetchall()
        return [float(row["seconds"]) for row in rows]
    finally:
        conn.close()


def test_a_second_full_gate_on_one_repo_queues_and_starts_when_the_first_ends(
    world, monkeypatch
) -> None:
    first, second = world["app1"], world["app2"]
    clock = FakeClock()
    runner = HeldRunner()
    gates = _gates(runner, clock)
    client = GatesClient(gates)
    try:
        one = gate.resolve(task_id=first, full=True)
        assert not gates.start(one)["queued"]
        wait_until(lambda: runner.started == [(first, True)], 5, what="the first full gate")
        clock.now = 180.0

        lines: list[str] = []
        code = gate.run_from_cli(
            task_id=second,
            repo=None,
            full=True,
            wait_seconds=0,
            out=lines.append,
            client=client,
            clock=clock,
        )

        assert code == gate.STILL_RUNNING
        assert lines[0].startswith("queued the full suite for app")
        assert lines[1] == f"queued behind task {first}'s full gate, started 3 min ago"
        assert lines[-1].startswith("full suite still queued: queued behind task")
        assert "Run this same command again" in lines[-1]
        time.sleep(0.1)
        assert runner.started == [(first, True)], "the second full gate started alongside"

        # Somebody watching the worker sees it queued, not silent.
        monkeypatch.setattr(
            "papaya_agent_runtime.supervisor.client.SupervisorClient",
            lambda: client,
        )
        state = rounds.gate_state(second)
        assert state.running and state.queued
        assert serve.gate_line(state).startswith("Gate queued under the supervisor: full suite")
        assert f"queued behind task {first}'s full gate" in serve.gate_line(state)
        (worker,) = [w for w in team.snapshot()["workers"] if w["task_id"] == second]
        assert worker["gate"]["state"] == "queued"
        assert "queued behind" in team._worker_line(worker)

        runner.release(one.key)
        wait_until(
            lambda: runner.started == [(first, True), (second, True)],
            5,
            what="the queued full gate to start once the first ended",
        )
        two = gate.resolve(task_id=second, full=True)
        runner.release(two.key)
        assert gates.wait(two.key, 5)["result"]["exit_code"] == 0
        (queued,) = _events(second, gate.GATE_QUEUED)
        assert queued["reason"].startswith(f"queued behind task {first}'s full gate")
        (left,) = _events(second, gate.GATE_UNQUEUED)
        assert left["started"] is True
    finally:
        gates.close()


def test_full_gates_on_two_repos_run_at_once(world) -> None:
    clock = FakeClock()
    runner = HeldRunner()
    gates = _gates(runner, clock)
    try:
        app = gate.resolve(task_id=world["app1"], full=True)
        api = gate.resolve(task_id=world["api1"], full=True)
        assert not gates.start(app)["queued"]
        assert not gates.start(api)["queued"]
        wait_until(
            lambda: sorted(t for t, _ in runner.started) == sorted([world["app1"], world["api1"]]),
            5,
            what="both repositories' full gates running together",
        )
        assert not _events(world["api1"], gate.GATE_QUEUED)
        runner.release(app.key)
        runner.release(api.key)
    finally:
        gates.close()


def test_a_scoped_gate_is_never_queued_behind_a_full_one(world) -> None:
    clock = FakeClock()
    runner = HeldRunner()
    gates = _gates(runner, clock)
    try:
        full = gate.resolve(task_id=world["app1"], full=True)
        scoped = gate.resolve(task_id=world["app2"], full=False)
        gates.start(full)
        wait_until(lambda: len(runner.started) == 1, 5, what="the full gate")

        answer = gates.start(scoped)

        assert answer["queued"] is False and answer["running"] is True
        wait_until(
            lambda: (world["app2"], False) in runner.started,
            5,
            what="the scoped gate starting while the full gate runs",
        )
        assert not _events(world["app2"], gate.GATE_QUEUED)
        runner.release(full.key)
        runner.release(scoped.key)
    finally:
        gates.close()


class TwoMinuteSuite:
    def __init__(self, clock: FakeClock, argv, *, stdout, **_kwargs) -> None:
        self.clock = clock
        self.ends = clock.now + 120
        self.pid = os.getpid()
        self.returncode = None
        stdout.write("=== 40 passed in 120.00s ===\n")
        stdout.flush()

    def poll(self):
        if self.clock.now >= self.ends:
            self.returncode = 0
        return self.returncode


def test_queued_time_is_neither_gate_duration_nor_worker_silence(world) -> None:
    first, second = world["app1"], world["app2"]
    clock = FakeClock()
    base = datetime.now(UTC) - timedelta(minutes=30)
    held = HeldRunner()

    def runner(spec, *, on_progress, on_process):
        if spec.task_id == first:
            return held(spec, on_progress=on_progress, on_process=on_process)
        return gate.run(
            spec,
            on_progress=on_progress,
            on_process=on_process,
            clock=clock,
            sleep=clock.sleep,
            popen=lambda argv, **kw: TwoMinuteSuite(clock, argv, **kw),
            poll=10,
            expected=None,
            memory=lambda _pid: None,
        )

    gates = _gates(runner, clock, wall=lambda: base + timedelta(seconds=clock.now))
    conn = init_db()
    conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (base.isoformat(), second))
    conn.commit()
    conn.close()
    try:
        one = gate.resolve(task_id=first, full=True)
        two = gate.resolve(task_id=second, full=True)
        gates.start(one)
        wait_until(lambda: held.started, 5, what="the first full gate")
        assert gates.start(two)["queued"]
        wait_until(lambda: _events(second, gate.GATE_QUEUED), 5, what="the queue on the ledger")

        clock.now = 600.0  # ten minutes queued behind the first
        assert gates.wait(two.key, 0)["elapsed"] == 0.0
        held.release(one.key)
        answer = gates.wait(two.key, 5)

        assert answer["result"]["duration_seconds"] == 120
        assert _observations(second, budgets.FULL_SUITE) == [120.0]
        (left,) = _events(second, gate.GATE_UNQUEUED)
        assert left["queued_seconds"] == 600

        # Thirty minutes since dispatch, ten of them queued: twenty minutes of silence.
        progress.record(second, phase="test", note="full suite green")
        (silence,) = _observations(second, budgets.SILENCE)
        assert 20 * 60 - 5 <= silence <= 20 * 60 + 60
    finally:
        gates.close()


def test_a_result_carries_peak_memory_and_low_free_memory_queues_with_the_reason(world) -> None:
    task_id = world["app1"]
    clock = FakeClock()
    samples = iter([300.0, 1250.5, 900.0, 700.0])

    def popen(argv, **kwargs):
        return TwoMinuteSuite(clock, argv, **kwargs)

    for _ in range(3):
        spec = gate.resolve(task_id=task_id, full=True)
        result = gate.run(
            spec,
            clock=clock,
            sleep=clock.sleep,
            popen=popen,
            poll=30,
            memory_every=30,
            expected=None,
            memory=lambda _pid: next(samples, 500.0),
        )
    assert _events(task_id, gate.GATE_RESULT)[0]["peak_memory_mb"] == 1250.5
    assert result.peak_memory_mb == 500.0
    found = budgets.memory("app")
    assert found.known and found.p90_mb == 1250.5
    assert "gate_memory" in budgets.render("app", [], found)

    free = {"mb": 812.0}
    runner = HeldRunner()
    gates = gate.Gates(
        runner=runner,
        clock=clock,
        settings=lambda: gate.GateSettings(),
        learned=lambda _repo, _full: None,
        free_memory=lambda: free["mb"],
        poll=0.01,
    )
    try:
        spec = gate.resolve(task_id=world["app2"], full=True)
        gates.start(spec)
        answer = wait_until(
            lambda: (a := gates.wait(spec.key, 0))["queued"] and a,
            5,
            what="the gate queued for memory",
        )
        assert answer["queued_reason"] == (
            "queued for memory: 812M free, below this repository's gate memory p90 of 1.2G"
        )
        assert runner.started == []
        (queued,) = _events(world["app2"], gate.GATE_QUEUED)
        assert queued["reason"] == answer["queued_reason"]

        free["mb"] = 4096.0
        wait_until(lambda: runner.started, 5, what="the gate starting once memory is free")
        runner.release(spec.key)
    finally:
        gates.close()
