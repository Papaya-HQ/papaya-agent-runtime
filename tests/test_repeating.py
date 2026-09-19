"""A ticket that keeps coming back with nothing to show: said once, parked, and surfaced.

PAP-210, 2026-09-19: an `in_progress` item whose brief turn found nothing to build
(the fix was on staging, waiting on QA) was offered by every sweep, picked up as a
new task each time, and got "Picked up; choosing the repository and writing the
brief." eleven times in an hour. Nothing noticed. These tests hold the three layers
of the fix: the ending is remembered (parked), the line is said once per assignment
of the work item, and repetition is a deficiency a person sees in `needs attention`.
"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import test_serve
from papaya_agent_runtime import cli, deficiencies, prompts, serve, sweep, team
from papaya_agent_runtime.state import store
from papaya_agent_runtime.state.db import init_db
from test_serve import (
    FakeEvents,
    FakePapaya,
    FakeTurns,
    Harness,
    Turn,
    _runner,
    _serve_ticket,
    _serving,
    _summaries,
    _until,
    dispatch_worker,
    history,
)

globals().update(
    {
        name: getattr(test_serve, name)
        for name in ("assigned", "client_home", "ready", "registered_repo")
    }
)

ITEM = "item-9"
KEY = "PAP-210"
PICKUP_LINE = "Picked up; choosing the repository and writing the brief."
NOTHING = "Checked the item.\n\nNOTHING TO BUILD: the fix is on staging; waiting on QA's recheck"


def _event(event_id: int, item: str = ITEM) -> dict[str, Any]:
    """An assignment carrying the item's display id, the way Papaya's summary does."""
    event = test_serve._assigned(event_id, item, "Radar QA")
    event["payload"]["work_item"]["short_id"] = KEY
    return event


def _nothing_to_build(turn: Turn) -> str | None:
    return NOTHING if turn.name == prompts.BRIEF else None


def _pickup_lines(papaya_api: FakePapaya, item: str = ITEM) -> list[str]:
    return [body for i, body in papaya_api.comments() if i == item and body == PICKUP_LINE]


def _repeating() -> list[deficiencies.Deficiency]:
    return [
        d
        for d in deficiencies.ledger(include_all=True)
        if d.kind == deficiencies.REPEATED_WITHOUT_PROGRESS
    ]


def _ticket_tasks(item: str = ITEM) -> list[int]:
    conn = init_db()
    try:
        rows = conn.execute(
            "SELECT tasks.id FROM tasks JOIN task_env ON task_env.task_id = tasks.id "
            "WHERE task_env.key = 'papaya_event_metadata' "
            "AND json_extract(task_env.value, '$.work_item_id') = ? ORDER BY tasks.id",
            (item,),
        ).fetchall()
        return [int(row["id"]) for row in rows]
    finally:
        conn.close()


async def _one_after_another(harness: Harness, events: list[dict[str, Any]]) -> None:
    """Deliver each event only once the hold before it has ended: pickup after pickup."""
    for n, event in enumerate(events, start=1):
        harness.events._queue.append(event)
        await _until(lambda n=n: len(harness.results) == n, what=f"hold {n} to end")


# ── G2: said once per assignment of the work item ───────────────────────────


def test_a_ticket_re_offered_after_a_restart_says_the_pickup_line_once(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """Two processes, two tasks for one item: the second pickup is not news."""
    papaya_api = FakePapaya()

    async def serve_once(event: dict[str, Any]) -> int:
        harness = Harness(FakeEvents([event]))
        runner = _serve_ticket(
            harness, client_home, _runner(FakeTurns(_nothing_to_build), papaya_api)
        )
        await _until(lambda: harness.results, what="the hold to end")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(serve_once(_event(101))) == 0
    # The restart: a new process, a new offer of the same item (a new event key).
    assert asyncio.run(serve_once(_event(102))) == 0

    assert len(_ticket_tasks()) == 2, "the second pickup was not a new task"
    assert _pickup_lines(papaya_api) == [PICKUP_LINE]


def test_a_ticket_handed_back_then_assigned_again_announces_the_new_assignment(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """A hand-back ends the assignment; being given the work again is news."""
    briefs: list[Turn] = []

    def act(turn: Turn) -> None:
        if turn.name != prompts.BRIEF:
            return
        briefs.append(turn)
        # The first assignment's two brief turns dispatch nothing: handed back.
        if len(briefs) > 2:
            dispatch_worker(turn.run_id)

    papaya_api = FakePapaya()
    harness = Harness(FakeEvents([]))

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(FakeTurns(act), papaya_api))
        await _one_after_another(harness, [_event(101)])
        assert serve.PHASE_DECLINED in history(101)
        harness.events._queue.append(_event(102))
        await _until(lambda: serve.PHASE_DISPATCHED in history(102), what="the new dispatch")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0
    assert _pickup_lines(papaya_api) == [PICKUP_LINE, PICKUP_LINE]


def test_two_sweeps_racing_an_event_on_one_subject_make_one_hold_and_one_comment(
    ppy_home, client_home, ready, registered_repo, assigned
) -> None:
    """The pulled event, the start sweep and a timed sweep all reach the same item."""
    assigned.items = [{"id": ITEM, "title": "Radar QA", "status": "todo", "short_id": KEY}]
    papaya_api = FakePapaya()
    harness = Harness(FakeEvents([_event(101)]))
    stderr = io.StringIO()
    clock = test_serve.Ticks()
    runner_ = _runner(FakeTurns(lambda turn: dispatch_worker(turn.run_id)), papaya_api)

    async def scenario() -> int:
        runner = await _serving(
            harness, client_home, stderr, sweep_sleep=clock.sleep, runner=runner_
        )
        await _until(lambda: _summaries(stderr), what="the start sweep")
        clock.tick()
        # A repeated identical summary is throttled; the listing call says it swept.
        await _until(lambda: assigned.calls >= 2, what="the timed sweep")
        await _until(lambda: _pickup_lines(papaya_api), what="the pickup line")
        await asyncio.sleep(0.2)
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0
    assert len(harness.jobs) == 1
    assert len(_ticket_tasks()) == 1
    assert _pickup_lines(papaya_api) == [PICKUP_LINE]


# ── G3: repetition is a deficiency, once ────────────────────────────────────


def test_five_pickups_with_nothing_to_show_are_one_deficiency_once_and_one_comment(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """Picked up again and again, each brief finding nothing: one ledger row, count one."""
    papaya_api = FakePapaya()
    harness = Harness(FakeEvents([]))

    async def scenario() -> int:
        runner = _serve_ticket(
            harness, client_home, _runner(FakeTurns(_nothing_to_build), papaya_api)
        )
        await _one_after_another(harness, [_event(101 + n) for n in range(5)])
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0
    assert len(_ticket_tasks()) == 5
    [row] = _repeating()
    assert row.detail == serve.picked_up_detail(KEY, serve.PHASE_REPORTED)
    assert row.count == 1, "recorded once per repetition, not once per fingerprint"
    assert (row.evidence[0]["ticket"], row.evidence[0]["times"]) == (KEY, 3)
    assert row.evidence[0]["code"] == serve.PHASE_REPORTED
    assert _pickup_lines(papaya_api) == [PICKUP_LINE]


def test_a_first_pickup_that_takes_a_while_with_a_live_worker_is_never_a_loop(
    ppy_home, client_home, ready, registered_repo
) -> None:
    harness = Harness(FakeEvents([_event(101)]))

    async def scenario() -> int:
        runner = _serve_ticket(
            harness,
            client_home,
            _runner(FakeTurns(lambda turn: dispatch_worker(turn.run_id)), FakePapaya()),
        )
        await _until(lambda: serve.PHASE_DISPATCHED in history(101), what="the dispatch")
        # Still held, its worker still going, an hour on.
        conn = init_db()
        try:
            later = datetime.now(UTC) + timedelta(minutes=59)
            assert serve.repeated_pickups(conn, ITEM, now=later) == (0, "")
        finally:
            conn.close()
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0
    assert _repeating() == []


def _seed_holds(*histories: list[str]) -> None:
    """One ticket task per history, all for ITEM, each phase recorded in order."""
    conn = init_db()
    try:
        for n, phases in enumerate(histories):
            run_id = store.create_run(conn, "Radar QA")
            task_id = store.add_task(conn, run_id=run_id, title="Radar QA")
            event = serve.papaya_events.PapayaEvent(
                id=str(900 + n),
                kind="work_item.assigned",
                subject=f"work_item:{ITEM}",
                payload={},
                work_item_id=ITEM,
            )
            serve.papaya_events.record_task(conn, task_id, event)
            for phase in phases:
                serve.record_phase(conn, task_id, phase)
    finally:
        conn.close()


def test_repeated_pickups_count_only_the_window_and_only_without_progress(ppy_home) -> None:
    looping = [
        serve.PHASE_PICKED_UP,
        serve.PHASE_BRIEFING,
        serve.PHASE_REPORTED,
        serve.PHASE_RELEASED,
    ]
    _seed_holds(looping, looping, [serve.PHASE_PICKED_UP])
    conn = init_db()
    try:
        assert serve.repeated_pickups(conn, ITEM) == (3, serve.PHASE_REPORTED)
        # Outside the window, nothing counts.
        later = datetime.now(UTC) + timedelta(hours=2)
        assert serve.repeated_pickups(conn, ITEM, now=later) == (0, serve.PHASE_RELEASED)
    finally:
        conn.close()

    # One of them reached a worker: that is progress, not a loop.
    _seed_holds([serve.PHASE_PICKED_UP, serve.PHASE_BRIEFING, serve.PHASE_DISPATCHED])
    conn = init_db()
    try:
        assert serve.repeated_pickups(conn, ITEM) == (0, "")
    finally:
        conn.close()


def test_record_once_skips_a_fingerprint_seen_within_the_window(ppy_home) -> None:
    kind = deficiencies.REPEATED_WITHOUT_PROGRESS
    first = deficiencies.record_once(kind, serve.picked_up_detail(KEY, "reported"), within=3600)
    again = deficiencies.record_once(kind, serve.picked_up_detail(KEY, "reported"), within=3600)
    other = deficiencies.record_once(
        kind, serve.picked_up_detail("PAP-211", "reported"), within=3600
    )
    assert first is not None and again is None and other is not None
    # Two tickets are two fingerprints, even though `normalise` would fold their numbers.
    assert first.fingerprint != other.fingerprint
    # A day later the same repetition is a new episode on the same row.
    tomorrow = lambda: datetime.now(UTC) + timedelta(days=1, minutes=1)  # noqa: E731
    later = deficiencies.record_once(
        kind, serve.picked_up_detail(KEY, "reported"), within=3600, clock=tomorrow
    )
    assert later is not None and later.count == 2 and later.fingerprint == first.fingerprint


# ── G1: nothing to build parks the ticket, and the sweep says so ────────────


def test_a_swept_ticket_with_nothing_to_build_is_parked_until_the_item_changes(
    ppy_home, client_home, ready, registered_repo, assigned
) -> None:
    """PAP-210 itself: stale `in_progress`, nothing to build. Offered once, then parked."""
    stale = "2026-09-14T09:00:00+00:00"
    assigned.items = [
        {
            "id": ITEM,
            "title": "Radar QA",
            "status": "in_progress",
            "short_id": KEY,
            "updated_at": stale,
        }
    ]
    papaya_api = FakePapaya()
    harness = Harness(FakeEvents([]))
    stderr = io.StringIO()
    clock = test_serve.Ticks()
    runner_ = _runner(FakeTurns(_nothing_to_build), papaya_api)

    async def scenario() -> int:
        runner = await _serving(
            harness, client_home, stderr, sweep_sleep=clock.sleep, runner=runner_
        )
        await _until(lambda: harness.results, what="the swept hold to end")
        clock.tick()
        await _until(lambda: len(_summaries(stderr)) == 2, what="the next sweep")
        # An unchanged sweep writes no line (the throttle); its offers are what count.
        clock.tick()
        await _until(lambda: assigned.calls >= 3, what="a third sweep")
        await asyncio.sleep(0.2)
        assert len(harness.jobs) == 1, "a parked ticket was offered again"
        # QA moves the item: it is offered again.
        assigned.items[0]["updated_at"] = datetime.now(UTC).isoformat()
        clock.tick()
        await _until(lambda: len(harness.jobs) == 2, what="the changed item to be offered")
        await _until(lambda: len(harness.results) == 2, what="the second hold to end")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0
    assert "ppy serve: sweep found 1: 1 waiting on a person, 0 offered" in _summaries(stderr)
    [(item_id, memo)] = sweep.parked_items().items()
    assert item_id == ITEM and memo["label"] == KEY
    assert memo["reason"] == "the fix is on staging; waiting on QA's recheck"
    assert _pickup_lines(papaya_api) == [PICKUP_LINE]


@pytest.mark.parametrize("changed", [False, True], ids=["still-parked", "changed-since"])
def test_a_restart_does_not_reclaim_a_parked_ticket_but_reclaims_a_changed_one(
    ppy_home, client_home, ready, registered_repo, assigned, changed: bool
) -> None:
    """The reclaim on start goes through the same gate as the sweep.

    A parked ticket's newest task ends `reported` then `released`, which is not given
    away, so without the gate every restart and reconnect re-briefed it, and three
    restarts filed a false repeating deficiency.
    """
    assigned.items = [
        {
            "id": ITEM,
            "title": "Radar QA",
            "status": "in_progress",
            "short_id": KEY,
            "updated_at": "2026-09-14T09:00:00+00:00",
        }
    ]
    papaya_api = FakePapaya()
    stderr = io.StringIO()

    async def serve_once(harness: Harness, calls: int) -> int:
        runner = await _serving(
            harness,
            client_home,
            stderr,
            sweep_sleep=test_serve.Ticks().sleep,
            runner=_runner(FakeTurns(_nothing_to_build), papaya_api),
        )
        await _until(lambda: assigned.calls >= calls, what="the start sweep")
        if calls == 1:
            await _until(lambda: harness.results, what="the first hold to end")
        else:
            await asyncio.sleep(0.3)
        if harness.jobs:
            await _until(lambda: harness.results, what="the reclaimed hold to end")
        harness.loop.request_stop()
        return await runner

    first = Harness(FakeEvents([]))
    assert asyncio.run(serve_once(first, 1)) == 0
    assert sweep.parked_items(), "the first hold did not park the ticket"
    comments = papaya_api.comments()

    if changed:
        assigned.items[0]["updated_at"] = datetime.now(UTC).isoformat()
    restart = Harness(FakeEvents([]))
    assert asyncio.run(serve_once(restart, 2)) == 0

    reclaim_lines = [line for line in stderr.getvalue().splitlines() if "reclaimed " in line]
    if changed:
        assert len(restart.jobs) == 1, "a changed parked ticket was not reclaimed"
        assert _reclaimed(restart) and any(KEY in line for line in reclaim_lines)
    else:
        assert restart.jobs == [] and not _reclaimed(restart) and reclaim_lines == []
        assert len(_ticket_tasks()) == 1
        assert papaya_api.comments() == comments
    assert _repeating() == []


def _reclaimed(harness: Harness) -> bool:
    return f"work_item:{ITEM}" in [subject for subject, _ in harness.events.reserves]


# ── the surface: `needs attention` ──────────────────────────────────────────


def test_needs_attention_names_repeating_parked_and_grown_and_a_look_resets_growth(
    ppy_home, capsys
) -> None:
    kind = deficiencies.REPEATED_WITHOUT_PROGRESS
    deficiencies.record_once(
        kind,
        serve.picked_up_detail(KEY, "reported"),
        within=60,
        evidence={"ticket": KEY, "code": "reported", "times": 3},
    )
    for ticket in ("PAP-219", "PAP-221"):
        deficiencies.record_once(
            kind,
            sweep.refused_detail("not_routed_here"),
            within=60,
            evidence={"ticket": ticket, "code": "not_routed_here", "times": 3},
            per="ticket",
        )
    for _ in range(4):
        deficiencies.record(deficiencies.IDLE_WORK_REFUSED, "not_routed_here refusal")
    for _ in range(2):
        deficiencies.record(deficiencies.REPEATED_STEER, "midpoint")
    deficiencies.record(deficiencies.PROMPT_DEFECT, "seen once is not a repetition")
    sweep.remember_parked(
        ITEM, updated_at="2026-09-19T16:10:00+00:00", reason="waits on QA", label=KEY
    )

    lines = team.attention_lines(team.look())
    repeating = [line for line in lines if line.startswith("repeating: ")]
    assert len(repeating) == 2
    assert any(KEY in line and "each brief found nothing to build" in line for line in repeating)
    # One row for the refusal reason, naming every ticket refused that way.
    assert any(
        "(not_routed_here): PAP-219, PAP-221 (last" in line and "use Run on this Mac" in line
        for line in repeating
    )
    parked = [line for line in lines if line.startswith("parked: ")]
    assert parked and parked[0].startswith(f"parked: {KEY} waiting on a person since ")
    assert "stamp 2026-09-19T16:10:00+00:00: waits on QA" in parked[0]
    grown = [line for line in lines if line.startswith("deficiency ")]
    # Worst first; the row seen once is not named.
    assert [line.split(":")[0] for line in grown] == [
        "deficiency idle-work-refused",
        "deficiency repeated-steer",
    ]
    assert "seen 4x (+3 since the last look)" in grown[0]

    # Looked at: nothing has grown since.
    assert [line for line in team.attention_lines(team.look()) if "deficiency" in line] == []
    deficiencies.record(deficiencies.IDLE_WORK_REFUSED, "not_routed_here refusal")
    [again] = [line for line in team.attention_lines(team.look()) if "deficiency" in line]
    assert "seen 5x (+1 since the last look)" in again

    # `ppy workers` carries the same section after its worker blocks, and as JSON.
    deficiencies.record(deficiencies.IDLE_WORK_REFUSED, "not_routed_here refusal")
    assert cli.main(["workers", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["workers"] == []
    assert [p["ticket"] for p in data["attention"]["parked"]] == [KEY]
    assert [g["kind"] for g in data["attention"]["grown"]] == ["idle-work-refused"]
    conn = init_db()
    try:
        rendered = team.render_workers([], needs=team.attention(conn))
    finally:
        conn.close()
    assert rendered[0] == "no workers in flight"
    heading = rendered.index("needs attention (4):")
    assert all(line.startswith("  ") for line in rendered[heading + 1 :])


def test_only_a_persons_first_look_on_a_terminal_is_recorded(ppy_home, capsys, monkeypatch) -> None:
    """An agent turn, a pipe, JSON or a `--follow` reprint must not use up the growth."""

    class Stream:
        def __init__(self, tty: bool) -> None:
            self.tty = tty

        def isatty(self) -> bool:
            return self.tty

    assert team.a_persons_look(Stream(True))
    assert not team.a_persons_look(Stream(False))
    assert not team.a_persons_look(Stream(True), as_json=True)
    assert not team.a_persons_look(Stream(True), reprint=True)

    for _ in range(3):
        deficiencies.record(deficiencies.IDLE_WORK_REFUSED, "not_routed_here refusal")

    def grown() -> list[int]:
        return [g["grown"] for g in team.peek()["grown"]]

    # capsys's stdout is not a terminal: what an agent's tool call sees.
    for argv in (["workers"], ["workers", "--json"], ["status", "--team"]):
        assert cli.main(argv) == 0
        assert grown() == [2], f"{argv} consumed the growth"
    capsys.readouterr()

    # A person at a terminal.
    monkeypatch.setattr(team, "a_persons_look", lambda stream, **kw: not kw.get("as_json"))
    assert cli.main(["workers"]) == 0
    assert grown() == []
