"""The lanes: everything the manager owes, decided once and acted on in both modes.

Supervision used to cover work items the manager currently **held**. Everything else
fell out of it for good: on 2026-09-17 a `ppy serve` restart handed three tickets over,
their workers lost every future turn with them, delivered pull requests went unposted,
and two finished workers sat unreviewed for a day while the runtime knew and nothing
acted (#72). The rule since (`docs/runtime-contract.md`, "Everything tracked gets acted
on"): whatever is being worked on, tracked, or assigned to the manager gets acted on,
held ticket or not, in `serve` and in an interactive session alike.

Three lanes, each one decision over `state.db`, on the same clock as the rounds:

- **owed** — every worker waiting on the manager with no live ticket
  (:func:`owed_decisions`): a stopped or done worker whose record says so is sent back
  to its gate (:data:`STEER`); a question or finished work gets a manager turn keyed on
  the task (:data:`TURN`); a decision only a person can make is recorded as a
  person-wait on the task (:data:`PERSON`), which `ppy status --team` lists.
- **ledger** — open, unblocked next steps that have sat past :data:`LEDGER_GRACE_SECONDS`
  (:func:`ledger_due`): a recorded next step is a queue item, not a diary entry, and gets
  executed or explicitly deferred with a reason.
- **deficiencies** — what the runtime recorded about itself is opened as issues
  (:func:`deficiency_step`), on the clock rather than only when the next record lands.

`ppy serve` acts through :class:`TurnRunner`: headless turns keyed on a task, with the
task's own facts and no hold (`rounds.Rounds._owed_lane`, `_ledger_lane`,
`_deficiency_lane`). An interactive session **is** the turn: the heartbeat lists the
same decisions and does the mechanical ones (:func:`interactive_step`), the Stop hook
refuses to let a turn end while a decision is the session's to act on
(:func:`stop_reasons`), and the session-start hook lists them (:func:`start_lines`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from papaya_agent_runtime import owed, papaya_events, prompts, review, supervision
from papaya_agent_runtime.state import db, store

log = logging.getLogger("papaya_agent_runtime.lanes")

#: What an owed worker gets: a manager turn keyed on it, a mechanical send-back, or a
#: person-wait recorded against it.
TURN = "turn"
STEER = "steer"
PERSON = "person"

#: The event kind, on a worker task, recording one task-keyed manager turn and how it ended.
TASK_TURN_EVENT = "task_turn"
#: The event kind, on no task, recording one ledger turn and the next steps it carried.
LEDGER_TURN_EVENT = "ledger_turn"

#: How a task turn ended: the worker was delivered, acted on (steered, answered,
#: resumed), the turn said it is waiting on something, or it did neither.
DELIVERED = "delivered"
ACTED = "acted"
WAITING = "waiting"
MISSED = "missed"

#: A turn that ends without doing its job is retried once with its tail; after that many
#: misses the task is a person's (the same count `serve.TicketRunner` keeps).
TURN_ATTEMPTS = 2

#: Gate send-backs the lane makes on one worker before its review turn decides instead
#: (the same count serve's runner keeps for a held ticket).
GATE_STEERS = 2

#: How long an owed task that a turn already handled without changing it waits before
#: the same turn is raised again with nothing new on the record.
OWED_RETRY_SECONDS = 60 * 60.0

#: How long an open, unblocked next step may sit before the ledger lane takes it up. A
#: session records a next step and usually does it within the turn; past this it is
#: a queue item.
LEDGER_GRACE_SECONDS = 30 * 60.0

#: A next step a ledger turn left untouched is not raised again for this long.
LEDGER_RETRY_SECONDS = 6 * 60 * 60.0

#: How often, at most, the deficiency lane opens issues.
DEFICIENCY_EVERY_SECONDS = 15 * 60.0

#: How many task turns `ppy serve` runs at once outside its held tickets.
TURNS_AT_ONCE = 2

#: The reason a task is blocked on a person after its turns gave up, as the todo says it.
_TURNS_GAVE_UP = "the manager turn ended {n} times without {job}; decide on it"
#: The reason a next step is blocked on a person after ledger turns left it twice.
_LEDGER_GAVE_UP = (
    "user:two manager turns left it untouched; do it, defer it with a reason, or drop it"
)


def _parse(stamp: object) -> datetime | None:
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _payload(row: Any) -> dict[str, Any]:
    try:
        value = json.loads(row["payload"])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _max_event_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()
    return int(row["m"])


def _newest_news(conn: sqlite3.Connection, task_id: int) -> int:
    """The newest event on the task that is not this lane's own record of a turn."""
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS m FROM events WHERE task_id = ? AND kind != ?",
        (task_id, TASK_TURN_EVENT),
    ).fetchone()
    return int(row["m"])


# ── the owed lane ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OwedDecision:
    """What one owed worker with no live ticket gets."""

    task_id: int
    status: str
    action: str
    #: Why, in one line: the owed reason, or the follow-up's line.
    line: str
    #: For :data:`TURN`: which turn (`prompts.REVIEW` or `prompts.ANSWER`).
    turn: str = ""
    #: For :data:`STEER`: the message; for :data:`PERSON`: what a person decides.
    message: str = ""
    repo: str | None = None
    #: The tail of the last turn's transcript, for a retry after a miss or a wait.
    tail: str = ""

    @property
    def failure(self) -> bool:
        """The worker stopped short: the review turn reads what stopped it, not a done note."""
        return self.status != "worker_done"

    def said(self) -> str:
        """The decision, in the words the heartbeat, the Stop hook and a round say it."""
        what = {
            TURN: f"needs the {self.turn} turn",
            STEER: "sent back to its gate",
            PERSON: "needs a person",
        }[self.action]
        where = f" ({self.repo})" if self.repo else ""
        return f"worker task {self.task_id}{where} {self.status}: {what}: {self.line}"


def _deferred(conn: sqlite3.Connection, task_id: int) -> bool:
    """An open todo blocked on someone or something names this task: it was deferred."""
    row = conn.execute(
        "SELECT 1 FROM todos WHERE task_id = ? AND status = 'open' AND blocked_on IS NOT NULL "
        "AND blocked_on != '' LIMIT 1",
        (task_id,),
    ).fetchone()
    return row is not None


def last_turn(conn: sqlite3.Connection, task_id: int) -> dict[str, Any] | None:
    """The newest task-turn record on a worker task, or ``None``."""
    row = conn.execute(
        "SELECT payload, created_at FROM events WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, TASK_TURN_EVENT),
    ).fetchone()
    if row is None:
        return None
    return {**_payload(row), "created_at": row["created_at"]}


def _steers_sent(conn: sqlite3.Connection, task_id: int) -> int:
    """Gate send-backs this lane made on the worker since it was last delivered or acted on."""
    rows = conn.execute(
        "SELECT kind, payload FROM events WHERE task_id = ? AND kind IN (?, 'delivered', "
        "'review_requested') ORDER BY id DESC",
        (task_id, TASK_TURN_EVENT),
    ).fetchall()
    count = 0
    for row in rows:
        if row["kind"] != TASK_TURN_EVENT:
            break
        payload = _payload(row)
        if payload.get("action") == STEER:
            count += 1
        elif payload.get("outcome") in (DELIVERED, ACTED):
            break
    return count


def _turn_due(conn: sqlite3.Connection, task_id: int, now: datetime) -> tuple[bool, str]:
    """Whether a turn is due on this task again, and the last transcript's tail if so."""
    from papaya_agent_runtime import serve

    record = last_turn(conn, task_id)
    if record is None or record.get("action") == STEER:
        return True, ""
    tail = str(record.get("tail") or "")
    at = _parse(record.get("created_at"))
    age = (now - at).total_seconds() if at is not None else OWED_RETRY_SECONDS
    outcome = record.get("outcome")
    if outcome == WAITING:
        waits = int(record.get("waits") or 1)
        return age >= serve.rerun_delay(waits, None), tail
    if outcome == MISSED:
        return int(record.get("misses") or 0) < TURN_ATTEMPTS, tail
    # Delivered or acted on, and owed again: only something new on the task since, or
    # the retry.
    if _newest_news(conn, task_id) > int(record.get("end_mark") or 0):
        return True, ""
    return age >= OWED_RETRY_SECONDS, ""


def owed_decisions(
    conn: sqlite3.Connection, *, now: datetime | None = None, covered: Iterable[int] = ()
) -> list[OwedDecision]:
    """Every worker waiting on the manager that no live ticket and no held loop covers.

    ``covered`` are the worker tasks a held ticket's runner acts on this round. A task
    with an open todo blocked on someone (a person, a review, another task) was
    explicitly deferred and is left alone. Reads only; :class:`TurnRunner` and the
    interactive surfaces act.
    """
    now = now or datetime.now(UTC)
    skip = set(covered)
    found: list[OwedDecision] = []
    for item in owed.collect(conn, now=now):
        if item.serve_owns or item.task_id in skip or item.status not in owed.OWED_STATUSES:
            continue
        if _deferred(conn, item.task_id):
            continue
        followup = item.followup
        if followup is not None and followup.action == supervision.PERSON:
            found.append(
                OwedDecision(
                    item.task_id, item.status, PERSON, followup.line, message=followup.line
                )
            )
            continue
        if (
            followup is not None
            and followup.action == supervision.STEER
            and _steers_sent(conn, item.task_id) < GATE_STEERS
        ):
            found.append(
                OwedDecision(
                    item.task_id,
                    item.status,
                    STEER,
                    followup.line,
                    message=followup.message,
                    repo=item.repo,
                )
            )
            continue
        due, tail = _turn_due(conn, item.task_id, now)
        if not due:
            continue
        turn = prompts.ANSWER if item.status == "blocked" else prompts.REVIEW
        found.append(
            OwedDecision(
                item.task_id, item.status, TURN, item.reason, turn=turn, repo=item.repo, tail=tail
            )
        )
    found += capability_decisions(conn, covered=skip, already={d.task_id for d in found})
    return found


def capability_decisions(
    conn: sqlite3.Connection, *, covered: Iterable[int] = (), already: Iterable[int] = ()
) -> list[OwedDecision]:
    """The answer turn for each live worker, with no ticket covering it, whose capability
    request the manager has not decided.

    A held ticket's worker gets it from the ticket's rounds (`Rounds._look_at`); this is
    every other worker (dispatched from a session, or its ticket ended). On 2026-09-18
    two `xcrun` requests from session-dispatched workers sat undecided for 25 minutes
    because only the ticket path asked. One turn per request: a task-turn recorded after
    the request means it was put to the manager already.
    """
    from papaya_agent_runtime import capability_requests

    skip = set(covered) | set(already)
    found: list[OwedDecision] = []
    for request in capability_requests.pending(conn):
        if request.task_id in skip:
            continue
        task = store.get_task(conn, request.task_id)
        if task is None or task["status"] not in owed.RUNNING_STATUSES:
            continue
        record = last_turn(conn, request.task_id) or {}
        if int(record.get("mark") or 0) >= request.id:
            continue
        asked = supervision.undecided_capability(request.task_id)
        if asked is None:
            continue
        found.append(
            OwedDecision(request.task_id, str(task["status"]), TURN, asked[1], turn=prompts.ANSWER)
        )
        skip.add(request.task_id)
    return found


def record_person_wait(task_id: int, text: str, *, conn: sqlite3.Connection | None = None) -> int:
    """A person-wait todo on the task: what `ppy status --team` lists under waiting on a person."""
    own = conn is None
    conn = conn or db.init_db()
    try:
        task = store.get_task(conn, task_id)
        return store.add_todo(
            conn,
            text,
            run_id=int(task["run_id"]) if task is not None else None,
            task_id=task_id,
            blocked_on="user",
        )
    finally:
        if own:
            conn.close()


def record_task_turn(task_id: int, **payload: Any) -> None:
    """Record, on the worker task, one lane action or turn and how it ended."""
    conn = db.init_db()
    try:
        task = store.get_task(conn, task_id)
        store.append_event(
            conn,
            kind=TASK_TURN_EVENT,
            payload={"task_id": task_id, **payload},
            run_id=int(task["run_id"]) if task is not None else None,
            task_id=task_id,
        )
    finally:
        conn.close()


def send_back(decision: OwedDecision, *, steer=None) -> str:
    """The mechanical send-back both modes make: the steer, recorded on the task."""
    (steer or supervision.steer_worker)(decision.task_id, decision.message)
    record_task_turn(decision.task_id, action=STEER, line=decision.line)
    return f"worker task {decision.task_id} sent back to its gate: {decision.line}"


def hand_to_person(decision: OwedDecision) -> str:
    """The person-wait both modes record for a decision only a person can make."""
    record_person_wait(decision.task_id, f"worker task {decision.task_id}: {decision.message}")
    record_task_turn(decision.task_id, action=PERSON, line=decision.line)
    return f"worker task {decision.task_id} needs a person: {decision.line}"


# ── the ledger lane ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LedgerItem:
    """One open, unblocked next step that has sat past the grace."""

    todo_id: int
    text: str
    run_id: int | None
    task_id: int | None
    updated_at: str
    seconds: float

    def said(self) -> str:
        refs = [f"run {self.run_id}" if self.run_id is not None else ""]
        refs.append(f"task {self.task_id}" if self.task_id is not None else "")
        where = ", ".join(r for r in refs if r)
        return f'todo #{self.todo_id} "{self.text}"' + (f" ({where})" if where else "")


def last_ledger_turn(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload, created_at FROM events WHERE kind = ? ORDER BY id DESC LIMIT 1",
        (LEDGER_TURN_EVENT,),
    ).fetchone()
    if row is None:
        return None
    return {**_payload(row), "created_at": row["created_at"]}


#: The event a released task wait leaves, for `ppy tail` and the tests.
WAIT_RELEASED_EVENT = "todo_wait_released"
#: The worker states that end what a `task:<id>` wait was waiting for: the task ended,
#: or its work came back to the manager. A worker that said done is not still working:
#: PAP-245's review step, recorded against its own worker and waiting on it, kept the
#: owed lane away from that finished worker for six hours (2026-09-18).
FINISHED_FOR_A_WAIT = (
    "delivered",
    "closed",
    "cancelled",
    "failed",
    "worker_done",
    "worker_stopped",
    "needs_recovery",
)
#: How long an `access` wait stands before the step is tried again: access that failed
#: once (a token refused mid-rotation) usually works on the next try, and nothing else
#: ever clears the wait.
ACCESS_RETRY_SECONDS = 60 * 60.0


def release_finished_waits(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[str]:
    """Open next steps waiting on a task that has since ended become due, and a step
    waiting on access is tried again after :data:`ACCESS_RETRY_SECONDS`. Never raises.

    Nothing else clears a `task:<id>` wait, and the moment the task ends is easy to miss
    (a restart, a delivery handled by another path): PAP-246's Calendar layer waited on
    its Gmail worker for good after that worker delivered (Shane, 2026-09-18). So every
    round and every heartbeat reconciles it from the record, not from the event. The
    ledger lane then takes the step up like any other.
    """
    lines: list[str] = []
    now = now or datetime.now(UTC)
    try:
        for row in conn.execute(
            "SELECT id, text, updated_at FROM todos WHERE status = 'open' "
            "AND (blocked_on = 'access' OR blocked_on LIKE 'access:%')"
        ).fetchall():
            since = _parse(row["updated_at"])
            if since is not None and (now - since).total_seconds() < ACCESS_RETRY_SECONDS:
                continue
            store.update_todo(conn, int(row["id"]), blocked_on=None)
            store.append_event(
                conn,
                kind=WAIT_RELEASED_EVENT,
                payload={"todo_id": int(row["id"]), "access": True},
            )
            lines.append(
                f"todo #{int(row['id'])} is tried again after waiting on access — {row['text']}"
            )
        rows = conn.execute(
            "SELECT id, text, blocked_on FROM todos WHERE status = 'open' "
            "AND blocked_on LIKE 'task:%'"
        ).fetchall()
        for row in rows:
            try:
                waited = int(str(row["blocked_on"]).split(":", 1)[1])
            except (IndexError, ValueError):
                continue
            task = store.get_task(conn, waited)
            status = str(task["status"]) if task is not None else "gone"
            if task is not None and status not in FINISHED_FOR_A_WAIT:
                continue
            store.update_todo(conn, int(row["id"]), blocked_on=None)
            store.append_event(
                conn,
                kind=WAIT_RELEASED_EVENT,
                payload={"todo_id": int(row["id"]), "task_id": waited, "status": status},
            )
            lines.append(
                f"todo #{int(row['id'])} no longer waits on task {waited} ({status}): "
                f"its next step is due — {row['text']}"
            )
        if lines:
            conn.commit()
            from papaya_agent_runtime import board

            board.write_board(conn)
    except Exception as exc:  # noqa: BLE001 - a round keeps going
        lines.append(f"could not release waits on finished tasks: {exc}")
    return lines


def ledger_due(
    conn: sqlite3.Connection, *, now: datetime | None = None, grace: float = LEDGER_GRACE_SECONDS
) -> list[LedgerItem]:
    """Open, unblocked next steps older than ``grace`` no recent ledger turn left as they are."""
    now = now or datetime.now(UTC)
    record = last_ledger_turn(conn) or {}
    left = record.get("left") if isinstance(record.get("left"), dict) else {}
    at = _parse(record.get("at") or record.get("created_at"))
    recent = at is not None and (now - at).total_seconds() < LEDGER_RETRY_SECONDS
    due: list[LedgerItem] = []
    for row in store.list_todos(conn, status="open"):
        if row["blocked_on"]:
            continue
        updated = _parse(row["updated_at"])
        seconds = (now - updated).total_seconds() if updated is not None else grace
        if seconds < grace:
            continue
        if recent and left.get(str(row["id"]), {}).get("updated_at") == row["updated_at"]:
            continue
        due.append(
            LedgerItem(
                int(row["id"]),
                str(row["text"]),
                row["run_id"],
                row["task_id"],
                str(row["updated_at"]),
                seconds,
            )
        )
    return due


def record_ledger_turn(
    items: list[LedgerItem], *, conn: sqlite3.Connection | None = None, now: datetime | None = None
) -> None:
    """After a ledger turn: what it left untouched, and block what two turns left.

    ``left`` maps each untouched todo to its ``updated_at`` and how many turns in a row
    left it; at :data:`TURN_ATTEMPTS` the todo is blocked on a person with the reason.
    """
    own = conn is None
    conn = conn or db.init_db()
    now = now or datetime.now(UTC)
    try:
        before = last_ledger_turn(conn) or {}
        misses = before.get("left") if isinstance(before.get("left"), dict) else {}
        left: dict[str, dict[str, Any]] = {}
        for item in items:
            row = store.get_todo(conn, item.todo_id)
            if row is None or row["status"] != "open" or row["blocked_on"]:
                continue
            if row["updated_at"] != item.updated_at:
                continue
            count = int(misses.get(str(item.todo_id), {}).get("misses") or 0) + 1
            if count >= TURN_ATTEMPTS:
                store.update_todo(conn, item.todo_id, blocked_on=_LEDGER_GAVE_UP)
                continue
            left[str(item.todo_id)] = {"updated_at": item.updated_at, "misses": count}
        store.append_event(
            conn,
            kind=LEDGER_TURN_EVENT,
            payload={"todos": [i.todo_id for i in items], "left": left, "at": now.isoformat()},
        )
    finally:
        if own:
            conn.close()


def ledger_facts(items: list[LedgerItem]) -> dict[str, object]:
    """What the ledger turn is given: the next steps, oldest first, with their ids."""
    from papaya_agent_runtime import health

    lines = [
        f"- todo #{i.todo_id} (waiting {health.humanize(int(i.seconds))}): {i.said()}"
        for i in items
    ]
    return {"the next steps that have sat in the ledger, oldest first": "\n".join(lines)}


# ── the deficiency lane ─────────────────────────────────────────────────────


def deficiency_step(reporter: Any = None) -> list[str]:
    """Open what the runtime recorded about itself as issues, on the clock. Never raises.

    `ppy serve` flushes when a record lands; a record that waited on the daily cap, or
    landed while nothing was running, otherwise waited for the next one. Both modes call
    this on their clock: serve's rounds and the session heartbeat's upkeep.
    """
    from papaya_agent_runtime import deficiencies

    try:
        return list((reporter or deficiencies.Reporter()).flush())
    except Exception as exc:  # noqa: BLE001 - reporting never ends a round or a tick
        return [f"could not open deficiencies as issues: {exc}"]


# ── what an interactive session does with the same decisions ───────────────


def interactive_step(
    conn: sqlite3.Connection, now: datetime, *, steer=None
) -> tuple[list[str], list[OwedDecision]]:
    """The owed lane from a session's heartbeat: act mechanically, list what needs a turn.

    Serve's rounds run the same decisions. A session runs them only while no serve does,
    so the two never send one worker back twice. Returns the lines of what it did and the
    decisions the session itself is the turn for.
    """
    if supervision.serve_running():
        return [], []
    lines: list[str] = release_finished_waits(conn, now=now)
    turns: list[OwedDecision] = []
    try:
        for decision in owed_decisions(conn, now=now):
            if decision.action == STEER:
                try:
                    lines.append(send_back(decision, steer=steer))
                except Exception as exc:  # noqa: BLE001 - a refused steer is the turn's
                    lines.append(f"could not send worker task {decision.task_id} back: {exc}")
            elif decision.action == PERSON:
                lines.append(hand_to_person(decision))
            else:
                turns.append(decision)
    except Exception as exc:  # noqa: BLE001 - the heartbeat keeps ticking
        lines.append(f"could not read what is owed: {exc}")
    return lines, turns


def stop_reasons(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[str]:
    """Why an interactive turn may not end: decisions that are this session's to act on.

    The same owed and ledger lanes serve runs turns for; here the session is the turn.
    """
    now = now or datetime.now(UTC)
    reasons: list[str] = []
    turns = [d for d in owed_decisions(conn, now=now) if d.action == TURN]
    if turns:
        listed = "\n".join(f"- {d.said()}" for d in turns)
        reasons.append(
            f"{len(turns)} worker task(s) are waiting on a turn only you can take here (review "
            "and deliver, or answer). Take each up now, or defer it with a reason: "
            '`ppy todo add --task <id> --blocked-on user|review|task:<id> "..."`.\n' + listed
        )
    due = ledger_due(conn, now=now)
    if due:
        listed = "\n".join(f"- {item.said()}" for item in due)
        minutes = int(LEDGER_GRACE_SECONDS // 60)
        reasons.append(
            f"{len(due)} next step(s) have sat in your ledger for over {minutes} minutes. A "
            "recorded next step is a queue item: do each one now, defer it with a reason "
            "(`ppy todo block <id> --on user:<why>|task:<id>|review`), or drop it "
            "(`ppy todo drop <id>`).\n" + listed
        )
    return reasons


def start_lines(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[str]:
    """What a starting session is told: the turns it owes and the next steps that sat."""
    now = now or datetime.now(UTC)
    lines = [d.said() for d in owed_decisions(conn, now=now)]
    lines += [
        f"{item.said()} has waited {int(item.seconds // 60)}m" for item in ledger_due(conn, now=now)
    ]
    return lines


# ── how `ppy serve` acts: headless turns keyed on a task ────────────────────


def task_facts(task_id: int, decision: OwedDecision | None = None) -> dict[str, object]:
    """The facts a task-keyed turn is given: the worker, its record, and where results go."""
    from papaya_agent_runtime import rounds

    conn = db.init_db()
    try:
        task = store.get_task(conn, task_id)
        if task is None:
            return {}
        repo = None
        if task["repo_id"] is not None:
            row = conn.execute("SELECT name FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
            repo = str(row["name"]) if row else None
        rows = conn.execute(
            f"SELECT created_at, payload FROM events WHERE task_id = ? AND {store.PROGRESS_NOTE} "
            "ORDER BY id",
            (task_id,),
        ).fetchall()
        log_lines = []
        for r in rows:
            note = _payload(r)
            log_lines.append(
                f"{r['created_at']} [{note.get('phase')}] {note.get('note') or ''}".rstrip()
            )
        question = None
        if task["status"] == "blocked":
            newest = conn.execute(
                "SELECT payload FROM events WHERE task_id = ? AND kind IN ('question', 'blocked') "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            question = _payload(newest).get("question") if newest is not None else None
    finally:
        conn.close()
    ticket = rounds.ticket_for_worker(task_id)
    facts: dict[str, object] = {
        "held work item": (
            "none: this worker has no live Papaya ticket (it was dispatched from a session, or "
            "its ticket ended); report results on the tracker record below if there is one"
        ),
        "tracked as (work item id)": ticket.work_item_id if ticket is not None else None,
        "ticket task id": ticket.task_id if ticket is not None else None,
        "run id": int(task["run_id"]),
        "worker task id": task_id,
        "worker status": task["status"],
        "repository": repo,
        "branch": task["branch"],
        "record a wait on a person against (`ppy todo add --task`)": task_id,
    }
    if decision is not None and decision.turn == prompts.ANSWER:
        facts["the worker's question"] = question or decision.line
    elif decision is not None:
        facts["what stopped the worker"] = decision.line if decision.failure else ""
        facts["the worker's recorded gate at its head"] = _gate_line(task_id)
    facts["the worker's full progress log, oldest first"] = (
        "\n".join(log_lines) or "(no progress reported yet)"
    )
    if decision is not None and decision.turn == prompts.REVIEW:
        facts["the worker's commits to review (what `ppy review show` diffs)"] = (
            review.review_base_line(task_id)
        )
    return facts


def _gate_line(task_id: int) -> str:
    from papaya_agent_runtime import gate

    try:
        recorded = gate.verdict(task_id)
    except Exception:  # noqa: BLE001 - an unreadable record is no record
        return ""
    return recorded.result.line() if recorded.result is not None else ""


class TurnRunner:
    """How `ppy serve` acts on the owed and ledger lanes: headless turns keyed on a task.

    The same launcher `serve.TicketRunner` uses, without a hold: the facts are the
    task's own, the transcript lands under the task's run, and the Papaya tools come
    from this machine's connection when there is one. Every collaborator with an
    outside world is a keyword seam, the runner's own: ``run_turn``, ``turn_tools``,
    ``config``, ``papaya_env``, ``steer``, ``agent_kind``.
    """

    def __init__(
        self,
        *,
        run_turn=None,
        turn_tools=None,
        config=None,
        runtime_dir: str | None = None,
        papaya_env: Callable[[], dict[str, str]] | None = None,
        steer=None,
        agent_kind=None,
        clock: Callable[[], datetime] | None = None,
        at_once: int = TURNS_AT_ONCE,
    ) -> None:
        self._run_turn = run_turn
        self._turn_tools = turn_tools
        self._config = config
        self._runtime_dir = runtime_dir
        self._papaya_env = papaya_env or (lambda: {})
        self._steer = steer or supervision.steer_worker
        self._agent_kind = agent_kind
        self._clock = clock or (lambda: datetime.now(UTC))
        self._at_once = int(at_once)
        self._stopping = threading.Event()
        #: Worker task id -> the turn running on it.
        self.running: dict[int, asyncio.Task[Any]] = {}
        #: The ledger turn, while one runs.
        self._ledger: asyncio.Task[Any] | None = None
        #: Every turn started, for tests and the record: `(task id or None, turn)`.
        self.started: list[tuple[int | None, str]] = []

    def should_stop(self) -> bool:
        return self._stopping.is_set()

    async def close(self) -> None:
        """Stop every running turn and wait for it: the harness is told to end."""
        self._stopping.set()
        pending = [*self.running.values(), *([self._ledger] if self._ledger else [])]
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def busy(self) -> int:
        return len(self.running)

    async def take_up(self, decisions: list[OwedDecision]) -> list[str]:
        """Act on the owed lane's decisions: send back, hand to a person, or start a turn."""
        lines: list[str] = []
        for decision in decisions:
            if decision.action == STEER:
                try:
                    lines.append(await asyncio.to_thread(send_back, decision, steer=self._steer))
                except Exception as exc:  # noqa: BLE001 - a refused steer is the turn's
                    lines.append(f"could not send worker task {decision.task_id} back: {exc}")
                continue
            if decision.action == PERSON:
                lines.append(await asyncio.to_thread(hand_to_person, decision))
                continue
            if decision.task_id in self.running:
                continue
            if self.busy() >= self._at_once:
                continue  # next round, when a slot is free
            self.running[decision.task_id] = asyncio.create_task(self._task_turn(decision))
            lines.append(f"worker task {decision.task_id}: running the {decision.turn} turn")
        return lines

    async def take_up_ledger(self, items: list[LedgerItem]) -> list[str]:
        """Start the ledger turn for the next steps that sat, unless one is running."""
        if not items or (self._ledger is not None and not self._ledger.done()):
            return []
        self._ledger = asyncio.create_task(self._ledger_turn(items))
        return [f"running the ledger turn for {len(items)} next step(s)"]

    async def _task_turn(self, decision: OwedDecision) -> None:
        from papaya_agent_runtime import serve

        task_id = decision.task_id
        try:
            facts = await asyncio.to_thread(task_facts, task_id, decision)
            if decision.tail:
                facts["previous attempt's transcript (tail)"] = decision.tail
            run_id = int(facts.get("run id") or 0)
            mark = await asyncio.to_thread(serve._max_event_id)
            result = await self._turn(decision.turn, facts, run_id=run_id, task_id=task_id)
            end_mark = await asyncio.to_thread(serve._max_event_id)
            record = await asyncio.to_thread(last_turn_record, task_id)
            if await asyncio.to_thread(serve.delivered_since, task_id, mark):
                outcome = DELIVERED
            elif await asyncio.to_thread(serve.acted_since, task_id, mark) or (
                decision.turn == prompts.ANSWER
                and await asyncio.to_thread(serve.worker_status, task_id) != "blocked"
            ):
                outcome = ACTED
            elif serve.waiting_reason(result) is not None:
                outcome = WAITING
            else:
                outcome = MISSED
            waits = int(record.get("waits") or 0) + 1 if outcome == WAITING else 0
            misses = int(record.get("misses") or 0) + 1 if outcome == MISSED else 0
            tail = result.tail() if hasattr(result, "tail") else str(result or "")
            await asyncio.to_thread(
                record_task_turn,
                task_id,
                action=TURN,
                turn=decision.turn,
                outcome=outcome,
                mark=mark,
                end_mark=end_mark,
                waits=waits,
                misses=misses,
                tail=tail[-2000:] if outcome in (WAITING, MISSED) else "",
                exit_code=getattr(result, "exit_code", None),
            )
            if outcome == MISSED and misses >= TURN_ATTEMPTS:
                job = (
                    "reviewing and delivering, or steering"
                    if decision.turn == prompts.REVIEW
                    else "answering or steering"
                )
                await asyncio.to_thread(
                    record_person_wait,
                    task_id,
                    f"worker task {task_id}: " + _TURNS_GAVE_UP.format(n=misses, job=job),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad turn must not end the lane
            log.exception("[lanes] The %s turn on task %d failed: %s", decision.turn, task_id, exc)
        finally:
            self.running.pop(task_id, None)

    async def _ledger_turn(self, items: list[LedgerItem]) -> None:
        try:
            await self._turn(prompts.LEDGER, ledger_facts(items), run_id=None, task_id=None)
            await asyncio.to_thread(record_ledger_turn, items, now=self._clock())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad turn must not end the lane
            log.exception("[lanes] The ledger turn failed: %s", exc)

    def _root(self) -> str:
        from papaya_agent_runtime.manager.launch import repo_root

        return str(Path(self._runtime_dir or repo_root()).resolve())

    def _environment(self, run_id: int | None) -> dict[str, str]:
        from papaya_agent_runtime import serve

        root = self._root()
        env = {**os.environ, **{k: v for k, v in self._papaya_env().items() if v}}
        try:
            env = {**env, **serve.turn_environment(env, root=root, run_id=run_id or 0)}
        except Exception as exc:  # noqa: BLE001 - the client's bounds are a help, not a gate
            log.debug("[lanes] Could not bound the turn's environment: %s", exc)
        if run_id is None:
            env.pop(papaya_events.TICKET_RUN_ENV, None)
        return env

    async def _turn(
        self, turn: str, facts: dict[str, object], *, run_id: int | None, task_id: int | None
    ) -> Any:
        from papaya_agent_runtime import serve
        from papaya_agent_runtime.manager.launch import (
            ManagerLaunchError,
            TurnResult,
            build_launch,
            prepare_turn_tools,
            resolve_profile,
            run_turn,
        )

        root = self._root()
        env = self._environment(run_id)
        connected = bool(env.get("PAPAYA_AGENT_TOKEN"))
        if connected and self._agent_kind is not None:
            kind = await asyncio.to_thread(self._agent_kind, env)
            if kind is not None:
                facts = {**facts, **kind.facts()}
        prompt = prompts.render(turn, runtime_dir=root, facts=facts)
        transcript = serve.turn_transcript_path(run_id or 0, turn)
        self.started.append((task_id, turn))
        try:
            config = self._config() if self._config is not None else serve._load_config()
            provider, _model, _reasoning = resolve_profile(config, None, None, None)
            tools = None
            if connected:
                prepare = self._turn_tools or prepare_turn_tools
                tools = await asyncio.to_thread(
                    prepare,
                    provider,
                    env,
                    root=root,
                    config_file=transcript.with_suffix(".mcp.json"),
                )
            launch = build_launch(config=config, turn=prompt, root=root, base_env=env, tools=tools)
        except ManagerLaunchError as exc:
            log.error("[lanes] Could not launch the %s turn: %s", turn, exc)
            return TurnResult(exit_code=127, transcript=f"the turn could not be launched: {exc}")
        runner = self._run_turn or run_turn
        result = await asyncio.to_thread(
            runner, launch, should_stop=self.should_stop, transcript_path=transcript
        )
        said = serve.runtime_report(result)
        if said is not None:
            from papaya_agent_runtime import deficiencies

            await asyncio.to_thread(
                deficiencies.record,
                deficiencies.TURN_REPORT,
                said,
                evidence={
                    "turn": turn,
                    "task_id": task_id,
                    "run_id": run_id,
                    "transcript": str(transcript),
                },
            )
        return result


def last_turn_record(task_id: int) -> dict[str, Any]:
    conn = db.init_db()
    try:
        return last_turn(conn, task_id) or {}
    finally:
        conn.close()


__all__ = [
    "ACTED",
    "DEFICIENCY_EVERY_SECONDS",
    "DELIVERED",
    "GATE_STEERS",
    "LEDGER_GRACE_SECONDS",
    "LEDGER_RETRY_SECONDS",
    "LEDGER_TURN_EVENT",
    "MISSED",
    "OWED_RETRY_SECONDS",
    "PERSON",
    "STEER",
    "TASK_TURN_EVENT",
    "TURN",
    "TURNS_AT_ONCE",
    "TURN_ATTEMPTS",
    "WAITING",
    "LedgerItem",
    "OwedDecision",
    "TurnRunner",
    "deficiency_step",
    "hand_to_person",
    "interactive_step",
    "ledger_due",
    "ledger_facts",
    "owed_decisions",
    "record_ledger_turn",
    "record_person_wait",
    "record_task_turn",
    "send_back",
    "start_lines",
    "stop_reasons",
    "task_facts",
]
