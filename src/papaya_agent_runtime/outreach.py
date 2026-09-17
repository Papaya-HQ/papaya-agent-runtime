"""Reaching the person a decision is waiting on, whichever way the runtime is running.

Some of what the runtime holds can only move when a person decides: a question the
manager recorded against a task (`ppy todo add --blocked-on user`), a capability a
worker asked for that policy left to a person, a delivered pull request the reconcile
lane gave up on. On 2026-09-17 a product decision (the pill copy on PAP-242) was posted
on its ticket as a decision already made, filed in the ledger as "waiting on user", and
mentioned in passing by the next session; the person it waited on found it by asking.
Shane's rule, standing: anything blocked on a person is chased until it is answered,
through a channel that works without a terminal — under `ppy serve` there is no session
to ask in.

This module is that procedure, one decision both modes run:

- :func:`collect` reads every open *ask* — the same three kinds, from the same ledger,
  every time;
- :func:`observe` keeps the `outreach` table in step with it: an ask seen for the first
  time, an ask that is gone (answered, closed, decided) marked resolved;
- :func:`due` says which asks to say now — never said, or said longer ago than
  :data:`REPEAT_AFTER_SECONDS`;
- :func:`message` and :func:`ticket_bodies` are the words: one grouped message for the
  owner's DM with this agent, and one comment per work item an ask belongs to;
- :func:`record_said` writes down where it was said, so nothing is said twice in a
  round and every repeat says which reminder it is.

`ppy serve` runs it every round (`rounds.Rounds._outreach_lane`) and posts through its
own connection. A session runs it from the heartbeat (`watch.outreach_step`), from the
session hooks (the list at start; at Stop, the asks nothing remote reached are put in
the reply, once) and from `ppy outreach run`. :func:`step` is the whole thing for a
session; serve takes the same pieces around its async posting.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from papaya_agent_runtime.state import store

log = logging.getLogger("papaya_agent_runtime.outreach")

#: How long after an ask was last said before it is said again.
REPEAT_AFTER_SECONDS = 2 * 60 * 60.0
#: The environment override for :data:`REPEAT_AFTER_SECONDS`, in seconds.
REPEAT_ENV = "PPY_OUTREACH_REPEAT_SECONDS"

#: The kinds of ask.
DECISION = "decision"
CAPABILITY = "capability"
PULL_REQUEST = "pull_request"

#: The channels an ask can be said through.
VIA_DM = "dm"
VIA_TICKET = "ticket"
VIA_DESKTOP = "desktop"
VIA_SESSION = "session"
#: The channels that reach a person who is not at a terminal.
REMOTE = frozenset({VIA_DM, VIA_TICKET})

#: The event recorded each time asks are said, for `ppy tail` and the tests.
SAID_EVENT = "outreach_said"


def repeat_after_seconds() -> float:
    raw = os.environ.get(REPEAT_ENV)
    if raw:
        try:
            return max(60.0, float(raw))
        except ValueError:
            pass
    return REPEAT_AFTER_SECONDS


def _parse(stamp: object) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _ago(now: datetime, stamp: object) -> str:
    since = _parse(stamp)
    if since is None:
        return ""
    from papaya_agent_runtime import health

    return health.humanize(int(max(0.0, (now - since).total_seconds())))


@dataclass(frozen=True)
class Ask:
    """One thing waiting on a person, said the way they read it."""

    key: str
    kind: str
    text: str
    #: What unblocks it: the reply to give, or the command to run.
    how: str
    task_id: int | None = None
    work_item_id: str | None = None
    since: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "text": self.text,
            "how": self.how,
            "task_id": self.task_id,
            "work_item_id": self.work_item_id,
            "since": self.since,
        }


@dataclass
class Plan:
    """What one round says, and where. Empty when nothing is due."""

    due: list[Ask] = field(default_factory=list)
    #: The DM text, one message for every ask due.
    dm: str = ""
    #: Work item id -> the comment for the asks on it.
    tickets: dict[str, str] = field(default_factory=dict)
    #: One line for a desktop notification.
    headline: str = ""

    def __bool__(self) -> bool:
        return bool(self.due)


# ── what is waiting on a person ──────────────────────────────────────────────


def work_item_of(conn: sqlite3.Connection, task_id: int | None) -> str | None:
    """The Papaya work item a task belongs to, through its ticket or its tracker link."""
    if task_id is None:
        return None
    from papaya_agent_runtime import papaya_events, tracker

    task = store.get_task(conn, task_id)
    if task is None:
        return None
    # A ticket task and the workers dispatched under it share a run.
    row = conn.execute(
        "SELECT task_env.value FROM task_env JOIN tasks ON tasks.id = task_env.task_id "
        "WHERE tasks.run_id = ? AND task_env.key = ? AND json_valid(task_env.value) "
        "ORDER BY tasks.id DESC LIMIT 1",
        (task["run_id"], papaya_events.PAPAYA_EVENT_METADATA),
    ).fetchone()
    if row is not None:
        item = json.loads(row[0]).get("work_item_id")
        if item:
            return str(item)
    link = tracker.task_link(conn, task_id)
    if link and (link.get("provider") or tracker.DEFAULT_PROVIDER) == tracker.DEFAULT_PROVIDER:
        return str(link["record"])
    return None


def _decisions(conn: sqlite3.Connection) -> list[Ask]:
    rows = conn.execute(
        "SELECT id, task_id, text, blocked_on, created_at FROM todos WHERE status = 'open' "
        "AND (blocked_on = 'user' OR blocked_on LIKE 'user:%') ORDER BY id"
    ).fetchall()
    found = []
    for row in rows:
        why = str(row["blocked_on"] or "")
        why = why[len("user:") :].strip() if why.startswith("user:") else ""
        task_id = int(row["task_id"]) if row["task_id"] is not None else None
        text = str(row["text"]).strip()
        if why and why not in text:
            text = f"{text} (waiting on: {why})"
        found.append(
            Ask(
                key=f"todo:{int(row['id'])}",
                kind=DECISION,
                text=text,
                how=f"answer it where you read it, then `ppy todo done {int(row['id'])}`",
                task_id=task_id,
                work_item_id=work_item_of(conn, task_id),
                since=row["created_at"],
            )
        )
    return found


def _capabilities(conn: sqlite3.Connection) -> list[Ask]:
    from papaya_agent_runtime import capability_requests, owed

    found = []
    for item in capability_requests.pending(conn):
        task = store.get_task(conn, item.task_id)
        # A request on a task that is over (delivered, closed) can be decided for nobody
        # (`ppy capability` refuses it too) and is not a person's to answer.
        if task is None or task["status"] not in (*owed.RUNNING_STATUSES, *owed.OWED_STATUSES):
            continue
        why = f" — {item.why}" if item.why else ""
        command = f" (it ran `{item.command}`)" if item.command else ""
        # A request's id is the id of the event that recorded it.
        since = conn.execute("SELECT created_at FROM events WHERE id = ?", (item.id,)).fetchone()
        found.append(
            Ask(
                key=f"capability:{item.id}",
                kind=CAPABILITY,
                text=f"worker task {item.task_id} needs `{item.program}`{why}{command}",
                how=(
                    f"`ppy capability approve {item.id}` (add `--always` for every worker on "
                    f'this machine) or `ppy capability deny {item.id} --reason "..."`'
                ),
                task_id=item.task_id,
                work_item_id=work_item_of(conn, item.task_id),
                since=since["created_at"] if since is not None else None,
            )
        )
    return found


def _pull_requests(conn: sqlite3.Connection) -> list[Ask]:
    from papaya_agent_runtime import reconcile, supervision, team

    found = []
    for task_id, reasons in supervision.prs_needing_a_person():
        url = team._pr_url(conn, task_id)
        marked = conn.execute(
            "SELECT created_at FROM events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
            (task_id, reconcile.NEEDS_A_PERSON),
        ).fetchone()
        where = f" {url}" if url else ""
        found.append(
            Ask(
                key=f"pr:{task_id}",
                kind=PULL_REQUEST,
                text=f"the pull request for task {task_id}{where} needs a person: "
                f"{reasons or 'the reconcile lane gave up on it'}",
                how="look at the pull request; the lane resumes once it changes",
                task_id=task_id,
                work_item_id=work_item_of(conn, task_id),
                since=marked["created_at"] if marked is not None else None,
            )
        )
    return found


def collect(conn: sqlite3.Connection) -> list[Ask]:
    """Every ask open right now: decisions, capability requests, pull requests. Never raises."""
    found: list[Ask] = []
    for read in (_decisions, _capabilities, _pull_requests):
        try:
            found.extend(read(conn))
        except Exception as exc:  # noqa: BLE001 - one unreadable kind never hides the others
            log.warning("[outreach] Could not read %s: %s", read.__name__.strip("_"), exc)
    return found


# ── the ledger of what has been said ─────────────────────────────────────────


def observe(conn: sqlite3.Connection, asks: list[Ask], *, now: datetime) -> list[str]:
    """Bring the table in line with the asks; the lines for what appeared and resolved."""
    stamp = _iso(now)
    lines: list[str] = []
    open_keys = {ask.key for ask in asks}
    for ask in asks:
        row = conn.execute(
            "SELECT key, resolved_at FROM outreach WHERE key = ?", (ask.key,)
        ).fetchone()
        if row is None or row["resolved_at"] is not None:
            conn.execute(
                "INSERT INTO outreach (key, kind, task_id, work_item_id, text, first_seen_at, "
                "last_seen_at, said_at, said_count, said_via, resolved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, NULL) "
                "ON CONFLICT(key) DO UPDATE SET kind = excluded.kind, task_id = excluded.task_id, "
                "work_item_id = excluded.work_item_id, text = excluded.text, "
                "first_seen_at = excluded.first_seen_at, last_seen_at = excluded.last_seen_at, "
                "said_at = NULL, said_count = 0, said_via = NULL, resolved_at = NULL",
                (
                    ask.key,
                    ask.kind,
                    ask.task_id,
                    ask.work_item_id,
                    ask.text,
                    ask.since or stamp,
                    stamp,
                ),
            )
            lines.append(f"waiting on a person: {ask.text}")
        else:
            conn.execute(
                "UPDATE outreach SET text = ?, task_id = ?, work_item_id = ?, last_seen_at = ? "
                "WHERE key = ?",
                (ask.text, ask.task_id, ask.work_item_id, stamp, ask.key),
            )
    for row in conn.execute("SELECT key, text FROM outreach WHERE resolved_at IS NULL").fetchall():
        if row["key"] in open_keys:
            continue
        conn.execute("UPDATE outreach SET resolved_at = ? WHERE key = ?", (stamp, row["key"]))
        lines.append(f"no longer waiting on a person: {row['text']}")
    conn.commit()
    return lines


def open_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM outreach WHERE resolved_at IS NULL ORDER BY first_seen_at, key"
        ).fetchall()
    )


def due(conn: sqlite3.Connection, asks: list[Ask], *, now: datetime) -> list[Ask]:
    """The asks to say now: never said, or said longer ago than the repeat interval."""
    repeat = repeat_after_seconds()
    said: dict[str, datetime | None] = {}
    for row in open_rows(conn):
        said[str(row["key"])] = _parse(row["said_at"])
    found = []
    for ask in asks:
        last = said.get(ask.key)
        if last is None or (now - last).total_seconds() >= repeat:
            found.append(ask)
    return found


def was_said(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute("SELECT said_count FROM outreach WHERE key = ?", (key,)).fetchone()
    return row is not None and int(row["said_count"] or 0) > 0


def record_said(
    conn: sqlite3.Connection, asks: list[Ask], via: list[str], *, now: datetime
) -> None:
    """Write down that these asks were said through ``via`` (an empty ``via`` records nothing)."""
    if not asks or not via:
        return
    stamp = _iso(now)
    for ask in asks:
        conn.execute(
            "UPDATE outreach SET said_at = ?, said_count = said_count + 1, said_via = ? "
            "WHERE key = ?",
            (stamp, json.dumps(sorted(via)), ask.key),
        )
    store.append_event(
        conn,
        kind=SAID_EVENT,
        payload={"keys": [a.key for a in asks], "via": sorted(via), "at": stamp},
    )
    conn.commit()


# ── the words ────────────────────────────────────────────────────────────────


def _nth(count: int) -> str:
    if count <= 0:
        return ""
    return f" · reminder {count}"


def _counts(conn: sqlite3.Connection, asks: list[Ask]) -> dict[str, int]:
    keys = [a.key for a in asks]
    if not keys:
        return {}
    marks = ",".join("?" for _ in keys)
    return {
        str(r["key"]): int(r["said_count"] or 0)
        for r in conn.execute(
            f"SELECT key, said_count FROM outreach WHERE key IN ({marks})", keys
        ).fetchall()
    }


def message(conn: sqlite3.Connection, asks: list[Ask], *, now: datetime, host: str) -> str:
    """The one DM for every ask due: what, since when, which reminder, how to unblock it."""
    if not asks:
        return ""
    from papaya_agent_runtime import blockers

    counts = _counts(conn, asks)
    head = (
        f"Waiting on you ({host}): {len(asks)} thing{'s' if len(asks) != 1 else ''} "
        "nothing else can move."
    )
    lines = [head]
    for i, ask in enumerate(asks, 1):
        ago = _ago(now, ask.since)
        where = f"[{ask.work_item_id}] " if ask.work_item_id else ""
        stamp = f" — since {ago} ago" if ago else ""
        lines.append(f"{i}. {where}{ask.text}{stamp}{_nth(counts.get(ask.key, 0))}")
        lines.append(f"   → {ask.how}")
    lines.append(f"I will say this again every {_every()} until each is answered.")
    return blockers.redact("\n".join(lines))


def _every() -> str:
    seconds = repeat_after_seconds()
    if seconds >= 3600:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 60)}m"


def ticket_bodies(conn: sqlite3.Connection, asks: list[Ask], *, now: datetime) -> dict[str, str]:
    """One comment per work item, for the asks that belong to it."""
    from papaya_agent_runtime import blockers

    counts = _counts(conn, asks)
    by_item: dict[str, list[Ask]] = {}
    for ask in asks:
        if ask.work_item_id:
            by_item.setdefault(ask.work_item_id, []).append(ask)
    bodies: dict[str, str] = {}
    for item, group in by_item.items():
        lines = ["Waiting on a person; nothing else can move this:"]
        for ask in group:
            ago = _ago(now, ask.since)
            stamp = f" (since {ago} ago)" if ago else ""
            lines.append(f"- {ask.text}{stamp}{_nth(counts.get(ask.key, 0))}")
            lines.append(f"  To unblock: {ask.how}")
        bodies[item] = blockers.redact("\n".join(lines))
    return bodies


def headline(asks: list[Ask]) -> str:
    if not asks:
        return ""
    first = asks[0].text
    first = first if len(first) <= 90 else first[:87] + "..."
    more = f" (+{len(asks) - 1} more)" if len(asks) > 1 else ""
    return f"Waiting on you: {first}{more}"


def plan(conn: sqlite3.Connection, *, now: datetime, host: str) -> tuple[Plan, list[str]]:
    """Read, reconcile and decide: the plan for this round and the lines of what changed."""
    asks = collect(conn)
    lines = observe(conn, asks, now=now)
    wanted = due(conn, asks, now=now)
    if not wanted:
        return Plan(), lines
    return (
        Plan(
            due=wanted,
            dm=message(conn, wanted, now=now, host=host),
            tickets=ticket_bodies(conn, wanted, now=now),
            headline=headline(wanted),
        ),
        lines,
    )


# ── the channels a session has ───────────────────────────────────────────────


#: The channel to fall back to when this agent has no DM with its owner, by name.
CHANNEL_ENV = "PPY_OUTREACH_CHANNEL"


def _channel_rows(channels: Any) -> list[dict[str, Any]]:
    if isinstance(channels, dict):
        channels = channels.get("channels")
    return (
        [c for c in (channels or []) if isinstance(c, dict)] if isinstance(channels, list) else []
    )


def fallback_channel_id(channels: Any, *, wanted: str | None = None) -> str | None:
    """A channel this agent is a member of to say things in when it has no DM.

    ``wanted`` (:data:`CHANNEL_ENV`) names one; otherwise the member channel with the
    fewest people, then by name — the closest thing to private among what it can see.
    """
    rows = [c for c in _channel_rows(channels) if c.get("is_member")]
    if wanted:
        for c in rows:
            if str(c.get("name") or "").strip().lower() == wanted.strip().lower():
                return str(c.get("id") or c.get("channel_id") or "") or None
    rows.sort(key=lambda c: (int(c.get("member_count") or 0), str(c.get("name") or "")))
    for c in rows:
        identifier = str(c.get("id") or c.get("channel_id") or "").strip()
        if identifier:
            return identifier
    return None


async def _owner_mention(api: Any) -> dict[str, str] | None:
    """The person who connected this agent, as a mention payload; ``None`` if unknown."""
    from papaya_agent_client import api_client

    me = await api_client.agent_whoami(api)
    connection = me.get("connection") if isinstance(me, dict) else None
    owner_id = str((connection or {}).get("owner_id") or "").strip()
    if not owner_id:
        return None
    workspace_id = api.agent_config["workspace_id"]
    members = await api.request_json("GET", f"/workspaces/{workspace_id}/members")
    rows = members.get("result") if isinstance(members, dict) else members
    for member in rows or []:
        if isinstance(member, dict) and str(member.get("id") or "") == owner_id:
            handle = str(member.get("handle") or "").strip()
            return {
                "type": "user",
                "id": owner_id,
                "handle": handle,
                "display_name": str(member.get("display_name") or handle or "owner"),
            }
    return {"type": "user", "id": owner_id, "handle": "", "display_name": "owner"}


async def say_in_workspace(api: Any, text: str) -> bool:
    """Put ``text`` where its owner reads it: their DM with this agent, or a channel with
    them mentioned. ``False`` when neither exists or the post did not land. Never raises.
    """
    if api is None or not text:
        return False
    try:
        from papaya_agent_client import api_client

        from papaya_agent_runtime.serve import dm_channel_id

        channels = await api_client.list_agent_channels(api)
        channel = dm_channel_id(channels)
        if channel is not None:
            await api_client.post_agent_channel_message(api, channel, text)
            return True
        channel = fallback_channel_id(channels, wanted=os.environ.get(CHANNEL_ENV))
        if channel is None:
            log.warning("[outreach] This agent is in no DM and no channel; nothing was posted")
            return False
        mention = await _owner_mention(api)
        content = text
        payload: dict[str, Any] = {"content": content}
        if mention:
            if mention["handle"]:
                content = f"@{mention['handle']} — {text}"
            payload = {"content": content, "mentions": [mention]}
        workspace_id = api.agent_config["workspace_id"]
        await api.request_json(
            "POST", f"/workspaces/{workspace_id}/channels/{channel}/messages", json=payload
        )
        return True
    except Exception as exc:  # noqa: BLE001 - an unreachable workspace is not a crash
        log.warning("[outreach] Could not post to the workspace: %s", exc)
        return False


def post_dm(text: str) -> bool:
    """The session's form of :func:`say_in_workspace`, on this connection's client."""
    import asyncio

    from papaya_agent_runtime import papaya

    try:
        return asyncio.run(say_in_workspace(papaya.agent_api(), text))
    except Exception as exc:  # noqa: BLE001 - an unreachable workspace is not a crash
        log.warning("[outreach] Could not post to the workspace: %s", exc)
        return False


def post_ticket(work_item_id: str, body: str, *, environ: dict[str, str] | None = None) -> bool:
    """Comment on a work item as this machine's agent. Never raises."""
    from papaya_agent_runtime import papaya, papaya_events

    try:
        env = environ if environ is not None else papaya.agent_env()
        if not env.get("PAPAYA_AGENT_TOKEN"):
            return False
        event = papaya_events.PapayaEvent(
            id=None,
            kind="",
            subject=f"work_item:{work_item_id}",
            payload={},
            work_item_id=work_item_id,
        )
        return papaya_events.post_work_item_comment(event, body, environ=env)
    except Exception as exc:  # noqa: BLE001 - a comment that did not land is said elsewhere
        log.warning("[outreach] Could not comment on %s: %s", work_item_id, exc)
        return False


#: Set to ``1`` to also raise a macOS desktop notification for what is due. Off by
#: default: `osascript`'s notifications are attributed to Script Editor, so clicking one
#: opens Script Editor rather than the ask (Shane, 2026-09-17) — noise, not a channel.
DESKTOP_ENV = "PPY_OUTREACH_DESKTOP"


def desktop_enabled() -> bool:
    return os.environ.get(DESKTOP_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def notify_desktop(text: str) -> bool:
    """A macOS desktop notification, only when :data:`DESKTOP_ENV` asks for one. Never raises."""
    if not desktop_enabled() or sys.platform != "darwin" or not text:
        return False
    safe = text.replace("\\", "\\\\").replace('"', '\\"')
    try:
        done = subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{safe}" with title "Papaya Agent Runtime"',
            ],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def deliver(
    conn: sqlite3.Connection,
    found: Plan,
    *,
    now: datetime,
    dm=None,
    ticket=None,
    desktop=None,
    session: bool = False,
) -> list[str]:
    """Say the plan through every channel that lands; record it; the lines of what happened.

    ``session`` is true when a session will put the asks in front of the person itself
    (the hook's reply): that counts as said only when nothing remote landed, so a person
    with no terminal open is never marked told by a line they could not read. The
    channels default to this module's at call time, so a test that replaces one
    replaces it everywhere.
    """
    if not found:
        return []
    dm = dm or post_dm
    ticket = ticket or post_ticket
    desktop = desktop or notify_desktop
    via: list[str] = []
    for item, body in found.tickets.items():
        if ticket(item, body):
            via.append(VIA_TICKET)
    if found.dm and dm(found.dm):
        via.append(VIA_DM)
    if found.headline and desktop(found.headline):
        via.append(VIA_DESKTOP)
    reached = sorted(set(via))
    if not (set(reached) & REMOTE) and session:
        reached = sorted({*reached, VIA_SESSION})
    record_said(conn, found.due, reached, now=now)
    what = ", ".join(reached) if reached else "nowhere it could reach"
    lines = [f"said to a person ({what}): {a.text}" for a in found.due]
    if not set(reached) & REMOTE:
        lines.append(
            "nothing remote reached the person: connect this machine to a Papaya agent "
            "(`ppy papaya connect`) so a decision does not wait on someone opening a terminal"
        )
    return lines


def step(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    host: str | None = None,
    dm=None,
    ticket=None,
    desktop=None,
    session: bool = False,
) -> list[str]:
    """The whole procedure for a session: read, reconcile, say what is due, record it."""
    from papaya_agent_runtime import blockers

    now = now or datetime.now(UTC)
    found, lines = plan(conn, now=now, host=host or blockers.short_hostname())
    return lines + deliver(
        conn, found, now=now, dm=dm, ticket=ticket, desktop=desktop, session=session
    )


# ── what a surface shows ─────────────────────────────────────────────────────


def summary(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Every open ask with when it was first seen and last said, for status and the hooks.

    Reads the ledger after bringing it in line with what is open now, so a surface that
    only looks (status, the session-start hook) still sees an ask recorded a moment ago.
    """
    now = now or datetime.now(UTC)
    observe(conn, collect(conn), now=now)
    found = []
    for row in open_rows(conn):
        via = []
        try:
            via = json.loads(row["said_via"] or "[]")
        except ValueError:
            via = []
        found.append(
            {
                "key": str(row["key"]),
                "kind": str(row["kind"]),
                "text": str(row["text"]),
                "task_id": row["task_id"],
                "work_item_id": row["work_item_id"],
                "waiting_seconds": max(
                    0.0, (now - (_parse(row["first_seen_at"]) or now)).total_seconds()
                ),
                "said_at": row["said_at"],
                "said_count": int(row["said_count"] or 0),
                "said_via": via,
            }
        )
    return found


def lines(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[str]:
    """One line per open ask: what, how long, how many times said and where."""
    from papaya_agent_runtime import health

    now = now or datetime.now(UTC)
    found = []
    for item in summary(conn, now=now):
        waited = health.humanize(int(item["waiting_seconds"]))
        if item["said_count"]:
            said = f"said {item['said_count']}x via {', '.join(item['said_via']) or '?'}"
        else:
            said = "not said yet"
        found.append(f"{item['text']} — waiting {waited}; {said}")
    return found


__all__ = [
    "CAPABILITY",
    "CHANNEL_ENV",
    "DECISION",
    "DESKTOP_ENV",
    "PULL_REQUEST",
    "REMOTE",
    "REPEAT_AFTER_SECONDS",
    "REPEAT_ENV",
    "SAID_EVENT",
    "VIA_DESKTOP",
    "VIA_DM",
    "VIA_SESSION",
    "VIA_TICKET",
    "Ask",
    "Plan",
    "collect",
    "deliver",
    "desktop_enabled",
    "due",
    "fallback_channel_id",
    "headline",
    "lines",
    "message",
    "notify_desktop",
    "observe",
    "open_rows",
    "plan",
    "post_dm",
    "post_ticket",
    "record_said",
    "repeat_after_seconds",
    "say_in_workspace",
    "step",
    "summary",
    "ticket_bodies",
    "was_said",
    "work_item_of",
]
