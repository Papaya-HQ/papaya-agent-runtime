"""Everything tracked gets acted on: the owed, ledger and deficiency lanes in both modes.

2026-09-17 (#72): a `ppy serve` restart handed three tickets over, their workers lost
every future turn with them, and two finished workers sat unreviewed for a day while
the runtime knew and nothing acted. The lanes are one decision over the ledger; serve
runs turns keyed on the task, a session is told and held to the same decisions.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from papaya_agent_runtime import (
    board,
    gate,
    hooks,
    lanes,
    owed,
    papaya_events,
    prompts,
    rounds,
    supervision,
    tool_learning,
    watch,
)
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.config import ManagerProfile, MMConfig, WorkerCeiling
from papaya_agent_runtime.manager.launch import MANAGER_TURN_ENV, TurnResult
from papaya_agent_runtime.state import init_db, store

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def home(ppy_home, monkeypatch):
    monkeypatch.delenv("PPY_DEV", raising=False)
    monkeypatch.delenv(MANAGER_TURN_ENV, raising=False)
    monkeypatch.setattr(owed, "watch_running", lambda: True)
    monkeypatch.setattr(supervision, "serve_running", lambda: False)
    return ppy_home


def _worker(conn, status: str, *, note: str | None = None) -> int:
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title="build")
    store.update_task_fields(conn, task_id, branch=f"ppy/task-{task_id}")
    store.set_task_status(conn, task_id, status)
    if note is not None:
        store.append_event(
            conn,
            kind="worker_progress",
            payload={"task_id": task_id, "phase": "done", "note": note},
            run_id=run_id,
            task_id=task_id,
        )
    return task_id


def _ticket(conn, task_id: int, phase: str) -> int:
    task = store.get_task(conn, task_id)
    ticket = store.add_task(conn, run_id=int(task["run_id"]), title="ticket")
    store.set_task_phase(conn, ticket, phase)
    store.set_task_env(
        conn, ticket, papaya_events.PAPAYA_EVENT_METADATA, json.dumps({"work_item_id": "w1"})
    )
    return ticket


def _followup(action: str, line: str = "the gate", message: str = "run the gate"):
    return lambda tid, *, stopped, detail="": supervision.GateFollowup(action, line, message)


# ── the owed lane: one decision ─────────────────────────────────────────────


def test_a_finished_worker_with_no_ticket_gets_the_review_turn(home) -> None:
    conn = init_db()
    task_id = _worker(conn, "worker_done", note="All green.")

    [decision] = lanes.owed_decisions(conn, now=NOW)

    assert (decision.task_id, decision.action, decision.turn) == (
        task_id,
        lanes.TURN,
        prompts.REVIEW,
    )
    assert not decision.failure and "All green." in decision.line


def test_a_question_gets_the_answer_turn_and_a_crash_the_review_turn_as_a_failure(home) -> None:
    conn = init_db()
    asked = _worker(conn, "blocked")
    crashed = _worker(conn, "failed")

    by_task = {d.task_id: d for d in lanes.owed_decisions(conn, now=NOW)}

    assert by_task[asked].turn == prompts.ANSWER
    assert by_task[crashed].turn == prompts.REVIEW and by_task[crashed].failure


def test_a_live_tickets_worker_a_covered_one_and_a_deferred_one_are_left_alone(home) -> None:
    conn = init_db()
    ticketed = _worker(conn, "worker_done")
    _ticket(conn, ticketed, "reviewing")
    covered = _worker(conn, "worker_done")
    deferred = _worker(conn, "worker_done")
    board.add("wait for the API decision", task_id=deferred, blocked_on="user", conn=conn)
    ended = _worker(conn, "worker_done")
    _ticket(conn, ended, "handed_over")
    noted = _worker(conn, "worker_done")
    board.add("review it", task_id=noted, conn=conn)  # an open next step is not a deferral

    found = {d.task_id for d in lanes.owed_decisions(conn, now=NOW, covered={covered})}

    assert found == {ended, noted}


def test_the_gate_follow_up_is_sent_back_twice_then_reviewed(home, monkeypatch) -> None:
    conn = init_db()
    task_id = _worker(conn, "worker_stopped")
    monkeypatch.setattr(supervision, "gate_followup", _followup(supervision.STEER, "red gate"))
    steered: list[tuple[int, str]] = []

    for _ in range(lanes.GATE_STEERS):
        [decision] = lanes.owed_decisions(conn, now=NOW)
        assert decision.action == lanes.STEER and decision.message == "run the gate"
        lanes.send_back(decision, steer=lambda t, m: steered.append((t, m)))
    [decision] = lanes.owed_decisions(conn, now=NOW)

    assert steered == [(task_id, "run the gate")] * 2
    assert decision.action == lanes.TURN and decision.turn == prompts.REVIEW


def test_a_persons_decision_is_recorded_once_and_lists_under_waiting(home, monkeypatch) -> None:
    conn = init_db()
    task_id = _worker(conn, "worker_stopped")
    monkeypatch.setattr(
        supervision, "gate_followup", _followup(supervision.PERSON, "red twice the same way")
    )

    [decision] = lanes.owed_decisions(conn, now=NOW)
    assert decision.action == lanes.PERSON
    lanes.hand_to_person(decision)

    assert lanes.owed_decisions(conn, now=NOW) == []
    [todo] = board.waiting(conn)
    assert todo["task_id"] == task_id and todo["blocked_on"] == "user"
    assert "red twice the same way" in todo["text"]


def test_a_turn_is_raised_again_only_when_something_moved_or_it_missed_or_waited(home) -> None:
    conn = init_db()
    task_id = _worker(conn, "worker_done")
    mark = watch.max_event_id(conn)

    lanes.record_task_turn(
        task_id,
        action=lanes.TURN,
        turn=prompts.REVIEW,
        outcome=lanes.ACTED,
        mark=mark,
        end_mark=mark,
    )
    assert lanes.owed_decisions(conn, now=NOW) == []
    # Something new on the record: the same worker is owed again.
    store.append_event(conn, kind="worker_progress", payload={"task_id": task_id}, task_id=task_id)
    assert [d.task_id for d in lanes.owed_decisions(conn, now=NOW)] == [task_id]

    # A miss is retried with the tail; two misses are a person's.
    end = watch.max_event_id(conn)
    lanes.record_task_turn(
        task_id,
        action=lanes.TURN,
        turn=prompts.REVIEW,
        outcome=lanes.MISSED,
        misses=1,
        mark=end,
        end_mark=end,
        tail="…ended without deciding",
    )
    [decision] = lanes.owed_decisions(conn, now=NOW)
    assert decision.tail == "…ended without deciding"
    lanes.record_task_turn(
        task_id,
        action=lanes.TURN,
        turn=prompts.REVIEW,
        outcome=lanes.MISSED,
        misses=2,
        mark=end,
        end_mark=end,
    )
    assert lanes.owed_decisions(conn, now=NOW) == []

    # A turn that said it is waiting is run again after the wait, not before.
    conn.execute("DELETE FROM events WHERE kind = ?", (lanes.TASK_TURN_EVENT,))
    conn.commit()
    lanes.record_task_turn(
        task_id,
        action=lanes.TURN,
        turn=prompts.REVIEW,
        outcome=lanes.WAITING,
        waits=1,
        mark=end,
        end_mark=end,
    )
    assert lanes.owed_decisions(conn, now=datetime.now(UTC)) == []
    assert [
        d.task_id for d in lanes.owed_decisions(conn, now=datetime.now(UTC) + timedelta(hours=1))
    ] == [task_id]


# ── the ledger lane ─────────────────────────────────────────────────────────


def _age_todo(conn, todo_id: int, minutes: float) -> None:
    stamp = (NOW - timedelta(minutes=minutes)).isoformat()
    conn.execute("UPDATE todos SET updated_at = ? WHERE id = ?", (stamp, todo_id))
    conn.commit()


def test_a_next_step_that_sat_is_due_and_a_fresh_or_blocked_one_is_not(home) -> None:
    conn = init_db()
    fresh = board.add("post PR #720 on its work item", conn=conn)
    sat = board.add("close the stale branch", conn=conn)
    blocked = board.add("merge once the requester agrees", blocked_on="user", conn=conn)
    _age_todo(conn, fresh, 5)
    _age_todo(conn, sat, 45)
    _age_todo(conn, blocked, 45)

    [item] = lanes.ledger_due(conn, now=NOW)

    assert item.todo_id == sat and item.seconds == 45 * 60
    assert "close the stale branch" in item.said()


def test_a_ticketless_workers_capability_request_gets_the_answer_turn_once(
    home, monkeypatch
) -> None:
    """2026-09-18: session-dispatched workers' `xcrun` requests sat undecided for 25 min."""
    from papaya_agent_runtime import capability_requests

    monkeypatch.setattr(tool_learning, "steer_worker", lambda _t, _m: None)
    conn = init_db()
    worker = _worker(conn, "in_progress")
    request = capability_requests.request(worker, "terraform", why="plan the stack")

    [decision] = [d for d in lanes.owed_decisions(conn, now=NOW) if d.task_id == worker]
    assert decision.action == lanes.TURN and decision.turn == prompts.ANSWER
    assert f"Capability request {request.id}" in decision.line
    assert lanes.task_facts(worker, decision)["the worker's question"] == decision.line

    # Put to the manager once: a turn recorded after the request settles it.
    lanes.record_task_turn(
        worker, action=lanes.TURN, turn=prompts.ANSWER, outcome=lanes.ACTED, mark=request.id
    )
    assert not [d for d in lanes.owed_decisions(conn, now=NOW) if d.task_id == worker]
    # A covered (held-ticket) worker is the ticket path's, not this lane's.
    other = _worker(conn, "in_progress")
    capability_requests.request(other, "terraform")
    assert not lanes.capability_decisions(conn, covered={other})


def test_a_step_waiting_on_a_task_that_ended_is_released_on_the_interval(home) -> None:
    """PAP-246 (2026-09-18): the Calendar layer waited on its delivered Gmail worker for good."""
    conn = init_db()
    delivered = _worker(conn, "delivered")
    running = _worker(conn, "in_progress")
    freed = board.add("brief the Calendar layer", blocked_on=f"task:{delivered}", conn=conn)
    held = board.add("review the other one", blocked_on=f"task:{running}", conn=conn)
    person = board.add("ask about the wording", blocked_on="user", conn=conn)

    lines = lanes.release_finished_waits(conn)

    assert lines == [
        f"todo #{freed} no longer waits on task {delivered} (delivered): its next step is due"
        " — brief the Calendar layer"
    ]
    assert store.get_todo(conn, freed)["blocked_on"] is None
    assert store.get_todo(conn, held)["blocked_on"] == f"task:{running}"
    assert store.get_todo(conn, person)["blocked_on"] == "user"
    assert lanes.release_finished_waits(conn) == []  # once
    _age_todo(conn, freed, 45)
    assert [i.todo_id for i in lanes.ledger_due(conn, now=NOW)] == [freed]


def test_a_review_step_waiting_on_its_own_finished_worker_lets_the_review_happen(home) -> None:
    """PAP-245 (2026-09-18): the step deferred the owed lane from a worker_done worker."""
    conn = init_db()
    worker = _worker(conn, "worker_done", note="done")
    step = board.add("review it", task_id=worker, blocked_on=f"task:{worker}", conn=conn)
    assert lanes._deferred(conn, worker)
    lanes.release_finished_waits(conn, now=NOW)
    assert store.get_todo(conn, step)["blocked_on"] is None
    assert not lanes._deferred(conn, worker)


def test_an_access_wait_is_tried_again_after_an_hour(home) -> None:
    conn = init_db()
    step = board.add("attach the frames to the ticket", blocked_on="access", conn=conn)
    _age_todo(conn, step, 30)
    assert lanes.release_finished_waits(conn, now=NOW) == []
    _age_todo(conn, step, 61)
    [line] = lanes.release_finished_waits(conn, now=NOW)
    assert "tried again after waiting on access" in line
    assert store.get_todo(conn, step)["blocked_on"] is None


def test_the_heartbeat_releases_finished_waits_too(home) -> None:
    conn = init_db()
    closed = _worker(conn, "closed")
    board.add("follow up", blocked_on=f"task:{closed}", conn=conn)
    lines, _turns = lanes.interactive_step(conn, NOW)
    assert any("no longer waits on task" in line for line in lines)


def test_a_ledger_turn_that_leaves_a_step_twice_hands_it_to_a_person(home) -> None:
    conn = init_db()
    todo_id = board.add("post PR #720 on its work item", conn=conn)
    _age_todo(conn, todo_id, 45)
    [item] = lanes.ledger_due(conn, now=NOW)

    lanes.record_ledger_turn([item], conn=conn, now=NOW)
    # Left once: not raised again until the retry, and open still.
    assert lanes.ledger_due(conn, now=NOW) == []
    assert store.get_todo(conn, todo_id)["blocked_on"] is None
    later = NOW + timedelta(seconds=lanes.LEDGER_RETRY_SECONDS)
    [again] = lanes.ledger_due(conn, now=later)
    assert again.todo_id == todo_id

    lanes.record_ledger_turn([again], conn=conn, now=later)
    row = store.get_todo(conn, todo_id)
    assert row["status"] == "open" and row["blocked_on"].startswith("user:")
    assert lanes.ledger_due(conn, now=later + timedelta(days=1)) == []

    # A step the turn touched (edited, done) is not a miss.
    other = board.add("read the plan", conn=conn)
    _age_todo(conn, other, 45)
    [item] = lanes.ledger_due(conn, now=NOW)
    board.done(other, conn=conn)
    lanes.record_ledger_turn([item], conn=conn, now=NOW)
    assert lanes.last_ledger_turn(conn)["left"] == {}


# ── the deficiency lane ─────────────────────────────────────────────────────


def test_the_deficiency_lane_groups_then_flushes_and_never_raises(home) -> None:
    """Grouping runs on this path too, or a session opens a second issue for one cause."""

    class Reporter:
        def __init__(self) -> None:
            self.flushed = 0
            self.merged = 0

        def merge_duplicates(self) -> list[str]:
            self.merged += 1
            return ["closed https://github.com/o/r/issues/8 as a duplicate of #7"]

        def flush(self) -> list[str]:
            self.flushed += 1
            return ["opened https://github.com/o/r/issues/9"]

    reporter = Reporter()
    assert lanes.deficiency_step(reporter) == [
        "closed https://github.com/o/r/issues/8 as a duplicate of #7",
        "opened https://github.com/o/r/issues/9",
    ]
    assert (reporter.merged, reporter.flushed) == (1, 1)

    class Broken:
        def merge_duplicates(self) -> list[str]:
            return []

        def flush(self) -> list[str]:
            raise RuntimeError("gh is signed out")

    assert lanes.deficiency_step(Broken()) == [
        "could not open deficiencies as issues: gh is signed out"
    ]


# ── an interactive session: the heartbeat and the hooks ─────────────────────


def test_the_heartbeat_acts_mechanically_and_names_the_turns_only_a_session_can_take(
    home, monkeypatch
) -> None:
    conn = init_db()
    done = _worker(conn, "worker_done", note="All green.")
    stopped = _worker(conn, "worker_stopped")
    monkeypatch.setattr(
        supervision,
        "gate_followup",
        lambda tid, *, stopped, detail="": supervision.GateFollowup(
            supervision.STEER if stopped else supervision.REVIEW, "red gate", "run the gate"
        ),
    )
    steered: list[int] = []

    acted, turns = lanes.interactive_step(conn, NOW, steer=lambda t, m: steered.append(t))

    assert steered == [stopped]
    assert acted == [f"worker task {stopped} sent back to its gate: red gate"]
    assert [(d.task_id, d.turn) for d in turns] == [(done, prompts.REVIEW)]

    line = watch.render(watch.tick(conn, turns=turns, repairs=acted))
    assert f"your turn: t{done} review (All green." in line
    assert f"repair: worker task {stopped} sent back to its gate" in line

    # With a serve running on this machine the heartbeat leaves the lane to it.
    monkeypatch.setattr(supervision, "serve_running", lambda: True)
    assert lanes.interactive_step(conn, NOW, steer=lambda t, m: steered.append(t)) == ([], [])
    assert steered == [stopped]


def test_the_heartbeat_names_next_steps_that_sat_and_stays_awake_for_them(home) -> None:
    conn = init_db()
    todo_id = board.add("post PR #720 on its work item", conn=conn)
    _age_todo(conn, todo_id, 45)

    snapshot = watch.tick(conn, now=NOW)

    assert snapshot["ledger_due"][0]["todo_id"] == todo_id
    assert not snapshot["idle"]
    assert f"ledger due: #{todo_id} post PR #720 on its work item" in watch.render(snapshot)


def test_the_heartbeat_loop_runs_the_owed_lane_only_when_no_serve_does(home, monkeypatch) -> None:
    conn = init_db()
    done = _worker(conn, "worker_done")
    calls: list[datetime] = []

    def owed_step(_conn, now):
        calls.append(now)
        return ["acted"], list(lanes.owed_decisions(_conn, now=now))

    monkeypatch.setattr(watch, "owed_step", owed_step)
    monkeypatch.setattr(watch, "repair_step", lambda conn, now: [])
    monkeypatch.setattr(watch, "listen_step", lambda now: [])
    monkeypatch.setattr(watch, "merge_step", lambda conn, now: [])
    monkeypatch.setattr(watch, "UpkeepStep", lambda: lambda now: [])
    import io

    out = io.StringIO()

    def sleep(_seconds):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        watch.run(1, out=out, sleep=sleep)

    assert len(calls) == 1
    assert f"your turn: t{done} review" in out.getvalue() and "repair: acted" in out.getvalue()


def test_a_session_turn_does_not_end_while_a_turn_or_a_next_step_is_its_to_take(home) -> None:
    conn = init_db()
    done = _worker(conn, "worker_done")
    todo_id = board.add("post PR #720 on its work item", conn=conn)
    _age_todo(conn, todo_id, 45)

    blocked = hooks.handle_hook_stdin("Stop", "{}")

    assert blocked["decision"] == "block"
    assert f"worker task {done} worker_done: needs the review turn" in blocked["reason"]
    assert "ppy todo block <id> --on user:<why>|task:<id>|review" in blocked["reason"]
    assert f'todo #{todo_id} "post PR #720 on its work item"' in blocked["reason"]

    # Deferred with reasons, both are a person's or another task's now.
    board.add("wait for the requester", task_id=done, blocked_on="user", conn=conn)
    board.block(todo_id, "task:1", conn=conn)
    # The decision waiting on a person is chased through Papaya (`outreach`); with this
    # machine not connected, the turn is held once so the ask goes in the reply.
    held = hooks.handle_hook_stdin("Stop", "{}")
    assert held["decision"] == "block" and "waiting on a person" in held["reason"]
    assert "decision" not in hooks.handle_hook_stdin("Stop", "{}")


def test_a_session_is_not_held_for_what_a_running_serve_takes_up(home, monkeypatch) -> None:
    conn = init_db()
    _worker(conn, "worker_done")
    board.add("watch serve review it", conn=conn)  # the ledger gate, satisfied as before
    monkeypatch.setattr(supervision, "serve_running", lambda: True)

    assert "decision" not in hooks.handle_hook_stdin("Stop", "{}")


def test_session_start_lists_the_next_steps_that_sat(home, monkeypatch) -> None:
    monkeypatch.setattr(hooks, "readiness_context", lambda: None)
    conn = init_db()
    todo_id = board.add("post PR #720 on its work item", conn=conn)
    _age_todo(conn, todo_id, 45)

    context = hooks.handle_hook_stdin("SessionStart", "{}")["hookSpecificOutput"][
        "additionalContext"
    ]

    assert "NEXT STEPS THAT SAT (1)" in context
    assert f'todo #{todo_id} "post PR #720 on its work item"' in context


def test_the_heartbeat_prints_one_tick_when_piped_unless_told_to_follow(home, capsys) -> None:
    # Under pytest stdout is not a terminal: an agent calling `ppy watch` as a tool
    # call gets one tick, not a five-minute cadence until its tool timeout.
    assert main(["watch", "--interval", "0"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 1
    assert main(["watch", "--interval", "0", "--follow", "--exit-when-idle"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 2


# ── `ppy serve`: turns keyed on a task ──────────────────────────────────────


def _config() -> MMConfig:
    return MMConfig(
        manager=ManagerProfile(provider="claude", model=None, reasoning=None),
        worker=WorkerCeiling(provider="claude", max_model="opus", max_reasoning="medium"),
    )


class FakeTurns:
    """The harness: `act` does to the ledger what the turn's `ppy` calls would."""

    def __init__(self, act=None) -> None:
        self._act = act or (lambda launch: None)
        self.prompts: list[str] = []
        self.transcripts: list[str] = []

    def __call__(self, launch: Any, *, should_stop, transcript_path=None) -> TurnResult:
        self.prompts.append(launch.seed_prompt)
        self.transcripts.append(str(transcript_path))
        said = self._act(launch)
        return TurnResult(exit_code=0, transcript=f"turn #{len(self.prompts)}\n{said or ''}")


def _runner(turns: FakeTurns, tmp_path, **kwargs: Any) -> lanes.TurnRunner:
    return lanes.TurnRunner(
        run_turn=turns, config=_config, runtime_dir=str(tmp_path), clock=lambda: NOW, **kwargs
    )


def _deliver(task_id: int) -> None:
    conn = init_db()
    try:
        store.set_task_status(conn, task_id, "delivered")
        store.append_event(conn, kind="delivered", payload={"task_id": task_id}, task_id=task_id)
    finally:
        conn.close()


async def _drain(runner: lanes.TurnRunner) -> None:
    for task in [*runner.running.values(), runner._ledger]:
        if task is not None:
            await task


def test_serve_reviews_and_delivers_a_finished_worker_that_has_no_ticket(home, tmp_path) -> None:
    conn = init_db()
    task_id = _worker(conn, "worker_done", note="All green; two files.")
    board.add("review it", task_id=task_id, conn=conn)
    turns = FakeTurns(lambda launch: _deliver(task_id))
    runner = _runner(turns, tmp_path)

    async def scenario() -> list[str]:
        lines = await runner.take_up(lanes.owed_decisions(conn, now=NOW))
        await _drain(runner)
        return lines

    assert asyncio.run(scenario()) == [f"worker task {task_id}: running the review turn"]
    [prompt] = turns.prompts
    assert prompt.startswith(prompts.load(prompts.REVIEW).splitlines()[0])
    facts = prompt.split(prompts.FACTS_HEADING, 1)[1]
    assert "- held work item: none" in facts and f"- worker task id: {task_id}" in facts
    assert "[done] All green; two files." in facts
    assert "the worker's commits to review" not in facts  # no worktree: nothing to diff
    record = lanes.last_turn(init_db(), task_id)
    assert record["outcome"] == lanes.DELIVERED and record["turn"] == prompts.REVIEW
    assert runner.running == {}
    # Delivered: owed no more, and the turn's transcript is under the worker's run.
    assert lanes.owed_decisions(init_db(), now=NOW) == []
    run_id = int(store.get_task(init_db(), task_id)["run_id"])
    assert turns.transcripts == [str(home / "runs" / str(run_id) / "turns" / "review-1.log")]


def test_a_task_turn_that_misses_twice_hands_the_worker_to_a_person(home, tmp_path) -> None:
    conn = init_db()
    task_id = _worker(conn, "blocked")
    store.append_event(
        conn,
        kind="question",
        payload={"task_id": task_id, "question": "v1 or v2?"},
        task_id=task_id,
    )
    turns = FakeTurns()
    runner = _runner(turns, tmp_path)

    async def scenario() -> None:
        for _ in range(2):
            await runner.take_up(lanes.owed_decisions(init_db(), now=NOW))
            await _drain(runner)

    asyncio.run(scenario())
    assert len(turns.prompts) == 2
    assert "- the worker's question: v1 or v2?" in turns.prompts[0]
    assert "previous attempt's transcript (tail)" in turns.prompts[1]
    conn = init_db()
    assert lanes.last_turn(conn, task_id)["misses"] == 2
    [todo] = board.waiting(conn)
    assert todo["task_id"] == task_id and "answering or steering" in todo["text"]
    assert lanes.owed_decisions(conn, now=NOW) == []


def test_the_ledger_turn_carries_the_steps_that_sat_and_records_what_it_left(
    home, tmp_path
) -> None:
    conn = init_db()
    done_id = board.add("post PR #720 on its work item", conn=conn)
    left_id = board.add("close the stale branch", conn=conn)
    _age_todo(conn, done_id, 45)
    _age_todo(conn, left_id, 45)
    turns = FakeTurns(lambda launch: board.done(done_id))
    runner = _runner(turns, tmp_path)

    async def scenario() -> list[str]:
        lines = await runner.take_up_ledger(lanes.ledger_due(conn, now=NOW))
        await _drain(runner)
        return lines

    assert asyncio.run(scenario()) == ["running the ledger turn for 2 next step(s)"]
    [prompt] = turns.prompts
    assert prompt.startswith(prompts.load(prompts.LEDGER).splitlines()[0])
    assert f"- todo #{done_id} (waiting 45m)" in prompt and f"todo #{left_id}" in prompt
    conn = init_db()
    assert set(lanes.last_ledger_turn(conn)["left"]) == {str(left_id)}
    assert lanes.ledger_due(conn, now=NOW) == []


def test_a_round_runs_the_three_lanes_after_its_held_tickets(home, tmp_path, monkeypatch) -> None:
    """`Rounds._round` reaches the lanes; the seams are the runner's, the reporter serve's."""
    conn = init_db()
    task_id = _worker(conn, "worker_done")
    todo_id = board.add("close the stale branch", conn=conn)
    _age_todo(conn, todo_id, 45)
    turns = FakeTurns(
        lambda launch: (
            _deliver(task_id)
            if prompts.load(prompts.REVIEW).splitlines()[0] in launch.seed_prompt
            else None
        )
    )

    class Reporter:
        flushed = 0

        def merge_duplicates(self) -> list[str]:
            return []

        def flush(self) -> list[str]:
            Reporter.flushed += 1
            return []

    class Runner:
        held: dict = {}
        _run_turn = turns
        _turn_tools = None
        _config = staticmethod(_config)
        _runtime_dir = str(tmp_path)

    built = type("Built", (), {"standalone": True})()
    manager_rounds = rounds.Rounds(
        built,
        Runner(),
        forge=lambda _conn: [],
        clock=lambda: NOW,
        prune=lambda _task_id: {"removed": [], "skipped": [], "reclaimed_bytes": 0},
        git=lambda *_a, **_k: 0,
        reporter=Reporter(),
    )

    async def scenario() -> list[str]:
        parts = await manager_rounds.round_once()
        await _drain(manager_rounds._turns)
        await manager_rounds.close()
        return parts

    parts = asyncio.run(scenario())
    assert f"worker task {task_id}: running the review turn" in parts
    assert "running the ledger turn for 1 next step(s)" in parts
    assert Reporter.flushed == 1
    assert store.get_task(init_db(), task_id)["status"] == "delivered"


def test_the_gate_verdict_line_reaches_a_stopped_workers_review_turn(
    home, tmp_path, monkeypatch
) -> None:
    conn = init_db()
    task_id = _worker(conn, "worker_stopped")
    store.append_event(
        conn,
        kind="worker_stopped",
        payload={"task_id": task_id, "summary": "worker stopped before done: session ended"},
        task_id=task_id,
    )
    result = gate.GateResult(
        repo="app",
        command="make test",
        full=False,
        exit_code=0,
        duration_seconds=3.0,
        summary="10 passed",
        head_sha="abc123",
        output_path="/tmp/g.txt",
        started_at="",
        finished_at="",
        failing_tests=[],
    )
    monkeypatch.setattr(
        gate, "verdict", lambda tid, **_k: gate.Verdict(gate.GREEN, "abc123", result)
    )
    monkeypatch.setattr(
        supervision, "gate_followup", _followup(supervision.REVIEW, "green at abc123")
    )
    turns = FakeTurns()
    runner = _runner(turns, tmp_path)

    async def scenario() -> None:
        await runner.take_up(lanes.owed_decisions(init_db(), now=NOW))
        await _drain(runner)

    asyncio.run(scenario())
    [prompt] = turns.prompts
    assert "- what stopped the worker: worker stopped before done: session ended" in prompt
    assert "- the worker's recorded gate at its head: " in prompt and "10 passed" in prompt
