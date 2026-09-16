"""Per-repository budgets: durations observed where they end, and every wait derived from them."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import make_git_repo
from papaya_agent_runtime import (
    budgets,
    cli,
    environment,
    gate,
    papaya_events,
    progress,
    readiness,
    repos,
    rounds,
    serve,
)
from papaya_agent_runtime.state import init_db, store


def _repo(name: str, path: Path | str = "/nowhere") -> int:
    conn = init_db()
    try:
        return store.add_repo(
            conn,
            name=name,
            origin=f"https://github.com/acme/{name}.git",
            local_path=str(path),
            default_branch="main",
            base_sha="a" * 40,
            forge_url=f"https://github.com/acme/{name}",
        )
    finally:
        conn.close()


def _task(repo_id: int, **fields: object) -> int:
    conn = init_db()
    try:
        run_id = store.create_run(conn, "budgets")
        task_id = store.add_task(conn, run_id=run_id, title="work", repo_id=repo_id)
        if fields:
            store.update_task_fields(conn, task_id, **fields)
        return task_id
    finally:
        conn.close()


def _observations(kind: str | None = None) -> list[dict]:
    conn = init_db()
    try:
        rows = conn.execute(
            "SELECT * FROM repo_observations WHERE ? IS NULL OR kind = ? ORDER BY id",
            (kind, kind),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ── the derivation ──────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Suite:
    """A gate process that finishes green after ``seconds`` on the fake clock."""

    def __init__(self, clock: FakeClock, seconds: float, *, stdout, **_kwargs) -> None:
        self.clock, self.seconds = clock, seconds
        self.pid = os.getpid()
        self.returncode = None
        stdout.write("12 passed\n")
        stdout.flush()

    def poll(self):
        if self.clock.now >= self.seconds:
            self.returncode = 0
        return self.returncode


def _run_gate(task_id: int, minutes: float, lines: list[str] | None = None) -> gate.GateResult:
    clock = FakeClock()
    spec = gate.resolve(task_id=task_id)
    return gate.run(
        spec,
        on_progress=(lines if lines is not None else []).append,
        clock=clock,
        sleep=clock.sleep,
        popen=lambda _argv, **kwargs: Suite(clock, minutes * 60, **kwargs),
        poll=30,
    )


def test_three_gate_runs_derive_a_thirty_minute_budget_and_one_run_stays_default(
    tmp_path, ppy_home
) -> None:
    worktree = Path(make_git_repo(tmp_path / "a"))
    task_id = _task(_repo("repo-a", worktree), worktree_path=str(worktree), status="in_progress")
    repos.set_settings("repo-a", local_gate="make test")
    other = Path(make_git_repo(tmp_path / "b"))
    other_task = _task(_repo("repo-b", other), worktree_path=str(other), status="in_progress")
    repos.set_settings("repo-b", local_gate="make test")

    for minutes in (4, 6, 20):
        _run_gate(task_id, minutes)
    _run_gate(other_task, 5)

    a = budgets.budget("repo-a", budgets.GATE)
    assert (a.source, a.observations, a.p90, a.seconds) == (budgets.DERIVED, 3, 1200.0, 1800.0)
    b = budgets.budget("repo-b", budgets.GATE)
    assert (b.source, b.observations, b.seconds) == (budgets.DEFAULT, 1, budgets.TOOL_CAP_SECONDS)
    assert "fewer than 3 observations" in b.line()
    assert [o["outcome"] for o in _observations(budgets.GATE)] == ["pass"] * 4

    # A gate past the derived budget says so, once, and is still a duration observation.
    lines: list[str] = []
    _run_gate(task_id, 35, lines)
    overdue = [line for line in lines if "taking longer than usual" in line]
    assert overdue == [
        "local gate is taking longer than usual: 30m30s elapsed, past this repository's "
        "budget of 30m00s"
    ]


def test_an_observation_flagged_stalled_is_left_out_of_the_silence_derivation(ppy_home) -> None:
    for minutes in (20, 20, 20):
        budgets.observe("repo-a", budgets.SILENCE, minutes * 60)
    budgets.observe("repo-a", budgets.SILENCE, 80 * 60, outcome=budgets.STALL)
    budgets.observe("repo-a", budgets.SILENCE, 85 * 60, outcome=budgets.KILL)

    silence = budgets.budget("repo-a", budgets.SILENCE)
    assert (silence.observations, silence.p90, silence.seconds) == (3, 1200.0, 1800.0)

    # And the write point flags it: a note after a quiet check-in on the worker ends a stall.
    task_id = _task(_repo("repo-c"))
    progress.record(task_id, phase="plan", note="the approach")
    conn = init_db()
    store.append_event(
        conn,
        kind=rounds.ROUND_EVENT,
        payload={"action": "checkin", "trigger": "quiet", "worker_task_id": task_id},
    )
    conn.close()
    progress.record(task_id, phase="implement", note="back at it")
    progress.record(task_id, phase="test", note="gate next")

    mine = [o for o in _observations(budgets.SILENCE) if o["repo"] == "repo-c"]
    assert [o["outcome"] for o in mine] == ["", budgets.STALL, ""]
    (plan,) = _observations(budgets.PLAN)
    assert plan["repo"] == "repo-c" and plan["task_id"] == task_id


def test_the_p90_is_nearest_rank_over_the_newest_twenty(ppy_home) -> None:
    assert budgets.percentile([240, 360, 1200]) == 1200
    assert budgets.percentile(range(1, 21)) == 18
    now = datetime(2026, 9, 16, tzinfo=UTC)
    for index in range(25):  # five slow old runs, then twenty quick ones
        seconds = 5000 if index < 5 else 100 + index
        budgets.observe("repo-a", budgets.GATE, seconds, at=now - timedelta(minutes=60 - index))
    found = budgets.budget("repo-a", budgets.GATE, now=now)
    assert (found.observations, found.p90) == (20, 122.0)


def test_old_observations_fall_out_of_the_window(ppy_home) -> None:
    now = datetime(2026, 9, 16, tzinfo=UTC)
    for days in (20, 19, 18):
        budgets.observe("repo-a", budgets.GATE, 3000, at=now - timedelta(days=days))
    assert budgets.budget("repo-a", budgets.GATE, now=now).source == budgets.DEFAULT
    assert budgets.budget("repo-a", budgets.GATE, now=now - timedelta(days=10)).derived


# ── the consumers ───────────────────────────────────────────────────────────


def _held_ticket(worker_repo: str | None) -> serve.Ticket:
    event = papaya_events.PapayaEvent(
        id="e1", kind="work_item.assigned", subject="work_item:1", payload={}, work_item_id="1"
    )
    return serve.Ticket(
        held=serve.Held(task_id=999, run_id=999, repo=None, event=event),
        job=None,
        phase=serve.PHASE_DISPATCHED,
        worker=serve.Worker(task_id=7, status="in_progress", repo=worker_repo, branch=None),
    )


def test_the_rounds_quiet_threshold_is_the_workers_repo_silence_budget(
    ppy_home, monkeypatch
) -> None:
    for minutes in (20, 30, 40):
        budgets.observe("repo-a", budgets.SILENCE, minutes * 60)
    seen: dict[str | None, timedelta] = {}

    def look(task_id, *, now, quiet_after):
        seen[ticket.worker.repo] = quiet_after
        return None

    monkeypatch.setattr(rounds, "look_at_worker", look)
    walker = rounds.Rounds(built=None, runner=object())
    now = datetime.now(UTC)
    for repo in ("repo-a", "repo-b"):
        ticket = _held_ticket(repo)
        asyncio.run(walker._look_at(ticket, now))

    assert seen["repo-a"] == timedelta(minutes=60)
    assert seen["repo-a"] == timedelta(seconds=budgets.budget("repo-a", budgets.SILENCE).seconds)
    assert seen["repo-b"] == timedelta(minutes=15)  # health.quiet_minutes, the global default


def test_plan_and_midpoint_check_ins_read_the_repos_budgets(ppy_home) -> None:
    for minutes in (30, 30, 30):
        budgets.observe("slow", budgets.PLAN, minutes * 60)
        budgets.observe("slow", budgets.WORKER_SESSION, minutes * 60 * 4)
    now = datetime.now(UTC)
    look = rounds.WorkerLook(
        task_id=7,
        status="in_progress",
        branch=None,
        created_at=now - timedelta(minutes=30),
        verdict="alive",
        silent_seconds=10,
        last_event_id=1,
        progress=[],
        question=None,
        stopped=None,
        last_acted_id=0,
    )
    default_due = rounds.Rounds._checkins_due(look, now, [], rounds.worker_budgets("fast"))
    assert [trigger for trigger, _why in default_due] == ["plan", "midpoint"]
    slow = rounds.worker_budgets("slow")
    assert (slow.plan_seconds, slow.midpoint_seconds) == (45 * 60, 90 * 60)
    assert rounds.Rounds._checkins_due(look, now, [], slow) == []


def test_a_waiting_turn_on_a_repo_with_a_long_gate_waits_the_gate_out(ppy_home) -> None:
    assert serve.rerun_delay(1) == 300
    for minutes in (40, 40, 40):
        budgets.observe("backend", budgets.FULL_SUITE, minutes * 60)
    assert serve.known_gate_budget("frontend") is None
    assert serve.rerun_delay(1, serve.known_gate_budget("backend")) == 3600
    assert serve.rerun_delay(4, 600) == 1800


def test_the_environment_block_says_how_long_the_gate_has_taken(tmp_path, ppy_home) -> None:
    worktree = Path(make_git_repo(tmp_path / "wt"))
    repo_id = _repo("backend", worktree)
    task_id = _task(repo_id)
    conn = init_db()
    first = environment.prepare(
        conn, store.get_repo(conn, "backend"), task_id=task_id, worktree=str(worktree), branch="b"
    )
    assert "How long gates take here" not in first.block
    for minutes in (10, 12, 14):
        budgets.observe("backend", budgets.FULL_SUITE, minutes * 60)
    second = environment.prepare(
        conn, store.get_repo(conn, "backend"), task_id=task_id, worktree=str(worktree), branch="b"
    )
    conn.close()
    assert "the full gate here has taken about 14 minutes (p90 of its last 3 runs)" in second.block


def test_ppy_repo_budgets_names_derived_default_and_override(ppy_home, capsys) -> None:
    _repo("repo-a")
    _repo("repo-b")
    for minutes in (4, 6, 20):
        budgets.observe("repo-a", budgets.GATE, minutes * 60)

    assert cli.main(["repo", "set", "repo-a", "--budget", "silence=1200"]) == 0
    capsys.readouterr()
    assert cli.main(["repo", "budgets"]) == 0
    out = capsys.readouterr().out

    lines = {
        (block.split(":")[0], line.split()[0]): line
        for block in out.split("\nrepo-")[1:]
        for line in block.splitlines()[1:]
    }
    gate_a = lines[("a", "gate")]
    assert "3 obs" in gate_a and "p90     20m" in gate_a and "budget     30m" in gate_a
    assert "derived" in gate_a
    assert "override" in lines[("a", "silence")] and "budget     20m" in lines[("a", "silence")]
    assert "default" in lines[("b", "gate")]
    assert "default" in lines[("a", "plan")]

    assert cli.main(["repo", "set", "repo-a", "--budget", "silence=0"]) == 0
    assert budgets.budget("repo-a", budgets.SILENCE).source == budgets.DEFAULT
    assert cli.main(["repo", "set", "repo-a", "--budget", "naps=5"]) == 1
    assert cli.main(["repo", "budgets", "nope"]) == 1


def test_readiness_warns_when_a_repos_gate_budget_exceeds_the_tool_cap(
    ppy_home, monkeypatch
) -> None:
    for check in (
        "_harness_problems",
        "_papaya_problems",
        "_config_problems",
        "_client_problems",
        "_repo_problems",
        "_gate_tool_problems",
    ):
        monkeypatch.setattr(readiness, check, lambda problems: None)
    _repo("quick")
    _repo("slow")
    for seconds in (300, 300, 300):
        budgets.observe("quick", budgets.GATE, seconds)  # 450s, floored at 600: not over
    assert readiness.check().problems == []

    for seconds in (500, 500, 500):
        budgets.observe("slow", budgets.GATE, seconds)  # 750s
    (problem,) = readiness.check().problems
    assert problem.code == "gate_budget_over_tool_cap"
    assert problem.blocking is False
    assert "slow local gate 12m (derived)" in problem.summary and "quick" not in problem.summary
    assert "ppy gate run" in problem.fix


# ── the other write points ──────────────────────────────────────────────────


def test_a_worker_session_is_observed_against_its_repo(ppy_home, source_repo) -> None:
    from papaya_agent_runtime.providers.fake import FakeProvider
    from papaya_agent_runtime.supervisor.runner import RunnerGuardian
    from test_runner import _dispatch_spec

    spec, _run_id, task_id = _dispatch_spec(source_repo)
    RunnerGuardian(FakeProvider()).run(spec)

    (session,) = _observations(budgets.WORKER_SESSION)
    assert session["task_id"] == task_id and session["outcome"] == "completed"
    conn = init_db()
    try:
        assert session["repo"] == budgets.repo_of_task(conn, task_id)
    finally:
        conn.close()
    assert session["repo"] and session["seconds"] >= 0


def test_ci_wall_time_is_read_from_the_checks_and_kept_once(ppy_home) -> None:
    from papaya_agent_runtime import watch

    checks = [
        {"name": "lint", "bucket": "pass", "startedAt": "2026-09-16T10:00:00Z",
         "completedAt": "2026-09-16T10:02:00Z"},
        {"name": "test", "bucket": "fail", "startedAt": "2026-09-16T10:00:30Z",
         "completedAt": "2026-09-16T10:12:00Z"},
        {"name": "skipped", "bucket": "skipping", "startedAt": "0001-01-01T00:00:00Z",
         "completedAt": "0001-01-01T00:00:00Z"},
    ]  # fmt: skip
    assert watch.ci_wall_seconds(checks) == 720
    task_id = _task(_repo("web"))
    rounds.observe_ci(task_id, 720, "fail")
    rounds.observe_ci(task_id, 720, "fail")
    (ci,) = _observations(budgets.CI)
    assert (ci["repo"], ci["seconds"], ci["outcome"]) == ("web", 720, "fail")


def test_a_push_through_a_full_suite_hook_is_a_full_suite_observation(
    tmp_path, ppy_home, monkeypatch
) -> None:
    import subprocess

    from papaya_agent_runtime import lifecycle, turn_end

    worktree = Path(make_git_repo(tmp_path / "wt"))
    repo_id = _repo("backend", worktree)
    repos.set_settings("backend", push_hook_runs_full_suite="yes")
    task_id = _task(repo_id, worktree_path=str(worktree), branch="task-1")
    monkeypatch.setattr(lifecycle, "require_live_lease", lambda conn, task_id: None)
    real_run = subprocess.run

    def run(argv, *args, **kwargs):
        if "push" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "hook failed")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(turn_end.subprocess, "run", run)
    conn = init_db()
    try:
        turn_end.push_lease_branch(conn, task_id)
    finally:
        conn.close()
    (hook,) = _observations(budgets.FULL_SUITE)
    assert (hook["repo"], hook["task_id"], hook["outcome"]) == ("backend", task_id, "fail")


def test_an_override_is_parsed_and_refused_when_malformed() -> None:
    assert budgets.parse_override("gate=900") == ("gate", 900.0)
    assert budgets.parse_override("gate=0") == ("gate", None)
    for bad in ("gate", "gate=soon", "nap=5", "gate=-1"):
        with pytest.raises(budgets.BudgetError):
            budgets.parse_override(bad)


def test_observe_never_raises_and_ignores_what_it_cannot_keep(ppy_home) -> None:
    assert budgets.observe(None, budgets.GATE, 5) is False
    assert budgets.observe("a", "nap", 5) is False
    assert budgets.observe("a", budgets.GATE, float("nan")) is False
    assert budgets.observe("a", budgets.GATE, -1) is False
    assert budgets.observe("a", budgets.GATE, 5) is True
    assert [o["repo"] for o in _observations()] == ["a"]
