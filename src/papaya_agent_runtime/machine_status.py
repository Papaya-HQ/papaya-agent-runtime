"""What this machine is doing and what it needs, told to Papaya as one snapshot.

A person asked the hosted agent "what is my machine working on?" and it could not
say: everything it would need was on this machine's ledger and nowhere else (Shane,
2026-09-22). The backend now keeps one snapshot per connection
(`PUT .../polyweave-agents/me/connection/status`, backend PR #1024) and renders it
into the hosted agent's `status_digest`. This module is the machine's half:

- :func:`build` reads the ledger once, in one read transaction, into a
  `MachineStatusSnapshot` exactly as the wire bounds it — what is in flight, what
  waits on a person (with the instruction that person can send back), what is
  blocked, what finished, capacity and health. Nothing in it is private: every
  string is redacted (tokens, home paths, email addresses), made one line, and cut
  to its bound with an ellipsis rather than refused.
- :class:`Publisher` sends it: one lock, the body built inside it (so the last body
  built is the last one sent), an identical body not sent again within
  :data:`DEDUP_SECONDS`, a 422 logged once with the field and never retried with
  that body, and a network failure logged without stopping anything.
- :func:`change_mark` is what `ppy serve` watches between rounds, so a phase that
  moved, an ask that appeared or resolved, or a capability request decided in
  another process (a turn's `ppy capability approve`) is published at once.

`ppy serve` publishes every round and on change (`rounds.Rounds`); a session
publishes once at the end of `ppy status` (:func:`publish_once`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from papaya_agent_runtime.state import store

log = logging.getLogger("papaya_agent_runtime.machine_status")

#: The wire's bounds (`MachineStatusSnapshot`, backend task 350).
MAX_BYTES = 65_536
LIST_MAX = 25
FINISHED_MAX = 10
REF_MAX = 200
TITLE_MAX = 300
PHASE_MAX = 100
TEXT_MAX = 500
HOW_MAX = 300
SUMMARY_MAX = 300
HEADLINE_MAX = 300
SHORT_ID_MAX = 40
URL_MAX = 2000
COUNT_MAX = 1000
NEED_KINDS = ("decision", "capability", "pull_request", "question", "blocker")
HEALTH_STATES = ("ready", "degraded", "blocked")

#: An identical body is not sent again within this many seconds.
DEDUP_SECONDS = 30.0
#: How often `ppy serve` looks for a change between rounds.
CHANGE_POLL_SECONDS = 10.0

#: The worker statuses that are in flight: running, or finished and waiting on review.
RUNNING = ("requested", "in_progress")
REVIEWABLE = ("worker_done",)
#: A worker in one of these is stopped and waits on something.
STOPPED = ("blocked", "worker_stopped", "needs_recovery", "failed")
FINISHED = ("delivered", "closed")

#: The ledger events whose arrival means the snapshot may have changed.
CHANGE_KINDS = (
    store.TICKET_PHASE_EVENT,
    "capability_request",
    "capability_decision",
    "needs_a_person",
    "delivered",
    "instruction_replied",
    "instruction_reported",
)

ELLIPSIS = "…"


# ── strings, as the wire takes them ─────────────────────────────────────────


def clean(text: object, limit: int, *, fallback: str = "-", own: str | None = None) -> str:
    """``text`` redacted, on one line, and cut to ``limit`` characters with an ellipsis.

    Never empty: the wire refuses an empty required string, so ``fallback`` stands
    in for one. Cutting is this side's job; a body is never refused locally.

    No `MI-<n>` either: a request's id is internal, and the hosted agent reads this
    snapshot to a person and quoted it (2026-09-23). A request is named by its title;
    ``own`` is the request the row is about, whose id reads "this request", and a
    sentence about any other request is dropped (`instructions.without_ids`).
    """
    from papaya_agent_runtime import blockers, instructions

    one = " ".join(instructions.without_ids(blockers.redact(str(text or "")), own).split())
    if not one:
        one = fallback
    return one if len(one) <= limit else one[: limit - 1].rstrip() + ELLIPSIS


def url_or_none(value: object) -> str | None:
    text = str(value or "").strip()
    if not re.match(r"https?://\S+$", text) or len(text) > URL_MAX:
        return None
    return text


def stamp(value: object, fallback: datetime) -> str:
    """An ISO-8601 time with a timezone, from a ledger stamp or ``fallback``."""
    moment: datetime | None = None
    if isinstance(value, datetime):
        moment = value
    elif value:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            moment = None
    moment = moment or fallback
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat(timespec="seconds")


def _count(value: object) -> int:
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0
    return max(0, min(COUNT_MAX, number))


# ── what the snapshot is built from ─────────────────────────────────────────


@dataclass(frozen=True)
class Health:
    state: str
    headline: str


def health_of(verdict: Any) -> Health:
    """The snapshot's health from a readiness verdict."""
    from papaya_agent_runtime import readiness

    state = str(getattr(verdict, "state", "") or "")
    if state not in HEALTH_STATES:
        state = "degraded"
    try:
        headline = readiness.headline(verdict)
    except Exception:  # noqa: BLE001 - a verdict with no headline still has a state
        headline = ""
    return Health(state, headline or ("All good." if state == "ready" else state))


def _payload(row: Any) -> dict[str, Any]:
    try:
        value = json.loads(row["payload"])
    except (TypeError, ValueError, KeyError, IndexError):
        return {}
    return value if isinstance(value, dict) else {}


def _run_env(conn: sqlite3.Connection, run_id: int, key: str) -> str:
    row = conn.execute(
        "SELECT task_env.value FROM task_env JOIN tasks ON tasks.id = task_env.task_id "
        "WHERE tasks.run_id = ? AND task_env.key = ? ORDER BY tasks.id DESC LIMIT 1",
        (run_id, key),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] else ""


def _about_request(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    """A request a person sent this machine: by its ticket's task, never its `MI-<n>`.

    The wire's `short_id` is an identifier ("the identifier a person reads and says",
    `SnapshotAbout`), not a title: the request's title is the row's `title`. Its
    `MI-<n>` is internal (quoted to a person by the hosted agent, 2026-09-23), so the
    identifier is the ticket's task, as every other row's `ref` is.
    """
    from papaya_agent_runtime import instructions

    row = conn.execute(
        "SELECT tasks.id FROM tasks JOIN task_env ON task_env.task_id = tasks.id "
        "WHERE tasks.run_id = ? AND task_env.key = ? ORDER BY tasks.id LIMIT 1",
        (run_id, instructions.INSTRUCTION_KEY),
    ).fetchone()
    return {
        "kind": "machine_instruction",
        "short_id": clean(f"task-{int(row['id'])}" if row is not None else "", SHORT_ID_MAX),
        "url": None,
    }


def _request_of(conn: sqlite3.Connection, run_id: int) -> str | None:
    """The `MI-<n>` of the request a run works, if one: what its rows' own id is."""
    from papaya_agent_runtime import instructions

    return _run_env(conn, run_id, instructions.INSTRUCTION_KEY) or None


def _request_of_task(conn: sqlite3.Connection, task_id: object) -> str | None:
    if task_id is None:
        return None
    row = conn.execute("SELECT run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _request_of(conn, int(row["run_id"])) if row is not None else None


def about(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    """What a task in ``run_id`` is for: its ticket's work item, or its instruction."""
    from papaya_agent_runtime import instructions, papaya_events

    short = _run_env(conn, run_id, instructions.INSTRUCTION_KEY)
    if short:
        return _about_request(conn, run_id)
    key = _run_env(conn, run_id, papaya_events.WORK_ITEM_KEY)
    item = ""
    metadata = _run_env(conn, run_id, papaya_events.PAPAYA_EVENT_METADATA)
    if metadata:
        try:
            item = str(json.loads(metadata).get("work_item_id") or "")
        except ValueError:
            item = ""
    if not key and not item:
        return None
    return {
        "kind": "work_item",
        "short_id": clean(key or item[:8], SHORT_ID_MAX),
        "url": url_or_none(_run_env(conn, run_id, papaya_events.WORK_ITEM_URL)),
    }


def _phase(conn: sqlite3.Connection, task_id: int, status: str) -> str:
    latest = store.latest_progress(conn, task_id)
    phase = _payload(latest).get("phase") if latest is not None else None
    return str(phase or status)


def _in_flight(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    from papaya_agent_runtime import instructions

    marks = ",".join("?" for _ in (*RUNNING, *REVIEWABLE))
    rows = conn.execute(
        f"SELECT id, run_id, title, status, updated_at, created_at FROM tasks "
        f"WHERE {store.WORKER_TASK} AND status IN ({marks}) ORDER BY id",
        (*RUNNING, *REVIEWABLE),
    ).fetchall()
    found = []
    runs_with_workers = set()
    for row in rows:
        runs_with_workers.add(int(row["run_id"]))
        status = str(row["status"])
        phase = (
            "waiting on review" if status in REVIEWABLE else _phase(conn, int(row["id"]), status)
        )
        found.append(
            {
                "ref": clean(f"task-{int(row['id'])}", REF_MAX),
                "title": clean(row["title"], TITLE_MAX, own=_request_of(conn, int(row["run_id"]))),
                "about": about(conn, int(row["run_id"])),
                "phase": clean(phase, PHASE_MAX),
                "since": stamp(row["updated_at"] or row["created_at"], now),
            }
        )
    # An instruction being answered has no worker: its ticket is what is in flight.
    for ticket in instructions.open_tickets(conn):
        if ticket["run_id"] in runs_with_workers:
            continue
        found.append(
            {
                "ref": clean(f"task-{ticket['task_id']}", REF_MAX),
                "title": clean(ticket["title"], TITLE_MAX, own=ticket["short_id"]),
                "about": _about_request(conn, int(ticket["run_id"])),
                "phase": clean(ticket["phase"], PHASE_MAX),
                "since": stamp(ticket["since"], now),
            }
        )
    return found


def _pr_number(url: str | None) -> str:
    from papaya_agent_runtime.delivery import pr_number

    number = pr_number(url) if url else None
    return str(number) if number else ""


def how_for(kind: str, key: str, *, pr: str = "", merge_allowed: bool = False) -> str:
    """The instruction a person can send back to unblock an ask of ``kind``."""
    ident = key.rsplit(":", 1)[-1]
    if kind == "capability":
        return f"Send me: approve capability {ident} (or: deny capability {ident} because ...)"
    if kind == "pull_request":
        name = f"PR {pr}" if pr else f"the pull request for task {ident}"
        if merge_allowed:
            return f"Send me: merge {name}, or hold {name}"
        return f"Review and merge it on GitHub, or send me: hold {name}"
    if kind == "decision":
        return f"Reply here with your decision (todo {ident})"
    return "Reply here with what you want done"


def _needs_you(
    conn: sqlite3.Connection, now: datetime, *, merge_allowed: bool
) -> list[dict[str, Any]]:
    from papaya_agent_runtime import capability_requests, outreach, team

    asks = list(outreach.collect(conn))
    keys = {ask.key for ask in asks}
    # A pending request is the manager's first, but a person may decide it too, and on
    # an instruction's work path no turn may approve one: said, so it can be sent back.
    for item in capability_requests.pending(conn):
        key = f"capability:{item.id}"
        if key in keys:
            continue
        since = conn.execute("SELECT created_at FROM events WHERE id = ?", (item.id,)).fetchone()
        asks.append(
            outreach.Ask(
                key=key,
                kind=outreach.CAPABILITY,
                text=f"worker task {item.task_id} asks to run `{item.label}`"
                + (f" — {item.why}" if item.why else ""),
                how="",
                task_id=item.task_id,
                since=since["created_at"] if since is not None else None,
            )
        )
    found = []
    for ask in asks:
        kind = ask.kind if ask.kind in NEED_KINDS else "question"
        pr = ""
        ref = ask.key.replace(":", "-")
        if kind == "pull_request" and ask.task_id is not None:
            pr = _pr_number(team._pr_url(conn, int(ask.task_id)))
            ref = f"pr-{pr}" if pr else f"task-{ask.task_id}-pr"
        found.append(
            {
                "kind": kind,
                "ref": clean(ref, REF_MAX),
                "text": clean(ask.text, TEXT_MAX, own=_request_of_task(conn, ask.task_id)),
                "how": clean(how_for(kind, ask.key, pr=pr, merge_allowed=merge_allowed), HOW_MAX),
                "since": stamp(ask.since, now),
            }
        )
    return found


def _blocked(conn: sqlite3.Connection, now: datetime, blockers_now: list[dict[str, Any]]):
    found = []
    for blocker in blockers_now:
        steps = blocker.get("steps") or []
        found.append(
            {
                "kind": "blocker",
                "ref": clean(f"blocker-{blocker.get('code') or 'setup'}", REF_MAX),
                "text": clean(blocker.get("title"), TEXT_MAX),
                "how": clean(steps[0] if steps else "", HOW_MAX, fallback="See ppy readiness"),
                "since": stamp(blocker.get("since"), now),
            }
        )
    for row in conn.execute(
        "SELECT id, task_id, text, blocked_on, created_at FROM todos WHERE status = 'open' "
        "AND blocked_on IS NOT NULL AND blocked_on != '' AND blocked_on != 'user' "
        "AND blocked_on NOT LIKE 'user:%' ORDER BY id"
    ).fetchall():
        found.append(
            {
                "kind": "blocker",
                "ref": clean(f"todo-{int(row['id'])}", REF_MAX),
                "text": clean(row["text"], TEXT_MAX, own=_request_of_task(conn, row["task_id"])),
                "how": clean(f"Waiting on {row['blocked_on']}; nothing for you yet", HOW_MAX),
                "since": stamp(row["created_at"], now),
            }
        )
    marks = ",".join("?" for _ in STOPPED)
    for row in conn.execute(
        f"SELECT id, title, status, updated_at FROM tasks WHERE {store.WORKER_TASK} "
        f"AND status IN ({marks}) ORDER BY id",
        STOPPED,
    ).fetchall():
        status = str(row["status"])
        found.append(
            {
                "kind": "question" if status == "blocked" else "blocker",
                "ref": clean(f"task-{int(row['id'])}", REF_MAX),
                "text": clean(
                    f"{row['title']} ({status.replace('_', ' ')})",
                    TEXT_MAX,
                    own=_request_of_task(conn, row["id"]),
                ),
                "how": clean(
                    f"The manager is taking it up; send me: what is task {int(row['id'])} "
                    "waiting on?",
                    HOW_MAX,
                ),
                "since": stamp(row["updated_at"], now),
            }
        )
    return found


def _recently_finished(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    from papaya_agent_runtime import instructions, team

    marks = ",".join("?" for _ in FINISHED)
    rows = conn.execute(
        f"SELECT id, title, status, merged_at, updated_at FROM tasks WHERE {store.WORKER_TASK} "
        f"AND status IN ({marks}) ORDER BY updated_at DESC, id DESC LIMIT ?",
        (*FINISHED, FINISHED_MAX),
    ).fetchall()
    found = []
    for row in rows:
        url = team._pr_url(conn, int(row["id"]))
        if row["merged_at"]:
            outcome = "PR merged"
        elif row["status"] == "delivered":
            outcome = "PR opened" if url else "delivered"
        else:
            outcome = "closed"
        found.append(
            {
                "ref": clean(f"task-{int(row['id'])}", REF_MAX),
                "title": clean(row["title"], TITLE_MAX, own=_request_of_task(conn, row["id"])),
                "outcome": clean(outcome, TITLE_MAX),
                "url": url_or_none(url),
                "at": stamp(row["merged_at"] or row["updated_at"], now),
            }
        )
    for done in instructions.finished_tickets(conn, limit=FINISHED_MAX):
        found.append(
            {
                "ref": clean(f"task-{done['task_id']}", REF_MAX),
                "title": clean(done["title"], TITLE_MAX, own=done["short_id"]),
                "outcome": clean(done["outcome"], TITLE_MAX),
                "url": url_or_none(done.get("url")),
                "at": stamp(done["at"], now),
            }
        )
    found.sort(key=lambda row: row["at"], reverse=True)
    return found[:FINISHED_MAX]


def _summary(in_flight: list, needs: list, blocked: list, health: Health) -> str:
    running = len(in_flight)
    parts = [
        f"{running} task{'s' if running != 1 else ''} in flight" if running else "Nothing running"
    ]
    if needs:
        parts.append(f"{len(needs)} waiting on you")
    if blocked:
        parts.append(f"{len(blocked)} blocked")
    if health.state != "ready":
        parts.append(f"health {health.state}")
    return clean("; ".join(parts) + ".", SUMMARY_MAX)


def size(body: dict[str, Any]) -> int:
    """The body's compact JSON size in bytes: what the wire's 64 KiB bound measures."""
    return len(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def fit(body: dict[str, Any]) -> dict[str, Any]:
    """Drop rows from the end of the longest list until the body is under :data:`MAX_BYTES`."""
    lists = ("recently_finished", "blocked", "in_flight", "needs_you")
    while size(body) > MAX_BYTES:
        longest = max(lists, key=lambda name: len(body.get(name) or []))
        if not body.get(longest):
            break
        body[longest].pop()
    return body


def build(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    health: Health,
    max_concurrent: int,
    merge_allowed: bool = False,
    blockers_now: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The `MachineStatusSnapshot` for this machine now, within every wire bound.

    One read transaction: a worker writing the ledger while this runs cannot make
    the snapshot count a task as both running and finished.
    """
    from papaya_agent_runtime import blockers

    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    blockers_now = blockers.current() if blockers_now is None else blockers_now
    began = not conn.in_transaction
    if began:
        conn.execute("BEGIN")
    try:
        in_flight = _in_flight(conn, now)
        needs = _needs_you(conn, now, merge_allowed=merge_allowed)
        blocked = _blocked(conn, now, blockers_now)
        finished = _recently_finished(conn, now)
        # A slot is a running worker; one done and waiting on review holds none.
        marks = ",".join("?" for _ in RUNNING)
        in_use = conn.execute(
            f"SELECT COUNT(*) FROM tasks WHERE {store.WORKER_TASK} AND status IN ({marks})",
            RUNNING,
        ).fetchone()[0]
    finally:
        if began:
            conn.rollback()
    body = {
        "as_of": stamp(now, now),
        "summary": _summary(in_flight, needs, blocked, health),
        "in_flight": in_flight[:LIST_MAX],
        "needs_you": needs[:LIST_MAX],
        "blocked": blocked[:LIST_MAX],
        "recently_finished": finished[:FINISHED_MAX],
        "capacity": {"max_concurrent": _count(max_concurrent), "in_use": _count(in_use)},
        "health": {
            "state": health.state if health.state in HEALTH_STATES else "degraded",
            "headline": clean(health.headline, HEADLINE_MAX),
        },
    }
    return fit(body)


def change_mark(conn: sqlite3.Connection) -> tuple[Any, ...]:
    """What changes when the snapshot might: newest relevant event, todos, task statuses."""
    marks = ",".join("?" for _ in CHANGE_KINDS)
    event = conn.execute(
        f"SELECT COALESCE(MAX(id), 0) FROM events WHERE kind IN ({marks})", CHANGE_KINDS
    ).fetchone()[0]
    todos = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM todos WHERE status = 'open'"
    ).fetchone()
    tasks = conn.execute("SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM tasks").fetchone()
    return (int(event), int(todos[0]), str(todos[1]), int(tasks[0]), str(tasks[1]))


# ── publishing ──────────────────────────────────────────────────────────────


def _comparable(body: dict[str, Any]) -> str:
    """The body without its clock, so two builds of the same state compare equal."""
    return json.dumps(
        {key: value for key, value in body.items() if key != "as_of"},
        sort_keys=True,
        separators=(",", ":"),
    )


SENT = "sent"
UNCHANGED = "unchanged"
REFUSED = "refused"
FAILED = "failed"
SKIPPED = "skipped"


class Publisher:
    """Send the snapshot: one at a time, never the same body twice in a row within 30 s.

    ``build`` returns the body now (or ``None``: nothing to publish, e.g. no ledger);
    ``put`` sends it and returns whether a call was made (``False``: not connected).
    Both are blocking and run on a thread. The body is built *inside* the lock, so
    two callers — the round and the change watch — never interleave, and whichever
    goes second builds and sends the newer state.
    """

    def __init__(
        self,
        *,
        build: Callable[[], dict[str, Any] | None],
        put: Callable[[dict[str, Any]], bool],
        clock: Callable[[], float] = time.monotonic,
        dedup_seconds: float = DEDUP_SECONDS,
    ) -> None:
        self._build = build
        self._put = put
        self._clock = clock
        self._dedup = float(dedup_seconds)
        self._lock = asyncio.Lock()
        self._last: str | None = None
        self._last_at: float | None = None
        #: Bodies Papaya refused with a 422: never sent again as they are.
        self._refused: set[str] = set()
        self.sent: list[dict[str, Any]] = []

    async def publish(self, why: str = "round") -> str:
        """Build and send the snapshot now. Returns what happened; never raises."""
        from papaya_agent_runtime import papaya_events

        async with self._lock:
            try:
                body = await asyncio.to_thread(self._build)
            except Exception as exc:  # noqa: BLE001 - a snapshot never stops a round
                log.warning("[status] Could not build the status snapshot: %s", exc)
                return FAILED
            if body is None:
                return SKIPPED
            same = _comparable(body)
            if same in self._refused:
                return REFUSED
            if (
                same == self._last
                and self._last_at is not None
                and self._clock() - self._last_at < self._dedup
            ):
                return UNCHANGED
            try:
                called = await asyncio.to_thread(self._put, body)
            except papaya_events.PapayaHTTPError as exc:
                if exc.code == 422:
                    self._refused.add(same)
                    fields = ", ".join(exc.fields()) or "an unnamed field"
                    log.warning(
                        "[status] Papaya refused the status snapshot (422) on %s; "
                        "this body is not sent again",
                        fields,
                    )
                    return REFUSED
                log.warning("[status] Could not publish the status snapshot: %s", exc)
                return FAILED
            except Exception as exc:  # noqa: BLE001 - the round goes on without it
                log.warning("[status] Could not publish the status snapshot: %s", exc)
                return FAILED
            if not called:
                return SKIPPED
            self._last, self._last_at = same, self._clock()
            self.sent.append(body)
            log.debug("[status] Published the status snapshot (%s)", why)
            return SENT

    async def watch(
        self,
        mark: Callable[[], Any],
        *,
        sleep: Callable[[float], Any] | None = None,
        interval: float = CHANGE_POLL_SECONDS,
    ) -> None:
        """Publish whenever ``mark`` changes, checked every ``interval``. Runs until cancelled."""
        sleep = sleep or asyncio.sleep
        last: Any = None
        while True:
            await sleep(interval)
            try:
                now = await asyncio.to_thread(mark)
            except Exception as exc:  # noqa: BLE001 - an unreadable mark is looked at again
                log.debug("[status] Could not read the change mark: %s", exc)
                continue
            if last is not None and now != last:
                await self.publish("change")
            last = now


# ── the defaults both modes use ─────────────────────────────────────────────


def merge_allowed() -> bool:
    """Whether this install lets the runtime merge a pull request (`authority.merge`)."""
    from papaya_agent_runtime.config import ConfigError, load_config

    try:
        return bool(load_config().authority.merge)
    except (ConfigError, AttributeError):
        return False


def max_concurrent() -> int:
    from papaya_agent_runtime.config import ConfigError, WorkerCeiling, load_config

    try:
        return int(load_config().worker.max_concurrent)
    except (ConfigError, AttributeError, TypeError, ValueError):
        return WorkerCeiling().max_concurrent


#: How long a readiness verdict is reused for the snapshot's health. Readiness asks the
#: machine (`gh auth status`, disk, docker); the change watch may publish every ten
#: seconds, and the blocker watch already re-checks on the rounds' clock.
VERDICT_MAX_AGE = 300.0
_verdict: list[Any] = []


def _readiness_now() -> Any:
    from papaya_agent_runtime import readiness
    from papaya_agent_runtime.paths import ppy_home

    now = time.monotonic()
    # Kept for one install and one checker: another home, or another `check`, reads afresh.
    key = (str(ppy_home()), id(readiness.check))
    if _verdict and _verdict[1] == key and now - _verdict[0] < VERDICT_MAX_AGE:
        return _verdict[2]
    verdict = readiness.check()
    _verdict[:] = [now, key, verdict]
    return verdict


def snapshot_now(verdict: Any = None) -> dict[str, Any] | None:
    """The snapshot from this install's ledger, config and readiness; ``None`` with no ledger."""
    from papaya_agent_runtime.paths import db_path
    from papaya_agent_runtime.state import init_db

    if not db_path().exists():
        return None
    verdict = verdict if verdict is not None else _readiness_now()
    conn = init_db()
    try:
        return build(
            conn,
            health=health_of(verdict),
            max_concurrent=max_concurrent(),
            merge_allowed=merge_allowed(),
        )
    finally:
        conn.close()


def mark_now() -> tuple[Any, ...]:
    from papaya_agent_runtime.paths import db_path
    from papaya_agent_runtime.state import init_db

    if not db_path().exists():
        return ()
    conn = init_db()
    try:
        return change_mark(conn)
    finally:
        conn.close()


def publish_once(env: dict[str, str] | None = None, *, verdict: Any = None) -> str:
    """A session's one publish (`ppy status`): connected only; never raises."""
    from papaya_agent_runtime import papaya, papaya_events

    try:
        env = env if env is not None else papaya.agent_env()
        if not env.get("PAPAYA_AGENT_TOKEN"):
            return SKIPPED
        publisher = Publisher(
            build=lambda: snapshot_now(verdict),
            put=lambda body: papaya_events.put_connection_status(body, environ=env),
        )
        return asyncio.run(publisher.publish("session"))
    except Exception as exc:  # noqa: BLE001 - a status check never fails on this
        log.warning("[status] Could not publish the status snapshot: %s", exc)
        return FAILED


__all__ = [
    "CHANGE_KINDS",
    "DEDUP_SECONDS",
    "FINISHED_MAX",
    "Health",
    "LIST_MAX",
    "MAX_BYTES",
    "Publisher",
    "build",
    "change_mark",
    "clean",
    "fit",
    "health_of",
    "how_for",
    "mark_now",
    "merge_allowed",
    "publish_once",
    "size",
    "snapshot_now",
]
