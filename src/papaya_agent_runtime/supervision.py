"""The supervision decisions both modes make about a worker, in one place.

`ppy serve` and an interactive session supervise the same workers (`parity`), so the
decision of what a worker needs is made here and each mode only acts on it: serve's
rounds and ticket runner steer or queue a turn; a session sees the same decision on
the heartbeat (`ppy watch`), in the session hooks and in `ppy checkin`.

Two decisions live here so far:

- **Gate follow-up** (:func:`decide_gate`, :func:`decide_commit`,
  :func:`gate_followup`): a worker that stopped or said done is judged by its
  recorded gate at head and by what its worktree holds that its branch does not.
  Green is reviewable; red is sent back with the gate's summary; no gate after a stop
  is sent back to `ppy gate run`; the same red twice is a person's decision;
  uncommitted work is sent back to commit or discard.
- **Check-ins** (:func:`checkins_due`, :func:`plan_reminder`, :func:`person_has_it`,
  :func:`worker_checkins`, :func:`record_checkin`): a live worker silent past its
  budget, planning too long, running past its midpoint, or not pushing is checked on,
  once per episode, and a person's fresh steer holds the check-in back.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from papaya_agent_runtime import health

#: Gate follow-up actions.
REVIEW = "review"
STEER = "steer"
PERSON = "person"

#: Past this many plan budgets, a worker still planning gets the plan check-in whatever
#: it is doing.
PLAN_HARD_FACTOR = 3


# ── gate follow-up ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GateFollowup:
    """What a stopped or done worker needs, judged by its record."""

    #: :data:`REVIEW`, :data:`STEER` or :data:`PERSON`.
    action: str
    #: One line saying why, for a person or a turn.
    line: str
    #: The steer to send, for :data:`STEER`.
    message: str = ""


def decide_gate(recorded: Any, *, stopped: bool, detail: str, worker_id: int) -> GateFollowup:
    """Judge a worker by its recorded gate at head (a `gate.Verdict`). No I/O.

    - the same red twice: a person's decision (re-running cannot change it);
    - green: reviewable;
    - red: sent back with the gate's summary;
    - none after a stop: sent back to run `ppy gate run`;
    - none after a done note: reviewable (the review re-checks).
    """
    from papaya_agent_runtime import gate, serve

    if recorded.state == gate.RED and recorded.repeated:
        return GateFollowup(PERSON, gate.repeated_line(recorded.repeated))
    if recorded.state == gate.GREEN and recorded.result is not None:
        return GateFollowup(REVIEW, recorded.result.line())
    if recorded.state == gate.NONE and not stopped:
        return GateFollowup(REVIEW, "no gate result recorded at its head")
    message = serve.gate_steer_message(detail, worker_id, recorded.result)
    if recorded.result is not None:
        return GateFollowup(STEER, recorded.result.line(), message)
    return GateFollowup(STEER, "stopped with no gate result recorded at its head", message)


def decide_commit(files: list[str] | None, branch: str | None) -> GateFollowup | None:
    """Uncommitted work in the worktree is sent back; ``None`` when there is none."""
    from papaya_agent_runtime import serve

    if not files:
        return None
    return GateFollowup(
        STEER, serve.uncommitted_finding(files), serve.uncommitted_steer_message(files, branch)
    )


def gate_followup(worker_task_id: int, *, stopped: bool, detail: str = "") -> GateFollowup:
    """Read a worker's record and decide: uncommitted work first, then its gate. Never raises."""
    from papaya_agent_runtime import gate, serve
    from papaya_agent_runtime.state import init_db, store

    try:
        conn = init_db()
        try:
            task = store.get_task(conn, worker_task_id)
        finally:
            conn.close()
        branch = task["branch"] if task is not None else None
        commit = decide_commit(serve.uncommitted_files(worker_task_id), branch)
        if commit is not None:
            return commit
        recorded = gate.verdict(worker_task_id)
    except Exception:  # noqa: BLE001 - an unreadable record decides nothing
        return GateFollowup(REVIEW, "its gate record could not be read")
    return decide_gate(recorded, stopped=stopped, detail=detail, worker_id=worker_task_id)


# ── check-ins ───────────────────────────────────────────────────────────────


def _parse(stamp: object) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def checkins_due(
    look: Any,
    now: datetime,
    records: list[tuple[int, dict[str, Any]]],
    waits: Any = None,
    push: Any = None,
) -> list[tuple[str, str]]:
    """The check-ins a live worker is due, as ``(trigger, why)``. No I/O.

    ``look`` is a `rounds.WorkerLook`, ``records`` the round records of the task that
    owns the worker's check-ins, ``waits`` its `rounds.WorkerBudgets`, ``push`` its
    `rounds.PushState` (``None`` when the forge was not read).
    """
    from papaya_agent_runtime import rounds

    waits = waits or rounds.worker_budgets(None)
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
        # Planning too long is no plan *and* no progress (#55): a worker still making
        # tool calls is doing the setup its brief asked for, and is left to it until
        # it is past three plan budgets.
        idle = look.tool_idle_seconds(now)
        if running >= PLAN_HARD_FACTOR * plan_after:
            due.append(
                (
                    "plan",
                    f"still planning after {health.humanize(int(running))}, past "
                    f"{PLAN_HARD_FACTOR} times its {int(plan_after // 60)}m plan budget; "
                    "it should commit to a plan or say what blocks it",
                )
            )
        elif look.latest_phase is None and idle >= waits.plan_idle_seconds:
            due.append(
                (
                    "plan",
                    f"no plan note after {health.humanize(int(running))}, longer than "
                    f"{int(plan_after // 60)}m, and no tool call for "
                    f"{health.humanize(int(idle))}; it should commit to a plan or say what "
                    "blocks it",
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
    if push is not None and push.unpushed and look.created_at is not None:
        # HEAD is not on the forge, and the forge's tip has not moved since the
        # session started or since a round first saw it there. A tip's commit date
        # is not when it was pushed: a push can land long after the commit.
        pushed_at = max(
            look.created_at,
            rounds.last_push_at(records, look.task_id, push.remote_sha) or look.created_at,
        )
        since = pushed_at
        for p in mine:
            at = _parse(p.get("at"))
            if p.get("trigger") == "push" and p.get("remote_sha") == push.remote_sha and at:
                since = max(since, at)
        if (now - since).total_seconds() >= rounds._push_by_seconds():
            minutes = int((now - pushed_at).total_seconds() // 60)
            due.append(("push", f"nothing pushed in {minutes} minutes"))
    return due


def plan_reminder(look: Any, now: datetime, waits: Any = None) -> str | None:
    """The one line a check-in carries for a busy worker with no plan note yet. No I/O.

    Past its plan budget but still making tool calls, the worker is not stuck, so this
    is never a check-in of its own: it rides along on one that is due anyway.
    """
    from papaya_agent_runtime import rounds

    waits = waits or rounds.worker_budgets(None)
    running = look.running_seconds(now)
    if look.latest_phase is not None or running < waits.plan_seconds:
        return None
    idle = look.tool_idle_seconds(now)
    if idle >= waits.plan_idle_seconds:
        return None
    return (
        f"no plan note yet after {health.humanize(int(running))}, while still working "
        f"(last tool call {health.humanize(int(idle))} ago); a `continue, note` can "
        "remind it to post one"
    )


def person_has_it(
    look: Any,
    steers: list[dict[str, Any]],
    records: list[tuple[int, dict[str, Any]]],
    now: datetime,
    waits: Any,
) -> bool:
    """A person steered this worker after the last check-in, and it has not answered yet.

    Answered means a progress note since the steer. Not forever: a worker still silent
    one silence budget after a person's steer is checked on like any other. No I/O.
    """
    if not steers:
        return False
    last = steers[-1]
    checked = max(
        (
            int(p.get("after_event_id") or 0)
            for _id, p in records
            if p.get("action") == "checkin" and p.get("worker_task_id") == look.task_id
        ),
        default=0,
    )
    if last["event_id"] <= checked:
        return False
    if any(event_id > last["event_id"] for event_id, *_rest in look.progress):
        return False
    at = _parse(last["at"])
    return at is None or (now - at).total_seconds() < waits.quiet_seconds


@dataclass(frozen=True)
class CheckinDue:
    """A live worker due a check-in, as a session is shown it."""

    worker_task_id: int
    #: The task its check-in records live on: its live ticket's task, else the worker.
    owner_task_id: int
    #: The ticket task `ppy serve` works it under, when that ticket is still live.
    ticket_task_id: int | None
    due: tuple[tuple[str, str], ...]
    after_event_id: int
    #: The forge's tip of its branch when a push check-in is due (what a push record keys on).
    remote_sha: str | None = None

    @property
    def reason(self) -> str:
        return "; ".join(why for _trigger, why in self.due)

    @property
    def triggers(self) -> str:
        return ",".join(trigger for trigger, _why in self.due)

    def line(self) -> str:
        return (
            f"worker task {self.worker_task_id} is due a check-in ({self.reason}): read "
            f"`ppy progress {self.worker_task_id}`, then steer it or "
            f'`ppy checkin {self.worker_task_id} --ok "<what you saw>"`'
        )


def _owner(worker_task_id: int) -> tuple[int, int | None]:
    from papaya_agent_runtime import owed
    from papaya_agent_runtime.state import init_db, store

    conn = init_db()
    try:
        task = store.get_task(conn, worker_task_id)
        live = owed._live_tickets(conn) if task is not None else {}
    finally:
        conn.close()
    ticket = live.get(int(task["run_id"])) if task is not None else None
    return (ticket if ticket is not None else worker_task_id), ticket


def checkin_for(worker_task_id: int, *, now: datetime, push_state=None) -> CheckinDue | None:
    """The check-in a live worker is due right now, read from the record, or ``None``.

    The same decision serve's rounds make for a held ticket's worker: none while its
    gate runs under the supervisor, none while a person's fresh steer stands.
    """
    from papaya_agent_runtime import rounds
    from papaya_agent_runtime.state import init_db, store

    conn = init_db()
    try:
        task = store.get_task(conn, worker_task_id)
        repo = None
        if task is not None and task["repo_id"] is not None:
            row = conn.execute("SELECT name FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
            repo = str(row["name"]) if row else None
    finally:
        conn.close()
    if task is None or task["status"] != "in_progress" or task["phase"] is not None:
        return None
    waits = rounds.worker_budgets(repo)
    look = rounds.look_at_worker(
        worker_task_id, now=now, quiet_after=timedelta(seconds=waits.quiet_seconds)
    )
    if look is None or look.verdict is None:
        return None
    if rounds.gate_state(worker_task_id).running:
        return None
    owner, ticket = _owner(worker_task_id)
    records = rounds.round_records(owner)
    steers = rounds.person_steers(worker_task_id)
    if person_has_it(look, steers, records, now, waits):
        return None
    push = (push_state or rounds.push_state)(worker_task_id)
    due = checkins_due(look, now, records, waits, push)
    if not due:
        return None
    remote = push.remote_sha if push is not None else None
    return CheckinDue(worker_task_id, owner, ticket, tuple(due), look.last_event_id, remote)


def worker_checkins(*, now: datetime | None = None, push_state=None) -> list[CheckinDue]:
    """Every live worker due a check-in, ticket or not. Never raises."""
    from papaya_agent_runtime.state import init_db

    now = now or datetime.now(UTC)
    try:
        conn = init_db()
        try:
            ids = [
                int(row["id"])
                for row in conn.execute(
                    "SELECT id FROM tasks WHERE phase IS NULL AND status = 'in_progress' "
                    "ORDER BY id"
                ).fetchall()
            ]
        finally:
            conn.close()
        found = []
        for task_id in ids:
            due = checkin_for(task_id, now=now, push_state=push_state)
            if due is not None:
                found.append(due)
        return found
    except Exception:  # noqa: BLE001 - a heartbeat or hook never fails on its evidence
        return []


def record_checkin(worker_task_id: int, *, note: str, now: datetime | None = None) -> list[str]:
    """Record that a session checked on a worker, for every trigger due. Returns them.

    The same `checkin` round record serve writes when it queues its check-in turn, so
    the episode is not raised again by either mode.
    """
    from papaya_agent_runtime import rounds

    now = now or datetime.now(UTC)
    due = checkin_for(worker_task_id, now=now)
    if due is None:
        return []
    for trigger, why in due.due:
        rounds.record_round(
            due.owner_task_id,
            "checkin",
            worker_task_id=worker_task_id,
            trigger=trigger,
            reason=why,
            after_event_id=due.after_event_id,
            by="person",
            note=note,
            **({"at": now.isoformat(), "remote_sha": due.remote_sha} if trigger == "push" else {}),
        )
    return [trigger for trigger, _why in due.due]


# ── pull request repair ─────────────────────────────────────────────────────


#: Pull request repair actions (:func:`pr_attention`).
PR_QUEUE = "queue"
PR_PERSON = "needs_a_person"
PR_GREEN = "green"
PR_NOTHING = "nothing"


@dataclass(frozen=True)
class PrAttention:
    """What one open, delivered pull request needs this round."""

    action: str
    reasons: tuple[Any, ...] = ()
    fingerprint: str = ""

    @property
    def summary(self) -> str:
        return "; ".join(r.text for r in self.reasons)


def pr_attention(worker_task_id: int, entry: dict[str, Any], now: datetime) -> PrAttention:
    """Decide what an open delivered pull request needs, from the forge and the lane record.

    Its reasons and head make a fingerprint. A new fingerprint is queued for the reserve
    lane; the same one is not raised again unless the lane's attempt at it failed, once.
    Two failures at one fingerprint are a person's. No reasons at all is green.
    """
    from papaya_agent_runtime import reconcile

    past = reconcile.history(worker_task_id)
    if past.open_attempt() is not None:
        return PrAttention(PR_NOTHING)
    budget = reconcile.ci_budget_seconds(worker_task_id)
    reasons = reconcile.reasons_for(
        entry,
        requires_up_to_date=past.requires_up_to_date,
        ci_budget_seconds=budget,
        now=now,
    )
    if not reasons:
        return PrAttention(PR_GREEN)
    fp = reconcile.fingerprint(reasons, entry.get("head"))
    decision = past.decide(fp)
    if decision == reconcile.NEEDS_A_PERSON:
        return PrAttention(PR_PERSON, tuple(reasons), fp)
    if decision == "queue":
        return PrAttention(PR_QUEUE, tuple(reasons), fp)
    return PrAttention(PR_NOTHING, tuple(reasons), fp)


def record_needs_a_person(worker_task_id: int, attention: PrAttention, entry: dict, now) -> None:
    from papaya_agent_runtime import reconcile

    reconcile.record(
        worker_task_id,
        reconcile.NEEDS_A_PERSON,
        fingerprint=attention.fingerprint,
        head=entry.get("head"),
        reasons=[r.text for r in attention.reasons],
        at=now.isoformat(),
    )


def record_queued(
    worker_task_id: int,
    attention: PrAttention,
    entry: dict,
    now: datetime,
    ticket_task_id: int | None,
) -> None:
    from papaya_agent_runtime import reconcile

    reasons = list(attention.reasons)
    reconcile.record(
        worker_task_id,
        reconcile.QUEUED,
        fingerprint=attention.fingerprint,
        head=entry.get("head"),
        rank=reconcile.rank_of(reasons),
        keys=[r.key for r in reasons],
        reasons=[r.text for r in reasons],
        summary=attention.summary,
        pr=entry.get("pr"),
        url=entry.get("url"),
        base=entry.get("base"),
        created_at=entry.get("created_at"),
        ticket_task_id=ticket_task_id,
        threads=list(entry.get("threads") or []),
        comments=list(entry.get("comments") or []),
        at=now.isoformat(),
    )


def start_repair(
    queued: dict[str, Any], entry: dict[str, Any], now: datetime, *, details: dict, steer
) -> None:
    """Start one queued repair on the reserve lane by steering its worker.

    A steer on a delivered task is admitted in the reserve lane and starts a fresh
    reconciler session from the attention recorded here (`reconcile.reconciler_brief`),
    so no turn is needed to compose it.
    """
    from papaya_agent_runtime import reconcile, rounds

    worker_id = int(queued.get("task_id") or 0)
    summary = str(queued.get("summary") or "")
    rounds.record_pr_attention(
        worker_id,
        summary,
        list(queued.get("reasons") or []),
        fingerprint=queued.get("fingerprint"),
        head=queued.get("head"),
        pr=queued.get("pr"),
        url=queued.get("url"),
        base=queued.get("base"),
        threads=queued.get("threads") or [],
        comments=queued.get("comments") or [],
        **details,
    )
    reconcile.record(
        worker_id,
        reconcile.STARTED,
        fingerprint=queued.get("fingerprint"),
        head=queued.get("head"),
        pr=queued.get("pr"),
        url=queued.get("url"),
        ticket_task_id=None,
        at=now.isoformat(),
    )
    where = queued.get("url") or f"PR #{queued.get('pr')}"
    steer(worker_id, f"Your delivered pull request {where} needs attention: {summary}")


def steer_worker(worker_task_id: int, message: str) -> None:
    """Steer a worker through the supervisor, as the manager."""
    from papaya_agent_runtime.state import store
    from papaya_agent_runtime.supervisor.client import SupervisorClient

    SupervisorClient().steer_task(worker_task_id, message, by=store.BY_MANAGER)


def serve_running() -> bool:
    """Is a `ppy serve` process running this instance's supervisor right now?

    The heartbeat does the steps serve's rounds do only when no serve is there to do
    them, so the two never act on the same pull request.
    """
    import json
    import os

    from papaya_agent_runtime.paths import run_dir

    try:
        data = json.loads((run_dir() / "supervisor.json").read_text(encoding="utf-8"))
        pid = int(data.get("pid") or 0)
    except (OSError, ValueError, TypeError):
        return False
    if data.get("role") != "serve" or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _no_live_ticket(worker_task_id: int) -> bool:
    return _owner(worker_task_id)[1] is None


def repair_untracked(
    entries: list[dict[str, Any]],
    now: datetime,
    *,
    steer,
    pr_details=None,
    room: int | None = None,
) -> list[str]:
    """Repair every open delivered pull request no live ticket covers. Returns lines.

    The ticket path in `ppy serve` goes through a review turn; this is everything else:
    work dispatched from a session, and tickets that ended (handed over, done, stalled).
    Both modes call it: serve's rounds each round, and the heartbeat when no serve is
    running. Never raises.
    """
    from papaya_agent_runtime import reconcile

    lines: list[str] = []
    try:
        mine: dict[int, dict[str, Any]] = {}
        for entry in entries:
            if not entry.get("known") or entry.get("pr") is None:
                continue
            if entry.get("status") != "delivered" or entry.get("state") != "OPEN":
                continue
            worker_id = int(entry["task_id"])
            if not _no_live_ticket(worker_id):
                continue
            mine[worker_id] = entry
            attention = pr_attention(worker_id, entry, now)
            if attention.action == PR_PERSON:
                record_needs_a_person(worker_id, attention, entry, now)
                lines.append(
                    f"worker task {worker_id}'s pull request needs a person: {attention.summary}"
                )
            elif attention.action == PR_QUEUE:
                record_queued(worker_id, attention, entry, now, None)
                lines.append(f"worker task {worker_id}: {attention.summary} (queued for repair)")
        busy = len(reconcile.open_lane())
        free = (reconcile.reconcile_slots() if room is None else room) - busy
        for _event_id, queued in reconcile.merge_readiness_order(reconcile.pending_queue()):
            worker_id = int(queued.get("task_id") or 0)
            if worker_id not in mine or queued.get("ticket_task_id") is not None:
                continue
            if free <= 0:
                break
            free -= 1
            details = (pr_details or reconcile.pr_details)(worker_id, mine[worker_id])
            start_repair(queued, mine[worker_id], now, details=details, steer=steer)
            lines.append(f"worker task {worker_id}: repairing ({queued.get('summary')})")
    except Exception as exc:  # noqa: BLE001 - a repair step never ends a round or a tick
        lines.append(f"could not check delivered pull requests: {exc}")
    return lines


def prs_needing_a_person() -> list[tuple[int, str]]:
    """Delivered pull requests the lane gave up on at their current fingerprint."""
    from papaya_agent_runtime import reconcile
    from papaya_agent_runtime.state import init_db

    conn = init_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT task_id FROM events WHERE kind = ?", (reconcile.NEEDS_A_PERSON,)
        ).fetchall()
        found = []
        for row in rows:
            task_id = int(row["task_id"])
            status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if status is None or status["status"] != "delivered":
                continue
            past = reconcile.history(task_id, conn)
            marked = past.marked[-1][1] if past.marked else {}
            later = [i for i, _p in past.queued if past.marked and i > past.marked[-1][0]]
            if marked and not later:
                found.append((task_id, "; ".join(marked.get("reasons") or [])))
        return found
    finally:
        conn.close()


# ── merge follow-up and the green-unmerged clock ────────────────────────────


#: The round record, on a ticket's task, that its merged pull request was followed up.
MERGED_FOLLOWUP = "merged_followup"


def merged_status_rule() -> str:
    """The status this workspace said a merged work item moves to; empty when not said."""
    try:
        from papaya_agent_runtime.config import load_config
        from papaya_agent_runtime.paths import config_path

        if config_path().exists():
            return str(load_config().delivery.merged_status or "").strip()
    except Exception:  # noqa: BLE001 - an unreadable config is the same as no rule
        return ""
    return ""


def merged_message(where: str, rule: str) -> tuple[str, str | None]:
    """The comment and the status for a merged pull request. No I/O.

    With no rule it moves nothing and asks: statuses differ per workspace, and the answer
    becomes this workspace's rule (`ppy config delivery --merged-status <status>`).
    """
    if rule:
        return f"Merged: {where}. Moved to {rule}, as this workspace asked.", rule
    return (
        f"Merged: {where}. The change is on main, so this item is out of date. Should it "
        "move to done, or to another status this workspace uses (for example until it is "
        "verified on staging)? Say which and I will do that for every merged pull request "
        "from now on.",
        None,
    )


def _merged(entry: dict[str, Any]) -> bool:
    return entry.get("merged") is True or entry.get("state") == "MERGED"


def merged_step(entries: list[dict[str, Any]], *, post, held: set[int] | None = None) -> list[str]:
    """Follow up every merged pull request whose ticket has not been, once. Returns lines.

    ``post(ticket, body, status)`` says it on the work item (serve through its connection,
    a session through :func:`papaya.agent_env`). The ticket's local phase becomes done.
    Both modes call it; a held ticket is its runner's. Never raises.
    """
    from papaya_agent_runtime import rounds, serve

    lines: list[str] = []
    try:
        tickets = {t.run_id: t for t in rounds.ticket_tasks()}
        rule = merged_status_rule()
        for entry in entries:
            if not entry.get("known") or entry.get("pr") is None or not _merged(entry):
                continue
            worker_id = int(entry["task_id"])
            ticket = rounds.ticket_for_worker(worker_id)
            if ticket is None or ticket.run_id not in tickets:
                continue
            if held and ticket.task_id in held:
                continue
            records = rounds.round_records(ticket.task_id)
            if rounds._done_before(records, MERGED_FOLLOWUP, worker_task_id=worker_id):
                continue
            where = entry.get("url") or f"PR #{entry['pr']}"
            body, status = merged_message(where, rule)
            post(ticket, body, status)
            rounds.record_round(ticket.task_id, MERGED_FOLLOWUP, worker_task_id=worker_id)
            rounds._set_phase(
                ticket.task_id, serve.PHASE_DONE, f"Worker task {worker_id} merged: {where}"
            )
            lines.append(
                f"ticket task {ticket.task_id}'s pull request merged: "
                + (f"moved to {status}" if status else "asked what it moves to")
            )
    except Exception as exc:  # noqa: BLE001 - a round or a heartbeat never ends on this
        lines.append(f"could not follow up merged pull requests: {exc}")
    return lines


GREEN_NOTHING = "nothing"
GREEN_RECORD = "record"
GREEN_SAY = "say"
GREEN_MERGE = "merge"


def green_clock(
    worker_id: int,
    entry: dict[str, Any],
    records: list[tuple[int, dict[str, Any]]],
    now: datetime,
    *,
    hours: float,
    auto_merge: bool,
) -> str:
    """What a green, mergeable, unrequested pull request needs. No I/O.

    The clock starts the first time it is seen green at its head (``record``). Past
    ``hours`` it is merged when the repository opted into auto-merge and no merge at
    this head failed (``merge``), else said once (``say``).
    """
    from papaya_agent_runtime import rounds

    mergeable = entry.get("mergeable") != "CONFLICTING" and entry.get("merge_state") != "DIRTY"
    if entry.get("ci") != "pass" or not mergeable:
        return GREEN_NOTHING
    head = entry.get("head")
    since = next(
        (
            _parse(p.get("at"))
            for _id, p in records
            if p.get("action") == "green"
            and p.get("worker_task_id") == worker_id
            and p.get("head") == head
        ),
        None,
    )
    if since is None:
        return GREEN_RECORD
    if (now - since).total_seconds() < hours * 3600:
        return GREEN_NOTHING
    if auto_merge and not rounds._done_before(
        records, "merge_failed", worker_task_id=worker_id, head=head
    ):
        return GREEN_MERGE
    if rounds._done_before(records, "green_unmerged", worker_task_id=worker_id):
        return GREEN_NOTHING
    return GREEN_SAY


def green_step(entries: list[dict[str, Any]], now: datetime, *, post, merge) -> list[str]:
    """Run the green-unmerged clock on every open delivered pull request a live ticket owns.

    The heartbeat's half of serve's `Rounds._green`, for a session with no serve running:
    record when it first went green, then merge (auto-merge repositories) or say it once.
    ``post(ticket, body, status)``; ``merge(worker_id, entry, method)``. Never raises.
    """
    from papaya_agent_runtime import reconcile, rounds

    lines: list[str] = []
    try:
        hours = reconcile.merge_after_hours()
        for entry in entries:
            if not entry.get("known") or entry.get("pr") is None:
                continue
            if entry.get("status") != "delivered" or entry.get("state") != "OPEN":
                continue
            worker_id = int(entry["task_id"])
            owner, live = _owner(worker_id)
            if live is None:
                continue
            ticket = rounds.ticket_for_worker(worker_id)
            if ticket is None:
                continue
            if pr_attention(worker_id, entry, now).action != PR_GREEN:
                continue
            records = rounds.round_records(owner)
            auto, method = reconcile.merge_policy(worker_id)
            decision = green_clock(worker_id, entry, records, now, hours=hours, auto_merge=auto)
            head = entry.get("head")
            where = entry.get("url") or f"PR #{entry['pr']}"
            if decision == GREEN_RECORD:
                rounds.record_round(
                    owner, "green", worker_task_id=worker_id, head=head, at=now.isoformat()
                )
            elif decision == GREEN_MERGE:
                result = merge(worker_id, entry, method)
                if getattr(result, "merged", False):
                    lines += merged_step([{**entry, "merged": True}], post=post)
                else:
                    rounds.record_round(
                        owner,
                        "merge_failed",
                        worker_task_id=worker_id,
                        head=head,
                        detail=getattr(result, "detail", ""),
                    )
                    lines.append(f"worker task {worker_id}: could not merge {where}")
            elif decision == GREEN_SAY:
                rounds.record_round(owner, "green_unmerged", worker_task_id=worker_id)
                span = "a day" if hours == 24 else f"{hours} hours"
                post(
                    ticket,
                    f"PR {entry['pr']} has been green and unmerged for {span}: {where}",
                    None,
                )
                lines.append(f"worker task {worker_id}'s {where} green and unmerged for {span}")
    except Exception as exc:  # noqa: BLE001 - a heartbeat never ends on this
        lines.append(f"could not run the green-unmerged clock: {exc}")
    return lines


def post_as_agent(ticket: Any, body: str, status: str | None) -> None:
    """Say one line on a ticket's work item as this machine's agent, from a session."""
    from papaya_agent_runtime import papaya, papaya_events

    env = papaya.agent_env()
    if not env.get("PAPAYA_AGENT_TOKEN"):
        raise RuntimeError("this machine is not connected to a Papaya agent")
    event = ticket.event()
    if status is not None:
        papaya_events.set_work_item_status(event, status, environ=env)
    papaya_events.post_work_item_comment(event, body, environ=env)


# ── hygiene ─────────────────────────────────────────────────────────────────


def hygiene_step(
    task_id: int | None,
    now: datetime,
    *,
    prune,
    git,
    post,
    kept_runs: dict[str, int] | None = None,
) -> list[str]:
    """Clean up worktrees under `ppy worktree prune`'s rules; say only what matters.

    ``task_id`` is one task's slot, straight after its work merged; ``None`` is the hourly
    run over every slot. The base clones touched get `git worktree prune` and
    `git fetch --prune`; one hygiene event records what went and what stayed; a kept slot
    only a person can settle becomes a person's todo once (and a comment on its ticket,
    through ``post(ticket, body, None)``). ``kept_runs`` carries the kept-streak count
    between hourly runs of one process. Both modes call it: serve's rounds hourly and after
    a merge, a session's heartbeat hourly while no serve runs. Never raises.
    """
    from papaya_agent_runtime import rounds
    from papaya_agent_runtime.worktree.reclaim import RECLAIMABLE_STATUSES, human_bytes

    try:
        result = prune(task_id)
    except Exception:  # noqa: BLE001 - hygiene never ends a round or a tick
        return []
    removed = list(result.get("removed") or [])
    kept = list(result.get("skipped") or [])
    repos = {str(r["repo"]) for r in removed if r.get("repo")}
    if task_id is None:
        repos |= {str(r["repo"]) for r in kept if r.get("repo") and r.get("managed", True)}
    clones = rounds.managed_clone_paths(repos if task_id is not None else None)
    for clone in clones:
        git(["worktree", "prune"], clone)
        git(["fetch", "--prune", "--quiet"], clone)

    streak: list[str] = []
    if task_id is None and kept_runs is not None:
        still = {str(r["path"]) for r in kept}
        fresh = {path: kept_runs.get(path, 0) + 1 for path in still}
        kept_runs.clear()
        kept_runs.update(fresh)
        streak = [
            f"kept {r['path']} for the {rounds.KEPT_SUMMARY_RUNS}rd run in a row: {r['reason']}"
            for r in kept
            if kept_runs.get(str(r["path"])) == rounds.KEPT_SUMMARY_RUNS
        ]

    already = rounds.surfaced_loose_ends()
    surfaced: list[str] = []
    for record in kept:
        path = str(record.get("path") or "")
        unpushed = int(record.get("unpushed_commits") or 0)
        if not path or path in already or record.get("task_status") not in RECLAIMABLE_STATUSES:
            continue
        if not record.get("dirty") and unpushed == 0:
            continue
        if record.get("managed") is False or record.get("open_pr") is not None:
            continue  # somebody else's, or waiting on its pull request, not on a person
        updated = rounds.task_updated_at(record.get("task_id"))
        if updated is None or (now - updated).total_seconds() < rounds.KEPT_LOOSE_END_SECONDS:
            continue
        ticket = rounds.ticket_for_worker(record.get("task_id"))
        text = (
            f"worktree {path} for task {record.get('task_id')} is kept: "
            f"{record.get('reason')}; commit and push what should stay, or discard it"
        )
        rounds.surface_kept_slot(ticket.task_id if ticket else record.get("task_id"), text)
        if ticket is not None:
            # The todo is the record; the comment a courtesy.
            with contextlib.suppress(Exception):
                post(ticket, f"waiting on you: {text}", None)
        surfaced.append(path)

    rounds.record_hygiene(
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
        }
    )
    parts: list[str] = []
    if removed:
        total = int(result.get("reclaimed_bytes") or 0)
        parts.append(f"removed {len(removed)} worktree(s), {human_bytes(total)} freed")
    parts += streak
    parts += [f"worktree {path} needs a person: waiting on you" for path in surfaced]
    return parts


def last_hygiene_at() -> datetime | None:
    """When any mode last ran the hourly hygiene over every slot."""
    from papaya_agent_runtime import rounds
    from papaya_agent_runtime.state import init_db

    conn = init_db()
    try:
        row = conn.execute(
            "SELECT created_at FROM events WHERE kind = ? "
            "AND json_extract(payload, '$.scope') = 'all' ORDER BY id DESC LIMIT 1",
            (rounds.HYGIENE_EVENT,),
        ).fetchone()
    finally:
        conn.close()
    return _parse(row["created_at"]) if row is not None else None


# ── start remedies and blocker reports ──────────────────────────────────────


def start_remedies(*, stderr) -> None:
    """What a start puts right: first-run setup, config, state, then the deficiency count.

    `ppy serve` runs it as it starts; a session's start hook runs it when no serve is
    running, so a session on a machine nobody served still gets its config migrated, dead
    runners closed, base clones and gate policies read again. Each remedy says one line
    per thing it changed on ``stderr``.
    """
    from papaya_agent_runtime import serve

    serve.self_setup(stderr=stderr)
    serve.keep_config_right(stderr=stderr)
    serve.keep_state_right(stderr=stderr)
    serve.announce_deficiencies(stderr=stderr)


def blocker_lines(changes: Any) -> list[str]:
    """What a blocker observation changed, in the words a heartbeat line or a DM uses."""
    lines = [f"new blocker: {b.title}" for b in getattr(changes, "appeared", [])]
    lines += [f"blocker changed: {b.title}" for b in getattr(changes, "changed", [])]
    lines += [f"blocker cleared: {b.title}" for b in getattr(changes, "cleared", [])]
    return lines


def blocker_step(*, check=None) -> list[str]:
    """Re-check readiness, keep the blocker ledger, and return what changed. Never raises.

    Serve's blocker watch does this on its clock and DMs the owner; a session's heartbeat
    does it while no serve runs and says it on its line, where the person is.
    """
    from papaya_agent_runtime import blockers, readiness

    try:
        verdict = (check or readiness.check)()
        return blocker_lines(blockers.update(verdict))
    except Exception as exc:  # noqa: BLE001 - a heartbeat keeps ticking
        return [f"could not re-check readiness: {exc}"]


# ── the full suite before review ────────────────────────────────────────────


def full_suite_missing(worker_task_id: int) -> str | None:
    """Why an approval must wait for the full suite at this head, or ``None``.

    When the repository's full suite is the supervisor's to run (not CI's), it runs once
    at the head that will be delivered before review. Serve runs it before its review
    turn (`TicketRunner._full_suite_before_review`); `ppy review approve` refuses a head
    with no full-suite result, so a review from a session keeps the same rule. A red
    result is the reviewer's to judge, not a refusal.
    """
    from papaya_agent_runtime import environment, gate
    from papaya_agent_runtime.state import init_db, store

    conn = init_db()
    try:
        task = store.get_task(conn, worker_task_id)
        row = (
            conn.execute("SELECT * FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
            if task is not None and task["repo_id"] is not None
            else None
        )
    finally:
        conn.close()
    if row is None or not environment.for_repo(row).supervisor_runs_full_suite:
        return None
    recorded = gate.verdict(worker_task_id, full=True)
    if not recorded.head_sha or recorded.state != gate.NONE:
        return None
    return (
        f"the full suite is the supervisor's to run and has no result at this head: run "
        f"`ppy gate run --task {worker_task_id} --full` (run it again while it says it is "
        "still running), then approve"
    )


# ── assigned work nobody picked up ──────────────────────────────────────────


def assigned_unpicked(*, now: datetime | None = None, api=None) -> list[dict[str, Any]]:
    """Work items assigned to this agent that nothing here or elsewhere is working.

    The same filters serve's sweep applies before it offers an item (`sweep.skip_reason`):
    open, no live task here, not in progress elsewhere, not declined earlier with nothing
    changed since. A session is shown them (session start, `ppy sweep`, the heartbeat)
    and takes one up by briefing and dispatching it. Never raises; ``[]`` when not
    connected.
    """
    import asyncio

    from papaya_agent_runtime import sweep

    now = now or datetime.now(UTC)
    try:
        if api is None:
            from papaya_agent_client.api_client import AgentTokenApi

            from papaya_agent_runtime import papaya

            found = papaya._best()
            if found is None:
                return []
            who, home = found
            config = papaya._read_config(home)
            agent = (config.get("agents") or {}).get(who.agent_id)
            if not agent:
                return []
            api = AgentTokenApi(config, agent)
        from papaya_agent_client import api_client

        answer = asyncio.run(api_client.list_assigned_work_items(api))
        items = sweep.sweep_order([i for i in sweep._items(answer) if sweep.is_open(i)])
        live = sweep.live_work_item_ids()
        declined = sweep.declined_items()
        return [
            item
            for item in items
            if sweep.skip_reason(
                item, now=now, live=live, declined=declined, stale_after=sweep.DEFAULT_STALE_AFTER
            )
            is None
        ]
    except Exception:  # noqa: BLE001 - a session is never stopped by an unreachable Papaya
        return []
