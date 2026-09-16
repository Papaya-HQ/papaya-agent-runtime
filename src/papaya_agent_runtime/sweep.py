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
- a full pool ends the round, and the rest wait for the next one.

Each sweep says one line on stderr — found, offered, skipped — and nothing on the
supervised protocol, which is for jobs, not for bookkeeping.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from papaya_agent_runtime import papaya_events

log = logging.getLogger("papaya_agent_runtime.sweep")

#: How often `serve` sweeps when nobody says otherwise: five minutes.
DEFAULT_SWEEP_INTERVAL = 300.0

#: The environment variable that sets the interval when `--sweep-interval` does not.
SWEEP_INTERVAL_ENV = "PPY_SWEEP_INTERVAL"

#: The work item statuses that still want somebody working on them.
OPEN_STATUSES = frozenset({"todo", "in_progress", "blocked", "changes_requested"})

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


def parse_interval(raw: str | float, *, source: str) -> float:
    """A sweep interval in seconds: zero or more, where zero turns the timer off."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{source} must be a number of seconds, not {raw!r}") from None
    if value < 0 or value != value:  # NaN is not a cadence either
        raise ValueError(f"{source} must be zero or more seconds, not {raw!r}")
    return value


@dataclass(frozen=True)
class SweepResult:
    """What one sweep found and did with it."""

    found: int = 0
    offered: int = 0
    skipped: int = 0
    #: Open items not reached because every slot was busy; the next sweep has them.
    waiting: int = 0
    #: Why the sweep could not ask Papaya at all, when it could not.
    error: str | None = None

    def summary(self) -> str:
        if self.error is not None:
            return f"sweep could not list assigned work: {self.error}"
        line = f"sweep found {self.found}, offered {self.offered}, skipped {self.skipped}"
        if self.waiting:
            line += f"; {self.waiting} left for the next sweep (every slot is busy)"
        return line

    def as_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "offered": self.offered,
            "skipped": self.skipped,
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


def is_open(item: dict[str, Any]) -> bool:
    return str(item.get("status") or "").strip().lower() in OPEN_STATUSES


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
    ) -> None:
        self._built = built
        self._interval = float(interval)
        self._stderr = stderr
        # Seam for tests: the timer, fired on demand instead of every five minutes.
        self._sleep = sleep or asyncio.sleep
        self._live_items = live_items or live_work_item_ids
        # A sweep asked for by hand and one on the timer never interleave: two
        # rounds offering the same item at once would each see it as not yet held.
        self._lock = asyncio.Lock()
        self.results: list[SweepResult] = []

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

    async def sweep_once(self) -> SweepResult:
        """One round: list, choose, offer. Never raises; a failure is the result."""
        async with self._lock:
            try:
                result = await self._sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad sweep must not end serve
                log.warning("[sweep] Sweep failed: %s", exc)
                result = SweepResult(error=str(exc) or exc.__class__.__name__)
        self.results.append(result)
        if self._stderr is not None:
            with contextlib.suppress(Exception):
                print(f"ppy serve: {result.summary()}", file=self._stderr, flush=True)
        return result

    async def _sweep(self) -> SweepResult:
        from papaya_agent_client import api_client

        built = self._built
        try:
            answer = await api_client.list_assigned_work_items(built.api)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - an unreachable Papaya is a quiet round
            log.warning("[sweep] Could not list assigned work items: %s", exc)
            return SweepResult(error=str(exc) or exc.__class__.__name__)

        items = [item for item in _items(answer) if is_open(item)]
        live = await asyncio.to_thread(self._live_items)
        agent_config = getattr(built, "agent_config", None) or {}
        agent_id = str(agent_config.get("agent_id") or "")
        workspace_id = str(agent_config.get("workspace_id") or "")

        offered = skipped = 0
        for index, item in enumerate(items):
            item_id = str(item["id"])
            subject = f"work_item:{item_id}"
            if subject in built.loop.running_subjects or item_id in live:
                log.debug("[sweep] %s already has a live task here; not offering it", subject)
                skipped += 1
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
                    waiting=len(items) - index,
                )
            else:
                # Held by another session, routed to another machine, taken over in
                # Papaya, or not this playbook's to act on. Not ours this round.
                log.debug("[sweep] %s not taken: someone else has it or it is not ours", subject)
                skipped += 1
        return SweepResult(found=len(items), offered=offered, skipped=skipped)

    def sweep_from_thread(
        self, event_loop: asyncio.AbstractEventLoop, *, timeout: float = REQUEST_TIMEOUT_SECONDS
    ) -> dict[str, Any]:
        """Run one sweep on `event_loop` from another thread and wait for its answer.

        The supervisor socket is served on a thread; the listener, and so every
        offer, lives on the event loop. This is the one crossing between them.
        """
        future = asyncio.run_coroutine_threadsafe(self.sweep_once(), event_loop)
        return future.result(timeout=timeout).as_dict()


__all__ = [
    "DEFAULT_SWEEP_INTERVAL",
    "ENDED_PHASES",
    "OPEN_STATUSES",
    "SWEEP_INTERVAL_ENV",
    "SweepResult",
    "Sweeper",
    "envelope_for",
    "interval_from_env",
    "is_open",
    "live_work_item_ids",
    "parse_interval",
]
