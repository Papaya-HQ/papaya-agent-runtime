"""A provider's usage limit: one classifier, the reset it names, and the pause it puts on turns.

On 2026-09-19 the Claude account hit its session limit at 17:59 UTC. Every manager
turn after that ended at once, exit 1, with the one line `You've hit your session
limit · resets 12:30pm (America/Los_Angeles)`. The lane counted each as a miss, and
after two it gave tasks 150 and 157 to a person for good: finished branches sat with no
review and no pull request long after the limit reset. A held ticket would have been
handed back. Workers died on the same line.

A usage limit is a temporary condition with a known end, never a verdict. This module
is the one place that says so:

- :func:`classify` reads a turn's or a worker's ending. Only the *terminal* line of an
  ending that failed (non-zero exit) counts, so a worker quoting "session limit" in its
  ordinary output is not a limit.
- :func:`reset_instant` turns the line's reset time into an instant in its own zone.
  The rules: a time with no zone, or one this machine cannot read, is never guessed at.
  A time of day just passed (within 15 minutes) is over; one further back is a stale
  line, not tomorrow. Every reset, streamed or read, is bounded to eight days ahead.
  Whenever no usable future reset comes out, the pause is :func:`backoff` instead
  (5 minutes doubling to 30), growing per such ending in a row.
- :func:`record` and :func:`pause` keep the pause as events in `state.db`, never in a
  process's memory, so a `ppy serve` restart still waits it out. Scope: per provider,
  per machine (this instance's ledger). A worker's `error` ending counts too, so a
  worker hitting the wall pauses the manager's turns on the same provider. A turn on
  that provider that ends normally afterwards ends the pause early (:data:`CLEAR_EVENT`):
  the limit is evidently gone. :func:`pause` never raises.
- :func:`worker_ending` finds a worker whose session the limit ended and that nothing
  has taken up since: it is resumed once after the reset, not reported as failed.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("papaya_agent_runtime.limits")

#: How a turn ended when the provider's usage limit ended it: waited out, never a miss.
LIMITED = "limited"

#: The event kind (on no task) recording one limit observation and when it ends.
LIMIT_EVENT = "provider_limited"
#: The event kind, on a worker task, recording that its limit ending was taken up.
RESUMED_EVENT = "limit_resumed"

#: What each provider's limit ending looks like. The one place to add wording, and
#: only wording seen for real. Claude's two forms were collected from this machine's
#: turn logs and worker `error` events on 2026-09-19:
#:   You've hit your session limit · resets 12:30pm (America/Los_Angeles)
#:   You've hit your weekly limit · resets Sep 22 at 7am (America/Los_Angeles)
#: No Codex limit ending has been seen yet; its tuple stays empty until one is.
PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "claude": (
        re.compile(
            r"^You['’]ve hit your (?P<which>[\w -]+?) limit\b"
            r"(?:\s*[·|–-]?\s*resets\s+(?P<when>.+?))?"
            r"\s*(?:\((?P<zone>[^()]+)\))?\s*\.?$",
            re.IGNORECASE,
        ),
    ),
    "codex": (),
}

#: The reset part of a limit line: `12:30pm`, `7am`, `Sep 22 at 7am`, `Sep 22, 7:15 pm`.
_WHEN = re.compile(
    r"^(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)$",
    re.IGNORECASE,
)
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}

#: A reset further away than this is not believed (the longest window is a week). It
#: bounds every source of a reset: the text, a streamed epoch, and a recorded pause.
LONGEST_RESET = timedelta(days=8)

#: A time-of-day reset seen up to this long after that time has just passed: the limit
#: is over, not back tomorrow. (The provider's clock and ours disagree by seconds; a turn
#: that waited for the reset can also end a few minutes after it.)
PAST_RESET_GRACE = timedelta(minutes=15)

#: The furthest ahead a time-of-day reset is believed. The only window that names a
#: bare time is the session window (five hours); a bare time further ahead than this is
#: a line seen after its own reset, never tomorrow's, and backs off instead.
LONGEST_TIME_OF_DAY = timedelta(hours=12)

#: An epoch above this is in milliseconds, not seconds (seconds pass it in the year 5138).
_MILLISECONDS = 1e11


@dataclass(frozen=True)
class Limit:
    """One limit ending: whose, what it said, when it was seen, and when it ends."""

    provider: str | None
    text: str
    at: datetime
    until: datetime
    #: True when the reset was read from the provider; False for the backoff.
    exact: bool

    def over(self, now: datetime) -> bool:
        return now >= self.until

    def said(self) -> str:
        """The pause, in the words the serve log and `ppy workers` say it."""
        who = self.provider or "the provider"
        when = self.until.strftime("%H:%M UTC")
        if self.until.date() != self.at.date():
            when = self.until.strftime("%b %d %H:%M UTC")
        how = "" if self.exact else " (no reset time could be read; trying again then)"
        return f"{who} usage limit: turns wait until {when}{how} — {self.text}"


def backoff(times: int = 1) -> float:
    """Seconds to wait on a limit whose reset cannot be read: serve's rerun delay, 30m cap."""
    from papaya_agent_runtime import serve

    return serve.rerun_delay(max(int(times), 1), None)


def _terminal_line(text: str) -> str:
    for line in reversed(str(text or "").splitlines()):
        if line.strip():
            return line.strip().lstrip("*_`> ").strip()
    return ""


def classify_text(
    text: str,
    exit_code: int | None,
    *,
    provider: str | None = None,
    at: datetime,
    times: int = 1,
    resets_at: float | None = None,
) -> Limit | None:
    """The one classifier: is this ending the provider's usage limit, and until when?

    ``text`` is the ending's output (a transcript, a tail, an `error` summary); only its
    last non-empty line is read, and only when ``exit_code`` says it failed. ``provider``
    narrows the patterns to that provider's; ``None`` tries them all. ``resets_at`` is an
    epoch the provider streamed alongside (a worker's `rate_limit_event`), which beats the
    text when it is a sane one. ``times`` is how many limits without a usable reset this
    one is in a row, for the backoff.

    A reset that is unreadable, too far ahead, or already over when the limit was hit is
    not usable: the provider has just said no, so the limit is evidently not over yet.
    That ending backs off (:func:`backoff`: 5 minutes doubling to 30), growing with each
    such ending in a row, so a wall that outlives its stated reset is never hit back to
    back and never becomes a long pause either.
    """
    if not exit_code:
        return None
    line = _terminal_line(text)
    if not line:
        return None
    names = [provider] if provider in PATTERNS else list(PATTERNS)
    for name in names:
        for pattern in PATTERNS[name]:
            match = pattern.match(line)
            if match is None:
                continue
            until = bounded(epoch_instant(resets_at), at)
            if until is None:
                until = bounded(reset_instant(match.group("when"), match.group("zone"), at), at)
            exact = until is not None and until > at
            if not exact:
                until = at + timedelta(seconds=backoff(times))
            return Limit(provider=name, text=line, at=at, until=until, exact=exact)
    return None


def epoch_instant(value: object) -> datetime | None:
    """A streamed reset epoch as an instant, or ``None`` if it is not a sane one.

    Seconds or milliseconds (told apart by size); a bool, a string, zero, a negative or
    an unrepresentable number is no answer.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    seconds = float(value)
    if not seconds > 0:
        return None
    if seconds > _MILLISECONDS:
        seconds /= 1000.0
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def bounded(until: datetime | None, at: datetime) -> datetime | None:
    """``until`` if it is believable for a limit seen at ``at``: no more than
    :data:`LONGEST_RESET` ahead, and never before ``at`` (a past reset is over)."""
    if until is None:
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    if until - at > LONGEST_RESET:
        return None
    return max(until, at)


def classify(
    result: Any, *, provider: str | None = None, at: datetime, times: int = 1
) -> Limit | None:
    """:func:`classify_text` over a turn's result (`manager.launch.TurnResult`)."""
    text = getattr(result, "transcript", None)
    if text is None:
        text = str(result or "")
    return classify_text(
        text, getattr(result, "exit_code", None), provider=provider, at=at, times=times
    )


def reset_instant(when: str | None, zone: str | None, at: datetime) -> datetime | None:
    """The instant a limit line's reset names, in UTC, or ``None`` if it cannot be read.

    ``at`` is when the line was seen. A date with no year is in ``at``'s year, or the next
    when that date is long past. A time of day is:

    - up to :data:`PAST_RESET_GRACE` before ``at``: just passed, so ``at`` (over);
    - otherwise the next such time, if that is within :data:`LONGEST_TIME_OF_DAY`;
    - otherwise no answer. Seen 16 minutes after `12:30pm` the next 12:30pm is a day
      away, which no session window is: the line is stale, and the caller backs off
      rather than pausing every turn for a day.

    No zone is no answer: a reset read in the wrong zone would be hours wrong.
    """
    if not when or not zone:
        return None
    try:
        tz = ZoneInfo(zone.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None
    match = _WHEN.match(when.strip())
    if match is None:
        return None
    hour = int(match.group("h"))
    minute = int(match.group("m") or 0)
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return None
    hour = hour % 12 + (12 if match.group("ap").lower() == "pm" else 0)
    local = at.astimezone(tz)
    if match.group("mon"):
        month = _MONTHS.get(match.group("mon").lower())
        if month is None:
            return None
        try:
            found = datetime(local.year, month, int(match.group("day")), hour, minute, tzinfo=tz)
            if found < local - timedelta(days=1):
                found = found.replace(year=local.year + 1)
        except ValueError:
            return None
    else:
        found = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if found <= local:
            if local - found <= PAST_RESET_GRACE:
                return at.astimezone(UTC)
            found = (found + timedelta(days=1)).replace(hour=hour, minute=minute)
        if found - local > LONGEST_TIME_OF_DAY:
            return None
    return bounded(found.astimezone(UTC), at.astimezone(UTC))


# ── the pause, kept as events ───────────────────────────────────────────────


def _payload(row: Any) -> dict[str, Any]:
    try:
        value = json.loads(row["payload"])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse(stamp: object) -> datetime | None:
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _from_payload(payload: dict[str, Any]) -> Limit | None:
    """A recorded observation, or ``None`` when its reset is not believable any more."""
    at = _parse(payload.get("at"))
    until = bounded(_parse(payload.get("until")), at) if at is not None else None
    if at is None or until is None:
        return None
    return Limit(
        provider=payload.get("provider") or None,
        text=str(payload.get("text") or ""),
        at=at,
        until=until,
        exact=bool(payload.get("exact")),
    )


def unreadable_in_a_row(conn: sqlite3.Connection, provider: str | None, at: datetime) -> int:
    """How many limits without a usable reset this provider hit back to back before ``at``.

    The chain breaks at an exact reset, a gap longer than the longest backoff, or a turn
    that ended normally since (:data:`CLEAR_EVENT`).
    """
    count = 0
    cleared = _cleared_at(conn, provider)
    for row in conn.execute(
        "SELECT payload FROM events WHERE kind = ? ORDER BY id DESC LIMIT 10", (LIMIT_EVENT,)
    ).fetchall():
        seen = _from_payload(_payload(row))
        if seen is None or seen.provider != provider:
            continue
        if cleared is not None and seen.at <= cleared:
            break
        if seen.exact or at - seen.until > timedelta(seconds=backoff(10)):
            break
        count += 1
        at = seen.at
    return count


def unreadable_count(provider: str | None, at: datetime) -> int:
    """:func:`unreadable_in_a_row` on its own connection (for a thread)."""
    from papaya_agent_runtime.state import db

    conn = db.init_db()
    try:
        return unreadable_in_a_row(conn, provider, at)
    finally:
        conn.close()


def record(
    limit: Limit,
    *,
    source: str,
    task_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Keep one limit observation as an event, so a restart still waits it out.

    Recorded on no task on purpose: the task a turn was about gets nothing new from its
    turn hitting the wall, and must not read this as news.
    """
    from papaya_agent_runtime.state import db, store

    own = conn is None
    conn = conn or db.init_db()
    try:
        store.append_event(
            conn,
            kind=LIMIT_EVENT,
            payload={
                "provider": limit.provider,
                "text": limit.text,
                "at": limit.at.isoformat(),
                "until": limit.until.isoformat(),
                "exact": limit.exact,
                "source": source,
                "about_task": task_id,
            },
        )
    finally:
        if own:
            conn.close()


def observe(
    result: Any, *, provider: str | None, at: datetime, source: str, task_id: int | None = None
) -> Limit | None:
    """After a manager turn: classify its ending and, if the limit ended it, keep the pause.

    Blocking (it writes the ledger); callers on an event loop run it on a thread. Said
    once in the log. A limit whose reset cannot be used backs off longer each time in a
    row. A turn that ended normally while a pause stood ends that pause (a clear event):
    the provider evidently answers again. Never raises.
    """
    try:
        limit = classify(result, provider=provider, at=at)
        if limit is None:
            if getattr(result, "exit_code", None) == 0:
                clear(provider, at)
            return None
    except Exception:  # noqa: BLE001 - classifying an ending never ends a turn
        log.exception("[limits] Could not classify a turn's ending")
        return None
    if not limit.exact:
        times = unreadable_count(limit.provider, at) + 1
        limit = classify(result, provider=provider, at=at, times=times) or limit
    record(limit, source=source, task_id=task_id)
    say_once(limit)
    return limit


def _worker_limits(conn: sqlite3.Connection) -> list[tuple[int, int, Limit]]:
    """`(event id, task id, limit)` for the newest worker `error` endings that were limits."""
    found = []
    rows = conn.execute(
        "SELECT e.id, e.task_id, e.payload, e.created_at, t.provider FROM events e "
        "LEFT JOIN tasks t ON t.id = e.task_id "
        "WHERE e.kind = 'error' AND e.payload LIKE '%limit%' ORDER BY e.id DESC LIMIT 20"
    ).fetchall()
    for row in rows:
        limit = _worker_limit(conn, row)
        if limit is not None:
            found.append((int(row["id"]), int(row["task_id"] or 0), limit))
    return found


def _worker_limit(conn: sqlite3.Connection, row: Any) -> Limit | None:
    payload = _payload(row)
    at = _parse(row["created_at"])
    if at is None:
        return None
    # The provider's own stream says when it resets, exactly; the text is the fallback.
    streamed = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND id < ? "
        "AND kind = 'worker_rate_limit_event' ORDER BY id DESC LIMIT 1",
        (row["task_id"], row["id"]),
    ).fetchone()
    resets_at = None
    if streamed is not None:
        stream = _payload(streamed)
        info = stream.get("rate_limit_info")
        same_session = stream.get("session_id") == payload.get("session_id")
        if same_session and isinstance(info, dict) and info.get("status") == "rejected":
            resets_at = info.get("resetsAt")
    try:
        code = payload.get("exit_code")
        exit_code = 1 if code is None else int(code)
    except (TypeError, ValueError):
        exit_code = 1
    return classify_text(
        str(payload.get("summary") or ""),
        exit_code,
        provider=row["provider"] or None,
        at=at,
        resets_at=resets_at,
    )


def pause(conn: sqlite3.Connection, provider: str | None, now: datetime) -> Limit | None:
    """The limit turns on ``provider`` wait out at ``now``, or ``None``. ``None`` = any provider.

    The newest end among every recorded observation, the manager's turns and the
    workers' `error` endings alike: one observation pauses every turn on that provider.
    An observation older than a turn on that provider that ended normally is over.
    Never raises: a record that cannot be read is no pause, and the owed lane and every
    round call this unguarded.
    """
    try:
        candidates: list[Limit] = []
        for row in conn.execute(
            "SELECT payload FROM events WHERE kind = ? ORDER BY id DESC LIMIT 20", (LIMIT_EVENT,)
        ).fetchall():
            seen = _from_payload(_payload(row))
            if seen is not None:
                candidates.append(seen)
        candidates += [limit for _id, _task, limit in _worker_limits(conn)]
        cleared = {name: _cleared_at(conn, name) for name in {c.provider for c in candidates}}
        live = [
            c
            for c in candidates
            if now < c.until
            and (provider is None or c.provider in (None, provider))
            and (cleared[c.provider] is None or c.at > cleared[c.provider])
        ]
        return max(live, key=lambda c: c.until) if live else None
    except Exception:  # noqa: BLE001 - a pause is a courtesy, never a crash
        log.exception("[limits] Could not read the usage-limit pause")
        return None


#: The event kind (on no task) recording that a turn on a provider ended normally while a
#: pause stood: the pause is over, whatever reset it named.
CLEAR_EVENT = "provider_limit_cleared"


def _cleared_at(conn: sqlite3.Connection, provider: str | None) -> datetime | None:
    """When a turn on ``provider`` last ended normally during a pause, or ``None``."""
    for row in conn.execute(
        "SELECT payload FROM events WHERE kind = ? ORDER BY id DESC LIMIT 20", (CLEAR_EVENT,)
    ).fetchall():
        payload = _payload(row)
        if provider is None or payload.get("provider") in (None, provider):
            return _parse(payload.get("at"))
    return None


def clear(provider: str | None, at: datetime) -> bool:
    """A turn on ``provider`` ended normally at ``at``: end any pause standing then.

    Recorded only when a pause stood, so ordinary turns write nothing. Returns whether
    one was ended. Never raises.
    """
    from papaya_agent_runtime.state import db, store

    try:
        conn = db.init_db()
    except Exception:  # noqa: BLE001
        return False
    try:
        if pause(conn, provider, at) is None:
            return False
        store.append_event(
            conn, kind=CLEAR_EVENT, payload={"provider": provider, "at": at.isoformat()}
        )
        log.warning("[limits] A %s turn ended normally: the usage-limit pause is over", provider)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        conn.close()


def paused(provider: str | None, now: datetime) -> Limit | None:
    """:func:`pause` on its own connection. Never raises: an unreadable ledger is no pause."""
    from papaya_agent_runtime.state import db

    try:
        conn = db.init_db()
    except Exception:  # noqa: BLE001 - a pause is a courtesy, not a gate
        return None
    try:
        return pause(conn, provider, now)
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()


#: Pauses already said in this process's log, so a pause is said once, not every round.
_said: set[tuple[str | None, str]] = set()


def say_once(limit: Limit, where: logging.Logger | None = None) -> bool:
    """Log the pause once per process and reset. Returns whether it was said now."""
    key = (limit.provider, limit.until.isoformat())
    if key in _said:
        return False
    _said.add(key)
    (where or log).warning("[limits] Paused: %s", limit.said())
    return True


# ── a worker the limit ended ────────────────────────────────────────────────

#: Events after a limit ending that mean the worker was taken up since (or finished).
_TAKEN_UP = ("worker_done", "resumed", "steer", "delivered", "dispatched", RESUMED_EVENT)

#: What the resumed worker is told.
RESUME_MESSAGE = (
    "Your session was ended by the provider's usage limit, which has now reset. Nothing "
    "was wrong with your work: pick it up where you left off in this worktree and carry "
    "on with the brief."
)


@dataclass(frozen=True)
class WorkerEnding:
    """A worker whose session the limit ended, and that nothing has taken up since."""

    task_id: int
    event_id: int
    limit: Limit


def worker_ending(conn: sqlite3.Connection, task_id: int) -> WorkerEnding | None:
    """The worker's newest `error` ending, when it was the limit and it still stands.

    Never raises (the owed lane calls it for every owed worker): unreadable is ``None``.
    """
    try:
        return _worker_ending(conn, task_id)
    except Exception:  # noqa: BLE001
        log.exception("[limits] Could not read worker task %d's ending", task_id)
        return None


def _worker_ending(conn: sqlite3.Connection, task_id: int) -> WorkerEnding | None:
    row = conn.execute(
        "SELECT e.id, e.task_id, e.payload, e.created_at, t.provider, t.status FROM events e "
        "JOIN tasks t ON t.id = e.task_id WHERE e.task_id = ? AND e.kind = 'error' "
        "ORDER BY e.id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None or row["status"] not in ("failed", "worker_stopped", "needs_recovery"):
        return None
    marks = ",".join("?" for _ in _TAKEN_UP)
    later = conn.execute(
        f"SELECT 1 FROM events WHERE task_id = ? AND id > ? AND kind IN ({marks}) LIMIT 1",
        (task_id, row["id"], *_TAKEN_UP),
    ).fetchone()
    if later is not None:
        return None
    limit = _worker_limit(conn, row)
    if limit is None:
        return None
    return WorkerEnding(task_id=task_id, event_id=int(row["id"]), limit=limit)


def resume_worker(ending: WorkerEnding, *, steer) -> str:
    """Take a limit-ended worker up once: record it first, then resume it through ``steer``.

    Recorded before the steer, so a refused steer is still the one attempt: the worker
    then goes to the review turn like any other failure.
    """
    from papaya_agent_runtime.state import db, store

    conn = db.init_db()
    try:
        task = store.get_task(conn, ending.task_id)
        store.append_event(
            conn,
            kind=RESUMED_EVENT,
            payload={
                "task_id": ending.task_id,
                "error_id": ending.event_id,
                "until": ending.limit.until.isoformat(),
            },
            run_id=int(task["run_id"]) if task is not None else None,
            task_id=ending.task_id,
        )
    finally:
        conn.close()
    steer(ending.task_id, RESUME_MESSAGE)
    return f"worker task {ending.task_id} resumed after the usage limit reset"


__all__ = [
    "LIMITED",
    "CLEAR_EVENT",
    "LIMIT_EVENT",
    "PAST_RESET_GRACE",
    "PATTERNS",
    "RESUMED_EVENT",
    "RESUME_MESSAGE",
    "Limit",
    "WorkerEnding",
    "backoff",
    "classify",
    "bounded",
    "classify_text",
    "clear",
    "epoch_instant",
    "observe",
    "pause",
    "paused",
    "record",
    "reset_instant",
    "resume_worker",
    "say_once",
    "unreadable_count",
    "unreadable_in_a_row",
    "worker_ending",
]
