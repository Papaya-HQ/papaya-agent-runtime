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


def _sweeper(built: FakeBuilt, clock: Clock, stderr: io.StringIO | None = None) -> sweep.Sweeper:
    return sweep.Sweeper(built, stderr=stderr, live_items=set, clock=clock)


def test_an_in_progress_item_touched_ten_minutes_ago_is_in_progress_elsewhere(
    ppy_home, assigned
) -> None:
    assigned.append(_item("item-1", "in_progress", minutes=10))
    built = FakeBuilt(loop=FakeLoop(slots=2))

    result = asyncio.run(_sweeper(built, Clock()).sweep_once())

    assert built.loop.offered == []
    assert (result.found, result.offered, result.skipped) == (1, 0, 1)
    assert result.in_progress_elsewhere == 1
    assert result.summary() == "sweep found 1, offered 0, skipped 1, 1 in progress elsewhere"


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
        "ppy serve: sweep found 2, offered 0, skipped 0; 2 left for the next sweep "
        "(every slot is busy)",
        "ppy serve: sweep found 2, offered 1, skipped 0; 1 left for the next sweep "
        "(every slot is busy)",
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

    full = (
        "ppy serve: sweep found 2, offered 0, skipped 0; 2 left for the next sweep "
        "(every slot is busy)"
    )
    assert _lines(stderr) == [full, "ppy serve: sweep unchanged: still 2 waiting for a slot", full]
