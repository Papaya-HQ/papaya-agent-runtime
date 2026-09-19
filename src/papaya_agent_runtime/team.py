"""The team as one picture, and the daemon's record as one feed.

A person copilots `ppy serve` from an interactive session. What they need is what the
daemon already writes, read back in the words of a status report rather than a table
dump, and nothing the record does not say:

- :func:`snapshot` is `ppy status --team` (and `--json`): held tickets with phase and
  age, every worker with what it is doing now, delivered pull requests with their CI,
  review and reconcile lane, blockers, the last round's summary, and everything waiting
  on a person. :func:`render` prints it one line per item.
- :func:`tail` is `ppy tail`: the event stream as one line per event, oldest first,
  from the same tables the daemon writes, with ``follow`` polling for new ones.
- :func:`workers` / :func:`render_workers` is `ppy workers`: one block per in-flight
  worker, named by the work item it serves (:func:`work_item`), with its last few
  actions (:func:`worker_actions`). :class:`Paint` is the one colour helper both
  views use; nothing is coloured unless :func:`colour_wanted` says so.
- :func:`status_line` is the one line a held ticket carries on its work item, kept
  current by the runner where Papaya lets a comment be edited in place.
- :func:`person_actions` is how the rounds tell a person's steer from their own, so a
  round never undoes what somebody at a session just decided.

Everything here only reads, except that the rounds record :data:`ROUND_SUMMARY_EVENT`
and :data:`PR_OBSERVED_EVENT` so their summary line and a pull request's CI state are
on the record at all.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from papaya_agent_runtime import health
from papaya_agent_runtime.state import db, store

#: The event kind a round writes with its summary line (`round: …`), when it said one.
ROUND_SUMMARY_EVENT = "round_summary"

#: The event kind a round writes on a delivered worker when its pull request's state,
#: CI, review decision or head differs from the last one recorded.
PR_OBSERVED_EVENT = "pr_observed"

#: The event kind `blockers.update` writes when a blocker opens or clears.
BLOCKERS_EVENT = "blockers_changed"

#: Ticket phases in which `ppy serve` is holding the ticket.
HELD_PHASES = ("picked_up", "briefing", "dispatched", "blocked", "reviewing", "delivering")

#: Worker statuses the team view lists: anything not yet delivered or finished.
WORKER_STATUSES = ("requested", "in_progress", "worker_done", "worker_stopped", "blocked")

#: How long a delivered pull request nobody has observed stays on the team view.
UNOBSERVED_PR_DAYS = 14

#: How many of a worker's newest stream events are read for what it is doing now.
ACTIVITY_WINDOW = 50


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


def _ago(now: datetime, stamp: object) -> int | None:
    at = _parse(stamp)
    return max(0, int((now - at).total_seconds())) if at is not None else None


def _clip(text: object, limit: int = 100) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _age(seconds: int | None) -> str:
    return health.humanize(seconds) if seconds is not None else "?"


# ── colour ──────────────────────────────────────────────────────────────────

#: The only styles the views use, as SGR codes. Nothing else is ever coloured.
STYLES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33"}

#: `--color` choices.
COLOR_CHOICES = ("auto", "always", "never")

#: How a health verdict is coloured.
VERDICT_STYLES = {"alive": "green", "quiet": "yellow", "dead": "red"}

#: Width a view clips to when stdout is not a terminal.
DEFAULT_WIDTH = 100


def is_tty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def colour_wanted(
    choice: str = "auto", stream: Any = None, environ: dict[str, str] | None = None
) -> bool:
    """Whether to colour: `always`/`never` as said; `auto` only for a terminal.

    `auto` honours `NO_COLOR` (any non-empty value), `CLICOLOR=0` and `TERM=dumb`.
    """
    import os
    import sys

    if choice == "always":
        return True
    if choice == "never":
        return False
    env = os.environ if environ is None else environ
    if env.get("NO_COLOR") or env.get("CLICOLOR") == "0" or env.get("TERM") == "dumb":
        return False
    return is_tty(sys.stdout if stream is None else stream)


def terminal_width(stream: Any = None) -> int:
    """The terminal's columns, or :data:`DEFAULT_WIDTH` when ``stream`` is not a terminal."""
    import shutil
    import sys

    if not is_tty(sys.stdout if stream is None else stream):
        return DEFAULT_WIDTH
    return max(40, shutil.get_terminal_size((DEFAULT_WIDTH, 24)).columns)


class Paint:
    """Wraps text in the named :data:`STYLES`, or leaves it alone when disabled."""

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    def __call__(self, text: str, *styles: str | None) -> str:
        codes = [STYLES[s] for s in styles if s]
        if not self.enabled or not codes or not text:
            return text
        return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"


#: A line as runs of text, each with its styles: clipped by visible width, then painted.
Segment = tuple[str, tuple[str, ...]]


def _seg(text: object, *styles: str | None) -> Segment:
    return (str(text), tuple(s for s in styles if s))


def clip_words(text: str, width: int) -> str:
    """``text`` cut to ``width`` columns at a word boundary, with `…` when cut."""
    if len(text) <= width:
        return text
    if width <= 1:
        return "…"[:width]
    cut = text[: width - 1]
    space = cut.rfind(" ")
    if space > 0 and not text[width - 1].isspace():
        cut = cut[:space]
    return cut.rstrip() + "…"


def fit(segments: list[Segment], width: int, paint: Paint) -> str:
    """The segments painted, clipped to ``width`` visible columns without splitting a word."""
    out, used = [], 0
    for text, styles in segments:
        if used + len(text) <= width:
            out.append(paint(text, *styles))
            used += len(text)
            continue
        if width - used > 1:
            out.append(paint(clip_words(text, width - used), *styles))
        break
    return "".join(out).rstrip()


# ── the work item a worker serves ───────────────────────────────────────────


def _env(conn: sqlite3.Connection, task_id: int | None, key: str) -> str:
    if task_id is None:
        return ""
    return str(store.get_task_env(conn, task_id, key) or "").strip()


def work_item(
    conn: sqlite3.Connection, worker_task_id: int, ticket: sqlite3.Row | None
) -> dict[str, Any] | None:
    """The work item a worker serves, as a person names it, and where that name came from.

    ``source`` says which: `ticket` (the display id recorded when the ticket was
    taken), `tracker` (a `ppy track` record on the worker or its ticket), or
    `work item id` (a ticket taken before display ids were recorded). ``None`` for a
    worker with no ticket and no tracker record.
    """
    from papaya_agent_runtime import papaya_events, tracker

    ticket_id = int(ticket["id"]) if ticket is not None else None
    title = _env(conn, ticket_id, papaya_events.WORK_ITEM_TITLE) or (
        str(ticket["title"] or "") if ticket is not None else ""
    )
    key = _env(conn, ticket_id, papaya_events.WORK_ITEM_KEY)
    if key:
        return {"key": key, "title": title, "source": "ticket", "ticket_task_id": ticket_id}
    for task_id in (worker_task_id, ticket_id):
        record = _env(conn, task_id, tracker.RECORD_KEY)
        if record:
            return {
                "key": record,
                "title": title or _env(conn, task_id, tracker.TITLE_KEY),
                "source": "tracker",
                "ticket_task_id": ticket_id,
            }
    if ticket is None:
        return None
    try:
        item_id = str(json.loads(ticket["metadata"]).get("work_item_id") or "")
    except (TypeError, ValueError, IndexError, KeyError):
        item_id = ""
    return {
        "key": f"work item {item_id[:8]}" if item_id else f"ticket task {ticket_id}",
        "title": title,
        "source": "work item id",
        "ticket_task_id": ticket_id,
    }


# ── who acted on a worker ───────────────────────────────────────────────────


def person_actions(conn: sqlite3.Connection, worker_task_id: int) -> list[dict[str, Any]]:
    """Every steer, answer or resume a person made on this worker, oldest first.

    One entry per act: a checkpoint steer and the resume that later delivers it are the
    same act, so only a `resumed` that carries no steer events counts on its own.
    """
    rows = conn.execute(
        "SELECT id, kind, payload, created_at FROM events WHERE task_id = ? "
        "AND kind IN ('steer', 'answer', 'resumed') "
        "AND json_extract(payload, '$.by') = ? ORDER BY id",
        (worker_task_id, store.BY_PERSON),
    ).fetchall()
    found = []
    for row in rows:
        payload = _payload(row)
        if row["kind"] == "resumed" and payload.get("steer_events"):
            continue
        if row["kind"] == "steer" and payload.get("after") == "interrupt":
            continue
        found.append(
            {
                "event_id": int(row["id"]),
                "kind": str(row["kind"]),
                "at": str(row["created_at"]),
                "message": str(payload.get("message") or ""),
            }
        )
    return found


# ── the snapshot ────────────────────────────────────────────────────────────


def _ticket_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The newest ticket task per work item."""
    from papaya_agent_runtime import papaya_events

    rows = conn.execute(
        "SELECT tasks.*, task_env.value AS metadata FROM tasks "
        "JOIN task_env ON task_env.task_id = tasks.id "
        "WHERE task_env.key = ? AND json_valid(task_env.value) ORDER BY tasks.id DESC",
        (papaya_events.PAPAYA_EVENT_METADATA,),
    ).fetchall()
    seen: set[str] = set()
    newest = []
    for row in rows:
        item = str(json.loads(row["metadata"]).get("work_item_id") or "")
        if not item or item in seen:
            continue
        seen.add(item)
        newest.append(row)
    return newest


def _newest(conn: sqlite3.Connection, task_id: int, kind: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, payload, created_at FROM events WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    ).fetchone()


def _worker_of(conn: sqlite3.Connection, ticket: sqlite3.Row) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM tasks WHERE run_id = ? AND id != ? ORDER BY id DESC LIMIT 1",
        (ticket["run_id"], ticket["id"]),
    ).fetchone()


def _pr_url(conn: sqlite3.Connection, worker_task_id: int) -> str | None:
    row = _newest(conn, worker_task_id, "delivered")
    return (_payload(row).get("pr_url") or None) if row is not None else None


def _tickets(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    from papaya_agent_runtime.lifecycle import TERMINAL_STATUSES

    tickets = []
    for row in reversed(_ticket_rows(conn)):
        if row["phase"] not in HELD_PHASES or row["status"] in TERMINAL_STATUSES:
            continue
        metadata = json.loads(row["metadata"])
        phase_row = _newest(conn, int(row["id"]), store.TICKET_PHASE_EVENT)
        worker = _worker_of(conn, row)
        tickets.append(
            {
                "task_id": int(row["id"]),
                "run_id": int(row["run_id"]),
                "work_item_id": str(metadata.get("work_item_id") or ""),
                "title": str(row["title"] or ""),
                "phase": str(row["phase"]),
                "phase_detail": str(_payload(phase_row).get("detail") or "") if phase_row else "",
                "phase_seconds": _ago(now, phase_row["created_at"] if phase_row else None),
                "held_seconds": _ago(now, row["created_at"]),
                "worker_task_id": int(worker["id"]) if worker is not None else None,
                "pr_url": _pr_url(conn, int(worker["id"])) if worker is not None else None,
            }
        )
    return tickets


def _activity(conn: sqlite3.Connection, worker_task_id: int) -> dict[str, Any]:
    """The tool the worker ran last, from its newest stream events."""
    from papaya_agent_runtime import serve

    row = conn.execute(
        "SELECT id FROM events WHERE task_id = ? AND kind LIKE 'worker\\_%' ESCAPE '\\' "
        "ORDER BY id DESC LIMIT 1 OFFSET ?",
        (worker_task_id, ACTIVITY_WINDOW),
    ).fetchone()
    activity = serve.worker_activity(worker_task_id, int(row["id"]) if row is not None else 0)
    if activity is None:
        return {"tool": None, "command": None, "tool_seconds": None, "said": None}
    return {
        "tool": activity.tool or None,
        "command": _clip(activity.command) if activity.command else None,
        "tool_seconds": (
            int(activity.elapsed_seconds) if activity.elapsed_seconds is not None else None
        ),
        "said": _clip(activity.said) if activity.said else None,
    }


def _gate(conn: sqlite3.Connection, worker_task_id: int, now: datetime) -> dict[str, Any] | None:
    """The worker's gate as the ledger has it: queued (and why) or running, else ``None``."""
    from papaya_agent_runtime import gate

    row = conn.execute(
        "SELECT kind, payload, created_at FROM events WHERE task_id = ? "
        "AND kind IN (?, ?, ?, ?, ?) ORDER BY id DESC LIMIT 1",
        (
            worker_task_id,
            gate.GATE_QUEUED,
            gate.GATE_UNQUEUED,
            gate.GATE_STARTED,
            gate.GATE_RESULT,
            gate.GATE_KILLED,
        ),
    ).fetchone()
    if row is None or row["kind"] in (gate.GATE_RESULT, gate.GATE_KILLED):
        return None
    payload = _payload(row)
    if row["kind"] == gate.GATE_UNQUEUED and not payload.get("started"):
        return None
    queued = row["kind"] == gate.GATE_QUEUED
    return {
        "state": "queued" if queued else "running",
        "full": bool(payload.get("full", "full" in str(payload.get("key") or "").split(":"))),
        "reason": _clip(payload.get("reason"), 120) if queued else None,
        "seconds": _ago(now, row["created_at"]),
    }


def _workers(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    marks = ",".join("?" for _ in WORKER_STATUSES)
    rows = conn.execute(
        "SELECT t.*, r.name AS repo FROM tasks t LEFT JOIN repos r ON r.id = t.repo_id "
        f"WHERE t.phase IS NULL AND t.status IN ({marks}) ORDER BY t.id",
        WORKER_STATUSES,
    ).fetchall()
    verdicts = {int(e["task_id"]): e for e in health.check(conn, now=now)}
    tickets = {int(t["run_id"]): t for t in _ticket_rows(conn)}
    workers = []
    for row in rows:
        task_id = int(row["id"])
        note = store.latest_progress(conn, task_id)
        note_payload = _payload(note) if note is not None else {}
        entry = verdicts.get(task_id)
        steers = person_actions(conn, task_id)
        ticket = tickets.get(int(row["run_id"]))
        workers.append(
            {
                "task_id": task_id,
                "ticket_task_id": int(ticket["id"]) if ticket is not None else None,
                "work_item": work_item(conn, task_id, ticket),
                "repo": row["repo"],
                "branch": row["branch"],
                "status": str(row["status"]),
                "running_seconds": _ago(now, row["created_at"]),
                "running_since": str(row["created_at"]),
                "session": entry["verdict"] if entry else None,
                "silent_seconds": entry["silent_seconds"] if entry else None,
                **_activity(conn, task_id),
                "gate": _gate(conn, task_id, now),
                "note_phase": note_payload.get("phase"),
                "note": _clip(note_payload.get("note"), 140) if note is not None else None,
                "note_seconds": _ago(now, note["created_at"]) if note is not None else None,
                "note_at": str(note["created_at"]) if note is not None else None,
                "last_person_steer": steers[-1] if steers else None,
            }
        )
    return workers


def _pull_requests(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    from papaya_agent_runtime import reconcile

    rows = conn.execute(
        "SELECT t.*, r.name AS repo FROM tasks t LEFT JOIN repos r ON r.id = t.repo_id "
        "WHERE t.status = 'delivered' ORDER BY t.id"
    ).fetchall()
    open_lane = {entry.worker_task_id for entry in reconcile.open_lane(conn)}
    queued = {int(p.get("task_id") or 0) for _id, p in reconcile.pending_queue(conn)}
    tickets = {int(t["run_id"]): t for t in _ticket_rows(conn)}
    found = []
    for row in rows:
        task_id = int(row["id"])
        delivered = _newest(conn, task_id, "delivered")
        seen = _newest(conn, task_id, PR_OBSERVED_EVENT)
        state = _payload(seen) if seen is not None else {}
        ticket = tickets.get(int(row["run_id"]))
        if ticket is not None and ticket["phase"] in ("done", "handed_over"):
            continue
        if state.get("state") in ("MERGED", "CLOSED") or state.get("merged") is True:
            continue
        delivered_ago = _ago(now, delivered["created_at"]) if delivered is not None else None
        if seen is None and (delivered_ago or 0) > UNOBSERVED_PR_DAYS * 86400:
            continue
        if task_id in open_lane:
            lane = "reconciling"
        elif task_id in queued:
            lane = "queued"
        elif ticket is not None and ticket["phase"] == reconcile.NEEDS_A_PERSON:
            lane = "needs a person"
        else:
            lane = "idle"
        found.append(
            {
                "task_id": task_id,
                "ticket_task_id": int(ticket["id"]) if ticket is not None else None,
                "repo": row["repo"],
                "url": (_payload(delivered).get("pr_url") if delivered is not None else None)
                or state.get("url"),
                "state": state.get("state"),
                "ci": state.get("ci"),
                "review": state.get("review") or None,
                "observed_seconds": _ago(now, seen["created_at"]) if seen is not None else None,
                "lane": lane,
            }
        )
    return found


def _last_round(conn: sqlite3.Connection, now: datetime) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload, created_at FROM events WHERE kind = ? ORDER BY id DESC LIMIT 1",
        (ROUND_SUMMARY_EVENT,),
    ).fetchone()
    if row is None:
        return None
    return {"line": str(_payload(row).get("line") or ""), "seconds": _ago(now, row["created_at"])}


def _waiting(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, task_id, text, created_at FROM todos WHERE status = 'open' "
        "AND (blocked_on = 'user' OR blocked_on LIKE 'user:%') ORDER BY id"
    ).fetchall()
    waiting = [
        {
            "todo_id": int(row["id"]),
            "task_id": row["task_id"],
            "text": _clip(row["text"], 160),
            "seconds": _ago(now, row["created_at"]),
        }
        for row in rows
    ]
    from papaya_agent_runtime import owed

    for item in owed.overdue(owed.collect(conn, now=now)):
        waiting.append(
            {
                "todo_id": None,
                "task_id": item.task_id,
                "text": _clip(item.line(), 160),
                "seconds": item.seconds,
            }
        )
    for row in _ticket_rows(conn):
        if row["phase"] != "needs_a_person":
            continue
        phase_row = _newest(conn, int(row["id"]), store.TICKET_PHASE_EVENT)
        waiting.append(
            {
                "todo_id": None,
                "task_id": int(row["id"]),
                "text": _clip(
                    (_payload(phase_row).get("detail") if phase_row else "")
                    or "its pull request needs a person",
                    160,
                ),
                "seconds": _ago(now, phase_row["created_at"] if phase_row else None),
            }
        )
    return waiting


# ── what the runtime keeps failing to do ────────────────────────────────────
#
# The runtime reported what it did and not what it kept failing to do: an hour of
# identical pickup comments on PAP-210 and two days of one refusal (a deficiency row
# at count 527) passed unnoticed because nothing surfaced either. `needs attention`
# is one section, in `ppy workers` and `ppy status --team`, read from the ledger the
# runtime already writes: tickets repeating without progress, tickets parked on a
# person, and any deficiency whose count grew since the person last looked.

#: How long a `repeated-without-progress` row stays named after it was last recorded.
REPEATING_SHOWN_FOR = 24 * 60 * 60.0

#: What a person can do about a repeating ticket, by how its holds ended.
_REPEAT_ACTIONS = {
    "reported": "each brief found nothing to build: answer or change the ticket, or close it",
    "declined": "handed back each time: read the reason on the ticket",
    "handed_back": "handed back each time: read the reason on the ticket",
    "stalled": "stalled each time: check its worker with `ppy workers`",
    "released": "released each time with no ending: read its holds with `ppy tail`",
}
_REFUSED_ACTION = "Papaya keeps refusing it here: use Run on this Mac, or reassign it"


def attention_seen_path():
    """Where each deficiency's count at the last look is kept: `.ppy/attention-seen.json`."""
    from papaya_agent_runtime.paths import ppy_home

    return ppy_home() / "attention-seen.json"


def _seen_counts() -> dict[str, int]:
    try:
        data = json.loads(attention_seen_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): int(v) for k, v in data.items() if isinstance(v, int)}


def attention(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    """`needs attention`, as data. Reads only; :func:`mark_looked` records the look.

    ``repeating``: `repeated-without-progress` rows seen in the last day. ``parked``:
    tickets the sweep skips as waiting on a person, with the reason and the stamp.
    ``grown``: every other deficiency whose count is above the count at the last look
    (a first look counts from one, so a row seen once is not a repetition), worst first.
    """
    from papaya_agent_runtime import deficiencies, sweep

    now = now or datetime.now(UTC)
    seen = _seen_counts()
    rows = conn.execute(
        "SELECT fingerprint, kind, title, detail, count, last_seen, evidence, status "
        "FROM deficiencies WHERE status != ? ORDER BY last_seen DESC",
        (deficiencies.RECLASSIFIED,),
    ).fetchall()
    repeating, grown, counts = [], [], {}
    for row in rows:
        counts[str(row["fingerprint"])] = int(row["count"])
        seconds = _ago(now, row["last_seen"])
        if row["kind"] == deficiencies.REPEATED_WITHOUT_PROGRESS:
            if seconds is not None and seconds <= REPEATING_SHOWN_FOR:
                try:
                    entries = [
                        e for e in json.loads(row["evidence"] or "[]") if isinstance(e, dict)
                    ]
                except ValueError:
                    entries = []
                evidence = entries[-1] if entries else {}
                recent = [
                    str(e["ticket"])
                    for e in entries
                    if e.get("ticket") and (_ago(now, e.get("at")) or 0) <= REPEATING_SHOWN_FOR
                ]
                repeating.append(
                    {
                        "ticket": evidence.get("ticket"),
                        # A refusal row gathers every ticket refused that way in the day.
                        "tickets": list(dict.fromkeys(recent)),
                        "ending": evidence.get("code"),
                        "detail": str(row["detail"]),
                        "count": int(row["count"]),
                        "last_seen": str(row["last_seen"]),
                        "seconds": seconds,
                    }
                )
            continue
        more = int(row["count"]) - seen.get(str(row["fingerprint"]), 1)
        if more > 0:
            grown.append(
                {
                    "fingerprint": str(row["fingerprint"]),
                    "kind": str(row["kind"]),
                    "title": str(row["title"]),
                    "count": int(row["count"]),
                    "grown": more,
                    "last_seen": str(row["last_seen"]),
                    "seconds": seconds,
                }
            )
    grown.sort(key=lambda d: (-d["grown"], -d["count"]))
    parked = [
        {
            "work_item_id": item_id,
            "ticket": str(memo.get("label") or sweep.ticket_label({"id": item_id})),
            "reason": str(memo.get("reason") or ""),
            "since": memo.get("declined_at"),
            "stamp": memo.get("updated_at"),
            "seconds": _ago(now, memo.get("declined_at")),
        }
        for item_id, memo in sweep.parked_items().items()
    ]
    parked.sort(key=lambda p: -(p["seconds"] or 0))
    return {
        "paused": _paused(conn, now),
        "gave_up": _gave_up(conn, now),
        "repeating": repeating,
        "parked": parked,
        "grown": grown,
        "counts": counts,
    }


def _paused(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """The provider usage limit manager turns are waiting out now, with its reset."""
    from papaya_agent_runtime import limits

    try:
        found = limits.pause(conn, None, now)
    except sqlite3.Error:
        return []
    if found is None:
        return []
    return [
        {
            "provider": found.provider,
            "until": found.until.isoformat(timespec="seconds"),
            "exact": found.exact,
            "text": found.text,
            "seconds": max(int((found.until - now).total_seconds()), 0),
        }
    ]


def _gave_up(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """Worker tasks the manager's turns gave up on, still standing (nothing new since)."""
    from papaya_agent_runtime import lanes

    try:
        found = lanes.gave_up(conn)
    except sqlite3.Error:
        return []
    return [{**g, "seconds": _ago(now, g["since"])} for g in found]


def mark_looked(found: dict[str, Any]) -> None:
    """Record every deficiency's count as seen, so the next look names only new growth."""
    path = attention_seen_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(found.get("counts") or {}, sort_keys=True), encoding="utf-8")
        temp.replace(path)
    except OSError:
        pass


def peek(now: datetime | None = None) -> dict[str, Any]:
    """`needs attention` now, read only: nothing is recorded as looked at."""
    from papaya_agent_runtime.paths import db_path

    if not db_path().exists():
        return {
            "paused": [],
            "gave_up": [],
            "repeating": [],
            "parked": [],
            "grown": [],
            "counts": {},
        }
    conn = db.init_db()
    try:
        return attention(conn, now)
    finally:
        conn.close()


def look(now: datetime | None = None) -> dict[str, Any]:
    """`needs attention` for a person looking now: read it, then record the look."""
    found = peek(now)
    mark_looked(found)
    return found


def a_persons_look(stream: Any, *, as_json: bool = False, reprint: bool = False) -> bool:
    """Whether printing to ``stream`` is a person looking, so the look may be recorded.

    Only a first print, not as JSON, to a terminal. A `--follow` reprint is the same
    look; JSON is for a script; and a stdout that is not a terminal is an agent turn
    or a pipe (turns are told to run `ppy status --team`), which must not use up the
    growth a person has not seen.
    """
    return not as_json and not reprint and is_tty(stream)


def attention_lines(found: dict[str, Any] | None) -> list[str]:
    """One line per thing that needs a person: a usage-limit pause, give-ups, repeating,
    parked, then grown."""
    if not found:
        return []
    lines = []
    for p in found.get("paused") or []:
        how = "" if p.get("exact") else " (no reset time could be read; trying again then)"
        lines.append(
            f"paused: {p.get('provider') or 'the provider'} usage limit until "
            f"{_utc(p['until'])} (in {_age(p['seconds'])}){how}: {_clip(p.get('text'), 90)}"
            " · manager turns wait for the reset and limit-stopped workers resume after it;"
            " nothing is counted as a failure"
        )
    for g in found.get("gave_up") or []:
        lines.append(
            f"gave up: worker task {g['task_id']} since {_utc(g['since'])} "
            f"({_age(g['seconds'])} ago): {_clip(g['reason'], 90)} · anything new on the task "
            f"takes it up again; or decide on it and close todo #{g['todo_id']}"
        )
    for r in found.get("repeating") or []:
        ending = str(r.get("ending") or "")
        action = _REPEAT_ACTIONS.get(ending)
        which = ""
        if action is None:
            # A refusal row is keyed on the reason; the tickets are in its evidence.
            action = _REFUSED_ACTION
            which = f": {', '.join(r.get('tickets') or [])}" if r.get("tickets") else ""
        lines.append(
            f"repeating: {r['detail']}{which} (last {_utc(r['last_seen'])}, "
            f"{_age(r['seconds'])} ago) · {action}"
        )
    for p in found.get("parked") or []:
        lines.append(
            f"parked: {p['ticket']} waiting on a person since {_utc(p['since'])} "
            f"({_age(p['seconds'])} ago), stamp {p['stamp'] or '?'}: {_clip(p['reason'], 90)}"
            " · un-parks when the item changes or a person comments after the stamp"
        )
    for d in found.get("grown") or []:
        lines.append(
            f"deficiency {d['kind']}: {_clip(d['title'], 80)} · seen {d['count']}x "
            f"(+{d['grown']} since the last look), last {_utc(d['last_seen'])}"
        )
    return lines


def snapshot(conn: sqlite3.Connection | None = None, *, now: datetime | None = None) -> dict:
    """Everything `ppy status --team` says, as data. Reads only."""
    from papaya_agent_runtime import blockers, reconcile

    own = conn is None
    conn = conn or db.init_db()
    now = now or datetime.now(UTC)
    try:
        return {
            "at": now.isoformat(timespec="seconds"),
            "tickets": _tickets(conn, now),
            "workers": _workers(conn, now),
            "pull_requests": _pull_requests(conn, now),
            "lane": reconcile.lane_status(conn, now),
            "blockers": blockers.current(),
            "last_round": _last_round(conn, now),
            "waiting_on_a_person": _waiting(conn, now),
            "attention": attention(conn, now),
        }
    finally:
        if own:
            conn.close()


def _ticket_line(t: dict[str, Any]) -> str:
    parts = [f"ticket task {t['task_id']}", f'"{_clip(t["title"], 60)}"']
    parts.append(f"{t['phase']} {_age(t['phase_seconds'])}, held {_age(t['held_seconds'])}")
    if t["worker_task_id"] is not None:
        parts.append(f"worker task {t['worker_task_id']}")
    if t["pr_url"]:
        parts.append(t["pr_url"])
    return " · ".join(parts)


def _doing(w: dict[str, Any]) -> str | None:
    if w["command"] or w["tool"]:
        doing = f"running `{w['command']}`" if w["command"] else f"running {w['tool']}"
        if w["tool_seconds"] is not None:
            doing += f" ({_age(w['tool_seconds'])} in)"
        return doing
    if w["said"]:
        return f'said "{w["said"]}"'
    return None


def _gate_words(g: dict[str, Any] | None) -> str | None:
    if not g:
        return None
    scope = "full suite" if g["full"] else "local gate"
    if g["state"] == "queued":
        return f"{scope} queued {_age(g['seconds'])}: {g['reason'] or 'waiting for a slot'}"
    return f"{scope} running under the supervisor ({_age(g['seconds'])} in)"


def _verdict_words(w: dict[str, Any]) -> str:
    silent = f", silent {_age(w['silent_seconds'])}" if w["session"] == "quiet" else ""
    return f"{w['session']}{silent}"


def _worker_line(w: dict[str, Any], paint: Paint | None = None) -> str:
    paint = paint or Paint()
    parts = [f"worker task {w['task_id']}"]
    if w.get("work_item"):
        parts.append(paint(w["work_item"]["key"], "bold"))
    if w["repo"]:
        parts.append(str(w["repo"]))
    parts.append(f"{w['status']} {_age(w['running_seconds'])}")
    if w["session"]:
        parts.append("session " + paint(_verdict_words(w), VERDICT_STYLES.get(str(w["session"]))))
    doing = _doing(w)
    if doing:
        parts.append(doing)
    gate_said = _gate_words(w.get("gate"))
    if gate_said:
        parts.append(gate_said)
    if w["note"] is not None:
        parts.append(f"note [{w['note_phase']}] {w['note']} ({_age(w['note_seconds'])} ago)")
    if w["last_person_steer"]:
        parts.append(f'steered by a person: "{_clip(w["last_person_steer"]["message"], 60)}"')
    return " · ".join(parts)


def _pr_line(p: dict[str, Any]) -> str:
    parts = [f"worker task {p['task_id']}", p["url"] or "no pull request url recorded"]
    if p["observed_seconds"] is None:
        parts.append("not observed by a round yet")
    else:
        parts.append(f"{p['state'] or '?'}, CI {p['ci'] or '?'}, review {p['review'] or 'none'}")
        parts.append(f"seen {_age(p['observed_seconds'])} ago")
    parts.append(f"lane {p['lane']}")
    return " · ".join(parts)


def render(snap: dict[str, Any], paint: Paint | None = None) -> list[str]:
    """`ppy status --team`: one line per item, under one heading per section."""
    lines = [f"team at {snap['at']}"]

    def section(name: str, items: list[str]) -> None:
        lines.append(f"{name} ({len(items)}):" if items else f"{name}: none")
        lines.extend(f"  {item}" for item in items)

    section("held tickets", [_ticket_line(t) for t in snap["tickets"]])
    section("workers", [_worker_line(w, paint) for w in snap["workers"]])
    section("pull requests", [_pr_line(p) for p in snap["pull_requests"]])
    lines.append(f"reconcile lane: {snap['lane']}")
    section(
        "blockers",
        [f"{b.get('code')}: {b.get('title')} (since {b.get('since')})" for b in snap["blockers"]],
    )
    last = snap["last_round"]
    lines.append(
        f"last round ({_age(last['seconds'])} ago): {last['line']}"
        if last
        else "last round: none recorded"
    )
    section(
        "waiting on a person",
        [
            " · ".join(
                part
                for part in (
                    f"todo {w['todo_id']}" if w["todo_id"] is not None else "",
                    f"task {w['task_id']}" if w["task_id"] is not None else "",
                    f"{_age(w['seconds'])}",
                    w["text"],
                )
                if part
            )
            for w in snap["waiting_on_a_person"]
        ],
    )
    if "attention" in snap:
        # Recording the look is the command's, and only for a person (`a_persons_look`).
        section("needs attention", attention_lines(snap["attention"]))
    return lines


# ── the living status line on a held ticket ─────────────────────────────────


#: A worker's status, as the status line says it to somebody reading the ticket.
_WORKER_WORDS = {
    "requested": "starting",
    "in_progress": "working",
    "worker_done": "done, awaiting review",
    "worker_stopped": "stopped",
    "blocked": "asking a question",
}


def status_line(snap: dict[str, Any], ticket_task_id: int) -> str | None:
    """The one line a held ticket carries on its work item, from the same facts.

    Phase and how long, what its worker is doing, its pull request with CI, and what
    waits on a person. ``None`` when the ticket is not held.
    """
    ticket = next((t for t in snap["tickets"] if t["task_id"] == ticket_task_id), None)
    if ticket is None:
        return None
    parts = [
        f"{ticket['phase'].replace('_', ' ').capitalize()} for {_age(ticket['phase_seconds'])}"
    ]
    worker = next((w for w in snap["workers"] if w["task_id"] == ticket["worker_task_id"]), None)
    if worker is not None:
        doing = _doing(worker)
        state = _WORKER_WORDS.get(worker["status"], worker["status"].replace("_", " "))
        said = f"worker {state}"
        if doing and worker["status"] == "in_progress":
            said += f", {doing}"
        gate_said = _gate_words(worker.get("gate"))
        if gate_said:
            said += f"; {gate_said}"
        if worker["note"] is not None:
            said += f"; last note: {_clip(worker['note'], 80)}"
        parts.append(said)
    pr = next((p for p in snap["pull_requests"] if p["task_id"] == ticket["worker_task_id"]), None)
    if pr is not None:
        ci = f", CI {pr['ci']}" if pr["ci"] else ""
        review = f", review {pr['review'].lower().replace('_', ' ')}" if pr["review"] else ""
        parts.append(f"PR {pr['url'] or '(no url)'}{ci}{review}")
    elif ticket["pr_url"]:
        parts.append(f"PR {ticket['pr_url']}")
    ids = {ticket_task_id, ticket["worker_task_id"]}
    for wait in snap["waiting_on_a_person"]:
        if wait["task_id"] in ids:
            parts.append(f"waiting on you: {wait['text']}")
    return " · ".join(parts)


# ── the feed ────────────────────────────────────────────────────────────────

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> timedelta:
    """`10m`, `2h`, `30s`, `1d`, or a bare number of seconds."""
    match = _DURATION.match(str(text or ""))
    if match is None:
        raise ValueError(f"a duration is a number with s, m, h or d (like 10m), not {text!r}")
    return timedelta(seconds=float(match.group(1)) * _UNITS[match.group(2)])


def _on(row: Any, payload: dict[str, Any]) -> str:
    task = row["task_id"] if row["task_id"] is not None else payload.get("task_id")
    return f"task {task}" if task is not None else "runtime"


def event_line(row: Any) -> str | None:
    """One ledger event as one line of the feed, or ``None`` for stream chatter."""
    kind, payload = str(row["kind"]), _payload(row)
    on = _on(row, payload)
    by = f" by {payload['by']}" if payload.get("by") else ""
    if kind == store.TICKET_PHASE_EVENT:
        detail = f": {_clip(payload.get('detail'))}" if payload.get("detail") else ""
        return f"{on} phase {payload.get('phase')}{detail}"
    if kind == "worker_progress":
        if not payload.get("phase"):
            return None
        return f"{on} note [{payload.get('phase')}] {_clip(payload.get('note'), 160)}"
    if kind == "ticket_checkin":
        message = f": {_clip(payload.get('message'))}" if payload.get("message") else ""
        error = f" (not delivered: {_clip(payload.get('error'))})" if payload.get("error") else ""
        return (
            f"{on} check-in on worker task {payload.get('worker_task_id')} "
            f"({payload.get('trigger') or payload.get('reason')}): "
            f"{payload.get('decision')}{message}{error}"
        )
    if kind == "ticket_round":
        extra = payload.get("reason") or payload.get("worker_task_id") or ""
        return f"{on} round {payload.get('action')}" + (f": {_clip(extra)}" if extra else "")
    if kind == "worktree_hygiene":
        removed, kept = payload.get("removed") or [], payload.get("kept") or []
        return f"hygiene: removed {len(removed)} worktree(s), kept {len(kept)}"
    if kind == "pr_attention":
        return f"{on} PR attention: {_clip(payload.get('summary'), 160)}"
    if kind == PR_OBSERVED_EVENT:
        return (
            f"{on} PR {payload.get('url') or payload.get('pr')}: {payload.get('state')}, "
            f"CI {payload.get('ci')}, review {payload.get('review') or 'none'}"
        )
    if kind in ("pr_attention_queued", "reconcile_started"):
        what = "queued for the reconcile lane" if kind.endswith("queued") else "reconcile started"
        return f"{on} {what}"
    if kind == "reconcile_finished":
        return f"{on} reconcile finished: {payload.get('outcome')}"
    if kind == "needs_a_person":
        return f"{on} pull request needs a person"
    if kind == ROUND_SUMMARY_EVENT:
        return str(payload.get("line") or "round:")
    if kind == BLOCKERS_EVENT:
        opened = ", ".join(payload.get("opened") or []) or "none"
        cleared = ", ".join(payload.get("cleared") or []) or "none"
        return f"blockers: opened {opened}; cleared {cleared}"
    if kind == "steer":
        if payload.get("after") == "interrupt":
            return None
        return f"{on} steered{by}: {_clip(payload.get('message'))}"
    if kind == "answer":
        return f"{on} answered{by}: {_clip(payload.get('answer') or payload.get('message'))}"
    if kind == "resumed":
        if payload.get("steer_events"):
            return f"{on} resumed with its queued steer"
        message = f": {_clip(payload.get('message'))}" if payload.get("message") else ""
        return f"{on} resumed{by}{message}"
    if kind in ("question", "blocked"):
        return f"{on} asks: {_clip(payload.get('question'), 160)}"
    if kind == "dispatched":
        return f"{on} dispatched"
    if kind in ("worker_done", "worker_stopped", "delivered", "task_closed"):
        detail = payload.get("pr_url") or payload.get("summary") or payload.get("reason") or ""
        return f"{on} {kind.replace('_', ' ')}" + (f": {_clip(detail)}" if detail else "")
    if kind == "error":
        return f"{on} error: {_clip(payload.get('summary'))}"
    return None


def _stamp(value: object) -> str:
    at = _parse(value)
    return at.strftime("%H:%M:%S") if at is not None else "--:--:--"


def feed(
    conn: sqlite3.Connection, *, since: datetime, after_id: int = 0, deficiencies_seen=None
) -> tuple[list[tuple[str, str]], int]:
    """New feed lines as `(timestamp, line)` in order, and the newest event id read.

    ``deficiencies_seen`` maps a deficiency fingerprint to the count already said, and is
    updated, so a deficiency seen again is a new line and one unchanged is not.
    """
    rows = conn.execute(
        "SELECT id, task_id, kind, payload, created_at FROM events "
        "WHERE id > ? AND created_at >= ? ORDER BY id",
        (after_id, since.isoformat()),
    ).fetchall()
    lines = []
    newest = after_id
    for row in rows:
        newest = int(row["id"])
        line = event_line(row)
        if line is not None:
            lines.append((str(row["created_at"]), line))
    seen = deficiencies_seen if deficiencies_seen is not None else {}
    for row in conn.execute(
        "SELECT fingerprint, kind, title, count, last_seen FROM deficiencies "
        "WHERE last_seen >= ? ORDER BY last_seen",
        (since.isoformat(),),
    ).fetchall():
        if seen.get(row["fingerprint"]) == int(row["count"]):
            continue
        seen[row["fingerprint"]] = int(row["count"])
        # A deficiency is stamped to the second; it goes after the events of that second.
        at = _parse(row["last_seen"])
        after = (at + timedelta(microseconds=999_999)).isoformat() if at else row["last_seen"]
        lines.append(
            (
                str(after),
                f"deficiency {row['kind']}: {_clip(row['title'])} (seen {row['count']}x)",
            )
        )
    lines.sort(key=lambda item: _parse(item[0]) or datetime.min.replace(tzinfo=UTC))
    return lines, newest


def tail(
    write: Callable[[str], None],
    *,
    since: timedelta,
    follow: bool = False,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    interval: float = 2.0,
    polls: int | None = None,
) -> int:
    """`ppy tail`: every feed line since ``since`` ago, then new ones while ``follow``.

    ``polls`` bounds how many times a follow looks again (``None`` is until interrupted),
    which is the test seam alongside ``sleep``. Returns how many lines were written.
    """
    now = now or (lambda: datetime.now(UTC))
    start = now() - since
    after, seen, written, looked = 0, {}, 0, 0
    while True:
        conn = db.init_db()
        try:
            lines, after = feed(conn, since=start, after_id=after, deficiencies_seen=seen)
        finally:
            conn.close()
        for at, line in lines:
            write(f"{_stamp(at)} {line}")
            written += 1
        if not follow or (polls is not None and looked >= polls):
            return written
        looked += 1
        sleep(interval)


# ── one worker at a time: `ppy workers` ─────────────────────────────────────

#: `--actions` default and ceiling.
DEFAULT_ACTIONS = 5
MAX_ACTIONS = 20

#: What a Claude tool call's name reads as when it names a target, not a command.
_TOOL_VERBS = {
    "read": "read",
    "edit": "edited",
    "multiedit": "edited",
    "write": "wrote",
    "grep": "searched for",
    "glob": "listed",
    "webfetch": "fetched",
    "websearch": "searched the web for",
    "todowrite": "updated its todo list",
}


def _tool_call_words(block: dict[str, Any]) -> str:
    from papaya_agent_runtime import serve

    name = str(block.get("name") or "a tool")
    arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
    if str(arguments.get("command") or "").strip():
        return f"ran `{_clip(arguments['command'], 160)}`"
    target = serve._tool_command(block)
    verb = _TOOL_VERBS.get(name.lower())
    if verb and not target:
        return verb
    if verb:
        return f"{verb} {_clip(target, 160)}"
    return f"used {name}" + (f" on {_clip(target, 160)}" if target else "")


def _gate_scope(payload: dict[str, Any]) -> str:
    full = payload.get("full", "full" in str(payload.get("key") or "").split(":"))
    return "full suite" if full else "local gate"


def _action_words(
    row: Any, payload: dict[str, Any], shown_tools: set[str]
) -> list[tuple[str, str | None]]:
    """What one event says the worker did, as `(words, tone)`; `[]` for chatter.

    ``tone`` is `pass`/`fail` for a gate result and ``None`` otherwise. ``shown_tools``
    collects the tool calls already said, so a running tool's 30-second heartbeat is
    one action, not one per beat.
    """
    from papaya_agent_runtime import gate, serve

    kind = str(row["kind"])
    if kind == "worker_assistant":
        said: list[tuple[str, str | None]] = []
        uses = serve._tool_uses(payload)
        text = serve._assistant_text(payload)
        if text:
            said.append((f'said "{_clip(text, 140)}"', None))
        for block in uses:
            shown_tools.add(str(block.get("id") or ""))
            said.append((_tool_call_words(block), None))
        return said
    if kind == "worker_tool_progress":
        call = str(payload.get("parent_tool_use_id") or payload.get("tool_use_id") or "")
        call = call.split("-heartbeat-")[0]
        if not call or call in shown_tools:
            return []
        shown_tools.add(call)
        return [(f"running {payload.get('tool_name') or 'a tool'}", None)]
    if kind in ("worker_item.started", "worker_item.completed"):
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        if item.get("command"):
            command = _clip(item["command"], 160)
            if kind.endswith("started"):
                return [(f"ran `{command}`", None)]
            code = item.get("exit_code")
            ended = f" (exit {code})" if code is not None else ""
            return [(f"finished `{command}`{ended}", None)]
        if kind.endswith("completed") and item.get("type") == "agent_message" and item.get("text"):
            return [(f'said "{_clip(item["text"], 140)}"', None)]
        return []
    if kind == gate.GATE_QUEUED:
        reason = f": {_clip(payload.get('reason'), 120)}" if payload.get("reason") else ""
        return [(f"{_gate_scope(payload)} queued{reason}", None)]
    if kind == gate.GATE_STARTED:
        command = f" `{_clip(payload.get('command'), 120)}`" if payload.get("command") else ""
        return [(f"{_gate_scope(payload)} started{command}", None)]
    if kind == gate.GATE_RESULT:
        took = payload.get("duration_seconds")
        after = f" in {_age(int(float(took)))}" if isinstance(took, int | float) else ""
        if payload.get("exit_code") == 0:
            return [(f"{_gate_scope(payload)} passed{after}", "pass")]
        summary = f": {_clip(payload.get('summary'), 100)}" if payload.get("summary") else ""
        return [
            (
                f"{_gate_scope(payload)} failed (exit {payload.get('exit_code')}){after}{summary}",
                "fail",
            )
        ]
    if kind == gate.GATE_KILLED:
        return [(f"{_gate_scope(payload)} killed", "fail")]
    if kind == "progress_guidance":
        by = payload.get("by") or "the manager"
        return [(f"note from {by}: {_clip(payload.get('note'), 140)}", None)]
    line = event_line(row)
    if line is None:
        return []
    for prefix in (f"task {row['task_id']} ", "runtime "):
        if line.startswith(prefix):
            line = line[len(prefix) :]
            break
    return [(line, None)]


def worker_actions(
    conn: sqlite3.Connection,
    worker_task_id: int,
    ticket_task_id: int | None,
    *,
    limit: int = DEFAULT_ACTIONS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """The worker's newest ``limit`` actions, newest last, from its events and its check-ins.

    Each is `{event_id, at (ISO), seconds (ago), what, tone}`. Reads only.
    """
    now = now or datetime.now(UTC)
    rows = conn.execute(
        "SELECT id, task_id, kind, payload, created_at FROM events WHERE task_id = ? "
        "OR (task_id = ? AND kind = 'ticket_checkin' "
        "AND json_extract(payload, '$.worker_task_id') = ?) ORDER BY id",
        (worker_task_id, ticket_task_id if ticket_task_id is not None else -1, worker_task_id),
    ).fetchall()
    shown: set[str] = set()
    found: list[dict[str, Any]] = []
    for row in rows:
        for what, tone in _action_words(row, _payload(row), shown):
            found.append(
                {
                    "event_id": int(row["id"]),
                    "at": str(row["created_at"]),
                    "seconds": _ago(now, row["created_at"]),
                    "what": what,
                    "tone": tone,
                }
            )
    return found[-limit:] if limit > 0 else []


def workers(
    conn: sqlite3.Connection | None = None,
    *,
    now: datetime | None = None,
    actions: int = DEFAULT_ACTIONS,
) -> list[dict[str, Any]]:
    """Every in-flight worker as `ppy workers` says it, with its last ``actions`` actions."""
    own = conn is None
    conn = conn or db.init_db()
    now = now or datetime.now(UTC)
    actions = max(0, min(MAX_ACTIONS, actions))
    try:
        found = []
        for worker in _workers(conn, now):
            worker["actions"] = worker_actions(
                conn, worker["task_id"], worker["ticket_task_id"], limit=actions, now=now
            )
            found.append(worker)
        return found
    finally:
        if own:
            conn.close()


def utc_clock(at: datetime | None = None) -> str:
    """`HH:MM:SS UTC` for ``at`` (default now)."""
    return (at or datetime.now(UTC)).astimezone(UTC).strftime("%H:%M:%S UTC")


def _utc(stamp: object) -> str:
    at = _parse(stamp)
    return at.astimezone(UTC).strftime("%H:%M:%S UTC") if at is not None else "--:--:-- UTC"


#: The width an action's relative age is padded to, so the words line up as a column.
_AGO_WIDTH = len("(99h59m ago)")

_TONE_STYLES = {"pass": "green", "fail": "red"}


def _header(w: dict[str, Any], width: int, paint: Paint) -> str:
    tail: list[Segment] = [_seg(f" · task {w['task_id']}")]
    if w["repo"]:
        tail.append(_seg(f" · {w['repo']}"))
    tail.append(_seg(f" · {w['status']} {_age(w['running_seconds'])}"))
    if w["session"]:
        tail.append(_seg(" · "))
        tail.append(_seg(_verdict_words(w), VERDICT_STYLES.get(str(w["session"]))))
    item = w.get("work_item")
    if item is None:
        return fit([_seg("no ticket"), *tail], width, paint)
    head = [_seg(item["key"], "bold")]
    # The title gives way first, so the facts after it always fit.
    room = width - len(item["key"]) - sum(len(t) for t, _ in tail) - 3
    if item.get("title") and room >= 8:
        head.append(_seg(" "))
        head.append(_seg(f'"{clip_words(item["title"], room)}"', "bold"))
    return fit([*head, *tail], width, paint)


def render_workers(
    found: list[dict[str, Any]],
    *,
    width: int = DEFAULT_WIDTH,
    paint: Paint | None = None,
    needs: dict[str, Any] | None = None,
) -> list[str]:
    """`ppy workers`: one block per worker, a blank line between blocks, then `needs attention`.

    ``needs`` is :func:`attention`'s answer; left out, it is read now (:func:`peek`).
    Rendering records nothing: the command records a person's look (`a_persons_look`).
    """
    paint = paint or Paint()
    lines = _worker_blocks(found, width, paint)
    extra = attention_lines(peek() if needs is None else needs)
    if extra:
        lines.append("")
        lines.append(paint(f"needs attention ({len(extra)}):", "bold"))
        # Not clipped to the width: the end of each line is what to do about it.
        lines.extend(f"  {line}" for line in extra)
    return lines


def _worker_blocks(found: list[dict[str, Any]], width: int, paint: Paint) -> list[str]:
    if not found:
        return ["no workers in flight"]
    lines: list[str] = []
    for w in found:
        if lines:
            lines.append("")
        lines.append(_header(w, width, paint))
        lines.append(fit([_seg(f"  doing: {_doing(w) or 'nothing recorded yet'}")], width, paint))
        gate_said = _gate_words(w.get("gate"))
        if gate_said:
            lines.append(fit([_seg(f"  gate: {gate_said}")], width, paint))
        if w["note"] is not None:
            lines.append(
                fit(
                    [
                        _seg(f"  note: [{w['note_phase']}] {w['note']} "),
                        _seg(f"({_age(w['note_seconds'])} ago)", "dim"),
                    ],
                    width,
                    paint,
                )
            )
        if w["last_person_steer"]:
            steer = w["last_person_steer"]["message"]
            lines.append(fit([_seg(f'  steered by a person: "{steer}"')], width, paint))
        acts = w.get("actions") or []
        lines.append(f"  last {len(acts)} actions:" if acts else "  no actions recorded yet")
        for act in acts:
            lines.append(
                fit(
                    [
                        _seg("    "),
                        _seg(_utc(act["at"]), "dim"),
                        _seg("  "),
                        _seg(f"({_age(act['seconds'])} ago)".ljust(_AGO_WIDTH), "dim"),
                        _seg("  "),
                        _seg(act["what"], _TONE_STYLES.get(str(act.get("tone")))),
                    ],
                    width,
                    paint,
                )
            )
    return lines


def workers_json(
    found: list[dict[str, Any]], needs: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The same facts, machine-readable: ages in seconds beside absolute ISO timestamps.

    `{"workers": [...], "attention": {paused, gave_up, repeating, parked, grown}}`: the
    workers, then the
    `needs attention` section as data (read only; JSON is never a person's look).
    """

    def iso(stamp: object) -> str | None:
        at = _parse(stamp)
        return at.isoformat() if at is not None else None

    needs = peek() if needs is None else needs
    return {
        "workers": [
            {
                **w,
                "running_since": iso(w["running_since"]),
                "note_at": iso(w["note_at"]),
                "actions": [{**a, "at": iso(a["at"])} for a in w["actions"]],
            }
            for w in found
        ],
        "attention": {
            key: needs.get(key) or []
            for key in ("paused", "gave_up", "repeating", "parked", "grown")
        },
    }


def _signature(conn: sqlite3.Connection) -> tuple[Any, ...]:
    """What changes when any worker does something: newest event, task statuses."""
    newest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    statuses = conn.execute("SELECT id, status FROM tasks ORDER BY id").fetchall()
    return (int(newest), tuple((int(r["id"]), str(r["status"])) for r in statuses))


def follow_workers(
    write: Callable[[list[dict[str, Any]]], None],
    *,
    actions: int = DEFAULT_ACTIONS,
    follow: bool = False,
    sleep: Callable[[float], None] | None = None,
    interval: float = 2.0,
    polls: int | None = None,
) -> int:
    """`ppy workers [--follow]`: hand ``write`` the workers, then again whenever they change.

    The same polling shape as :func:`tail`: ``polls`` bounds how many times a follow
    looks again (``None`` is until interrupted). Returns how many times it wrote.
    """
    sleep = sleep or time.sleep
    last: tuple[Any, ...] | None = None
    written, looked = 0, 0
    while True:
        conn = db.init_db()
        try:
            signature = _signature(conn)
            if signature != last:
                write(workers(conn, actions=actions))
                written += 1
                last = signature
        finally:
            conn.close()
        if not follow or (polls is not None and looked >= polls):
            return written
        looked += 1
        sleep(interval)


__all__ = [
    "BLOCKERS_EVENT",
    "COLOR_CHOICES",
    "PR_OBSERVED_EVENT",
    "ROUND_SUMMARY_EVENT",
    "Paint",
    "colour_wanted",
    "event_line",
    "follow_workers",
    "render_workers",
    "attention",
    "attention_lines",
    "a_persons_look",
    "look",
    "mark_looked",
    "peek",
    "terminal_width",
    "work_item",
    "worker_actions",
    "workers",
    "workers_json",
    "feed",
    "parse_duration",
    "person_actions",
    "render",
    "snapshot",
    "status_line",
    "tail",
]
