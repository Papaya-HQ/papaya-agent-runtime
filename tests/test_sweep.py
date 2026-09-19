"""The sweep's choosing: the order it offers in, what it leaves alone, what it says."""

from __future__ import annotations

import asyncio
import io
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from papaya_agent_runtime import sweep

NOW = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)


def _stamp(**ago: float) -> str:
    return (NOW - timedelta(**ago)).isoformat()


def _item(item_id: str, status: str = "todo", priority: str = "normal", **ago: float) -> dict:
    return {
        "id": item_id,
        "status": status,
        "priority": priority,
        "updated_at": _stamp(**(ago or {"days": 1})),
    }


def test_the_sweep_offers_the_most_important_first() -> None:
    """Priority, then the statuses somebody is waiting on, then the oldest."""
    expected = [
        _item("urgent-blocked", "blocked", "urgent", hours=1),
        _item("urgent-todo", "todo", "urgent", days=3),
        _item("high-changes", "changes_requested", "high", hours=2),
        _item("high-in-progress", "in_progress", "high", days=9),
        _item("normal-old-todo", "todo", "normal", days=4),
        _item("normal-new-todo", "todo", "normal", hours=3),
        _item("low-blocked", "blocked", "low", minutes=5),
        _item("low-in-progress", "in_progress", "low", days=30),
    ]
    shuffled = list(expected)
    random.Random(256).shuffle(shuffled)
    assert shuffled != expected

    assert [item["id"] for item in sweep.sweep_order(shuffled)] == [item["id"] for item in expected]


# ── a sweep against a fake listener ─────────────────────────────────────────


@dataclass
class FakeLoop:
    """The listener's `offer`: pending while there are slots, blocked after."""

    slots: int = 0
    offered: list[str] = field(default_factory=list)
    running_subjects: set[str] = field(default_factory=set)

    async def offer(self, envelope: dict[str, Any]) -> str:
        if self.slots <= 0:
            return sweep.OFFER_BLOCKED
        self.slots -= 1
        self.offered.append(envelope["work_item_id"])
        return sweep.OFFER_PENDING


@dataclass
class FakeBuilt:
    loop: FakeLoop = field(default_factory=FakeLoop)
    api: object = None
    agent_config: dict = field(default_factory=dict)


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> float:
        return self.now.timestamp()

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def assigned(monkeypatch) -> list[dict[str, Any]]:
    from papaya_agent_client import api_client

    items: list[dict[str, Any]] = []

    async def list_assigned_work_items(_api: Any, *, status: str | None = None) -> list[dict]:
        return [dict(item) for item in items]

    monkeypatch.setattr(api_client, "list_assigned_work_items", list_assigned_work_items)
    return items


class Reads:
    """Papaya's comments, reservations and reclaim route, per work item."""

    def __init__(self) -> None:
        self.thread: dict[str, list[dict[str, Any]]] = {}
        #: Work item id -> its reservation; an id not here has none.
        self.reservations: dict[str, dict[str, Any]] = {}
        self.reclaims: list[str] = []
        self.reclaim_answer = (sweep.RECLAIM_UNSUPPORTED, "Papaya has no reclaim route yet")

    async def comments(self, work_item_id: str) -> list[dict[str, Any]] | None:
        return list(self.thread.get(work_item_id, []))

    async def reservation(self, subject: str) -> dict[str, Any] | None:
        return self.reservations.get(subject.removeprefix("work_item:"))

    async def reclaim(self, work_item_id: str) -> tuple[str, str]:
        self.reclaims.append(work_item_id)
        return self.reclaim_answer


def _sweeper(
    built: FakeBuilt,
    clock: Clock,
    stderr: io.StringIO | None = None,
    *,
    reads: Reads | None = None,
    **kwargs: Any,
) -> sweep.Sweeper:
    return sweep.Sweeper(
        built,
        stderr=stderr,
        live_items=set,
        clock=clock,
        reads=reads if reads is not None else Reads(),
        connection_ids=set,
        tickets=dict,
        **kwargs,
    )


def test_an_in_progress_item_touched_ten_minutes_ago_is_in_progress_elsewhere(
    ppy_home, assigned
) -> None:
    assigned.append(_item("item-1", "in_progress", minutes=10))
    built = FakeBuilt(loop=FakeLoop(slots=2))

    result = asyncio.run(_sweeper(built, Clock()).sweep_once())

    assert built.loop.offered == []
    assert (result.found, result.offered, result.skipped) == (1, 0, 1)
    assert result.in_progress_elsewhere == 1
    assert result.summary() == "sweep found 1: 1 in progress elsewhere, 0 offered"


def test_an_in_progress_item_untouched_for_a_day_is_offered_after_the_todo_items(
    ppy_home, assigned
) -> None:
    assigned.extend(
        [
            _item("left", "in_progress", days=1),
            _item("todo-new", "todo", minutes=30),
            _item("todo-old", "todo", hours=5),
        ]
    )
    built = FakeBuilt(loop=FakeLoop(slots=3))

    result = asyncio.run(_sweeper(built, Clock()).sweep_once())

    assert built.loop.offered == ["todo-old", "todo-new", "left"]
    assert (result.offered, result.in_progress_elsewhere) == (3, 0)


def test_how_long_in_progress_must_sit_is_configurable(ppy_home, assigned, monkeypatch) -> None:
    assert sweep.stale_after_from_env({}) == sweep.DEFAULT_STALE_AFTER == 6 * 60 * 60
    monkeypatch.setenv(sweep.SWEEP_STALE_AFTER_ENV, "300")
    assigned.append(_item("item-1", "in_progress", minutes=10))
    built = FakeBuilt(loop=FakeLoop(slots=1))

    asyncio.run(_sweeper(built, Clock()).sweep_once())

    assert built.loop.offered == ["item-1"]


def test_a_parked_ticket_waits_on_a_person_until_somebody_who_is_not_an_agent_comments(
    ppy_home, assigned
) -> None:
    """`updated_at` does not move for a comment; a person's comment still un-parks it."""
    assigned.append(_item("PAP-210", "in_progress", days=5))
    parked_at = _stamp(hours=1)
    sweep.remember_parked("PAP-210", updated_at=parked_at, reason="waits on QA", label="PAP-210")
    reads = Reads()
    # The agent's own recheck request, after parking, is not an answer.
    reads.thread["PAP-210"] = [{"author_type": "agent", "created_at": _stamp(minutes=30)}]
    built = FakeBuilt(loop=FakeLoop(slots=1))

    parked = asyncio.run(_sweeper(built, Clock(), reads=reads).sweep_once())
    assert built.loop.offered == []
    assert parked.summary() == "sweep found 1: 1 waiting on a person, 0 offered"

    # QA answers on the thread.
    reads.thread["PAP-210"].append({"author_type": "user", "created_at": _stamp(minutes=5)})
    answered = asyncio.run(_sweeper(built, Clock(), reads=reads).sweep_once())
    assert built.loop.offered == ["PAP-210"]
    assert answered.waiting_on_a_person == 0


def test_a_comment_carrying_an_author_actor_is_an_agents_and_un_parks_nothing() -> None:
    """One authorship rule: the runner's (`serve._is_agent_comment`) and the sweep's."""
    from papaya_agent_runtime import serve

    stamp = _stamp(hours=1)
    by_agent = {
        "author_type": "user",
        "author_actor": {"type": "agent", "id": "agent-1"},
        "created_at": _stamp(minutes=5),
    }
    assert serve._is_agent_comment(by_agent) and sweep.is_agent_comment(by_agent)
    assert not sweep.person_spoke_since([by_agent], stamp)
    by_person = {"author_type": "user", "created_at": _stamp(minutes=5)}
    assert not serve._is_agent_comment(by_person)
    assert sweep.person_spoke_since([by_person], stamp)


def test_a_parked_ticket_is_forgotten_once_a_listing_no_longer_has_it_open(
    ppy_home, assigned, monkeypatch
) -> None:
    for item_id in ("still-open", "closed", "reassigned"):
        sweep.remember_parked(item_id, updated_at=_stamp(hours=1), reason="waits", label=item_id)
    built = FakeBuilt(loop=FakeLoop(slots=0))

    # A listing that failed forgets nothing.
    from papaya_agent_client import api_client

    async def unreachable(_api: Any, **_kw: Any) -> list[dict]:
        raise ConnectionError("Papaya is unreachable")

    with monkeypatch.context() as patched:
        patched.setattr(api_client, "list_assigned_work_items", unreachable)
        failed = asyncio.run(_sweeper(built, Clock()).sweep_once())
    assert failed.error is not None
    assert sorted(sweep.parked_items()) == ["closed", "reassigned", "still-open"]

    # A listing that succeeded: `closed` is done, `reassigned` is not listed at all.
    assigned.extend([_item("still-open", "in_progress", days=5), _item("closed", "done")])
    asyncio.run(_sweeper(built, Clock()).sweep_once())
    assert sorted(sweep.parked_items()) == ["still-open"]


def test_the_reclaim_on_start_leaves_a_parked_ticket_until_a_person_answers(
    ppy_home, assigned
) -> None:
    """The reclaim goes through the same gate as the sweep, comments included."""
    from types import SimpleNamespace

    assigned.append(_item("PAP-210", "in_progress", days=5))
    sweep.remember_parked("PAP-210", updated_at=_stamp(hours=1), reason="waits", label="PAP-210")
    ticket = SimpleNamespace(task_id=7, phase="released", work_item_id="PAP-210")
    reads = Reads()
    built = FakeBuilt(loop=FakeLoop(slots=1))

    def reclaiming() -> sweep.Sweeper:
        return sweep.Sweeper(
            built,
            live_items=set,
            clock=Clock(),
            reads=reads,
            connection_ids=lambda: {"conn-earlier"},
            tickets=lambda: {"PAP-210": ticket},
        )

    parked = reclaiming()
    asyncio.run(parked.sweep_once())
    assert reads.reclaims == [] and built.loop.offered == [] and parked.reclaim_lines == []

    reads.thread["PAP-210"] = [{"author_type": "user", "created_at": _stamp(minutes=5)}]
    answered = reclaiming()
    asyncio.run(answered.sweep_once())
    assert reads.reclaims == ["PAP-210"] and built.loop.offered == ["PAP-210"]
    assert any(line.startswith("reclaimed ") for line in answered.reclaim_lines)


def test_a_parked_ticket_changed_recently_is_offered_not_judged_in_progress_elsewhere(
    ppy_home, assigned
) -> None:
    assigned.append(_item("PAP-210", "in_progress", minutes=10))
    sweep.remember_parked("PAP-210", updated_at=_stamp(hours=1), reason="waits", label="PAP-210")
    built = FakeBuilt(loop=FakeLoop(slots=1))

    asyncio.run(_sweeper(built, Clock()).sweep_once())

    assert built.loop.offered == ["PAP-210"]


def _lines(stderr: io.StringIO) -> list[str]:
    return stderr.getvalue().splitlines()


def test_identical_sweeps_write_one_summary_until_one_offers_something(ppy_home, assigned) -> None:
    assigned.extend([_item("item-1"), _item("item-2")])
    built = FakeBuilt(loop=FakeLoop(slots=0))
    clock = Clock()
    stderr = io.StringIO()
    sweeper = _sweeper(built, clock, stderr)

    async def scenario() -> None:
        for _ in range(3):  # every five minutes, the pool still full
            await sweeper.sweep_once()
            clock.advance(minutes=5)
        built.loop.slots = 1
        await sweeper.sweep_once()

    asyncio.run(scenario())

    assert _lines(stderr) == [
        "ppy serve: sweep found 2: 0 offered; 2 left for the next sweep (every slot is busy)",
        "ppy serve: sweep found 2: 1 offered; 1 left for the next sweep (every slot is busy)",
    ]


def test_an_unchanged_sweep_still_says_so_every_half_hour(ppy_home, assigned) -> None:
    assigned.extend([_item("item-1"), _item("item-2")])
    built = FakeBuilt(loop=FakeLoop(slots=0))
    clock = Clock()
    stderr = io.StringIO()
    sweeper = _sweeper(built, clock, stderr)

    async def scenario() -> None:
        for _ in range(8):  # forty minutes of five-minute sweeps
            await sweeper.sweep_once()
            clock.advance(minutes=5)
        # A person asking always gets the line.
        await sweeper.sweep_once(by_hand=True)

    asyncio.run(scenario())

    full = "ppy serve: sweep found 2: 0 offered; 2 left for the next sweep (every slot is busy)"
    assert _lines(stderr) == [full, "ppy serve: sweep unchanged: still 2 waiting for a slot", full]


# ── work Papaya keeps somewhere else ────────────────────────────────────────

#: The holder Papaya names refusing a reserve for work it kept with the hosted agent.
KEPT_IN_PAPAYA = {
    "connection_id": "papaya-hosted",
    "connection_name": "Engineering Agent in Papaya",
    "session_id": "not-routed-to-this-machine",
}


@dataclass
class RoutingEvents:
    """Papaya's reserve: refused as not sent to this machine, unless routed here."""

    reserves: list[str] = field(default_factory=list)
    routed_here: set[str] = field(default_factory=set)
    holder: dict[str, Any] = field(default_factory=lambda: dict(KEPT_IN_PAPAYA))

    async def reserve(self, subject: str, session_id: str, **_: Any) -> dict[str, Any]:
        from papaya_agent_client.api_client import SubjectHeld

        self.reserves.append(subject)
        if subject not in self.routed_here:
            raise SubjectHeld(subject, dict(self.holder), None)
        return {"renewed": False}


@dataclass
class RoutingLoop(FakeLoop):
    """`offer` the way the client's loop does it: reserve, and a refusal is just `done`."""

    _events: RoutingEvents = field(default_factory=RoutingEvents)

    async def offer(self, envelope: dict[str, Any]) -> str:
        from papaya_agent_client.api_client import SubjectHeld

        try:
            await self._events.reserve(envelope["subject"], "sess-here")
        except SubjectHeld:
            return "done"  # the client's `not_routed_here` skip
        self.offered.append(envelope["work_item_id"])
        self.running_subjects.add(envelope["subject"])
        return sweep.OFFER_PENDING


KEPT_FIVE = (
    "sweep found 5: 5 kept by Engineering Agent in Papaya and being worked "
    "(use Run on this Mac to route one here), 0 offered"
)

#: A reservation the hosted agent is renewing: evidence somebody is doing the work.
LIVE_LEASE = {
    "subject": "work_item:x",
    "holder": {**KEPT_IN_PAPAYA, "agent_id": "agent-1"},
    "lease_expires_at": (NOW + timedelta(days=30)).isoformat(),
}


def _five_kept(assigned: list[dict[str, Any]]) -> FakeBuilt:
    """Five items Papaya keeps with the hosted agent, each under a live reservation."""
    assigned.extend(
        {**_item(f"item-{n}", hours=n), "reservation": dict(LIVE_LEASE)} for n in range(1, 6)
    )
    return FakeBuilt(loop=RoutingLoop())


def test_work_papaya_keeps_elsewhere_is_not_asked_for_again_for_thirty_minutes(
    ppy_home, assigned
) -> None:
    built = _five_kept(assigned)
    clock = Clock()
    stderr = io.StringIO()
    sweeper = _sweeper(built, clock, stderr)

    async def scenario() -> list[sweep.SweepResult]:
        results = [await sweeper.sweep_once()]
        clock.advance(minutes=5)
        results.append(await sweeper.sweep_once())
        clock.advance(minutes=25)  # thirty minutes after the refusals
        results.append(await sweeper.sweep_once())
        return results

    first, second, third = asyncio.run(scenario())

    events = built.loop._events
    assert len(events.reserves) == 10, "asked five, then none, then five again"
    assert first.summary() == KEPT_FIVE
    assert (second.offered, second.kept_total) == (0, 5)
    assert second.summary() == KEPT_FIVE
    assert third.summary() == KEPT_FIVE
    remembered = sweep.kept_items()
    assert sorted(remembered) == [f"item-{n}" for n in range(1, 6)]
    assert remembered["item-1"]["holder"]["connection_name"] == "Engineering Agent in Papaya"
    assert remembered["item-1"]["updated_at"] == _stamp(hours=1)
    # Said once, and again only when the half hour is up.
    assert _lines(stderr) == [
        f"ppy serve: {KEPT_FIVE}",
        "ppy serve: sweep unchanged: still found 5: 5 kept by Engineering Agent in Papaya "
        "and being worked (use Run on this Mac to route one here), 0 offered",
    ]


def test_kept_work_whose_updated_at_moved_is_asked_for_before_the_thirty_minutes(
    ppy_home, assigned
) -> None:
    built = _five_kept(assigned)
    clock = Clock()
    sweeper = _sweeper(built, clock)

    async def scenario() -> sweep.SweepResult:
        await sweeper.sweep_once()
        clock.advance(minutes=5)
        # Someone routed it here from the app, which touches the item.
        assigned[2]["updated_at"] = _stamp(minutes=-5)
        built.loop._events.routed_here.add("work_item:item-3")
        return await sweeper.sweep_once()

    result = asyncio.run(scenario())

    assert built.loop._events.reserves[5:] == ["work_item:item-3"]
    assert built.loop.offered == ["item-3"]
    assert (result.offered, result.kept_total) == (1, 4)
    # Picked up, so nothing is remembered about it any more.
    assert "item-3" not in sweep.kept_items()


def test_include_kept_asks_for_kept_work_regardless(ppy_home, assigned) -> None:
    built = _five_kept(assigned)
    sweeper = _sweeper(built, Clock())

    async def scenario() -> sweep.SweepResult:
        await sweeper.sweep_once()
        return await sweeper.sweep_once(include_kept=True, by_hand=True)

    result = asyncio.run(scenario())

    assert len(built.loop._events.reserves) == 10
    assert result.summary() == KEPT_FIVE


def test_picking_an_item_up_forgets_that_papaya_kept_it_elsewhere(ppy_home) -> None:
    sweep.remember_kept(
        {
            "item-1": {"updated_at": None, "holder": KEPT_IN_PAPAYA, "kept_at": NOW.isoformat()},
            "item-2": {"updated_at": None, "holder": KEPT_IN_PAPAYA, "kept_at": NOW.isoformat()},
        }
    )

    # What `serve` calls the moment it takes a ticket.
    sweep.forget_declined("item-1")

    assert sorted(sweep.kept_items()) == ["item-2"]


# ── evidence of work, not claims of ownership ───────────────────────────────

#: The holder Papaya names for a subject its on-call fallback took (the guard window).
FALLBACK_HOLDER = {
    "connection_id": "papaya-hosted",
    "connection_name": "Engineering Agent in Papaya",
    "session_id": "on-call-fallback",
}


def _hosted_comment(item_id: str, **ago: float) -> dict[str, Any]:
    """A comment the hosted agent wrote: as the agent, through no connection."""
    return {
        "id": f"c-{item_id}",
        "author_type": "agent",
        "author_id": "agent-1",
        "author_actor": {"type": "agent", "id": "agent-1", "via_connection": None},
        "body": "Looking at this now.",
        "created_at": _stamp(**ago),
        "metadata": {},
    }


def test_kept_work_with_no_evidence_of_work_is_offered_again_on_the_next_sweep(
    ppy_home, assigned
) -> None:
    """No reservation, no job and no word from the holder for fifteen minutes: idle."""
    assigned.extend(
        [
            _item("idle", hours=1),
            {**_item("leased", hours=1), "reservation": dict(LIVE_LEASE)},
            _item("talking", hours=1),
            # Left `in_progress` by the fallback an hour ago: recent enough for the
            # six-hour rule, but nobody is working it.
            _item("stuck", "in_progress", hours=1),
        ]
    )
    reads = Reads()
    reads.thread["talking"] = [_hosted_comment("talking", minutes=5)]
    # Papaya refused all three here half an hour's recheck ago; that memory would
    # keep every one of them from being asked for again.
    sweep.remember_kept(
        {
            item["id"]: {
                "updated_at": item["updated_at"],
                "holder": FALLBACK_HOLDER,
                "kept_at": NOW.isoformat(),
            }
            for item in assigned
        }
    )
    built = FakeBuilt(loop=RoutingLoop(), agent_config={"agent_id": "agent-1"})
    built.loop._events.routed_here.add("work_item:idle")
    clock = Clock()
    clock.advance(minutes=5)

    result = asyncio.run(_sweeper(built, clock, reads=reads).sweep_once())

    # Only the idle ones are asked for; the leased one and the one its holder spoke on
    # five minutes ago are being worked. Papaya still refuses the stuck one.
    assert built.loop._events.reserves == ["work_item:idle", "work_item:stuck"]
    assert built.loop.offered == ["idle"]
    assert result.kept == (("Engineering Agent in Papaya", 2),)
    assert result.idle == (("Engineering Agent in Papaya", 1, 65),)
    assert result.offered == 1
    assert "idle" not in sweep.kept_items()

    # Fifteen minutes after the holder last spoke, that one is idle too; a live
    # reservation still is not; the stuck one is asked for again.
    clock.advance(minutes=10)
    later = asyncio.run(_sweeper(built, clock, reads=reads).sweep_once())
    assert built.loop._events.reserves[2:] == ["work_item:talking", "work_item:stuck"]
    assert later.kept == (("Engineering Agent in Papaya", 1),)
    assert later.idle_total == 2


def test_a_refused_idle_item_is_one_blocker_updated_on_change_and_a_deficiency_on_the_third(
    ppy_home, assigned, monkeypatch
) -> None:
    from papaya_agent_runtime import blockers, deficiencies

    assigned.extend(
        {**_item(item_id, hours=2), "short_id": item_id} for item_id in ("PAP-219", "PAP-221")
    )
    assigned.append({**_item("PAP-222", hours=2), "short_id": "PAP-222"})
    built = FakeBuilt(loop=RoutingLoop(), agent_config={"agent_id": "agent-1"})
    built.loop._events.holder = dict(FALLBACK_HOLDER)
    published: list[bool] = []
    clock = Clock()
    sweeper = _sweeper(built, clock, publish=lambda: published.append(True))

    def idle_blockers() -> list[blockers.Blocker]:
        ledger = blockers.Ledger.load()
        return [b for b in ledger.open.values() if b.code == blockers.IDLE_WORK_KEPT]

    def refused_deficiencies() -> list[deficiencies.Deficiency]:
        found = deficiencies.ledger(include_all=True)
        return sorted(
            (d for d in found if d.kind == deficiencies.REPEATED_WITHOUT_PROGRESS),
            key=lambda d: d.detail,
        )

    async def sweep_after(minutes: float) -> sweep.SweepResult:
        clock.advance(minutes=minutes)
        return await sweeper.sweep_once()

    # First sweep: one blocker naming all three.
    asyncio.run(sweep_after(0))
    [blocker] = idle_blockers()
    assert blocker.title == (
        "Papaya keeps 3 idle items from this Mac: PAP-219, PAP-221, PAP-222; "
        "use Run on this Mac, or wait for the guard to lift"
    )
    first_seen = blocker.first_seen
    assert published == [True]
    # Readiness rounds do not clear what they did not raise.
    from papaya_agent_runtime import readiness

    blockers.update(readiness.Readiness(state=readiness.READY))
    assert len(idle_blockers()) == 1

    # Same set: still one blocker, not re-recorded.
    asyncio.run(sweep_after(5))
    [same] = idle_blockers()
    assert (same.title, same.first_seen) == (blocker.title, first_seen)
    assert published == [True]
    assert refused_deficiencies() == []

    # PAP-222 was routed here: the blocker is updated to the two left.
    built.loop._events.routed_here.add("work_item:PAP-222")
    asyncio.run(sweep_after(5))
    [changed] = idle_blockers()
    assert changed.title.startswith("Papaya keeps 2 idle items from this Mac: PAP-219, PAP-221;")
    assert changed.first_seen == first_seen
    assert published == [True, True]
    # The third refusal running of PAP-219 and PAP-221, with no evidence of work: one
    # row for the reason, both tickets in its evidence. Two tickets refused the same
    # way is past the kind's threshold: an issue, not two.
    [found] = refused_deficiencies()
    assert found.detail == sweep.refused_detail("handled_in_papaya")
    assert sorted((e["ticket"], e["times"]) for e in found.evidence) == [
        ("PAP-219", 3),
        ("PAP-221", 3),
    ]
    assert found.status == deficiencies.PENDING
    assert not any(d.kind == deficiencies.IDLE_WORK_REFUSED for d in deficiencies.ledger())

    # A fourth and fifth refusal do not record them again: each ticket once a day, not
    # once per sweep (the 527-count row of 2026-09-19).
    asyncio.run(sweep_after(5))
    asyncio.run(sweep_after(5))
    assert [d.count for d in refused_deficiencies()] == [2]

    # Nothing refused any more: the blocker clears.
    built.loop._events.routed_here.update({"work_item:PAP-219", "work_item:PAP-221"})
    asyncio.run(sweep_after(5))
    assert idle_blockers() == []
    assert published == [True, True, True]


def test_a_refusal_streak_counts_only_sweeps_refused_for_the_same_reason(
    ppy_home, assigned
) -> None:
    from papaya_agent_runtime import deficiencies

    assigned.append({**_item("PAP-219", hours=2), "short_id": "PAP-219"})
    built = FakeBuilt(loop=RoutingLoop(), agent_config={"agent_id": "agent-1"})
    built.loop._events.holder = dict(FALLBACK_HOLDER)
    clock = Clock()
    sweeper = _sweeper(built, clock)

    def recorded() -> list[deficiencies.Deficiency]:
        return [
            d
            for d in deficiencies.ledger(include_all=True)
            if d.kind == deficiencies.REPEATED_WITHOUT_PROGRESS
        ]

    async def sweeps(n: int) -> None:
        for _ in range(n):
            clock.advance(minutes=5)
            await sweeper.sweep_once()

    asyncio.run(sweeps(2))
    # The reason changes: the streak starts again.
    built.loop._events.holder = dict(KEPT_IN_PAPAYA)
    asyncio.run(sweeps(2))
    assert recorded() == []
    asyncio.run(sweeps(1))
    [row] = recorded()
    assert row.detail == sweep.refused_detail("not_routed_here")
    assert row.evidence[0]["ticket"] == "PAP-219"
    # One stuck ticket is a `needs attention` line, not an issue on first sight.
    assert row.status == deficiencies.WATCHING


def test_the_summary_says_which_kept_work_is_being_worked_and_which_is_idle(
    ppy_home, assigned
) -> None:
    assigned.extend(
        [
            {**_item("leased", hours=3), "reservation": dict(LIVE_LEASE)},
            _item("quiet", minutes=40),
            _item("quieter", hours=2),
        ]
    )
    built = FakeBuilt(loop=RoutingLoop(), agent_config={"agent_id": "agent-1"})
    built.loop._events.holder = dict(FALLBACK_HOLDER)

    result = asyncio.run(_sweeper(built, Clock()).sweep_once())

    assert result.summary() == (
        "sweep found 3: 1 kept by Engineering Agent in Papaya and being worked, "
        "2 kept by Engineering Agent in Papaya and idle for 40 minutes "
        "(use Run on this Mac to route one here), 0 offered"
    )


def test_a_start_reclaims_what_an_earlier_connection_held_and_resumes_the_ticket_at_review(
    ppy_home, assigned, monkeypatch
) -> None:
    """PAP-219: the ticket stalled with its worker done, then the fallback took it."""
    import test_serve
    from papaya_agent_runtime import papaya_events, rounds, serve
    from papaya_agent_runtime.state import store
    from papaya_agent_runtime.state.db import init_db

    conn = init_db()
    run_id = store.create_run(conn, "Ticket PAP-219")
    ticket = store.add_task(conn, run_id=run_id, title="Ticket PAP-219")
    event = papaya_events.PapayaEvent(
        id="219",
        kind="work_item.assigned",
        subject="work_item:item-219",
        payload={},
        work_item_id="item-219",
    )
    papaya_events.record_task(conn, ticket, event)
    for phase in (
        serve.PHASE_PICKED_UP,
        serve.PHASE_BRIEFING,
        serve.PHASE_DISPATCHED,
        serve.PHASE_STALLED,
        serve.PHASE_HANDED_OVER,
    ):
        serve.record_phase(conn, ticket, phase)
    conn.close()
    worker = test_serve.dispatch_worker(run_id)
    test_serve.worker_event(worker, "worker_done", status="worker_done", summary="Gate green.")

    # The earlier connection's hold, and nothing working the item now.
    assigned.append(
        {
            **_item("item-219", "in_progress", minutes=20),
            "short_id": "PAP-219",
            "run_on_this_mac": {"connection_id": "conn-before", "online": False},
        }
    )
    runner = test_serve._runner(test_serve.FakeTurns(), test_serve.FakePapaya())
    reads = Reads()
    built = FakeBuilt(loop=RoutingLoop(), agent_config={"agent_id": "agent-1"})
    built.loop._events.routed_here.add("work_item:item-219")
    stderr = io.StringIO()
    sweeper = sweep.Sweeper(
        built,
        stderr=stderr,
        live_items=set,
        clock=Clock(),
        reads=reads,
        runner=runner,
        connection_ids=lambda: {"conn-before", "conn-now"},
        tickets=sweep.ticket_index,
    )

    result = asyncio.run(sweeper.sweep_once())

    assert reads.reclaims == ["item-219"]
    assert built.loop.offered == ["item-219"]
    assert result.offered == 1
    assert sweeper.reclaim_lines == [
        f"reclaimed PAP-219 (ticket task {ticket} here; reclaim unsupported: Papaya has no "
        f"reclaim route yet); resuming ticket task {ticket}",
        "reclaim on connect: 1 held by an earlier connection, 1 reclaimed, 0 refused",
    ]
    # The offer (a new event key, as every offer has) lands on the same ticket task,
    # at review: its worker is done.
    offer = papaya_events.PapayaEvent(
        id="offer-219",
        kind="work_item.assigned",
        subject="work_item:item-219",
        payload={},
        work_item_id="item-219",
    )
    conn = init_db()
    try:
        held = runner._record(conn, offer, None)
    finally:
        conn.close()
    assert (held.task_id, held.resume_from, held.reclaimed) == (
        ticket,
        serve.PHASE_REVIEWING,
        True,
    )
    # The rounds leave a handed-over ticket to the sweep's evidence.
    assert rounds.reclaimable(rounds.ticket_tasks()) == []
    # A second sweep, with Papaya reachable all along, does not reclaim again.
    asyncio.run(sweeper.sweep_once())
    assert reads.reclaims == ["item-219"]
