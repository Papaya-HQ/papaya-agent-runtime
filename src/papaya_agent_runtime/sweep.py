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
- an item Papaya refused to send here — it keeps the work with the agent in
  Papaya, or with another person's or another machine's runtime — is **kept
  elsewhere**: remembered with its `updated_at` and not asked for again until
  that moves or `KEPT_RECHECK_EVERY` (thirty minutes) passes;
- but a claim is not work. Kept work is left alone only while there is
  **evidence** somebody is doing it (:func:`evidence_of_work`): a live
  reservation on the subject, a live agent job, or a comment or status change by
  the holder within `sweep.idle_claim_minutes` (fifteen). Without it the item is
  **idle**: the memory is bypassed and it is asked for on every sweep. When Papaya
  still refuses it (the fallback's guard window), the refused idle items are one
  blocker a person sees (:func:`blockers.set_idle_work_kept`), and an item refused
  for the same reason on :data:`REFUSALS_BEFORE_DEFICIENCY` sweeps running is a
  `repeated-without-progress` occurrence, one row per reason, each ticket once a day;
- a ticket a brief turn found nothing to build on is **parked**, waiting on a
  person (:func:`remember_parked`): left alone until its `updated_at` moves or
  somebody who is not an agent comments after the stamp;
- a full pool ends the round, and the rest wait for the next one.

On start, and on the first sweep after Papaya could not be reached (a reconnect),
the sweep first **reclaims** what an earlier connection of this runtime held
(:meth:`Sweeper._reclaim_earlier`): an item with a ticket task here, a
reservation or a Run on this Mac hold naming an earlier connection id
(`.ppy/papaya-sessions.json`), or an on-call fallback note on an item this
runtime's since-revoked connection had commented on. It calls Papaya's reclaim
route and offers the item, and the runner resumes the ticket from its recorded
state. Nothing a live reservation of somebody else's or a live job shows being
done is taken.

A sweep says one line on stderr — what it found, why it left each one alone,
what it offered — and nothing on the supervised protocol, which is for jobs, not
for bookkeeping. Kept work is said as "being worked" or "idle for N minutes". A
sweep that found exactly what the one before it found says so at most every half
hour.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
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

#: How long an item Papaya kept elsewhere is left alone when nobody changes it: thirty minutes.
KEPT_RECHECK_EVERY = 30 * 60.0

#: What a person can do about work Papaya keeps elsewhere, said once per summary line.
ROUTE_HERE_HINT = "use Run on this Mac to route one here"

#: The work item statuses that still want somebody working on them.
OPEN_STATUSES = frozenset({"todo", "in_progress", "blocked", "changes_requested"})

#: Offer order, most important first. An unknown priority sorts as `normal`.
PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3}

#: Within a priority: somebody is waiting on these, then fresh work, then work
#: that was started once and left. An unknown status sorts last.
STATUS_RANK = {"changes_requested": 0, "blocked": 0, "todo": 1, "in_progress": 2}

#: The phases that mean a hold on the ticket has ended. A task in one of these is
#: history, not work in flight, and does not stop the ticket being offered again.
ENDED_PHASES = ("released", "handed_back", "stalled", "declined", "handed_over", "done")

#: The client's answers to `offer`, named once here.
OFFER_PENDING = "pending"
OFFER_BLOCKED = "blocked"

#: How long `ppy sweep` waits for the running `serve` to finish one sweep.
REQUEST_TIMEOUT_SECONDS = 120.0

#: Minutes kept work may go without evidence of work before it is idle.
DEFAULT_IDLE_CLAIM_MINUTES = 15

#: Sweeps running on which Papaya refuses one idle item before that is a deficiency.
REFUSALS_BEFORE_DEFICIENCY = 3

#: `metadata.system_type` of the note Papaya's on-call fallback leaves on a work item.
FALLBACK_NOTE_TYPE = "on_call_fallback"

#: The connection id Papaya names when its hosted agent is the holder.
HOSTED_CONNECTION_ID = "papaya-hosted"

#: Agent job statuses that mean a run is still going.
LIVE_JOB_STATUSES = frozenset({"queued", "pending", "claimed", "running", "in_progress"})

#: What Papaya's reclaim route said, or that it could not say.
RECLAIMED = "reclaimed"
RECLAIM_REFUSED = "refused"
RECLAIM_UNSUPPORTED = "unsupported"
RECLAIM_FAILED = "failed"

#: Ticket phases that mean this runtime gave the work away on purpose.
GIVEN_AWAY_PHASES = ("handed_back", "declined", "done")

#: What a remembered item's `ended` says when a brief turn found nothing to build:
#: the ticket is parked, waiting on a person (a QA recheck, a decision), not declined.
PARKED = "waiting_on_a_person"

#: The skip reason for a parked ticket nobody has changed or spoken on since.
WAITING_ON_A_PERSON = "waiting on a person"

#: How long one repeating ticket (one fingerprint) is recorded once for: a day.
REPEAT_SAID_EVERY = 24 * 60 * 60.0


def idle_claim_minutes() -> int:
    """`sweep.idle_claim_minutes` from the config, or fifteen when there is none."""
    from papaya_agent_runtime.config import ConfigError, load_config

    try:
        return int(load_config().sweep.idle_claim_minutes)
    except (ConfigError, OSError, ValueError, TypeError):
        return DEFAULT_IDLE_CLAIM_MINUTES


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


def _read_items(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def _write_items(path: Path, data: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def declined_items() -> dict[str, dict[str, Any]]:
    """Every remembered decline, `{work item id: {updated_at, reason, declined_at}}`."""
    return _read_items(declined_path())


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
        _write_items(declined_path(), data)


def forget_declined(work_item_id: str) -> None:
    """Drop everything the sweep remembers about a ticket: it was taken, so it is stale.

    That is a remembered decline and a remembered "kept elsewhere" alike — a
    ticket this machine has just picked up is neither.
    """
    if not work_item_id:
        return
    with _declined_lock:
        data = declined_items()
        if data.pop(str(work_item_id), None) is not None:
            _write_items(declined_path(), data)
    forget_kept([work_item_id])


# ── the tickets parked on a person ──────────────────────────────────────────
#
# A brief turn that finds nothing to build ("the fix is on staging; waiting on QA's
# recheck") ends the hold without handing the ticket back: it is still this agent's,
# and still `in_progress`. Forgotten, it is an `in_progress` item nobody has touched
# for hours, so every sweep offered it, a new brief turn found nothing again, and the
# item got the pickup line every five minutes (PAP-210, 2026-09-19). So the ending is
# remembered beside the declines, with `ended` = :data:`PARKED`, and the sweep leaves
# the ticket alone until the item's `updated_at` moves or somebody who is not an
# agent comments after the stamp. A new pickup, from any path, forgets it.


def remember_parked(
    work_item_id: str, *, updated_at: str | None, reason: str, label: str = ""
) -> None:
    """Park a ticket a brief turn found nothing to build on, as it stood at the time."""
    if not work_item_id:
        return
    now = datetime.now(UTC).isoformat()
    with _declined_lock:
        data = declined_items()
        data[str(work_item_id)] = {
            "updated_at": updated_at,
            "reason": reason,
            "declined_at": now,
            "ended": PARKED,
            "label": label,
        }
        _write_items(declined_path(), data)


def is_parked(remembered: Mapping[str, Any] | None) -> bool:
    return bool(remembered) and (remembered or {}).get("ended") == PARKED


def parked_items() -> dict[str, dict[str, Any]]:
    """Every parked ticket, `{work item id: {updated_at, reason, declined_at, label}}`."""
    return {key: value for key, value in declined_items().items() if is_parked(value)}


def is_agent_comment(comment: Mapping[str, Any]) -> bool:
    """Whether an agent wrote ``comment``: the one authorship rule (`serve` uses it too).

    An `author_actor` is what an agent's comment carries; a person's comment in the
    app has only `author_type`.
    """
    return str(comment.get("author_type") or "") == "agent" or bool(comment.get("author_actor"))


def person_spoke_since(comments: list[dict[str, Any]] | None, stamp: Any) -> bool:
    """Whether anybody who is not an agent (:func:`is_agent_comment`) commented after ``stamp``.

    Comments that could not be read say nothing: the ticket stays parked, and the
    next sweep reads them again.
    """
    since = _timestamp(stamp)
    if not comments or since is None:
        return False
    for comment in comments:
        if is_agent_comment(comment):
            continue
        said = _timestamp(comment.get("created_at"))
        if said is not None and said > since:
            return True
    return False


def ticket_label(item: Mapping[str, Any]) -> str:
    """How a deficiency names a work item: its display id, else `work item <id8>`.

    Never the whole id: a 36-character id is redacted as opaque, and every ticket
    would then share one fingerprint.
    """
    for name in ("short_id", "display_id", "key"):
        if str(item.get(name) or "").strip():
            return str(item[name]).strip()
    return f"work item {str(item.get('id') or '')[:8]}"


def refused_detail(reason: str) -> str:
    """A `repeated-without-progress` detail for work refused sweep after sweep.

    Fingerprinted on the reason alone, and nothing else belongs in it: a refusal is
    Papaya's routing, ten tickets refused for one reason are one problem (one issue),
    and every word added here — which ticket, what claim this runtime had on it — would
    split that one issue into one per ticket. Those go in the evidence.
    """
    return (
        f"assigned work was refused here on {REFUSALS_BEFORE_DEFICIENCY} sweeps running "
        f"for the same reason ({reason})"
    )


#: The refusal reasons Papaya gives for work it deliberately keeps somewhere else: not
#: routed to this machine, held by another machine, taken over by the agent in Papaya.
#: These are Papaya doing its job, so they are not deficiencies on their own — the
#: sweep keeps asking, the blocker names them, and `ppy workers` shows them as kept
#: elsewhere. They become a deficiency only on work this runtime has a claim on
#: (:func:`held_earlier`), which is the shape of the September storm: twelve items this
#: machine held, refused as `not_routed_here` for two days.
EXPECTED_REFUSALS = frozenset({"not_routed_here", "handled_in_papaya", "held_elsewhere"})


@dataclass(frozen=True)
class Refusal:
    """One idle item Papaya refused this sweep, as the blocker and the ledger read it."""

    #: The holder's name, as the blocker lists it.
    name: str
    #: Papaya's own word for why (`not_routed_here`), or `refused` when it gave none.
    reason: str
    #: How a deficiency names the ticket: its short id, else `work item <id8>`.
    label: str
    #: Why this runtime had a claim on the item, when it had one (:func:`held_earlier`).
    claim: str | None = None


# ── the tickets Papaya keeps somewhere else ─────────────────────────────────
#
# Papaya routes an item to one person's machines and keeps the rest with the
# agent in Papaya; a reserve for work that was not sent here is refused
# (the client's `not_routed_here` skip). The sweep cannot change that routing,
# and asking again every five minutes only costs a reserve call per item. So a
# refusal is remembered with the item's `updated_at`, and the item is left alone
# until that moves, `KEPT_RECHECK_EVERY` passes (routing can change without the
# item changing), or a person runs `ppy sweep --include-kept`.


def kept_path() -> Path:
    """Where work Papaya keeps elsewhere is remembered: `.ppy/sweep-kept.json`."""
    return declined_path().with_name("sweep-kept.json")


def kept_items() -> dict[str, dict[str, Any]]:
    """Every remembered refusal, `{work item id: {updated_at, holder, kept_at}}`."""
    return _read_items(kept_path())


def remember_kept(records: Mapping[str, dict[str, Any]]) -> None:
    """Record, per work item id, who Papaya said keeps it, as the item stood then."""
    if not records:
        return
    with _declined_lock:
        data = kept_items()
        data.update({str(key): dict(value) for key, value in records.items()})
        _write_items(kept_path(), data)


def forget_kept(work_item_ids: Any) -> None:
    """Drop remembered refusals for `work_item_ids`: the items were taken here."""
    ids = {str(item_id) for item_id in work_item_ids if item_id}
    if not ids:
        return
    with _declined_lock:
        data = kept_items()
        if ids & data.keys():
            _write_items(kept_path(), {key: value for key, value in data.items() if key not in ids})


def holder_name(holder: Mapping[str, Any] | None) -> str:
    """The name Papaya gave the holder of refused work, as a person would recognise it."""
    holder = holder or {}
    if not holder.get("connection_name") and holder.get("connection_id") == HOSTED_CONNECTION_ID:
        return "the agent in Papaya"
    return str(holder.get("connection_name") or holder.get("connection_id") or "another machine")


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


def kept_elsewhere(item: dict[str, Any], remembered: dict[str, Any] | None, *, now: float) -> bool:
    """Whether Papaya refused `item` here recently and nobody has changed it since.

    Recently is within `KEPT_RECHECK_EVERY` of the refusal, by the sweep's clock:
    routing can move without the item moving, so the question is asked again
    after that. A strictly later `updated_at` than the one remembered asks it now.
    """
    if remembered is None:
        return False
    kept_at = _timestamp(remembered.get("kept_at"))
    if kept_at is None or now - kept_at.timestamp() >= KEPT_RECHECK_EVERY:
        return False
    then = _timestamp(remembered.get("updated_at"))
    current = _timestamp(item.get("updated_at"))
    return then is None or current is None or current <= then


# ── evidence of work ────────────────────────────────────────────────────────
#
# Papaya saying who keeps an item is a claim. Whether anybody is doing it is read
# from what doing it leaves behind: a lease being renewed, a run in flight, or
# the holder saying or changing something on the item lately.


@dataclass(frozen=True)
class Evidence:
    """Whether kept work shows anybody doing it, and when its holder last did anything."""

    working: bool
    #: What showed it, for a log line: "a live reservation", "a live agent job", ...
    what: str = ""
    last_activity: datetime | None = None

    def idle_minutes(self, now: datetime) -> int | None:
        if self.working or self.last_activity is None:
            return None
        return max(0, int((now - self.last_activity).total_seconds() // 60))


def live_reservation(item: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
    """The item's reservation when it has one whose lease has not run out."""
    reservation = item.get("reservation")
    if not isinstance(reservation, dict) or not reservation:
        return None
    expires = _timestamp(reservation.get("lease_expires_at"))
    if expires is not None and expires <= now:
        return None
    return reservation


def live_job(item: Mapping[str, Any]) -> bool:
    """Whether the item names an agent job or run that is still going."""
    for key in ("agent_jobs", "linked_agent_jobs", "agent_runs", "jobs"):
        for job in item.get(key) or ():
            status = str(job.get("status") or "").strip().lower() if isinstance(job, dict) else ""
            if status in LIVE_JOB_STATUSES:
                return True
    return False


def _via_connection(comment: Mapping[str, Any]) -> dict[str, Any]:
    actor = comment.get("author_actor")
    via = actor.get("via_connection") if isinstance(actor, dict) else None
    return via if isinstance(via, dict) else {}


def said_by_holder(comment: Mapping[str, Any], holder: Mapping[str, Any], agent_id: str) -> bool:
    """Whether the holder Papaya named wrote ``comment``.

    A machine writes through its connection. The hosted agent writes as the agent
    through no connection at all.
    """
    connection = str(holder.get("connection_id") or "")
    via = _via_connection(comment)
    if connection and connection != HOSTED_CONNECTION_ID:
        return str(via.get("id") or "") == connection
    if str(comment.get("author_type") or "") != "agent" or via:
        return False
    return not agent_id or str(comment.get("author_id") or "") == agent_id


def holder_activity(
    item: Mapping[str, Any],
    comments: list[dict[str, Any]],
    holder: Mapping[str, Any],
    *,
    agent_id: str,
) -> datetime | None:
    """When the holder last commented on or changed the item, as far as can be read.

    A status change is read from `status_changed_at` when Papaya sends one, else
    from `updated_at`, which moves for any change: that can only make work look
    busier than it is, never idler.
    """
    stamps = [
        _timestamp(comment.get("created_at"))
        for comment in comments
        if said_by_holder(comment, holder, agent_id)
    ]
    stamps.append(_timestamp(item.get("status_changed_at")) or _timestamp(item.get("updated_at")))
    return max((stamp for stamp in stamps if stamp is not None), default=None)


def evidence_of_work(
    item: Mapping[str, Any],
    comments: list[dict[str, Any]] | None,
    holder: Mapping[str, Any] | None,
    *,
    agent_id: str,
    now: datetime,
    idle_after: float,
) -> Evidence:
    """Whether anybody is doing kept work: a live reservation, a live job, or recent activity.

    Comments that could not be read cannot show the work idle, so they count as work.
    """
    if live_reservation(item, now) is not None:
        return Evidence(True, "a live reservation")
    if live_job(item):
        return Evidence(True, "a live agent job")
    if comments is None:
        return Evidence(True, "comments that could not be read")
    last = holder_activity(item, comments, holder or {}, agent_id=agent_id)
    if last is not None and (now - last).total_seconds() < idle_after:
        return Evidence(True, "recent activity by the holder", last)
    return Evidence(False, "", last)


def _short(item: Mapping[str, Any]) -> str:
    return str(item.get("short_id") or item.get("id") or "")


def held_earlier(
    item: Mapping[str, Any], ticket: Any, earlier: set[str], *, now: datetime
) -> str | None:
    """Why an earlier connection of this runtime held ``item``, from what needs no extra call."""
    if ticket is not None and ticket.phase not in GIVEN_AWAY_PHASES:
        return f"ticket task {ticket.task_id} here"
    reservation = live_reservation(item, now) or {}
    holder = reservation.get("holder") if isinstance(reservation.get("holder"), dict) else {}
    if holder and str(holder.get("connection_id") or "") in earlier:
        return "reserved by an earlier connection"
    hold = item.get("run_on_this_mac")
    if isinstance(hold, dict) and str(hold.get("connection_id") or "") in earlier:
        return "kept for an earlier connection"
    return None


def taken_by_fallback(comments: list[dict[str, Any]] | None, earlier: set[str]) -> str | None:
    """Why Papaya's on-call fallback took ``comments``' item from this runtime, if it did.

    The fallback leaves a note carrying its `handoff_id`; the item was this runtime's
    when one of its own connections, since revoked, had commented on it.
    """
    if not comments:
        return None
    notes = [
        comment
        for comment in comments
        if isinstance(comment.get("metadata"), dict)
        and comment["metadata"].get("system_type") == FALLBACK_NOTE_TYPE
        and comment["metadata"].get("handoff_id")
    ]
    if not notes:
        return None
    for comment in comments:
        via = _via_connection(comment)
        if str(via.get("id") or "") in earlier and str(via.get("status") or "") == "revoked":
            return f"taken by the on-call fallback (handoff {notes[-1]['metadata']['handoff_id']})"
    return None


class PapayaReads:
    """The Papaya calls the evidence and the reclaim need, over the listener's agent api."""

    def __init__(self, api: Any) -> None:
        self._api = api

    def _workspace(self) -> str:
        return str((getattr(self._api, "agent_config", None) or {}).get("workspace_id") or "")

    async def comments(self, work_item_id: str) -> list[dict[str, Any]] | None:
        """The item's comments, or ``None`` when they cannot be read."""
        try:
            answer = await self._api.request_json(
                "GET", f"/workspaces/{self._workspace()}/work-items/{work_item_id}/comments"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - unreadable is "cannot tell", not an error
            log.debug("[sweep] Could not read the comments on %s: %s", work_item_id, exc)
            return None
        if isinstance(answer, dict):
            answer = answer.get("comments") or answer.get("items") or []
        if not isinstance(answer, list):
            return None
        return [comment for comment in answer if isinstance(comment, dict)]

    async def reservation(self, subject: str) -> dict[str, Any] | None:
        from papaya_agent_client import api_client

        return await api_client.get_subject_reservation(self._api, subject)

    async def reclaim(self, work_item_id: str) -> tuple[str, str]:
        """Ask Papaya to give this connection back what an earlier one held.

        The route is new in the backend ("reconnect is not a decline"); a server
        without it answers 404, which is :data:`RECLAIM_UNSUPPORTED`, and the offer
        that follows falls back on `reserve`.
        """
        path = f"/agent-client/workspaces/{self._workspace()}/work-items/{work_item_id}/reclaim"
        try:
            response = await self._api.request_response("POST", path, json={})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the offer is still worth making
            return RECLAIM_FAILED, str(exc) or exc.__class__.__name__
        if response.status_code in (404, 405):
            return RECLAIM_UNSUPPORTED, "Papaya has no reclaim route yet"
        if response.status_code >= 400:
            return RECLAIM_FAILED, f"HTTP {response.status_code}"
        try:
            body = response.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        reason = str(body.get("reason") or "")
        return (RECLAIMED if body.get("reclaimed") else RECLAIM_REFUSED), reason


def ticket_index() -> dict[str, Any]:
    """The newest ticket task per work item, from the rounds' reading of the table."""
    from papaya_agent_runtime import rounds
    from papaya_agent_runtime.lifecycle import TERMINAL_STATUSES

    return {
        ticket.work_item_id: ticket
        for ticket in rounds.ticket_tasks()
        if ticket.status not in TERMINAL_STATUSES
    }


def earlier_connection_ids() -> set[str]:
    """Every connection id this runtime has held a lease under (`.ppy/papaya-sessions.json`)."""
    from papaya_agent_runtime import serve

    return set(serve.stored_session_ids())


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
    #: Of `skipped`, tickets parked when a brief turn found nothing to build, unchanged since.
    waiting_on_a_person: int = 0
    #: Of `skipped`, the items Papaya keeps elsewhere and somebody is working, as
    #: `(holder name, count)`, most first.
    kept: tuple[tuple[str, int], ...] = ()
    #: Of `skipped`, the items Papaya keeps elsewhere that nobody is working, as
    #: `(holder name, count, minutes since the holder last did anything or None)`.
    idle: tuple[tuple[str, int, int | None], ...] = ()
    #: Open items not reached because every slot was busy; the next sweep has them.
    waiting: int = 0
    #: Why the sweep could not ask Papaya at all, when it could not.
    error: str | None = None

    @property
    def kept_total(self) -> int:
        return sum(count for _, count in self.kept) + self.idle_total

    @property
    def idle_total(self) -> int:
        return sum(count for _, count, _ in self.idle)

    def _parts(self) -> str:
        """Why each found item was left alone, then what was offered."""
        parts = [f"{count} kept by {name} and being worked" for name, count in self.kept]
        for name, count, minutes in self.idle:
            idle = "idle" if minutes is None else f"idle for {minutes} minutes"
            parts.append(f"{count} kept by {name} and {idle}")
        if parts:
            parts[-1] += f" ({ROUTE_HERE_HINT})"
        if self.declined_earlier:
            parts.append(f"{self.declined_earlier} declined earlier")
        if self.in_progress_elsewhere:
            parts.append(f"{self.in_progress_elsewhere} in progress elsewhere")
        if self.waiting_on_a_person:
            parts.append(f"{self.waiting_on_a_person} {WAITING_ON_A_PERSON}")
        # Live here already, or held by another session this round.
        taken = (
            self.skipped
            - self.kept_total
            - self.declined_earlier
            - self.in_progress_elsewhere
            - self.waiting_on_a_person
        )
        if taken:
            parts.append(f"{taken} already taken")
        parts.append(f"{self.offered} offered")
        return ", ".join(parts)

    def summary(self) -> str:
        if self.error is not None:
            return f"sweep could not list assigned work: {self.error}"
        line = f"sweep found {self.found}: {self._parts()}"
        if self.waiting:
            line += f"; {self.waiting} left for the next sweep (every slot is busy)"
        return line

    def unchanged_summary(self) -> str:
        """The line for a sweep that found exactly what the one before it found."""
        if self.error is not None:
            return f"sweep still cannot list assigned work: {self.error}"
        if self.waiting:
            return f"sweep unchanged: still {self.waiting} waiting for a slot"
        return f"sweep unchanged: still found {self.found}: {self._parts()}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "offered": self.offered,
            "skipped": self.skipped,
            "declined_earlier": self.declined_earlier,
            "in_progress_elsewhere": self.in_progress_elsewhere,
            "waiting_on_a_person": self.waiting_on_a_person,
            "kept": dict(self.kept),
            "idle": {name: {"count": n, "minutes": minutes} for name, n, minutes in self.idle},
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


def skip_reason(
    item: dict[str, Any],
    *,
    now: datetime,
    live: set[str],
    declined: dict[str, dict[str, Any]],
    stale_after: float,
    running: set[str] | frozenset[str] = frozenset(),
    kept: dict[str, dict[str, Any]] | None = None,
    elsewhere: bool = True,
) -> str | None:
    """Why an assigned item is not offered, from what needs no Papaya call, or ``None``.

    The synchronous half of :func:`gate`, which every path that offers or lists work
    calls; nothing should call this directly, because it cannot read the comments a
    parked ticket un-parks on. ``elsewhere=False`` leaves out the "in progress
    elsewhere" guess, for the reclaim, whose items are known to have been this
    runtime's. Work kept elsewhere is judged on evidence afterwards, by the sweep.
    """
    item_id = str(item["id"])
    if f"work_item:{item_id}" in running or item_id in live:
        return "live here"
    remembered = declined.get(item_id)
    # A parked ticket is this runtime's own: a recent touch is a change on it (QA's
    # answer), not somebody else working it, so it is not judged "elsewhere".
    if (
        elsewhere
        and (kept or {}).get(item_id) is None
        and not is_parked(remembered)
        and in_progress_elsewhere(item, now=now, stale_after=stale_after)
    ):
        return "in progress elsewhere"
    if declined_earlier(item, remembered):
        return WAITING_ON_A_PERSON if is_parked(remembered) else "declined earlier, unchanged"
    return None


async def gate(
    item: dict[str, Any],
    *,
    now: datetime,
    live: set[str],
    declined: dict[str, dict[str, Any]],
    stale_after: float,
    comments: Callable[[str], Awaitable[list[dict[str, Any]] | None]],
    running: set[str] | frozenset[str] = frozenset(),
    kept: dict[str, dict[str, Any]] | None = None,
    elsewhere: bool = True,
) -> str | None:
    """Why an assigned item is not offered or listed, or ``None``: the one gate.

    Every path that offers or lists assigned work calls it: the sweep
    (`Sweeper._sweep`), the reclaim on start and reconnect (`Sweeper._reclaim_earlier`)
    and a session's list of waiting work (`supervision.assigned_unpicked`). It holds
    the memories — live here, declined earlier and unchanged, parked on a person — and
    the whole un-park rule: a parked ticket is offered again when its `updated_at`
    moves past the stamp, or when somebody who is not an agent commented after the
    stamp (``comments`` is read only for a parked ticket that has not otherwise moved).
    """
    reason = skip_reason(
        item,
        now=now,
        live=live,
        declined=declined,
        stale_after=stale_after,
        running=running,
        kept=kept,
        elsewhere=elsewhere,
    )
    if reason != WAITING_ON_A_PERSON:
        return reason
    item_id = str(item["id"])
    stamp = (declined.get(item_id) or {}).get("updated_at")
    try:
        said = await comments(item_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - unreadable comments leave it parked
        log.debug("[sweep] Could not read the comments on %s: %s", item_id, exc)
        return reason
    if person_spoke_since(said, stamp):
        log.debug("[sweep] work_item:%s: a person spoke since it was parked", item_id)
        return None
    return reason


def forget_parked_absent(open_ids: set[str]) -> list[str]:
    """Drop parked tickets no longer open and assigned here, after a *successful* listing.

    A parked ticket that was closed or reassigned is no longer waiting on anybody
    here; left in place it would stay in `needs attention` for ever. Returns the ids
    dropped. Never call it with the answer of a listing that failed.
    """
    with _declined_lock:
        data = declined_items()
        gone = [key for key, value in data.items() if is_parked(value) and key not in open_ids]
        if gone:
            _write_items(declined_path(), {k: v for k, v in data.items() if k not in gone})
    return gone


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
        lock: asyncio.Lock | None = None,
        runner: Any = None,
        reads: Any = None,
        idle_after: float | None = None,
        connection_ids: Callable[[], set[str]] | None = None,
        tickets: Callable[[], dict[str, Any]] | None = None,
        publish: Callable[[], None] | None = None,
    ) -> None:
        self._built = built
        #: The ticket runner a reclaimed item resumes on (its `reclaim(item, task, mark)`).
        self._runner = runner
        # Seams for tests: Papaya's comments, reservation and reclaim route; the
        # connection ids this runtime has held; its ticket tasks by work item.
        self._reads = reads if reads is not None else PapayaReads(getattr(built, "api", None))
        #: `sweep.idle_claim_minutes`, in seconds: kept work with no evidence this long is idle.
        self._idle_after = idle_claim_minutes() * 60.0 if idle_after is None else float(idle_after)
        self._connection_ids = connection_ids or earlier_connection_ids
        self._tickets = tickets or ticket_index
        #: Sends a supervised host a fresh `status` when the blockers changed.
        self._publish = publish
        #: Reclaim what earlier connections held before the next sweep: at start and
        #: after a sweep that could not reach Papaya.
        self._reclaim_due = True
        #: Idle items Papaya refused, by work item id: the name the blocker lists.
        self._refused_idle: dict[str, str] | None = None
        #: Work item id -> (reason, sweeps running on which Papaya refused it for that
        #: reason while idle).
        self._refusal_streak: dict[str, tuple[str, int]] = {}
        #: Reclaim lines, for tests and `ppy serve`'s log.
        self.reclaim_lines: list[str] = []
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
        # `serve` passes the lock its manager rounds hold too, for the same reason.
        self._lock = lock or asyncio.Lock()
        self.results: list[SweepResult] = []
        #: When a summary line was last written, or None before the first.
        self._last_written: float | None = None
        #: Subjects whose reserve Papaya refused on its own word (not routed here, or
        #: handled in Papaya), with the holder.
        self._refused: dict[str, dict[str, Any]] = {}
        #: The listener's events client whose `reserve` is already watched.
        self._watched: Any = None

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
        self, *, include_declined: bool = False, include_kept: bool = False, by_hand: bool = False
    ) -> SweepResult:
        """One round: list, choose, offer. Never raises; a failure is the result.

        `include_declined` offers tickets this runtime declined earlier even when
        nobody has changed them since — the by-hand `ppy sweep --include-declined`.
        `include_kept` asks again for items Papaya recently kept elsewhere
        (`ppy sweep --include-kept`). `by_hand` is a person asking, who always
        gets the line.
        """
        async with self._lock:
            try:
                result = await self._sweep(
                    include_declined=include_declined, include_kept=include_kept
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad sweep must not end serve
                log.warning("[sweep] Sweep failed: %s", exc)
                result = SweepResult(error=str(exc) or exc.__class__.__name__)
                from papaya_agent_runtime import deficiencies

                await asyncio.to_thread(deficiencies.record_exception, "the sweep", exc)
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
        # Idle minutes grow every sweep; that alone is not something new to say.
        same = previous is not None and replace(
            result, idle=tuple((n, c, None) for n, c, _ in result.idle)
        ) == replace(previous, idle=tuple((n, c, None) for n, c, _ in previous.idle))
        if by_hand or previous is None or result.offered or not same:
            self._last_written = now
            return result.summary()
        if self._last_written is not None and now - self._last_written < UNCHANGED_SUMMARY_EVERY:
            return None
        self._last_written = now
        return result.unchanged_summary()

    def _watch_refusals(self, loop: Any) -> None:
        """Notice when Papaya refuses a reserve because the work was not sent here.

        `offer` answers that refusal with the same `done` as a lost race or a
        playbook skip, and the holder it names goes no further than a log line.
        The reserve call is where it can be seen, so the loop's events client has
        its `reserve` wrapped once: a `SubjectHeld` the client classifies as
        Papaya's own word (`not_routed_here`, `handled_in_papaya`) is noted by
        subject and raised on unchanged. An ordinary lost race is not noted.
        """
        events = getattr(loop, "_events", None)
        reserve = getattr(events, "reserve", None)
        if reserve is None or events is self._watched:
            return
        from papaya_agent_client.api_client import SubjectHeld
        from papaya_agent_client.listener import refusal_skip_reason

        refused = self._refused

        async def watched_reserve(subject: str, *args: Any, **kwargs: Any) -> Any:
            try:
                return await reserve(subject, *args, **kwargs)
            except SubjectHeld as held:
                if refusal_skip_reason(held.holder) is not None:
                    refused[subject] = dict(held.holder or {})
                raise

        events.reserve = watched_reserve
        self._watched = events

    def _identity(self) -> tuple[str, str]:
        agent_config = getattr(self._built, "agent_config", None) or {}
        return str(agent_config.get("agent_id") or ""), str(agent_config.get("workspace_id") or "")

    async def _offer(self, item: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """Offer one item to the loop: its answer, and the holder when Papaya refused it."""
        subject = f"work_item:{item['id']}"
        agent_id, workspace_id = self._identity()
        self._refused.pop(subject, None)
        status = await self._built.loop.offer(
            envelope_for(item, agent_id=agent_id, workspace_id=workspace_id)
        )
        return status, self._refused.pop(subject, None)

    async def _evidence(
        self, item: dict[str, Any], holder: Mapping[str, Any] | None, now: datetime
    ) -> Evidence:
        """Read what kept work leaves behind: its reservation, its jobs, its holder's activity."""
        subject = f"work_item:{item['id']}"
        if "reservation" not in item:
            try:
                item = {**item, "reservation": await self._reads.reservation(subject)}
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - cannot tell is not idle
                log.debug("[sweep] Could not read the reservation on %s: %s", subject, exc)
                return Evidence(True, "a reservation that could not be read")
        if live_reservation(item, now) is not None or live_job(item):
            return evidence_of_work(
                item, [], holder, agent_id="", now=now, idle_after=self._idle_after
            )
        comments = await self._reads.comments(str(item["id"]))
        return evidence_of_work(
            item,
            comments,
            holder,
            agent_id=self._identity()[0],
            now=now,
            idle_after=self._idle_after,
        )

    def _say(self, line: str) -> None:
        self.reclaim_lines.append(line)
        log.info("[sweep] %s", line)
        if self._stderr is not None:
            with contextlib.suppress(Exception):
                print(f"ppy serve: {line}", file=self._stderr, flush=True)

    async def _reclaim_earlier(
        self,
        items: list[dict[str, Any]],
        live: set[str],
        now: datetime,
        declined: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, tuple[str, dict[str, Any] | None]]:
        """Take back what an earlier connection of this runtime held. Returns each offer's answer.

        One line per item reclaimed or refused, then one summary line; nothing at all
        when no open item was held earlier. A refusal is returned so the sweep that
        follows counts it (and its blocker) without asking Papaya a second time.

        Through the one :func:`gate` first: a ticket parked on a person (its newest task
        ended `reported` then `released`, which is not given away) is not reclaimed on
        every restart and reconnect, and one a person has since answered is.
        """
        self._reclaim_due = False
        earlier = await asyncio.to_thread(self._connection_ids)
        tickets = await asyncio.to_thread(self._tickets)
        running = set(getattr(self._built.loop, "running_subjects", ()) or ())
        asked: dict[str, tuple[str, dict[str, Any] | None]] = {}
        reclaimed = refused = 0
        for item in items:
            item_id = str(item["id"])
            if await gate(
                item,
                now=now,
                live=live,
                declined=declined or {},
                stale_after=self._stale_after,
                comments=self._reads.comments,
                running=running,
                # These were this runtime's: a recent touch is not somebody else's work.
                elsewhere=False,
            ):
                continue
            ticket = tickets.get(item_id)
            why = held_earlier(item, ticket, earlier, now=now)
            if why is None and earlier:
                why = taken_by_fallback(await self._reads.comments(item_id), earlier)
            if why is None:
                continue
            lease_holder = (live_reservation(item, now) or {}).get("holder")
            holder = lease_holder if isinstance(lease_holder, dict) else {}
            if (holder and str(holder.get("connection_id") or "") not in earlier) or live_job(item):
                log.debug("[sweep] %s is being worked by somebody else; not reclaiming it", item_id)
                continue
            answer, reason = await self._reads.reclaim(item_id)
            if ticket is not None and getattr(self._runner, "reclaim", None) is not None:
                from papaya_agent_runtime import serve

                mark = await asyncio.to_thread(serve._max_event_id)
                self._runner.reclaim(item_id, ticket.task_id, mark)
            status, holder = await self._offer(item)
            asked[item_id] = (status, holder)
            said = answer if not reason else f"{answer}: {reason}"
            if status == OFFER_PENDING:
                reclaimed += 1
                resume = f"; resuming ticket task {ticket.task_id}" if ticket is not None else ""
                self._say(f"reclaimed {_short(item)} ({why}; reclaim {said}){resume}")
                continue
            forget = getattr(self._runner, "forget_reclaim", None)
            if forget is not None:
                forget(item_id)
            if status == OFFER_BLOCKED:
                self._say(f"could not reclaim {_short(item)} yet ({why}): every slot is busy")
                continue
            refused += 1
            by = holder_name(holder) if holder is not None else "another session"
            self._say(f"could not reclaim {_short(item)} ({why}; reclaim {said}): {by} refused it")
        if reclaimed or refused or asked:
            self._say(
                f"reclaim on connect: {len(asked)} held by an earlier connection, "
                f"{reclaimed} reclaimed, {refused} refused"
            )
        return asked

    async def _claim_here(
        self, item: Mapping[str, Any], now: datetime, cache: dict[str, Any]
    ) -> str | None:
        """Why this runtime has a claim on ``item``, or ``None``.

        The same reading the reclaim uses (:func:`held_earlier`): a ticket task here
        that was not given away, a live lease held by one of this runtime's connections,
        or a `run on this Mac` naming one. ``cache`` holds the two reads for the sweep,
        so they happen once and only when something was refused.
        """
        if not cache:
            cache["earlier"] = await asyncio.to_thread(self._connection_ids)
            cache["tickets"] = await asyncio.to_thread(self._tickets)
        ticket = cache["tickets"].get(str(item.get("id") or ""))
        return held_earlier(item, ticket, cache["earlier"], now=now)

    async def _record_refusals(
        self, refused_now: dict[str, Refusal], reached: set[str], found: set[str]
    ) -> None:
        """Bring the idle-work blocker and the refusal streaks in line with this sweep.

        ``refused_now`` is the idle items Papaya refused this sweep, by work item id;
        ``reached`` the items this sweep got as far as asking about; ``found`` every open
        item. An item a full pool kept this sweep from reaching keeps its place in the
        blocker until a sweep reaches it.

        An item refused for the same reason on :data:`REFUSALS_BEFORE_DEFICIENCY` sweeps
        running is a `repeated-without-progress` occurrence: fingerprinted on the reason
        (one row, one issue, however many tickets), the ticket in the evidence, and each
        ticket recorded once a day however long its refusals go on. One line a person
        reads, not one occurrence per sweep (the 527-count row of 2026-09-17..19).

        Unless Papaya is only doing its job. A refusal whose reason is one of
        :data:`EXPECTED_REFUSALS` on work this runtime has no claim on is the routing
        working: it stays in the blocker and in `ppy workers` as kept elsewhere, and
        nothing is recorded. With a claim — a ticket task here that was not given away,
        a lease or a `run on this Mac` naming one of this runtime's connections — the
        same refusal is a deficiency, because the work was sent here and this machine
        cannot have it. A reason the runtime cannot explain is always a deficiency.
        """
        from papaya_agent_runtime import blockers, deficiencies

        previous = self._refused_idle
        kept_over = {
            item_id: name
            for item_id, name in (previous or {}).items()
            if item_id not in reached and item_id in found
        }
        current = {**kept_over, **{item_id: r.name for item_id, r in refused_now.items()}}
        for item_id in list(self._refusal_streak):
            if (item_id in reached and item_id not in refused_now) or item_id not in found:
                del self._refusal_streak[item_id]
        for item_id, refusal in refused_now.items():
            reason = refusal.reason
            last_reason, last_streak = self._refusal_streak.get(item_id, (reason, 0))
            streak = last_streak + 1 if last_reason == reason else 1
            self._refusal_streak[item_id] = (reason, streak)
            if streak < REFUSALS_BEFORE_DEFICIENCY:
                continue
            if reason in EXPECTED_REFUSALS and refusal.claim is None:
                log.debug(
                    "[sweep] %s has been refused (%s) %d times running, and this runtime has "
                    "no claim on it: Papaya keeps it elsewhere, which is not a deficiency",
                    item_id,
                    reason,
                    streak,
                )
                continue
            evidence = {"ticket": refusal.label, "code": reason, "times": streak}
            if refusal.claim:
                evidence["trigger"] = refusal.claim
            await asyncio.to_thread(
                functools.partial(
                    deficiencies.record_once,
                    deficiencies.REPEATED_WITHOUT_PROGRESS,
                    refused_detail(reason),
                    within=REPEAT_SAID_EVERY,
                    evidence=evidence,
                    scope=f"refusal:{reason}",
                    per="ticket",
                )
            )
        self._refused_idle = current
        if previous is not None and set(previous.values()) == set(current.values()):
            return
        changes = await asyncio.to_thread(
            functools.partial(blockers.set_idle_work_kept, sorted(current.values()))
        )
        if changes and self._publish is not None:
            with contextlib.suppress(Exception):
                self._publish()

    async def _sweep(self, *, include_declined: bool, include_kept: bool) -> SweepResult:
        from papaya_agent_client import api_client

        built = self._built
        self._watch_refusals(built.loop)
        try:
            answer = await api_client.list_assigned_work_items(built.api)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - an unreachable Papaya is a quiet round
            log.warning("[sweep] Could not list assigned work items: %s", exc)
            # Reaching Papaya again is a reconnect: take back what was held first.
            self._reclaim_due = True
            return SweepResult(error=str(exc) or exc.__class__.__name__)

        items = sweep_order([item for item in _items(answer) if is_open(item)])
        # The listing succeeded: a parked ticket not in it was closed or reassigned.
        await asyncio.to_thread(forget_parked_absent, {str(item["id"]) for item in items})
        live = await asyncio.to_thread(self._live_items)
        declined = {} if include_declined else await asyncio.to_thread(declined_items)
        kept = {} if include_kept else await asyncio.to_thread(kept_items)
        clock_now = self._clock()
        now = datetime.fromtimestamp(clock_now, UTC)
        asked: dict[str, tuple[str, dict[str, Any] | None]] = {}
        if self._reclaim_due:
            asked = await self._reclaim_earlier(items, live, now, declined)

        offered = skipped = earlier = elsewhere = waiting = parked = 0
        kept_by: dict[str, int] = {}
        idle_by: dict[str, list[int | None]] = {}
        newly_kept: dict[str, dict[str, Any]] = {}
        refused_idle: dict[str, Refusal] = {}
        # What a claim is read from, read once per sweep and only if something is refused.
        claims: dict[str, Any] = {}
        reached: set[str] = set()
        taken: list[str] = []
        for index, item in enumerate(items):
            item_id = str(item["id"])
            subject = f"work_item:{item_id}"
            reached.add(item_id)
            if item_id in asked and asked[item_id][0] == OFFER_PENDING:
                offered += 1
                taken.append(item_id)
                continue
            remembered = kept.get(item_id)
            # The one gate (`gate`), which the reclaim and a session's list of waiting
            # work call too. Work Papaya has refused here before is judged on evidence,
            # not on how recently somebody touched it.
            reason = await gate(
                item,
                now=now,
                live=set() if item_id in asked else live,
                declined=declined,
                stale_after=self._stale_after,
                comments=self._reads.comments,
                running=frozenset() if item_id in asked else built.loop.running_subjects,
                kept=kept,
            )
            if reason is not None:
                log.debug("[sweep] %s not offered: %s", subject, reason)
                skipped += 1
                elsewhere += reason == "in progress elsewhere"
                earlier += reason == "declined earlier, unchanged"
                parked += reason == WAITING_ON_A_PERSON
                continue
            evidence: Evidence | None = None
            if item_id not in asked and kept_elsewhere(item, remembered, now=clock_now):
                holder = (remembered or {}).get("holder") or {}
                name = holder_name(holder)
                evidence = await self._evidence(item, holder, now)
                if evidence.working:
                    log.debug(
                        "[sweep] %s is kept by %s and shows %s; not asking again yet",
                        subject,
                        name,
                        evidence.what,
                    )
                    skipped += 1
                    kept_by[name] = kept_by.get(name, 0) + 1
                    continue
                log.debug("[sweep] %s is kept by %s but idle; asking", subject, name)
            if item_id in asked:
                status, refused = asked[item_id]
            else:
                status, refused = await self._offer(item)
            if status == OFFER_PENDING:
                offered += 1
                taken.append(item_id)
            elif status == OFFER_BLOCKED:
                # Every slot is busy (or the reserve failed): nothing was taken,
                # and asking for the rest this round would only be refused again.
                log.debug("[sweep] %s not offered: no free slot; stopping this round", subject)
                reached.discard(item_id)
                waiting = len(items) - index
                break
            elif refused is not None:
                # Papaya keeps this work somewhere else: the agent in Papaya, another
                # person's machines, or another machine that keeps it.
                name = holder_name(refused)
                skipped += 1
                if evidence is None:
                    evidence = await self._evidence(item, refused, now)
                # Remembered either way. Idle work is still asked for every sweep (its
                # evidence is read before the memory is believed), and the memory is
                # what keeps a recently touched `in_progress` item from being skipped
                # as in progress elsewhere on the next one.
                newly_kept[item_id] = {
                    "updated_at": item.get("updated_at"),
                    "holder": refused,
                    "kept_at": now.isoformat(),
                }
                if evidence.working:
                    log.debug("[sweep] %s is kept by %s (%s)", subject, name, evidence.what)
                    kept_by[name] = kept_by.get(name, 0) + 1
                    continue
                # Refused, and nobody is doing it: asked for again every sweep, and
                # a person hears about it through the blocker.
                from papaya_agent_client.listener import refusal_skip_reason

                reason = refusal_skip_reason(refused) or "refused"
                log.debug("[sweep] %s is kept by %s and idle; Papaya refused it", subject, name)
                idle_by.setdefault(name, []).append(evidence.idle_minutes(now))
                refused_idle[item_id] = Refusal(
                    name=_short(item),
                    reason=reason,
                    label=ticket_label(item),
                    claim=await self._claim_here(item, now, claims),
                )
            else:
                # Held by another session, taken over in Papaya, or not this
                # playbook's to act on. Not ours this round.
                log.debug("[sweep] %s not taken: someone else has it or it is not ours", subject)
                skipped += 1
        if newly_kept:
            await asyncio.to_thread(remember_kept, newly_kept)
        if taken:
            await asyncio.to_thread(forget_kept, taken)
        await self._record_refusals(refused_idle, reached, {str(item["id"]) for item in items})
        return SweepResult(
            found=len(items),
            offered=offered,
            skipped=skipped,
            declined_earlier=earlier,
            in_progress_elsewhere=elsewhere,
            waiting_on_a_person=parked,
            kept=tuple(sorted(kept_by.items(), key=lambda pair: (-pair[1], pair[0]))),
            idle=tuple(
                sorted(
                    (
                        (name, len(ages), min((m for m in ages if m is not None), default=None))
                        for name, ages in idle_by.items()
                    ),
                    key=lambda entry: (-entry[1], entry[0]),
                )
            ),
            waiting=waiting,
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
        `ppy sweep --include-declined` and `--include-kept` are one flag on the
        wire, so `include_declined` sets aside both memories.
        """
        future = asyncio.run_coroutine_threadsafe(
            self.sweep_once(
                include_declined=include_declined, include_kept=include_declined, by_hand=True
            ),
            event_loop,
        )
        return future.result(timeout=timeout).as_dict()


__all__ = [
    "DEFAULT_STALE_AFTER",
    "DEFAULT_SWEEP_INTERVAL",
    "ENDED_PHASES",
    "EXPECTED_REFUSALS",
    "KEPT_RECHECK_EVERY",
    "OPEN_STATUSES",
    "PARKED",
    "PRIORITY_RANK",
    "REPEAT_SAID_EVERY",
    "WAITING_ON_A_PERSON",
    "ROUTE_HERE_HINT",
    "STATUS_RANK",
    "SWEEP_INTERVAL_ENV",
    "SWEEP_STALE_AFTER_ENV",
    "UNCHANGED_SUMMARY_EVERY",
    "Refusal",
    "SweepResult",
    "Sweeper",
    "declined_earlier",
    "declined_items",
    "declined_path",
    "envelope_for",
    "forget_declined",
    "forget_kept",
    "holder_name",
    "in_progress_elsewhere",
    "interval_from_env",
    "forget_parked_absent",
    "gate",
    "is_agent_comment",
    "is_open",
    "is_parked",
    "kept_elsewhere",
    "parked_items",
    "person_spoke_since",
    "refused_detail",
    "remember_parked",
    "ticket_label",
    "kept_items",
    "kept_path",
    "live_work_item_ids",
    "parse_interval",
    "remember_declined",
    "remember_kept",
    "stale_after_from_env",
    "sweep_order",
]
