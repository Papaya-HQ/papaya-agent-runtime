"""`ppy serve`'s rounds: every worker and every loose end, looked at on a clock.

The rounds run inside the real `serve.run`, beside the client's own loop, exactly
as `tests/test_serve.py` drives it. What is faked is the outside world and time:
the harness (:class:`FakeTurns`), Papaya (:class:`FakePapaya`, :class:`FakeEvents`),
the forge, the worktree pruner, and two clocks — the rounds' timer, fired by hand,
and the wall clock a round reads silence and age on, moved by hand.
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
import os
import sqlite3
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import test_serve
from conftest import make_git_repo, scale
from papaya_agent_runtime import (
    gate,
    papaya_events,
    preflight,
    progress,
    prompts,
    reconcile,
    repos,
    rounds,
    serve,
)
from papaya_agent_runtime.state import store
from papaya_agent_runtime.state.db import init_db
from test_serve import (
    CONNECTION_ID,
    EVENT,
    SUBJECT,
    FakeEvents,
    FakePapaya,
    FakeTurns,
    Harness,
    Turn,
    _runner,
    _until,
    dispatch_worker,
    history,
    ticket_task,
    worker_event,
    workers_in,
)
from test_worktree_reclaim import _lease_a_task

# `serve`'s own fixtures, registered here under their names. Bound through `globals()`
# rather than imported, because a test parameter named like an import reads as a
# redefinition to the linter.
globals().update(
    {
        name: getattr(test_serve, name)
        for name in ("assigned", "client_home", "progress_lines", "ready", "registered_repo")
    }
)

GOALS = "- Add the /things endpoint, and nothing else."


# ── time, by hand ───────────────────────────────────────────────────────────


class Timer:
    """The rounds' interval sleep, fired on demand."""

    def __init__(self) -> None:
        self._go: asyncio.Queue[None] | None = None
        self.waiting = 0

    async def sleep(self, _interval: float) -> None:
        if self._go is None:
            self._go = asyncio.Queue()
        self.waiting += 1
        await self._go.get()

    async def round(self) -> None:
        """Fire one round and wait until it has finished and gone back to sleep."""
        await _until(lambda: self._go is not None, what="the rounds timer")
        before = self.waiting
        assert self._go is not None
        self._go.put_nowait(None)
        await _until(lambda: self.waiting > before, what="the round to finish")


class WallClock:
    """The wall clock a round measures silence and age on."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


# ── the world ───────────────────────────────────────────────────────────────


@pytest.fixture
def pruned() -> list[int | None]:
    return []


def _seams(
    timer: Timer,
    clock: WallClock,
    pruned: list[int | None],
    *,
    forge: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    def prune(task_id: int | None) -> dict[str, Any]:
        pruned.append(task_id)
        return {"removed": [], "skipped": [], "reclaimed_bytes": 0}

    return {
        "sleep": timer.sleep,
        "clock": clock,
        "forge": lambda _conn: [dict(entry) for entry in (forge or [])],
        "prune": prune,
        "git": lambda *_args, **_kwargs: 0,
        "branch_ahead": lambda _task_id: True,
        "gate": lambda _task_id: rounds.GateState(False, "no gate result recorded for this worker"),
        "gate_verdict": lambda _task_id: gate.Verdict(gate.NONE),
        "pushed": lambda _task_id: rounds.PushState("abc123", None, False),
    }


def _serve(
    harness: Harness,
    client_home: Any,
    runner: serve.TicketRunner,
    seams: dict[str, Any],
    stderr: io.StringIO | None = None,
) -> asyncio.Task[int]:
    async def never(_interval: float) -> None:
        await asyncio.Event().wait()

    return asyncio.create_task(
        serve.run(
            serve.parse_args(["--working-directory", str(client_home.work_dir)]),
            stdout=io.StringIO(),
            stderr=stderr or io.StringIO(),
            extra=harness.extra(),
            runner=runner,
            sweep_sleep=never,
            rounds_seams=seams,
        )
    )


def live_session(task_id: int) -> None:
    """A runner process for the worker that is alive: this test's own pid."""
    conn = init_db()
    try:
        store.register_runner(
            conn, runner_id=f"runner-{task_id}", task_id=task_id, provider="claude", pid=os.getpid()
        )
    finally:
        conn.close()


def working_worker(run_id: int, *, note: str, phase: str = "implement", live: bool = True) -> int:
    """What a brief turn leaves: a dispatched worker with a brief, a progress note, a session."""
    worker = dispatch_worker(run_id)
    preflight.archive_brief(
        "runtime",
        worker,
        f"# Task\n\n## Goals\n\n{GOALS}\n\n## Intent\n\nA person asked.\n\n"
        "## In scope\n\nThe endpoint.\n\n## Out of scope\n\nFlags.\n",
    )
    progress.record(worker, phase=phase, note=note)
    if live:
        live_session(worker)
    return worker


def checkins() -> list[dict[str, Any]]:
    conn = init_db()
    try:
        rows = conn.execute(
            "SELECT payload FROM events WHERE kind = ? ORDER BY id", (serve.CHECKIN_EVENT,)
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]
    finally:
        conn.close()


def events_of(task_id: int, kind: str) -> list[dict[str, Any]]:
    conn = init_db()
    try:
        rows = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]
    finally:
        conn.close()


async def _dispatched(timer: Timer) -> int:
    await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
    await _until(lambda: timer.waiting, what="the rounds to go to sleep")
    (worker,) = workers_in(int(ticket_task()["run_id"]))
    return worker


# ── stuck workers ───────────────────────────────────────────────────────────


def test_a_quiet_worker_gets_one_checkin_and_its_steer_line_becomes_the_steer(
    ppy_home, client_home, ready, registered_repo, assigned, pruned
) -> None:
    steer_text = "Drop the feature flag; the brief asks for the endpoint only."
    steers: list[tuple[int, str]] = []

    def act(turn: Turn) -> str | None:
        if turn.name == prompts.BRIEF:
            working_worker(turn.run_id, note="Adding the endpoint and a feature flag.")
        elif turn.name == prompts.CHECKIN:
            return f"The flag is not in the brief.\nCHECK-IN: steer {steer_text}"
        return None

    turns, timer, clock = FakeTurns(act), Timer(), WallClock()
    harness = Harness(FakeEvents([EVENT]))
    runner = _runner(turns, FakePapaya(), steer=lambda t, m: steers.append((t, m)))

    async def scenario() -> int:
        task = _serve(harness, client_home, runner, _seams(timer, clock, pruned))
        worker = await _dispatched(timer)
        clock.advance(minutes=21)
        await timer.round()
        await _until(lambda: steers, what="the check-in's steer")
        clock.advance(minutes=5)
        await timer.round()
        await asyncio.sleep(scale(0.2))
        harness.loop.request_stop()
        assert await task == 0
        return worker

    worker = asyncio.run(scenario())

    assert turns.names() == [prompts.BRIEF, prompts.CHECKIN]
    prompt = turns.calls[1].prompt
    assert GOALS in prompt
    assert "[implement] Adding the endpoint and a feature flag." in prompt
    assert "m with its session still alive" in prompt and "- worker session: alive" in prompt
    assert steers == [(worker, steer_text)]
    (record,) = checkins()
    assert record["decision"] == prompts.CHECKIN_STEER
    assert record["message"] == steer_text
    assert "quiet" in record["trigger"]


def test_a_continue_line_records_the_check_and_nothing_else(
    ppy_home, client_home, ready, registered_repo, assigned, pruned, progress_lines
) -> None:
    def act(turn: Turn) -> str | None:
        if turn.name == prompts.BRIEF:
            working_worker(turn.run_id, note="Writing the endpoint.")
        elif turn.name == prompts.CHECKIN:
            return "It is on course.\nCHECK-IN: continue"
        return None

    turns, timer, clock, papaya_api = FakeTurns(act), Timer(), WallClock(), FakePapaya()
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        task = _serve(
            harness, client_home, _runner(turns, papaya_api), _seams(timer, clock, pruned)
        )
        await _dispatched(timer)
        # The dispatch comment is posted just after the phase is recorded.
        await _until(
            lambda: any(body.startswith("Dispatched") for _i, body in papaya_api.comments()),
            what="the dispatch comment",
        )
        comments = len(papaya_api.comments())
        clock.advance(minutes=16)
        await timer.round()
        await _until(lambda: checkins(), what="the check-in record")
        await asyncio.sleep(scale(0.2))
        assert len(papaya_api.comments()) == comments
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0
    assert turns.names() == [prompts.BRIEF, prompts.CHECKIN]
    (record,) = checkins()
    assert (record["decision"], record["message"], record["error"]) == ("continue", "", "")
    assert not any("Checked in on" in detail for _s, _p, detail in progress_lines)


class FakeRemote:
    """The worker's remote lease branch: empty until a test pushes to it."""

    def __init__(self) -> None:
        self.tips: dict[int, str] = {}

    def push(self, task_id: int, sha: str) -> None:
        self.tips[task_id] = sha

    def __call__(self, task_id: int) -> rounds.PushState:
        tip = self.tips.get(task_id)
        # Pushed means the remote tip is the worker's head: nothing left to push.
        return rounds.PushState(tip, None, tip is None)


@pytest.mark.parametrize("pushed", [False, True], ids=["nothing-pushed", "pushed"])
def test_a_long_session_with_nothing_on_the_remote_gets_a_push_checkin_and_a_pushed_one_none(
    ppy_home, client_home, ready, registered_repo, assigned, pruned, pushed
) -> None:
    """PAP-219: 123 minutes, twelve files in the worktree, and no branch on the remote."""
    remote = FakeRemote()

    def act(turn: Turn) -> str | None:
        if turn.name == prompts.BRIEF:
            worker = working_worker(turn.run_id, note="Implementing goal 1.")
            if pushed:
                remote.push(worker, "abc123")
        elif turn.name == prompts.CHECKIN:
            return "CHECK-IN: continue"
        return None

    turns, timer, clock = FakeTurns(act), Timer(), WallClock()
    harness = Harness(FakeEvents([EVENT]))
    seams = {**_seams(timer, clock, pruned), "pushed": remote}

    def push_rounds() -> list[dict[str, Any]]:
        return [
            p
            for p in events_of(int(ticket_task()["id"]), rounds.ROUND_EVENT)
            if p.get("trigger") == "push"
        ]

    async def scenario() -> None:
        task = _serve(harness, client_home, _runner(turns, FakePapaya()), seams)
        await _dispatched(timer)
        clock.advance(minutes=50)
        await timer.round()
        await _until(lambda: len(checkins()) == 1, what="the 50-minute check-in")
        # Twenty more minutes with nothing pushed is not another `push_by_minutes`.
        clock.advance(minutes=20)
        await timer.round()
        await asyncio.sleep(scale(0.2))
        assert len(push_rounds()) == (0 if pushed else 1)
        # Past another 45 minutes since the last push check-in, it is said again.
        clock.advance(minutes=26)
        await timer.round()
        if not pushed:
            await _until(lambda: len(checkins()) == 2, what="the repeated push check-in")
        await asyncio.sleep(scale(0.2))
        harness.loop.request_stop()
        assert await task == 0

    asyncio.run(scenario())

    records = push_rounds()
    if pushed:
        assert records == []
        assert all("push" not in c["trigger"] for c in checkins())
        return
    first, second = records
    assert first["reason"] in ("nothing pushed in 49 minutes", "nothing pushed in 50 minutes")
    assert first["remote_sha"] is None
    assert second["reason"].startswith("nothing pushed in 9")
    push_checkins = [c for c in checkins() if "push" in c["trigger"].split(",")]
    assert len(push_checkins) == 2
    assert "nothing pushed in" in push_checkins[0]["reason"]
    assert "commit what is green and push" in turns.calls[1].prompt


def _clone_beside_a_forge(tmp_path: Path) -> tuple[Path, Path, Path, Any]:
    """A clone whose `origin` is not the forge, the #46 shape.

    The forge is a bare repository the clone reaches through a second remote, `forge`;
    its `origin` is a stale copy that is never pushed to. Returns the forge, the stale
    origin, the clone and a `git` helper that runs in the clone.
    """
    forge, stale = tmp_path / "forge.git", tmp_path / "stale.git"
    for bare in (forge, stale):
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    worktree = Path(make_git_repo(tmp_path / "wt"))

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(worktree), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("remote", "add", "origin", str(stale))
    git("remote", "add", "forge", str(forge))
    return forge, stale, worktree, git


def _forge_and_worktree(tmp_path: Path, *, forge_url: bool = True) -> tuple[int, Path, Any]:
    """A worker task in a clone beside a forge registered as its repo's `forge_url`."""
    forge, stale, worktree, git = _clone_beside_a_forge(tmp_path)
    conn = init_db()
    try:
        repo_id = store.add_repo(
            conn,
            name="pushed",
            origin=str(stale),
            local_path=str(worktree),
            default_branch="main",
            base_sha=None,
            forge_url=str(forge) if forge_url else None,
        )
        run_id = store.create_run(conn, "push")
        task_id = store.add_task(conn, run_id=run_id, title="worker", repo_id=repo_id)
        store.update_task_fields(conn, task_id, branch="ppy/task-1", worktree_path=str(worktree))
    finally:
        conn.close()
    return task_id, worktree, git


@pytest.mark.parametrize("head", ["on-the-forge", "ahead-of-the-forge"])
def test_a_worker_whose_head_is_on_the_forge_is_never_nudged_to_push(
    ppy_home, client_home, ready, registered_repo, assigned, pruned, tmp_path, head
) -> None:
    """#46: fifty minutes with no progress note, and a HEAD the forge already has.

    The clone's `origin` is not the forge (the backend's was rewritten by hand), so
    the worktree's `refs/remotes/origin/<branch>` never moves; the rounds read the
    forge itself. A HEAD one commit past the forge's tip is still nudged.
    """
    forge, _stale, worktree, git = _clone_beside_a_forge(tmp_path)
    git("push", "-q", "forge", "HEAD:ppy/task-1")
    pushed_sha = git("rev-parse", "HEAD")
    if head == "ahead-of-the-forge":
        (worktree / "goal.py").write_text("x = 1\n")
        git("add", "-A")
        git("commit", "-qm", "goal 1, not pushed")
    head_sha = git("rev-parse", "HEAD")

    def act(turn: Turn) -> str | None:
        if turn.name == prompts.BRIEF:
            worker = working_worker(turn.run_id, note="Implementing goal 1.")
            conn = init_db()
            try:
                store.update_repo_fields(conn, "runtime", forge_url=str(forge))
                store.update_task_fields(
                    conn, worker, branch="ppy/task-1", worktree_path=str(worktree)
                )
            finally:
                conn.close()
        elif turn.name == prompts.CHECKIN:
            return "CHECK-IN: continue"
        return None

    turns, timer, clock = FakeTurns(act), Timer(), WallClock()
    harness = Harness(FakeEvents([EVENT]))
    seams = {**_seams(timer, clock, pruned), "pushed": rounds.push_state}

    def seen() -> list[dict[str, Any]]:
        return [
            p
            for p in events_of(int(ticket_task()["id"]), rounds.ROUND_EVENT)
            if p["action"] == "push_seen"
        ]

    async def scenario() -> None:
        task = _serve(harness, client_home, _runner(turns, FakePapaya()), seams)
        await _dispatched(timer)
        # A round sees the forge's tip; the next is fifty minutes on, with no progress
        # note in between, so a quiet check-in is due whatever the push trigger says.
        for _ in range(10):
            await timer.round()
            if seen():
                break
        clock.advance(minutes=50)
        await timer.round()
        await _until(lambda: checkins(), what="the fifty-minute check-in")
        await asyncio.sleep(scale(0.2))
        harness.loop.request_stop()
        assert await task == 0

    asyncio.run(scenario())

    (record,) = checkins()
    triggers = record["trigger"].split(",")
    if head == "on-the-forge":
        assert "push" not in triggers
        assert "nothing pushed" not in record["reason"]
        assert "remote_sha" not in record
    ticket = int(ticket_task()["id"])
    (first_seen,) = seen()
    assert first_seen["remote_sha"] == pushed_sha
    if head == "on-the-forge":
        return
    assert "push" in triggers
    assert record["remote_sha"] == pushed_sha
    assert record["head_sha"] == head_sha
    assert record["last_push_at"] == first_seen["at"]
    prompt = turns.calls[1].prompt
    assert f"the lease branch's tip on the forge (read now): {pushed_sha}" in prompt
    assert f"the worktree's HEAD: {head_sha}" in prompt
    assert first_seen["at"] in prompt
    (round_record,) = [
        p for p in events_of(ticket, rounds.ROUND_EVENT) if p.get("trigger") == "push"
    ]
    assert (round_record["remote_sha"], round_record["head_sha"]) == (pushed_sha, head_sha)


def test_the_push_clock_runs_from_when_the_push_was_seen_not_from_the_commit_date() -> None:
    """#46's cause: the tip was committed 47 minutes before the check, pushed ten minutes after.

    The rounds used the tip's commit date as the last push. The last push is when a
    round first saw that tip on the forge.
    """
    start = datetime(2026, 9, 16, 23, 7, tzinfo=UTC)
    look = rounds.WorkerLook(
        task_id=18,
        status="in_progress",
        branch="ppy/task-18",
        created_at=start,
        verdict="alive",
        silent_seconds=0,
        last_event_id=10638,
        progress=[],
        question=None,
        stopped=None,
        last_acted_id=0,
    )
    seen_at = datetime(2026, 9, 16, 23, 55, tzinfo=UTC)
    seen = {"worker_task_id": 18, "remote_sha": "fc0cbdc", "at": seen_at.isoformat()}
    records = [(1, {"action": rounds.PUSH_SEEN, **seen})]
    ahead = rounds.PushState("fc0cbdc", "0e0b511", True)
    waits = rounds.WorkerBudgets(10**6, 10**6, 10**6)

    def push_due(now: datetime) -> list[str]:
        due = rounds.Rounds._checkins_due(look, now, records, waits, ahead)
        return [why for trigger, why in due if trigger == "push"]

    assert push_due(datetime(2026, 9, 17, 0, 32, tzinfo=UTC)) == []
    assert push_due(seen_at + timedelta(minutes=45)) == ["nothing pushed in 45 minutes"]
    on_forge = rounds.PushState("fc0cbdc", "fc0cbdc", False)
    later = seen_at + timedelta(hours=3)
    assert rounds.Rounds._checkins_due(look, later, records, waits, on_forge) == []


def test_push_state_asks_the_forge_and_compares_with_head(ppy_home, tmp_path) -> None:
    task_id, worktree, git = _forge_and_worktree(tmp_path)
    head = git("rev-parse", "HEAD")

    assert rounds.push_state(task_id) == rounds.PushState(None, head, True)
    git("push", "-q", "forge", "HEAD:ppy/task-1")
    # `origin` never saw the push, and its remote-tracking ref does not exist: the
    # forge is what is asked.
    assert rounds.push_state(task_id) == rounds.PushState(head, head, False)
    # Uncommitted files are not a push to nag about.
    (worktree / "new.py").write_text("x = 1\n")
    assert not rounds.push_state(task_id).unpushed
    git("add", "-A")
    git("commit", "-qm", "goal 1")
    ahead = git("rev-parse", "HEAD")
    assert rounds.push_state(task_id) == rounds.PushState(head, ahead, True)
    git("push", "-q", "forge", "HEAD:ppy/task-1")
    # A HEAD behind the forge's tip is on the forge too.
    git("reset", "-q", "--hard", "HEAD~1")
    assert rounds.push_state(task_id) == rounds.PushState(ahead, head, False)


def test_push_state_without_a_forge_url_asks_origin_and_an_unreachable_forge_is_unknown(
    ppy_home, tmp_path
) -> None:
    task_id, _worktree, git = _forge_and_worktree(tmp_path, forge_url=False)
    head = git("rev-parse", "HEAD")
    git("push", "-q", "origin", "HEAD:ppy/task-1")
    assert rounds.push_state(task_id) == rounds.PushState(head, head, False)
    git("remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert rounds.push_state(task_id) is None


def test_a_dead_session_with_no_done_note_takes_the_worker_stopped_path_in_one_round(
    ppy_home, client_home, ready, registered_repo, assigned, pruned
) -> None:
    steers: list[tuple[int, str]] = []
    turns = FakeTurns(
        lambda turn: (
            working_worker(turn.run_id, note="Half done.", live=False)
            if turn.name == prompts.BRIEF
            else None
        )
    )
    timer, clock = Timer(), WallClock()
    harness = Harness(FakeEvents([EVENT]))
    runner = _runner(turns, FakePapaya(), steer=lambda t, m: steers.append((t, m)))

    async def scenario() -> int:
        task = _serve(harness, client_home, runner, _seams(timer, clock, pruned))
        worker = await _dispatched(timer)
        clock.advance(minutes=3)
        await timer.round()
        await _until(lambda: steers, what="the stopped worker to be sent back")
        harness.loop.request_stop()
        assert await task == 0
        return worker

    worker = asyncio.run(scenario())
    (stopped,) = events_of(worker, serve.WORKER_STOPPED)
    assert stopped["source"] == "rounds"
    assert "no runner process is left" in stopped["summary"]
    ((steered, message),) = steers
    assert steered == worker
    assert f"`ppy gate run --task {worker}`" in message
    assert turns.names() == [prompts.BRIEF]


def test_a_worker_whose_gate_runs_under_the_supervisor_is_not_nudged(
    ppy_home, client_home, ready, registered_repo, assigned, pruned
) -> None:
    turns = FakeTurns(
        lambda turn: (
            working_worker(turn.run_id, note="Running the full suite.")
            if turn.name == prompts.BRIEF
            else None
        )
    )
    timer, clock = Timer(), WallClock()
    harness = Harness(FakeEvents([EVENT]))
    seams = {
        **_seams(timer, clock, pruned),
        "gate": lambda _id: rounds.GateState(True, "running under the supervisor for 30m"),
    }

    async def scenario() -> int:
        task = _serve(harness, client_home, _runner(turns, FakePapaya()), seams)
        await _dispatched(timer)
        clock.advance(minutes=30)
        await timer.round()
        await asyncio.sleep(scale(0.2))
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0
    assert turns.names() == [prompts.BRIEF]
    assert checkins() == []


class SessionSupervisor:
    """The supervisor `ppy steer` reaches from a session: it records the steer, as the real one."""

    def steer_task(
        self, task_id: int, message: str, delivery: str = "append", by: str | None = None
    ) -> dict[str, Any]:
        conn = init_db()
        try:
            task = store.get_task(conn, task_id)
            store.append_event(
                conn,
                kind="steer",
                payload={
                    "task_id": task_id,
                    "mode": "checkpoint_pending",
                    "message": message,
                    "delivery": delivery,
                    "by": by,
                },
                run_id=task["run_id"],
                task_id=task_id,
            )
        finally:
            conn.close()
        return {"ok": True, "mode": "checkpoint_pending", "queue": []}


def test_a_person_steer_from_a_session_is_not_undone_by_the_next_round(
    ppy_home, client_home, ready, registered_repo, assigned, pruned, monkeypatch
) -> None:
    from papaya_agent_runtime import cli
    from papaya_agent_runtime.supervisor import client as supervisor_client

    person = "Use the existing things table; do not add a migration."
    monkeypatch.setattr(supervisor_client, "SupervisorClient", SessionSupervisor)
    monkeypatch.delenv(papaya_events.TICKET_RUN_ENV, raising=False)
    runtime_steers: list[tuple[int, str]] = []

    def act(turn: Turn) -> str | None:
        if turn.name == prompts.BRIEF:
            working_worker(
                turn.run_id, note="Deciding between a new table and a migration.", phase="plan"
            )
        elif turn.name == prompts.CHECKIN:
            return "The person's direction stands and the worker has it.\nCHECK-IN: continue"
        return None

    turns, timer, clock = FakeTurns(act), Timer(), WallClock()
    harness = Harness(FakeEvents([EVENT]))
    runner = _runner(turns, FakePapaya(), steer=lambda t, m: runtime_steers.append((t, m)))

    async def scenario() -> int:
        task = _serve(harness, client_home, runner, _seams(timer, clock, pruned))
        worker = await _dispatched(timer)
        # Planning past its budget: the round is due to check in on it. A person at a
        # session steers it first, through `ppy steer`, while the daemon runs.
        clock.advance(minutes=11)
        assert await asyncio.to_thread(cli.main, ["steer", str(worker), "--message", person]) == 0
        await timer.round()
        await asyncio.sleep(scale(0.2))
        assert checkins() == [] and turns.names() == [prompts.BRIEF]
        # The round still notes the forge's tip (`push_seen`); it acts on nothing.
        acted = events_of(int(ticket_task()["id"]), rounds.ROUND_EVENT)
        assert [p for p in acted if p["action"] != rounds.PUSH_SEEN] == []
        # A silence budget later with nothing from the worker, the round checks in, and
        # the check-in turn is told what the person said.
        clock.advance(minutes=6)
        await timer.round()
        await _until(lambda: checkins(), what="the check-in after the person's steer")
        harness.loop.request_stop()
        assert await task == 0
        return worker

    worker = asyncio.run(scenario())

    (steer,) = events_of(worker, "steer")
    assert (steer["by"], steer["message"]) == ("person", person)
    assert turns.names() == [prompts.BRIEF, prompts.CHECKIN]
    prompt = turns.calls[1].prompt
    assert "by: person" in prompt and person in prompt
    (record,) = checkins()
    assert record["decision"] == prompts.CHECKIN_CONTINUE
    assert runtime_steers == []
    assert [s for s in events_of(worker, "steer") if s.get("by") != "person"] == []


# ── blocked workers ─────────────────────────────────────────────────────────


def test_a_worker_asking_a_question_gets_the_answer_turn_next_round(
    ppy_home, client_home, ready, registered_repo, assigned, pruned
) -> None:
    question = "Should the endpoint be v1 or v2?"

    def act(turn: Turn) -> None:
        if turn.name == prompts.BRIEF:
            working_worker(turn.run_id, note=question)
        elif turn.name == prompts.ANSWER:
            (worker,) = workers_in(turn.run_id)
            worker_event(worker, "answer", answer="v2")

    turns, timer, clock = FakeTurns(act), Timer(), WallClock()
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        task = _serve(
            harness, client_home, _runner(turns, FakePapaya()), _seams(timer, clock, pruned)
        )
        await _dispatched(timer)
        assert turns.names() == [prompts.BRIEF], "a progress note alone started a turn"
        clock.advance(minutes=1)
        await timer.round()
        await _until(lambda: prompts.ANSWER in turns.names(), what="the answer turn")
        clock.advance(minutes=1)
        await timer.round()
        await asyncio.sleep(scale(0.2))
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0
    assert turns.names() == [prompts.BRIEF, prompts.ANSWER]
    assert question in turns.calls[1].prompt


def test_a_question_waiting_fifteen_minutes_on_a_person_is_said_once_and_blocks_the_ticket(
    ppy_home, client_home, ready, registered_repo, assigned, pruned
) -> None:
    question = "Should the endpoint be public?"

    def act(turn: Turn) -> str | None:
        if turn.name == prompts.BRIEF:
            working_worker(turn.run_id, note="Wiring the endpoint.")
            conn = init_db()
            try:
                ticket = int(ticket_task()["id"])
                store.add_todo(conn, question, task_id=ticket, blocked_on="user")
            finally:
                conn.close()
        return "CHECK-IN: continue" if turn.name == prompts.CHECKIN else None

    turns, timer, clock, papaya_api = FakeTurns(act), Timer(), WallClock(), FakePapaya()
    harness = Harness(FakeEvents([EVENT]))

    def waiting() -> list[str]:
        return [body for _item, body in papaya_api.comments() if body.startswith("waiting on you")]

    async def scenario() -> int:
        task = _serve(
            harness, client_home, _runner(turns, papaya_api), _seams(timer, clock, pruned)
        )
        await _dispatched(timer)
        clock.advance(minutes=14)
        await timer.round()
        assert waiting() == [], "said before fifteen minutes"
        clock.advance(minutes=2)
        await timer.round()
        await _until(lambda: waiting(), what="the waiting-on-you comment")
        clock.advance(minutes=5)
        await timer.round()
        await asyncio.sleep(scale(0.2))
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0
    assert waiting() == [f"waiting on you: {question}"]
    assert serve.PHASE_BLOCKED in history()
    assert ("item-9", papaya_events.STATUS_BLOCKED) in papaya_api.statuses()


# ── reclaim on restart ──────────────────────────────────────────────────────


def seed_ticket(phases: list[str], *, worker_status: str = "in_progress") -> tuple[int, int, int]:
    """What an earlier `serve` left: a ticket task at ``phases`` and its worker."""
    conn = init_db()
    try:
        run_id = store.create_run(conn, "Fix the thing")
        ticket = store.add_task(conn, run_id=run_id, title="Fix the thing")
        event = papaya_events.PapayaEvent(
            id="77", kind="work_item.assigned", subject=SUBJECT, payload={}, work_item_id="item-9"
        )
        papaya_events.record_task(conn, ticket, event)
        for phase in phases:
            serve.record_phase(conn, ticket, phase)
    finally:
        conn.close()
    worker = dispatch_worker(run_id)
    worker_event(worker, "worker_progress", status=worker_status, phase="implement", note="Going.")
    return ticket, run_id, worker


def _ticket_tasks() -> int:
    conn = init_db()
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM task_env WHERE key = ?",
                (papaya_events.PAPAYA_EVENT_METADATA,),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def test_a_restart_re_reserves_a_dispatched_ticket_and_watches_its_live_worker(
    ppy_home, client_home, ready, registered_repo, assigned, pruned, progress_lines
) -> None:
    ticket, run_id, worker = seed_ticket(
        [serve.PHASE_PICKED_UP, serve.PHASE_BRIEFING, serve.PHASE_DISPATCHED]
    )
    live_session(worker)
    turns, timer, clock = FakeTurns(), Timer(), WallClock()
    harness = Harness(FakeEvents([]))
    runner = _runner(turns, FakePapaya())
    # The hold starts on a thread after the offer, late on a loaded machine (CI run
    # 35129973517). Hold it back until the worker has reported, so the report always
    # lands in that window: it is news, not history.
    reported = threading.Event()
    take = runner.take

    def slow_take(job: Any) -> Any:
        assert reported.wait(scale(5.0)), "the test never let the hold start"
        return take(job)

    runner.take = slow_take  # type: ignore[method-assign]

    async def scenario() -> int:
        task = _serve(harness, client_home, runner, _seams(timer, clock, pruned))
        await _until(lambda: harness.jobs, what="the reclaimed hold")
        progress.record(worker, phase="test", note="Suite running.")
        reported.set()
        await _until(
            lambda: any("Suite running." in d for _s, _p, d in progress_lines),
            what="the worker's progress to be relayed",
        )
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0
    session = serve.stored_session_ids()[CONNECTION_ID]
    assert harness.events.reserves[0] == (SUBJECT, session)
    assert turns.names() == []
    assert workers_in(run_id) == [worker]
    assert _ticket_tasks() == 1
    assert (SUBJECT, serve.PHASE_DISPATCHED, f"Resuming task {ticket} from dispatched.") in (
        progress_lines
    )


def test_a_restart_closes_a_ticket_someone_else_now_holds_as_handed_over(
    ppy_home, client_home, ready, registered_repo, assigned, pruned
) -> None:
    ticket, _run_id, worker = seed_ticket(
        [serve.PHASE_PICKED_UP, serve.PHASE_BRIEFING, serve.PHASE_DISPATCHED]
    )
    turns, timer, clock, papaya_api = FakeTurns(), Timer(), WallClock(), FakePapaya()
    harness = Harness(FakeEvents([]))
    harness.events.held.add(SUBJECT)

    async def scenario() -> int:
        task = _serve(
            harness, client_home, _runner(turns, papaya_api), _seams(timer, clock, pruned)
        )
        await _until(
            lambda: store.task_phase(init_db(), ticket) == serve.PHASE_HANDED_OVER,
            what="the ticket to be handed over",
        )
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0
    assert harness.jobs == []
    assert papaya_api.calls == [], "a hand-over posts nothing"
    assert store.get_task(init_db(), worker)["branch"] == f"ppy/task-{worker}"


# ── missed updates from outside ─────────────────────────────────────────────

PR_URL = "https://github.com/acme/runtime/pull/7"


def delivered_ticket() -> tuple[int, int]:
    ticket, _run_id, worker = seed_ticket(
        [
            serve.PHASE_PICKED_UP,
            serve.PHASE_BRIEFING,
            serve.PHASE_DISPATCHED,
            serve.PHASE_REVIEWING,
            serve.PHASE_DELIVERING,
            serve.PHASE_REPORTED,
            serve.PHASE_RELEASED,
        ]
    )
    worker_event(worker, "delivered", status="delivered", pr_url=PR_URL)
    return ticket, worker


def _pr(worker: int, **fields: Any) -> dict[str, Any]:
    return {
        "known": True,
        "task_id": worker,
        "status": "delivered",
        "branch": f"ppy/task-{worker}",
        "pr": 7,
        "url": PR_URL,
        "state": "OPEN",
        "ci": "pass",
        "failing": [],
        "merged": False,
        "review": "",
        **fields,
    }


def test_a_delivered_ticket_whose_pr_turns_red_is_steered_with_the_failure(
    ppy_home, client_home, ready, registered_repo, assigned, pruned, progress_lines
) -> None:
    ticket, worker = delivered_ticket()
    fix = "Fix the failing unit tests on PR #7, push, and file your done note."

    def act(turn: Turn) -> None:
        if turn.name == prompts.REVIEW and "CI failing on PR #7: unit tests" in turn.prompt:
            worker_event(worker, "steer", message=fix)

    turns, timer, clock = FakeTurns(act), Timer(), WallClock()
    harness = Harness(FakeEvents([]))
    forge = [_pr(worker, ci="fail", failing=["unit tests"])]

    async def scenario() -> int:
        task = _serve(
            harness,
            client_home,
            _runner(turns, FakePapaya()),
            _seams(timer, clock, pruned, forge=forge),
        )
        await _until(lambda: timer.waiting, what="the rounds to go to sleep")
        await timer.round()
        await _until(lambda: events_of(worker, "steer"), what="the review turn's steer")
        await timer.round()
        await asyncio.sleep(scale(0.2))
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0
    assert turns.names() == [prompts.REVIEW]
    assert "what stopped the worker" in turns.calls[0].prompt
    assert [e["message"] for e in events_of(worker, "steer")] == [fix]
    assert len(events_of(worker, serve.PR_ATTENTION)) == 1
    assert any(
        d == f"Worker task {worker}'s pull request needs attention: "
        "pr_attention: CI failing on PR #7: unit tests"
        for _s, _p, d in progress_lines
    )
    assert _ticket_tasks() == 1 and ticket_task(77)["id"] == ticket


def test_a_delivered_ticket_whose_pr_merges_is_done_with_one_comment_and_cleaned_up(
    ppy_home, client_home, ready, registered_repo, assigned, pruned
) -> None:
    ticket, worker = delivered_ticket()
    turns, timer, clock, papaya_api = FakeTurns(), Timer(), WallClock(), FakePapaya()
    harness = Harness(FakeEvents([]))
    forge = [_pr(worker, state="MERGED", merged=True)]

    async def scenario() -> int:
        runner = _runner(turns, papaya_api)
        task = _serve(harness, client_home, runner, _seams(timer, clock, pruned, forge=forge))
        await _until(lambda: timer.waiting, what="the rounds to go to sleep")
        await timer.round()
        clock.advance(minutes=5)
        await timer.round()
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0
    assert store.task_phase(init_db(), ticket) == serve.PHASE_DONE
    assert papaya_api.comments() == [("item-9", f"Merged: {PR_URL}. Done.")]
    assert papaya_api.statuses() == [("item-9", papaya_events.STATUS_DONE)]
    # Straight away, for that one task, not only on the hourly run.
    assert worker in pruned
    assert harness.jobs == [] and turns.names() == []


# ── a quiet round ───────────────────────────────────────────────────────────


def test_a_quiet_round_writes_no_comment_no_progress_line_and_nothing_on_stderr(
    ppy_home, client_home, ready, registered_repo, assigned, pruned, progress_lines
) -> None:
    turns = FakeTurns(
        lambda turn: (
            working_worker(turn.run_id, note="Endpoint written.")
            if turn.name == prompts.BRIEF
            else None
        )
    )
    timer, clock, papaya_api, stderr = Timer(), WallClock(), FakePapaya(), io.StringIO()
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        task = _serve(
            harness, client_home, _runner(turns, papaya_api), _seams(timer, clock, pruned), stderr
        )
        await _dispatched(timer)
        await _until(
            lambda: any("Endpoint written." in d for _s, _p, d in progress_lines),
            what="the worker's progress",
        )
        await asyncio.sleep(scale(0.1))
        before = (len(papaya_api.comments()), len(progress_lines), stderr.getvalue())
        clock.advance(minutes=1)
        await timer.round()
        await asyncio.sleep(scale(0.2))
        assert (len(papaya_api.comments()), len(progress_lines), stderr.getvalue()) == before
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0
    assert "round:" not in stderr.getvalue()
    assert pruned == [None], "the first round is the hourly hygiene run"


# ── hygiene ─────────────────────────────────────────────────────────────────


@pytest.fixture
def repo(ppy_home, source_repo):
    return repos.add_repo(source_repo)


def _hygiene() -> list[dict[str, Any]]:
    return rounds.hygiene_records()


def _bare_rounds(stderr: io.StringIO, clock: WallClock, **seams: Any) -> rounds.Rounds:
    return rounds.Rounds(
        SimpleNamespace(loop=None, agent_config={}),
        SimpleNamespace(held={}),
        stderr=stderr,
        clock=clock,
        forge=lambda _conn: [],
        **seams,
    )


def test_hygiene_removes_a_clean_delivered_slot_and_keeps_a_dirty_one(repo) -> None:
    clean_id, clean = _lease_a_task(repo, status="delivered")
    dirty_id, dirty = _lease_a_task(repo, status="delivered")
    Path(dirty.worktree_path, "scratch.txt").write_text("uncommitted\n")
    gits: list[tuple[tuple[str, ...], str]] = []
    stderr, clock = io.StringIO(), WallClock()
    walker = _bare_rounds(
        stderr, clock, git=lambda args, cwd, **_kw: gits.append((tuple(args), cwd)) or 0
    )

    asyncio.run(walker.round_once())

    assert not Path(clean.worktree_path).exists()
    assert Path(dirty.worktree_path).exists()
    (record,) = _hygiene()
    ((removed,),) = [record["removed"]]
    assert removed["task_id"] == clean_id and removed["size_bytes"] > 0
    assert record["reclaimed_bytes"] == removed["size_bytes"]
    (kept,) = record["kept"]
    assert kept["task_id"] == dirty_id
    assert kept["reason"] == "uncommitted changes in the worktree"
    base = store.get_repo(init_db(), repo.name)["local_path"]
    assert record["base_clones"] == [str(Path(base).resolve())]
    base = record["base_clones"][0]
    assert (("worktree", "prune"), str(base)) in gits
    assert (("fetch", "--prune", "--quiet"), str(base)) in gits
    assert "removed 1 worktree(s)" in stderr.getvalue()
    # Not a day old yet: no person is asked about the dirty slot.
    assert record["surfaced"] == []


def test_a_kept_dirty_terminal_slot_a_day_old_is_surfaced_to_a_person_once(repo) -> None:
    dirty_id, dirty = _lease_a_task(repo, status="delivered")
    Path(dirty.worktree_path, "scratch.txt").write_text("uncommitted\n")
    conn = init_db()
    stamp = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (stamp, dirty_id))
    conn.commit()
    conn.close()
    stderr, clock = io.StringIO(), WallClock()
    walker = _bare_rounds(stderr, clock, git=lambda *_a, **_k: 0)

    asyncio.run(walker.round_once())
    clock.advance(minutes=61)
    asyncio.run(walker.round_once())

    todos = (
        init_db()
        .execute("SELECT text FROM todos WHERE task_id = ? AND blocked_on = 'user'", (dirty_id,))
        .fetchall()
    )
    assert len(todos) == 1
    assert dirty.worktree_path in todos[0]["text"]
    assert [r["surfaced"] for r in _hygiene()] == [[dirty.worktree_path], []]
    assert stderr.getvalue().count("needs a person") == 1


def test_hygiene_runs_hourly_and_names_a_slot_kept_three_runs_in_a_row(ppy_home) -> None:
    calls: list[int | None] = []

    def prune(task_id: int | None) -> dict[str, Any]:
        calls.append(task_id)
        kept = {"path": "/pool/slot-1", "reason": "uncommitted changes in the worktree"}
        return {"removed": [], "skipped": [dict(kept, task_status="in_progress")]}

    stderr, clock = io.StringIO(), WallClock()
    walker = _bare_rounds(stderr, clock, prune=prune, git=lambda *_a, **_k: 0)

    for minutes in (0, 30, 31, 60, 30):
        clock.advance(minutes=minutes)
        asyncio.run(walker.round_once())

    # Rounds at 0, 30, 61, 121 and 151 minutes: hygiene at 0, 61 and 121.
    assert calls == [None, None, None]
    lines = [line for line in stderr.getvalue().splitlines() if "round:" in line]
    assert lines == [
        "ppy serve: round: kept /pool/slot-1 for the 3rd run in a row: "
        "uncommitted changes in the worktree"
    ]
    assert len(_hygiene()) == 3


# ── the pieces ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("transcript", "decision"),
    [
        ("All fine.\nCHECK-IN: continue", ("continue", "")),
        (
            "**CHECK-IN: steer Commit to the plan in your note.**",
            ("steer", "Commit to the plan in your note."),
        ),
        (
            "CHECK-IN: stop and resume with Revert the flag.",
            ("stop and resume with", "Revert the flag."),
        ),
        ("CHECK-IN: steer", None),
        ("I think it should continue.", None),
    ],
)
def test_the_checkin_decision_is_read_from_its_last_line(transcript, decision) -> None:
    from papaya_agent_runtime.manager.launch import TurnResult

    assert serve.checkin_decision(TurnResult(exit_code=0, transcript=transcript)) == decision


def test_the_rounds_interval_comes_from_the_flag_then_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(rounds.ROUNDS_INTERVAL_ENV, "120")
    assert serve.parse_args([]).rounds_interval == 120.0
    assert serve.parse_args(["--rounds-interval", "0"]).rounds_interval == 0.0
    assert "--rounds-interval" in (
        serve.parse_args(["--rounds-interval", "soon"]).invalid_arguments or ""
    )


def test_the_checkin_prompt_names_its_three_endings_and_both_skills() -> None:
    text = prompts.load(prompts.CHECKIN)
    for ending in ("CHECK-IN: continue", "CHECK-IN: steer <", "CHECK-IN: stop and resume with <"):
        assert ending in text
    assert prompts.BRIEF_SKILL in text and prompts.REVIEW_SKILL in text


# ── a round over every shape of state, on real threads ──────────────────────

#: The modules whose coroutines run the rounds, the reclaim and liveness.
THREADED_MODULES = (rounds, reconcile, serve)
_OPENERS = {"init_db", "connect"}


def _called_name(node: ast.expr) -> str:
    """`db.init_db` -> "init_db", `self._forge_states` -> "_forge_states", `f` -> "f"."""
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _requires_conn(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A parameter that is a connection and has no default: the caller must hand one over.

    An optional one (`conn=None`, the function opens its own) is only a crossing when
    a connection is actually passed, which the argument check catches.
    """
    positional = [*fn.args.posonlyargs, *fn.args.args]
    defaults = [None] * (len(positional) - len(fn.args.defaults)) + list(fn.args.defaults)
    params = [
        *zip(positional, defaults, strict=True),
        *zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=True),
    ]
    return any(
        default is None
        and (p.arg == "conn" or "Connection" in ast.unparse(p.annotation or ast.Constant("")))
        for p, default in params
    )


def _definitions(module: Any) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(Path(module.__file__).read_text())
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def connection_crossings(module: Any, others: tuple[Any, ...] = ()) -> list[str]:
    """Every place a coroutine in ``module`` could use a connection from another thread.

    - a `to_thread` target that opens a connection (it comes back to the loop thread);
    - a `to_thread` target whose definition requires a `conn`: one in ``module`` by
      name, or `other.name` in one of ``others`` (`reconcile.history`);
    - a `to_thread` argument that is a connection (`conn`, `*.conn`);
    - a connection opened in a coroutine's own body (it would be handed on, or block).
    """
    tree = ast.parse(Path(module.__file__).read_text())
    defined = _definitions(module)
    elsewhere = {
        f"{other.__name__.rsplit('.', 1)[-1]}.{name}": node
        for other in others
        for name, node in _definitions(other).items()
    }
    where = Path(module.__file__).name
    found: list[str] = []

    def coroutine_body(fn: ast.AsyncFunctionDef):
        stack = list(fn.body)
        while stack:
            node = stack.pop()
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                continue  # a nested def runs wherever it is called; judged at its call
            yield node
            stack.extend(ast.iter_child_nodes(node))

    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)):
        for node in coroutine_body(fn):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node.func)
            at = f"{where}:{node.lineno} in {fn.name}"
            if name in _OPENERS and ast.unparse(node.func) != "asyncio.to_thread":
                found.append(f"{at}: opens a connection on the loop thread")
            if ast.unparse(node.func) != "asyncio.to_thread" or not node.args:
                continue
            target, *args = node.args
            target_name = _called_name(target)
            definition = elsewhere.get(ast.unparse(target)) or (
                defined.get(target_name)
                if isinstance(target, ast.Name) or ast.unparse(target).startswith("self.")
                else None
            )
            if target_name in _OPENERS:
                found.append(f"{at}: to_thread({ast.unparse(target)}) returns a connection")
            elif definition is not None and _requires_conn(definition):
                found.append(f"{at}: to_thread({ast.unparse(target)}) takes a conn from the caller")
            for arg in [*args, *(k.value for k in node.keywords)]:
                if _called_name(arg) == "conn":
                    found.append(f"{at}: passes {ast.unparse(arg)} into to_thread")
    return found


@pytest.mark.parametrize("module", THREADED_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_no_connection_crosses_a_to_thread_boundary(module) -> None:
    """Database steps from a coroutine go through `store.run_in_thread`, which opens in-thread."""
    others = tuple(m for m in THREADED_MODULES if m is not module)
    assert connection_crossings(module, others) == []


def test_the_crossing_check_catches_the_shape_that_killed_the_first_round(tmp_path) -> None:
    source = tmp_path / "bad.py"
    source.write_text(
        "import asyncio\n"
        "from papaya_agent_runtime.state import db\n\n"
        "def rows(conn, run_id):\n"
        "    return conn.execute('SELECT 1').fetchall()\n\n"
        "async def clean(run_id):\n"
        "    conn = await asyncio.to_thread(db.init_db)\n"
        "    await asyncio.to_thread(rows, conn, run_id)\n"
        "    other = db.init_db()\n"
    )
    assert sorted(connection_crossings(SimpleNamespace(__file__=str(source)))) == [
        "bad.py:10 in clean: opens a connection on the loop thread",
        "bad.py:8 in clean: to_thread(db.init_db) returns a connection",
        "bad.py:9 in clean: passes conn into to_thread",
        "bad.py:9 in clean: to_thread(rows) takes a conn from the caller",
    ]


def test_the_crossing_check_follows_a_target_into_the_reconcile_lane(tmp_path) -> None:
    lane = tmp_path / "lane.py"
    lane.write_text(
        "def needs(worker_id, conn):\n    pass\n\n"
        "def opens_its_own(worker_id, conn=None):\n    pass\n"
    )
    caller = tmp_path / "caller.py"
    caller.write_text(
        "import asyncio\n\n"
        "async def follow(worker_id):\n"
        "    await asyncio.to_thread(lane.needs, worker_id)\n"
        "    await asyncio.to_thread(lane.opens_its_own, worker_id)\n"
    )
    others = (SimpleNamespace(__file__=str(lane), __name__="papaya_agent_runtime.lane"),)
    assert connection_crossings(SimpleNamespace(__file__=str(caller)), others) == [
        "caller.py:4 in follow: to_thread(lane.needs) takes a conn from the caller"
    ]


def test_a_failed_round_logs_its_traceback_records_a_deficiency_and_the_next_round_runs(
    ppy_home, caplog
) -> None:
    from papaya_agent_runtime import deficiencies

    class Runner:
        failures = 1

        @property
        def held(self) -> dict[int, Any]:
            if Runner.failures:
                Runner.failures -= 1
                raise RuntimeError("the runner's holds could not be read")
            return {}

    pruned: list[int | None] = []
    stderr, clock = io.StringIO(), WallClock()
    walker = rounds.Rounds(
        SimpleNamespace(loop=None, agent_config={}),
        Runner(),
        stderr=stderr,
        clock=clock,
        forge=lambda _conn: [],
        prune=lambda task_id: pruned.append(task_id) or {"removed": [], "skipped": []},
        git=lambda *_a, **_k: 0,
    )
    caplog.set_level("WARNING", logger=rounds.log.name)

    asyncio.run(walker.round_once())
    asyncio.run(walker.round_once())

    (failed,) = [r for r in caplog.records if "Round failed" in r.getMessage()]
    assert failed.exc_info is not None and failed.exc_info[0] is RuntimeError
    (row,) = deficiencies.ledger()
    assert row.kind == deficiencies.UNHANDLED_EXCEPTION
    assert row.detail.startswith("a manager round: RuntimeError")
    assert "Traceback" in row.evidence[0]["error"]
    assert "round: the round failed: the runner's holds could not be read" in stderr.getvalue()
    # The next round ran to its end: the hourly hygiene the failed one never reached.
    assert pruned == [None]


def test_run_in_thread_opens_uses_and_closes_the_connection_in_one_worker_thread(ppy_home) -> None:
    seen: dict[str, Any] = {}

    def step(conn: Any, value: int, *, plus: int) -> int:
        seen["thread"] = threading.get_ident()
        seen["conn"] = conn
        return int(conn.execute("SELECT ? + ?", (value, plus)).fetchone()[0])

    assert asyncio.run(store.run_in_thread(step, 2, plus=3)) == 5
    assert seen["thread"] != threading.get_ident()
    with pytest.raises(sqlite3.ProgrammingError):
        seen["conn"].execute("SELECT 1")


def _seed(
    item: str, event_id: int, phases: list[str], worker_status: str | None
) -> tuple[int, int]:
    """A ticket task for ``item`` at ``phases``, and its worker at ``worker_status`` (0: none)."""
    conn = init_db()
    try:
        run_id = store.create_run(conn, f"Ticket {item}")
        ticket = store.add_task(conn, run_id=run_id, title=f"Ticket {item}")
        event = papaya_events.PapayaEvent(
            id=str(event_id),
            kind="work_item.assigned",
            subject=f"work_item:{item}",
            payload={},
            work_item_id=item,
        )
        papaya_events.record_task(conn, ticket, event)
        for phase in phases:
            serve.record_phase(conn, ticket, phase)
    finally:
        conn.close()
    if worker_status is None:
        return ticket, 0
    worker = dispatch_worker(run_id)
    worker_event(worker, "worker_progress", status=worker_status, phase="implement", note="Going.")
    return ticket, worker


def test_one_round_over_a_ticket_in_every_phase_completes_on_real_threads(
    ppy_home, client_home, ready, registered_repo, assigned, pruned, monkeypatch, caplog
) -> None:
    """PAP-219's first start: a round died on a connection opened in another thread.

    The state is the shape Shane's `.ppy/state.db` had — a stalled ticket whose worker
    is done, a handed-over one, a dispatched one with a live worker, a delivered one
    with an open pull request — plus every other phase and worker status. The round
    runs with the real `asyncio.to_thread` and SQLite's own `check_same_thread`.
    """
    from papaya_agent_runtime.state import db

    real_connect = db.sqlite3.connect

    def same_thread_only(*args: Any, **kwargs: Any) -> Any:
        kwargs["check_same_thread"] = True
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(db.sqlite3, "connect", same_thread_only)

    pickup = [serve.PHASE_PICKED_UP, serve.PHASE_BRIEFING, serve.PHASE_DISPATCHED]
    finished = [*pickup, serve.PHASE_REVIEWING, serve.PHASE_DELIVERING, serve.PHASE_REPORTED]
    stalled, stalled_worker = _seed("item-8", 8, [*pickup, serve.PHASE_STALLED], "worker_done")
    _seed("item-11", 11, [*finished, serve.PHASE_RELEASED], "closed")
    released, delivered = _seed("item-12", 12, [*finished, serve.PHASE_RELEASED], "delivered")
    worker_event(delivered, "delivered", status="delivered", pr_url=PR_URL)
    _seed("item-13", 13, [serve.PHASE_PICKED_UP, serve.PHASE_DECLINED], "cancelled")
    _seed("item-14", 14, [*pickup, serve.PHASE_HANDED_OVER], "worker_stopped")
    elsewhere, _blocked = _seed("item-15", 15, pickup, "blocked")
    live, live_worker = _seed("item-16", 16, pickup, "in_progress")
    live_session(live_worker)
    _seed("item-17", 17, [serve.PHASE_PICKED_UP, serve.PHASE_HANDED_BACK], None)
    _seed("item-18", 18, [*pickup, serve.PHASE_DONE], "needs_recovery")
    # A delivered pull request gone red: the round queues it and starts the reconcile lane.
    _red, red_worker = _seed("item-19", 19, [*finished, serve.PHASE_RELEASED], "delivered")
    worker_event(red_worker, "delivered", status="delivered", pr_url=f"{PR_URL}8")

    def act(turn: Turn) -> None:
        if turn.name == prompts.REVIEW and turn.item() == "item-8":
            worker_event(stalled_worker, "reviewed", verdict="approved")

    turns, timer, clock, stderr = FakeTurns(act), Timer(), WallClock(), io.StringIO()
    harness = Harness(FakeEvents([]))
    harness.events.held.add("work_item:item-15")
    forge = [
        _pr(delivered),
        _pr(red_worker, pr=78, url=f"{PR_URL}8", ci="fail", failing=["unit tests"]),
    ]
    seams = {
        **_seams(timer, clock, pruned, forge=forge),
        "pr_details": lambda _worker, _entry: {},
    }
    caplog.set_level("WARNING", logger=rounds.log.name)

    async def scenario() -> int:
        task = _serve(harness, client_home, _runner(turns, FakePapaya()), seams, stderr)
        await _until(lambda: timer.waiting, what="the start's reclaim to finish")
        await _until(lambda: prompts.REVIEW in turns.names(), what="the stalled ticket's review")
        clock.advance(minutes=1)
        await timer.round()
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0

    failures = [r.getMessage() for r in caplog.records if "Round failed" in r.getMessage()]
    assert failures == []
    rounds_said = [line for line in stderr.getvalue().splitlines() if "round:" in line]
    assert not any("failed" in line for line in rounds_said), rounds_said
    start = next(line for line in rounds_said if "took ticket task" in line)
    assert f"took ticket task {stalled} back up (item-8)" in start
    assert f"took ticket task {live} back up (item-16)" in start
    assert f"ticket task {elsewhere} is held by" in start
    assert store.task_phase(init_db(), elsewhere) == serve.PHASE_HANDED_OVER
    # The stalled ticket with a done worker resumed at review, not at a new brief.
    assert turns.calls[0].item() == "item-8" and turns.names()[0] == prompts.REVIEW
    assert prompts.BRIEF not in turns.names()
    assert serve.PHASE_REVIEWING in history(8)
    # The round after the start got to its last step: the hourly hygiene run.
    assert pruned == [None]
    assert store.task_phase(init_db(), released) == serve.PHASE_RELEASED
    # The red pull request went through the reconcile lane's to_thread steps in the round.
    assert events_of(red_worker, reconcile.STARTED)
    assert events_of(red_worker, serve.PR_ATTENTION)
    assert serve.PHASE_DISPATCHED in history(19)[len(finished) + 1 :]
