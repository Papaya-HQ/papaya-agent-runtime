"""The manager's rounds: `ppy serve` looks at every worker and every loose end on a clock.

The runner reacts: a worker's question starts the answer turn, `worker_done` starts
the review. Nothing it does happens *because time passed*, so a worker that goes
silent, wanders off the brief, or waits on a person nobody told stays that way until
somebody at a terminal runs `ppy health`. The rounds are that somebody. Every
`rounds_interval` (five minutes; `--rounds-interval`, `$PPY_ROUNDS_INTERVAL`, or
`health.rounds_interval`) a round walks the board, in this order:

1. **Pull requests.** For each delivered worker whose ticket is not held: a merged
   pull request is followed up once (`supervision.merged_step`): its work item moves
   to the status this workspace said (`delivery.merged_status`), or, with no rule yet,
   one comment asks which, and its worktree is cleaned up at once. Otherwise its
   reasons (:mod:`papaya_agent_runtime.reconcile`:
   conflicts, a branch behind a base that requires up-to-date branches, red CI, CI
   pending past the repository's CI budget, a review asking for changes, reviewer
   threads and comments since the last push) and its head make a fingerprint. A new
   fingerprint is queued for the reserved reconcile lane and started while the lane has
   room, closest to merging first: recorded on the worker as `pr_attention` and the
   ticket back to `dispatched`, so step 2 takes it up and the review turn steers the
   worker with the facts attached. The same fingerprint is not raised again unless the
   lane's attempt failed; two failures mark the ticket `needs_a_person` with one
   comment. A pull request green and unmerged past `delivery.merge_after_hours` is said
   once, or merged on a repository with `auto_merge`.
2. **Reclaim.** Every ticket this runtime was working and is not holding — after a
   restart, all of them — is offered to the client's loop again, under the
   persisted session id, and the runner resumes it on its own task from its
   recorded phase; a worker already running is watched, never re-dispatched. A
   ticket whose hold the client ended as `stalled` while its worker's work went on
   (live, done, or stopped ahead of base) counts as working
   (:func:`serve.stalled_resume_phase`). A
   ticket Papaya says somebody else holds is closed here as `handed_over`, its
   branch kept and nothing posted. On the first round of a process, tickets
   declined because a turn "ended without" doing its job (PAP-213) are offered
   once more, with the earlier worker's branch in the brief's facts.
3. **Every runner row with no process behind it**, whatever its ticket's state, is
   closed (:mod:`papaya_agent_runtime.supervisor.dead_runners`): its slot comes back
   and a task still in flight is `worker_stopped` with its session kept. Workers of
   held tickets are left to the next step, which says it on the ticket.
   **Every held ticket's worker** (facts from :mod:`papaya_agent_runtime.health`):
   a dead session with no done note is recorded as `worker_stopped`, which the
   runner's existing path sends back to its gate; a question (status `blocked`, or
   a last progress note that asks one) gets the answer turn; a `worker_stopped`
   worker with a branch ahead of base and nothing in flight gets the stopped-short
   path; a live worker silent past its repository's silence budget, past its plan
   budget with no plan note and no tool call for `health.plan_idle_minutes` (or still
   planning past three plan budgets, whatever it is doing), first running for half its
   worker-session budget (:mod:`papaya_agent_runtime.budgets`; with no history,
   `health.quiet_minutes`, `health.plan_minutes` and `health.checkin_after`), or with a
   HEAD the forge does not have and no new tip there for `health.push_by_minutes`
   (asked with `ls-remote` every round; again every as many minutes until something
   lands) gets the **check-in turn**; a person-wait older than fifteen minutes is said
   on the ticket once ("waiting on you: …") and the ticket is `blocked`.
4. **The owed lane** (:mod:`papaya_agent_runtime.lanes`): every worker waiting on the
   manager that no live ticket covers — dispatched from a session, or its ticket was
   released, handed over or ended — is taken up the way a held ticket's would be: a
   stopped or done worker whose record calls for it is sent back to its gate, a
   question gets the answer turn and finished work the review turn, keyed on the task
   and run without a hold (:class:`~papaya_agent_runtime.lanes.TurnRunner`), and a
   decision only a person can make is recorded against the task as a person-wait.
5. **The ledger lane**: open, unblocked next steps that sat past
   `lanes.LEDGER_GRACE_SECONDS` get the ledger turn, which does each, defers it with a
   reason, or drops it.
6. **The deficiency lane**, every `lanes.DEFICIENCY_EVERY_SECONDS`: what the runtime
   recorded about itself is opened as issues, on the clock rather than only when the
   next record lands.
7. **Hygiene**, at most once an hour: `ppy worktree prune`'s own rules, unattended
   (only terminal tasks, clean, every commit on a remote, base clone under
   `.ppy/repos`), then `git worktree prune` and `git fetch --prune` on the base
   clones. A kept slot that is a loose end — terminal, dirty or unpushed, a day old
   — becomes one "waiting on you" item.
8. **Instructions and status**: an instruction a person sent this machine that was
   answered at its origin and never reported (a crash between the two) is reported
   (:func:`instructions.recover`); one a restart left unanswered is offered back, or,
   refused, told to the person as not finished and closed
   (:meth:`Rounds._reclaim_instructions`); and this machine's status snapshot is published to
   Papaya (:mod:`papaya_agent_runtime.machine_status`) — once a round, and between
   rounds whenever the board changes (:meth:`Rounds.watch_changes`).

Step 1 covers every delivered pull request, live ticket or not: one a live ticket owns
goes through that ticket's review turn; every other one (no ticket, or a ticket that
ended) goes through the same repair step a session's heartbeat runs
(`supervision.repair_untracked`), and a merged one is recorded merged and cleaned up
whether or not a ticket ever existed for it.

The rule the whole module keeps: **the round is the clock and the facts; judgment
stays in turns.** A round never writes a message for a worker. It queues a
:class:`~papaya_agent_runtime.serve.Nudge` on the held ticket, and the ticket's own
loop runs the turn, so a round's turn never overlaps the ticket's others and every
"continue", "steer", "stop" and "answer" comes from a turn reading the record. A
worker with no ticket has no loop of its own, so its turns run beside the rounds under
the turn runner, at most `lanes.TURNS_AT_ONCE` at a time and never two on one task.

A round holds the lock the sweep holds, so the two never overlap. It says one
progress line per ticket whose state it changed and one stderr summary; a round
that found nothing writes nothing at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from papaya_agent_runtime import (
    deficiencies,
    health,
    instructions,
    lanes,
    machine_status,
    outreach,
    papaya_events,
    serve,
    supervision,
    sweep,
    workitems,
)
from papaya_agent_runtime.state import db, store

log = logging.getLogger("papaya_agent_runtime.rounds")

#: How often `serve` does its rounds when nobody says otherwise: five minutes.
DEFAULT_ROUNDS_INTERVAL = 300.0

#: The environment variable that sets the interval when `--rounds-interval` does not.
ROUNDS_INTERVAL_ENV = "PPY_ROUNDS_INTERVAL"

#: How long a question may wait on a person before the ticket says so.
PERSON_WAIT_SECONDS = 15 * 60.0

#: How long a worker may run with nothing new on its remote branch: `health.push_by_minutes`.
DEFAULT_PUSH_BY_MINUTES = 45

#: How long a worker past its plan budget with no plan note may go without a tool call
#: before the plan check-in fires: `health.plan_idle_minutes`.
DEFAULT_PLAN_IDLE_MINUTES = 5

#: Past this many plan budgets, a worker still planning gets the plan check-in whatever
#: it is doing.
PLAN_HARD_FACTOR = supervision.PLAN_HARD_FACTOR

#: Events that start a worker session. A check-in measures the session in front of
#: it from the newest of these, not from when the task was first created.
SESSION_START_KINDS = ("dispatched", "resumed")

#: How long a worker with no live session must have been silent before it is dead
#: rather than between a runner exiting and its result being recorded.
DEAD_GRACE_SECONDS = 2 * 60.0

#: How often, at most, a round cleans up worktrees.
HYGIENE_EVERY_SECONDS = 60 * 60.0

#: How old a kept, terminal, dirty or unpushed slot must be to be a person's loose end.
KEPT_LOOSE_END_SECONDS = 24 * 60 * 60.0

#: The hygiene run on which a slot kept every time is worth a summary line.
KEPT_SUMMARY_RUNS = 3

#: The event kind, on a ticket's task, that records what a round did once.
ROUND_EVENT = "ticket_round"

#: The round action that records a new tip of a worker's branch on the forge, and when
#: a round first saw it: the runtime's record of a push, which the push check-in reads.
PUSH_SEEN = "push_seen"

#: The event kind every hygiene run records: what went, what stayed, and why.
HYGIENE_EVENT = "worktree_hygiene"

#: The hand-back reason the runner gives when a turn missed its job (PAP-213).
_TURN_MISSED = "the manager turn ended"

#: The event a ticket task gets when a missed-turn re-offer finds its work item is no
#: longer this agent's to run, with the reason; its phase becomes `done` in the same step.
TICKET_CLOSED = "ticket_closed"
#: Papaya has the item done or cancelled.
CLOSED_ITEM_CLOSED = "item_closed"
#: Papaya has the item owned by somebody other than the agent this runtime is connected as.
CLOSED_NOT_AGENTS_ITEM = "not_agents_item"
#: The work item statuses that end it in Papaya.
_ITEM_ENDED = (papaya_events.STATUS_DONE, "cancelled")

#: How long one `git fetch --prune` may take before hygiene moves on.
FETCH_TIMEOUT_SECONDS = 120.0


def interval_from_env(environ: Mapping[str, str] | None = None) -> float:
    """The rounds interval: `$PPY_ROUNDS_INTERVAL`, else `health.rounds_interval`, else 300s.

    Raises ``ValueError`` for an environment value that is not a non-negative
    number, so `serve` reports it like a bad flag.
    """
    env = os.environ if environ is None else environ
    raw = str(env.get(ROUNDS_INTERVAL_ENV) or "").strip()
    if raw:
        return sweep.parse_interval(raw, source=ROUNDS_INTERVAL_ENV)
    try:
        from papaya_agent_runtime.config import load_config

        return float(load_config().health.rounds_interval)
    except Exception:  # noqa: BLE001 - no config yet is the default
        return DEFAULT_ROUNDS_INTERVAL


@dataclass(frozen=True)
class WorkerBudgets:
    """How long a round lets a worker in one repository go before it checks in."""

    #: Silent this long with a live session is `quiet`: the repository's silence budget.
    quiet_seconds: float
    #: Still planning this long after dispatch: the repository's plan budget.
    plan_seconds: float
    #: Running this long gets the midpoint check-in: half its worker-session budget.
    midpoint_seconds: float
    #: Which of the three history stands behind, for the reason a check-in gives.
    sources: tuple[str, str, str] = ("default", "default", "default")
    #: Past the plan budget with no plan note, this long with no tool call is stuck:
    #: `health.plan_idle_minutes`.
    plan_idle_seconds: float = DEFAULT_PLAN_IDLE_MINUTES * 60.0


def worker_budgets(repo: str | None) -> WorkerBudgets:
    """The rounds' thresholds for a worker in ``repo``, from its budgets.

    A repository with no history gets the configured defaults (`health.quiet_minutes`,
    `health.plan_minutes`, `health.checkin_after`), exactly what every repository got
    before budgets existed.
    """
    from papaya_agent_runtime import budgets

    conn = db.init_db() if repo else None
    try:
        quiet = budgets.budget(repo, budgets.SILENCE, conn=conn)
        plan = budgets.budget(repo, budgets.PLAN, conn=conn)
        session = budgets.budget(repo, budgets.WORKER_SESSION, conn=conn)
        # The scoped gate, never the full suite: a worker runs only the scoped gate, so
        # the midpoint never lands before one scoped gate could have finished.
        scoped = budgets.budget(repo, budgets.GATE, conn=conn)
    finally:
        if conn is not None:
            conn.close()
    midpoint = session.seconds / 2
    if scoped.derived:
        midpoint = max(midpoint, scoped.seconds)
    return WorkerBudgets(
        quiet_seconds=quiet.seconds,
        plan_seconds=plan.seconds,
        midpoint_seconds=midpoint,
        sources=(quiet.source, plan.source, session.source),
        plan_idle_seconds=_plan_idle_seconds(),
    )


def _plan_idle_seconds() -> float:
    """`health.plan_idle_minutes` in seconds; the default when there is no config yet."""
    try:
        from papaya_agent_runtime.config import load_config

        return float(load_config().health.plan_idle_minutes) * 60
    except Exception:  # noqa: BLE001 - rounds run before setup too
        return DEFAULT_PLAN_IDLE_MINUTES * 60.0


def _push_by_seconds() -> float:
    """`health.push_by_minutes` in seconds; the default when there is no config yet."""
    try:
        from papaya_agent_runtime.config import load_config

        return float(load_config().health.push_by_minutes) * 60
    except Exception:  # noqa: BLE001 - rounds run before setup too
        return DEFAULT_PUSH_BY_MINUTES * 60.0


# ── reading the ledger for a round ──────────────────────────────────────────


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


@dataclass
class WorkerLook:
    """One worker, as a round sees it."""

    task_id: int
    status: str
    branch: str | None
    created_at: datetime | None
    #: `alive`, `quiet` or `dead` by :func:`health.check`; `None` when not in flight.
    verdict: str | None
    silent_seconds: int | None
    last_event_id: int
    #: `(event id, created at, phase, note)` for every progress report, oldest first.
    progress: list[tuple[int, str, str, str]]
    #: The newest `question`/`blocked` event, as `(event id, question)`.
    question: tuple[int, str] | None
    #: The newest `worker_stopped` event, as `(event id, what stopped it)`.
    stopped: tuple[int, str] | None
    #: Event ids of the newest `answer`/`steer`/... the manager did on this worker.
    last_acted_id: int
    #: When the worker's session last made or ran a tool call, from its provider stream.
    last_tool_at: datetime | None = None
    #: When the session running right now started: the newest dispatch or resume.
    session_started_at: datetime | None = None

    @property
    def latest_phase(self) -> str | None:
        return self.progress[-1][2] if self.progress else None

    def running_seconds(self, now: datetime) -> float:
        """How long the CURRENT session has been going, not how old the task is.

        A check-in asks whether this worker is still heading where the brief asked,
        which is a question about the session in front of it. Measured from the
        task's creation instead, a fresh reconciler picked up on a task delivered
        hours ago is past its midpoint the moment it starts (2026-09-17, task 30).
        """
        since = self.session_started_at or self.created_at
        return (now - since).total_seconds() if since else 0.0

    def tool_idle_seconds(self, now: datetime) -> float:
        """Since the last tool call; since dispatch for a worker that has made none."""
        since = self.last_tool_at or self.created_at
        return max(0.0, (now - since).total_seconds()) if since else 0.0


def look_at_worker(task_id: int, *, now: datetime, quiet_after: timedelta) -> WorkerLook | None:
    conn = db.init_db()
    try:
        task = store.get_task(conn, task_id)
        if task is None:
            return None
        entry = next(
            (
                e
                for e in health.check(conn, quiet_after=quiet_after, now=now)
                if e["task_id"] == task_id
            ),
            None,
        )
        rows = conn.execute(
            "SELECT id, kind, payload, created_at FROM events WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        progress: list[tuple[int, str, str, str]] = []
        question = stopped = None
        last_acted = 0
        last_tool_at: datetime | None = None
        session_started_at: datetime | None = None
        for row in rows:
            kind, payload = str(row["kind"]), _payload(row)
            if _is_tool_activity(kind, payload):
                last_tool_at = _parse(row["created_at"]) or last_tool_at
            if kind in SESSION_START_KINDS:
                session_started_at = _parse(row["created_at"]) or session_started_at
            if kind == "worker_progress" and payload.get("phase"):
                progress.append(
                    (
                        int(row["id"]),
                        str(row["created_at"]),
                        str(payload.get("phase") or ""),
                        str(payload.get("note") or ""),
                    )
                )
            elif kind in ("question", "blocked"):
                question = (int(row["id"]), str(payload.get("question") or ""))
            elif kind == serve.WORKER_STOPPED:
                stopped = (int(row["id"]), serve._failure(kind, payload))
            elif kind in serve.ACTED_KINDS:
                last_acted = int(row["id"])
        return WorkerLook(
            task_id=task_id,
            status=str(task["status"]),
            branch=task["branch"],
            created_at=_parse(task["created_at"]),
            verdict=entry["verdict"] if entry else None,
            silent_seconds=entry["silent_seconds"] if entry else None,
            last_event_id=int(rows[-1]["id"]) if rows else 0,
            progress=progress,
            question=question,
            stopped=stopped,
            last_acted_id=last_acted,
            last_tool_at=last_tool_at,
            session_started_at=session_started_at,
        )
    finally:
        conn.close()


def _is_tool_activity(kind: str, payload: dict[str, Any]) -> bool:
    """A provider stream event that says a tool call was made or is still running.

    The same events `serve.worker_activity` reads for liveness lines: Claude's
    `tool_progress` heartbeat and `assistant` messages with a `tool_use` block, and
    Codex's `item.started`.
    """
    if kind in ("worker_tool_progress", "worker_item.started"):
        return True
    return kind == "worker_assistant" and bool(serve._tool_uses(payload))


def round_records(ticket_task_id: int) -> list[tuple[int, dict[str, Any]]]:
    """Every `ticket_round` record on a ticket's task, as `(event id, payload)`."""
    conn = db.init_db()
    try:
        rows = conn.execute(
            "SELECT id, payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id",
            (ticket_task_id, ROUND_EVENT),
        ).fetchall()
        return [(int(row["id"]), _payload(row)) for row in rows]
    finally:
        conn.close()


def record_round(ticket_task_id: int, action: str, **details: Any) -> None:
    """Record, on the ticket's own task, that a round did ``action`` once."""
    conn = db.init_db()
    try:
        task = store.get_task(conn, ticket_task_id)
        store.append_event(
            conn,
            kind=ROUND_EVENT,
            payload={"task_id": ticket_task_id, "action": action, **details},
            run_id=int(task["run_id"]) if task is not None else None,
            task_id=ticket_task_id,
        )
    finally:
        conn.close()


def _done_before(records: list[tuple[int, dict[str, Any]]], action: str, **match: Any) -> list[int]:
    return [
        event_id
        for event_id, payload in records
        if payload.get("action") == action
        and all(payload.get(key) == value for key, value in match.items())
    ]


def record_round_summary(line: str) -> None:
    """The round's summary line, as an event on no task."""
    from papaya_agent_runtime import team

    conn = db.init_db()
    try:
        store.append_event(conn, kind=team.ROUND_SUMMARY_EVENT, payload={"line": line})
    finally:
        conn.close()


def observe_pr(worker_task_id: int, entry: dict[str, Any]) -> bool:
    """Record a delivered worker's pull request state when it differs from the last record.

    The rounds read the forge live and keep none of it, so without this nothing on the
    record says what a pull request's CI or review is. Returns whether it recorded.
    """
    from papaya_agent_runtime import team

    observed = {
        "task_id": worker_task_id,
        "pr": entry.get("pr"),
        "url": entry.get("url"),
        "state": entry.get("state"),
        "merged": bool(entry.get("merged")),
        "ci": entry.get("ci"),
        "review": entry.get("review") or None,
        "head": entry.get("head"),
    }
    conn = db.init_db()
    try:
        row = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
            (worker_task_id, team.PR_OBSERVED_EVENT),
        ).fetchone()
        if row is not None and _payload(row) == observed:
            return False
        task = store.get_task(conn, worker_task_id)
        store.append_event(
            conn,
            kind=team.PR_OBSERVED_EVENT,
            payload=observed,
            run_id=int(task["run_id"]) if task is not None else None,
            task_id=worker_task_id,
        )
        return True
    finally:
        conn.close()


def person_steers(worker_task_id: int) -> list[dict[str, Any]]:
    """Every steer, answer or resume a person made on this worker (:func:`team.person_actions`)."""
    from papaya_agent_runtime import team

    conn = db.init_db()
    try:
        return team.person_actions(conn, worker_task_id)
    finally:
        conn.close()


def person_wait_since(ticket_task_id: int) -> tuple[int, str, datetime | None] | None:
    """The open person-wait todo on a ticket, with when it was recorded."""
    conn = db.init_db()
    try:
        row = conn.execute(
            "SELECT id, text, created_at FROM todos WHERE task_id = ? AND status = 'open' "
            "AND (blocked_on = 'user' OR blocked_on LIKE 'user:%') ORDER BY id DESC LIMIT 1",
            (ticket_task_id,),
        ).fetchone()
        return (int(row["id"]), str(row["text"]), _parse(row["created_at"])) if row else None
    finally:
        conn.close()


def _outreach_said(key: str) -> bool:
    conn = db.init_db()
    try:
        return outreach.was_said(conn, key)
    finally:
        conn.close()


def _outreach_plan(now: datetime, host: str) -> tuple[outreach.Plan, list[str]]:
    """The outreach plan for this round, read on the thread that owns the connection."""
    conn = db.init_db()
    try:
        return outreach.plan(conn, now=now, host=host)
    finally:
        conn.close()


def _outreach_deliver(
    found: outreach.Plan,
    now: datetime,
    tickets: dict[str, bool],
    dm_landed: bool,
    origins: dict[int, bool] | None = None,
) -> list[str]:
    """Record what serve's own posting landed, through the shared procedure."""
    landed = origins or {}
    conn = db.init_db()
    try:
        return outreach.deliver(
            conn,
            found,
            now=now,
            ticket=lambda item, _body: tickets.get(item, False),
            dm=lambda _text: dm_landed,
            origin=lambda request, _body: landed.get(request, False),
        )
    finally:
        conn.close()


def brief_goals(worker: serve.Worker) -> str | None:
    """The Goals section of the brief this worker was dispatched with, verbatim."""
    from papaya_agent_runtime import brief_lint
    from papaya_agent_runtime.preflight import archived_brief_path

    if not worker.repo:
        return None
    try:
        text = archived_brief_path(worker.repo, worker.task_id).read_text(encoding="utf-8")
    except OSError:
        return None
    return brief_lint.outcome_sections(text).get("Goals")


@dataclass(frozen=True)
class GateState:
    """A worker's gate as a round sees it: running under the supervisor, and the last result."""

    #: True while `ppy gate run` has the gate running under the supervisor.
    running: bool
    #: The newest recorded gate result's one line, or what is known instead.
    line: str
    #: While running: the gate's command, how long it has run, and whether it is the full suite.
    command: str = ""
    elapsed_seconds: float = 0.0
    full: bool = False
    #: Waiting under the supervisor for a repository gate slot or for memory. A queued
    #: gate is ``running`` too: the worker waiting on it is not silent.
    queued: bool = False
    queued_reason: str = ""


def gate_state(worker_task_id: int) -> GateState:
    """Read the worker's gate: the newest `gate_result` and the supervisor's `gate_wait`.

    A `gate_started` (or `gate_queued`) with no result after it names the gate's key;
    the supervisor is then asked (without waiting) whether that gate is still running
    or queued. A start with no gate behind it — the supervisor restarted, the gate was
    killed — is not running.
    """
    from papaya_agent_runtime import gate

    open_kinds = (gate.GATE_STARTED, gate.GATE_QUEUED, gate.GATE_UNQUEUED)
    conn = db.init_db()
    try:
        rows = conn.execute(
            "SELECT kind, payload FROM events WHERE task_id = ? AND kind IN (?, ?, ?, ?, ?) "
            "ORDER BY id DESC",
            (worker_task_id, *open_kinds, gate.GATE_RESULT, gate.GATE_KILLED),
        ).fetchall()
    finally:
        conn.close()
    last_result = next((_payload(r) for r in rows if r["kind"] == gate.GATE_RESULT), None)
    line = (
        gate._result_from(last_result).line()
        if last_result is not None
        else "no gate result recorded for this worker"
    )
    if not rows or rows[0]["kind"] not in open_kinds:
        return GateState(False, line)
    started = _payload(rows[0])
    scope = "full" if started.get("full") else "local"
    key = str(
        started.get("key") or f"task:{worker_task_id}:{scope}:{started.get('head_sha') or ''}"
    )
    try:
        from papaya_agent_runtime.supervisor.client import SupervisorClient

        answer = SupervisorClient().gate_wait(key, timeout=0.0)
    except Exception:  # noqa: BLE001 - no supervisor to ask means nothing runs under it
        return GateState(False, line)
    if not answer.get("ok") or not answer.get("running"):
        return GateState(False, line)
    seconds = float(answer.get("elapsed") or 0)
    command = str(answer.get("command") or started.get("command") or "the gate")
    full = bool(answer.get("full", started.get("full")))
    if answer.get("queued"):
        reason = str(answer.get("queued_reason") or "queued")
        return GateState(
            True,
            f"queued under the supervisor: `{command}`, {reason}; {line}",
            command=command,
            full=full,
            queued=True,
            queued_reason=reason,
        )
    return GateState(
        True,
        f"running under the supervisor for {health.humanize(int(seconds))}: `{command}`; {line}",
        command=command,
        elapsed_seconds=seconds,
        full=full,
    )


def close_dead_runners(watched: set[int]) -> list[Any]:
    """Close runner rows with no process behind them, whatever their ticket's state.

    A worker under an ended ticket (`handed_over`, `done`, declined) is never looked
    at by step 3, and its dead row held a worker slot for three hours on 2026-09-16.
    Workers of held tickets are left to step 3, which says why they stopped on the
    ticket; ``watched`` names them. The grace covers a runner in this process between
    its worker exiting and its result being recorded.
    """
    from papaya_agent_runtime.supervisor import dead_runners

    conn = db.init_db()
    try:
        return dead_runners.close_dead_runners(
            conn, grace_s=DEAD_GRACE_SECONDS, skip_tasks=watched, source="rounds"
        )
    finally:
        conn.close()


def record_worker_stopped(task_id: int, detail: str) -> None:
    """A dead session with no done note, recorded the way the runner records a stop.

    The reasons are the evidence :func:`turn_end.why_stopped` finds, after the one
    the round saw: no runner process is left. Runner rows still marked live are
    orphaned first, as `ppy reconcile` would, so nothing reads the task as running.
    """
    from papaya_agent_runtime import turn_end

    conn = db.init_db()
    try:
        task = store.get_task(conn, task_id)
        if task is None:
            return
        for runner in store.live_runners_for_task(conn, task_id):
            store.update_runner(conn, runner["id"], status="orphaned")
        reasons = [detail]
        try:
            reasons.extend(turn_end.why_stopped(conn, task_id).reasons)
        except Exception as exc:  # noqa: BLE001 - the dead session is reason enough
            log.debug("[rounds] Could not judge why task %d stopped: %s", task_id, exc)
        store.set_task_status(conn, task_id, turn_end.WORKER_STOPPED)
        store.append_event(
            conn,
            kind=turn_end.WORKER_STOPPED,
            payload={
                "task_id": task_id,
                "summary": "worker stopped before done: " + "; ".join(reasons),
                "reasons": reasons,
                "source": "rounds",
            },
            run_id=int(task["run_id"]),
            task_id=task_id,
        )
    finally:
        conn.close()


def branch_ahead_of_base(worker_task_id: int) -> bool | None:
    """Whether the worker's branch holds commits its base does not, or ``None`` if unknown."""
    conn = db.init_db()
    try:
        row = conn.execute(
            "SELECT t.branch, t.base_sha, t.worktree_path, r.local_path, r.default_branch "
            "FROM tasks t LEFT JOIN repos r ON r.id = t.repo_id WHERE t.id = ?",
            (worker_task_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or not row["branch"]:
        return None
    cwd = next(
        (p for p in (row["worktree_path"], row["local_path"]) if p and os.path.isdir(p)), None
    )
    base = row["base_sha"] or (f"origin/{row['default_branch']}" if row["default_branch"] else None)
    if cwd is None or base is None:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-list", "--count", f"{base}..{row['branch']}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return int(out) > 0 if proc.returncode == 0 and out.isdigit() else None


@dataclass(frozen=True)
class PushState:
    """What of a worker's work is on the forge's copy of its lease branch."""

    #: The forge's tip of the branch, or ``None`` when nothing was ever pushed to it.
    remote_sha: str | None
    #: The worktree's HEAD when the forge was asked.
    head_sha: str | None
    #: HEAD is not on the forge: neither the tip nor a commit behind it. Uncommitted
    #: files do not count; a worker whose commits are all pushed is not nudged to push.
    unpushed: bool


def _git_out(cwd: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def push_state(worker_task_id: int) -> PushState | None:
    """Ask the forge for the worker's lease branch now, or ``None`` if it cannot be read.

    The remote side is `git ls-remote <forge_url> refs/heads/<branch>`, fresh every
    call: never a remote-tracking ref, which is only as current as the remote a
    clone was told about when it last fetched or pushed. A repository with no
    `forge_url` is asked through the worktree's `origin`. The local side is the
    worktree's HEAD, which is on the forge when it is the tip or behind it.
    """
    from papaya_agent_runtime import repos

    conn = db.init_db()
    try:
        row = conn.execute(
            "SELECT t.branch, t.worktree_path, r.forge_url FROM tasks t "
            "LEFT JOIN repos r ON r.id = t.repo_id WHERE t.id = ?",
            (worker_task_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or not row["branch"] or not row["worktree_path"]:
        return None
    cwd = str(row["worktree_path"])
    if not os.path.isdir(cwd):
        return None
    head = _git_out(cwd, "rev-parse", "HEAD")
    url = row["forge_url"] or repos.remote_url(cwd)
    if head is None or not url:
        return None
    read, remote = repos.forge_branch_tip(str(url), str(row["branch"]), cwd=cwd)
    if not read:
        return None
    if remote is None:
        return PushState(None, head, True)
    return PushState(remote, head, not _on_forge(cwd, head, remote))


def _on_forge(cwd: str, head: str, remote: str) -> bool:
    """HEAD is the forge's tip, or an ancestor of it (the object must be here to tell)."""
    if head == remote:
        return True
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "merge-base", "--is-ancestor", head, remote],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def push_record(
    push: PushState, records: list[tuple[int, dict[str, Any]]], look: WorkerLook
) -> dict[str, str | None]:
    """What a push check-in saw: the forge's tip, the worktree's HEAD, the last push.

    The last push is when a round first saw the forge's tip, or ``None`` for a
    branch the forge has never had.
    """
    at = last_push_at(records, look.task_id, push.remote_sha)
    return {
        "remote_sha": push.remote_sha,
        "head_sha": push.head_sha,
        "last_push_at": at.isoformat() if at is not None else None,
    }


def push_facts(seen: Mapping[str, str | None]) -> tuple[tuple[str, str], ...]:
    """The push check-in's three values, as facts the check-in turn reads."""
    return (
        (
            "the lease branch's tip on the forge (read now)",
            seen.get("remote_sha") or "(the forge has no such branch: nothing was ever pushed)",
        ),
        ("the worktree's HEAD", seen.get("head_sha") or "(unknown)"),
        (
            "the last push the runtime recorded (when a round first saw that tip)",
            seen.get("last_push_at") or "(none)",
        ),
    )


def last_push_at(
    records: list[tuple[int, dict[str, Any]]], worker_task_id: int, remote_sha: str | None
) -> datetime | None:
    """When a round first saw the forge's tip at ``remote_sha``, or ``None`` if none did.

    That is the runtime's record of the push: a round asks the forge every round, so
    it is at most one round late, and never earlier than the push really landed.
    """
    if remote_sha is None:
        return None
    seen = [
        _parse(payload.get("at"))
        for _id, payload in records
        if payload.get("action") == PUSH_SEEN
        and payload.get("worker_task_id") == worker_task_id
        and payload.get("remote_sha") == remote_sha
    ]
    stamps = [at for at in seen if at is not None]
    return min(stamps) if stamps else None


# ── the tickets a round can act on without holding them ─────────────────────


@dataclass(frozen=True)
class Ticket:
    """A ticket task in this runtime's table, as a round finds it."""

    task_id: int
    run_id: int
    status: str
    phase: str | None
    work_item_id: str
    subject: str
    title: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def event(self) -> papaya_events.PapayaEvent:
        return papaya_events.PapayaEvent(
            id=self.metadata.get("id"),
            kind=str(self.metadata.get("kind") or ""),
            subject=self.subject,
            payload={},
            work_item_id=self.work_item_id,
        )


def ticket_tasks() -> list[Ticket]:
    """The newest ticket task for every work item this runtime ever recorded."""
    from papaya_agent_runtime.paths import db_path

    if not db_path().exists():
        return []
    conn = db.init_db()
    try:
        rows = conn.execute(
            "SELECT tasks.id, tasks.run_id, tasks.status, tasks.phase, tasks.title, "
            "task_env.value AS metadata FROM tasks JOIN task_env ON task_env.task_id = tasks.id "
            "WHERE task_env.key = ? AND json_valid(task_env.value) ORDER BY tasks.id DESC",
            (papaya_events.PAPAYA_EVENT_METADATA,),
        ).fetchall()
    finally:
        conn.close()
    newest: dict[str, Ticket] = {}
    for row in rows:
        metadata = json.loads(row["metadata"])
        item = str(metadata.get("work_item_id") or "")
        if not item or item in newest:
            continue
        newest[item] = Ticket(
            task_id=int(row["id"]),
            run_id=int(row["run_id"]),
            status=str(row["status"]),
            phase=row["phase"],
            work_item_id=item,
            subject=str(metadata.get("subject") or f"work_item:{item}"),
            title=str(row["title"] or ""),
            metadata=metadata,
        )
    return list(newest.values())


def reclaimable(tickets: list[Ticket]) -> list[Ticket]:
    """The tickets a new offer would resume: work was under way and nobody gave it away.

    A ticket handed over to another holder is not the rounds' to ask for again every
    five minutes: the sweep asks once that holder shows no evidence of working it.
    """
    from papaya_agent_runtime.lifecycle import TERMINAL_STATUSES

    conn = db.init_db()
    try:
        return [
            t
            for t in tickets
            if t.status not in TERMINAL_STATUSES
            and t.phase != serve.PHASE_HANDED_OVER
            and serve.resumable_phase(conn, t.task_id)
        ]
    finally:
        conn.close()


def declined_for_a_missed_turn(tickets: list[Ticket]) -> list[Ticket]:
    """Tickets the runner handed back because a turn ended without doing its job (PAP-213)."""
    conn = db.init_db()
    try:
        found = []
        for ticket in tickets:
            if ticket.phase != serve.PHASE_DECLINED:
                continue
            row = conn.execute(
                "SELECT payload FROM events WHERE task_id = ? AND kind = ? "
                "ORDER BY id DESC LIMIT 1",
                (ticket.task_id, store.TICKET_PHASE_EVENT),
            ).fetchone()
            if row is not None and _TURN_MISSED in str(_payload(row).get("detail") or ""):
                found.append(ticket)
        return found
    finally:
        conn.close()


def closing_reason(item: dict[str, Any], agent_id: str) -> str | None:
    """Why Papaya's current record of a work item says it is not this agent's to run.

    ``None`` when it still is: open, and unowned or owned by ``agent_id``. Ownership
    is compared by id, never by handle or name; with no agent id to compare, only the
    status decides.
    """
    if str(item.get("status") or "") in _ITEM_ENDED:
        return CLOSED_ITEM_CLOSED
    owner = str(item.get("owner_id") or "")
    if owner and agent_id and owner != agent_id:
        return CLOSED_NOT_AGENTS_ITEM
    return None


def close_ticket(ticket: Ticket, reason: str, item: dict[str, Any]) -> None:
    """Close a ticket in the ledger: the reason on the record, then the phase `done`."""
    status, owner = str(item.get("status") or ""), str(item.get("owner_id") or "")
    conn = db.init_db()
    try:
        store.append_event(
            conn,
            kind=TICKET_CLOSED,
            payload={
                "task_id": ticket.task_id,
                "work_item_id": ticket.work_item_id,
                "reason": reason,
                "status": status,
                "owner_id": owner,
            },
            run_id=ticket.run_id,
            task_id=ticket.task_id,
        )
        detail = (
            f"closed ({reason}): the work item is {status}"
            if reason == CLOSED_ITEM_CLOSED
            else f"closed ({reason}): the work item is owned by {owner}"
        )
        serve.record_phase(conn, ticket.task_id, serve.PHASE_DONE, detail)
    finally:
        conn.close()


# ── instruction tickets: a person's request a hold started and did not finish ──

#: Why an instruction ticket was closed without being taken back up: Papaya refused
#: it to this machine, or Papaya has it closed already (answered, failed, cancelled).
CLOSED_NOT_RECLAIMED = "not_reclaimed"
CLOSED_AT_PAPAYA = "closed_at_papaya"
#: What an offer answers when it raised: nothing is known, so the next round tries again.
OFFER_FAILED = "failed"
#: A request Papaya answered 404 for this many reads in a row: closed here, silently.
NOT_FOUND_AFTER = 3
NOT_FOUND = "not_found"


@dataclass(frozen=True)
class InstructionTicket:
    """An instruction ticket that was being answered and is not answered yet."""

    task_id: int
    run_id: int
    phase: str | None
    instruction: papaya_events.Instruction

    def envelope(self, *, agent_id: str = "", workspace_id: str = "") -> dict[str, Any]:
        """The `machine.instruction` event an offer stands in for, as it first came.

        Its payload is the instruction recorded at intake, so the hold that takes it
        reads the same request, reply block and all, and lands on the same ticket
        (`instructions.record_ticket` keys on the subject).
        """
        envelope: dict[str, Any] = {
            "kind": papaya_events.MACHINE_INSTRUCTION,
            "subject": self.instruction.subject,
            "occurred_at": datetime.now(UTC).isoformat(),
            "payload": json.loads(self.instruction.as_json()),
        }
        if agent_id:
            envelope["agent_id"] = agent_id
        if workspace_id:
            envelope["workspace_id"] = workspace_id
        return envelope


def unfinished_instructions(conn: Any) -> list[InstructionTicket]:
    """Every instruction ticket this process lost to its own shutdown or a crash.

    What a restart leaves: the hold was cancelled at shutdown (`released`, marked
    `instructions.SHUTDOWN`), or the process died under it (`picked_up`, a working
    phase), and Papaya still has the request `picked_up` with nobody holding it. Not a
    lost lease — Papaya took it back, or a person released it in the app — nor a
    decline, nor anything answered (`instructions.live`). One replied to and not
    reported is `instructions.recover`'s.
    """
    from papaya_agent_runtime.lifecycle import TERMINAL_STATUSES

    rows = conn.execute(
        "SELECT tasks.id, tasks.run_id, tasks.status, tasks.phase FROM tasks "
        "JOIN task_env ON task_env.task_id = tasks.id WHERE task_env.key = ? ORDER BY tasks.id",
        (instructions.INSTRUCTION_SUBJECT,),
    ).fetchall()
    found = []
    for row in rows:
        task_id = int(row["id"])
        if row["status"] in TERMINAL_STATUSES or not instructions.live(conn, task_id):
            continue
        instruction = instructions.instruction_of(conn, task_id)
        if instruction is None:
            continue
        found.append(InstructionTicket(task_id, int(row["run_id"]), row["phase"], instruction))
    return found


def close_instruction(
    conn: Any,
    ticket: InstructionTicket,
    why: str,
    *,
    environ: dict[str, str],
    post: Callable[..., str | None] | None,
    report: Callable[..., bool] | None,
    tell: bool = True,
) -> instructions.Answered | None:
    """An instruction that cannot be taken back up, closed here.

    ``tell``: Papaya refused it to this machine while it still has it open, so the
    person hears it once, where they asked, with what it was waiting on, and the result
    is reported `failed` so Papaya stops showing it `picked_up`. Not ``tell``: Papaya
    has it closed already (answered, failed, cancelled), and nothing is said or
    reported — it is only closed on this side. Either way what it was blocked on is
    closed with it, so no report says it later, and phase `done` keeps every later
    round from finding it again.
    """
    answered = None
    if tell:
        waits = [
            instructions.for_person(text, ticket.instruction, allow_empty=True)
            for text in instructions.open_waits(conn, ticket.run_id)
        ]
        text = instructions.not_finished(ticket.instruction, why, [w for w in waits if w])
        seams: dict[str, Any] = {}
        if post is not None:
            seams["post"] = post
        if report is not None:
            seams["report"] = report
        answered = instructions.answer(
            conn, ticket.task_id, ticket.instruction, "failed", text, environ=environ, **seams
        )
    reason = CLOSED_NOT_RECLAIMED if tell else CLOSED_AT_PAPAYA
    instructions.close_waits(conn, ticket.run_id)
    store.append_event(
        conn,
        kind=TICKET_CLOSED,
        payload={"task_id": ticket.task_id, "reason": reason, "why": why},
        run_id=ticket.run_id,
        task_id=ticket.task_id,
    )
    serve.record_phase(conn, ticket.task_id, serve.PHASE_DONE, f"closed ({reason}): {why}")
    return answered


def observe_ci(worker_task_id: int, seconds: float, outcome: str) -> None:
    """Keep a delivered pull request's CI wall time once, however many rounds see it."""
    from papaya_agent_runtime import budgets

    conn = db.init_db()
    try:
        if not budgets.observed(
            conn, task_id=worker_task_id, kind=budgets.CI, seconds=seconds, outcome=outcome
        ):
            budgets.observe_task(worker_task_id, budgets.CI, seconds, outcome=outcome, conn=conn)
    finally:
        conn.close()


def record_pr_attention(
    worker_task_id: int, summary: str, reasons: list[str], **facts: Any
) -> None:
    """What the review turn reads, and a reconciler's brief is built from."""
    conn = db.init_db()
    try:
        task = store.get_task(conn, worker_task_id)
        store.append_event(
            conn,
            kind=serve.PR_ATTENTION,
            payload={"task_id": worker_task_id, "summary": summary, "reasons": reasons, **facts},
            run_id=int(task["run_id"]) if task is not None else None,
            task_id=worker_task_id,
        )
    finally:
        conn.close()


def workers_of(conn: Any, ticket: Ticket) -> set[int]:
    """Every other task in the ticket's run: its workers."""
    rows = conn.execute(
        "SELECT id FROM tasks WHERE run_id = ? AND id != ?", (ticket.run_id, ticket.task_id)
    ).fetchall()
    return {int(row["id"]) for row in rows}


def _set_phase(task_id: int, phase: str, detail: str = "") -> None:
    conn = db.init_db()
    try:
        serve.record_phase(conn, task_id, phase, detail)
    finally:
        conn.close()


# ── hygiene ─────────────────────────────────────────────────────────────────


def default_git(args: list[str], cwd: str, timeout: float = FETCH_TIMEOUT_SECONDS) -> int:
    """One git command in a base clone; a failure is a return code, never a raise."""
    try:
        return subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, check=False, timeout=timeout
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return 127


def default_prune(task_id: int | None) -> dict[str, Any]:
    from papaya_agent_runtime.worktree import reclaim

    return reclaim.prune(task_id=task_id, managed_only=True)


def managed_clone_paths(repo_names: set[str] | None = None) -> list[str]:
    """The base clones hygiene may tidy: this instance's, under `.ppy/repos`."""
    from papaya_agent_runtime.worktree import reclaim

    conn = db.init_db()
    try:
        clones = reclaim.managed_base_clones(conn)
    finally:
        conn.close()
    return sorted(path for path, name in clones.items() if repo_names is None or name in repo_names)


def task_updated_at(task_id: int | None) -> datetime | None:
    if task_id is None:
        return None
    conn = db.init_db()
    try:
        task = store.get_task(conn, task_id)
        return _parse(task["updated_at"]) if task is not None else None
    finally:
        conn.close()


def record_hygiene(payload: dict[str, Any]) -> None:
    conn = db.init_db()
    try:
        store.append_event(
            conn, kind=HYGIENE_EVENT, payload=payload, task_id=payload.get("task_id")
        )
    finally:
        conn.close()


def hygiene_records() -> list[dict[str, Any]]:
    conn = db.init_db()
    try:
        rows = conn.execute(
            "SELECT payload FROM events WHERE kind = ? ORDER BY id", (HYGIENE_EVENT,)
        ).fetchall()
        return [_payload(row) for row in rows]
    finally:
        conn.close()


def surfaced_loose_ends() -> set[str]:
    """Kept slot paths already surfaced to a person, from the hygiene record."""
    return {
        str(path) for record in hygiene_records() for path in record.get("surfaced") or [] if path
    }


def surface_kept_slot(task_id: int | None, text: str) -> int:
    """A person-wait todo for a kept slot: the same primitive a turn records to wait on a person."""
    conn = db.init_db()
    try:
        task = store.get_task(conn, task_id) if task_id is not None else None
        return store.add_todo(
            conn,
            text,
            run_id=int(task["run_id"]) if task is not None else None,
            task_id=task_id,
            blocked_on="user",
        )
    finally:
        conn.close()


def ticket_for_worker(worker_task_id: int | None) -> Ticket | None:
    if worker_task_id is None:
        return None
    conn = db.init_db()
    try:
        task = store.get_task(conn, worker_task_id)
    finally:
        conn.close()
    if task is None:
        return None
    return next((t for t in ticket_tasks() if t.run_id == int(task["run_id"])), None)


# ── the rounds ──────────────────────────────────────────────────────────────


class Rounds:
    """Walk the board on a clock, for one embedded listener and its runner.

    Every collaborator with an outside world is a keyword seam: ``sleep`` (the
    timer), ``clock`` (wall time, as an aware ``datetime``), ``forge`` (pull
    request states, :func:`watch.pr_states` by default), ``prune`` and ``git``
    (hygiene), ``branch_ahead``, ``pushed`` (what of a worker's work is on its
    remote branch, :func:`push_state`) and ``papaya_env`` (the credentials a comment
    outside a held job is posted with).
    """

    def __init__(
        self,
        built: Any,
        runner: Any,
        *,
        interval: float = DEFAULT_ROUNDS_INTERVAL,
        stderr: Any = None,
        lock: asyncio.Lock | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] | None = None,
        forge: Callable[[Any], list[dict[str, Any]]] | None = None,
        prune: Callable[[int | None], dict[str, Any]] | None = None,
        git: Callable[..., int] | None = None,
        branch_ahead: Callable[[int], bool | None] | None = None,
        papaya_env: Callable[[], dict[str, str]] | None = None,
        gate: Callable[[int], GateState] | None = None,
        gate_verdict: Callable[[int], Any] | None = None,
        pushed: Callable[[int], PushState | None] | None = None,
        runtime_repo: Callable[[], str | None] | None = None,
        pr_details: Callable[[int, dict[str, Any]], dict[str, Any]] | None = None,
        steer_worker: Callable[[int, str], Any] | None = None,
        merge: Callable[[int, dict[str, Any], str], Any] | None = None,
        turns: lanes.TurnRunner | None = None,
        reporter: Any = None,
        on_round: Callable[[], Awaitable[list[str]]] | None = None,
        status_publisher: machine_status.Publisher | None = None,
        change_sleep: Callable[[float], Awaitable[None]] | None = None,
        instruction_report: Callable[..., bool] | None = None,
        read_item: Callable[[Ticket], dict[str, Any] | None] | None = None,
        instruction_post: Callable[..., str | None] | None = None,
        read_instruction: Callable[..., str | None] | None = None,
    ) -> None:
        self._built = built
        #: What the process running the rounds checks of its own each round (`serve`:
        #: the supervisor it depends on and its lifeline watcher); returns summary parts.
        self._on_round = on_round
        self._runner = runner
        self._interval = float(interval)
        self._stderr = stderr
        self._lock = lock or asyncio.Lock()
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or (lambda: datetime.now(UTC))
        self._forge = forge or _default_forge
        self._prune = prune or default_prune
        self._git = git or default_git
        self._branch_ahead = branch_ahead or branch_ahead_of_base
        self._papaya_env = papaya_env or self._env_from_connection
        self._gate = gate or gate_state
        self._gate_verdict = gate_verdict or _default_gate_verdict
        self._pushed = pushed or push_state
        #: The runtime's own GitHub repository (`owner/name`), whose red CI is a deficiency.
        self._runtime_repo = runtime_repo or deficiencies.runtime_repo
        self._pr_details = pr_details or _default_pr_details
        #: How a repair with no ticket reaches its worker: a steer, admitted in the lane.
        self._steer_worker = steer_worker or supervision.steer_worker
        self._merge = merge or _default_merge
        #: How a turn keyed on a task runs (the owed and ledger lanes): the runner's own
        #: harness, tools and config seams, this connection's credentials, no hold.
        self._turns = turns or lanes.TurnRunner(
            run_turn=getattr(runner, "_run_turn", None),
            turn_tools=getattr(runner, "_turn_tools", None),
            config=getattr(runner, "_config", None),
            runtime_dir=getattr(runner, "_runtime_dir", None),
            papaya_env=self._papaya_env,
            steer=self._steer_worker,
            agent_kind=getattr(runner, "agent_kind", None),
            clock=self._clock,
        )
        #: What opens the runtime's recorded deficiencies as issues (`serve` hands in its own).
        self._reporter = reporter
        #: What tells Papaya this machine's status: every round, and on change.
        self.status = status_publisher or machine_status.Publisher(
            build=machine_status.snapshot_now,
            put=lambda body: papaya_events.put_connection_status(body, environ=self._papaya_env()),
        )
        self._change_sleep = change_sleep
        #: How an instruction replied to and never reported is reported now.
        self._instruction_report = instruction_report
        #: How the rounds say something where an instruction was asked (a request that
        #: could not be taken back up, an ask about it): the runner's own, by default.
        self._instruction_post = instruction_post
        #: Papaya's status for an instruction before it is taken back up:
        #: ``(reply block, environ=) -> status | None`` (`read_instruction_status`).
        self._read_instruction = read_instruction
        #: Ticket task -> how many reads in a row Papaya answered 404 for its request.
        self._not_found: dict[int, int] = {}
        self._last_deficiencies: datetime | None = None
        #: Subjects whose reserve Papaya refused during a reclaim, with the holder.
        self._refused: dict[str, dict[str, Any]] = {}
        self._watched: Any = None
        #: Work items this process already re-offered after a missed turn.
        self._reoffered: set[str] = set()
        #: How a missed-turn re-offer reads Papaya's current record of the work item.
        self._read_item = read_item or self._read_work_item
        #: Missed-turn tickets whose work item could not be read: tried again next round.
        self._unread: set[str] = set()
        self._last_hygiene: datetime | None = None
        #: Kept slot path -> how many hygiene runs in a row have kept it.
        self._kept_runs: dict[str, int] = {}
        self.summaries: list[str] = []

    # -- the timer -------------------------------------------------------------

    async def run(self) -> None:
        """Reclaim now, then a round every interval until cancelled. Zero reclaims once."""
        await self.start()
        if self._interval <= 0:
            return
        while True:
            await self._sleep(self._interval)
            await self.round_once()

    async def start(self) -> list[str]:
        """What a start owes the board: take back every ticket the last process left."""
        return await self._guarded(self._start)

    async def round_once(self) -> list[str]:
        """One round. Returns the summary parts; never raises."""
        return await self._guarded(self._round)

    async def _guarded(self, body: Callable[[], Awaitable[list[str]]]) -> list[str]:
        async with self._lock:
            try:
                parts = await body()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad round must not end serve
                log.exception("[rounds] Round failed: %s", exc)
                parts = [f"the round failed: {exc}"]
                await asyncio.to_thread(deficiencies.record_exception, "a manager round", exc)
        # A deficiency another process recorded (a `ppy` command, a worker) is
        # reported on the next round rather than on the next start.
        deficiencies.notify()
        if parts:
            line = "round: " + "; ".join(parts)
            self.summaries.append(line)
            # On the record too, so `ppy status --team` and `ppy tail` can say it.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(record_round_summary, line)
            if self._stderr is not None:
                with contextlib.suppress(Exception):
                    print(f"ppy serve: {line}", file=self._stderr, flush=True)
        return parts

    def _standalone(self) -> bool:
        """No Papaya connection: nothing to offer to, reserve from, or reclaim through."""
        return bool(getattr(self._built, "standalone", False))

    async def _start(self) -> list[str]:
        if self._standalone():
            # Reclaiming a ticket is a Papaya reserve; the next connected start does it.
            return []
        tickets = await asyncio.to_thread(ticket_tasks)
        parts = await self._reclaim(tickets)
        parts += await self._reclaim_instructions()
        parts += await self._reoffer_missed(tickets)
        parts += await self._instruction_lane()
        return parts

    async def _round(self) -> list[str]:
        now = self._clock()
        parts = await self._on_round() if self._on_round is not None else []
        parts += await self._pull_requests(now)
        if not self._standalone():
            tickets = await asyncio.to_thread(ticket_tasks)
            parts += await self._reclaim(tickets)
            parts += await self._reclaim_instructions()
            if self._unread:
                # Only the re-offers a failed read held back at start; nothing new.
                unread = [t for t in tickets if t.work_item_id in self._unread]
                parts += await self._reoffer_missed(unread)
            # Work items no held ticket listens to: the same check a session's heartbeat
            # runs while no serve does (`workitems.check_untracked`).
            parts += await asyncio.to_thread(
                workitems.check_untracked,
                env=self._papaya_env(),
                held=self._held_ids(),
                agent_id=getattr(self._runner, "_own_agent_id", lambda: None)(),
            )
        held = list(getattr(self._runner, "held", {}).values())
        watched = {t.worker.task_id for t in held if getattr(t, "worker", None) is not None}
        closed = await asyncio.to_thread(close_dead_runners, watched)
        parts += [entry.line() for entry in closed]
        for ticket in held:
            parts += await self._look_at(ticket, now)
        parts += await self._owed_lane(now, watched)
        parts += await self._ledger_lane(now)
        parts += await self._outreach_lane(now)
        parts += await self._deficiency_lane(now)
        if self._last_hygiene is None or (
            (now - self._last_hygiene).total_seconds() >= HYGIENE_EVERY_SECONDS
        ):
            self._last_hygiene = now
            parts += await self._hygiene(None, now)
        if not self._standalone():
            parts += await self._instruction_lane()
            await self.status.publish("round")
        return parts

    async def _instruction_lane(self) -> list[str]:
        """An instruction answered and never reported (a crash between the two) is reported."""
        seams = {"report": self._instruction_report} if self._instruction_report else {}
        try:
            return await store.run_in_thread(
                functools.partial(instructions.recover, environ=self._papaya_env(), **seams)
            )
        except Exception as exc:  # noqa: BLE001 - the rounds keep going
            log.warning("[rounds] Could not finish reporting instructions: %s", exc)
            return []

    async def watch_changes(self) -> None:
        """Publish the status snapshot as soon as the board changes, between rounds."""
        if self._standalone():
            return
        await self.status.watch(machine_status.mark_now, sleep=self._change_sleep)

    async def close(self) -> None:
        """End the turns the lanes have running: serve is stopping."""
        await self._turns.close()

    # -- the lanes: what no held ticket covers ---------------------------------------

    async def _owed_lane(self, now: datetime, covered: set[int]) -> list[str]:
        """Every worker waiting on the manager with no live ticket (`lanes.owed_decisions`).

        ``covered`` are the held tickets' workers; connected, the workers of every ticket
        this process can offer back to the loop are the ticket path's too (a stalled or
        released hold resumes from its worker's state, and its review turn is that
        ticket's), so the lane never runs a second turn beside a reclaim.
        """
        if not self._standalone():
            covered = covered | await store.run_in_thread(self._ticket_workers)
        decisions = await store.run_in_thread(lanes.owed_decisions, now=now, covered=covered)
        if not decisions:
            return []
        return await self._turns.take_up(decisions)

    def _ticket_workers(self, conn: Any) -> set[int]:
        """The workers of every ticket held, being offered, or that a reclaim would resume."""
        tickets = ticket_tasks()
        running, held = self._running(), self._held_ids()
        theirs = [t for t in tickets if t.subject in running or t.task_id in held]
        theirs += reclaimable([t for t in tickets if t not in theirs])
        found: set[int] = set()
        for ticket in theirs:
            found |= workers_of(conn, ticket)
        # A request being answered, or one a reclaim takes back up, reviews its own worker.
        for request in unfinished_instructions(conn):
            found |= {
                int(row["id"])
                for row in conn.execute(
                    "SELECT id FROM tasks WHERE run_id = ? AND id != ?",
                    (request.run_id, request.task_id),
                ).fetchall()
            }
        return found

    async def _ledger_lane(self, now: datetime) -> list[str]:
        """Next steps that sat in the ledger get the ledger turn (`lanes.ledger_due`).

        First, a step waiting on a task that has ended is released
        (`lanes.release_finished_waits`), so it is due this round, not never.
        """
        released = await store.run_in_thread(lanes.release_finished_waits, now=now)
        due = await store.run_in_thread(lanes.ledger_due, now=now)
        if not due:
            return released
        return released + await self._turns.take_up_ledger(due)

    async def _outreach_lane(self, now: datetime) -> list[str]:
        """Everything waiting on a person is said to them, and again on a clock (`outreach`).

        The same plan a session's heartbeat and hooks make; serve says it through its
        own connection: the work item comments with this connection's credentials, the
        message through the listener's client (`outreach.say_in_workspace`: the DM, or a
        channel with the owner mentioned). Never raises: a person who could not be
        reached this round is reached the next.
        """
        try:
            found, lines = await asyncio.to_thread(_outreach_plan, now, serve._where())
            if not found:
                return lines
            env = self._papaya_env()
            landed: dict[str, bool] = {}
            for item, body in found.tickets.items():
                landed[item] = await asyncio.to_thread(
                    outreach.post_ticket, item, body, environ=env
                )
            # An ask about a person's request: where they asked, as a progress reply.
            origins: dict[int, bool] = {}
            for request, body in found.origins.items():
                origins[request] = await asyncio.to_thread(
                    functools.partial(
                        outreach.post_origin, request, body, environ=env, post=self._post_seam()
                    )
                )
            dm_landed = bool(found.dm) and await outreach.say_in_workspace(
                getattr(self._built, "api", None), found.dm
            )
            return lines + await asyncio.to_thread(
                _outreach_deliver, found, now, landed, dm_landed, origins
            )
        except Exception as exc:  # noqa: BLE001 - the rounds keep going
            log.warning("[rounds] Could not reach the person things wait on: %s", exc)
            return [f"could not reach the person things wait on: {exc}"]

    async def _deficiency_lane(self, now: datetime) -> list[str]:
        """Open recorded deficiencies as issues on the clock (`lanes.deficiency_step`)."""
        if self._last_deficiencies is not None and (
            (now - self._last_deficiencies).total_seconds() < lanes.DEFICIENCY_EVERY_SECONDS
        ):
            return []
        self._last_deficiencies = now
        return await asyncio.to_thread(lanes.deficiency_step, self._reporter)

    # -- reclaim -----------------------------------------------------------------

    def _running(self) -> set[str]:
        loop = getattr(self._built, "loop", None)
        return set(getattr(loop, "running_subjects", ()) or ())

    def _held_ids(self) -> set[int]:
        return set(getattr(self._runner, "held", {}) or {})

    async def _reclaim(self, tickets: list[Ticket]) -> list[str]:
        running, held = self._running(), self._held_ids()
        waiting = [t for t in tickets if t.subject not in running and t.task_id not in held]
        parts: list[str] = []
        for ticket in await asyncio.to_thread(reclaimable, waiting):
            outcome = await self._offer(ticket)
            if outcome == sweep.OFFER_PENDING:
                parts.append(f"took ticket task {ticket.task_id} back up ({ticket.work_item_id})")
            elif isinstance(outcome, dict):
                holder = sweep.holder_name(outcome)
                await asyncio.to_thread(
                    _set_phase, ticket.task_id, serve.PHASE_HANDED_OVER, f"held by {holder}"
                )
                parts.append(f"ticket task {ticket.task_id} is held by {holder}: handed over")
                parts += await self._clean_if_merged(ticket)
        return parts

    async def _reoffer_missed(self, tickets: list[Ticket]) -> list[str]:
        running, held = self._running(), self._held_ids()
        parts: list[str] = []
        for ticket in await asyncio.to_thread(declined_for_a_missed_turn, tickets):
            if ticket.work_item_id in self._reoffered or ticket.subject in running:
                continue
            if ticket.task_id in held:
                continue
            # The ledger outlives the item: Papaya's record now decides, read once
            # per ticket per round (PAP-255 was offered four days after it was done,
            # and under a different agent).
            try:
                item = await asyncio.to_thread(self._read_item, ticket)
            except Exception as exc:  # noqa: BLE001 - one bad read must not end the round
                item, why = None, str(exc)
            else:
                why = "not connected"
            if not isinstance(item, dict):
                self._unread.add(ticket.work_item_id)
                log.warning(
                    "[rounds] Not offering ticket task %d again yet: could not read %s (%s)",
                    ticket.task_id,
                    ticket.work_item_id,
                    why,
                )
                continue
            self._unread.discard(ticket.work_item_id)
            reason = closing_reason(item, self._agent_id())
            if reason is not None:
                await asyncio.to_thread(close_ticket, ticket, reason, item)
                parts.append(
                    f"closed ticket task {ticket.task_id} instead of offering it again "
                    f"({ticket.work_item_id}): {reason}"
                )
                continue
            self._reoffered.add(ticket.work_item_id)
            await asyncio.to_thread(sweep.forget_declined, ticket.work_item_id)
            if await self._offer(ticket) == sweep.OFFER_PENDING:
                parts.append(
                    f"offered ticket task {ticket.task_id} again after a missed turn "
                    f"({ticket.work_item_id})"
                )
        return parts

    def _read_work_item(self, ticket: Ticket) -> dict[str, Any] | None:
        return papaya_events.read_work_item(ticket.event(), environ=self._papaya_env())

    def _agent_id(self) -> str:
        """The id of the agent this runtime's connection speaks for: the listener's own,
        else the pinned connection (`papaya.identity`). Never a handle or a name."""
        agent_config = getattr(self._built, "agent_config", None) or {}
        found = str(agent_config.get("agent_id") or "")
        if not found:
            own = getattr(self._runner, "_own_agent_id", None)
            found = str((own() if own is not None else None) or "")
        return found

    async def _offer(self, ticket: Ticket) -> str | dict[str, Any]:
        """Offer a ticket back to the loop, on its own task. A refusal returns its holder."""
        loop = getattr(self._built, "loop", None)
        if loop is None:
            return "done"
        self._watch_refusals(loop)
        reclaim = getattr(self._runner, "reclaim", None)
        if reclaim is not None:
            # History is what the ledger holds *now*, at the offer; whatever the worker
            # reports before the hold gets round to starting is news for the ticket.
            mark = await asyncio.to_thread(serve._max_event_id)
            reclaim(ticket.work_item_id, ticket.task_id, mark)
        agent_config = getattr(self._built, "agent_config", None) or {}
        envelope = sweep.envelope_for(
            {"id": ticket.work_item_id, "title": ticket.title},
            agent_id=str(agent_config.get("agent_id") or ""),
            workspace_id=str(agent_config.get("workspace_id") or ""),
        )
        outcome = await self._send_offer(loop, ticket.subject, envelope)
        if outcome != sweep.OFFER_PENDING:
            forget = getattr(self._runner, "forget_reclaim", None)
            if forget is not None:
                forget(ticket.work_item_id)
        return outcome

    async def _send_offer(
        self, loop: Any, subject: str, envelope: dict[str, Any]
    ) -> str | dict[str, Any]:
        """``loop.offer`` for one subject: its answer, or the holder a refusal named."""
        self._refused.pop(subject, None)
        try:
            status = await loop.offer(envelope)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad offer must not end the round
            log.warning("[rounds] Could not offer %s: %s", subject, exc)
            status = OFFER_FAILED
        refused = self._refused.pop(subject, None)
        if refused is not None and status != sweep.OFFER_PENDING:
            return refused
        return status

    async def _reclaim_instructions(self) -> list[str]:
        """Take back up every request this process lost to its shutdown or a crash.

        First Papaya's word on it (`papaya_events.read_instruction_status`): one Papaya
        has closed (answered, failed, cancelled, never picked up) is closed here without
        a word; one that cannot be read is left for the next round. An open one is
        offered to the loop as the `machine.instruction` it came as: reserved again, the
        hold lands on the same ticket and resumes from its state (its worker, what it
        already said). Only a reserve Papaya refused (`SubjectHeld`, with its holder) is
        told to the person at the origin as not finished, with what it waited on, and
        closed. Anything else — a bare `done` (already running here, a playbook or scope
        skip), a busy loop, an offer that raised — changes nothing and is looked at
        again next round.
        """
        loop = getattr(self._built, "loop", None)
        if loop is None:
            return []
        self._watch_refusals(loop)
        agent_config = getattr(self._built, "agent_config", None) or {}
        parts: list[str] = []
        for request in await store.run_in_thread(unfinished_instructions):
            subject = request.instruction.subject
            if subject in self._running() or request.task_id in self._held_ids():
                continue
            named = instructions.named(request.instruction)
            status = await asyncio.to_thread(self._instruction_status, request)
            if status is None:
                continue
            if status not in papaya_events.INSTRUCTION_OPEN:
                why = (
                    f"Papaya has no record of it ({NOT_FOUND_AFTER} reads in a row)"
                    if status == NOT_FOUND
                    else f"Papaya has it {status}"
                )
                if status == NOT_FOUND:
                    log.warning(
                        "[rounds] Closing ticket task %d for request %s: %s",
                        request.task_id,
                        named,
                        why,
                    )
                await store.run_in_thread(
                    functools.partial(
                        close_instruction,
                        ticket=request,
                        why=why,
                        environ=self._papaya_env(),
                        post=None,
                        report=None,
                        tell=False,
                    )
                )
                self._not_found.pop(request.task_id, None)
                parts.append(f"closed ticket task {request.task_id} for request {named}: {why}")
                continue
            envelope = request.envelope(
                agent_id=str(agent_config.get("agent_id") or ""),
                workspace_id=str(agent_config.get("workspace_id") or ""),
            )
            outcome = await self._send_offer(loop, subject, envelope)
            if outcome == sweep.OFFER_PENDING:
                parts.append(f"took request {named} back up (ticket task {request.task_id})")
                continue
            if not isinstance(outcome, dict):
                # Nothing refused it: already running (a race with the stream), a skip,
                # a busy loop or an offer that raised. The next round looks again.
                if subject not in self._running() and request.task_id not in self._held_ids():
                    log.info(
                        "[rounds] Offering request %s back answered %s; trying again next round",
                        named,
                        outcome,
                    )
                continue
            await store.run_in_thread(
                functools.partial(
                    close_instruction,
                    ticket=request,
                    why=(
                        "this machine restarted while working on it and could not take it back up"
                    ),
                    environ=self._papaya_env(),
                    post=self._post_seam(),
                    report=self._report_seam(),
                )
            )
            parts.append(
                f"could not take request {named} back up (held by "
                f"{sweep.holder_name(outcome)}): told the person and closed ticket task "
                f"{request.task_id}"
            )
        return parts

    def _instruction_status(self, request: InstructionTicket) -> str | None:
        """Papaya's status for a request, or ``None``: not connected, or not readable now.

        A 404 is read again, quietly, and only :data:`NOT_FOUND_AFTER` in a row make it
        :data:`NOT_FOUND`: Papaya has no such request, so nothing will ever answer it.
        """
        read = self._read_instruction or functools.partial(
            papaya_events.read_instruction_status,
            **(
                {"opener": getattr(self._runner, "_opener", None)}
                if getattr(self._runner, "_opener", None) is not None
                else {}
            ),
        )
        try:
            status = read(request.instruction.reply, environ=self._papaya_env())
        except papaya_events.PapayaHTTPError as exc:
            if exc.code != 404:
                return self._unread_request(request, exc)
            count = self._not_found.get(request.task_id, 0) + 1
            self._not_found[request.task_id] = count
            if count >= NOT_FOUND_AFTER:
                return NOT_FOUND
            log.debug("[rounds] Request %d is not found at Papaya (%d)", request.task_id, count)
            return None
        except Exception as exc:  # noqa: BLE001 - an unread request waits for the next round
            return self._unread_request(request, exc)
        self._not_found.pop(request.task_id, None)
        return status

    def _unread_request(self, request: InstructionTicket, exc: Exception) -> None:
        # Anything but a 404 breaks a run of them: "not found" means in a row.
        self._not_found.pop(request.task_id, None)
        log.warning(
            "[rounds] Could not read request %s from Papaya; trying again next round: %s",
            instructions.named(request.instruction),
            exc,
        )
        return None

    def _post_seam(self) -> Callable[..., str | None] | None:
        """How the rounds post at an instruction's origin: their seam, else the runner's."""
        if self._instruction_post is not None:
            return self._instruction_post
        post = getattr(self._runner, "_instruction_post", None)
        if post is not None:
            return post
        opener = getattr(self._runner, "_opener", None)
        if opener is not None:
            return functools.partial(papaya_events.post_instruction_reply, opener=opener)
        return None

    def _report_seam(self) -> Callable[..., bool] | None:
        if self._instruction_report is not None:
            return self._instruction_report
        report = getattr(self._runner, "_instruction_report", None)
        if report is not None:
            return report
        opener = getattr(self._runner, "_opener", None)
        if opener is not None:
            return functools.partial(papaya_events.report_instruction_result, opener=opener)
        return None

    def _watch_refusals(self, loop: Any) -> None:
        """Hear every `SubjectHeld` a reclaim's reserve raises: somebody else has the ticket."""
        events = getattr(loop, "_events", None)
        reserve = getattr(events, "reserve", None)
        if reserve is None or events is self._watched:
            return
        from papaya_agent_client.api_client import SubjectHeld

        refused = self._refused

        async def watched_reserve(subject: str, *args: Any, **kwargs: Any) -> Any:
            try:
                return await reserve(subject, *args, **kwargs)
            except SubjectHeld as held:
                refused[subject] = dict(held.holder or {})
                raise

        events.reserve = watched_reserve
        self._watched = events

    # -- one held ticket ---------------------------------------------------------

    async def _look_at(self, ticket: serve.Ticket, now: datetime) -> list[str]:
        parts: list[str] = []
        task_id = ticket.held.task_id
        records = await asyncio.to_thread(round_records, task_id)
        parts += await self._person_wait(ticket, now, records)
        worker = ticket.worker
        if worker is None or ticket.phase not in (serve.PHASE_DISPATCHED, serve.PHASE_BLOCKED):
            return parts
        waits = await asyncio.to_thread(worker_budgets, worker.repo)
        quiet_after = timedelta(seconds=waits.quiet_seconds)
        look = await asyncio.to_thread(
            look_at_worker, worker.task_id, now=now, quiet_after=quiet_after
        )
        if look is None:
            return parts
        busy = ticket.turn_running is not None or ticket.trigger is not None
        queued = {nudge.kind for nudge in ticket.nudges}
        gate_now = await asyncio.to_thread(self._gate, worker.task_id)
        if gate_now.running:
            # A gate in progress (or queued behind another) under the supervisor is not
            # silence, and a worker waiting on it is not to be nudged: its result lands
            # on the record.
            return parts

        if look.verdict == "dead" and look.latest_phase != "done":
            if (look.silent_seconds or 0) >= DEAD_GRACE_SECONDS:
                detail = "its session is gone: no runner process is left and no done note was filed"
                await asyncio.to_thread(record_worker_stopped, worker.task_id, detail)
                serve._report_progress(
                    ticket.job,
                    ticket.phase,
                    f"Worker task {worker.task_id}'s session is gone; recorded it as stopped.",
                )
                parts.append(f"worker task {worker.task_id}'s session is gone: recorded as stopped")
            return parts
        if busy:
            return parts

        answer = self._question(look)
        if answer is None and "answer" not in queued:
            # A capability the worker asked for is the manager's to decide, in the answer
            # turn: nobody else is asked unless that turn escalates it. The worker keeps
            # working meanwhile, so the rest of the round still looks at it.
            request = await asyncio.to_thread(supervision.undecided_capability, worker.task_id)
            if request is not None and not _done_before(
                records, "answer", worker_task_id=worker.task_id, event_id=request[0]
            ):
                event_id, question = request
                reason = f"worker task {worker.task_id} asked for a capability"
                ticket.nudges.append(
                    serve.Nudge("capability", reason, detail=question, event_id=event_id)
                )
                await asyncio.to_thread(
                    record_round,
                    task_id,
                    "answer",
                    worker_task_id=worker.task_id,
                    event_id=event_id,
                )
                parts.append(f"{reason}: deciding it")
                return parts
        if answer is not None and "answer" not in queued:
            event_id, question = answer
            if not _done_before(
                records, "answer", worker_task_id=worker.task_id, event_id=event_id
            ):
                reason = f"worker task {worker.task_id} is waiting on an answer"
                ticket.nudges.append(
                    serve.Nudge("answer", reason, detail=question, event_id=event_id)
                )
                await asyncio.to_thread(
                    record_round,
                    task_id,
                    "answer",
                    worker_task_id=worker.task_id,
                    event_id=event_id,
                )
                parts.append(f"{reason}: answering")
            return parts

        if look.status == serve.WORKER_STOPPED and look.stopped and "stopped" not in queued:
            event_id, detail = look.stopped
            if not _done_before(
                records, "stopped", worker_task_id=worker.task_id, event_id=event_id
            ) and await self._stopped_without_gate(worker.task_id):
                ticket.nudges.append(
                    serve.Nudge(
                        "stopped",
                        f"worker task {worker.task_id} stopped short",
                        detail=detail,
                        event_id=event_id,
                    )
                )
                await asyncio.to_thread(
                    record_round,
                    task_id,
                    "stopped",
                    worker_task_id=worker.task_id,
                    event_id=event_id,
                )
                parts.append(f"worker task {worker.task_id} stopped short: taking it up")
            return parts

        if look.status != "in_progress" or look.verdict is None or "checkin" in queued:
            return parts
        # The forge is asked every round, so a push is on the record within a round of
        # landing, whatever else this worker is doing.
        push = await asyncio.to_thread(self._pushed, worker.task_id)
        records = await self._saw_push(task_id, worker.task_id, push, now, records)
        steers = await asyncio.to_thread(person_steers, worker.task_id)
        if self._person_has_it(look, steers, records, now, waits):
            # A person at a session just gave this worker direction. A check-in now
            # would second-guess it before the worker has even answered.
            return parts
        due = self._checkins_due(look, now, records, waits, push)
        if not due:
            return parts
        reason = "; ".join(why for _trigger, why in due)
        facts = await asyncio.to_thread(self._checkin_facts, worker, look, gate_now, steers)
        reminder = self._plan_reminder(look, now, waits)
        if reminder is not None:
            facts = (*facts, ("plan note", reminder))
        seen: dict[str, str | None] = {}
        if push is not None and any(trigger == "push" for trigger, _why in due):
            seen = push_record(push, records, look)
            facts = (*facts, *push_facts(seen))
        ticket.nudges.append(
            serve.Nudge(
                "checkin",
                reason,
                trigger=",".join(trigger for trigger, _why in due),
                facts=facts,
                record=tuple(seen.items()),
            )
        )
        for trigger, why in due:
            # A push check-in remembers what it saw and when, so the next one waits
            # another `push_by_minutes` unless something lands meanwhile.
            pushed = {"at": now.isoformat(), **seen} if trigger == "push" else {}
            await asyncio.to_thread(
                record_round,
                task_id,
                "checkin",
                worker_task_id=worker.task_id,
                trigger=trigger,
                reason=why,
                after_event_id=look.last_event_id,
                **pushed,
            )
        parts.append(f"checking in on worker task {worker.task_id} ({reason})")
        return parts

    async def _stopped_without_gate(self, worker_task_id: int) -> bool:
        """A stopped worker whose branch is ahead of base and has no gate recorded at head.

        That is the one the runner's existing gate steer (`ppy gate run`, task 263) is
        for; a green or red record at head was already decided on when it stopped.
        """
        from papaya_agent_runtime import gate

        if not await asyncio.to_thread(self._branch_ahead, worker_task_id):
            return False
        try:
            recorded = await asyncio.to_thread(self._gate_verdict, worker_task_id)
        except Exception:  # noqa: BLE001 - an unreadable record is no record
            return True
        return getattr(recorded, "state", gate.NONE) == gate.NONE

    @staticmethod
    def _question(look: WorkerLook) -> tuple[int, str] | None:
        """The question a worker is waiting on and nobody has acted on since, if any."""
        if look.status == "blocked" and look.question is not None:
            event_id, question = look.question
            return (event_id, question) if look.last_acted_id < event_id else None
        if look.status == "in_progress" and look.progress:
            event_id, _at, _phase, note = look.progress[-1]
            if note.strip().endswith("?") and look.last_acted_id < event_id:
                return event_id, note.strip()
        return None

    @staticmethod
    def _checkins_due(
        look: WorkerLook,
        now: datetime,
        records: list[tuple[int, dict[str, Any]]],
        waits: WorkerBudgets | None = None,
        push: PushState | None = None,
    ) -> list[tuple[str, str]]:
        """The shared decision (`supervision.checkins_due`); a session reads the same."""
        return supervision.checkins_due(look, now, records, waits, push)

    @staticmethod
    def _plan_reminder(
        look: WorkerLook, now: datetime, waits: WorkerBudgets | None = None
    ) -> str | None:
        """The shared line (`supervision.plan_reminder`)."""
        return supervision.plan_reminder(look, now, waits)

    async def _saw_push(
        self,
        ticket_task_id: int,
        worker_task_id: int,
        push: PushState | None,
        now: datetime,
        records: list[tuple[int, dict[str, Any]]],
    ) -> list[tuple[int, dict[str, Any]]]:
        """Record a forge tip no round has seen for this worker yet; the records after."""
        if push is None or push.remote_sha is None:
            return records
        if last_push_at(records, worker_task_id, push.remote_sha) is not None:
            return records
        details = {
            "worker_task_id": worker_task_id,
            "remote_sha": push.remote_sha,
            "head_sha": push.head_sha,
            "at": now.isoformat(),
        }
        await asyncio.to_thread(record_round, ticket_task_id, PUSH_SEEN, **details)
        return [*records, (0, {"task_id": ticket_task_id, "action": PUSH_SEEN, **details})]

    @staticmethod
    def _person_has_it(
        look: WorkerLook,
        steers: list[dict[str, Any]],
        records: list[tuple[int, dict[str, Any]]],
        now: datetime,
        waits: WorkerBudgets,
    ) -> bool:
        """The shared decision (`supervision.person_has_it`)."""
        return supervision.person_has_it(look, steers, records, now, waits)

    @staticmethod
    def _checkin_facts(
        worker: serve.Worker,
        look: WorkerLook,
        gate_now: GateState,
        steers: list[dict[str, Any]] | None = None,
    ) -> tuple[tuple[str, str], ...]:
        log_lines = "\n".join(
            f"{at} [{phase}] {note}".rstrip() for _id, at, phase, note in look.progress
        )
        person = "\n".join(f"{s['at']} {s['kind']}: {s['message']}".rstrip() for s in steers or [])
        return (
            (
                "direction a person gave this worker from a session (by: person), oldest first; "
                "it stands unless the record shows it is wrong",
                person or "(none)",
            ),
            (
                "the brief's Goals",
                brief_goals(worker) or "(the archived brief has no Goals section)",
            ),
            (
                "the worker's full progress log, oldest first",
                log_lines or "(no progress reported yet)",
            ),
            ("worker session", "alive" if look.verdict in ("alive", "quiet") else "not running"),
            ("silent for", health.humanize(look.silent_seconds)),
            (
                "gate running",
                gate_now.line if gate_now.running else f"no; {gate_now.line}",
            ),
        )

    async def _person_wait(
        self, ticket: serve.Ticket, now: datetime, records: list[tuple[int, dict[str, Any]]]
    ) -> list[str]:
        """A question that has waited fifteen minutes on a person is said on the ticket, once."""
        task_id = ticket.held.task_id
        wait = await asyncio.to_thread(person_wait_since, task_id)
        if wait is None:
            return []
        todo_id, question, since = wait
        if since is None or (now - since).total_seconds() < PERSON_WAIT_SECONDS:
            return []
        if _done_before(records, "waiting_on_you", todo_id=todo_id):
            return []
        await asyncio.to_thread(record_round, task_id, "waiting_on_you", todo_id=todo_id)
        # The outreach lane says the question on the ticket and in the DM the moment it
        # is recorded, and again on its clock; this status line is not a second comment.
        if not await asyncio.to_thread(_outreach_said, f"todo:{todo_id}"):
            await self._runner._say(
                ticket, f"waiting_on_you:{todo_id}", f"waiting on you: {question}"
            )
        if ticket.phase != serve.PHASE_BLOCKED:
            await self._runner._enter(ticket, serve.PHASE_BLOCKED, f"Waiting on you: {question}")
        else:
            serve._report_progress(ticket.job, ticket.phase, f"Still waiting on you: {question}")
        await self._runner._status(ticket, papaya_events.STATUS_BLOCKED)
        return [f"ticket task {task_id} has waited on a person for 15m: said so"]

    # -- pull requests -------------------------------------------------------------

    async def _pull_requests(self, now: datetime) -> list[str]:
        held = self._held_ids()
        tickets = {t.run_id: t for t in await asyncio.to_thread(ticket_tasks)}
        try:
            entries = await asyncio.to_thread(self._forge_states)
        except Exception as exc:  # noqa: BLE001 - an unreadable forge is a quiet round
            log.warning("[rounds] Could not read pull requests: %s", exc)
            return []
        by_worker = {int(e["task_id"]): e for e in entries if e.get("known") and e.get("pr")}
        # One line per pull request this round changed, keyed by worker.
        said: dict[int, str] = {}
        await self._finish_attempts(by_worker, now)
        #: Worker -> the fingerprint its pull request has this round, for the lane.
        current: dict[int, str] = {}
        #: Workers the ticket path took up this round: not the repair step's too.
        covered: set[int] = set()
        for entry in entries:
            if not entry.get("known") or entry.get("pr") is None:
                continue
            worker_id = int(entry["task_id"])
            if entry.get("status") != "delivered":
                continue
            await asyncio.to_thread(observe_pr, worker_id, entry)
            if entry.get("ci_seconds") is not None:
                await asyncio.to_thread(
                    observe_ci, worker_id, float(entry["ci_seconds"]), str(entry.get("ci") or "")
                )
            ticket = await asyncio.to_thread(self._ticket_of, worker_id, tickets)
            if ticket is not None and ticket.task_id in held:
                continue
            if entry.get("merged") is True or entry.get("state") == "MERGED":
                # Ticket or not, handed over or not: the same follow-up a session's
                # heartbeat makes, and the merge on the record so nothing asks again.
                lines = await self._merged(ticket, worker_id, entry, now)
                if lines:
                    said[worker_id] = "; ".join(lines)
                continue
            # A pull request whose ticket this process can offer back to the loop goes
            # through that ticket's review turn (attention sets it `dispatched`, and the
            # reclaim above takes it up). Every other one — no ticket, a ticket done or
            # handed over, or anything standalone with no loop to offer to — is the
            # repair step's below, the same one a session's heartbeat runs
            # (`supervision.repair_untracked`), so every delivered pull request is
            # somebody's and none is two people's (#72).
            if ticket is None or self._standalone():
                continue
            if ticket.phase in (serve.PHASE_DONE, serve.PHASE_HANDED_OVER):
                continue
            covered.add(worker_id)
            if entry.get("state") != "OPEN":
                continue
            line = await self._follow(ticket, worker_id, entry, now, current)
            if line:
                said[worker_id] = line
        for worker_id, line in (
            await self._start_attempts(tickets, by_worker, current, now)
        ).items():
            said[worker_id] = line
        lines = await asyncio.to_thread(
            supervision.repair_untracked,
            entries,
            now,
            steer=self._steer_worker,
            pr_details=self._pr_details,
            covered=covered,
        )
        return [line for line in said.values() if line] + lines

    async def _follow(
        self,
        ticket: Ticket,
        worker_id: int,
        entry: dict[str, Any],
        now: datetime,
        current: dict[int, str],
    ) -> str | None:
        """Decide what one open, delivered pull request needs this round.

        Its reasons and head make a fingerprint. A new fingerprint is queued for the
        reconcile lane; the same one is not raised again, unless the lane's attempt
        at it failed, once. Two failures at one fingerprint are a person's. No reasons
        at all is the green-and-unmerged clock.
        """

        # The decision a session's heartbeat makes too (`supervision.pr_attention`).
        attention = await asyncio.to_thread(supervision.pr_attention, worker_id, entry, now)
        if attention.action == supervision.PR_GREEN:
            return await self._green(ticket, worker_id, entry, now)
        if not attention.reasons:
            return None
        reasons, fp = list(attention.reasons), attention.fingerprint
        current[worker_id] = fp
        summary = attention.summary
        if attention.action == supervision.PR_PERSON:
            await asyncio.to_thread(
                supervision.record_needs_a_person, worker_id, attention, entry, now
            )
            where = entry.get("url") or f"PR #{entry['pr']}"
            await asyncio.to_thread(
                _set_phase,
                ticket.task_id,
                serve.PHASE_NEEDS_A_PERSON,
                f"The reconcile lane could not fix {where} twice at the same head: {summary}",
            )
            head = str(entry.get("head") or "")[:8] or "its head"
            await self._post(
                ticket,
                f"{where} needs a person: the runtime tried to fix it twice at {head} and it "
                "still needs attention:\n" + "\n".join(f"- {r.text}" for r in reasons),
            )
            return f"worker task {worker_id}'s pull request needs a person: {summary}"
        if attention.action != supervision.PR_QUEUE:
            return None
        if entry.get("ci") == "fail":
            # Raised when the attention is, not when the lane gets to it.
            await asyncio.to_thread(self._runtime_ci_red, worker_id, ticket, entry)
        await asyncio.to_thread(
            supervision.record_queued, worker_id, attention, entry, now, ticket.task_id
        )
        return f"worker task {worker_id}: {summary} (queued for the reconcile lane)"

    async def _start_attempts(
        self,
        tickets: dict[int, Ticket],
        by_worker: dict[int, dict[str, Any]],
        current: dict[int, str],
        now: datetime,
    ) -> dict[int, str]:
        """Start queued pull requests while the reconcile lane has room, closest to merging first.

        Starting is the path attention always took: `pr_attention` on the worker, the
        ticket back to `dispatched`, and the reclaim later in this round offers it, so
        the review turn composes the steer. The steer's resume is admitted in the
        lane (the supervisor knows a delivered task's run is a reconciliation).
        """
        from papaya_agent_runtime import reconcile

        lines: dict[int, str] = {}
        busy = len(await asyncio.to_thread(reconcile.open_lane))
        room = await asyncio.to_thread(reconcile.reconcile_slots) - busy
        queue = reconcile.merge_readiness_order(await asyncio.to_thread(reconcile.pending_queue))
        for _event_id, queued in queue:
            worker_id = int(queued.get("task_id") or 0)
            # Only what this round saw still needs it: a newer push or a green run since
            # the queueing means that entry is stale, and a held ticket waits its turn.
            if current.get(worker_id) != queued.get("fingerprint"):
                continue
            ticket = next(
                (t for t in tickets.values() if t.task_id == queued.get("ticket_task_id")), None
            )
            if ticket is None:
                continue
            if room <= 0:
                where = queued.get("url") or f"PR #{queued.get('pr')}"
                lines.setdefault(
                    worker_id,
                    f"worker task {worker_id}: {queued.get('summary')} "
                    f"(queued for the reconcile lane; {where} waits its turn)",
                )
                continue
            room -= 1
            entry = by_worker.get(worker_id, {})
            try:
                details = await asyncio.to_thread(self._pr_details, worker_id, entry)
            except Exception as exc:  # noqa: BLE001 - details are a help, never a blocker
                log.warning("[rounds] Could not read details for task %d: %s", worker_id, exc)
                details = {}
            summary = str(queued.get("summary") or "")
            reasons = list(queued.get("reasons") or [])
            await asyncio.to_thread(
                record_pr_attention,
                worker_id,
                summary,
                reasons,
                fingerprint=queued.get("fingerprint"),
                head=queued.get("head"),
                pr=queued.get("pr"),
                url=queued.get("url"),
                base=queued.get("base"),
                threads=queued.get("threads") or [],
                comments=queued.get("comments") or [],
                **details,
            )
            await asyncio.to_thread(
                reconcile.record,
                worker_id,
                reconcile.STARTED,
                fingerprint=queued.get("fingerprint"),
                head=queued.get("head"),
                pr=queued.get("pr"),
                url=queued.get("url"),
                ticket_task_id=ticket.task_id,
                at=now.isoformat(),
            )
            await asyncio.to_thread(
                _set_phase,
                ticket.task_id,
                serve.PHASE_DISPATCHED,
                f"Worker task {worker_id}'s pull request needs attention: {summary}",
            )
            lines[worker_id] = f"worker task {worker_id}: {summary}"
        return lines

    async def _finish_attempts(self, by_worker: dict[int, dict[str, Any]], now: datetime) -> None:
        """Close every lane attempt that has stopped running, with how it ended."""
        from papaya_agent_runtime import reconcile

        for attempt in await asyncio.to_thread(reconcile.open_lane):
            over = await asyncio.to_thread(reconcile.attempt_over, attempt)
            if over is None:
                continue
            entry = by_worker.get(attempt.worker_task_id)
            if over == reconcile.OUTCOME_MERGED or (
                entry is not None
                and (entry.get("merged") is True or entry.get("state") == "MERGED")
            ):
                outcome = reconcile.OUTCOME_MERGED
            elif entry is None:
                # The forge was read this round and does not list this pull request at
                # all — merged, closed, or dropped from the watch. It never will again,
                # so the attempt is closed on what is known rather than holding a slot.
                outcome = reconcile.OUTCOME_ENDED
            elif not entry.get("head"):
                continue  # the forge cannot say where the branch is; judge next round
            elif entry.get("head") != attempt.payload.get("head"):
                outcome = reconcile.OUTCOME_FIXED
            else:
                outcome = reconcile.OUTCOME_FAILED
            # Judging it may have closed it (an adopted merge frees the lane as it is
            # written down), and a hand-run `ppy deliver --merged` can land in the gap.
            if not await asyncio.to_thread(reconcile.attempt_is_open, attempt.started_event_id):
                continue
            await asyncio.to_thread(
                reconcile.record,
                attempt.worker_task_id,
                reconcile.FINISHED,
                started_event_id=attempt.started_event_id,
                fingerprint=attempt.payload.get("fingerprint"),
                head=attempt.payload.get("head"),
                outcome=outcome,
                at=now.isoformat(),
            )

    async def _green(
        self, ticket: Ticket, worker_id: int, entry: dict[str, Any], now: datetime
    ) -> str | None:
        """Green, mergeable, nobody asking for changes, and nobody merging: also a state.

        The clock starts the first round that sees it green at its head. Past
        `delivery.merge_after_hours`, the ticket says so once, or — on a repository
        that opted into `auto_merge` — the runtime merges and the ticket is done.
        """
        from papaya_agent_runtime import reconcile

        records = await asyncio.to_thread(round_records, ticket.task_id)
        head = entry.get("head")
        hours = await asyncio.to_thread(reconcile.merge_after_hours)
        auto, method = await asyncio.to_thread(reconcile.merge_policy, worker_id)
        # The decision a session's heartbeat makes too (`supervision.green_clock`).
        decision = supervision.green_clock(
            worker_id, entry, records, now, hours=hours, auto_merge=auto
        )
        if decision == supervision.GREEN_RECORD:
            await asyncio.to_thread(
                record_round,
                ticket.task_id,
                "green",
                worker_task_id=worker_id,
                head=head,
                at=now.isoformat(),
            )
            return None
        if decision == supervision.GREEN_NOTHING:
            return None
        where = entry.get("url") or f"PR #{entry['pr']}"
        if decision == supervision.GREEN_MERGE:
            result = await asyncio.to_thread(self._merge, worker_id, entry, method)
            if getattr(result, "merged", False):
                merged = {**entry, "merged": True, "state": "MERGED"}
                return "; ".join(await self._merged(ticket, worker_id, merged, now))
            await asyncio.to_thread(
                record_round,
                ticket.task_id,
                "merge_failed",
                worker_task_id=worker_id,
                head=head,
                detail=getattr(result, "detail", ""),
            )
            return (
                f"worker task {worker_id}: could not merge {where}: {getattr(result, 'detail', '')}"
            )
        await asyncio.to_thread(
            record_round, ticket.task_id, "green_unmerged", worker_task_id=worker_id
        )
        span = "a day" if hours == 24 else f"{hours} hours"
        await self._post(
            ticket, f"PR {entry['pr']} has been green and unmerged for {span}: {where}"
        )
        return f"worker task {worker_id}'s {where} has been green and unmerged for {span}: said so"

    def _runtime_ci_red(self, worker_id: int, ticket: Ticket, entry: dict[str, Any]) -> None:
        """Red CI on a pull request the runtime delivered to its own repository."""
        try:
            runtime = self._runtime_repo()
            if not runtime:
                return
            conn = db.init_db()
            try:
                row = conn.execute(
                    "SELECT r.name, r.origin, r.forge_url FROM tasks t "
                    "JOIN repos r ON r.id = t.repo_id WHERE t.id = ?",
                    (worker_id,),
                ).fetchone()
            finally:
                conn.close()
            if row is None:
                return
            slugs = {
                deficiencies.github_slug(row["forge_url"]),
                deficiencies.github_slug(row["origin"]),
            }
            if runtime.lower() not in {s.lower() for s in slugs if s}:
                return
            failing = ", ".join(sorted(entry.get("failing") or [])) or "a check"
            deficiencies.record(
                deficiencies.RUNTIME_CI_RED,
                f"failing: {failing}",
                evidence={
                    "repo": row["name"],
                    "ticket": ticket.work_item_id,
                    "task_id": ticket.task_id,
                    "worker_task_id": worker_id,
                    "pr": entry.get("url") or f"#{entry.get('pr')}",
                },
            )
        except Exception as exc:  # noqa: BLE001 - reporting must never end a round
            log.warning("[rounds] Could not check worker task %d's repository: %s", worker_id, exc)

    def _forge_states(self) -> list[dict[str, Any]]:
        conn = db.init_db()
        try:
            return list(self._forge(conn))
        finally:
            conn.close()

    @staticmethod
    def _ticket_of(worker_id: int, tickets: dict[int, Ticket]) -> Ticket | None:
        conn = db.init_db()
        try:
            task = store.get_task(conn, worker_id)
        finally:
            conn.close()
        if task is None or task["phase"] is not None:
            return None
        return tickets.get(int(task["run_id"]))

    async def _merged(
        self, ticket: Ticket | None, worker_id: int, entry: dict[str, Any], now: datetime
    ) -> list[str]:
        """Follow up a merged pull request once (`supervision.merged_step`), then clean up.

        The merge is recorded on the worker first (`delivery.record_merged`, what a
        session's heartbeat does on observing one), so the task reads delivered at its
        merge commit and the forge is never asked about it again. With no ticket there is
        nothing to say and nowhere to say it: the record and the clean-up are the follow-up.
        """
        loop = asyncio.get_running_loop()

        def post(target: Ticket, body: str, status: str | None) -> None:
            asyncio.run_coroutine_threadsafe(self._post(target, body, status=status), loop).result()

        recorded = await asyncio.to_thread(supervision.record_merge, worker_id, entry)
        parts = await asyncio.to_thread(supervision.merged_step, [entry], post=post)
        if ticket is None and recorded:
            parts.append(f"worker task {worker_id}'s pull request merged: recorded, no ticket")
        if not parts:
            return []
        return parts + await self._hygiene(worker_id, now)

    async def _clean_if_merged(self, ticket: Ticket) -> list[str]:
        """A ticket handed over whose pull request already merged: its worktree can go now."""
        try:
            entries = await asyncio.to_thread(self._forge_states)
        except Exception:  # noqa: BLE001 - unknown is not merged
            return []
        workers = await store.run_in_thread(workers_of, ticket)
        parts: list[str] = []
        for entry in entries:
            if int(entry.get("task_id") or 0) in workers and (
                entry.get("merged") is True or entry.get("state") == "MERGED"
            ):
                parts += await self._hygiene(int(entry["task_id"]), self._clock())
        return parts

    async def _post(self, ticket: Ticket, body: str, *, status: str | None = None) -> None:
        """Say one mechanical line on a ticket this process does not hold. Never fatal.

        Running without Papaya (no listener was built), nothing is attempted: the
        skipped steps are recorded on the ticket task instead.
        """
        if self._standalone():
            from papaya_agent_runtime import standalone

            steps = [f"comment: {body.splitlines()[0] if body else ''}"]
            if status is not None:
                steps.append(f"set the work item to {status}")
            await store.run_in_thread(
                standalone.record_skipped,
                ticket.task_id,
                "round",
                steps=steps,
                reason=standalone.NOT_CONNECTED,
            )
            return
        env = self._papaya_env()
        opener = getattr(self._runner, "_opener_kwargs", lambda: {})()
        event = ticket.event()
        try:
            if status is not None:
                await asyncio.to_thread(
                    papaya_events.set_work_item_status, event, status, environ=env, **opener
                )
            await asyncio.to_thread(
                papaya_events.post_work_item_comment, event, body, environ=env, **opener
            )
        except papaya_events.PapayaEventError as exc:
            log.warning("[rounds] Could not update %s: %s", ticket.subject, exc)

    def _env_from_connection(self) -> dict[str, str]:
        """The API url, workspace and token a job would have been given, from the connection."""
        agent_config = getattr(self._built, "agent_config", None) or {}
        api = getattr(self._built, "api", None)
        server = str((getattr(api, "config", None) or {}).get("server_url") or "")
        from papaya_agent_runtime.manager.launch import AGENT_REF_ENV

        return {
            "PAPAYA_API_URL": os.environ.get("PAPAYA_API_URL") or server,
            "PAPAYA_WORKSPACE_ID": str(agent_config.get("workspace_id") or ""),
            "PAPAYA_AGENT_TOKEN": str(agent_config.get("client_token") or ""),
            # A turn keyed on a task asks the client for its tools by this ref.
            AGENT_REF_ENV: str(getattr(self._built, "agent_ref", "") or ""),
        }

    # -- hygiene -------------------------------------------------------------------

    async def _hygiene(self, task_id: int | None, now: datetime) -> list[str]:
        """Worktree hygiene (`supervision.hygiene_step`); a session's heartbeat runs it too."""
        loop = asyncio.get_running_loop()

        def post(target: Ticket, body: str, status: str | None) -> None:
            asyncio.run_coroutine_threadsafe(self._post(target, body, status=status), loop).result()

        return await asyncio.to_thread(
            supervision.hygiene_step,
            task_id,
            now,
            prune=self._prune,
            git=self._git,
            post=post,
            kept_runs=self._kept_runs,
        )

    async def _loose_ends(self, kept: list[dict[str, Any]], now: datetime) -> list[str]:
        """Kept slots a person must settle: part of `supervision.hygiene_step` now."""
        return []


def _default_gate_verdict(task_id: int) -> Any:
    from papaya_agent_runtime import gate

    return gate.verdict(task_id)


def _default_pr_details(worker_task_id: int, entry: dict[str, Any]) -> dict[str, Any]:
    from papaya_agent_runtime import reconcile

    return reconcile.pr_details(worker_task_id, entry)


def _default_merge(worker_task_id: int, entry: dict[str, Any], method: str) -> Any:
    from papaya_agent_runtime import delivery

    return delivery.merge_pull_request(
        worker_task_id, str(entry.get("url") or entry.get("pr")), method
    )


def _default_forge(conn: Any) -> list[dict[str, Any]]:
    from papaya_agent_runtime import watch

    return watch.pr_states(conn, conversation=True)


__all__ = [
    "DEAD_GRACE_SECONDS",
    "DEFAULT_ROUNDS_INTERVAL",
    "HYGIENE_EVENT",
    "HYGIENE_EVERY_SECONDS",
    "KEPT_LOOSE_END_SECONDS",
    "KEPT_SUMMARY_RUNS",
    "PERSON_WAIT_SECONDS",
    "PUSH_SEEN",
    "ROUNDS_INTERVAL_ENV",
    "ROUND_EVENT",
    "PushState",
    "Rounds",
    "WorkerLook",
    "interval_from_env",
    "last_push_at",
    "look_at_worker",
    "push_facts",
    "push_record",
    "push_state",
    "record_worker_stopped",
    "ticket_tasks",
]
