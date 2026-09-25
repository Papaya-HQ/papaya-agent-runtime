"""Where a machine task was asked, and the milestones said back there.

Papaya (backend #1077) gives every task it routes to a machine one return address.
`machine.instruction` and `work_item.assigned` carry it as a `machine_task` block:

    {"id": ..., "origin": {"kind", "ref", "url"?, "label"?} | null,
     "reply": {"method": "POST", "path": ".../machine-tasks/<id>/reply", ...}}

The origin is where a person asked for the work — a Papaya thread, their DM with
this agent, or an object in a connected tool — and the reply route reaches it
whatever it is. It takes four milestones (picked up, delivered, blocked, done), each
said once per task. Shane, 2026-09-25: work started with "@agent start working on
this" in a thread (PAP-319) was done, and every update landed only as work-item
comments; nothing came back to the thread.

This module is the one decision both modes reach: whether a ticket has an origin to
answer, and whether a milestone was already said there. A work item's block is kept
on its ticket task when the event arrives (:func:`remember`), because what offers a
ticket again later — the sweep, the rounds' reclaim — carries no block. A ticket
with no block, or a block with no origin, is never sent anything: Papaya would keep
such a reply as another comment on the item, beside the runtime's own.

Each milestone is recorded on the ticket task once Papaya answered it
(:data:`REPLIED`), keyed on the machine task's id, so a resumed or restarted hold
never says it twice; Papaya's own once-per-milestone rule is the second guard, not
the only one.

An instruction asked in a Papaya thread or DM is not answered here: its own reply
block already speaks in that conversation (`instructions.answer`). One asked from a
connected tool answers through the same route, via `papaya_events.post_instruction_reply`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.request
from collections.abc import Mapping
from typing import Any

from papaya_agent_runtime import papaya_events
from papaya_agent_runtime.state import db, store

log = logging.getLogger(__name__)

#: The ticket task's `machine_task` block, as the event carried it (JSON).
MACHINE_TASK = "papaya_machine_task"
#: A milestone Papaya answered for this ticket: `{machine_task, milestone, status, ...}`.
REPLIED = "machine_task_replied"
#: Refusals that will not change on a retry: the milestone is recorded as settled.
FINAL_CODES = (403, 404, 422)

PICKED_UP = papaya_events.MILESTONE_PICKED_UP
DELIVERED = papaya_events.MILESTONE_DELIVERED
BLOCKED = papaya_events.MILESTONE_BLOCKED
DONE = papaya_events.MILESTONE_DONE


def block_from(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """An event payload's `machine_task` block when it has an origin to answer, else ``None``."""
    block = payload.get("machine_task") if isinstance(payload, Mapping) else None
    if not isinstance(block, Mapping):
        return None
    origin, reply = block.get("origin"), block.get("reply")
    if not str(block.get("id") or "").strip():
        return None
    if not isinstance(origin, Mapping) or not str(origin.get("kind") or "").strip():
        return None
    if not isinstance(reply, Mapping) or not str(reply.get("path") or "").strip():
        return None
    return json.loads(json.dumps(block))


def remember(
    conn: sqlite3.Connection,
    task_id: int,
    payload: Mapping[str, Any] | None,
    work_item_id: str | None = None,
) -> bool:
    """Keep the event's block on its ticket task. An event without one changes nothing.

    A newer block replaces an older one: an item routed to this machine again is a
    new machine task, with its own milestones. A ticket task an offer made (after a
    hand-back, say: offers carry no block) takes the block an earlier ticket of the
    same ``work_item_id`` kept, so its milestones still reach where it was asked.
    """
    block = block_from(payload)
    if block is None:
        if not work_item_id or store.get_task_env(conn, task_id, MACHINE_TASK):
            return False
        row = conn.execute(
            """
            SELECT kept.value FROM task_env AS kept
            JOIN task_env AS event ON event.task_id = kept.task_id
            WHERE kept.key = ? AND event.key = ? AND kept.task_id != ?
              AND json_extract(event.value, '$.work_item_id') = ?
            ORDER BY kept.task_id DESC LIMIT 1
            """,
            (MACHINE_TASK, papaya_events.PAPAYA_EVENT_METADATA, task_id, str(work_item_id)),
        ).fetchone()
        if row is None:
            return False
        store.set_task_env(conn, task_id, MACHINE_TASK, str(row[0]), source="papaya_event")
        return True
    store.set_task_env(
        conn, task_id, MACHINE_TASK, json.dumps(block, sort_keys=True), source="papaya_event"
    )
    return True


def block_of(conn: sqlite3.Connection, task_id: int) -> dict[str, Any] | None:
    """The block kept on a ticket task, or ``None``."""
    raw = store.get_task_env(conn, task_id, MACHINE_TASK)
    if not raw:
        return None
    try:
        return block_from({"machine_task": json.loads(raw)})
    except ValueError:
        return None


def said_before(conn: sqlite3.Connection, machine_task: str, milestone: str) -> bool:
    """Was this milestone of this machine task already answered, by any of its tickets?"""
    rows = conn.execute(
        "SELECT payload FROM events WHERE kind = ? AND payload LIKE ?",
        (REPLIED, f"%{json.dumps(machine_task)}%"),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row[0])
        except ValueError:
            continue
        if payload.get("machine_task") == machine_task and payload.get("milestone") == milestone:
            return True
    return False


def _label(conn: sqlite3.Connection, task_id: int) -> str:
    return str(store.get_task_env(conn, task_id, papaya_events.WORK_ITEM_KEY) or "").strip()


def said(label: str, text: str) -> str:
    """The line for the person: the item's id in front, unless the line names it already."""
    line = " ".join(str(text or "").split())
    if label and label not in line:
        return f"{label}: {line}"
    return line


def send(
    task_id: int,
    milestone: str,
    text: str,
    *,
    environ: Mapping[str, str],
    opener=urllib.request.urlopen,
) -> bool:
    """Say ``milestone`` where the ticket's work was asked, once. Never raises.

    Returns whether Papaya answered it now. Nothing is sent for a ticket with no
    origin, for a milestone already answered, or with nothing to call with. A
    refusal that a retry cannot change (a 403, 404 or 422) is recorded as settled; a
    Papaya that could not be reached is logged, and the next time this milestone
    comes round it is tried again.
    """
    try:
        conn = db.init_db()
        try:
            block = block_of(conn, task_id)
            if block is None:
                return False
            machine_task = str(block["id"])
            if said_before(conn, machine_task, milestone):
                return False
            line = said(_label(conn, task_id), text)
        finally:
            conn.close()
        if not line:
            return False
        refused: papaya_events.PapayaHTTPError | None = None
        try:
            answer = papaya_events.post_machine_task_reply(
                block["reply"]["path"], line, milestone, environ=environ, opener=opener
            )
        except papaya_events.PapayaHTTPError as exc:
            if exc.code not in FINAL_CODES:
                raise
            refused, answer = exc, {"status": "refused", "reason": str(exc)}
        if answer is None:
            return False
        where = answer.get("delivered_to")
        payload = {
            "task_id": task_id,
            "machine_task": machine_task,
            "milestone": milestone,
            "status": str(answer.get("status") or ""),
            "delivered_to": str(where.get("kind") or "") if isinstance(where, Mapping) else "",
            "reason": str(answer.get("reason") or ""),
            "replayed": bool(answer.get("replayed")),
        }
        conn = db.init_db()
        try:
            store.append_event(conn, kind=REPLIED, payload=payload, task_id=task_id)
        finally:
            conn.close()
        if refused is not None:
            log.warning(
                "[machine-task] Papaya refused the %s reply for task %d: %s",
                milestone,
                task_id,
                refused,
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - the work goes on; the item has the comment
        log.warning(
            "[machine-task] Could not say %s where task %d was asked: %s", milestone, task_id, exc
        )
        return False


def send_as_agent(task_id: int, milestone: str, text: str) -> bool:
    """:func:`send` through this machine's own connection, from a session. Never raises."""
    from papaya_agent_runtime import papaya

    try:
        env = papaya.agent_env()
    except Exception as exc:  # noqa: BLE001 - not connected is nothing to say
        log.warning(
            "[machine-task] No connection to say %s for task %d: %s", milestone, task_id, exc
        )
        return False
    return send(task_id, milestone, text, environ=env)


__all__ = [
    "BLOCKED",
    "DELIVERED",
    "DONE",
    "FINAL_CODES",
    "MACHINE_TASK",
    "PICKED_UP",
    "REPLIED",
    "block_from",
    "block_of",
    "remember",
    "said",
    "said_before",
    "send",
    "send_as_agent",
]
