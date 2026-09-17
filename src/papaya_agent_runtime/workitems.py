"""What changed on a work item while its work is going on, heard the same way in both modes.

A person adds a comment, rewrites the description or the acceptance criteria, or moves
who it is assigned to, and the worker building it should hear. `ppy serve` used to read comments
only for a ticket it held, and nothing read edits at all, so a worker could build
against a spec that had changed an hour ago. This module is the one decision:

- **Which items** (:func:`tracked`): every work item this runtime recorded a ticket for
  whose work is still going on: a worker not closed, or a delivered pull request not
  yet merged.
- **What changed** (:func:`detect`): comments by anyone but this agent after the newest
  one already handled (`serve.last_handled_comment`), and edits to the fields that shape
  a brief (:data:`SPEC_FIELDS`) against the last snapshot. The first look at an item
  records where listening starts and wakes nothing.
- **Who acts**: a held ticket's changes go to its answer turn (`serve.TicketRunner._listen`).
  Everything else (a ticket that ended, a delivered one) is checked by
  :func:`check_untracked`, which `ppy serve`'s rounds run every round and a session's
  heartbeat runs while no serve is running; each change is recorded on the ticket's task
  as :data:`CHANGE_EVENT` and is owed work until a manager steers the worker or says it
  was heard (`ppy heard <task>`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

#: The work item fields whose change can change what the worker should build. Status is
#: left out: the runtime sets it itself as the work moves, and would hear its own writes.
SPEC_FIELDS = ("title", "description", "acceptance_criteria", "priority", "assignee_id")

SNAPSHOT_EVENT = "work_item_seen"
CHANGE_EVENT = "work_item_changed"
HEARD_EVENT = "work_item_change_heard"

#: Worker statuses whose ticket's item is no longer being worked.
_FINISHED = ("closed", "cancelled")


@dataclass(frozen=True)
class Change:
    """One thing a person did on a work item since it was last heard."""

    kind: str  # "comment" or "edit"
    author: str
    text: str
    comment: dict[str, Any] | None = None

    def as_comment(self) -> dict[str, Any]:
        """The shape the answer turn reads comments in; an edit reads as a note from Papaya."""
        if self.comment is not None:
            return self.comment
        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:12]
        return {"id": f"edit:{digest}", "author_type": "system", "body": self.text}


@dataclass(frozen=True)
class Tracked:
    ticket_task_id: int
    run_id: int
    work_item_id: str
    subject: str
    metadata: dict[str, Any]
    workers: tuple[int, ...]


def tracked() -> list[Tracked]:
    """Every work item whose work is still going on, newest ticket per item."""
    from papaya_agent_runtime import papaya_events
    from papaya_agent_runtime.state import init_db

    conn = init_db()
    try:
        rows = conn.execute(
            "SELECT tasks.id, tasks.run_id, task_env.value AS metadata FROM tasks "
            "JOIN task_env ON task_env.task_id = tasks.id "
            "WHERE task_env.key = ? AND json_valid(task_env.value) ORDER BY tasks.id DESC",
            (papaya_events.PAPAYA_EVENT_METADATA,),
        ).fetchall()
        seen: set[str] = set()
        found = []
        for row in rows:
            metadata = json.loads(row["metadata"])
            item = str(metadata.get("work_item_id") or "")
            if not item or item in seen:
                continue
            seen.add(item)
            workers = conn.execute(
                "SELECT id, status, merged_sha FROM tasks WHERE run_id = ? AND phase IS NULL",
                (int(row["run_id"]),),
            ).fetchall()
            going = tuple(
                int(w["id"])
                for w in workers
                if w["status"] not in _FINISHED
                and not (w["status"] == "delivered" and w["merged_sha"])
            )
            if not going:
                continue
            found.append(
                Tracked(
                    int(row["id"]),
                    int(row["run_id"]),
                    item,
                    str(metadata.get("subject") or f"work_item:{item}"),
                    metadata,
                    going,
                )
            )
        return found
    finally:
        conn.close()


def _snapshot(item: dict[str, Any]) -> dict[str, str]:
    return {key: str(item.get(key) or "") for key in SPEC_FIELDS}


def _last(task_id: int, kind: str) -> dict[str, Any] | None:
    from papaya_agent_runtime.state import init_db

    conn = init_db()
    try:
        row = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
            (task_id, kind),
        ).fetchone()
    finally:
        conn.close()
    try:
        return json.loads(row["payload"]) if row is not None else None
    except (TypeError, ValueError):
        return None


def _record(task_id: int, kind: str, payload: dict[str, Any]) -> None:
    from papaya_agent_runtime.state import init_db, store

    conn = init_db()
    try:
        task = store.get_task(conn, task_id)
        store.append_event(
            conn,
            kind=kind,
            payload={"task_id": task_id, **payload},
            run_id=int(task["run_id"]) if task is not None else None,
            task_id=task_id,
        )
    finally:
        conn.close()


def _clip(text: str, width: int = 300) -> str:
    one = " ".join(text.split())
    return one if len(one) <= width else one[: width - 1] + "…"


def edits(ticket_task_id: int, item: dict[str, Any] | None) -> list[Change]:
    """The spec fields that changed since the last snapshot; records the new snapshot.

    The first snapshot of an item wakes nothing. ``None`` (the item could not be read)
    changes nothing.
    """
    if not isinstance(item, dict) or not item:
        return []
    now = _snapshot(item)
    before = (_last(ticket_task_id, SNAPSHOT_EVENT) or {}).get("fields")
    if before == now:
        return []
    _record(ticket_task_id, SNAPSHOT_EVENT, {"fields": now})
    if not isinstance(before, dict):
        return []
    found = []
    for key in SPEC_FIELDS:
        if before.get(key, "") != now[key]:
            found.append(
                Change(
                    "edit",
                    "Papaya",
                    f"The work item's {key.replace('_', ' ')} changed from "
                    f"“{_clip(before.get(key, ''), 140)}” to “{_clip(now[key])}”.",
                )
            )
    return found


def new_comments(
    ticket_task_id: int, comments: list[dict[str, Any]] | None, agent_id: str | None
) -> list[Change]:
    """Comments by anyone but this agent after the newest handled; records the newest.

    ``None`` comments (unreadable) change nothing; the first look records where
    listening starts and wakes nothing.
    """
    from papaya_agent_runtime import serve

    if comments is None:
        return []
    handled = serve.last_handled_comment(ticket_task_id)
    newer = serve.comments_after(comments, handled) if handled is not None else None
    serve.record_comment_handled(ticket_task_id, comments[-1] if comments else None)
    if newer is None:
        return []
    return [
        Change("comment", serve.comment_author(c), _clip(str(c.get("body") or "")), c)
        for c in newer
        if not serve.is_own_comment(c, agent_id)
    ]


def detect(
    ticket_task_id: int,
    item: dict[str, Any] | None,
    comments: list[dict[str, Any]] | None,
    agent_id: str | None,
) -> list[Change]:
    """Everything a person did on the item since it was last heard, oldest kind first."""
    return new_comments(ticket_task_id, comments, agent_id) + edits(ticket_task_id, item)


def check_untracked(
    *, env: dict[str, str], held: set[int] | None = None, agent_id: str | None = None
) -> list[str]:
    """Read every tracked item no held ticket is listening to; record what changed.

    Both modes call it: serve's rounds with the connection's environment, a session's
    heartbeat (while no serve runs) with :func:`papaya.agent_env`. Returns one line per
    item that changed. Never raises.
    """
    from papaya_agent_runtime import papaya_events

    lines: list[str] = []
    try:
        for entry in tracked():
            if held and entry.ticket_task_id in held:
                continue
            event = papaya_events.PapayaEvent(
                id=entry.metadata.get("id"),
                kind=str(entry.metadata.get("kind") or ""),
                subject=entry.subject,
                payload={},
                work_item_id=entry.work_item_id,
            )
            try:
                comments = papaya_events.list_work_item_comments(event, environ=env)
                hydrated = papaya_events.hydrate_work_item(event, environ=env)
            except papaya_events.PapayaEventError:
                continue
            item = hydrated.payload.get("work_item") if hydrated.payload else None
            changes = detect(entry.ticket_task_id, item, comments, agent_id)
            if not changes:
                continue
            _record(
                entry.ticket_task_id,
                CHANGE_EVENT,
                {
                    "work_item_id": entry.work_item_id,
                    "workers": list(entry.workers),
                    "changes": [
                        {"kind": c.kind, "author": c.author, "text": c.text} for c in changes
                    ],
                    "at": datetime.now(UTC).isoformat(),
                },
            )
            lines.append(
                f"work item {entry.work_item_id} (ticket task {entry.ticket_task_id}): "
                + "; ".join(f"{c.author}: {c.text}" for c in changes)
            )
    except Exception as exc:  # noqa: BLE001 - a round or a heartbeat never ends on this
        lines.append(f"could not read work item changes: {exc}")
    return lines


def unheard() -> list[dict[str, Any]]:
    """Recorded changes no manager has acted on: no steer on its workers, no `ppy heard`."""
    from papaya_agent_runtime.serve import ACTED_KINDS
    from papaya_agent_runtime.state import init_db

    conn = init_db()
    try:
        rows = conn.execute(
            "SELECT id, task_id, payload FROM events WHERE kind = ? ORDER BY id", (CHANGE_EVENT,)
        ).fetchall()
        newest: dict[int, tuple[int, dict[str, Any]]] = {}
        for row in rows:
            newest[int(row["task_id"])] = (int(row["id"]), json.loads(row["payload"]))
        found = []
        for ticket_task_id, (event_id, payload) in newest.items():
            heard = conn.execute(
                "SELECT 1 FROM events WHERE task_id = ? AND kind = ? AND id > ? LIMIT 1",
                (ticket_task_id, HEARD_EVENT, event_id),
            ).fetchone()
            if heard is not None:
                continue
            workers = [int(w) for w in payload.get("workers") or []]
            marks = ",".join("?" for _ in ACTED_KINDS)
            acted = (
                conn.execute(
                    f"SELECT 1 FROM events WHERE task_id IN ({','.join('?' for _ in workers)}) "
                    f"AND kind IN ({marks}) AND id > ? LIMIT 1",
                    (*workers, *ACTED_KINDS, event_id),
                ).fetchone()
                if workers
                else None
            )
            if acted is not None:
                continue
            found.append({"ticket_task_id": ticket_task_id, **payload})
        return found
    finally:
        conn.close()


def mark_heard(ticket_task_id: int, note: str) -> None:
    _record(ticket_task_id, HEARD_EVENT, {"note": note, "at": datetime.now(UTC).isoformat()})
