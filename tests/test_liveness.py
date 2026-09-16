"""A busy worker is not a stalled job: `ppy serve` says what a held ticket's worker is doing.

PAP-219: the client called a job stalled, and handed it back, while its worker had sent
a tool heartbeat every 30 seconds for 32 minutes and was mid-`make verify`. None of that
reached the client, whose stall clock hears only progress lines. These tests drive the
client's own `ListenerLoop` and `Job` — its renew cadence fired by hand, its activity
clock injected — so what is shown is the client's stall rule seeing the runtime's lines,
not a copy of that rule.
"""

from __future__ import annotations

import asyncio
import io
import os
import time
from typing import Any

import pytest

import test_rounds
import test_serve
from conftest import scale
from papaya_agent_runtime import prompts, rounds, serve
from papaya_agent_runtime.state.db import init_db
from test_rounds import Timer, WallClock, _seams, live_session, working_worker
from test_rounds import _serve as _serve_with_rounds
from test_serve import (
    EVENT,
    SUBJECT,
    Clock,
    FakeEvents,
    FakePapaya,
    FakeTurns,
    Harness,
    Host,
    Turn,
    _deliver,
    _end_hold,
    _runner,
    _serve_ticket,
    _until,
    dispatch_worker,
    history,
    ticket_task,
    worker_event,
    workers_in,
)

# Fixtures from the serve and rounds tests, bound by name (see `tests/test_rounds.py`).
globals().update(
    {
        name: getattr(test_serve, name)
        for name in ("assigned", "client_home", "progress_lines", "ready", "registered_repo")
    }
)
globals().update({"pruned": test_rounds.pruned})

MINUTE = 60.0


def tool_call(worker: int, tool_use_id: str, command: str) -> None:
    """The worker starting a shell command: Claude's `assistant` message with a `tool_use`."""
    worker_event(
        worker,
        "worker_assistant",
        type="assistant",
        message={
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Bash",
                    "input": {"command": command},
                }
            ]
        },
    )


def heartbeat(worker: int, tool_use_id: str, elapsed: float) -> None:
    """Claude's `tool_progress`: the call is still running, this long in."""
    worker_event(
        worker,
        "worker_tool_progress",
        type="tool_progress",
        tool_use_id=tool_use_id,
        tool_name="Bash",
        elapsed_time_seconds=elapsed,
    )


def liveness(lines: list[tuple[str, str, str]]) -> list[str]:
    return [
        detail
        for _subject, _phase, detail in lines
        if " active: " in detail or detail.startswith("Gate running")
    ]


def _worker(run_id: int) -> int:
    (worker,) = workers_in(run_id)
    return worker


async def _twelve_minutes(clock: Clock, every_minute, lines, due: dict[int, int]) -> None:
    """Move the runner's clock a minute at a time, waiting for each line that is due."""
    for minute in range(1, 13):
        every_minute(minute)
        clock.advance(MINUTE)
        if minute in due:
            await _until(
                lambda count=due[minute]: len(liveness(lines)) >= count,
                what=f"the line at {minute} min",
            )
        else:
            await asyncio.sleep(scale(0.03))
    await asyncio.sleep(scale(0.1))


def test_worker_heartbeats_over_twelve_minutes_make_exactly_two_liveness_lines(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    def act(turn: Turn) -> None:
        if turn.name == prompts.BRIEF:
            worker = dispatch_worker(turn.run_id)
            live_session(worker)
            # The suite twice already; this is the third time it runs `make verify`.
            tool_call(worker, "toolu_1", "make verify")
            tool_call(worker, "toolu_2", "make verify")
            tool_call(worker, "toolu_3", "make verify")

    clock, turns = Clock(), FakeTurns(act)
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, FakePapaya(), clock=clock))
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        worker = _worker(int(ticket_task()["run_id"]))
        # A heartbeat every 30 seconds, as Claude sends them.
        await _twelve_minutes(
            clock,
            lambda m: (
                heartbeat(worker, "toolu_3", m * MINUTE - 30),
                heartbeat(worker, "toolu_3", m * MINUTE),
            ),
            progress_lines,
            {5: 1, 10: 2},
        )
        return await _end_hold(harness, runner)

    assert asyncio.run(scenario()) == 0
    worker = _worker(int(ticket_task()["run_id"]))
    assert liveness(progress_lines) == [
        f"Worker task {worker} active: `make verify`, 5 min in, third run",
        f"Worker task {worker} active: `make verify`, 10 min in, third run",
    ]


def test_a_live_worker_with_no_events_for_twelve_minutes_reports_nothing(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    def act(turn: Turn) -> None:
        if turn.name == prompts.BRIEF:
            live_session(dispatch_worker(turn.run_id))

    clock, turns = Clock(), FakeTurns(act)
    harness = Harness(FakeEvents([EVENT]))
    gate_checks: list[int] = []

    def gate_state(task_id: int) -> rounds.GateState:
        gate_checks.append(task_id)
        return rounds.GateState(False, "no gate running")

    async def scenario() -> int:
        runner = _serve_ticket(
            harness,
            client_home,
            _runner(turns, FakePapaya(), clock=clock, gate_state=gate_state),
        )
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        await _twelve_minutes(clock, lambda _m: None, progress_lines, {})
        return await _end_hold(harness, runner)

    assert asyncio.run(scenario()) == 0
    # It looked, twice, and found silence both times: silence is what it said.
    assert len(gate_checks) == 2
    assert liveness(progress_lines) == []


def test_a_gate_running_under_the_supervisor_with_no_worker_events_makes_the_gate_line(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    def gate_state(_task_id: int) -> rounds.GateState:
        return rounds.GateState(
            True,
            "running under the supervisor for 8m: `make verify`",
            command="make verify",
            elapsed_seconds=8 * MINUTE,
            full=True,
        )

    clock, turns = Clock(), FakeTurns(test_serve._brief_dispatches)
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = _serve_ticket(
            harness,
            client_home,
            _runner(turns, FakePapaya(), clock=clock, gate_state=gate_state),
        )
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        clock.advance(5 * MINUTE)
        await _until(lambda: liveness(progress_lines), what="the gate line")
        return await _end_hold(harness, runner)

    assert asyncio.run(scenario()) == 0
    assert liveness(progress_lines) == [
        "Gate running under the supervisor: full suite `make verify`, 8 min"
    ]


class ActivityClock:
    """The client's activity clock: real time, moved forward by hand."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


def test_a_stall_reported_while_the_worker_is_busy_is_answered_with_one_line_and_clears(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    """The client's own stall rule, over the supervised protocol, hearing the runtime's line."""
    stdout_read, stdout_write = os.pipe()
    stdin_read, stdin_write = os.pipe()
    stdout = os.fdopen(stdout_write, "w", buffering=1)
    host = Host(stdout_read, stdin_write)
    host.start()

    def act(turn: Turn) -> None:
        if turn.name == prompts.BRIEF:
            worker = dispatch_worker(turn.run_id)
            live_session(worker)
            tool_call(worker, "toolu_9", "make verify")
            heartbeat(worker, "toolu_9", 45.0)

    activity_clock = ActivityClock()
    harness = Harness(FakeEvents([EVENT]))

    def loop_factory(events: Any, **kwargs: Any) -> Any:
        kwargs.update(
            stall_after=30 * MINUTE, stall_grace=10 * MINUTE, activity_clock=activity_clock
        )
        return harness._build_loop(events, **kwargs)

    options = serve.parse_args(
        [
            "--supervised",
            "--working-directory",
            str(client_home.work_dir),
            "--approval-timeout",
            "30",
        ]
    )
    extra = {**harness.extra(), "loop_factory": loop_factory, "stdin_fd": stdin_read}
    turns = FakeTurns(act)

    async def scenario() -> tuple[float, float]:
        runner = asyncio.create_task(
            serve.run(
                options,
                stdout=stdout,
                stderr=io.StringIO(),
                extra=extra,
                runner=_runner(turns, FakePapaya(), clock=Clock()),
            )
        )
        # The dispatch line touches the stamp itself, so wait for it before ageing the stamp.
        await _until(
            lambda: any(d.startswith("Dispatched worker task") for _s, _p, d in progress_lines),
            what="the dispatch line",
        )
        await asyncio.sleep(scale(0.1))
        job = harness.jobs[0]
        # Thirty-one quiet minutes on the stamp the client reads, as PAP-219 had.
        quiet_since = time.time() - 31 * MINUTE
        os.utime(job.activity_file, (quiet_since, quiet_since))
        harness.ticks.tick()
        await _until(lambda: host.of_type("job.stalled"), what="the client to call it stalled")
        await _until(lambda: liveness(progress_lines), what="the answer to the stall")
        stamped = job.activity_file.stat().st_mtime
        # The next renew tick reads the stamp the line moved: the stall is over.
        harness.ticks.tick()
        await _until(lambda: host.of_type("job.stall_cleared"), what="the stall to clear")
        # And past the grace period, nothing is handed back.
        activity_clock.offset = 11 * MINUTE
        harness.ticks.tick()
        await _until(lambda: harness.ticks.count >= 3, what="the tick past the grace")
        await asyncio.sleep(scale(0.1))
        assert harness.events.hand_backs == []
        os.close(stdin_write)
        await runner
        return quiet_since, stamped

    try:
        quiet_since, stamped = asyncio.run(scenario())
    finally:
        stdout.close()
        host.join(timeout=scale(5.0))

    assert stamped > quiet_since + 30 * MINUTE, "the line did not move the client's stall clock"
    worker = _worker(int(ticket_task()["run_id"]))
    assert liveness(progress_lines) == [f"Worker task {worker} active: `make verify`, 45 s in"]
    assert len(host.of_type("job.stalled")) == 1
    assert all(m.get("outcome") != "stalled" for m in host.of_type("job.finished"))


# ── a hand-back while the worker's work goes on ────────────────────────────


def _session_ended(worker: int) -> None:
    conn = init_db()
    try:
        conn.execute(
            "UPDATE runners SET pid = NULL, status = 'exited' WHERE task_id = ?", (worker,)
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("state", "resumes"),
    [
        ("live", serve.PHASE_DISPATCHED),
        ("worker_done", serve.PHASE_REVIEWING),
        ("worker_stopped", serve.PHASE_DISPATCHED),
    ],
)
def test_a_stalled_hand_back_keeps_the_worker_and_the_next_round_resumes_without_a_dispatch(
    ppy_home,
    client_home,
    ready,
    registered_repo,
    assigned,
    pruned,
    progress_lines,
    monkeypatch,
    state,
    resumes,
) -> None:
    """PAP-219 at 18:23: handed back stalled with worker task 9 alive in `make verify`."""
    from papaya_agent_client.listener import STOP_STALLED

    monkeypatch.setattr(rounds, "branch_ahead_of_base", lambda _task_id: True)
    steers: list[tuple[int, str]] = []

    def act(turn: Turn) -> None:
        if turn.name == prompts.BRIEF:
            working_worker(turn.run_id, note="Running make verify.")
        elif turn.name == prompts.REVIEW:
            _deliver(turn)

    turns, timer, clock, papaya_api = FakeTurns(act), Timer(), WallClock(), FakePapaya()
    harness = Harness(FakeEvents([EVENT]))
    runner = _runner(turns, papaya_api, steer=lambda t, m: steers.append((t, m)))

    async def scenario() -> int:
        task = _serve_with_rounds(harness, client_home, runner, _seams(timer, clock, pruned))
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        await _until(lambda: timer.waiting, what="the rounds to go to sleep")
        worker = _worker(int(ticket_task()["run_id"]))

        await harness.loop.hand_back(SUBJECT, stop_reason=STOP_STALLED)
        await _until(lambda: harness.results, what="the stalled hold to end")
        assert history()[-1] == serve.PHASE_STALLED
        assert serve.worker_session_live(worker), "the hand-back ended the worker"

        if state == "worker_done":
            _session_ended(worker)
            worker_event(worker, "worker_done", status="worker_done", summary="Gate green.")
        elif state == "worker_stopped":
            _session_ended(worker)
            worker_event(
                worker, serve.WORKER_STOPPED, status=serve.WORKER_STOPPED, summary="ended mid-gate"
            )

        await timer.round()
        ticket = int(ticket_task()["id"])
        await _until(
            lambda: (SUBJECT, resumes, f"Resuming task {ticket} from {resumes}.") in progress_lines,
            what="the reclaimed hold to resume",
        )
        if state == "worker_done":
            await _until(lambda: len(harness.results) >= 2, what="the review to deliver")
        elif state == "worker_stopped":
            await _until(lambda: steers, what="the stopped worker's gate steer")
        else:
            await asyncio.sleep(scale(0.2))
        harness.loop.request_stop()
        assert await task == 0
        return worker

    worker = asyncio.run(scenario())

    run_id = int(ticket_task()["run_id"])
    assert workers_in(run_id) == [worker], "a new worker was dispatched"
    expected = [prompts.BRIEF, prompts.REVIEW] if state == "worker_done" else [prompts.BRIEF]
    assert turns.names() == expected, "a new brief ran"
    assert len(harness.jobs) == 2
    session = serve.stored_session_ids()[test_serve.CONNECTION_ID]
    assert [s for s in harness.events.reserves if s[0] == SUBJECT][-1] == (SUBJECT, session)
    # Nothing on the item said it was handed back, and it was never put back to todo.
    assert not any(body.startswith("handed back") for _i, body in papaya_api.comments())
    assert ("item-9", "todo") not in papaya_api.statuses()
    if state == "worker_stopped":
        assert steers[0][0] == worker


def test_a_stalled_ticket_whose_worker_is_gone_is_not_reclaimable(ppy_home, monkeypatch) -> None:
    monkeypatch.setattr(rounds, "branch_ahead_of_base", lambda _task_id: False)
    ticket, _run_id, worker = test_rounds.seed_ticket(
        [serve.PHASE_PICKED_UP, serve.PHASE_DISPATCHED, serve.PHASE_STALLED],
        worker_status=serve.WORKER_STOPPED,
    )
    conn = init_db()
    try:
        assert serve.resumable_phase(conn, ticket) is None
        live_session(worker)
        assert serve.resumable_phase(conn, ticket) == serve.PHASE_DISPATCHED
    finally:
        conn.close()
