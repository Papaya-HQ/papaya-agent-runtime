"""The sweep: `ppy serve` looks for its work as well as waiting for it.

The event stream says what just happened; it does not say what is still owned.
An assignment can be missed — the machine was off, every slot was busy for long
enough, an earlier client aged the event out — and a ticket handed back while
nobody was looking stays assigned with nothing that will ever pick it up. So on
start, and then every `sweep_interval`, `serve` asks Papaya what is assigned to
this agent and offers every open item this runtime is not already working to the
client's loop (`ListenerLoop.offer`, papaya-agent-client 0.16.0).

An offer is not a second path. It goes through the same playbook, reservation,
supervised approval and runner a pulled event does, so a swept ticket asks the
app exactly what an event-picked one asks and leaves the same task row behind.
What the sweep adds is only the choosing:

- **open** items only (`todo`, `in_progress`, `blocked`, `changes_requested`);
- not one this loop already holds, and not one with a **live task** here —
  looked up by work item id, whatever event first created the task. A task whose
  hold ended (released, handed back, stalled, declined) or whose status is closed
  is not live, so a ticket that is still assigned is offered again;
- a subject someone else holds is a skip and a debug line, never an error, and it
  is not remembered — the next sweep simply asks again;
- an `in_progress` item somebody touched in the last `stale_after` (six hours by
  default) is being worked somewhere else — another session, the hosted agent —
  and offering it would only start the same work twice, so it is skipped and
  counted as in progress elsewhere; one left untouched for longer is fair game;
- the most important first (`sweep_order`): priority, then the statuses somebody
  is waiting on, then the oldest;
- a full pool ends the round, and the rest wait for the next one.

A sweep says one line on stderr — found, offered, skipped — and nothing on the
supervised protocol, which is for jobs, not for bookkeeping. A sweep that found
exactly what the one before it found says so at most every half hour.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from papaya_agent_runtime import papaya_events

log = logging.getLogger("papaya_agent_runtime.sweep")

#: How often `serve` sweeps when nobody says otherwise: five minutes.
DEFAULT_SWEEP_INTERVAL = 300.0

#: The environment variable that sets the interval when `--sweep-interval` does not.
SWEEP_INTERVAL_ENV = "PPY_SWEEP_INTERVAL"

#: The environment variable that sets how long an `in_progress` item must sit untouched.
SWEEP_STALE_AFTER_ENV = "PPY_SWEEP_STALE_AFTER"

#: How long an `in_progress` item must sit untouched before the sweep offers it: six hours.
DEFAULT_STALE_AFTER = 6 * 60 * 60.0

#: How often a sweep that found nothing new may still say so: every thirty minutes.
UNCHANGED_SUMMARY_EVERY = 30 * 60.0

#: The work item statuses that still want somebody working on them.
OPEN_STATUSES = frozenset({"todo", "in_progress", "blocked", "changes_requested"})

#: Offer order, most important first. An unknown priority sorts as `normal`.
PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3}

#: Within a priority: somebody is waiting on these, then fresh work, then work
#: that was started once and left. An unknown status sorts last.
STATUS_RANK = {"changes_requested": 0, "blocked": 0, "todo": 1, "in_progress": 2}

#: The phases that mean a hold on the ticket has ended. A task in one of these is
#: history, not work in flight, and does not stop the ticket being offered again.
ENDED_PHASES = ("released", "handed_back", "stalled", "declined")

#: The client's answers to `offer`, named once here.
OFFER_PENDING = "pending"
OFFER_BLOCKED = "blocked"

#: How long `ppy sweep` waits for the running `serve` to finish one sweep.
REQUEST_TIMEOUT_SECONDS = 120.0


def interval_from_env(environ: Mapping[str, str] | None = None) -> float:
    """The sweep interval `PPY_SWEEP_INTERVAL` names, or the default.

    Raises ``ValueError`` for a value that is not a non-negative number, so the
    caller can report it the way it reports a bad flag rather than sweep on a
    cadence nobody asked for.
    """
    env = os.environ if environ is None else environ
    raw = str(env.get(SWEEP_INTERVAL_ENV) or "").strip()
    if not raw:
        return DEFAULT_SWEEP_INTERVAL
    return parse_interval(raw, source=SWEEP_INTERVAL_ENV)


def stale_after_from_env(environ: Mapping[str, str] | None = None) -> float:
    """How long `PPY_SWEEP_STALE_AFTER` says an `in_progress` item must sit, or six hours."""
    env = os.environ if environ is None else environ
    raw = str(env.get(SWEEP_STALE_AFTER_ENV) or "").strip()
    if not raw:
        return DEFAULT_STALE_AFTER
    return parse_interval(raw, source=SWEEP_STALE_AFTER_ENV)


def parse_interval(raw: str | float, *, source: str) -> float:
    """A sweep interval in seconds: zero or more, where zero turns the timer off."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{source} must be a number of seconds, not {raw!r}") from None
    if value < 0 or value != value:  # NaN is not a cadence either
        raise ValueError(f"{source} must be zero or more seconds, not {raw!r}")
    return value


# ── the tickets this runtime already said no to ─────────────────────────────
#
# A declined ticket leaves no task row — declining is the opposite of taking work
# on — so without a memory of its own the sweep would offer it again every round:
# the app asks the person, the runner declines, the subject goes back as declined,
# and Papaya writes another event, every five minutes, for every such item. So a
# decline is remembered with the item's `updated_at` at the time, and the sweep
# leaves the item alone until somebody changes it (an edit or a comment moves
# `updated_at`, and that is worth another look) or a person runs
# `ppy sweep --include-declined`.

_declined_lock = threading.Lock()


def declined_path() -> Path:
    """Where declined tickets are remembered: `.ppy/sweep-declined.json`."""
    from papaya_agent_runtime.paths import ppy_home

    return ppy_home() / "sweep-declined.json"


def declined_items() -> dict[str, dict[str, Any]]:
    """Every remembered decline, `{work item id: {updated_at, reason, declined_at}}`."""
    try:
        data = json.loads(declined_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def _write_declined(data: dict[str, dict[str, Any]]) -> None:
    path = declined_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def remember_declined(work_item_id: str, *, updated_at: str | None, reason: str) -> None:
    """Record that this runtime declined a ticket, as the ticket stood at the time."""
    if not work_item_id:
        return
    with _declined_lock:
        data = declined_items()
        data[str(work_item_id)] = {
            "updated_at": updated_at,
            "reason": reason,
            "declined_at": datetime.now(UTC).isoformat(),
        }
        _write_declined(data)


def forget_declined(work_item_id: str) -> None:
    """Drop a remembered decline: the ticket was taken, so the memory is stale."""
    if not work_item_id:
        return
    with _declined_lock:
        data = declined_items()
        if data.pop(str(work_item_id), None) is not None:
            _write_declined(data)


def _timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def declined_earlier(item: dict[str, Any], remembered: dict[str, Any] | None) -> bool:
    """Whether `item` is a ticket this runtime declined and nobody has touched since.

    Newer means strictly later than the `updated_at` remembered with the decline.
    A decline remembered without one (an event summary that carried none) cannot
    be compared, so the item gets one more look, and that decline records the
    timestamp the sweep's own envelope carries.
    """
    if remembered is None:
        return False
    then = _timestamp(remembered.get("updated_at"))
    now = _timestamp(item.get("updated_at"))
    if then is None:
        return False
    return now is None or now <= then


@dataclass(frozen=True)
class SweepResult:
    """What one sweep found and did with it."""

    found: int = 0
    offered: int = 0
    skipped: int = 0
    #: Of `skipped`, the ones this runtime declined earlier and nobody has changed since.
    declined_earlier: int = 0
    #: Of `skipped`, `in_progress` items touched too recently to be anybody's but their own.
    in_progress_elsewhere: int = 0
    #: Open items not reached because every slot was busy; the next sweep has them.
    waiting: int = 0
    #: Why the sweep could not ask Papaya at all, when it could not.
    error: str | None = None

    def summary(self) -> str:
        if self.error is not None:
            return f"sweep could not list assigned work: {self.error}"
        line = f"sweep found {self.found}, offered {self.offered}, skipped {self.skipped}"
        if self.declined_earlier:
            line += f", {self.declined_earlier} declined earlier"
        if self.in_progress_elsewhere:
            line += f", {self.in_progress_elsewhere} in progress elsewhere"
        if self.waiting:
            line += f"; {self.waiting} left for the next sweep (every slot is busy)"
        return line

    def unchanged_summary(self) -> str:
        """The line for a sweep that found exactly what the one before it found."""
        if self.error is not None:
            return f"sweep still cannot list assigned work: {self.error}"
        if self.waiting:
            return f"sweep unchanged: still {self.waiting} waiting for a slot"
        return f"sweep unchanged: still found {self.found}, skipped {self.skipped}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "offered": self.offered,
            "skipped": self.skipped,
            "declined_earlier": self.declined_earlier,
            "in_progress_elsewhere": self.in_progress_elsewhere,
            "waiting": self.waiting,
            "error": self.error,
            "summary": self.summary(),
        }


def _items(answer: Any) -> list[dict[str, Any]]:
    """The work items in whatever the route answered: a list, or one wrapped in a dict."""
    if isinstance(answer, dict):
        answer = answer.get("work_items") or answer.get("items") or answer.get("data")
    if not isinstance(answer, list):
        return []
    return [item for item in answer if isinstance(item, dict) and str(item.get("id") or "")]


def _status(item: dict[str, Any]) -> str:
    return str(item.get("status") or "").strip().lower()


def is_open(item: dict[str, Any]) -> bool:
    return _status(item) in OPEN_STATUSES


def sweep_order(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`items` most important first, so a busy pool takes the ones that matter.

    Priority (urgent, high, normal, low); then `changes_requested` and `blocked`,
    which somebody is waiting on, before `todo`, before an `in_progress` item left
    untouched; then the oldest `updated_at`. An item with no readable `updated_at`
    goes after the dated ones at its rank, and ties keep the order Papaya gave.
    """
    never = datetime.max.replace(tzinfo=UTC)

    def key(item: dict[str, Any]) -> tuple[int, int, datetime]:
        priority = str(item.get("priority") or "").strip().lower()
        return (
            PRIORITY_RANK.get(priority, PRIORITY_RANK["normal"]),
            STATUS_RANK.get(_status(item), len(STATUS_RANK)),
            _timestamp(item.get("updated_at")) or never,
        )

    return sorted(items, key=key)


def in_progress_elsewhere(item: dict[str, Any], *, now: datetime, stale_after: float) -> bool:
    """Whether `item` is `in_progress` and touched too recently to be offered.

    Started somewhere else and still moving, it is that session's work; offering
    it would dispatch the same work twice. An `in_progress` item whose `updated_at`
    cannot be read cannot be shown to be abandoned, so it is left alone too.
    """
    if _status(item) != "in_progress":
        return False
    touched = _timestamp(item.get("updated_at"))
    if touched is None:
        return True
    return (now - touched).total_seconds() < stale_after


def live_work_item_ids() -> set[str]:
    """Every work item id that has a task still live in this runtime's table.

    By work item id, not by event key: the key names the event (or offer) that
    first created the task, and a ticket is the same ticket whichever of those
    brought it in.
    """
    from papaya_agent_runtime.lifecycle import TERMINAL_STATUSES
    from papaya_agent_runtime.paths import db_path
    from papaya_agent_runtime.state import db

    if not db_path().exists():
        return set()
    ended_phases = ",".join("?" for _ in ENDED_PHASES)
    closed = ",".join("?" for _ in TERMINAL_STATUSES)
    conn = db.init_db()
    try:
        rows = conn.execute(
            f"""
            SELECT DISTINCT json_extract(task_env.value, '$.work_item_id')
            FROM tasks
            JOIN task_env ON task_env.task_id = tasks.id
            WHERE task_env.key = ?
              AND json_valid(task_env.value)
              AND (tasks.phase IS NULL OR tasks.phase NOT IN ({ended_phases}))
              AND tasks.status NOT IN ({closed})
            """,
            (papaya_events.PAPAYA_EVENT_METADATA, *ENDED_PHASES, *TERMINAL_STATUSES),
        ).fetchall()
    finally:
        conn.close()
    return {str(row[0]) for row in rows if row[0] is not None}


def envelope_for(item: dict[str, Any], *, agent_id: str = "", workspace_id: str = "") -> dict:
    """The assignment an offer stands in for, shaped like the event would have been.

    `id` and `reservable` are left out on purpose: `offer` mints the one and sets
    the other, because they are the loop's to decide.
    """
    item_id = str(item["id"])
    envelope: dict[str, Any] = {
        "kind": "work_item.assigned",
        "subject": f"work_item:{item_id}",
        "work_item_id": item_id,
        "occurred_at": datetime.now(UTC).isoformat(),
        "payload": {"work_item": dict(item)},
    }
    if agent_id:
        envelope["agent_id"] = agent_id
    if workspace_id:
        envelope["workspace_id"] = workspace_id
    return envelope


class Sweeper:
    """Look for assigned work on a timer, and on request, for one embedded listener."""

    def __init__(
        self,
        built: Any,
        *,
        interval: float = DEFAULT_SWEEP_INTERVAL,
        stderr: Any = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        live_items: Callable[[], set[str]] | None = None,
        stale_after: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._built = built
        self._interval = float(interval)
        self._stderr = stderr
        # Seam for tests: the timer, fired on demand instead of every five minutes.
        self._sleep = sleep or asyncio.sleep
        self._live_items = live_items or live_work_item_ids
        #: `sweep_stale_after`: how long an `in_progress` item must sit untouched to be offered.
        self._stale_after = stale_after_from_env() if stale_after is None else float(stale_after)
        # Seam for tests: wall-clock seconds, for staleness and for the summary throttle.
        self._clock = clock or time.time
        # A sweep asked for by hand and one on the timer never interleave: two
        # rounds offering the same item at once would each see it as not yet held.
        self._lock = asyncio.Lock()
        self.results: list[SweepResult] = []
        #: When a summary line was last written, or None before the first.
        self._last_written: float | None = None

    @property
    def interval(self) -> float:
        return self._interval

    async def run(self) -> None:
        """Sweep once now, then every interval until cancelled. Zero sweeps once."""
        await self.sweep_once()
        if self._interval <= 0:
            return
        while True:
            await self._sleep(self._interval)
            await self.sweep_once()

    async def sweep_once(
        self, *, include_declined: bool = False, by_hand: bool = False
    ) -> SweepResult:
        """One round: list, choose, offer. Never raises; a failure is the result.

        `include_declined` offers tickets this runtime declined earlier even when
        nobody has changed them since — the by-hand `ppy sweep --include-declined`.
        `by_hand` is a person asking, who always gets the line.
        """
        async with self._lock:
            try:
                result = await self._sweep(include_declined=include_declined)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad sweep must not end serve
                log.warning("[sweep] Sweep failed: %s", exc)
                result = SweepResult(error=str(exc) or exc.__class__.__name__)
            previous = self.results[-1] if self.results else None
            self.results.append(result)
            line = self._line(result, previous, by_hand=by_hand)
        if line is not None and self._stderr is not None:
            with contextlib.suppress(Exception):
                print(f"ppy serve: {line}", file=self._stderr, flush=True)
        return result

    def _line(
        self, result: SweepResult, previous: SweepResult | None, *, by_hand: bool
    ) -> str | None:
        """What this sweep writes on stderr, if anything.

        The first sweep, a sweep that offered something, one that found something
        different from the sweep before it, and one a person asked for always
        write. A repeat writes at most every `UNCHANGED_SUMMARY_EVERY`: a pool that
        stays full is one line every half hour, not one every five minutes.
        """
        now = self._clock()
        if by_hand or previous is None or result.offered or result != previous:
            self._last_written = now
            return result.summary()
        if self._last_written is not None and now - self._last_written < UNCHANGED_SUMMARY_EVERY:
            return None
        self._last_written = now
        return result.unchanged_summary()

    async def _sweep(self, *, include_declined: bool) -> SweepResult:
        from papaya_agent_client import api_client

        built = self._built
        try:
            answer = await api_client.list_assigned_work_items(built.api)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - an unreachable Papaya is a quiet round
            log.warning("[sweep] Could not list assigned work items: %s", exc)
            return SweepResult(error=str(exc) or exc.__class__.__name__)

        items = sweep_order([item for item in _items(answer) if is_open(item)])
        live = await asyncio.to_thread(self._live_items)
        declined = {} if include_declined else await asyncio.to_thread(declined_items)
        agent_config = getattr(built, "agent_config", None) or {}
        agent_id = str(agent_config.get("agent_id") or "")
        workspace_id = str(agent_config.get("workspace_id") or "")
        now = datetime.fromtimestamp(self._clock(), UTC)

        offered = skipped = earlier = elsewhere = 0
        for index, item in enumerate(items):
            item_id = str(item["id"])
            subject = f"work_item:{item_id}"
            if subject in built.loop.running_subjects or item_id in live:
                log.debug("[sweep] %s already has a live task here; not offering it", subject)
                skipped += 1
                continue
            if in_progress_elsewhere(item, now=now, stale_after=self._stale_after):
                log.debug(
                    "[sweep] %s is in progress and was touched within %ss; not offering it",
                    subject,
                    int(self._stale_after),
                )
                skipped += 1
                elsewhere += 1
                continue
            if declined_earlier(item, declined.get(item_id)):
                log.debug(
                    "[sweep] %s was declined earlier (%s) and has not changed since",
                    subject,
                    declined[item_id].get("reason") or "no reason recorded",
                )
                skipped += 1
                earlier += 1
                continue
            status = await built.loop.offer(
                envelope_for(item, agent_id=agent_id, workspace_id=workspace_id)
            )
            if status == OFFER_PENDING:
                offered += 1
            elif status == OFFER_BLOCKED:
                # Every slot is busy (or the reserve failed): nothing was taken,
                # and asking for the rest this round would only be refused again.
                log.debug("[sweep] %s not offered: no free slot; stopping this round", subject)
                return SweepResult(
                    found=len(items),
                    offered=offered,
                    skipped=skipped,
                    declined_earlier=earlier,
                    in_progress_elsewhere=elsewhere,
                    waiting=len(items) - index,
                )
            else:
                # Held by another session, routed to another machine, taken over in
                # Papaya, or not this playbook's to act on. Not ours this round.
                log.debug("[sweep] %s not taken: someone else has it or it is not ours", subject)
                skipped += 1
        return SweepResult(
            found=len(items),
            offered=offered,
            skipped=skipped,
            declined_earlier=earlier,
            in_progress_elsewhere=elsewhere,
        )

    def sweep_from_thread(
        self,
        event_loop: asyncio.AbstractEventLoop,
        *,
        include_declined: bool = False,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Run one sweep on `event_loop` from another thread and wait for its answer.

        The supervisor socket is served on a thread; the listener, and so every
        offer, lives on the event loop. This is the one crossing between them.
        """
        future = asyncio.run_coroutine_threadsafe(
            self.sweep_once(include_declined=include_declined, by_hand=True), event_loop
        )
        return future.result(timeout=timeout).as_dict()


__all__ = [
    "DEFAULT_STALE_AFTER",
    "DEFAULT_SWEEP_INTERVAL",
    "ENDED_PHASES",
    "OPEN_STATUSES",
    "PRIORITY_RANK",
    "STATUS_RANK",
    "SWEEP_INTERVAL_ENV",
    "SWEEP_STALE_AFTER_ENV",
    "UNCHANGED_SUMMARY_EVERY",
    "SweepResult",
    "Sweeper",
    "declined_earlier",
    "declined_items",
    "declined_path",
    "envelope_for",
    "forget_declined",
    "in_progress_elsewhere",
    "interval_from_env",
    "is_open",
    "live_work_item_ids",
    "parse_interval",
    "remember_declined",
    "stale_after_from_env",
    "sweep_order",
]
