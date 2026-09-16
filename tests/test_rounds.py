"""`ppy serve`'s rounds: every worker and every loose end, looked at on a clock.

The rounds run inside the real `serve.run`, beside the client's own loop, exactly
as `tests/test_serve.py` drives it. What is faked is the outside world and time:
the harness (:class:`FakeTurns`), Papaya (:class:`FakePapaya`, :class:`FakeEvents`),
the forge, the worktree pruner, and two clocks — the rounds' timer, fired by hand,
and the wall clock a round reads silence and age on, moved by hand.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import test_serve
from conftest import scale
from papaya_agent_runtime import (
    gate,
    papaya_events,
    preflight,
    progress,
    prompts,
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

    async def scenario() -> int:
        task = _serve(
            harness, client_home, _runner(turns, FakePapaya()), _seams(timer, clock, pruned)
        )
        await _until(lambda: harness.jobs, what="the reclaimed hold")
        progress.record(worker, phase="test", note="Suite running.")
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
