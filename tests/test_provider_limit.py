"""A turn the provider's usage limit ended is waited out, never counted as a failure.

2026-09-19: the Claude account hit its session limit at 17:59 UTC. The lane's review
turns on tasks 150 and 157 ended at once with `You've hit your session limit · resets
12:30pm (America/Los_Angeles)`, twice each; the lane counted both as misses, wrote a
person-wait todo, and never looked at either task again — not when the limit reset at
19:30, not when task 150 said `worker_done` at 19:42. A held ticket would have been
handed back. The limit is a temporary condition with a known end; a give-up is a
verdict that news must be able to overturn.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import test_serve
from papaya_agent_runtime import board, lanes, limits, prompts, serve, supervision, team, watch
from papaya_agent_runtime.config import ManagerProfile, MMConfig, WorkerCeiling
from papaya_agent_runtime.manager.launch import MANAGER_TURN_ENV, TurnResult
from papaya_agent_runtime.state import init_db, store
from test_serve import (
    EVENT,
    FakeEvents,
    FakePapaya,
    FakeTurns,
    Harness,
    Turn,
    _deliver,
    _one_ticket,
    _runner,
    dispatch_worker,
    history,
    ticket_task,
    worker_event,
    workers_in,
)

globals().update(
    {
        name: getattr(test_serve, name)
        for name in ("client_home", "ready", "registered_repo", "progress_lines")
    }
)

#: The real strings, as the live turn logs and worker `error` events have them.
SESSION = "You've hit your session limit · resets 12:30pm (America/Los_Angeles)"
WEEKLY = "You've hit your weekly limit · resets Sep 22 at 7am (America/Los_Angeles)"

#: When task 150's first limited turn ended (18:05 UTC = 11:05 PDT).
SEEN = datetime(2026, 9, 19, 18, 5, 43, tzinfo=UTC)
#: 12:30pm in Los Angeles on that day (PDT, UTC-7).
RESET = datetime(2026, 9, 19, 19, 30, tzinfo=UTC)


class Wall:
    """A wall clock moved by hand, or by a fixed step on every read."""

    def __init__(self, at: datetime, step: timedelta = timedelta(0)) -> None:
        self.now = at
        self.step = step

    def __call__(self) -> datetime:
        now = self.now
        self.now = now + self.step
        return now


@pytest.fixture
def home(ppy_home, monkeypatch):
    monkeypatch.delenv("PPY_DEV", raising=False)
    monkeypatch.delenv(MANAGER_TURN_ENV, raising=False)
    monkeypatch.setattr(supervision, "serve_running", lambda: False)
    return ppy_home


# ── the one classifier ──────────────────────────────────────────────────────


def test_the_session_limit_line_resets_at_its_named_time_in_its_own_zone() -> None:
    found = limits.classify(TurnResult(exit_code=1, transcript=SESSION + "\n"), at=SEEN)

    assert found is not None and found.exact and found.provider == "claude"
    assert found.until == RESET
    assert found.text == SESSION


def test_the_weekly_limit_line_resets_on_its_named_day() -> None:
    found = limits.classify_text(WEEKLY, 1, provider="claude", at=SEEN)

    assert found is not None and found.exact
    assert found.until == datetime(2026, 9, 22, 14, 0, tzinfo=UTC)  # 7am PDT


@pytest.mark.parametrize("late", [timedelta(seconds=20), timedelta(minutes=14)])
def test_a_time_of_day_just_passed_is_over_never_tomorrow(late) -> None:
    """Seen seconds or minutes after `12:30pm`: the reset has just happened."""
    seen = RESET + late

    assert limits.reset_instant("12:30pm", "America/Los_Angeles", seen) == seen
    found = limits.classify_text(SESSION, 1, at=seen)
    # The provider still said no, so not "over" either: the shortest backoff, not a day.
    assert found is not None and not found.exact
    assert found.until == seen + timedelta(minutes=5)


def test_a_time_of_day_past_the_grace_is_a_stale_line_and_backs_off() -> None:
    """Seen 16 minutes after `12:30pm`, the next 12:30pm is a day away, which no session
    window is: the line is not believed, and the ending backs off 5, 10, 20, 30 minutes."""
    seen = RESET + timedelta(minutes=16)

    assert limits.reset_instant("12:30pm", "America/Los_Angeles", seen) is None
    first = limits.classify_text(SESSION, 1, at=seen)
    fourth = limits.classify_text(SESSION, 1, at=seen, times=4)
    assert first is not None and first.until == seen + timedelta(minutes=5)
    assert fourth is not None and fourth.until == seen + timedelta(minutes=30)


def test_a_time_of_day_across_midnight_is_the_next_one() -> None:
    seen = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)  # 11pm PDT on the 19th
    line = "You've hit your session limit · resets 3am (America/Los_Angeles)"

    found = limits.classify_text(line, 1, at=seen)

    assert found is not None and found.exact
    assert found.until == datetime(2026, 9, 20, 10, 0, tzinfo=UTC)


def test_a_named_reset_already_past_backs_off_instead_of_hammering() -> None:
    seen = datetime(2026, 9, 19, 18, 0, tzinfo=UTC)  # 11am PDT, after 7am
    line = "You've hit your weekly limit · resets Sep 19 at 7am (America/Los_Angeles)"

    found = limits.classify_text(line, 1, at=seen)

    assert found is not None and not found.exact
    assert found.until == seen + timedelta(minutes=5)


@pytest.mark.parametrize(
    "resets_at",
    [4e9, 0, -5, "1789846200", True, float("inf"), float("nan")],
    ids=["year-2096", "zero", "negative", "string", "bool", "inf", "nan"],
)
def test_an_insane_streamed_reset_falls_back_to_the_text(resets_at) -> None:
    found = limits.classify_text(SESSION, 1, at=SEEN, resets_at=resets_at)

    assert found is not None and found.exact and found.until == RESET


def test_a_millisecond_epoch_is_read_as_milliseconds() -> None:
    streamed = datetime(2026, 9, 19, 19, 45, tzinfo=UTC)

    found = limits.classify_text(SESSION, 1, at=SEEN, resets_at=int(streamed.timestamp() * 1000))

    assert found is not None and found.until == streamed


def test_a_recorded_pause_past_the_cap_or_unreadable_is_no_pause(home) -> None:
    conn = init_db()
    for until in ((SEEN + timedelta(days=30)).isoformat(), "not a time", None):
        store.append_event(
            conn,
            kind=limits.LIMIT_EVENT,
            payload={"provider": "claude", "at": SEEN.isoformat(), "until": until},
        )
    store.append_event(conn, kind=limits.LIMIT_EVENT, payload="not even a dict")

    assert limits.pause(conn, "claude", SEEN + timedelta(minutes=1)) is None


@pytest.mark.parametrize(
    "resets_at",
    [4e9, 1789846200000, 0, -1, "soon"],
    ids=["year-2096", "milliseconds", "zero", "negative", "string"],
)
def test_the_owed_lane_survives_any_streamed_reset(home, resets_at) -> None:
    """A worker stream with a bad `resetsAt` never breaks the round that reads it."""
    conn = init_db()
    task_id = _limit_stopped(conn, resets_at=resets_at)

    found = limits.pause(conn, None, SEEN + timedelta(minutes=1))
    decisions = lanes.owed_decisions(conn, now=SEEN + timedelta(minutes=1))

    assert found is not None and found.until - SEEN <= limits.LONGEST_RESET
    assert decisions == []  # paused: waited out, not a failure
    after = lanes.owed_decisions(conn, now=found.until + timedelta(seconds=1))
    assert [(d.task_id, d.action) for d in after] == [(task_id, lanes.RESUME)]


def test_pause_never_raises(home, monkeypatch) -> None:
    conn = init_db()

    def broken(*_a, **_k):
        raise RuntimeError("a corrupt row")

    monkeypatch.setattr(limits, "_worker_limits", broken)

    assert limits.pause(conn, "claude", SEEN) is None
    assert lanes.owed_decisions(conn, now=SEEN) == []


def test_a_turn_that_ends_normally_ends_the_pause_early(home) -> None:
    conn = init_db()
    limits.record(limits.Limit("claude", SESSION, SEEN, RESET, exact=True), source="review turn")
    later = SEEN + timedelta(minutes=10)
    assert limits.pause(conn, "claude", later) is not None

    # A turn already running when the wall was hit, ending well afterwards.
    assert (
        limits.observe(
            TurnResult(exit_code=0, transcript="done"), provider="claude", at=later, source="t"
        )
        is None
    )

    assert limits.pause(conn, "claude", later + timedelta(seconds=1)) is None
    # A limit hit after the clear is a new pause.
    newer = limits.Limit("claude", SESSION, later + timedelta(minutes=1), RESET, exact=True)
    limits.record(newer, source="review turn")
    assert limits.pause(conn, "claude", later + timedelta(minutes=2)) is not None
    # With no pause standing, a normal ending records nothing.
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    limits.observe(TurnResult(exit_code=0, transcript="ok"), provider="codex", at=later, source="t")
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before


@pytest.mark.parametrize(
    "line",
    [
        "You've hit your session limit · resets 12:30pm",  # no zone: never guessed
        "You've hit your session limit · resets soon (America/Los_Angeles)",
        "You've hit your session limit · resets 12:30pm (Mars/Olympus_Mons)",
        "You've hit your session limit",
    ],
)
def test_an_unreadable_reset_backs_off_like_a_wait_capped_at_thirty_minutes(line) -> None:
    first = limits.classify_text(line, 1, at=SEEN)
    third = limits.classify_text(line, 1, at=SEEN, times=3)
    tenth = limits.classify_text(line, 1, at=SEEN, times=10)

    assert first is not None and not first.exact
    assert first.until == SEEN + timedelta(minutes=5)
    assert third is not None and third.until == SEEN + timedelta(minutes=20)
    assert tenth is not None and tenth.until == SEEN + timedelta(minutes=30)


def test_only_the_terminal_line_of_a_failed_ending_is_a_limit() -> None:
    # A clean exit is never a limit, whatever it says.
    assert limits.classify_text(SESSION, 0, at=SEEN) is None
    # A failure with another ending is a failure, not a limit.
    assert limits.classify_text("Error: the gate crashed", 1, at=SEEN) is None
    # A worker quoting the words in its ordinary output is not a limit either.
    quoted = f"The docs say:\n{SESSION}\nso I retried and the build is red.\n"
    assert limits.classify_text(quoted, 1, at=SEEN) is None
    assert limits.classify_text("I hit the session limit of the test fixture", 1, at=SEEN) is None


def test_codex_has_no_wording_until_one_is_seen() -> None:
    assert limits.PATTERNS["codex"] == ()
    assert limits.classify_text(SESSION, 1, provider="codex", at=SEEN) is None
    # Asked with no provider, every provider's patterns are tried.
    assert limits.classify_text(SESSION, 1, provider=None, at=SEEN) is not None


# ── the lane: task-keyed turns ──────────────────────────────────────────────


def _config() -> MMConfig:
    return MMConfig(
        manager=ManagerProfile(provider="claude", model=None, reasoning=None),
        worker=WorkerCeiling(provider="claude", max_model="opus", max_reasoning="medium"),
    )


class Harness2:
    """The manager harness: each launch answers with the next ending in the script."""

    def __init__(self, *endings: tuple[int, str], act=None) -> None:
        self.endings = list(endings)
        self.act = act or (lambda launch: None)
        self.prompts: list[str] = []

    def __call__(self, launch: Any, *, should_stop, transcript_path=None) -> TurnResult:
        self.prompts.append(launch.seed_prompt)
        code, text = self.endings.pop(0) if self.endings else (0, "turn")
        if code == 0:
            self.act(launch)
        return TurnResult(exit_code=code, transcript=text)


def _worker(conn, status: str) -> int:
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title="build")
    store.update_task_fields(conn, task_id, branch=f"ppy/task-{task_id}")
    store.set_task_status(conn, task_id, status)
    return task_id


def _lane(turns, wall: Wall, tmp_path) -> lanes.TurnRunner:
    return lanes.TurnRunner(run_turn=turns, config=_config, runtime_dir=str(tmp_path), clock=wall)


def _round(runner: lanes.TurnRunner, now: datetime) -> list[str]:
    async def scenario() -> list[str]:
        lines = await runner.take_up(lanes.owed_decisions(init_db(), now=now))
        for task in list(runner.running.values()):
            await task
        return lines

    return asyncio.run(scenario())


def _deliver_task(task_id: int) -> None:
    conn = init_db()
    try:
        store.set_task_status(conn, task_id, "delivered")
        store.append_event(conn, kind="delivered", payload={"task_id": task_id}, task_id=task_id)
    finally:
        conn.close()


def test_a_limited_review_turn_is_waited_out_and_never_a_miss(home, tmp_path) -> None:
    """The H2 probe for the lane: two limit endings, then the reset, then the review."""
    conn = init_db()
    task_id = _worker(conn, "worker_done")
    wall = Wall(SEEN)
    turns = Harness2((1, SESSION + "\n"), act=lambda launch: _deliver_task(task_id))
    runner = _lane(turns, wall, tmp_path)

    _round(runner, SEEN)

    conn = init_db()
    other = _worker(conn, "worker_done")
    record = lanes.last_turn(conn, task_id)
    assert record["outcome"] == lanes.LIMITED and record["misses"] == 0
    assert record["until"] == RESET.isoformat()
    assert board.waiting(conn) == []  # no person-wait
    # One observation pauses every turn on the provider: the other worker is not
    # launched into the same wall, and neither is this one before the reset.
    assert len(turns.prompts) == 1
    wall.now = SEEN + timedelta(minutes=10)
    assert _round(runner, wall.now) == []
    assert len(turns.prompts) == 1
    assert {d.task_id for d in lanes.owed_decisions(conn, now=wall.now)} == {other}

    wall.now = RESET + timedelta(seconds=1)
    _round(runner, wall.now)

    assert lanes.last_turn(init_db(), task_id)["outcome"] == lanes.DELIVERED
    assert store.get_task(init_db(), task_id)["status"] == "delivered"


def test_a_failed_ending_that_is_not_the_limit_is_still_a_miss(home, tmp_path) -> None:
    conn = init_db()
    task_id = _worker(conn, "worker_done")
    turns = Harness2((1, "Error: the harness crashed\n"), (1, "Error: again\n"))
    runner = _lane(turns, Wall(SEEN), tmp_path)

    _round(runner, SEEN)
    _round(runner, SEEN)

    conn = init_db()
    assert lanes.last_turn(conn, task_id)["outcome"] == lanes.MISSED
    assert lanes.last_turn(conn, task_id)["misses"] == 2
    [todo] = board.waiting(conn)
    assert "the manager turn ended 2 times" in todo["text"]
    assert limits.pause(conn, None, SEEN) is None


def test_a_limited_ledger_turn_counts_nothing_against_the_next_steps(home, tmp_path) -> None:
    conn = init_db()
    todo_id = board.add("close the stale branch", conn=conn)
    stamp = (SEEN - timedelta(minutes=45)).isoformat()
    conn.execute("UPDATE todos SET updated_at = ? WHERE id = ?", (stamp, todo_id))
    conn.commit()
    turns = Harness2((1, SESSION))
    runner = _lane(turns, Wall(SEEN), tmp_path)

    async def scenario() -> list[str]:
        lines = await runner.take_up_ledger(lanes.ledger_due(conn, now=SEEN))
        await runner._ledger
        # Paused now: no second ledger turn into the same wall.
        lines += await runner.take_up_ledger(lanes.ledger_due(conn, now=SEEN))
        return lines

    assert asyncio.run(scenario()) == ["running the ledger turn for 1 next step(s)"]
    assert lanes.last_ledger_turn(init_db()) is None
    assert store.get_todo(init_db(), todo_id)["blocked_on"] is None


def test_the_pause_is_read_from_the_record_so_a_restart_still_waits(home, tmp_path) -> None:
    conn = init_db()
    task_id = _worker(conn, "worker_done")
    limits.record(limits.Limit("claude", SESSION, SEEN, RESET, exact=True), source="review turn")
    # A runner that never saw the limit, as after a `ppy serve` restart.
    turns = Harness2(act=lambda launch: _deliver_task(task_id))
    fresh = _lane(turns, Wall(SEEN + timedelta(minutes=20)), tmp_path)

    assert _round(fresh, SEEN + timedelta(minutes=20)) == []
    assert turns.prompts == []

    later = _lane(turns, Wall(RESET), tmp_path)
    assert _round(later, RESET) == [f"worker task {task_id}: running the review turn"]


# ── a give-up ends when something new happens ─────────────────────────────


def _missed(task_id: int, misses: int, **extra: Any) -> None:
    end = watch.max_event_id(init_db())
    lanes.record_task_turn(
        task_id,
        action=lanes.TURN,
        turn=prompts.REVIEW,
        outcome=lanes.MISSED,
        misses=misses,
        mark=end,
        end_mark=end,
        **extra,
    )


def test_news_after_a_give_up_takes_the_task_up_again_with_a_fresh_count(home) -> None:
    """The H1 probe: two genuine misses, then `worker_done`, then a turn is due."""
    conn = init_db()
    task_id = _worker(conn, "worker_done")
    _missed(task_id, 1, tail="ended without deciding", exit_code=0)
    _missed(task_id, 2, tail="ended without deciding", exit_code=0)
    lanes.record_person_wait(
        task_id,
        f"worker task {task_id}: the manager turn ended 2 times without reviewing and "
        "delivering, or steering; decide on it",
    )

    # No news: still given up, and the surface says so.
    assert lanes.owed_decisions(conn, now=SEEN) == []
    [gave_up] = team.attention(conn, SEEN)["gave_up"]
    assert gave_up["task_id"] == task_id

    store.append_event(
        conn,
        kind="worker_done",
        payload={"task_id": task_id, "summary": "done at 8d5ed5b5"},
        task_id=task_id,
    )

    [decision] = lanes.owed_decisions(conn, now=SEEN)
    assert (decision.task_id, decision.action, decision.turn) == (task_id, lanes.TURN, "review")
    assert team.attention(conn, SEEN)["gave_up"] == []
    # The count starts again: one more miss is a retry, not a second give-up.
    assert lanes._misses_so_far(conn, task_id, SEEN) == 0
    lanes.lift_give_up(task_id, SEEN, conn)
    assert board.waiting(conn) == []
    _missed(task_id, 1, tail="ended without deciding", exit_code=0)
    assert [d.task_id for d in lanes.owed_decisions(conn, now=SEEN)] == [task_id]


def test_a_persons_steer_and_a_push_seen_are_news_too(home) -> None:
    for kind in ("steer", "pr_observed", "answer"):
        conn = init_db()
        task_id = _worker(conn, "worker_done")
        _missed(task_id, 1, exit_code=0)
        _missed(task_id, 2, exit_code=0)
        assert task_id not in {d.task_id for d in lanes.owed_decisions(conn, now=SEEN)}
        store.append_event(
            conn, kind=kind, payload={"task_id": task_id, "by": "person"}, task_id=task_id
        )
        assert task_id in {d.task_id for d in lanes.owed_decisions(conn, now=SEEN)}


def test_tasks_stranded_by_limit_endings_counted_as_misses_recover_on_upgrade(
    home, tmp_path
) -> None:
    """Tasks 150 and 157, as `state.db` has them: two limit tails recorded as misses."""
    conn = init_db()
    task_id = _worker(conn, "worker_done")
    for misses, at in ((1, "2026-09-19T18:05:44+00:00"), (2, "2026-09-19T18:11:02+00:00")):
        _missed(task_id, misses, tail=SESSION + "\n", exit_code=1)
        conn.execute(
            "UPDATE events SET created_at = ? WHERE id = (SELECT MAX(id) FROM events)", (at,)
        )
        conn.commit()
    lanes.record_person_wait(
        task_id,
        f"worker task {task_id}: the manager turn ended 2 times without reviewing and "
        "delivering, or steering; decide on it",
    )

    # Before the reset the old endings still stand as the limit; after it, due again.
    assert lanes.owed_decisions(conn, now=RESET - timedelta(minutes=5)) == []
    after = RESET + timedelta(minutes=12)  # 19:42, task 150's worker_done
    [decision] = lanes.owed_decisions(conn, now=after)
    assert decision.task_id == task_id and decision.action == lanes.TURN
    assert decision.tail == ""  # the limit line is not a previous attempt
    assert team.attention(conn, after)["gave_up"] == []

    turns = Harness2(act=lambda launch: _deliver_task(task_id))
    _round(_lane(turns, Wall(after), tmp_path), after)

    conn = init_db()
    assert store.get_task(conn, task_id)["status"] == "delivered"
    assert board.waiting(conn) == []  # the stale give-up todo is dropped


# ── a worker the limit stopped ──────────────────────────────────────────────


def _limit_stopped(conn, *, resets_at: float | None = None, session: str = "s-1") -> int:
    task_id = _worker(conn, "failed")
    if resets_at is not None:
        store.append_event(
            conn,
            kind="worker_rate_limit_event",
            payload={
                "type": "rate_limit_event",
                "rate_limit_info": {"status": "rejected", "resetsAt": resets_at},
                "session_id": session,
            },
            task_id=task_id,
        )
    store.append_event(
        conn,
        kind="error",
        payload={"task_id": task_id, "summary": SESSION, "exit_code": 1, "session_id": session},
        task_id=task_id,
    )
    conn.execute("UPDATE events SET created_at = ? WHERE task_id = ?", (SEEN.isoformat(), task_id))
    conn.commit()
    return task_id


def test_a_worker_the_limit_stopped_is_resumed_once_after_the_reset(home, tmp_path) -> None:
    conn = init_db()
    task_id = _limit_stopped(conn)
    steered: list[tuple[int, str]] = []
    turns = Harness2()
    runner = lanes.TurnRunner(
        run_turn=turns,
        config=_config,
        runtime_dir=str(tmp_path),
        clock=Wall(SEEN),
        steer=lambda t, m: steered.append((t, m)),
    )

    # Before the reset: no turn, no resume, not a failure; the pause is on the surface.
    assert lanes.owed_decisions(conn, now=SEEN + timedelta(minutes=30)) == []
    [paused] = team.attention(conn, SEEN + timedelta(minutes=30))["paused"]
    assert paused["until"] == RESET.isoformat() and paused["exact"]
    assert any(
        line.startswith("paused: claude usage limit until")
        for line in team.attention_lines(team.attention(conn, SEEN + timedelta(minutes=30)))
    )

    after = RESET + timedelta(minutes=1)
    [decision] = lanes.owed_decisions(conn, now=after)
    assert decision.action == lanes.RESUME
    assert _round(runner, after) == [f"worker task {task_id} resumed after the usage limit reset"]
    assert steered == [(task_id, limits.RESUME_MESSAGE)]
    assert turns.prompts == []

    # Once: the fake steer changed nothing, so the worker is a failure like any other now.
    [decision] = lanes.owed_decisions(init_db(), now=after)
    assert decision.action == lanes.TURN and decision.turn == prompts.REVIEW


def test_the_workers_streamed_reset_beats_the_text(home) -> None:
    conn = init_db()
    streamed = datetime(2026, 9, 19, 19, 45, tzinfo=UTC)
    task_id = _limit_stopped(conn, resets_at=streamed.timestamp())

    ending = limits.worker_ending(conn, task_id)

    assert ending is not None and ending.limit.until == streamed


def test_a_worker_error_that_only_quotes_the_limit_is_a_failure(home) -> None:
    conn = init_db()
    task_id = _worker(conn, "failed")
    store.append_event(
        conn,
        kind="error",
        payload={"summary": f"tests red; the fixture said {SESSION!r} earlier", "exit_code": 1},
        task_id=task_id,
    )

    assert limits.worker_ending(conn, task_id) is None
    [decision] = lanes.owed_decisions(conn, now=SEEN)
    assert decision.action == lanes.TURN


LEDGER_BLOCK = "user:two manager turns left it untouched; do it, defer it with a reason, or drop it"


def _ledger_turn(conn, todos: list[int], at: datetime, transcript: str, home) -> None:
    """What the old code left for one ledger turn: its transcript, then its record."""
    turns = home / "runs" / "0" / "turns"
    turns.mkdir(parents=True, exist_ok=True)
    log = turns / f"ledger-{len(list(turns.glob('ledger-*.log'))) + 1}.log"
    log.write_text(transcript, encoding="utf-8")
    written = (at - timedelta(seconds=3)).timestamp()
    os.utime(log, (written, written))
    store.append_event(
        conn,
        kind=lanes.LEDGER_TURN_EVENT,
        payload={"todos": todos, "left": {}, "at": at.isoformat()},
    )


def _blocked_by_two_ledger_turns(
    conn, text: str, transcripts: tuple[str, str], home, start: datetime
) -> int:
    todo_id = board.add(text, conn=conn)
    first, second = start, start + timedelta(minutes=10)
    _ledger_turn(conn, [todo_id], first, transcripts[0], home)
    _ledger_turn(conn, [todo_id], second, transcripts[1], home)
    store.update_todo(conn, todo_id, blocked_on=LEDGER_BLOCK)
    conn.execute("UPDATE todos SET updated_at = ? WHERE id = ?", (second.isoformat(), todo_id))
    conn.commit()
    return todo_id


def test_next_steps_blocked_by_limit_killed_ledger_turns_recover_on_upgrade(home) -> None:
    """The old code blocked a next step on a person after two limit-killed ledger turns
    (runs/0/turns/ledger-41..54 were all the limit line). Re-read, they are due again;
    a genuine two-miss block stays, and is listed under needs attention."""
    conn = init_db()
    assert lanes._LEDGER_GAVE_UP == LEDGER_BLOCK
    limited = _blocked_by_two_ledger_turns(
        conn, "close the stale branch", (SESSION + "\n", SESSION + "\n"), home, SEEN
    )
    genuine = _blocked_by_two_ledger_turns(
        conn,
        "post PR #720 on its work item",
        ("Looked at todo; left it.\n", "Still not done; the PR is not open.\n"),
        home,
        SEEN + timedelta(hours=1),
    )
    mixed = _blocked_by_two_ledger_turns(
        conn,
        "merge the docs PR",
        (SESSION + "\n", "Left it for later.\n"),
        home,
        SEEN + timedelta(hours=2),
    )

    lines = lanes.release_finished_waits(conn, now=SEEN + timedelta(hours=3))

    conn = init_db()
    assert store.get_todo(conn, limited)["blocked_on"] is None
    assert store.get_todo(conn, limited)["status"] == "open"
    assert store.get_todo(conn, genuine)["blocked_on"] == LEDGER_BLOCK
    assert store.get_todo(conn, mixed)["blocked_on"] == LEDGER_BLOCK
    assert any(f"todo #{limited} is due again" in line for line in lines)
    later = datetime.now(UTC) + timedelta(hours=1)  # unblocking stamps the todo now
    assert [i.todo_id for i in lanes.ledger_due(conn, now=later)] == [limited]
    # Once: nothing more to lift on the next round.
    assert not any("is due again" in x for x in lanes.release_finished_waits(conn, now=SEEN))
    shown = team.attention_lines(team.attention(conn, SEEN + timedelta(hours=2)))
    gave_up = [line for line in shown if line.startswith("gave up: todo")]
    assert len(gave_up) == 2
    assert any(f"todo #{genuine}" in line for line in gave_up)
    assert not any(f"todo #{limited}" in line for line in gave_up)


def test_needs_attention_names_a_give_up_with_its_reason(home) -> None:
    conn = init_db()
    task_id = _worker(conn, "worker_done")
    _missed(task_id, 1, exit_code=0)
    _missed(task_id, 2, exit_code=0)
    lanes.record_person_wait(
        task_id,
        f"worker task {task_id}: the manager turn ended 2 times without reviewing and "
        "delivering, or steering; decide on it",
    )

    lines = team.attention_lines(team.attention(conn, SEEN))

    [line] = [line for line in lines if line.startswith("gave up:")]
    assert f"worker task {task_id}" in line and "reviewing and delivering" in line
    assert "anything new on the task takes it up again" in line


# ── the ticket runner: brief, answer, review and check-in turns ─────────────


def _limited_first(turns: FakeTurns, limited: dict[str, int]):
    """Wrap a FakeTurns so the first ``n`` launches of each named turn hit the limit."""

    def harness(launch: Any, *, should_stop, transcript_path=None) -> TurnResult:
        name = test_serve._which_turn(launch.seed_prompt)
        if limited.get(name, 0) > 0:
            limited[name] -= 1
            harness.limited.append(name)
            return TurnResult(exit_code=1, transcript=SESSION + "\n")
        return turns(launch, should_stop=should_stop, transcript_path=transcript_path)

    harness.limited = []
    return harness


def test_a_held_tickets_limited_turns_are_waited_out_not_handed_back(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    """The H2 probe: two limited brief turns and a limited review, then it delivers."""

    def act(turn: Turn) -> None:
        if turn.name == prompts.BRIEF:
            test_serve._dispatch_then_finish(turn)
        elif turn.name == prompts.REVIEW:
            _deliver(turn)

    papaya_api = FakePapaya()
    turns = FakeTurns(act)
    harness = _limited_first(turns, {prompts.BRIEF: 2, prompts.REVIEW: 1})
    runner = _runner(harness, papaya_api)
    # Ten minutes pass on the wall clock with every read: the reset comes round.
    runner._wall = Wall(SEEN, step=timedelta(minutes=10))

    assert _one_ticket(Harness(FakeEvents([EVENT])), client_home, runner) == 0

    assert harness.limited == [prompts.BRIEF, prompts.BRIEF, prompts.REVIEW]
    assert turns.names() == [prompts.BRIEF, prompts.REVIEW]
    assert serve.PHASE_HANDED_BACK not in history()
    assert history()[-2:] == [serve.PHASE_REPORTED, serve.PHASE_RELEASED]
    # Nothing on the ticket about it, and nothing recorded as the runtime's failure.
    bodies = [body for _item, body in papaya_api.comments()]
    assert not any("limit" in body or "handed back" in body for body in bodies)
    conn = init_db()
    kinds = [row["kind"] for row in conn.execute("SELECT kind FROM deficiencies").fetchall()]
    assert kinds == []
    details = [detail for _s, _p, detail in progress_lines]
    assert any("waits for the usage limit to reset" in d for d in details)
    assert not any("attempt 1 of" in d for d in details)
    assert [p["source"] for p in _limit_events(conn)] == [
        "brief turn",
        "brief turn",
        "review turn",
    ]


def test_limit_endings_past_their_reset_back_off_one_launch_each(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    """The provider still refuses after the reset it named: no back-to-back launches.

    Seen at 11am PDT, `resets 7am` is long past: each such ending backs off 5, 10, then
    20 minutes, one launch per ending, one progress line per new pause.
    """
    stale = "You've hit your session limit · resets 7am (America/Los_Angeles)"
    wall = Wall(SEEN, step=timedelta(minutes=1))
    launched: list[datetime] = []

    def act(turn: Turn) -> None:
        if turn.name == prompts.BRIEF:
            test_serve._dispatch_then_finish(turn)
        elif turn.name == prompts.REVIEW:
            _deliver(turn)

    turns = FakeTurns(act)

    def harness(launch: Any, *, should_stop, transcript_path=None) -> TurnResult:
        launched.append(wall.now)
        if len(launched) <= 3:
            return TurnResult(exit_code=1, transcript=stale + "\n")
        return turns(launch, should_stop=should_stop, transcript_path=transcript_path)

    runner = _runner(harness, FakePapaya())
    runner._wall = wall

    assert _one_ticket(Harness(FakeEvents([EVENT])), client_home, runner) == 0

    assert turns.names() == [prompts.BRIEF, prompts.REVIEW]
    assert len(launched) == 5  # three limited briefs, the brief, the review
    gaps = [later - earlier for earlier, later in zip(launched, launched[1:4], strict=False)]
    assert [g >= timedelta(minutes=m) for g, m in zip(gaps, (5, 10, 20), strict=True)] == [True] * 3
    assert all(g < timedelta(minutes=m + 5) for g, m in zip(gaps, (5, 10, 20), strict=True))
    details = [d for _s, _p, d in progress_lines]
    assert sum("was ended by the provider's usage limit" in d for d in details) == 1
    waits = [d for d in details if "waits for the usage limit to reset" in d]
    assert len(waits) == 3 and len(set(waits)) == 3


def test_a_comment_during_a_pause_is_heard_and_a_stop_ends_the_wait(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    papaya_api = FakePapaya()
    launches: list[str] = []

    def harness(launch: Any, *, should_stop, transcript_path=None) -> TurnResult:
        launches.append(test_serve._which_turn(launch.seed_prompt))
        papaya_api.comment_from("item-9", "Use the v2 endpoint, please.")
        return TurnResult(exit_code=1, transcript=SESSION + "\n")

    runner = _runner(harness, papaya_api)
    runner._wall = Wall(SEEN)  # the clock never reaches the reset: only a stop ends it
    heard = "A comment from"
    events = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        task = test_serve._serve_ticket(events, client_home, runner)
        await test_serve._until(
            lambda: any(heard in d and "usage-limit pause" in d for _s, _p, d in progress_lines),
            what="the comment to be heard during the pause",
        )
        events.loop.request_stop()
        return await asyncio.wait_for(task, timeout=30)

    assert asyncio.run(scenario()) == 0
    assert launches == [prompts.BRIEF]  # nothing launched into the pause
    assert serve.PHASE_HANDED_BACK not in history()
    assert not any(body.startswith("handed back") for _i, body in papaya_api.comments())


def _limit_events(conn) -> list[dict[str, Any]]:
    return [
        json.loads(row["payload"])
        for row in conn.execute(
            "SELECT payload FROM events WHERE kind = ? ORDER BY id", (limits.LIMIT_EVENT,)
        ).fetchall()
    ]


def test_a_held_tickets_worker_the_limit_stopped_is_resumed_not_reviewed(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    steered: list[tuple[int, str]] = []

    def act(turn: Turn) -> None:
        if turn.name == prompts.BRIEF:
            worker = dispatch_worker(turn.run_id)
            worker_event(
                worker, "error", status="failed", summary=SESSION, exit_code=1, session_id="s"
            )
        elif turn.name == prompts.REVIEW:
            _deliver(turn)

    def steer(task_id: int, message: str) -> None:
        steered.append((task_id, message))
        # What the resumed worker does: finish.
        worker_event(task_id, "worker_done", status="worker_done", summary="done at abc123")

    papaya_api = FakePapaya()
    turns = FakeTurns(act)
    runner = _runner(turns, papaya_api, steer=steer)
    runner._wall = Wall(datetime.now(UTC), step=timedelta(minutes=30))

    assert _one_ticket(Harness(FakeEvents([EVENT])), client_home, runner) == 0

    (worker,) = workers_in(int(ticket_task()["run_id"]))
    assert steered == [(worker, limits.RESUME_MESSAGE)]
    # Reviewed once, when it said done; never as a failure.
    assert turns.names() == [prompts.BRIEF, prompts.REVIEW]
    assert "stopped short" not in " ".join(d for _s, _p, d in progress_lines)
    assert any("resumed after the reset" in d for _s, _p, d in progress_lines)
