"""The manager's rounds: `ppy serve` looks at every worker and every loose end on a clock.

The runner reacts: a worker's question starts the answer turn, `worker_done` starts
the review. Nothing it does happens *because time passed*, so a worker that goes
silent, wanders off the brief, or waits on a person nobody told stays that way until
somebody at a terminal runs `ppy health`. The rounds are that somebody. Every
`rounds_interval` (five minutes; `--rounds-interval`, `$PPY_ROUNDS_INTERVAL`, or
`health.rounds_interval`) a round walks the board, in this order:

1. **Pull requests.** For each delivered worker whose ticket is not held: a merged
   pull request moves the ticket to `done` with one comment (and its worktree is
   cleaned up at once); red CI or a review asking for changes is recorded on the
   worker as `pr_attention` and the ticket goes back to `dispatched`, so step 2
   takes it up and the review turn steers the worker with the failure attached.
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
3. **Every held ticket's worker** (facts from :mod:`papaya_agent_runtime.health`):
   a dead session with no done note is recorded as `worker_stopped`, which the
   runner's existing path sends back to its gate; a question (status `blocked`, or
   a last progress note that asks one) gets the answer turn; a `worker_stopped`
   worker with a branch ahead of base and nothing in flight gets the stopped-short
   path; a live worker silent past its repository's silence budget, still planning
   past its plan budget, or first running for half its worker-session budget
   (:mod:`papaya_agent_runtime.budgets`; with no history, `health.quiet_minutes`,
   `health.plan_minutes` and `health.checkin_after`) gets the **check-in turn**; a
   person-wait older than fifteen minutes is said on the ticket once ("waiting on
   you: …") and the ticket is `blocked`.
4. **Hygiene**, at most once an hour: `ppy worktree prune`'s own rules, unattended
   (only terminal tasks, clean, every commit on a remote, base clone under
   `.ppy/repos`), then `git worktree prune` and `git fetch --prune` on the base
   clones. A kept slot that is a loose end — terminal, dirty or unpushed, a day old
   — becomes one "waiting on you" item.

The rule the whole module keeps: **the round is the clock and the facts; judgment
stays in turns.** A round never writes a message for a worker. It queues a
:class:`~papaya_agent_runtime.serve.Nudge` on the held ticket, and the ticket's own
loop runs the turn, so a round's turn never overlaps the ticket's others and every
"continue", "steer", "stop" and "answer" comes from a turn reading the record.

A round holds the lock the sweep holds, so the two never overlap. It says one
progress line per ticket whose state it changed and one stderr summary; a round
that found nothing writes nothing at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from papaya_agent_runtime import health, papaya_events, serve, sweep
from papaya_agent_runtime.state import db, store

log = logging.getLogger("papaya_agent_runtime.rounds")

#: How often `serve` does its rounds when nobody says otherwise: five minutes.
DEFAULT_ROUNDS_INTERVAL = 300.0

#: The environment variable that sets the interval when `--rounds-interval` does not.
ROUNDS_INTERVAL_ENV = "PPY_ROUNDS_INTERVAL"

#: How long a question may wait on a person before the ticket says so.
PERSON_WAIT_SECONDS = 15 * 60.0

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

#: The event kind every hygiene run records: what went, what stayed, and why.
HYGIENE_EVENT = "worktree_hygiene"

#: The hand-back reason the runner gives when a turn missed its job (PAP-213).
_TURN_MISSED = "the manager turn ended"

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
    finally:
        if conn is not None:
            conn.close()
    return WorkerBudgets(
        quiet_seconds=quiet.seconds,
        plan_seconds=plan.seconds,
        midpoint_seconds=session.seconds / 2,
        sources=(quiet.source, plan.source, session.source),
    )


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

    @property
    def latest_phase(self) -> str | None:
        return self.progress[-1][2] if self.progress else None

    def running_seconds(self, now: datetime) -> float:
        return (now - self.created_at).total_seconds() if self.created_at else 0.0


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
        for row in rows:
            kind, payload = str(row["kind"]), _payload(row)
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
        )
    finally:
        conn.close()


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


def gate_state(worker_task_id: int) -> GateState:
    """Read the worker's gate: the newest `gate_result` and the supervisor's `gate_wait`.

    A `gate_started` with no result after it names the gate's key; the supervisor
    is then asked (without waiting) whether that gate is still running. A start
    with no running gate behind it — the supervisor restarted, the gate was
    killed — is not running.
    """
    from papaya_agent_runtime import gate

    conn = db.init_db()
    try:
        rows = conn.execute(
            "SELECT kind, payload FROM events WHERE task_id = ? AND kind IN (?, ?, ?) "
            "ORDER BY id DESC",
            (worker_task_id, gate.GATE_STARTED, gate.GATE_RESULT, gate.GATE_KILLED),
        ).fetchall()
    finally:
        conn.close()
    last_result = next((_payload(r) for r in rows if r["kind"] == gate.GATE_RESULT), None)
    line = (
        gate._result_from(last_result).line()
        if last_result is not None
        else "no gate result recorded for this worker"
    )
    if not rows or rows[0]["kind"] != gate.GATE_STARTED:
        return GateState(False, line)
    started = _payload(rows[0])
    scope = "full" if started.get("full") else "local"
    key = f"task:{worker_task_id}:{scope}:{started.get('head_sha') or ''}"
    try:
        from papaya_agent_runtime.supervisor.client import SupervisorClient

        answer = SupervisorClient().gate_wait(key, timeout=0.0)
    except Exception:  # noqa: BLE001 - no supervisor to ask means nothing runs under it
        return GateState(False, line)
    if not answer.get("ok") or not answer.get("running"):
        return GateState(False, line)
    seconds = float(answer.get("elapsed") or 0)
    command = str(answer.get("command") or started.get("command") or "the gate")
    return GateState(
        True,
        f"running under the supervisor for {health.humanize(int(seconds))}: `{command}`; {line}",
        command=command,
        elapsed_seconds=seconds,
        full=bool(started.get("full")),
    )


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
    """The tickets a new offer would resume: work was under way and nobody gave it away."""
    from papaya_agent_runtime.lifecycle import TERMINAL_STATUSES

    conn = db.init_db()
    try:
        return [
            t
            for t in tickets
            if t.status not in TERMINAL_STATUSES and serve.resumable_phase(conn, t.task_id)
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


def pr_attention_since_delivery(worker_task_id: int) -> bool:
    """Has this worker's pull request already been flagged since it was last delivered?"""
    conn = db.init_db()
    try:
        row = conn.execute(
            "SELECT MAX(CASE WHEN kind = 'delivered' THEN id END) AS delivered, "
            "MAX(CASE WHEN kind = ? THEN id END) AS flagged FROM events WHERE task_id = ?",
            (serve.PR_ATTENTION, worker_task_id),
        ).fetchone()
        return bool(row["flagged"]) and int(row["flagged"]) > int(row["delivered"] or 0)
    finally:
        conn.close()


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


def record_pr_attention(worker_task_id: int, summary: str, reasons: list[str]) -> None:
    conn = db.init_db()
    try:
        task = store.get_task(conn, worker_task_id)
        store.append_event(
            conn,
            kind=serve.PR_ATTENTION,
            payload={"task_id": worker_task_id, "summary": summary, "reasons": reasons},
            run_id=int(task["run_id"]) if task is not None else None,
            task_id=worker_task_id,
        )
    finally:
        conn.close()


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
    (hygiene), ``branch_ahead`` and ``papaya_env`` (the credentials a comment
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
    ) -> None:
        self._built = built
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
        #: Subjects whose reserve Papaya refused during a reclaim, with the holder.
        self._refused: dict[str, dict[str, Any]] = {}
        self._watched: Any = None
        #: Work items this process already re-offered after a missed turn.
        self._reoffered: set[str] = set()
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
                log.warning("[rounds] Round failed: %s", exc)
                parts = [f"the round failed: {exc}"]
        if parts:
            line = "round: " + "; ".join(parts)
            self.summaries.append(line)
            if self._stderr is not None:
                with contextlib.suppress(Exception):
                    print(f"ppy serve: {line}", file=self._stderr, flush=True)
        return parts

    async def _start(self) -> list[str]:
        tickets = await asyncio.to_thread(ticket_tasks)
        parts = await self._reclaim(tickets)
        parts += await self._reoffer_missed(tickets)
        return parts

    async def _round(self) -> list[str]:
        now = self._clock()
        parts = await self._pull_requests(now)
        parts += await self._reclaim(await asyncio.to_thread(ticket_tasks))
        for ticket in list(getattr(self._runner, "held", {}).values()):
            parts += await self._look_at(ticket, now)
        if self._last_hygiene is None or (
            (now - self._last_hygiene).total_seconds() >= HYGIENE_EVERY_SECONDS
        ):
            self._last_hygiene = now
            parts += await self._hygiene(None, now)
        return parts

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
            self._reoffered.add(ticket.work_item_id)
            await asyncio.to_thread(sweep.forget_declined, ticket.work_item_id)
            if await self._offer(ticket) == sweep.OFFER_PENDING:
                parts.append(
                    f"offered ticket task {ticket.task_id} again after a missed turn "
                    f"({ticket.work_item_id})"
                )
        return parts

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
        self._refused.pop(ticket.subject, None)
        try:
            status = await loop.offer(envelope)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad offer must not end the round
            log.warning("[rounds] Could not offer %s: %s", ticket.subject, exc)
            status = "done"
        refused = self._refused.pop(ticket.subject, None)
        if status != sweep.OFFER_PENDING:
            forget = getattr(self._runner, "forget_reclaim", None)
            if forget is not None:
                forget(ticket.work_item_id)
        if refused is not None and status != sweep.OFFER_PENDING:
            return refused
        return status

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
            # A gate in progress under the supervisor is not silence, and a worker
            # waiting on it is not to be nudged: its result lands on the record.
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
        due = self._checkins_due(look, now, records, waits)
        if not due:
            return parts
        reason = "; ".join(why for _trigger, why in due)
        facts = await asyncio.to_thread(self._checkin_facts, worker, look, gate_now)
        ticket.nudges.append(
            serve.Nudge(
                "checkin",
                reason,
                trigger=",".join(trigger for trigger, _why in due),
                facts=facts,
            )
        )
        for trigger, why in due:
            await asyncio.to_thread(
                record_round,
                task_id,
                "checkin",
                worker_task_id=worker.task_id,
                trigger=trigger,
                reason=why,
                after_event_id=look.last_event_id,
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
    ) -> list[tuple[str, str]]:
        waits = waits or worker_budgets(None)
        due: list[tuple[str, str]] = []
        mine = [
            payload
            for _id, payload in records
            if payload.get("action") == "checkin" and payload.get("worker_task_id") == look.task_id
        ]
        running = look.running_seconds(now)
        if look.verdict == "quiet":
            episode = [p for p in mine if p.get("trigger") == "quiet"]
            if not any(int(p.get("after_event_id") or 0) >= look.last_event_id for p in episode):
                due.append(
                    (
                        "quiet",
                        f"silent for {health.humanize(look.silent_seconds)} "
                        "with its session still alive, past its "
                        f"{health.humanize(int(waits.quiet_seconds))} silence budget "
                        f"({waits.sources[0]})",
                    )
                )
        plan_after = waits.plan_seconds
        if (
            look.latest_phase in (None, "plan")
            and running >= plan_after
            and not any(p.get("trigger") == "plan" for p in mine)
        ):
            due.append(
                (
                    "plan",
                    f"still planning after {health.humanize(int(running))}, longer than "
                    f"{int(plan_after // 60)}m; it should commit to a plan or say what blocks it",
                )
            )
        checkin_after = waits.midpoint_seconds
        if running >= checkin_after and not any(p.get("trigger") == "midpoint" for p in mine):
            due.append(
                (
                    "midpoint",
                    f"running for {health.humanize(int(running))}: time to check it is still "
                    "heading where the brief asked",
                )
            )
        return due

    @staticmethod
    def _checkin_facts(
        worker: serve.Worker, look: WorkerLook, gate_now: GateState
    ) -> tuple[tuple[str, str], ...]:
        log_lines = "\n".join(
            f"{at} [{phase}] {note}".rstrip() for _id, at, phase, note in look.progress
        )
        return (
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
        await self._runner._say(ticket, f"waiting_on_you:{todo_id}", f"waiting on you: {question}")
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
        parts: list[str] = []
        for entry in entries:
            if not entry.get("known") or entry.get("pr") is None:
                continue
            worker_id = int(entry["task_id"])
            if entry.get("status") != "delivered":
                continue
            if entry.get("ci_seconds") is not None:
                await asyncio.to_thread(
                    observe_ci, worker_id, float(entry["ci_seconds"]), str(entry.get("ci") or "")
                )
            ticket = await asyncio.to_thread(self._ticket_of, worker_id, tickets)
            if ticket is None or ticket.task_id in held:
                continue
            if ticket.phase in (serve.PHASE_DONE, serve.PHASE_HANDED_OVER):
                continue
            pr = f"PR #{entry['pr']}"
            if entry.get("merged") is True or entry.get("state") == "MERGED":
                parts += await self._merged(ticket, worker_id, entry, now)
                continue
            if entry.get("state") != "OPEN":
                continue
            reasons: list[str] = []
            if entry.get("ci") == "fail":
                failing = ", ".join(entry.get("failing") or []) or "a check"
                reasons.append(f"CI failing on {pr}: {failing}")
            if entry.get("review") == "CHANGES_REQUESTED":
                reasons.append(f"a review requested changes on {pr}")
            if not reasons or await asyncio.to_thread(pr_attention_since_delivery, worker_id):
                continue
            summary = "; ".join(reasons)
            await asyncio.to_thread(record_pr_attention, worker_id, summary, reasons)
            await asyncio.to_thread(
                _set_phase,
                ticket.task_id,
                serve.PHASE_DISPATCHED,
                f"Worker task {worker_id}'s pull request needs attention: {summary}",
            )
            parts.append(f"worker task {worker_id}: {summary}")
        return parts

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
        self, ticket: Ticket, worker_id: int, entry: dict[str, Any], now: datetime
    ) -> list[str]:
        where = entry.get("url") or f"PR #{entry['pr']}"
        await asyncio.to_thread(
            _set_phase, ticket.task_id, serve.PHASE_DONE, f"Worker task {worker_id} merged: {where}"
        )
        await self._post(ticket, f"Merged: {where}. Done.", status=papaya_events.STATUS_DONE)
        parts = [f"ticket task {ticket.task_id}'s pull request merged: done"]
        return parts + await self._hygiene(worker_id, now)

    async def _clean_if_merged(self, ticket: Ticket) -> list[str]:
        """A ticket handed over whose pull request already merged: its worktree can go now."""
        try:
            entries = await asyncio.to_thread(self._forge_states)
        except Exception:  # noqa: BLE001 - unknown is not merged
            return []
        conn = await asyncio.to_thread(db.init_db)
        try:
            workers = {
                int(row["id"])
                for row in conn.execute(
                    "SELECT id FROM tasks WHERE run_id = ? AND id != ?",
                    (ticket.run_id, ticket.task_id),
                ).fetchall()
            }
        finally:
            conn.close()
        parts: list[str] = []
        for entry in entries:
            if int(entry.get("task_id") or 0) in workers and (
                entry.get("merged") is True or entry.get("state") == "MERGED"
            ):
                parts += await self._hygiene(int(entry["task_id"]), self._clock())
        return parts

    async def _post(self, ticket: Ticket, body: str, *, status: str | None = None) -> None:
        """Say one mechanical line on a ticket this process does not hold. Never fatal."""
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
        return {
            "PAPAYA_API_URL": os.environ.get("PAPAYA_API_URL") or server,
            "PAPAYA_WORKSPACE_ID": str(agent_config.get("workspace_id") or ""),
            "PAPAYA_AGENT_TOKEN": str(agent_config.get("client_token") or ""),
        }

    # -- hygiene -------------------------------------------------------------------

    async def _hygiene(self, task_id: int | None, now: datetime) -> list[str]:
        """Clean up worktrees under `ppy worktree prune`'s rules, and say so only when it matters.

        ``task_id`` is one task's slot, straight after its ticket is done; ``None``
        is the hourly run over every slot. Either way the base clones touched get
        `git worktree prune` and `git fetch --prune`, and one hygiene event records
        what went (with bytes) and what stayed (with why).
        """
        try:
            result = await asyncio.to_thread(self._prune, task_id)
        except Exception as exc:  # noqa: BLE001 - hygiene must never end a round
            log.warning("[rounds] Worktree hygiene failed: %s", exc)
            return []
        removed = list(result.get("removed") or [])
        kept = list(result.get("skipped") or [])
        repos = {str(r["repo"]) for r in removed if r.get("repo")}
        if task_id is None:
            repos |= {str(r["repo"]) for r in kept if r.get("repo") and r.get("managed", True)}
        clones = await asyncio.to_thread(
            managed_clone_paths, repos if task_id is not None else None
        )
        for clone in clones:
            await asyncio.to_thread(self._git, ["worktree", "prune"], clone)
            await asyncio.to_thread(self._git, ["fetch", "--prune", "--quiet"], clone)

        parts: list[str] = []
        streak_parts: list[str] = []
        if task_id is None:
            still = {str(r["path"]) for r in kept}
            self._kept_runs = {path: self._kept_runs.get(path, 0) + 1 for path in still}
            streak_parts = [
                f"kept {r['path']} for the {KEPT_SUMMARY_RUNS}rd run in a row: {r['reason']}"
                for r in kept
                if self._kept_runs.get(str(r["path"])) == KEPT_SUMMARY_RUNS
            ]
        surfaced = await self._loose_ends(kept, now)
        await asyncio.to_thread(
            record_hygiene,
            {
                "task_id": task_id,
                "scope": "task" if task_id is not None else "all",
                "removed": [
                    {
                        "task_id": r.get("task_id"),
                        "path": r.get("path"),
                        "branch": r.get("branch"),
                        "size_bytes": int(r.get("size_bytes") or 0),
                    }
                    for r in removed
                ],
                "kept": [
                    {
                        "task_id": r.get("task_id"),
                        "path": r.get("path"),
                        "reason": r.get("reason"),
                        "dirty": r.get("dirty"),
                        "unpushed_commits": r.get("unpushed_commits"),
                    }
                    for r in kept
                ],
                "reclaimed_bytes": int(result.get("reclaimed_bytes") or 0),
                "base_clones": clones,
                "surfaced": surfaced,
            },
        )
        if removed:
            from papaya_agent_runtime.worktree.reclaim import human_bytes

            total = int(result.get("reclaimed_bytes") or 0)
            parts.append(f"removed {len(removed)} worktree(s), {human_bytes(total)} freed")
        parts += streak_parts
        parts += [f"worktree {path} needs a person: waiting on you" for path in surfaced]
        return parts

    async def _loose_ends(self, kept: list[dict[str, Any]], now: datetime) -> list[str]:
        """Kept slots that only a person can settle, surfaced once each."""
        from papaya_agent_runtime.worktree.reclaim import RECLAIMABLE_STATUSES

        already = await asyncio.to_thread(surfaced_loose_ends)
        surfaced: list[str] = []
        for record in kept:
            path = str(record.get("path") or "")
            status = record.get("task_status")
            unpushed = int(record.get("unpushed_commits") or 0)
            if not path or path in already or status not in RECLAIMABLE_STATUSES:
                continue
            if not record.get("dirty") and unpushed == 0:
                continue
            if record.get("managed") is False:
                continue
            updated = await asyncio.to_thread(task_updated_at, record.get("task_id"))
            if updated is None or (now - updated).total_seconds() < KEPT_LOOSE_END_SECONDS:
                continue
            ticket = await asyncio.to_thread(ticket_for_worker, record.get("task_id"))
            text = (
                f"worktree {path} for task {record.get('task_id')} is kept: "
                f"{record.get('reason')}; commit and push what should stay, or discard it"
            )
            await asyncio.to_thread(
                surface_kept_slot, ticket.task_id if ticket else record.get("task_id"), text
            )
            if ticket is not None:
                await self._post(ticket, f"waiting on you: {text}")
            surfaced.append(path)
        return surfaced


def _default_gate_verdict(task_id: int) -> Any:
    from papaya_agent_runtime import gate

    return gate.verdict(task_id)


def _default_forge(conn: Any) -> list[dict[str, Any]]:
    from papaya_agent_runtime import watch

    return watch.pr_states(conn)


__all__ = [
    "DEAD_GRACE_SECONDS",
    "DEFAULT_ROUNDS_INTERVAL",
    "HYGIENE_EVENT",
    "HYGIENE_EVERY_SECONDS",
    "KEPT_LOOSE_END_SECONDS",
    "KEPT_SUMMARY_RUNS",
    "PERSON_WAIT_SECONDS",
    "ROUNDS_INTERVAL_ENV",
    "ROUND_EVENT",
    "Rounds",
    "WorkerLook",
    "interval_from_env",
    "look_at_worker",
    "record_worker_stopped",
    "ticket_tasks",
]
