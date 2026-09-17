"""A delivered pull request is followed until it merges.

Delivery used to be the end of a worker's interest in its pull request. The rounds
reacted to red CI and a formal changes-requested review, once per delivery, and to
nothing else — and one afternoon produced every other case: a pull request auto-closed
and conflicting after its base merged, one behind a base whose ruleset requires
up-to-date branches, and no worker that knew any of it was still theirs.

This module is what the rounds (:mod:`papaya_agent_runtime.rounds`) and the supervisor
(:mod:`papaya_agent_runtime.supervisor.core`) share about that:

- **Reasons.** :func:`reasons_for` turns one forge entry (:func:`watch.pr_states`) into
  the reasons it needs a worker again, each with a stable key and a rank that says how
  close the pull request is to merging: behind only, then conflicts, then red or stuck
  CI, then what reviewers said.
- **The fingerprint.** The keys plus the head commit. The same reasons at the same head
  are raised once; a new push, check, conflict or comment is a new fingerprint.
- **The lane.** Attention is queued on the worker's task (:data:`QUEUED`) and started
  (:data:`STARTED`) only while the reserved reconcile lane (``worker.reconcile_slots``)
  has room, ordered by rank and then by age; the lane is busy until the attempt ends
  (:data:`FINISHED`, with an outcome). Two failed attempts at one fingerprint mark the
  pull request :data:`NEEDS_A_PERSON` and stop.
- **The reconciler.** When the session that delivered a pull request cannot be resumed,
  the supervisor starts a fresh session on the same task from :func:`reconciler_brief`.

Every record lives on the worker's task in the event ledger, so a restart loses none of
it and ``ppy status`` can say what the lane is doing.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from papaya_agent_runtime import health
from papaya_agent_runtime.state import db, store

#: The event kinds, all on the worker's task.
QUEUED = "pr_attention_queued"
STARTED = "reconcile_started"
FINISHED = "reconcile_finished"
NEEDS_A_PERSON = "needs_a_person"

#: How a finished attempt ended.
OUTCOME_FIXED = "fixed"
OUTCOME_FAILED = "failed"
OUTCOME_MERGED = "merged"

#: Attempts at one fingerprint that may fail before a person is asked.
ATTEMPTS = 2

#: Merge-readiness ranks: lower is closer to merging, and goes first.
RANK_BEHIND = 0
RANK_CONFLICTS = 1
RANK_CI = 2
RANK_REVIEW = 3

#: Ticket phases after which an attempt that started has nothing left running.
_ENDED_PHASES = (
    "reported",
    "released",
    "handed_back",
    "stalled",
    "declined",
    "handed_over",
    "done",
    NEEDS_A_PERSON,
)

#: How many lines of a failing check's log a reconciler is given.
LOG_TAIL_LINES = 60


@dataclass(frozen=True)
class Reason:
    """One reason a delivered pull request needs a worker again."""

    #: Stable across rounds: the same reason at the same head has the same key.
    key: str
    #: What the review turn and the ticket read.
    text: str
    rank: int


def _first_line(text: object, width: int = 120) -> str:
    line = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    return line if len(line) <= width else line[: width - 1] + "…"


def _parse(stamp: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def reasons_for(
    entry: dict[str, Any],
    *,
    requires_up_to_date: bool = False,
    ci_budget_seconds: float | None = None,
    now: datetime | None = None,
) -> list[Reason]:
    """Every reason an open pull request needs its worker, from one forge entry."""
    pr = f"PR #{entry.get('pr')}"
    base = entry.get("base") or "its base"
    reasons: list[Reason] = []
    if entry.get("mergeable") == "CONFLICTING" or entry.get("merge_state") == "DIRTY":
        reasons.append(
            Reason(
                "conflicts",
                f"merge conflicts on {pr}: rebase onto {base} and resolve conflicts",
                RANK_CONFLICTS,
            )
        )
    elif entry.get("merge_state") == "BEHIND" and requires_up_to_date:
        reasons.append(
            Reason(
                "behind",
                f"{pr} is behind {base}, which requires up-to-date branches: update the branch",
                RANK_BEHIND,
            )
        )
    if entry.get("ci") == "fail":
        failing = sorted(entry.get("failing") or [])
        reasons.append(
            Reason(
                "ci:" + ",".join(failing),
                f"CI failing on {pr}: {', '.join(failing) or 'a check'}",
                RANK_CI,
            )
        )
    if ci_budget_seconds and now is not None:
        stuck = []
        for check in entry.get("pending_checks") or []:
            started = _parse(check.get("started_at"))
            # GitHub reports a check that never started with the zero time.
            if started is None or started.year < 2000:
                continue
            waited = (now - started).total_seconds()
            if waited > ci_budget_seconds:
                stuck.append((str(check.get("name") or "a check"), waited))
        if stuck:
            names = sorted(name for name, _ in stuck)
            longest = int(max(waited for _, waited in stuck))
            reasons.append(
                Reason(
                    "ci_stuck:" + ",".join(names),
                    f"CI stuck on {pr}: {', '.join(names)} pending for "
                    f"{health.humanize(longest)}, past its "
                    f"{health.humanize(int(ci_budget_seconds))} budget: rerun or look",
                    RANK_CI,
                )
            )
    if entry.get("review") == "CHANGES_REQUESTED":
        reasons.append(
            Reason("changes_requested", f"a review requested changes on {pr}", RANK_REVIEW)
        )
    for thread in entry.get("threads") or []:
        where = thread.get("path") or "the diff"
        if thread.get("line"):
            where = f"{where}:{thread['line']}"
        reasons.append(
            Reason(
                f"thread:{thread.get('id')}",
                f"unresolved review thread on {pr} at {where} from "
                f"{thread.get('author') or 'a reviewer'}: {_first_line(thread.get('body'))}",
                RANK_REVIEW,
            )
        )
    for comment in entry.get("comments") or []:
        reasons.append(
            Reason(
                f"comment:{comment.get('id')}",
                f"{comment.get('author') or 'a reviewer'} commented on {pr} since the last "
                f"push: {_first_line(comment.get('body'))}",
                RANK_REVIEW,
            )
        )
    return reasons


def fingerprint(reasons: list[Reason], head: str | None) -> str:
    """The reason set plus the head it was seen at, as one short, stable string."""
    material = json.dumps({"head": head or "", "keys": sorted(r.key for r in reasons)})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def rank_of(reasons: list[Reason]) -> int:
    """A pull request is as far from merging as its furthest reason."""
    return max((r.rank for r in reasons), default=RANK_REVIEW)


# ── the ledger ──────────────────────────────────────────────────────────────


def _payload(row: Any) -> dict[str, Any]:
    try:
        value = json.loads(row["payload"])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _append(conn: sqlite3.Connection, task_id: int, kind: str, payload: dict[str, Any]) -> int:
    task = store.get_task(conn, task_id)
    return store.append_event(
        conn,
        kind=kind,
        payload={"task_id": task_id, **payload},
        run_id=int(task["run_id"]) if task is not None else None,
        task_id=task_id,
    )


def record(task_id: int, kind: str, **payload: Any) -> int:
    conn = db.init_db()
    try:
        return _append(conn, task_id, kind, payload)
    finally:
        conn.close()


@dataclass
class History:
    """What the lane has already done for one worker's pull request."""

    worker_task_id: int
    #: `(event id, payload)` per kind, oldest first.
    queued: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    started: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    finished: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    marked: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    #: Delivery recorded that the base requires up-to-date branches, or a merge was
    #: refused for it.
    requires_up_to_date: bool = False

    def _with(self, rows: list[tuple[int, dict[str, Any]]], fp: str) -> list[int]:
        return [event_id for event_id, p in rows if p.get("fingerprint") == fp]

    def open_attempt(self) -> tuple[int, dict[str, Any]] | None:
        """The attempt started and not finished, if any."""
        done = {p.get("started_event_id") for _id, p in self.finished}
        return next(((i, p) for i, p in reversed(self.started) if i not in done), None)

    def failures(self, fp: str) -> int:
        return sum(
            1
            for _id, p in self.finished
            if p.get("fingerprint") == fp and p.get("outcome") == OUTCOME_FAILED
        )

    def pending(self, fp: str) -> bool:
        """Queued at this fingerprint and not started since."""
        queued = self._with(self.queued, fp)
        started = self._with(self.started, fp)
        return bool(queued) and (not started or max(queued) > max(started))

    def decide(self, fp: str) -> str | None:
        """``queue``, ``needs_a_person``, or ``None`` for nothing new to do."""
        if self._with(self.marked, fp) or self.pending(fp):
            return None
        raised = len(self._with(self.queued, fp))
        if raised == 0:
            return "queue"
        failed = self.failures(fp)
        if failed >= ATTEMPTS:
            return NEEDS_A_PERSON
        # One more attempt after a failure; anything else at this fingerprint is spent.
        return "queue" if failed >= raised else None


def history(worker_task_id: int, conn: sqlite3.Connection | None = None) -> History:
    own = conn is None
    conn = conn or db.init_db()
    try:
        rows = conn.execute(
            "SELECT id, kind, payload FROM events WHERE task_id = ? AND kind IN (?, ?, ?, ?, ?, ?) "
            "ORDER BY id",
            (
                worker_task_id,
                QUEUED,
                STARTED,
                FINISHED,
                NEEDS_A_PERSON,
                "delivered",
                "merge_refused",
            ),
        ).fetchall()
    finally:
        if own:
            conn.close()
    found = History(worker_task_id)
    lists = {
        QUEUED: found.queued,
        STARTED: found.started,
        FINISHED: found.finished,
        NEEDS_A_PERSON: found.marked,
    }
    for row in rows:
        kind, payload = str(row["kind"]), _payload(row)
        if kind in lists:
            lists[kind].append((int(row["id"]), payload))
        elif kind == "delivered" and payload.get("requires_up_to_date") is not None:
            found.requires_up_to_date = bool(payload.get("requires_up_to_date"))
        elif kind == "merge_refused" and payload.get("reason") == "up_to_date_required":
            found.requires_up_to_date = True
    return found


@dataclass(frozen=True)
class LaneEntry:
    """One pull request the lane is working on."""

    worker_task_id: int
    started_event_id: int
    payload: dict[str, Any]


def open_lane(conn: sqlite3.Connection | None = None) -> list[LaneEntry]:
    """Every attempt started and not finished, oldest first."""
    own = conn is None
    conn = conn or db.init_db()
    try:
        rows = conn.execute(
            "SELECT id, task_id, kind, payload FROM events WHERE kind IN (?, ?) ORDER BY id",
            (STARTED, FINISHED),
        ).fetchall()
    finally:
        if own:
            conn.close()
    started: dict[int, LaneEntry] = {}
    for row in rows:
        payload = _payload(row)
        if row["kind"] == STARTED:
            started[int(row["id"])] = LaneEntry(int(row["task_id"]), int(row["id"]), payload)
        else:
            started.pop(int(payload.get("started_event_id") or 0), None)
    return list(started.values())


def attempt_over(entry: LaneEntry) -> str | None:
    """Has this attempt stopped running? ``merged``, ``ended``, or ``None`` if not yet.

    Over means nothing of it is still running — no live runner on the worker — and
    either the worker delivered again since it started, or the ticket reached a phase
    after which nothing more happens on its own (reported, released, handed back, …).
    """
    conn = db.init_db()
    try:
        if store.live_runners_for_task(conn, entry.worker_task_id):
            return None
        ticket = entry.payload.get("ticket_task_id")
        if ticket is not None:
            if store.task_phase(conn, int(ticket)) == "done":
                return OUTCOME_MERGED
            row = conn.execute(
                "SELECT payload FROM events WHERE task_id = ? AND kind = ? AND id > ? "
                "ORDER BY id DESC LIMIT 1",
                (int(ticket), store.TICKET_PHASE_EVENT, entry.started_event_id),
            ).fetchone()
            if row is not None and _payload(row).get("phase") in _ENDED_PHASES:
                return "ended"
        delivered = conn.execute(
            "SELECT 1 FROM events WHERE task_id = ? AND kind = 'delivered' AND id > ? LIMIT 1",
            (entry.worker_task_id, entry.started_event_id),
        ).fetchone()
        return "ended" if delivered is not None else None
    finally:
        conn.close()


def pending_queue(conn: sqlite3.Connection | None = None) -> list[tuple[int, dict[str, Any]]]:
    """The newest queued attention per worker that has not been started, as `(id, payload)`."""
    own = conn is None
    conn = conn or db.init_db()
    try:
        rows = conn.execute(
            "SELECT id, task_id, kind, payload FROM events WHERE kind IN (?, ?) ORDER BY id",
            (QUEUED, STARTED),
        ).fetchall()
    finally:
        if own:
            conn.close()
    newest: dict[int, tuple[int, dict[str, Any]]] = {}
    for row in rows:
        worker, payload = int(row["task_id"]), _payload(row)
        if row["kind"] == QUEUED:
            newest[worker] = (int(row["id"]), payload)
        elif worker in newest and payload.get("fingerprint") == newest[worker][1].get(
            "fingerprint"
        ):
            del newest[worker]
    return list(newest.values())


def merge_readiness_order(
    items: list[tuple[int, dict[str, Any]]],
) -> list[tuple[int, dict[str, Any]]]:
    """Closest to merging first (behind only, conflicts, CI, reviews), then oldest."""

    def key(item: tuple[int, dict[str, Any]]) -> tuple[int, str, int]:
        event_id, payload = item
        return (
            int(payload.get("rank", RANK_REVIEW)),
            str(payload.get("created_at") or ""),
            event_id,
        )

    return sorted(items, key=key)


def is_reconciliation(conn: sqlite3.Connection, task_id: int) -> bool:
    """A task that was ever delivered only ever runs again to fix its pull request."""
    row = conn.execute(
        "SELECT 1 FROM events WHERE task_id = ? AND kind = 'delivered' LIMIT 1", (task_id,)
    ).fetchone()
    return row is not None


def pull_request_number(conn: sqlite3.Connection, task_id: int) -> int | None:
    """The delivered task's pull request number: the PR watch record, else the delivery's URL."""
    from papaya_agent_runtime import team

    row = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (task_id, team.PR_OBSERVED_EVENT),
    ).fetchone()
    number = _payload(row).get("pr") if row is not None else None
    if number is None:
        row = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = 'delivered' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        url = str(_payload(row).get("pr_url") or "") if row is not None else ""
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        number = tail if tail.isdigit() else None
    try:
        return int(number) if number is not None else None
    except (TypeError, ValueError):
        return None


def pr_head_sources(conn: sqlite3.Connection, task: Any) -> list[tuple[str, str]]:
    """Where a delivered task's pull request head is fetched from, best first.

    ``(refspec, label)`` pairs: the forge's ``refs/pull/<n>/head`` when the pull request
    is known — that is the head the pull request shows, whoever pushed it — then the
    task's branch. A worktree rebuilt for the reconcile lane starts from one of these
    and never from the task's base commit (PAP-222, 2026-09-17: the slot came back at
    the base, and the gate and the approval ran against it).
    """
    sources: list[tuple[str, str]] = []
    number = pull_request_number(conn, int(task["id"]))
    if number is not None:
        sources.append((f"refs/pull/{number}/head", f"PR #{number} head"))
    if task["branch"]:
        sources.append((f"refs/heads/{task['branch']}", f"branch {task['branch']}"))
    return sources


def lane_status(conn: sqlite3.Connection | None = None, now: datetime | None = None) -> str:
    """What `ppy status` says about the lane: idle, or which pull request since when."""
    lane = open_lane(conn)
    if not lane:
        queued = len(pending_queue(conn))
        return "idle" + (f", {queued} queued" if queued else "")
    now = now or datetime.now(UTC)
    parts = []
    for entry in lane:
        since = _parse(entry.payload.get("at"))
        age = health.humanize(int((now - since).total_seconds())) if since else "?"
        where = entry.payload.get("url") or f"PR #{entry.payload.get('pr')}"
        parts.append(f"{where} (task {entry.worker_task_id}) for {age}")
    queued = len(pending_queue(conn))
    return "reconciling " + "; ".join(parts) + (f", {queued} queued" if queued else "")


def reconcile_slots() -> int:
    try:
        from papaya_agent_runtime.config import load_config

        return int(load_config().worker.reconcile_slots)
    except Exception:  # noqa: BLE001 - no config yet is the schema default
        from papaya_agent_runtime.config import WorkerCeiling

        return WorkerCeiling().reconcile_slots


def merge_after_hours() -> int:
    try:
        from papaya_agent_runtime.config import load_config

        return int(load_config().delivery.merge_after_hours)
    except Exception:  # noqa: BLE001 - no config yet is the schema default
        from papaya_agent_runtime.config import DeliveryPolicy

        return DeliveryPolicy().merge_after_hours


def repo_of(worker_task_id: int) -> sqlite3.Row | None:
    conn = db.init_db()
    try:
        return conn.execute(
            "SELECT r.* FROM tasks t JOIN repos r ON r.id = t.repo_id WHERE t.id = ?",
            (worker_task_id,),
        ).fetchone()
    finally:
        conn.close()


def ci_budget_seconds(worker_task_id: int) -> float | None:
    """How long this worker's repository's CI usually takes (task 265), as a budget."""
    from papaya_agent_runtime import budgets

    row = repo_of(worker_task_id)
    try:
        return float(budgets.budget(row["name"] if row is not None else None, budgets.CI).seconds)
    except Exception:  # noqa: BLE001 - no budget means no stuck check
        return None


def merge_policy(worker_task_id: int) -> tuple[bool, str]:
    """Whether the worker's repository opted into `auto_merge`, and with which method."""
    from papaya_agent_runtime import environment

    row = repo_of(worker_task_id)
    if row is None:
        return False, environment.DEFAULT_MERGE_METHOD
    env = environment.for_repo(row)
    return env.auto_merge, env.merge_method


# ── what a reconciler is given ───────────────────────────────────────────────


def pr_details(worker_task_id: int, entry: dict[str, Any]) -> dict[str, Any]:
    """The facts a fix needs that a round does not fetch: log tails and conflicting files.

    Best effort, and never raises: a detail that cannot be read is left out.
    """
    from papaya_agent_runtime import watch

    details: dict[str, Any] = {}
    conn = db.init_db()
    try:
        task = store.get_task(conn, worker_task_id)
    finally:
        conn.close()
    cwd = task["worktree_path"] if task is not None else None
    tails = []
    for link in (entry.get("failing_links") or [])[:2]:
        run = _run_id(str(link))
        if run is None:
            continue
        code, out = watch._run_gh(["run", "view", run, "--log-failed"], cwd=cwd)
        if code == 0 and out.strip():
            tails.append("\n".join(out.strip().splitlines()[-LOG_TAIL_LINES:]))
    if tails:
        details["log_tail"] = "\n\n".join(tails)
    if entry.get("mergeable") == "CONFLICTING" or entry.get("merge_state") == "DIRTY":
        files = conflicting_files(cwd, entry.get("base"))
        if files:
            details["conflicting_files"] = files
    return details


def _run_id(link: str) -> str | None:
    parts = link.rstrip("/").split("/")
    if "runs" in parts:
        index = parts.index("runs")
        if index + 1 < len(parts) and parts[index + 1].isdigit():
            return parts[index + 1]
    return None


def conflicting_files(cwd: str | None, base: str | None) -> list[str]:
    """Files a merge of the base into this worktree's head would conflict on."""
    import os
    import subprocess

    if not cwd or not base or not os.path.isdir(cwd):
        return []
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                cwd,
                "merge-tree",
                "--write-tree",
                "--name-only",
                "--no-messages",
                "HEAD",
                f"origin/{base}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 1:
        return []
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return lines[1:]  # the first line is the tree


def latest_attention(conn: sqlite3.Connection, task_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = 'pr_attention' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return _payload(row) if row is not None else {}


def reconciler_brief(conn: sqlite3.Connection, task: Any, message: str | None) -> str:
    """The scoped brief a fresh reconciler session starts from, for one pull request."""
    from papaya_agent_runtime import environment, prompts
    from papaya_agent_runtime.manager.launch import repo_root

    task_id = int(task["id"])
    attention = latest_attention(conn, task_id)
    repo_row = conn.execute("SELECT * FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
    env = environment.for_repo(repo_row) if repo_row is not None else None
    threads = "\n".join(
        f"- {t.get('path') or 'the diff'}"
        + (f":{t['line']}" if t.get("line") else "")
        + f" ({t.get('author') or 'a reviewer'}): {t.get('body') or ''}"
        for t in attention.get("threads") or []
    )
    comments = "\n".join(
        f"- {c.get('author') or 'a reviewer'}: {c.get('body') or ''}"
        for c in attention.get("comments") or []
    )
    gate_policy = None
    if env is not None:
        gate_policy = (
            f"scoped gate: {env.local_gate or 'the one the repository names'}; "
            f"full suite: {env.full_suite_command or 'not set'} "
            f"(run it with `ppy gate run --task {task_id} --full`), owned by "
            f"{env.full_suite_owner}"
        )
    facts = {
        "worker task id": task_id,
        "repository": repo_row["name"] if repo_row is not None else None,
        "pull request": attention.get("url")
        or (f"#{attention['pr']}" if attention.get("pr") else None),
        "branch to push to": task["branch"],
        "base to rebase onto": attention.get("base"),
        "head it was at": attention.get("head"),
        "why it needs a worker": "\n".join(f"- {r}" for r in attention.get("reasons") or []),
        "the failing check's log tail": attention.get("log_tail"),
        "conflicting files": "\n".join(attention.get("conflicting_files") or []),
        "unresolved reviewer threads": threads,
        "reviewer comments since the last push": comments,
        "the repository's gate policy": gate_policy,
        "what the manager asked of you": message,
    }
    return prompts.render(prompts.RECONCILE, runtime_dir=repo_root(), facts=facts)


__all__ = [
    "ATTEMPTS",
    "FINISHED",
    "History",
    "LaneEntry",
    "NEEDS_A_PERSON",
    "QUEUED",
    "Reason",
    "STARTED",
    "attempt_over",
    "fingerprint",
    "history",
    "is_reconciliation",
    "lane_status",
    "merge_readiness_order",
    "open_lane",
    "pending_queue",
    "pr_head_sources",
    "pull_request_number",
    "reasons_for",
    "reconciler_brief",
]
