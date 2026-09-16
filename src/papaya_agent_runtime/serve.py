"""`ppy serve`: one long-lived process that is the manager.

Everything this runtime does for a person arrives as a Papaya event, and until now
every arrival was somebody else's process: `papaya-agent listen` held the lease,
started a harness, and let go the moment that harness exited — while the work it
had started was still being built. A ticket that takes an hour cannot be held by
something that lives for a minute.

So the manager owns its own process. `ppy serve` runs two things at once:

- **the runtime's own supervisor**, the same :class:`SupervisorServer` that
  `ppy supervisor serve` runs, so `ppy dispatch`, `ppy review` and `ppy deliver`
  from any shell on this machine talk to it; and
- **the Papaya client's event loop, in-process**, through the client's library
  entry points (`papaya_agent_client.embed`), with a runner of this runtime's own.

The second half is the important one, and the rule behind it is that *no copy of
the client's loop exists here*. The cursor, the acquire-or-extend reservation and
its renewal, lost-lease detection, staleness, the stall grace and the supervised
protocol are subtle and they are the client's; a copy of them drifts, and a
drifted lease rule is indistinguishable from data loss. The client made the loop
importable and the job execution pluggable precisely so this file could be small.

The two share the process cleanly because they are built differently: the
supervisor is a blocking socket server that already knows how to run on a thread
(:meth:`SupervisorServer.start_background`), and the client's loop is asyncio. So
the supervisor takes a thread, the listener takes the main thread's event loop,
and neither waits on the other. The supervisor binds *first*, synchronously, so
that the one refusal this command can make — another `serve` or `supervisor serve`
already owns this ``PPY_HOME`` — happens before anything has been said to Papaya.

Before either of them runs, `serve` sets the checkout up if nobody ever has
(:func:`self_setup`) and then tells the connection's owner, once, what is still
missing (:func:`report_readiness`). Both are here rather than in a command
somebody is expected to type, because the experience this runtime is for is
"clone it, point the app at it, connect" — and a machine that has only ever been
connected has no first turn in which to run `ppy setup` by hand.

What the runner does
--------------------
It **sequences**, and it never judges. For each approved job it records the ticket
and then walks it through its phases, writing each one on the task row, as a
``ticket_phase`` event, and as a ``job.report_progress`` line::

    picked_up -> briefing -> dispatched -> reviewing -> delivering -> reported -> released
                                |    ^
                                v    |
                              blocked (a worker's question, or a person's reply)

Every step that needs judgment is a **manager turn**: a headless session of the
configured manager harness, launched through the same builder as `ppy start`
(:func:`~papaya_agent_runtime.manager.launch.build_launch`), in the runtime
directory, with the client's job environment and a write boundary of the runtime
directory alone. There are three, and their prompts are reviewed text in
:mod:`papaya_agent_runtime.prompts`: *brief* (choose the repository, define done,
brief and dispatch), *answer* (unblock a worker or ask a person) and *review*
(review at head, deliver, report back).

Between turns the runner watches the runtime's own state for this ticket's run:
a worker's progress becomes a progress line, `worker_done` starts the review
turn, a question starts the answer turn, and a stopped or failed worker starts the
review turn with the failure attached. It also listens to the ticket: while it
is `dispatched`, `reviewing` or `blocked`, a new comment by anyone but this agent
starts the answer turn with the comment as its fact (read at most a minute late,
queued behind a turn already running, and recorded so it is answered once, even
across a restart). How it knows a turn did its job is
mechanical too — a worker row in the ticket's run, or an answer, steer or
delivery event since the turn began. A turn that did not is retried once with the
tail of its transcript; a second miss hands the ticket back.

Waiting is a state, not a miss. A brief or review turn whose gate cannot finish
inside it ends with a first line `WAITING: <what>`; the runner says so as progress,
keeps the phase, and reruns the turn with its tail after five minutes, doubling to
thirty. And a long gate is the worker's, run through `ppy gate run` so it outlives
the worker's session, and its result is on the ledger at a head commit (PAP-213).
The runner reads that record, not the worker's note: a worker that stopped with a
green gate at its head is reviewed; one whose gate at its head is red is steered
with the summary (whether it stopped or said done); one that stopped with no gate
at its head is steered to run `ppy gate run`.

The work item's *status* is state, so the runner sets it: ``in_progress`` on
pickup, ``review`` when the pull request is open, ``blocked`` while a question
waits on a person, ``todo`` on hand-back. The *phase* is state too, so the runner
says each phase change on the item as one plain line, as the agent — that is what
the ticket card shows as the agent's status. Worker progress stays in the app.
Anything that needs judgment is still a turn's to say.

A turn's obligations on the record are checked afterwards rather than assumed:
a brief with Goals must have left acceptance criteria on the item, and a delivery
must have left the turn's report as a new agent comment. A miss reruns the turn
once with a one-line addendum; a second miss is a progress line (acceptance
criteria) or the runner's own fallback line with the pull request (the report).

Every turn is launched with the Papaya agent's tools — the MCP config
`papaya-agent mcp runner-config` writes, `--strict-mcp-config` and the client's
plugin — and its transcript is kept at `.ppy/runs/<run id>/turns/<turn>-<n>.log`.

A ticket this runtime cannot take at all (a repository it names that cannot be
registered, or an event carrying no work item) is declined through the client's
own decline path before any of that, so a peer that *can* do the work may still
take it.

Two things worth knowing about the identity. `home` is process-wide in the client,
so one `serve` serves one Papaya connection. And the **session id** is the lease
identity: Papaya's reserve is acquire-or-extend, keyed on it, so a manager that
minted a fresh id on every start would spend the minutes after each restart
racing leases it holds itself. `serve` persists one id per connection in
:func:`~papaya_agent_runtime.paths.papaya_sessions_path` and passes it back, so a
restart extends its own reservations instead of colliding with them.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from papaya_agent_runtime import (
    blockers,
    capabilities,
    deficiencies,
    gate,
    papaya,
    papaya_events,
    prompts,
    readiness,
    sweep,
)
from papaya_agent_runtime.paths import papaya_sessions_path
from papaya_agent_runtime.state import db, store

log = logging.getLogger("papaya_agent_runtime.serve")

#: The bundled harness keys `--harness` may name. The client owns the list; these
#: are the two this runtime is ever exec'd with, and naming them here is what lets
#: a wrong one be *reported* over the protocol rather than crash the process
#: before `hello` has been said.
HARNESSES = ("claude-code", "codex")

#: The supervised `error` code a blocked readiness verdict is reported under. Not
#: a protocol code the client knows: it is this runtime's own, and non-fatal,
#: because a runtime that cannot dispatch can still hold a conversation and say so.
RUNTIME_NOT_READY = "runtime_not_ready"

#: The phases this runner writes to a task, in `store.TASK_PHASES`' order. The
#: first seven are how far the work has got; the last four are how the hold ended.
PHASE_PICKED_UP = "picked_up"
PHASE_BRIEFING = "briefing"
PHASE_DISPATCHED = "dispatched"
PHASE_BLOCKED = "blocked"
PHASE_REVIEWING = "reviewing"
PHASE_DELIVERING = "delivering"
PHASE_REPORTED = "reported"
PHASE_RELEASED = "released"
PHASE_HANDED_BACK = "handed_back"
PHASE_STALLED = "stalled"
PHASE_DECLINED = "declined"
#: What the manager's rounds find after a hold (`rounds.py`): the ticket is held
#: by somebody else now, or its pull request merged.
PHASE_HANDED_OVER = "handed_over"
PHASE_DONE = "done"

#: The phases a ticket can be resumed from: work was under way and nobody gave it
#: away. `released` is not here, but the working phase before it is, which is
#: what lets a manager that was shut down mid-ticket pick the work up where it was.
WORKING_PHASES = (
    PHASE_BRIEFING,
    PHASE_DISPATCHED,
    PHASE_BLOCKED,
    PHASE_REVIEWING,
    PHASE_DELIVERING,
)

#: How often the runner looks at the ledger while it waits. The ledger is local
#: SQLite, so this is cheap; it only bounds how late a phase change is noticed.
POLL_SECONDS = 2.0

#: How often, at most, the runner reads a held ticket's comments while it waits.
#: Papaya's API, not the ledger, so once a minute rather than every poll: it
#: bounds how late a person's reply on the ticket is heard.
COMMENT_POLL_SECONDS = 60.0

#: How often, at most, a held ticket whose worker or gate is active says so as a
#: progress line (`health.liveness_minutes`). The client's stall clock hears only
#: progress lines and a harness's own output, and a manager's worker is neither.
LIVENESS_SECONDS = 5 * 60.0

#: The event kind, on the ticket's own task, that records the newest comment on
#: the work item already handled. The newest such event is the record.
COMMENT_HANDLED_EVENT = "ticket_comment_handled"

#: How many attempts a turn gets at its job before the ticket is handed back.
TURN_ATTEMPTS = 2

#: How long the runner waits before rerunning a turn that ended `WAITING:`, the
#: first time; each further wait in a row doubles it, up to the cap. Waiting is a
#: state, not a miss, so these never count toward `TURN_ATTEMPTS`.
WAIT_FIRST_SECONDS = 5 * 60.0
WAIT_MAX_SECONDS = 30 * 60.0

#: How many times in a row the runner itself sends a worker that stopped mid-gate
#: back to finish its gate before the review turn is given the failure instead.
GATE_STEERS = 2

#: The event kind `turn_end` records for a worker whose session ended mid-gate.
WORKER_STOPPED = "worker_stopped"
#: The triggers the runner checks against the worker's recorded gate before a review.
GATE_TRIGGERS = (WORKER_STOPPED, "worker_done")

#: Event kinds on a worker task that mean the manager has acted on it, so the
#: trigger before them is spent. Used both to tell whether a turn did its job and
#: to stop a replayed history re-triggering a turn that already ran.
#: How a review that sent the worker back reads: the ticket-phase detail (after
#: "Worker task N"), and the one comment the ticket gets the first time only.
SENT_BACK_DETAIL = "sent back with findings."
SENT_BACK_LINE = "Sent the worker back with findings; still working."
#: `_say`'s key for that comment, which is not a phase of its own.
SAID_SENT_BACK = "sent_back"

ACTED_KINDS = ("answer", "steer", "resumed", "auto_answered", "review_requested", "delivered")

#: The event kind, on a delivered worker's task, that the rounds write when its
#: pull request's CI went red or a review asked for changes. Read like a stopped
#: worker: the review turn gets the failure, and its steer is what answers it.
PR_ATTENTION = "pr_attention"

#: The event kind, on the ticket's own task, that records what a check-in turn
#: decided, why the check ran, and the message it gave the worker if any.
CHECKIN_EVENT = "ticket_checkin"


@dataclass(frozen=True)
class Nudge:
    """Something the rounds noticed about a held ticket's worker, for its runner to act on.

    The rounds only decide *that* a turn should run and hand over the facts; the
    runner runs it in the ticket's own loop, so it never overlaps the ticket's
    other turns, and the turn decides what is said to the worker.
    """

    #: `checkin`, `answer` or `stopped`.
    kind: str
    #: Why the rounds looked, in the words a turn and a progress line can use.
    reason: str
    #: For a check-in: `quiet`, `plan` or `midpoint`.
    trigger: str = ""
    #: The worker's question, or what stopped it, verbatim.
    detail: str = ""
    #: The ledger event the nudge is about, when there is one.
    event_id: int = 0
    #: Extra facts for the turn, in order.
    facts: tuple[tuple[str, str], ...] = ()


# ── arguments ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ServeOptions:
    """What `serve` was asked for, including what it could not honour.

    `invalid_arguments` is kept rather than raised because of *when* it has to be
    said: under `--supervised` the host is owed a `hello` before it is owed a
    complaint, and the client's `build_supervised_listener` takes the complaint as
    an argument for exactly that reason.
    """

    supervised: bool = False
    harness: str | None = None
    approval_timeout: float | None = None
    working_directory: str | None = None
    #: Seconds between sweeps for assigned work; zero sweeps once, at start.
    sweep_interval: float = sweep.DEFAULT_SWEEP_INTERVAL
    #: Seconds between the manager's rounds; zero reclaims once, at start, and stops.
    rounds_interval: float = 300.0
    #: Unknown `listen` flags, deduplicated, in the order they were given.
    ignored: tuple[str, ...] = ()
    invalid_arguments: str | None = None


class _ParseFailed(Exception):
    """An argparse complaint, caught instead of exiting the process."""


class _ServeParser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        raise _ParseFailed(message)


def _parser() -> argparse.ArgumentParser:
    parser = _ServeParser(
        prog="ppy serve",
        description=(
            "Run the Papaya manager: this runtime's supervisor and the Papaya client's "
            "event loop, in one process, until told to stop."
        ),
    )
    parser.add_argument(
        "--supervised",
        action="store_true",
        help="speak the supervised JSON Lines protocol on the inherited stdout",
    )
    parser.add_argument(
        "--harness",
        default=None,
        help=f"which bundled harness this connection is labelled as ({', '.join(HARNESSES)})",
    )
    parser.add_argument(
        "--approval-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="with --supervised: how long to wait for a job.decision before denying the job",
    )
    parser.add_argument(
        "--working-directory",
        default=None,
        metavar="PATH",
        help="run every job under this directory and allow no other",
    )
    parser.add_argument(
        "--sweep-interval",
        default=None,
        metavar="SECONDS",
        help=(
            "how often to look for assigned work nothing has picked up "
            f"(default {sweep.DEFAULT_SWEEP_INTERVAL:g}, or ${sweep.SWEEP_INTERVAL_ENV}); "
            "0 sweeps once, at start"
        ),
    )
    parser.add_argument(
        "--rounds-interval",
        default=None,
        metavar="SECONDS",
        help=(
            "how often to look at every worker and loose end "
            "(default 300, or $PPY_ROUNDS_INTERVAL); 0 reclaims once, at start"
        ),
    )
    return parser


def _ignored_flags(extra: list[str]) -> tuple[str, ...]:
    """The unknown flags in `extra`, once each, in order.

    Only tokens that look like flags are named: the bare word after an unknown
    `--match` is that flag's value, and warning about it twice would say the same
    thing in two ways. Repeats collapse, so `--match a --match b` is one line.
    """
    named: list[str] = []
    for token in extra:
        if not token.startswith("-"):
            continue
        name = token.split("=", 1)[0]
        if name not in named:
            named.append(name)
    return tuple(named)


def parse_args(argv: list[str]) -> ServeOptions:
    """Read the arguments the client passes across its exec into the runtime.

    Nothing here exits. An unknown flag is ignored and named; a *wrong* value for
    a known one is carried as `invalid_arguments` so that a supervised run can
    report it after `hello` instead of dying before it.
    """
    try:
        known, extra = _parser().parse_known_args(list(argv))
    except _ParseFailed as exc:
        # The parse failed, so `known.supervised` does not exist — and whether the
        # complaint goes to stderr or down the protocol depends on that one flag.
        # Reading the raw token is the only answer available at this point.
        return ServeOptions(supervised="--supervised" in argv, invalid_arguments=str(exc))

    invalid: str | None = None
    if known.harness is not None and known.harness not in HARNESSES:
        invalid = f"--harness must be one of {', '.join(HARNESSES)}, not {known.harness!r}"
    # The flag wins over the environment, and the environment over the default.
    interval = sweep.DEFAULT_SWEEP_INTERVAL
    try:
        interval = (
            sweep.parse_interval(known.sweep_interval, source="--sweep-interval")
            if known.sweep_interval is not None
            else sweep.interval_from_env()
        )
    except ValueError as exc:
        invalid = invalid or str(exc)
    from papaya_agent_runtime import rounds

    rounds_interval = rounds.DEFAULT_ROUNDS_INTERVAL
    try:
        rounds_interval = (
            sweep.parse_interval(known.rounds_interval, source="--rounds-interval")
            if known.rounds_interval is not None
            else rounds.interval_from_env()
        )
    except ValueError as exc:
        invalid = invalid or str(exc)
    return ServeOptions(
        supervised=bool(known.supervised),
        harness=known.harness,
        approval_timeout=known.approval_timeout,
        working_directory=known.working_directory,
        sweep_interval=interval,
        rounds_interval=rounds_interval,
        ignored=_ignored_flags(extra),
        invalid_arguments=invalid,
    )


# ── the lease identity, across restarts ─────────────────────────────────────


def stored_session_ids() -> dict[str, str]:
    """Every remembered `{connection id: session id}`, or nothing readable."""
    try:
        data = json.loads(papaya_sessions_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): str(value).strip()
        for key, value in data.items()
        if isinstance(value, str) and value.strip()
    }


def remember_session_id(connection_id: str, session_id: str) -> None:
    """Persist this connection's session id, replacing any earlier one."""
    if not connection_id or not session_id:
        return
    path = papaya_sessions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = stored_session_ids()
    data[connection_id] = session_id
    body = json.dumps(data, indent=2, sort_keys=True) + "\n"
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(body, encoding="utf-8")
    os.replace(temp, path)


def session_id_for(connection_id: str) -> str:
    """This manager's lease identity for one Papaya connection.

    The same id every time, so the acquire-or-extend reserve extends the leases
    this manager already holds after a restart. A machine with no connection at
    all still gets an id — the build that follows is about to fail with something
    far more useful than "no session id".
    """
    from papaya_agent_client.listener import mint_session_id

    existing = stored_session_ids().get(connection_id)
    if existing:
        return existing
    minted = mint_session_id()
    remember_session_id(connection_id, minted)
    return minted


# ── the supervised `hello`, with who is answering it ────────────────────────


def runtime_descriptor() -> dict[str, str]:
    """What `hello` says this process is, so a host never has to infer it."""
    from papaya_agent_runtime import __version__

    return {"name": capabilities.RUNTIME, "version": __version__}


def protocol_writer(stream: Any, *, on_stalled: Any = None) -> Any:
    """A `ProtocolWriter` whose `hello` and `status` also carry the runtime behind the client.

    It is also where this process hears the client call a job stalled: the stall
    observation is a `job.stalled` message and nothing else, so ``on_stalled`` is
    called with its job id once the host has been sent it. What the runtime then
    knows about the job's worker is the runner's to say (:meth:`TicketRunner.stalled`).

    The client's `hello` describes the *client*: its protocol version, its own
    version, the home and the working directory. A host that exec'd a runtime
    needs two more facts — which runtime answered, and what this machine needs
    from its owner — and they are added here as ``runtime``: the descriptor plus
    ``blockers`` (``[{code, title, steps, since}]``, redacted; see
    :mod:`~papaya_agent_runtime.blockers`), on `hello` and on every `status`, so
    the desktop app can show "Setup needed on this Mac" with copyable commands.
    Everything else goes through untouched.

    0.15.1 has the destination but not the road: `Supervisor.runtime` is a field
    the client deliberately never sets ("a runtime the client handed the process
    to fills it"), but `build_supervised_listener` constructs the `Supervisor`
    itself, takes no `runtime=` argument, and calls `hello()` before it returns —
    so there is no moment at which a host can reach the field. Injecting on the
    message is the seam that exists. When the builder forwards `runtime=`, delete
    this and pass it.
    """
    from papaya_agent_client.supervisor import ProtocolWriter

    from papaya_agent_runtime import blockers

    class _RuntimeWriter(ProtocolWriter):
        def send(self, message_type: str, **fields: Any) -> dict[str, Any]:
            if message_type in ("hello", "status"):
                fields.setdefault(
                    "runtime", {**runtime_descriptor(), "blockers": blockers.current()}
                )
            message = super().send(message_type, **fields)
            if message_type == "job.stalled" and on_stalled is not None:
                try:
                    on_stalled(str(fields.get("job_id") or ""))
                except Exception as exc:  # noqa: BLE001 - the protocol must outlive the answer
                    log.warning("[serve] Could not answer a stall: %s", exc)
            return message

    return _RuntimeWriter(stream)


# ── the runner ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Held:
    """A ticket this manager has taken and is now holding the lease for."""

    task_id: int
    #: The ticket's run. A worker dispatched into it is this ticket's worker.
    run_id: int
    #: The repository the item names, once registered; ``None`` when it names
    #: none, which leaves the choice to the brief turn.
    repo: str | None
    event: papaya_events.PapayaEvent
    #: The working phase to pick the ticket up from, or ``None`` for a fresh pickup.
    resume_from: str | None = None
    #: Taken up again on a task this runtime already had, by the rounds' reclaim:
    #: the run's history is not news, so its progress is not relayed a second time.
    reclaimed: bool = False
    #: The newest ledger event that was already history when the ticket was offered
    #: back, when the offer said. Progress after it is news, however late the hold
    #: starts; ``None`` falls back to the newest event when the hold starts.
    reported_until: int | None = None


@dataclass(frozen=True)
class Declined:
    """A ticket this manager is not the right machine for, and why."""

    reason: str
    #: Refused because this machine needs its owner (a blocker). The ticket then
    #: gets the one neutral comment instead of nothing.
    setup: bool = False
    event: papaya_events.PapayaEvent | None = None
    verdict: readiness.Readiness | None = None


@dataclass(frozen=True)
class HandBack:
    """The work on a held ticket cannot go on here, and why."""

    reason: str
    #: The job a manager turn missed twice, when that is why: a deficiency of the runtime.
    missed: str = ""
    #: Handed back because this machine needs its owner: the comment is the
    #: neutral line, never the reason.
    setup: bool = False


@dataclass(frozen=True)
class Worker:
    """The worker task dispatched for a ticket, as the ledger has it now."""

    task_id: int
    status: str
    repo: str | None
    branch: str | None


@dataclass(frozen=True)
class Trigger:
    """Something a worker did that a manager turn has to answer."""

    #: The phase it moves the ticket to: `blocked` or `reviewing`, or `delivering`.
    phase: str
    event_id: int
    #: The worker's question, or what stopped it — verbatim, for the turn to read.
    detail: str = ""
    #: True when the worker did not finish: the review turn is then a steer.
    failure: bool = False
    #: The ledger event kind that raised it (`worker_done`, `worker_stopped`, ...).
    kind: str = ""


@dataclass
class Ticket:
    """The runner's in-memory view of one held ticket while it works it."""

    held: Held
    job: Any
    #: The phase last entered, which is what a progress line in between says.
    phase: str = PHASE_PICKED_UP
    #: The newest ledger event already read for this ticket's run.
    cursor: int = 0
    #: Worker progress at or before this id was reported by an earlier hold.
    quiet_until: int = 0
    worker: Worker | None = None
    trigger: Trigger | None = None
    #: Set when the listener cancels the hold (shutdown), which does not set
    #: `job.stop`: a turn still running on its thread must end then too.
    cancelled: bool = False
    #: The phase the last comment on the work item was about. A comment is posted
    #: only when the phase differs, so a retry or a wait never repeats one.
    said: str | None = None
    #: Whether the review turn's own report was found on the work item: ``True``
    #: it was, ``False`` it was not (the runner posts the fallback), ``None`` the
    #: record could not be read (nothing is claimed either way).
    reported: bool | None = False
    #: Comments by somebody other than this agent, noticed and not yet handled,
    #: oldest first. A comment that arrives while a turn runs waits here.
    pending: list[dict[str, Any]] = field(default_factory=list)
    #: When the comments were last read, on the runner's clock; ``None`` reads
    #: them at the next chance, which is what the end of every turn asks for.
    comments_read_at: float | None = None
    #: How many times in a row the runner has sent a stopped worker back to its gate.
    gate_steers: int = 0
    #: The gate recorded at the worker's head when it last stopped or said done, as one
    #: line for the review turn; empty when none was.
    recorded_gate: str = ""
    #: How many times in a row the runner has sent a worker back for uncommitted work.
    dirty_steers: int = 0
    #: The uncommitted-work finding at the last review, for the review turn; empty when clean.
    uncommitted: str = ""
    #: What the manager's rounds noticed and the ticket's loop has not acted on yet.
    nudges: list[Nudge] = field(default_factory=list)
    #: The turn running for this ticket right now, if one is.
    turn_running: str | None = None
    #: When the last liveness check ran, on the runner's clock; ``None`` before the first.
    liveness_at: float | None = None
    #: The newest worker event a liveness line has already accounted for.
    liveness_cursor: int = 0
    #: Where the last turn's transcript is, for the evidence of a self-report.
    last_transcript: str = ""

    def should_stop(self) -> bool:
        return self.cancelled or self.job.stop.is_set()


def _one_line(text: object) -> str:
    """A comment is one line: the first line of ``text``, whitespace collapsed."""
    lines = [line for line in str(text or "").strip().splitlines() if line.strip()]
    return " ".join(lines[0].split()) if lines else ""


def _is_agent_comment(comment: dict[str, Any]) -> bool:
    return str(comment.get("author_type") or "") == "agent" or bool(comment.get("author_actor"))


def _comment_ids(comments: list[dict[str, Any]]) -> frozenset[str]:
    return frozenset(str(comment.get("id")) for comment in comments if comment.get("id"))


def is_own_comment(comment: dict[str, Any], agent_id: str | None) -> bool:
    """Did this agent write ``comment``? Only then is it not worth waking for.

    Another agent's comment is somebody else talking. An agent comment whose
    author cannot be told apart — no id on it, or no id for this agent — is
    taken as this agent's own: every comment the runner and its turns post is
    an agent comment, and waking on those would answer ourselves.
    """
    if not _is_agent_comment(comment):
        return False
    author = str(comment.get("author_id") or "")
    return not agent_id or not author or author == agent_id


def comment_author(comment: dict[str, Any]) -> str:
    """Who wrote a comment, in the words a progress line can use."""
    actor = comment.get("author_actor")
    if isinstance(actor, dict):
        for key in ("name", "handle", "agent_name", "agent_handle"):
            if str(actor.get(key) or "").strip():
                return str(actor[key]).strip()
    kind = str(comment.get("author_type") or "").strip() or "someone"
    author = str(comment.get("author_id") or "").strip()
    return f"{kind} {author}" if author else kind


def _comments_fact(comments: list[dict[str, Any]]) -> str:
    """The comments an answer turn is woken for, verbatim, oldest first."""
    return "\n\n".join(
        f"From {comment_author(c)} (comment {c.get('id')}"
        + (f", {c['created_at']}" if c.get("created_at") else "")
        + f"):\n{str(c.get('body') or '').strip()}"
        for c in comments
    )


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def comments_after(
    comments: list[dict[str, Any]], handled: dict[str, Any]
) -> list[dict[str, Any]] | None:
    """The comments newer than the handled record, or ``None`` when it cannot tell.

    The record names the newest comment handled; everything after it in
    Papaya's order (oldest first) is new. A record with no comment id was taken
    when the item had none, so every comment is new. A record whose comment is
    gone falls back to its timestamp; with neither, the answer is ``None`` and
    the caller starts a fresh record rather than guess.
    """
    handled_id = handled.get("comment_id")
    if not handled_id:
        return list(comments)
    for index, comment in enumerate(comments):
        if str(comment.get("id")) == str(handled_id):
            return comments[index + 1 :]
    since = _parse_time(handled.get("created_at")) if handled.get("created_at") else None
    if since is None:
        return None
    return [
        comment
        for comment in comments
        if (stamp := _parse_time(comment.get("created_at"))) is not None and stamp > since
    ]


def waiting_reason(result: object) -> str | None:
    """What a turn said it is waiting for, or ``None`` if it did not say it is.

    The contract is in the brief and review prompts: a turn that cannot finish
    (a gate that outlasts it) ends with a message whose first line starts
    `WAITING:`. A transcript carries more than that message, so the last such line
    in its tail is the one that counts. An empty reason is still a wait.
    """
    text = result.tail() if hasattr(result, "tail") else str(result or "")
    for line in reversed(text.splitlines()):
        stripped = line.strip().lstrip("*_`> ").strip()
        if stripped.startswith(prompts.WAITING_PREFIX):
            reason = stripped.removeprefix(prompts.WAITING_PREFIX).strip().rstrip("*_`").strip()
            return reason or "(no reason given)"
    return None


def rerun_delay(waits: int, gate_budget: float | None = None) -> float:
    """Seconds before rerunning a turn that has ended `WAITING:` ``waits`` times in a row.

    ``gate_budget`` is the repository's gate budget when its history stands behind one:
    a turn waiting on a gate known to take forty minutes is not rerun every five.
    """
    delay = min(WAIT_FIRST_SECONDS * 2 ** max(waits - 1, 0), WAIT_MAX_SECONDS)
    return max(delay, gate_budget) if gate_budget else delay


def turn_repo(ticket: Ticket) -> str | None:
    """The repository a ticket's turns are about: its worker's, else the one the item names."""
    worker = ticket.worker
    return (worker.repo if worker is not None else None) or ticket.held.repo


def known_gate_budget(repo: str | None) -> float | None:
    """The longer of this repository's gate and full-suite budgets, when history backs one."""
    from papaya_agent_runtime import budgets

    found = budgets.longest_gate(repo)
    return found.seconds if found is not None else None


def runtime_report(result: object) -> str | None:
    """What a turn said the runtime got in its way with, or ``None`` if it said nothing.

    The contract is the `RUNTIME:` rule every turn prompt carries: a line of its
    own, at the end of the turn's last message. The last such line in the tail
    counts; one that only repeats the prompt's placeholder is not a report.
    """
    text = result.tail() if hasattr(result, "tail") else str(result or "")
    for line in reversed(text.splitlines()):
        stripped = line.strip().lstrip("*_`> ").strip()
        if not stripped.startswith(prompts.RUNTIME_PREFIX):
            continue
        said = stripped.removeprefix(prompts.RUNTIME_PREFIX).strip().rstrip("*_`").strip()
        if not said or said.startswith("<"):
            return None
        return said
    return None


def ticket_key(event: papaya_events.PapayaEvent) -> str:
    """The ticket's key (`PAP-213`), or its work item id: what a self-report may name."""
    item = event.payload.get("work_item")
    if isinstance(item, dict):
        for name in ("key", "identifier", "ticket_key"):
            if str(item.get(name) or "").strip():
                return str(item[name]).strip()
    return str(event.work_item_id or event.subject or "")


def private_strings(event: papaya_events.PapayaEvent) -> list[str]:
    """What a self-report must never carry about this ticket: its text and its people.

    The title, description and acceptance criteria, and every string under a key
    that names a person (a name, a handle, an email), however deep in the item.
    """
    found: list[str] = []

    def walk(value: object, key: str = "") -> None:
        if isinstance(value, dict):
            for name, inner in value.items():
                walk(inner, str(name).lower())
        elif isinstance(value, list):
            for inner in value:
                walk(inner, key)
        elif (
            isinstance(value, str)
            and value.strip()
            and (
                key in ("title", "description", "acceptance_criteria", "body")
                or any(part in key for part in ("name", "handle", "email", "author", "assignee"))
            )
        ):
            found.append(value)

    walk(event.payload.get("work_item"))
    who = papaya.identity()
    for attr in ("agent_name", "agent_handle", "addressed"):
        value = getattr(who, attr, None) if who is not None else None
        if isinstance(value, str) and value.strip():
            found.append(value)
    return found


def gate_steer_message(
    detail: str, task_id: int | str = "<task id>", result: gate.GateResult | None = None
) -> str:
    """What a worker is told when the runner sends it back to its gate.

    With a red ``result`` it is the gate's own summary; without one, the session ended
    before any gate was recorded at the worker's head.
    """
    command = f"`ppy gate run --task {task_id}`"
    if result is not None:
        return (
            f"Your recorded {result.line()}. Output: {result.output_path}. Fix what it "
            f"reports, commit, then run {command} in the foreground and file your done "
            "note once it is green."
        )
    why = _one_line(detail)
    return (
        "Your session ended before your verification gate finished"
        + (f" ({why})" if why else "")
        + f", and no gate result is recorded at your head. Run {command} in the foreground: "
        "it runs the gate as the supervisor's process and records the result. "
        f"{prompts.TEN_MINUTE_RULE} If it says the gate is still running, run it again. "
        "Then file your done note with the gate's summary line, and push your branch."
    )


def uncommitted_files(worker_task_id: int) -> list[str] | None:
    """The paths `git status` reports in the worker's worktree, or ``None`` if unreadable.

    Ignored paths (the evidence directory is excluded at dispatch) are not reported.
    """
    conn = db.init_db()
    try:
        task = store.get_task(conn, worker_task_id)
    finally:
        conn.close()
    cwd = str(task["worktree_path"] or "") if task is not None else ""
    if not cwd or not os.path.isdir(cwd):
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [line[3:] for line in proc.stdout.splitlines() if line.strip()]


def uncommitted_finding(files: list[str]) -> str:
    """The review finding for a worktree holding what its branch does not."""
    return f"uncommitted work in the worktree: {len(files)} files"


def uncommitted_steer_message(files: list[str], branch: str | None) -> str:
    """What a worker is told when it says done with work that is not on its branch."""
    shown = ", ".join(f"`{path}`" for path in files[:10]) + (" and more" if len(files) > 10 else "")
    push = f"`git push origin HEAD:{branch}`" if branch else "your lease branch"
    return (
        f"Review finding: {uncommitted_finding(files)} ({shown}). The review reads your "
        "pushed branch, never your worktree, so none of that is reviewed. Commit what should "
        f"ship and push it ({push}), or discard what should not, then file your done note "
        "again."
    )


def steer_worker(task_id: int, message: str) -> dict[str, Any]:
    """`ppy steer`, from inside `serve`: through the supervisor this process runs."""
    from papaya_agent_runtime.supervisor.client import SupervisorClient

    resp = SupervisorClient().steer_task(task_id, message)
    if not resp.get("ok"):
        raise RuntimeError(str(resp.get("error") or "the supervisor refused the steer"))
    return resp


def stop_and_resume_worker(task_id: int, message: str) -> dict[str, Any]:
    """A check-in's "stop and resume with": the steer that supersedes, from inside `serve`.

    `replace` delivery interrupts a live turn where the provider can and resumes
    it with this message alone; where it cannot, it supersedes whatever was
    queued, so the message is the next and only thing the worker reads. A worker
    with no live turn is resumed with it straight away.
    """
    from papaya_agent_runtime.supervisor.client import SupervisorClient

    resp = SupervisorClient().steer_task(task_id, message, delivery="replace")
    if not resp.get("ok"):
        raise RuntimeError(str(resp.get("error") or "the supervisor refused the steer"))
    return resp


def checkin_decision(result: object) -> tuple[str, str] | None:
    """What a check-in turn decided, as ``(decision, message)``, or ``None`` if it did not say.

    The contract is `prompts/checkin.md`: the last `CHECK-IN:` line in the
    transcript's tail, one of `continue`, `steer <message>` or `stop and resume
    with <message>`. A steer or a stop with no message is no decision: there is
    nothing to give the worker, and the runner never writes one itself.
    """
    text = result.tail() if hasattr(result, "tail") else str(result or "")
    for line in reversed(text.splitlines()):
        stripped = line.strip().lstrip("*_`> ").strip().rstrip("*_`").strip()
        if not stripped.startswith(prompts.CHECKIN_PREFIX):
            continue
        said = stripped.removeprefix(prompts.CHECKIN_PREFIX).strip()
        lowered = said.lower()
        # Longest first: "stop and resume with" before anything it could start with.
        for decision in (prompts.CHECKIN_STOP, prompts.CHECKIN_STEER, prompts.CHECKIN_CONTINUE):
            if lowered == decision or lowered.startswith(decision + " "):
                message = said[len(decision) :].strip().lstrip(":").strip()
                if decision == prompts.CHECKIN_CONTINUE:
                    return decision, ""
                return (decision, message) if message else None
        return None
    return None


class _Stopped(Exception):
    """The client stopped the hold while the runner was working it."""


def _title(event: papaya_events.PapayaEvent) -> str:
    work_item = event.payload.get("work_item")
    if isinstance(work_item, dict):
        title = str(work_item.get("title") or "").strip()
        if title:
            return title
    return " ".join(part for part in (event.kind, event.subject) if part) or "Papaya work"


def phase_for_stop(reason: str | None) -> str:
    """Which phase a stopped hold ended in, in the client's own vocabulary."""
    from papaya_agent_client.listener import STOP_HANDED_BACK, STOP_STALLED

    if reason == STOP_HANDED_BACK:
        return PHASE_HANDED_BACK
    if reason == STOP_STALLED:
        return PHASE_STALLED
    # No reason at all is a lost lease or this process shutting down. Both end the
    # hold without anyone declining anything, which is what `released` names.
    return PHASE_RELEASED


def _result(job: Any, exit_code: int, output: str) -> dict[str, Any]:
    """What `run_command` answers, so the loop cannot tell a runner from a harness."""
    return {
        "request_id": job.job_id,
        "exit_code": exit_code,
        "output": output,
        "error": None,
        "duration_seconds": 0.0,
    }


def _report_progress(job: Any, phase: str, detail: str) -> None:
    """Say what this job is doing now, and never let saying it end the hold.

    `Job.report_progress` (papaya-agent-client 0.15.1) is the client's own path
    for this: a supervised host gets a `job.progress` message, a terminal
    listener gets a log line, and the runner does not have to know which it is
    talking to. It is wrapped only because a hold must outlive a reporting
    failure — the lease is the thing that matters, and a host that has gone away
    is not a reason to give a work item back.
    """
    try:
        job.report_progress(phase, detail)
    except Exception as exc:  # noqa: BLE001 - reporting must never end a hold
        log.warning("[serve] Could not report progress for %s: %s", job.job_id, exc)


class TicketRunner:
    """Take one approved job's ticket and work it through its phases to the end.

    Everything the runner itself does is mechanical: record, launch a turn, read
    the ledger, set a status. Everything that needs judgment is a manager turn.
    All of the blocking work — reading the envelope, cloning a repository,
    SQLite, a turn that runs for twenty minutes — runs on a thread, because the
    client renews this ticket's lease on the event loop this coroutine runs on,
    and a manager that stops renewing while it works loses the very lease it holds.

    Every collaborator with an outside world is a keyword seam, so the whole
    phase machine is testable with fakes: ``run_turn`` (the harness), ``opener``
    (Papaya's HTTP API), ``worker_capacity`` (the worker pool), ``steer`` (the
    supervisor), ``clock`` and ``config``.
    """

    def __init__(
        self,
        *,
        check_readiness=None,
        run_turn=None,
        config=None,
        opener=None,
        worker_capacity=None,
        poll_seconds: float = POLL_SECONDS,
        runtime_dir: str | None = None,
        turn_tools=None,
        comment_poll_seconds: float = COMMENT_POLL_SECONDS,
        clock=None,
        agent_id: str | None = None,
        steer=None,
        gate_verdict=None,
        stop_and_resume=None,
        gate_state=None,
        liveness_seconds: float | None = None,
        uncommitted=None,
    ) -> None:
        # Checked per job rather than once, so a runtime that is set up *while*
        # `serve` is running starts taking work without a restart.
        self._check_readiness = check_readiness or readiness.check
        self._run_turn = run_turn
        self._config = config
        self._opener = opener
        self._worker_capacity = worker_capacity or default_worker_capacity
        self._poll_seconds = float(poll_seconds)
        self._runtime_dir = runtime_dir
        #: `manager.launch.prepare_turn_tools`' seam: what gives a turn MCP and the plugin.
        self._turn_tools = turn_tools
        self._comment_poll_seconds = float(comment_poll_seconds)
        #: What "a minute since the comments were last read" is measured on.
        self._clock = clock or time.monotonic
        #: Who "this agent" is when telling a person's comment from our own;
        #: read from the connection on first use when not given.
        self._agent_id = agent_id
        #: `ppy steer`'s seam: how a worker that stopped mid-gate is sent back.
        self._steer = steer or steer_worker
        #: `gate.verdict`'s seam: the recorded gate result at a worker's head.
        self._gate_verdict = gate_verdict or gate.verdict
        #: How a check-in turn's "stop and resume with" reaches the worker.
        self._stop_and_resume = stop_and_resume or stop_and_resume_worker
        #: `rounds.gate_state`'s seam: whether a worker's gate runs under the supervisor.
        self._gate_state = gate_state or default_gate_state
        #: Seconds between liveness lines; ``None`` reads `health.liveness_minutes`.
        self._liveness_seconds = liveness_seconds
        #: The event loop the holds run on, for a stall heard from the protocol writer.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._answering: set[asyncio.Task[Any]] = set()
        #: What is in a worker's worktree and not on its branch, read before a review.
        self._uncommitted = uncommitted or uncommitted_files
        #: Every ticket held right now, by its task id: what the rounds walk.
        self.held: dict[int, Ticket] = {}
        #: Work item id -> the task the rounds re-offered it for, so the offer
        #: (which carries a new event key) lands on that task, not on a new one, and
        #: the newest event id at the moment of the offer.
        self._reclaiming: dict[str, tuple[int, int | None]] = {}

    def reclaim(self, work_item_id: str, task_id: int, reported_until: int | None = None) -> None:
        """Take the next offer of ``work_item_id`` up on ``task_id``, the rounds' reclaim.

        ``reported_until`` is the newest event id when the offer was made. The hold
        starts on a thread some time later — seconds, on a loaded machine — and a
        worker's progress in between is news: marking history at the hold's start
        instead silently dropped it.
        """
        self._reclaiming[str(work_item_id)] = (int(task_id), reported_until)

    def forget_reclaim(self, work_item_id: str) -> None:
        self._reclaiming.pop(str(work_item_id), None)

    async def __call__(self, job: Any) -> dict[str, Any]:
        try:
            outcome = await asyncio.to_thread(self.take, job)
        except Exception as exc:
            log.exception("[serve] Taking %s failed", job.job_id)
            await asyncio.to_thread(deficiencies.record_exception, "a ticket's pickup", exc)
            raise
        if isinstance(outcome, Declined):
            log.info("[serve] Declining %s: %s", job.job_id, outcome.reason)
            job.decline(outcome.reason)
            if outcome.setup and outcome.event is not None and outcome.verdict is not None:
                await asyncio.to_thread(
                    self._setup_comment, outcome.event, outcome.verdict, job.env
                )
            return _result(job, _declined_exit_code(), outcome.reason)

        held = outcome
        ticket = Ticket(held=held, job=job)
        self._loop = asyncio.get_running_loop()
        self.held[held.task_id] = ticket
        alive = asyncio.create_task(self._keep_alive(ticket))
        try:
            return await self._hold(ticket)
        except Exception as exc:
            log.exception("[serve] Holding task %d failed", held.task_id)
            await asyncio.to_thread(
                deficiencies.record_exception,
                "a ticket's hold",
                exc,
                scrub=private_strings(held.event),
                **self._evidence(ticket),
            )
            raise
        finally:
            alive.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await alive
            if self.held.get(held.task_id) is ticket:
                del self.held[held.task_id]

    # -- liveness: the worker's activity is the ticket's -------------------------

    def stalled(self, job_id: str) -> None:
        """The client called ``job_id`` stalled: say at once what its worker is doing.

        Called from the protocol writer, on whichever thread sent `job.stalled`. A
        progress line is activity to the client, so an active worker's line ends the
        stall before its grace runs out; a worker that really is quiet gets nothing,
        and the client's hand-back goes ahead.
        """
        ticket = next((t for t in self.held.values() if t.job.job_id == job_id), None)
        loop = self._loop
        if ticket is None or loop is None or loop.is_closed():
            return
        log.info("[serve] %s was reported stalled; looking at its worker", job_id)
        loop.call_soon_threadsafe(self._answer_stall, ticket)

    def _answer_stall(self, ticket: Ticket) -> None:
        answering = asyncio.ensure_future(self._say_alive(ticket, stalled=True))
        self._answering.add(answering)
        answering.add_done_callback(self._answering.discard)

    async def _keep_alive(self, ticket: Ticket) -> None:
        """Say what the worker is doing, at most once a liveness interval, while it does it.

        The runtime records a worker's every tool call and a heartbeat for a long one,
        but none of that is activity to the client, which watches progress lines. So
        while the ticket's worker session is live, or its gate runs under the
        supervisor, one line of that record goes out per interval. A check that finds
        no new worker event and no running gate says nothing: silence stays silence,
        so the client's stall observation still fires for a worker that truly went
        quiet, and what quiet means for a repository is still the rounds' budget.
        """
        interval = await asyncio.to_thread(self._liveness_interval)
        ticket.liveness_cursor = max(ticket.liveness_cursor, await asyncio.to_thread(_max_event_id))
        ticket.liveness_at = self._clock()
        while not ticket.should_stop():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ticket.job.stop.wait(), timeout=self._poll_seconds)
            if ticket.should_stop() or self._clock() - ticket.liveness_at < interval:
                continue
            ticket.liveness_at = self._clock()
            await self._say_alive(ticket)

    async def _say_alive(self, ticket: Ticket, *, stalled: bool = False) -> bool:
        """One liveness line for this ticket, if its worker or gate is active. Never fatal."""
        try:
            line = await asyncio.to_thread(self._activity_line, ticket)
        except Exception as exc:  # noqa: BLE001 - liveness must never end a hold
            log.warning("[serve] Could not read %s's worker activity: %s", ticket.job.subject, exc)
            return False
        if line is None:
            if stalled:
                log.info(
                    "[serve] %s shows no worker activity; leaving the stall", ticket.job.job_id
                )
            return False
        ticket.liveness_at = self._clock()
        _report_progress(ticket.job, ticket.phase, line)
        return True

    def _activity_line(self, ticket: Ticket) -> str | None:
        """What the ticket's worker and gate are doing since the last line, or ``None``."""
        worker = ticket.worker
        if worker is None:
            return None
        activity = worker_activity(worker.task_id, ticket.liveness_cursor)
        if activity is not None:
            ticket.liveness_cursor = activity.last_event_id
        try:
            gate_now = self._gate_state(worker.task_id)
        except Exception as exc:  # noqa: BLE001 - an unreadable gate is not a running one
            log.warning("[serve] Could not read worker task %d's gate: %s", worker.task_id, exc)
            gate_now = None
        running = gate_now if gate_now is not None and gate_now.running else None
        if activity is not None and (running is not None or worker_session_live(worker.task_id)):
            said = activity.line(worker.task_id)
            return f"{said}; {gate_line(running)}" if running is not None else said
        return gate_line(running) if running is not None else None

    def _liveness_interval(self) -> float:
        if self._liveness_seconds is not None:
            return float(self._liveness_seconds)
        config = self._config() if self._config is not None else _load_config()
        minutes = getattr(getattr(config, "health", None), "liveness_minutes", None)
        return float(minutes) * 60 if minutes else LIVENESS_SECONDS

    # -- self-reports: what this ticket showed about the runtime ------------------

    def _evidence(self, ticket: Ticket, **extra: Any) -> dict[str, Any]:
        """The facts a self-report about this ticket may carry: ids, never text."""
        worker = ticket.worker
        return {
            "phase": ticket.phase,
            "repo": ticket.held.repo or (worker.repo if worker is not None else None),
            "ticket": ticket_key(ticket.held.event),
            "task_id": ticket.held.task_id,
            "run_id": ticket.held.run_id,
            "worker_task_id": worker.task_id if worker is not None else None,
            "transcript": ticket.last_transcript,
            **extra,
        }

    def _deficiency(
        self, ticket: Ticket, kind: str, detail: str, *, scope: str | None = None, **extra: Any
    ) -> None:
        """Record a deficiency about this ticket's handling, with its ids. Blocking: on a thread."""
        try:
            evidence = {**self._evidence(ticket, **extra), "event_id": _max_event_id()}
            scrub = private_strings(ticket.held.event)
        except Exception as exc:  # noqa: BLE001 - reporting the runtime must never break a hold
            log.warning("[serve] Could not gather a self-report's evidence: %s", exc)
            return
        deficiencies.record(kind, detail, evidence=evidence, scope=scope, scrub=scrub)

    async def _hold(self, ticket: Ticket) -> dict[str, Any]:
        held, job = ticket.held, ticket.job
        where = f" in {held.repo}" if held.repo else ""
        log.info("[serve] Holding %s for task %d%s", job.subject, held.task_id, where)
        if held.reclaimed or held.resume_from is not None:
            ticket.quiet_until = (
                held.reported_until
                if held.reported_until is not None
                else await asyncio.to_thread(_max_event_id)
            )
        if held.resume_from is None:
            await self._status(ticket, papaya_events.STATUS_IN_PROGRESS)
            _report_progress(job, PHASE_PICKED_UP, f"Recorded as task {held.task_id}{where}.")
            said = "Picked up; choosing the repository and writing the brief."
            await self._say(ticket, PHASE_BRIEFING, said)
        else:
            # A redelivered ticket that was already being worked goes back to where
            # it was. Nothing is picked up twice: no second brief, no second status,
            # and no second comment for the phase the earlier hold already announced.
            ticket.said = held.resume_from
            _report_progress(
                job, held.resume_from, f"Resuming task {held.task_id} from {held.resume_from}."
            )

        try:
            ending = await self._work(ticket)
        except asyncio.CancelledError:
            ticket.cancelled = True
            await asyncio.to_thread(self._record_phase, held.task_id, PHASE_RELEASED)
            raise
        except _Stopped:
            ending = None

        if isinstance(ending, HandBack):
            if ending.missed:
                await asyncio.to_thread(
                    self._deficiency, ticket, deficiencies.MISSED_TURN, ending.missed
                )
            return await self._hand_back(ticket, ending.reason, setup=ending.setup)
        if ending is None:
            return await self._stopped(ticket)
        await asyncio.to_thread(self._record_phase, held.task_id, PHASE_RELEASED)
        log.info("[serve] Released %s for task %d (done)", job.subject, held.task_id)
        return _result(job, 0, f"task {held.task_id} {PHASE_REPORTED}")

    # -- the phase machine ---------------------------------------------------

    async def _work(self, ticket: Ticket) -> HandBack | str:
        """Walk the ticket from where it is to the end. Returns how it ended.

        A plain loop over the current phase, each step answering the next one:
        resuming is nothing more than starting the loop at a later phase.
        """
        phase = ticket.held.resume_from or PHASE_BRIEFING
        while True:
            self._check_stop(ticket)
            if phase == PHASE_BRIEFING:
                step = await self._brief(ticket)
            elif phase in (PHASE_DISPATCHED, PHASE_BLOCKED):
                step = await self._watch(ticket)
            elif phase == PHASE_REVIEWING:
                step = await self._review(ticket)
            elif phase == PHASE_DELIVERING:
                step = await self._deliver(ticket)
            elif phase == PHASE_REPORTED:
                return PHASE_REPORTED
            else:  # pragma: no cover - every phase above is the whole vocabulary
                return HandBack(f"the runner does not know how to continue from {phase!r}")
            if isinstance(step, HandBack):
                return step
            phase = step

    async def _brief(self, ticket: Ticket) -> HandBack | str:
        """Brief-and-dispatch: until a worker row is in the ticket's run."""
        held = ticket.held
        misses: list[str] = []
        tail = ""
        waits = 0
        while True:
            worker = await asyncio.to_thread(find_worker, held)
            if worker is not None:
                return await self._dispatched(ticket, worker)
            # A ticket resumed while its question still waits on a person waits
            # first, rather than asking the same question again.
            if await self._wait_on_person(ticket):
                continue
            briefing = "Briefing: choosing the repository and writing the brief."
            await self._enter(ticket, PHASE_BRIEFING, briefing, say=briefing)
            result = await self._turn(ticket, prompts.BRIEF, self._brief_facts(ticket, tail))
            worker = await asyncio.to_thread(find_worker, held)
            if worker is not None:
                await self._check_acceptance_criteria(ticket, worker)
                return await self._dispatched(ticket, worker)
            if await self._wait_on_person(ticket):
                continue
            if await self._wait_for_slot(ticket):
                continue
            if (
                waited := await self._rerun_later(ticket, prompts.BRIEF, result, waits)
            ) is not None:
                waits, tail = waits + 1, waited
                continue
            outcome = self._missed(ticket, misses, "dispatching a worker", result)
            if isinstance(outcome, HandBack):
                return outcome
            tail = outcome

    async def _dispatched(self, ticket: Ticket, worker: Worker) -> str:
        ticket.worker = worker
        repo = f" in {worker.repo}" if worker.repo else ""
        dispatched = f"Dispatched worker task {worker.task_id}{repo}."
        await self._enter(ticket, PHASE_DISPATCHED, dispatched, say=dispatched)
        return PHASE_DISPATCHED

    async def _check_acceptance_criteria(self, ticket: Ticket, worker: Worker) -> None:
        """The brief turn's first obligation, read off the record rather than assumed.

        A brief with Goals means done was defined; the work item carrying no
        acceptance criteria then means the turn did not write them where the
        person who asked can see and correct them. One rerun with a pointed
        addendum, then a progress line: the brief exists, so the ticket goes on.
        A record that cannot be read claims nothing and triggers nothing.
        """
        if not await asyncio.to_thread(brief_has_goals, worker):
            return
        for attempt in range(TURN_ATTEMPTS):
            criteria = await asyncio.to_thread(self._acceptance_criteria, ticket)
            if criteria is None or criteria:
                return
            if attempt + 1 >= TURN_ATTEMPTS:
                break
            _report_progress(
                ticket.job,
                ticket.phase,
                "The work item has no acceptance criteria yet; running the brief turn once more.",
            )
            facts = {
                **self._brief_facts(ticket, ""),
                prompts.ADDENDUM_FACT: prompts.ACCEPTANCE_ADDENDUM,
            }
            await self._turn(ticket, prompts.BRIEF, facts)
        _report_progress(
            ticket.job,
            ticket.phase,
            "The work item still has no acceptance criteria after a second brief turn; "
            "going on with the brief's Goals.",
        )

    def _acceptance_criteria(self, ticket: Ticket) -> str | None:
        """The item's acceptance criteria as Papaya has them now, or ``None`` if unreadable."""
        try:
            fresh = papaya_events.hydrate_work_item(
                ticket.held.event, environ=ticket.job.env, **self._opener_kwargs()
            )
        except papaya_events.PapayaEventError:
            return None
        if fresh is ticket.held.event:
            return None  # not connected: nothing was read
        item = fresh.payload.get("work_item")
        return (
            str(item.get("acceptance_criteria") or "").strip() if isinstance(item, dict) else None
        )

    async def _watch(self, ticket: Ticket) -> HandBack | str:
        """Wait on the worker: report its progress, and stop at what needs a turn."""
        while True:
            self._check_stop(ticket)
            read = await asyncio.to_thread(read_run, ticket.held, ticket.cursor)
            ticket.cursor = read.cursor
            ticket.worker = read.worker or ticket.worker
            for event_id, task_id, phase, note in read.progress:
                if event_id <= ticket.quiet_until:
                    continue
                detail = f"Worker task {task_id} {phase}" + (f": {note}" if note else ".")
                _report_progress(ticket.job, PHASE_DISPATCHED, detail)
            if read.closed:
                return HandBack(read.closed)
            if read.trigger is not None:
                ticket.trigger = read.trigger
                if read.trigger.kind == PR_ATTENTION:
                    worker_id = ticket.worker.task_id if ticket.worker else read.trigger.event_id
                    _report_progress(
                        ticket.job,
                        PHASE_DISPATCHED,
                        f"Worker task {worker_id}'s pull request needs attention: "
                        f"{_one_line(read.trigger.detail)}",
                    )
                if read.trigger.phase == PHASE_BLOCKED:
                    step = await self._answer(ticket)
                    if isinstance(step, HandBack):
                        return step
                    continue
                if read.trigger.kind in GATE_TRIGGERS and await self._back_to_gate(ticket):
                    continue
                trigger = ticket.trigger or read.trigger
                if not trigger.failure:
                    ticket.gate_steers = 0
                return trigger.phase
            if ticket.worker is None:
                # The ticket was dispatched, but its worker never made it into the
                # ledger (a resume from `dispatched` after the row was lost). Only a
                # new brief can put one there.
                return PHASE_BRIEFING
            if ticket.nudges:
                nudged = await self._nudged(ticket, ticket.nudges.pop(0))
                if nudged is not None:
                    return nudged
                continue
            if await self._hear(ticket):
                if await self._wait_on_person(ticket):
                    back = f"Watching worker task {ticket.worker.task_id} again."
                    await self._enter(ticket, PHASE_DISPATCHED, back, say=back)
                continue
            await self._sleep(ticket)

    async def _nudged(self, ticket: Ticket, nudge: Nudge) -> HandBack | str | None:
        """Act on what the rounds noticed: the turn it calls for, in this ticket's loop.

        Returns the phase to move to, a hand-back, or ``None`` to keep watching.
        """
        worker = ticket.worker
        if worker is None:
            return None
        if nudge.kind == "checkin":
            await self._checkin(ticket, nudge)
            return None
        if nudge.kind == "answer":
            ticket.trigger = Trigger(PHASE_BLOCKED, nudge.event_id, nudge.detail, kind="question")
            return await self._answer(ticket)
        if nudge.kind == "stopped":
            ticket.trigger = Trigger(
                PHASE_REVIEWING, nudge.event_id, nudge.detail, failure=True, kind=WORKER_STOPPED
            )
            if await self._back_to_gate(ticket):
                return None
            return PHASE_REVIEWING
        return None

    async def _checkin(self, ticket: Ticket, nudge: Nudge) -> None:
        """Run the check-in turn and do what its last line says, exactly once.

        The rounds chose the moment and the facts; the turn chose the words. The
        runner only reads the decision, passes the turn's message to the worker
        verbatim, and records what was decided and why the check ran. A turn that
        ends with no decision is recorded as that, and nothing reaches the worker.
        """
        assert ticket.worker is not None
        worker_id = ticket.worker.task_id
        facts = {
            **_ticket_facts(ticket.held),
            **_worker_facts(ticket.worker),
            "why the rounds are checking in": nudge.reason,
            **dict(nudge.facts),
        }
        result = await self._turn(ticket, prompts.CHECKIN, facts)
        decision = checkin_decision(result)
        choice, message = decision if decision is not None else ("none", "")
        error = ""
        if choice == prompts.CHECKIN_STEER:
            send = self._steer
        elif choice == prompts.CHECKIN_STOP:
            send = self._stop_and_resume
        else:
            send = None
        if send is not None:
            try:
                await asyncio.to_thread(send, worker_id, message)
            except Exception as exc:  # noqa: BLE001 - a refused steer is recorded, not fatal
                log.warning("[serve] Could not deliver the check-in to task %d: %s", worker_id, exc)
                error = str(exc)
        await asyncio.to_thread(
            record_checkin,
            ticket.held.task_id,
            worker_id=worker_id,
            trigger=nudge.trigger,
            reason=nudge.reason,
            decision=choice,
            message=message,
            error=error,
        )
        if choice == prompts.CHECKIN_STEER and not error:
            # Once is judgment; the same reason again on one ticket is the check-in
            # not being able to move the worker, which is the runtime's to look at.
            await asyncio.to_thread(
                self._deficiency,
                ticket,
                deficiencies.REPEATED_STEER,
                nudge.trigger or nudge.reason,
                scope=f"ticket:{ticket_key(ticket.held.event)}",
                trigger=nudge.trigger or nudge.reason,
            )
        if choice == prompts.CHECKIN_CONTINUE:
            return
        if choice == "none":
            said = "the check-in turn ended without a decision"
        elif error:
            said = f"the check-in turn chose to {choice}, and it could not be delivered: {error}"
        else:
            said = "steered" if choice == prompts.CHECKIN_STEER else "stopped and resumed"
        _report_progress(
            ticket.job,
            ticket.phase,
            f"Checked in on worker task {worker_id} ({nudge.reason}): {said}.",
        )

    async def _back_to_gate(self, ticket: Ticket) -> bool:
        """Decide on a stopped or done worker by its recorded gate. Returns whether it was steered.

        A long gate is the worker's to run, not the review turn's: a review turn
        that starts a suite the worker never finished outlasts itself (PAP-213).
        So the runner reads the gate result recorded at the worker's head
        (`ppy gate run`) and decides mechanically:

        - **green**: reviewable. A `worker_stopped` stops being a failure; the
          review turn gets the gate line as its fact.
        - **red**: steered with the gate's summary, whether it stopped or said done.
        - **none**: a `worker_stopped` is steered to run `ppy gate run`; a
          `worker_done` goes to review as before, where the re-check happens.

        After `GATE_STEERS` in a row, or a refused steer, the review turn gets it.
        """
        trigger, worker = ticket.trigger, ticket.worker
        if trigger is None or worker is None:
            return False
        worker_id = worker.task_id
        try:
            recorded = await asyncio.to_thread(self._gate_verdict, worker_id)
        except Exception as exc:  # noqa: BLE001 - an unreadable record decides nothing
            log.warning("[serve] Could not read worker task %d's gate: %s", worker_id, exc)
            recorded = gate.Verdict(gate.NONE)
        ticket.recorded_gate = recorded.result.line() if recorded.result is not None else ""
        if recorded.state == gate.GREEN and recorded.result is not None:
            if trigger.failure:
                line = recorded.result.line()
                ticket.trigger = Trigger(
                    PHASE_REVIEWING, trigger.event_id, trigger.detail, kind=trigger.kind
                )
                _report_progress(
                    ticket.job,
                    ticket.phase,
                    f"Worker task {worker_id} stopped, but its {line}; reviewing.",
                )
            return False
        if recorded.state == gate.NONE and trigger.kind != WORKER_STOPPED:
            return False
        if ticket.gate_steers >= GATE_STEERS:
            return False
        message = gate_steer_message(trigger.detail, worker_id, recorded.result)
        try:
            await asyncio.to_thread(self._steer, worker_id, message)
        except Exception as exc:  # noqa: BLE001 - a refused steer is the review turn's to handle
            log.warning(
                "[serve] Could not send worker task %d back to its gate: %s", worker_id, exc
            )
            _report_progress(
                ticket.job,
                ticket.phase,
                f"Could not send worker task {worker_id} back to its gate: {exc}; "
                "reviewing instead.",
            )
            return False
        ticket.gate_steers += 1
        ticket.trigger = None
        if recorded.result is not None:
            sent = (
                f"Worker task {worker_id}'s gate is red at its head; sent back with the summary: "
                f"{recorded.result.summary or recorded.result.command}"
            )
        else:
            sent = (
                f"Worker task {worker_id} stopped mid-gate; sent back to run its gate to "
                "completion with `ppy gate run`."
            )
        await self._enter(ticket, PHASE_DISPATCHED, sent)
        return True

    async def _back_to_commit(self, ticket: Ticket) -> bool:
        """Send a worker back when its worktree holds what its branch does not.

        The review reads the branch, never the worktree, so uncommitted work at review
        time is a finding, not something to review (PAP-219). The runner steers with the
        count and the paths, and the worker commits and pushes or discards. After
        `GATE_STEERS` in a row, or a refused steer, the review turn gets the finding as
        a fact and steers itself. Returns whether the worker was sent back.
        """
        worker = ticket.worker
        if worker is None:
            return False
        try:
            files = await asyncio.to_thread(self._uncommitted, worker.task_id) or []
        except Exception as exc:  # noqa: BLE001 - an unreadable worktree is not a finding
            log.warning("[serve] Could not read worker task %d's worktree: %s", worker.task_id, exc)
            files = []
        ticket.uncommitted = uncommitted_finding(files) if files else ""
        if not files:
            ticket.dirty_steers = 0
            return False
        if ticket.dirty_steers >= GATE_STEERS:
            return False
        message = uncommitted_steer_message(files, worker.branch)
        try:
            await asyncio.to_thread(self._steer, worker.task_id, message)
        except Exception as exc:  # noqa: BLE001 - a refused steer is the review turn's to handle
            log.warning("[serve] Could not steer worker task %d: %s", worker.task_id, exc)
            _report_progress(
                ticket.job,
                ticket.phase,
                f"Could not send worker task {worker.task_id} back for its "
                f"{ticket.uncommitted}: {exc}; reviewing instead.",
            )
            return False
        ticket.dirty_steers += 1
        ticket.trigger = None
        await self._enter(
            ticket,
            PHASE_DISPATCHED,
            f"Worker task {worker.task_id} has {ticket.uncommitted}; sent back to commit and "
            "push it or discard it.",
        )
        return True

    async def _answer(self, ticket: Ticket) -> HandBack | None:
        """Answer-or-steer: until the worker is no longer waiting on its question."""
        trigger = ticket.trigger
        assert trigger is not None and ticket.worker is not None
        worker_id = ticket.worker.task_id
        first_line = trigger.detail.strip().splitlines()[0] if trigger.detail.strip() else ""
        misses: list[str] = []
        tail = ""
        while True:
            await self._wait_on_person(ticket)
            asked = f"Worker task {worker_id} asked: {first_line or '(no text)'}"
            await self._enter(ticket, PHASE_BLOCKED, asked, say=f"Blocked: {asked}")
            # Whatever somebody said on the ticket meanwhile goes to the same turn.
            await self._listen(ticket)
            comments = await self._take_pending(ticket)
            mark = await asyncio.to_thread(_max_event_id)
            result = await self._turn(
                ticket, prompts.ANSWER, self._answer_facts(ticket, tail, comments)
            )
            acted = await asyncio.to_thread(acted_since, worker_id, mark)
            if not acted and await self._wait_on_person(ticket):
                continue
            # A worker no longer `blocked` was unblocked too, even with no event of
            # the turn's own to show for it (the supervisor may have auto-answered).
            if acted or await asyncio.to_thread(worker_status, worker_id) != "blocked":
                ticket.trigger = None
                unblocked = f"Worker task {worker_id} unblocked."
                await self._enter(ticket, PHASE_DISPATCHED, unblocked, say=unblocked)
                return None
            outcome = self._missed(ticket, misses, "answering or steering the worker", result)
            if isinstance(outcome, HandBack):
                return outcome
            tail = outcome

    async def _review(self, ticket: Ticket) -> HandBack | str:
        """Review-and-deliver, or steer: until the worker is delivered or sent back."""
        held = ticket.held
        if ticket.worker is None:
            ticket.worker = await asyncio.to_thread(find_worker, held)
        if ticket.worker is None:
            return PHASE_BRIEFING
        worker_id = ticket.worker.task_id
        misses: list[str] = []
        tail = ""
        waits = 0
        while True:
            await self._wait_on_person(ticket)
            failure = ticket.trigger is not None and ticket.trigger.failure
            # A delivered worker whose pull request needs attention still reads
            # `delivered`; only a new delivery event counts as delivering again.
            by_status = not (ticket.trigger is not None and ticket.trigger.kind == PR_ATTENTION)
            detail = (
                f"Worker task {worker_id} stopped short; reviewing what stopped it."
                if failure
                else f"Reviewing worker task {worker_id} at its head."
            )
            # Review internals are progress, not ticket comments: the person
            # reading the thread wants decisions and results.
            await self._enter(ticket, PHASE_REVIEWING, detail)
            heard = await asyncio.to_thread(_max_event_id)
            if await self._hear(ticket):
                # A comment turn that steered the worker reopened the work: there
                # is nothing at its head to review until it is done again.
                if await asyncio.to_thread(delivered_since, worker_id, heard, by_status):
                    ticket.trigger, ticket.reported = None, None
                    return PHASE_DELIVERING
                if await asyncio.to_thread(acted_since, worker_id, heard):
                    ticket.trigger = None
                    steered = f"Worker task {worker_id} steered on a comment."
                    await self._enter(ticket, PHASE_DISPATCHED, steered, say=steered)
                    return PHASE_DISPATCHED
                await self._wait_on_person(ticket)
                continue
            if await self._back_to_commit(ticket):
                return PHASE_DISPATCHED
            if not failure and await self._delivery_blocked(ticket):
                # The review turn would approve and then fail to open the pull
                # request with a gh error nobody sees. The branch is kept.
                return HandBack(blockers.DECLINE_REASON, setup=True)
            mark = await asyncio.to_thread(_max_event_id)
            # Taken before the turn and after the runner's own comment: nothing but
            # the turn writes on the item while it runs, so a new agent comment
            # after it is the turn's report.
            before = await asyncio.to_thread(self._comments, ticket)
            result = await self._turn(ticket, prompts.REVIEW, self._review_facts(ticket, tail))
            if await asyncio.to_thread(delivered_since, worker_id, mark, by_status):
                ticket.trigger = None
                ticket.reported = await self._check_reported(ticket, before)
                return PHASE_DELIVERING
            if await asyncio.to_thread(acted_since, worker_id, mark):
                ticket.trigger = None
                # The first send-back is news on the ticket; the rounds after it are not.
                first = not await asyncio.to_thread(sent_back_before, held.task_id)
                sent_back = f"Worker task {worker_id} {SENT_BACK_DETAIL}"
                await self._enter(ticket, PHASE_DISPATCHED, sent_back)
                if first:
                    await self._say(ticket, SAID_SENT_BACK, SENT_BACK_LINE)
                return PHASE_DISPATCHED
            if await self._wait_on_person(ticket):
                continue
            if (
                waited := await self._rerun_later(ticket, prompts.REVIEW, result, waits)
            ) is not None:
                waits, tail = waits + 1, waited
                continue
            outcome = self._missed(ticket, misses, "approving and delivering, or steering", result)
            if isinstance(outcome, HandBack):
                return outcome
            tail = outcome

    async def _delivery_blocked(self, ticket: Ticket) -> bool:
        """Does a blocker make a pull request for this ticket's repository impossible?"""
        repo = (ticket.worker.repo if ticket.worker else None) or ticket.held.repo
        if not repo:
            return False
        verdict = await asyncio.to_thread(self._check_readiness)
        return readiness.setup_blocker(verdict, repo, delivery=True) is not None

    async def _deliver(self, ticket: Ticket) -> str:
        """The pull request is open: say so, move the item to review, and finish."""
        held = ticket.held
        worker = ticket.worker or await asyncio.to_thread(find_worker, held)
        pr_url = await asyncio.to_thread(pull_request_url, worker.task_id) if worker else None
        opened = f"Pull request open: {pr_url}" if pr_url else "Delivered."
        # The ticket's last agent comment should be the turn's own report. Only
        # when the report could not be checked does the runner name the pull
        # request, and only when it is missing does it post the fallback.
        await self._enter(
            ticket, PHASE_DELIVERING, opened, say=opened if ticket.reported is None else ""
        )
        await self._status(ticket, papaya_events.STATUS_REVIEW)
        if ticket.reported is False:
            # The review turn's report was looked for and is not there, twice. Say
            # the one thing the runner knows for certain, and say that it did.
            where = pr_url or "the pull request on the worker's branch"
            fallback = f"Pull request open: {where}; see the pull request for details."
            await self._enter(ticket, PHASE_REPORTED, "reported (fallback)", say=fallback)
        elif ticket.reported:
            await self._enter(ticket, PHASE_REPORTED, "Result reported on this item.")
        else:
            await self._enter(
                ticket, PHASE_REPORTED, "reported (unverified: the work item could not be read)"
            )
        return PHASE_REPORTED

    def _comments(self, ticket: Ticket) -> list[dict[str, Any]] | None:
        """The item's comments now, or ``None`` when they cannot be read."""
        try:
            return papaya_events.list_work_item_comments(
                ticket.held.event, environ=ticket.job.env, **self._opener_kwargs()
            )
        except papaya_events.PapayaEventError as exc:
            log.warning("[serve] Could not read the comments on %s: %s", ticket.job.subject, exc)
            return None

    def _agent_commented_since(
        self, ticket: Ticket, before: list[dict[str, Any]] | None
    ) -> bool | None:
        if before is None:
            return None
        now = self._comments(ticket)
        if now is None:
            return None
        known = _comment_ids(before)
        return any(
            _is_agent_comment(comment) and str(comment.get("id")) not in known for comment in now
        )

    async def _check_reported(
        self, ticket: Ticket, before: list[dict[str, Any]] | None
    ) -> bool | None:
        """Did the review turn post its result on the work item? Checked, not assumed.

        On the first real run the review turn had no tools, posted nothing, and the
        runner said "Result posted on the work item" anyway. So: a new comment by
        the agent since the turn began, or one rerun told only to post it, or
        ``False`` — and then the runner posts the fallback itself.
        """
        posted = await asyncio.to_thread(self._agent_commented_since, ticket, before)
        if posted is not False:
            return posted
        _report_progress(
            ticket.job,
            PHASE_REVIEWING,
            "The review turn delivered but posted no result on the work item; "
            "running it once more.",
        )
        before = await asyncio.to_thread(self._comments, ticket)
        facts = {**self._review_facts(ticket, ""), prompts.ADDENDUM_FACT: prompts.REPORT_ADDENDUM}
        await self._turn(ticket, prompts.REVIEW, facts)
        return await asyncio.to_thread(self._agent_commented_since, ticket, before)

    # -- listening to the ticket while the work is in flight ------------------

    async def _hear(self, ticket: Ticket) -> bool:
        """Run the answer turn for what somebody said on the ticket. Returns whether it ran.

        The client skips a `work_item.comment` event for a subject this session
        already holds, so a person's reply to a question the manager asked would
        otherwise go unheard until review. The runner reads the comments itself,
        at most once a minute and right after every turn, and a comment that
        arrived while a turn ran is answered after it — all of them in one turn.
        What to do about a comment is the turn's judgment, not the runner's.
        """
        await self._listen(ticket)
        comments = await self._take_pending(ticket)
        if not comments:
            return False
        await self._turn(ticket, prompts.ANSWER, self._answer_facts(ticket, "", comments))
        return True

    async def _listen(self, ticket: Ticket) -> None:
        """Read the comments when it is time, and queue what somebody else said."""
        now = self._clock()
        read_at = ticket.comments_read_at
        if read_at is not None and now - read_at < self._comment_poll_seconds:
            return
        ticket.comments_read_at = now
        comments = await asyncio.to_thread(self._comments, ticket)
        if comments is None:
            return
        task_id = ticket.held.task_id
        handled = await asyncio.to_thread(last_handled_comment, task_id)
        newer = comments_after(comments, handled) if handled is not None else None
        if newer is None:
            # Nothing recorded yet (or nothing the record can be placed against):
            # what is on the item now is where listening starts, and wakes nothing.
            await asyncio.to_thread(record_comment_handled, task_id, _newest(comments))
            return
        agent_id = self._own_agent_id()
        queued = _comment_ids(ticket.pending)
        for comment in newer:
            if is_own_comment(comment, agent_id) or str(comment.get("id")) in queued:
                continue
            ticket.pending.append(comment)

    async def _take_pending(self, ticket: Ticket) -> list[dict[str, Any]]:
        """Hand the queued comments to a turn: one progress line each, then recorded."""
        comments, ticket.pending = ticket.pending, []
        if not comments:
            return []
        for comment in comments:
            _report_progress(
                ticket.job, ticket.phase, f"Answering a comment from {comment_author(comment)}"
            )
        await asyncio.to_thread(record_comment_handled, ticket.held.task_id, comments[-1])
        return comments

    async def _mark_read(self, ticket: Ticket) -> None:
        """A turn is about to read the item: what is on it now counts as handled.

        Every turn's prompt reads the work item and its comments, so a comment
        that is there when one starts has been heard — a person's reply that a
        brief turn acted on is not answered again once the worker is dispatched.
        Taken before the launch, so a comment made while the turn runs is newer.
        """
        comments = await asyncio.to_thread(self._comments, ticket)
        if comments is None:
            return
        await asyncio.to_thread(record_comment_handled, ticket.held.task_id, _newest(comments))
        ticket.pending.clear()

    def _own_agent_id(self) -> str | None:
        if self._agent_id is None:
            who = papaya.identity()
            self._agent_id = who.agent_id if who is not None else ""
        return self._agent_id or None

    # -- waiting, without ever blocking the loop ------------------------------

    async def _wait_on_person(self, ticket: Ticket) -> bool:
        """Hold in `blocked` while a turn's question waits on a person's reply.

        A turn that needs a person says so by recording a todo blocked on `user`
        against the ticket task — an existing, local primitive, so the runner can
        read it without interpreting the transcript. The client drops a new event
        for a subject this session already holds, so the reply cannot arrive as a
        `work_item.comment`; the runner watches the work item itself instead, and
        resumes when it changes, when a comment by somebody else is read (it is
        then queued for the next turn), or when the todo is closed here.

        The activity stamp is touched while waiting: the ticket is knowingly
        parked on a person, in a phase the app shows, which is not a stall.
        """
        held = ticket.held
        wait = await asyncio.to_thread(open_person_wait, held.task_id)
        if wait is None:
            return False
        todo_id, question = wait
        waiting = f"Waiting on a person: {question}"
        await self._enter(ticket, PHASE_BLOCKED, waiting, say=f"Blocked: {waiting}")
        await self._status(ticket, papaya_events.STATUS_BLOCKED)
        before = await asyncio.to_thread(self._fingerprint, ticket)
        while True:
            await self._sleep(ticket)
            ticket.job.touch_activity()
            if await asyncio.to_thread(open_person_wait, held.task_id) is None:
                break
            now = await asyncio.to_thread(self._fingerprint, ticket)
            if now is not None and now != before:
                break
            await self._listen(ticket)
            if ticket.pending:
                break
        await asyncio.to_thread(close_person_wait, todo_id)
        await self._status(ticket, papaya_events.STATUS_IN_PROGRESS)
        _report_progress(ticket.job, PHASE_BLOCKED, "A reply arrived; picking the ticket back up.")
        return True

    async def _wait_for_slot(self, ticket: Ticket) -> bool:
        """Hold in `dispatched` while the worker pool is full. Never a hand-back.

        Worker capacity is refused before a task row exists, so a full pool looks
        like a brief turn that dispatched nothing. Reading the pool is what tells
        the two apart; a full pool is not the turn's miss and not a reason to
        give the ticket away.
        """
        capacity = await asyncio.to_thread(self._worker_capacity)
        if capacity is None or capacity[0] < capacity[1]:
            return False
        busy, limit = capacity
        await self._enter(
            ticket,
            PHASE_DISPATCHED,
            f"Waiting for a worker slot: {busy} of {limit} workers are busy.",
        )
        while True:
            await self._sleep(ticket)
            ticket.job.touch_activity()
            capacity = await asyncio.to_thread(self._worker_capacity)
            if capacity is None or capacity[0] < capacity[1]:
                return True

    async def _sleep(self, ticket: Ticket) -> None:
        """One poll interval, cut short by a stop."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(ticket.job.stop.wait(), timeout=self._poll_seconds)
        self._check_stop(ticket)

    @staticmethod
    def _check_stop(ticket: Ticket) -> None:
        if ticket.job.stop.is_set():
            raise _Stopped

    # -- turns ----------------------------------------------------------------

    def _missed(
        self, ticket: Ticket, misses: list[str], job_of_turn: str, result: Any
    ) -> HandBack | str:
        """A turn ended without doing its job: report it, and retry or give up.

        Returns the tail to hand the retry, or the hand-back after the last miss.
        """
        misses.append(job_of_turn)
        attempt = len(misses)
        detail = f"The turn ended without {job_of_turn} (attempt {attempt} of {TURN_ATTEMPTS})."
        _report_progress(ticket.job, ticket.phase, detail)
        if attempt >= TURN_ATTEMPTS:
            return HandBack(
                f"the manager turn ended {attempt} times without {job_of_turn}",
                missed=job_of_turn,
            )
        return result.tail() if hasattr(result, "tail") else str(result or "")

    async def _rerun_later(self, ticket: Ticket, turn: str, result: Any, waits: int) -> str | None:
        """A turn that ended `WAITING:` is waited out, not missed. Returns the tail, or ``None``.

        The reason is a progress line; the phase does not change. The wait is on
        the runner's clock — `WAIT_FIRST_SECONDS`, doubling for each wait in a row
        up to `WAIT_MAX_SECONDS` — touching the activity stamp as it goes, and is
        cut short by a stop or by a comment from somebody else, which the caller's
        loop then hears before the turn runs again.
        """
        reason = waiting_reason(result)
        if reason is None:
            return None
        gate_budget = await asyncio.to_thread(known_gate_budget, turn_repo(ticket))
        delay = rerun_delay(waits + 1, gate_budget)
        _report_progress(
            ticket.job,
            ticket.phase,
            f"The {turn} turn is waiting: {reason}; "
            f"running it again in {round(delay / 60)} minutes.",
        )
        deadline = self._clock() + delay
        while self._clock() < deadline:
            await self._sleep(ticket)
            ticket.job.touch_activity()
            await self._listen(ticket)
            if ticket.pending:
                break
        return result.tail() if hasattr(result, "tail") else str(result or "")

    async def _turn(self, ticket: Ticket, turn: str, facts: dict[str, object]) -> Any:
        """Launch one manager turn for this ticket and wait for it, on a thread."""
        from papaya_agent_runtime.manager.launch import (
            ManagerLaunchError,
            TurnResult,
            build_launch,
            prepare_turn_tools,
            resolve_profile,
            run_turn,
        )

        root = self._root()
        prompt = prompts.render(turn, runtime_dir=root, facts=facts)
        env = {
            **os.environ,
            **turn_environment(ticket.job.env, root=root, run_id=ticket.held.run_id),
        }
        transcript = turn_transcript_path(ticket.held.run_id, turn)
        ticket.last_transcript = str(transcript)
        # Named before the turn starts, so a turn still running can be watched.
        _report_progress(ticket.job, ticket.phase, f"The {turn} turn's transcript: {transcript}")
        try:
            config = self._config() if self._config is not None else _load_config()
            provider, _model, _reasoning = resolve_profile(config, None, None, None)
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
            log.error("[serve] Could not launch the %s turn: %s", turn, exc)
            text = f"the turn could not be launched: {exc}"
            with contextlib.suppress(OSError):
                transcript.parent.mkdir(parents=True, exist_ok=True)
                transcript.write_text(text + "\n", encoding="utf-8")
            return TurnResult(exit_code=127, transcript=text)
        runner = self._run_turn or run_turn
        await self._mark_read(ticket)
        ticket.turn_running = turn
        started = self._clock()
        try:
            result = await asyncio.to_thread(
                runner, launch, should_stop=ticket.should_stop, transcript_path=transcript
            )
        finally:
            ticket.turn_running = None
            # Whatever was said while the turn ran is read as soon as it ends.
            ticket.comments_read_at = None
        await self._observe_turn(ticket, turn, self._clock() - started, result)
        said = runtime_report(result)
        if said is not None:
            _report_progress(
                ticket.job, ticket.phase, f"The {turn} turn reported a runtime problem."
            )
            await asyncio.to_thread(
                self._deficiency, ticket, deficiencies.TURN_REPORT, said, turn=turn
            )
        self._check_stop(ticket)
        return result

    async def _observe_turn(self, ticket: Ticket, turn: str, seconds: float, result: Any) -> None:
        """Keep how long a brief or review turn took, against its repository."""
        from papaya_agent_runtime import budgets

        kind = {prompts.BRIEF: budgets.BRIEF_TURN, prompts.REVIEW: budgets.REVIEW_TURN}.get(turn)
        if kind is None:
            return
        if ticket.should_stop():
            outcome = budgets.KILL
        elif waiting_reason(result) is not None:
            outcome = "waiting"
        else:
            outcome = "ok" if getattr(result, "exit_code", 0) == 0 else "fail"
        await asyncio.to_thread(
            budgets.observe,
            turn_repo(ticket),
            kind,
            seconds,
            task_id=ticket.held.task_id,
            outcome=outcome,
        )

    def _root(self) -> str:
        from papaya_agent_runtime.manager.launch import repo_root

        return str(Path(self._runtime_dir or repo_root()).resolve())

    def _brief_facts(self, ticket: Ticket, tail: str) -> dict[str, object]:
        held = ticket.held
        earlier = earlier_worker_for(str(held.event.work_item_id or ""))
        return {
            **_ticket_facts(held),
            "repository named by the item": held.repo,
            # A ticket taken up again after an earlier worker built on a branch
            # (a hand-back re-offered, PAP-213): start from that branch rather
            # than redoing the work.
            "an earlier worker's branch for this ticket (build on it; do not redo its work)": (
                f"{earlier.branch} (worker task {earlier.task_id}, {earlier.status})"
                if earlier is not None
                else None
            ),
            "previous attempt's transcript (tail)": tail,
        }

    def _answer_facts(
        self, ticket: Ticket, tail: str, comments: list[dict[str, Any]] | None = None
    ) -> dict[str, object]:
        worker = ticket.worker
        trigger = ticket.trigger
        return {
            **_ticket_facts(ticket.held),
            **_worker_facts(worker),
            "the worker's question": (
                trigger.detail if trigger is not None and trigger.phase == PHASE_BLOCKED else ""
            ),
            "new comments on the work item, by someone other than you": _comments_fact(
                comments or []
            ),
            "previous attempt's transcript (tail)": tail,
        }

    def _review_facts(self, ticket: Ticket, tail: str) -> dict[str, object]:
        trigger = ticket.trigger
        return {
            **_ticket_facts(ticket.held),
            **_worker_facts(ticket.worker),
            "what stopped the worker": trigger.detail if trigger and trigger.failure else "",
            "the worker's recorded gate at its head": ticket.recorded_gate,
            "finding: uncommitted work": ticket.uncommitted,
            "previous attempt's transcript (tail)": tail,
        }

    # -- Papaya, mechanically ------------------------------------------------

    async def _status(self, ticket: Ticket, status: str) -> None:
        """Set the work item's status; a refusal is progress, never fatal."""
        try:
            await asyncio.to_thread(
                papaya_events.set_work_item_status,
                ticket.held.event,
                status,
                environ=ticket.job.env,
                **self._opener_kwargs(),
            )
        except papaya_events.PapayaEventError as exc:
            log.warning("[serve] Could not set %s to %s: %s", ticket.job.subject, status, exc)
            _report_progress(ticket.job, ticket.phase, f"Could not set the item to {status}: {exc}")

    def _fingerprint(self, ticket: Ticket) -> str | None:
        """What the work item looks like now, for noticing that a person replied."""
        try:
            fresh = papaya_events.hydrate_work_item(
                ticket.held.event, environ=ticket.job.env, **self._opener_kwargs()
            )
        except papaya_events.PapayaEventError:
            return None
        return work_item_fingerprint(fresh.payload.get("work_item"))

    def _opener_kwargs(self) -> dict[str, Any]:
        return {"opener": self._opener} if self._opener is not None else {}

    async def _enter(self, ticket: Ticket, phase: str, detail: str, *, say: str = "") -> None:
        """Record a phase on the task and say it as progress, which is activity.

        With ``say``, the phase change is also one comment on the work item — the
        line the ticket card shows as this agent's status.
        """
        await asyncio.to_thread(self._record_phase, ticket.held.task_id, phase, detail)
        ticket.phase = phase
        _report_progress(ticket.job, phase, detail)
        if say:
            await self._say(ticket, phase, say)

    async def _say(self, ticket: Ticket, phase: str, text: str) -> None:
        """One comment, as the agent, when the phase differs from the last one said.

        Only phase changes reach the ticket: a retried turn, a slot wait or a
        worker's progress stays in the app and the log. ``phase`` is what repeats
        are told apart by, so a line that is not a phase (`SAID_SENT_BACK`) passes
        its own key. Never fatal.
        """
        line = _one_line(text)
        if not line or phase == ticket.said:
            return
        ticket.said = phase
        try:
            await asyncio.to_thread(
                papaya_events.post_work_item_comment,
                ticket.held.event,
                line,
                environ=ticket.job.env,
                **self._opener_kwargs(),
            )
        except papaya_events.PapayaEventError as exc:
            log.warning("[serve] Could not comment on %s: %s", ticket.job.subject, exc)

    # -- how a hold ends -------------------------------------------------------

    async def _hand_back(
        self, ticket: Ticket, reason: str, *, setup: bool = False
    ) -> dict[str, Any]:
        """Give the ticket back: declined, status `todo`, one comment, branch kept.

        ``setup``: handed back because this machine needs its owner, so the comment
        is the neutral line and names nothing.
        """
        held, job = ticket.held, ticket.job
        log.info("[serve] Handing back %s: %s", job.subject, reason)
        await asyncio.to_thread(self._record_phase, held.task_id, PHASE_DECLINED, reason)
        job.decline(reason)
        await self._status(ticket, papaya_events.STATUS_TODO)
        if not setup:
            await self._comment(ticket, reason)
        elif held.event.work_item_id:
            verdict = await asyncio.to_thread(self._check_readiness)
            if await asyncio.to_thread(blockers.comment_on, held.event.work_item_id, verdict):
                try:
                    await asyncio.to_thread(
                        papaya_events.post_work_item_comment,
                        held.event,
                        blockers.TICKET_COMMENT,
                        environ=job.env,
                        **self._opener_kwargs(),
                    )
                except papaya_events.PapayaEventError as exc:
                    log.warning("[serve] Could not comment on %s: %s", job.subject, exc)
        # Remembered for the sweep like a first-pickup decline: the task row now reads
        # `declined`, which the sweep treats as ended, so without this the brief turns
        # would run again every sweep. The status and the comment just written move
        # the item's `updated_at` themselves, so the stamp is taken after them — only
        # a change somebody makes later reads as newer. Never earlier than the item's
        # own `updated_at`, so a clock behind Papaya's cannot make it read as changed.
        if held.event.work_item_id:
            work_item = held.event.payload.get("work_item")
            known = work_item.get("updated_at") if isinstance(work_item, dict) else None
            stamp = datetime.now(UTC).isoformat()
            if known and sweep.declined_earlier({"updated_at": stamp}, {"updated_at": known}):
                stamp = str(known)
            try:
                await asyncio.to_thread(
                    sweep.remember_declined,
                    held.event.work_item_id,
                    updated_at=stamp,
                    reason=reason,
                )
            except Exception as exc:  # noqa: BLE001 - remembering must not stop the hand-back
                log.warning("[serve] Could not remember handing back %s: %s", job.subject, exc)
        return _result(job, _declined_exit_code(), reason)

    async def _stopped(self, ticket: Ticket) -> dict[str, Any]:
        """The client ended the hold. Record why; a stall is a hand-back of ours.

        Unless the ticket's worker's work goes on — its session is live, it said done,
        or it stopped with its branch ahead of base: then only the hold stalled. The
        worker is left as it is under the supervisor, nothing is said on the item, and
        the phase `stalled` is what the rounds' reclaim reads to offer the ticket again
        and resume it from there (:func:`stalled_resume_phase`).
        """
        held, job = ticket.held, ticket.job
        phase = phase_for_stop(job.stop.reason)
        if phase == PHASE_STALLED:
            await asyncio.to_thread(self._record_phase, held.task_id, phase)
            # A stall while the worker is demonstrably working is the runtime's problem,
            # not the ticket's: the stall check read activity the work never touched.
            worker = ticket.worker or await asyncio.to_thread(find_worker, held)
            if worker is not None and await asyncio.to_thread(worker_session_live, worker.task_id):
                ticket.worker = worker
                await asyncio.to_thread(
                    self._deficiency,
                    ticket,
                    deficiencies.STALL_WHILE_LIVE,
                    "the worker session was live when the hold stalled",
                )
            resume = await asyncio.to_thread(_stalled_resume, held.task_id)
            if resume is not None:
                log.info(
                    "[serve] Released %s for task %d (stalled); its worker's work goes on, "
                    "and the rounds resume it from %s",
                    job.subject,
                    held.task_id,
                    resume,
                )
                return _result(job, 0, f"task {held.task_id} {phase}")
        else:
            await asyncio.to_thread(self._record_phase, held.task_id, phase)
        if phase == PHASE_STALLED:
            # The client hands a stalled subject back itself; the item still says
            # `in_progress` and nobody has said why unless the runner does.
            await self._status(ticket, papaya_events.STATUS_TODO)
            await self._comment(ticket, "stalled: nothing happened on it for too long")
        log.info("[serve] Released %s for task %d (%s)", job.subject, held.task_id, phase)
        return _result(job, 0, f"task {held.task_id} {phase}")

    async def _comment(self, ticket: Ticket, reason: str) -> None:
        worker = ticket.worker or await asyncio.to_thread(find_worker, ticket.held)
        kept = (
            f"branch {worker.branch} kept" if worker is not None and worker.branch else "no branch"
        )
        body = f"handed back: {reason}; {kept}"
        try:
            await asyncio.to_thread(
                papaya_events.post_work_item_comment,
                ticket.held.event,
                body,
                environ=ticket.job.env,
                **self._opener_kwargs(),
            )
        except papaya_events.PapayaEventError as exc:
            log.warning("[serve] Could not comment on %s: %s", ticket.job.subject, exc)

    # -- the blocking half, run on a thread --------------------------------

    def take(self, job: Any) -> Held | Declined:
        """Record this job's ticket, or say why this machine is not the one."""
        try:
            event = papaya_events.parse_event(job.event_file, environ=job.env)
        except papaya_events.PapayaEventError as exc:
            return Declined(f"this event could not be read: {exc}")
        if not event.work_item_id:
            # The manager's unit of work is a work item: it is what a repository,
            # a brief and a pull request all hang off. An event carrying none has
            # nothing for this runtime to place, whatever its kind.
            kind = event.kind or "this"
            return Declined(f"this runtime takes work items, and a {kind} event carries none")

        verdict = self._check_readiness()
        if verdict.state == readiness.BLOCKED:
            if readiness.setup_blocker(verdict) is not None:
                return self._decline(event, blockers.DECLINE_REASON, setup=verdict)
            return self._decline(event, readiness.headline(verdict))
        try:
            event = papaya_events.hydrate_work_item(event, environ=job.env, **self._opener_kwargs())
        except papaya_events.PapayaEventError as exc:
            # Best effort, and deliberately not a decline of its own. The envelope
            # is thinner than the full record — no description, no repository
            # metadata — and the brief turn reads the item through MCP anyway;
            # declining here would give work away over one network error.
            log.warning("[serve] Could not read the full work item for %s: %s", job.subject, exc)

        # The first way of placing a ticket, and the only mechanical one: the item
        # names its repository. That is registered now, and a name that cannot be
        # (not in any account this person belongs to) is a decline, so a peer who
        # can take it may. An item that names nothing is *not* declined: choosing a
        # repository is the brief turn's judgment, with five more ways to answer.
        ensured = None
        try:
            papaya_events.repository_spec(event)
        except papaya_events.PapayaEventError:
            pass
        else:
            try:
                ensured = papaya_events.ensure_repository(event)
            except papaya_events.PapayaEventError as exc:
                return self._decline(event, str(exc))
        # A blocker scoped to this repository — its forge signed out, a toolchain it
        # needs missing — refuses it here, while work elsewhere goes on.
        if ensured is not None and readiness.setup_blocker(verdict, ensured.name) is not None:
            return self._decline(event, blockers.DECLINE_REASON, setup=verdict)

        conn = db.init_db()
        try:
            return self._record(conn, event, ensured.name if ensured is not None else None)
        finally:
            conn.close()

    def _setup_comment(
        self,
        event: papaya_events.PapayaEvent,
        verdict: readiness.Readiness,
        environ: dict[str, str],
    ) -> None:
        """The one neutral comment a ticket refused for a blocker gets, and the sweep's memory.

        Once per ticket per blocker, so a re-offer while the same blocker stands is
        silent. The decline is remembered with a stamp taken *after* the comment,
        which itself moves the item's `updated_at`; otherwise the sweep would read
        its own comment as a change and offer the ticket straight back.
        """
        if not event.work_item_id:
            return
        if blockers.comment_on(event.work_item_id, verdict):
            try:
                papaya_events.post_work_item_comment(
                    event, blockers.TICKET_COMMENT, environ=environ, **self._opener_kwargs()
                )
            except papaya_events.PapayaEventError as exc:
                log.warning("[serve] Could not comment on %s: %s", event.subject, exc)
        with contextlib.suppress(Exception):
            sweep.remember_declined(
                event.work_item_id,
                updated_at=datetime.now(UTC).isoformat(),
                reason=blockers.DECLINE_REASON,
            )

    @staticmethod
    def _decline(
        event: papaya_events.PapayaEvent,
        reason: str,
        *,
        setup: readiness.Readiness | None = None,
    ) -> Declined:
        """Decline, and say so on the task if this ticket already has one.

        A first delivery has no task and leaves none: declining is the opposite of
        taking work on, so it must not be the thing that creates a row. A *re*
        delivery of a ticket this manager took earlier is the case worth
        recording — the hold ended and the next attempt was refused, and a task
        still reading `picked_up` would be describing a lease nobody holds.

        Either way the decline is remembered for the sweep, with the ticket's
        `updated_at` as it stood: a ticket with no task row would otherwise be
        offered, asked about and declined again every sweep until somebody changed it.
        """
        from papaya_agent_runtime.paths import db_path

        if event.work_item_id:
            work_item = event.payload.get("work_item")
            updated_at = work_item.get("updated_at") if isinstance(work_item, dict) else None
            try:
                sweep.remember_declined(
                    event.work_item_id,
                    updated_at=str(updated_at) if updated_at else None,
                    reason=reason,
                )
            except Exception as exc:  # noqa: BLE001 - remembering must not stop the decline
                log.warning("[serve] Could not remember declining %s: %s", event.subject, exc)

        if db_path().exists():
            with contextlib.suppress(Exception):
                conn = db.init_db()
                try:
                    existing = papaya_events.find_existing_task(
                        conn, papaya_events.event_key(event)
                    )
                    if existing is not None:
                        record_phase(conn, int(existing["id"]), PHASE_DECLINED, reason)
                finally:
                    conn.close()
        if setup is None:
            return Declined(reason)
        return Declined(reason, setup=True, event=event, verdict=setup)

    def _record(self, conn, event: papaya_events.PapayaEvent, repo_name: str | None) -> Held:
        """The task row for this event, found or created, and where to take it up.

        Found *or* created: the event key is what makes a redelivered event
        harmless, and a second pick-up of the same ticket has to land on the task
        the first one made rather than fork a new one beside it. A found task that
        was mid-work resumes from its working phase instead of being picked up
        again; anything else starts over at `picked_up`.
        """
        existing = papaya_events.find_existing_task(conn, papaya_events.event_key(event))
        reclaimed = False
        reported_until: int | None = None
        if existing is None and event.work_item_id:
            # An offer carries a new event key every time, so a ticket this runtime
            # was already working — re-offered by the rounds' reclaim, or by the
            # sweep after its hold ended — is found by its work item instead.
            wanted = self._reclaiming.pop(str(event.work_item_id), None)
            if wanted is not None:
                wanted_task, reported_until = wanted
                existing = store.get_task(conn, wanted_task)
            else:
                existing = resumable_task_for(conn, str(event.work_item_id))
            reclaimed = existing is not None
        if existing is not None:
            task_id, run_id = int(existing["id"]), int(existing["run_id"])
            resume_from = resumable_phase(conn, task_id)
        else:
            title = _title(event)
            repo = store.get_repo(conn, repo_name) if repo_name else None
            run_id = store.create_run(conn, title)
            task_id = store.add_task(
                conn,
                run_id=run_id,
                title=title,
                repo_id=int(repo["id"]) if repo is not None else None,
            )
            papaya_events.record_task(conn, task_id, event)
            resume_from = None
        if resume_from is None:
            record_phase(conn, task_id, PHASE_PICKED_UP)
        if event.work_item_id:
            # Taken now, so an earlier decline no longer describes this ticket. Left
            # in place it would keep the sweep away after this hold is released.
            with contextlib.suppress(Exception):
                sweep.forget_declined(event.work_item_id)
        return Held(
            task_id=task_id,
            run_id=run_id,
            repo=repo_name,
            event=event,
            resume_from=resume_from,
            reclaimed=reclaimed,
            reported_until=reported_until,
        )

    @staticmethod
    def _record_phase(task_id: int, phase: str, detail: str = "") -> None:
        conn = db.init_db()
        try:
            record_phase(conn, task_id, phase, detail)
        finally:
            conn.close()


def _declined_exit_code() -> int:
    """75, read from the client rather than restated, so the two cannot drift."""
    from papaya_agent_client.command_runner import DECLINED_EXIT_CODE

    return int(DECLINED_EXIT_CODE)


# ── reading the ledger, mechanically ────────────────────────────────────────


def record_phase(conn, task_id: int, phase: str, detail: str = "") -> None:
    """Write a phase on the task row *and* as an event.

    The column only holds the latest phase, which is what a reader of the task
    wants. The event is the history: the order a ticket went through its phases,
    and the working phase to resume from after a shutdown wrote `released` over it.
    """
    store.set_task_phase(conn, task_id, phase)
    task = store.get_task(conn, task_id)
    store.append_event(
        conn,
        kind=store.TICKET_PHASE_EVENT,
        payload={"task_id": task_id, "phase": phase, "detail": detail},
        run_id=int(task["run_id"]) if task is not None else None,
        task_id=task_id,
    )


def phase_history(conn, task_id: int) -> list[str]:
    """Every phase this ticket's task has been through, oldest first."""
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, store.TICKET_PHASE_EVENT),
    ).fetchall()
    return [str(_payload(row).get("phase") or "") for row in rows]


def sent_back_before(task_id: int) -> bool:
    """Has a review already sent this ticket's worker back? Read from the phase history,
    so a resumed ticket does not tell the thread a second time."""
    conn = db.init_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM events WHERE task_id = ? AND kind = ? "
            "AND json_extract(payload, '$.phase') = ? "
            "AND json_extract(payload, '$.detail') LIKE ? LIMIT 1",
            (task_id, store.TICKET_PHASE_EVENT, PHASE_DISPATCHED, f"% {SENT_BACK_DETAIL}"),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def resumable_phase(conn, task_id: int) -> str | None:
    """The working phase a redelivered ticket should pick up from, if any.

    A ticket mid-work resumes. So does one whose hold ended as `released` — a lost
    lease or this process shutting down — from the working phase before it, since
    nobody gave that work away. So does one whose hold `stalled` while its worker's
    work went on (:func:`stalled_resume_phase`): the client gave the lease back, not
    the work, so it resumes from the phase the worker's state implies. A ticket that
    was handed back, stalled with nothing left of its worker, declined, or finished
    starts over.
    """
    phase = store.task_phase(conn, task_id)
    if phase in WORKING_PHASES:
        return phase
    if phase == PHASE_STALLED:
        return stalled_resume_phase(conn, task_id)
    if phase != PHASE_RELEASED:
        return None
    for earlier in reversed(phase_history(conn, task_id)):
        if earlier in (PHASE_RELEASED, PHASE_STALLED):
            continue
        return earlier if earlier in WORKING_PHASES else None
    return None


def stalled_resume_phase(conn, ticket_task_id: int) -> str | None:
    """Where a ticket whose hold stalled picks up again, by its worker's state, or ``None``.

    The client's stall is about the lease, and a worker under the supervisor does not
    stop with it (PAP-219). So a worker whose session is still live is watched again
    (`dispatched`); one that said done is reviewed (`reviewing`); one that stopped with
    its branch ahead of base is watched too, where its `worker_stopped` takes the gate
    steer. None of those is a new brief or a new dispatch. Anything else starts over.
    """
    task = store.get_task(conn, ticket_task_id)
    if task is None:
        return None
    row = conn.execute(
        "SELECT id, status FROM tasks WHERE run_id = ? AND id != ? ORDER BY id DESC LIMIT 1",
        (task["run_id"], ticket_task_id),
    ).fetchone()
    if row is None:
        return None
    from papaya_agent_runtime import health, rounds

    worker_id, status = int(row["id"]), str(row["status"])
    if health.session_alive(conn, worker_id):
        return PHASE_DISPATCHED
    if status == "worker_done":
        return PHASE_REVIEWING
    if status == WORKER_STOPPED and rounds.branch_ahead_of_base(worker_id):
        return PHASE_DISPATCHED
    return None


def resumable_task_for(conn, work_item_id: str):
    """The newest task for this work item that a new offer should resume, or ``None``."""
    rows = conn.execute(
        "SELECT tasks.* FROM tasks JOIN task_env ON task_env.task_id = tasks.id "
        "WHERE task_env.key = ? AND json_valid(task_env.value) "
        "AND json_extract(task_env.value, '$.work_item_id') = ? ORDER BY tasks.id DESC",
        (papaya_events.PAPAYA_EVENT_METADATA, work_item_id),
    ).fetchall()
    for row in rows:
        if resumable_phase(conn, int(row["id"])) is not None:
            return row
        # Only the newest task speaks for the ticket: an older one was superseded.
        return None
    return None


def earlier_worker_for(work_item_id: str) -> Worker | None:
    """The newest worker, in any run this ticket ever had, that left a branch behind."""
    if not work_item_id:
        return None
    conn = db.init_db()
    try:
        row = conn.execute(
            "SELECT w.id, w.status, w.branch, r.name AS repo FROM tasks w "
            "JOIN tasks t ON t.run_id = w.run_id AND t.id != w.id "
            "JOIN task_env e ON e.task_id = t.id "
            "LEFT JOIN repos r ON r.id = w.repo_id "
            "WHERE e.key = ? AND json_valid(e.value) "
            "AND json_extract(e.value, '$.work_item_id') = ? "
            "AND w.phase IS NULL AND w.branch IS NOT NULL AND w.branch != '' "
            "ORDER BY w.id DESC LIMIT 1",
            (papaya_events.PAPAYA_EVENT_METADATA, work_item_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return Worker(
        task_id=int(row["id"]), status=str(row["status"]), repo=row["repo"], branch=row["branch"]
    )


def record_checkin(
    task_id: int,
    *,
    worker_id: int,
    trigger: str,
    reason: str,
    decision: str,
    message: str = "",
    error: str = "",
) -> None:
    """Record a check-in on the ticket's own task: why it ran, what the turn decided."""
    conn = db.init_db()
    try:
        task = store.get_task(conn, task_id)
        store.append_event(
            conn,
            kind=CHECKIN_EVENT,
            payload={
                "task_id": task_id,
                "worker_task_id": worker_id,
                "trigger": trigger,
                "reason": reason,
                "decision": decision,
                "message": message,
                "error": error,
            },
            run_id=int(task["run_id"]) if task is not None else None,
            task_id=task_id,
        )
    finally:
        conn.close()


def _payload(row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _max_event_id() -> int:
    conn = db.init_db()
    try:
        return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
    finally:
        conn.close()


def _worker_row(conn, held: Held) -> Worker | None:
    row = conn.execute(
        "SELECT t.id, t.status, t.branch, r.name AS repo FROM tasks t "
        "LEFT JOIN repos r ON r.id = t.repo_id "
        "WHERE t.run_id = ? AND t.id != ? ORDER BY t.id DESC LIMIT 1",
        (held.run_id, held.task_id),
    ).fetchone()
    if row is None:
        return None
    return Worker(
        task_id=int(row["id"]), status=str(row["status"]), repo=row["repo"], branch=row["branch"]
    )


def find_worker(held: Held) -> Worker | None:
    """The newest task in the ticket's run other than the ticket's own: its worker."""
    conn = db.init_db()
    try:
        return _worker_row(conn, held)
    finally:
        conn.close()


def worker_status(task_id: int) -> str | None:
    conn = db.init_db()
    try:
        task = store.get_task(conn, task_id)
        return str(task["status"]) if task is not None else None
    finally:
        conn.close()


def worker_session_live(task_id: int) -> bool:
    """Is this worker's session process still running?"""
    from papaya_agent_runtime import health

    conn = db.init_db()
    try:
        return health.session_alive(conn, task_id)
    finally:
        conn.close()


def _stalled_resume(ticket_task_id: int) -> str | None:
    conn = db.init_db()
    try:
        return stalled_resume_phase(conn, ticket_task_id)
    finally:
        conn.close()


def default_gate_state(worker_task_id: int) -> Any:
    """`rounds.gate_state`: the worker's gate as the supervisor has it now."""
    from papaya_agent_runtime import rounds

    return rounds.gate_state(worker_task_id)


# ── liveness lines ──────────────────────────────────────────────────────────

_ORDINALS = {2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth"}


def _ordinal(n: int) -> str:
    if n in _ORDINALS:
        return _ORDINALS[n]
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _duration(seconds: float) -> str:
    return f"{int(seconds)} s" if seconds < 90 else f"{int(seconds // 60)} min"


def _clipped(text: str, limit: int = 100) -> str:
    line = _one_line(text)
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


@dataclass(frozen=True)
class WorkerActivity:
    """What a worker's session recorded since the last liveness line."""

    #: The newest worker event read.
    last_event_id: int
    #: How many worker events there were.
    events: int
    #: The tool running most recently, and what it runs (a shell command, say).
    tool: str = ""
    command: str = ""
    #: How long that tool has been running, when a heartbeat said.
    elapsed_seconds: float | None = None
    #: How many times this worker has run that same command.
    runs: int = 0
    #: The worker's latest words, when it said something and ran nothing.
    said: str = ""

    def line(self, worker_id: int) -> str:
        head = f"Worker task {worker_id} active: "
        if self.command or self.tool:
            parts = [f"`{_clipped(self.command)}`" if self.command else self.tool]
            if self.elapsed_seconds is not None:
                parts.append(f"{_duration(self.elapsed_seconds)} in")
            if self.runs >= 2:
                parts.append(f"{_ordinal(self.runs)} run")
            return head + ", ".join(parts)
        if self.said:
            return head + f'said "{_clipped(self.said)}"'
        return head + f"{self.events} event{'s' if self.events != 1 else ''} since the last line"


def _tool_uses(payload: dict[str, Any]) -> list[dict[str, Any]]:
    message = payload.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def _assistant_text(payload: dict[str, Any]) -> str:
    message = payload.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return ""
    texts = [str(b.get("text") or "") for b in content if isinstance(b, dict)]
    return " ".join(t for t in texts if t.strip())


def _tool_command(block: dict[str, Any]) -> str:
    """What a tool call runs, in the words a person recognises: its command, else its target."""
    arguments = block.get("input")
    if not isinstance(arguments, dict):
        return ""
    for key in ("command", "description", "file_path", "pattern", "url"):
        if str(arguments.get(key) or "").strip():
            return str(arguments[key]).strip()
    return ""


def worker_activity(worker_id: int, after_event_id: int) -> WorkerActivity | None:
    """The worker's session events after ``after_event_id``, summarised, or ``None`` if none.

    Read from what the supervisor already records for a worker (`worker_<provider
    event type>`): Claude's `tool_progress` heartbeat names the running tool call
    and its elapsed time, its `assistant` messages carry the call's command and the
    worker's words, and Codex's `item.started` carries a command execution. Whatever
    a provider says that none of those describe still counts, as a number of events.
    """
    conn = db.init_db()
    try:
        rows = conn.execute(
            "SELECT id, kind, payload FROM events WHERE task_id = ? AND id > ? "
            "AND kind LIKE 'worker\\_%' ESCAPE '\\' ORDER BY id",
            (worker_id, after_event_id),
        ).fetchall()
        if not rows:
            return None
        tool = command = said = ""
        elapsed: float | None = None
        for row in rows:
            kind, payload = str(row["kind"]), _payload(row)
            if kind == "worker_tool_progress":
                tool = str(payload.get("tool_name") or "")
                command = _tool_command_by_id(
                    conn, worker_id, str(payload.get("tool_use_id") or "")
                )
                try:
                    elapsed = float(payload.get("elapsed_time_seconds"))
                except (TypeError, ValueError):
                    elapsed = None
            elif kind == "worker_assistant":
                uses = _tool_uses(payload)
                if uses:
                    tool, command, elapsed = (
                        str(uses[-1].get("name") or ""),
                        _tool_command(uses[-1]),
                        None,
                    )
                elif text := _assistant_text(payload):
                    said = text
            elif kind == "worker_item.started":
                item = payload.get("item")
                if isinstance(item, dict) and item.get("command"):
                    tool, command, elapsed = "shell", str(item["command"]), None
        runs = _command_runs(conn, worker_id, command) if command else 0
        return WorkerActivity(
            last_event_id=int(rows[-1]["id"]),
            events=len(rows),
            tool=tool,
            command=command,
            elapsed_seconds=elapsed,
            runs=runs,
            said=said,
        )
    finally:
        conn.close()


def _tool_command_by_id(conn, worker_id: int, tool_use_id: str) -> str:
    """The command of the tool call a heartbeat is about, from the message that made it."""
    if not tool_use_id:
        return ""
    row = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = 'worker_assistant' "
        "AND instr(payload, ?) > 0 ORDER BY id DESC LIMIT 1",
        (worker_id, tool_use_id),
    ).fetchone()
    if row is None:
        return ""
    for block in _tool_uses(_payload(row)):
        if str(block.get("id") or "") == tool_use_id:
            return _tool_command(block)
    return ""


def _command_runs(conn, worker_id: int, command: str) -> int:
    """How many tool calls this worker has made with exactly ``command``."""
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = 'worker_assistant' "
        "AND instr(payload, 'tool_use') > 0",
        (worker_id,),
    ).fetchall()
    return sum(
        1 for row in rows for block in _tool_uses(_payload(row)) if _tool_command(block) == command
    )


def gate_line(state: Any) -> str:
    """The liveness line for a gate running under the supervisor."""
    command = str(getattr(state, "command", "") or "")
    if not command:
        return f"Gate {getattr(state, 'line', 'running under the supervisor')}"
    scope = "full suite" if getattr(state, "full", False) else "local gate"
    elapsed = _duration(float(getattr(state, "elapsed_seconds", 0.0) or 0.0))
    return f"Gate running under the supervisor: {scope} `{_clipped(command)}`, {elapsed}"


@dataclass(frozen=True)
class RunRead:
    """What the ledger says happened in a ticket's run since the last look."""

    cursor: int
    worker: Worker | None
    #: `(event id, task id, phase, note)` for each worker progress report, in order.
    progress: list[tuple[int, int, str, str]]
    #: The newest unanswered thing a worker did, or ``None``.
    trigger: Trigger | None
    #: Why the worker is gone for good, when it is.
    closed: str | None


def read_run(held: Held, cursor: int) -> RunRead:
    """Read the ticket's run after ``cursor``: worker progress, and what needs a turn.

    Replaying a run from the start is safe, which is what makes `dispatched`
    resumable: a trigger is spent by any later sign the manager acted on it, so a
    `worker_done` that was already reviewed and steered does not start a second
    review, and only the newest unanswered trigger is returned.
    """
    conn = db.init_db()
    try:
        rows = conn.execute(
            "SELECT id, task_id, kind, payload FROM events "
            "WHERE run_id = ? AND id > ? AND (task_id IS NULL OR task_id != ?) ORDER BY id",
            (held.run_id, cursor, held.task_id),
        ).fetchall()
        progress: list[tuple[int, int, str, str]] = []
        trigger: Trigger | None = None
        closed: str | None = None
        for row in rows:
            cursor = int(row["id"])
            kind = str(row["kind"])
            payload = _payload(row)
            task_id = int(row["task_id"] or 0)
            if kind == "worker_progress" and payload.get("phase"):
                # A note, not the stream's phaseless `progress` chatter of the same kind.
                progress.append(
                    (
                        cursor,
                        task_id,
                        str(payload.get("phase") or ""),
                        str(payload.get("note") or ""),
                    )
                )
            elif kind == "worker_done":
                summary = str(payload.get("summary") or "")
                trigger = Trigger(PHASE_REVIEWING, cursor, summary, kind=kind)
            elif kind in ("question", "blocked"):
                question = str(payload.get("question") or "")
                trigger = Trigger(PHASE_BLOCKED, cursor, question, kind=kind)
            elif kind in (WORKER_STOPPED, "error", PR_ATTENTION):
                failure = _failure(kind, payload)
                trigger = Trigger(PHASE_REVIEWING, cursor, failure, failure=True, kind=kind)
            elif kind == "delivered":
                trigger = Trigger(PHASE_DELIVERING, cursor, kind=kind)
            elif kind in ACTED_KINDS:
                trigger = None
            elif kind == "task_closed":
                reason = str(payload.get("reason") or "no reason recorded")
                closed = f"worker task {task_id} was closed: {reason}"
                trigger = None
            elif kind == "dispatched":
                # A turn replaced a closed worker with a new one; the ticket goes on.
                closed = None
        return RunRead(
            cursor=cursor,
            worker=_worker_row(conn, held),
            progress=progress,
            trigger=trigger,
            closed=closed,
        )
    finally:
        conn.close()


def _failure(kind: str, payload: dict[str, Any]) -> str:
    """What stopped a worker, in the ledger's own words, for the review turn."""
    lines = [f"{kind}: {payload.get('summary') or 'no summary recorded'}"]
    reasons = payload.get("reasons")
    if isinstance(reasons, list):
        lines.extend(f"- {reason}" for reason in reasons if reason)
    if payload.get("resume_message"):
        lines.append(str(payload["resume_message"]))
    return "\n".join(lines)


def acted_since(worker_id: int, mark: int) -> bool:
    """Has the manager answered, steered, resumed or delivered this worker since ``mark``?"""
    marks = ",".join("?" for _ in ACTED_KINDS)
    conn = db.init_db()
    try:
        row = conn.execute(
            f"SELECT 1 FROM events WHERE task_id = ? AND id > ? AND kind IN ({marks}) LIMIT 1",
            (worker_id, mark, *ACTED_KINDS),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def delivered_since(worker_id: int, mark: int, by_status: bool = True) -> bool:
    """Is this worker's work delivered — by an event since ``mark``, or by its status?

    ``by_status=False`` asks for the event alone: a worker sent back over its
    open pull request still reads `delivered` until it delivers again.
    """
    conn = db.init_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM events WHERE task_id = ? AND id > ? AND kind = 'delivered' LIMIT 1",
            (worker_id, mark),
        ).fetchone()
        if row is not None:
            return True
        if not by_status:
            return False
        task = store.get_task(conn, worker_id)
        return task is not None and task["status"] == "delivered"
    finally:
        conn.close()


def pull_request_url(worker_id: int) -> str | None:
    conn = db.init_db()
    try:
        row = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = 'delivered' "
            "ORDER BY id DESC LIMIT 1",
            (worker_id,),
        ).fetchone()
        return (_payload(row).get("pr_url") or None) if row is not None else None
    finally:
        conn.close()


def open_person_wait(task_id: int) -> tuple[int, str] | None:
    """The open todo a turn recorded against this ticket to wait on a person."""
    conn = db.init_db()
    try:
        row = conn.execute(
            "SELECT id, text FROM todos WHERE task_id = ? AND status = 'open' "
            "AND (blocked_on = 'user' OR blocked_on LIKE 'user:%') ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return (int(row["id"]), str(row["text"])) if row is not None else None
    finally:
        conn.close()


def last_handled_comment(task_id: int) -> dict[str, Any] | None:
    """The newest handled-comment record on a ticket's task, or ``None`` if it has none."""
    conn = db.init_db()
    try:
        row = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
            (task_id, COMMENT_HANDLED_EVENT),
        ).fetchone()
        return _payload(row) if row is not None else None
    finally:
        conn.close()


def record_comment_handled(task_id: int, comment: dict[str, Any] | None) -> None:
    """Record ``comment`` as the newest handled on this ticket; ``None`` means "none yet".

    An event on the ticket's own task rather than memory, so a restarted
    `serve` does not answer old comments again. Unchanged records are not
    written twice.
    """
    comment_id = str(comment.get("id")) if comment and comment.get("id") else None
    created_at = str(comment.get("created_at")) if comment and comment.get("created_at") else None
    current = last_handled_comment(task_id)
    if current is not None and current.get("comment_id") == comment_id:
        return
    conn = db.init_db()
    try:
        task = store.get_task(conn, task_id)
        store.append_event(
            conn,
            kind=COMMENT_HANDLED_EVENT,
            payload={"task_id": task_id, "comment_id": comment_id, "created_at": created_at},
            run_id=int(task["run_id"]) if task is not None else None,
            task_id=task_id,
        )
    finally:
        conn.close()


def _newest(comments: list[dict[str, Any]]) -> dict[str, Any] | None:
    return comments[-1] if comments else None


def close_person_wait(todo_id: int) -> None:
    from papaya_agent_runtime import board

    with contextlib.suppress(board.TodoError):
        board.done(todo_id)


#: The work-item fields whose change means somebody did something to it.
_FINGERPRINT_KEYS = ("updated_at", "status", "comment_count", "comments_count", "last_comment_at")


def work_item_fingerprint(item: object) -> str | None:
    """A comparable summary of a work item, or ``None`` when it has nothing to compare.

    ``None`` is an honest answer: an item payload carrying none of these fields
    cannot show that a person replied, and the wait then ends only when the todo
    that recorded it is closed.
    """
    if not isinstance(item, dict):
        return None
    seen = {key: item[key] for key in _FINGERPRINT_KEYS if item.get(key) is not None}
    comments = item.get("comments")
    if isinstance(comments, list):
        seen["comments"] = len(comments)
    return json.dumps(seen, sort_keys=True, default=str) if seen else None


def turn_environment(job_env: dict[str, str], *, root: str, run_id: int) -> dict[str, str]:
    """The environment a manager turn gets: the client's job, bounded to the runtime.

    Everything the client gives a job — the context, event, decline and activity
    files, the plugin directory, the workspace and API variables — so the turn
    speaks as the Papaya agent and its tool use keeps the ticket's activity
    stamp fresh. Then two overrides: the write boundary is the runtime directory
    and nothing else, so a turn can register and dispatch but never edit a
    repository by hand; and the ticket's run, so a dispatch lands in it.
    """
    from papaya_agent_client.write_boundary import ALLOWED_ROOTS_ENV

    from papaya_agent_runtime.manager.launch import AGENT_BIN_ENV, papaya_agent_command

    env = dict(job_env)
    env[ALLOWED_ROOTS_ENV] = json.dumps([root])
    env["PAPAYA_WORKING_DIRECTORY"] = root
    env[papaya_events.TICKET_RUN_ENV] = str(run_id)
    # The plugin's hooks and `mcp runner-config` call back into the client as
    # `${PAPAYA_AGENT_BIN:-papaya-agent}` and read its home from the environment.
    # `serve` is not the `papaya-agent` console script, so the client does not set
    # the first, and the home may have been found some other way than the second.
    if not env.get(AGENT_BIN_ENV):
        command = papaya_agent_command({**os.environ, **env})
        if len(command) == 1:
            env[AGENT_BIN_ENV] = command[0]
    if not env.get(papaya.CLIENT_HOME_ENV) and not os.environ.get(papaya.CLIENT_HOME_ENV):
        env[papaya.CLIENT_HOME_ENV] = str(papaya.client_home())
    return env


def turn_transcript_path(run_id: int, turn: str) -> Path:
    """Where the next ``turn`` of this run keeps its transcript: `<turn>-<n>.log`.

    Under the run's own directory, numbered per turn kind, so a brief retried
    twice leaves `brief-1.log` and `brief-2.log` side by side.
    """
    from papaya_agent_runtime.paths import runs_dir

    turns = runs_dir() / str(run_id) / "turns"
    taken = len(list(turns.glob(f"{turn}-*.log"))) if turns.is_dir() else 0
    return turns / f"{turn}-{taken + 1}.log"


def brief_has_goals(worker: Worker) -> bool:
    """Does the brief this worker was dispatched with carry a Goals section?"""
    from papaya_agent_runtime import brief_lint
    from papaya_agent_runtime.preflight import archived_brief_path

    if not worker.repo:
        return False
    try:
        text = archived_brief_path(worker.repo, worker.task_id).read_text(encoding="utf-8")
    except OSError:
        return False
    return "Goals" in brief_lint.outcome_sections(text)


def default_worker_capacity() -> tuple[int, int] | None:
    """`(busy, limit)` for the worker pool, or ``None`` when there is no ceiling to read."""
    from papaya_agent_runtime.config import ConfigError, load_config

    try:
        limit = int(load_config().worker.max_concurrent)
    except ConfigError:
        return None
    conn = db.init_db()
    try:
        return len(store.live_runners(conn)), limit
    finally:
        conn.close()


def _load_config():
    from papaya_agent_runtime.config import ConfigError, load_config

    try:
        return load_config()
    except ConfigError:
        return None


def _ticket_facts(held: Held) -> dict[str, object]:
    item = held.event.payload.get("work_item")
    title = item.get("title") if isinstance(item, dict) else None
    return {
        "work item id": held.event.work_item_id,
        "work item title": title,
        "event": held.event.kind,
        "ticket task id": held.task_id,
        "run id (dispatch with --run-id)": held.run_id,
    }


def _worker_facts(worker: Worker | None) -> dict[str, object]:
    if worker is None:
        return {}
    return {
        "worker task id": worker.task_id,
        "worker status": worker.status,
        "repository": worker.repo,
        "branch": worker.branch,
    }


# ── running ─────────────────────────────────────────────────────────────────


async def _build(options: ServeOptions, runner: Any, *, stdout, extra: dict[str, Any]):
    """The embedded listener for these options, supervised or not."""
    from papaya_agent_client.embed import build_listener, build_supervised_listener

    home = papaya.client_home()
    identity = papaya.identity()
    session_id = session_id_for(identity.connection_id if identity else "")
    shared: dict[str, Any] = {
        "runner": runner,
        "harness": options.harness,
        # What this connection announces itself as to Papaya, on every start.
        # Without it the label follows `--harness`, so a runtime exec'd as
        # `--harness codex` would register as a Codex CLI listener — which is
        # exactly what it is not, and the one fact the app needs to tell a
        # machine running the manager from a machine running a bare harness.
        # `--harness` still names the bundled harness (the `key` and `label` a
        # supervised host reads on `job.request`); only the runtime label is ours.
        "runtime_kind": capabilities.RUNTIME,
        "home": home,
        "working_directory": options.working_directory,
        "session_id": session_id,
    }
    # `extra` is applied last throughout, so a caller holding a seam (the tests
    # hold `events_factory` and `loop_factory`) can also replace anything above it.
    if not options.supervised:
        return await build_listener(**{**shared, **extra})

    from papaya_agent_client.supervisor import DEFAULT_APPROVAL_TIMEOUT_SECONDS

    timeout = (
        options.approval_timeout
        if options.approval_timeout is not None
        else DEFAULT_APPROVAL_TIMEOUT_SECONDS
    )
    supervised: dict[str, Any] = {
        **shared,
        "stdin_fd": _stdin_fd(),
        "approval_timeout": float(timeout),
        "invalid_arguments": options.invalid_arguments,
    }
    writer = protocol_writer(stdout, on_stalled=getattr(runner, "stalled", None))
    return await build_supervised_listener(writer, **{**supervised, **extra})


def _stdin_fd() -> int | None:
    """The host's stdin, when there is one to read decisions from."""
    try:
        return sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return None


# ── setting the checkout up on the way in ───────────────────────────────────


def self_setup(*, stderr) -> None:
    """Configure this checkout, if nobody ever has, before anything listens.

    The intended experience is: clone the runtime, point the desktop app at it,
    connect. Nothing else. Until now that left a checkout in `no_config` — no
    driver profile, no worker ceiling, no database, no memory tree — until a
    person happened to open a session here and run `ppy setup` by hand. The
    readiness module has always said config is the runtime's own job "on its
    first turn"; under `ppy serve` there is no first turn unless `serve` takes
    it, so it takes it here.

    Non-interactive and with no overrides on purpose. Every default then comes
    from where it should: the provider from the harness the person chose when
    they connected this machine (both roles the same — the runtime does not mix
    agents behind anybody's back), and the rest from the setup wizard, which is
    the same code path `ppy setup` runs. A checkout that already has a config is
    left exactly as it is; this is first-run setup, never a reset.

    Nothing here can stop `serve` from starting. The one expected failure is a
    machine with no signed-in harness, which is a person's to fix and which
    readiness is about to say out loud — so it is said once on stderr and the
    listener goes up anyway, ready to take the work it can and to be *there*
    when somebody signs in.
    """
    from papaya_agent_runtime.paths import config_path, ppy_home
    from papaya_agent_runtime.setup.wizard import run_setup

    if config_path().exists():
        return
    try:
        cfg = run_setup(non_interactive=True)
    except Exception as exc:  # noqa: BLE001 - setup must never be why serve did not start
        log.warning("[serve] Could not set this runtime up: %s", exc)
        print(f"ppy serve: could not set this runtime up: {exc}", file=stderr)
        return
    print(
        f"ppy serve: set this runtime up in {ppy_home()} — manager {cfg.manager.provider}, "
        f"workers {cfg.worker.provider} (up to {cfg.worker.max_concurrent} at once), "
        "state database and memory created",
        file=stderr,
    )


def keep_config_right(*, stderr) -> None:
    """Migrate the config, apply every safe remedy, and say each change once.

    Loading migrates an older file in place; `config_changes.apply` restores dropped
    gate tools and learns denied safe-family tools. Every change since the last start
    — including ones a load made between starts — gets one line here, then is marked
    said. Nothing here can stop `serve` from starting.
    """
    from papaya_agent_runtime import config_changes
    from papaya_agent_runtime.config import ConfigError, load_config
    from papaya_agent_runtime.paths import config_path

    if not config_path().exists():
        return
    try:
        load_config()
    except (ConfigError, OSError):
        return  # readiness says what is wrong with it
    config_changes.apply(context="serve start")
    try:
        entries = config_changes.unannounced()
        for entry in entries:
            text = config_changes.line(entry)
            log.info("[serve] %s", text)
            print(f"ppy serve: {text}", file=stderr)
        config_changes.mark_announced(entries)
    except Exception as exc:  # noqa: BLE001 - saying it must never be why serve did not start
        log.warning("[serve] Could not read the config history: %s", exc)


# ── telling the owner what still needs them ─────────────────────────────────

#: The keys a channel may carry its kind under, and the values that mean this one
#: is a direct message. Several because the shape belongs to the workspace API,
#: not to this runtime, and reading one spelling would silently find nothing.
_CHANNEL_KIND_KEYS = ("kind", "type", "channel_type", "channel_kind")
#: Papaya's own agent-to-person DM. The workspace API names it `agent_private`
#: (a channel called `agent-dm:<user>:<agent>`); until task 253 this list did not
#: have it, so the one channel an agent's owner reads was never found.
_AGENT_DM_KINDS = frozenset({"agent_private"})
_DM_KINDS = frozenset({"dm", "direct", "direct_message", "directmessage"})


def dm_channel_id(channels: Any) -> str | None:
    """The DM to speak into, out of everything this agent can see.

    The route that exists for an agent token: `GET /workspaces/{id}/channels`
    answers the public channels plus the ones *this agent* is a member of, and
    `POST .../channels/{id}/messages` posts into one as the agent. There is no
    agent-token route that opens a DM (`POST /workspaces/{id}/dm` takes a
    person's session and writes as that person), so the DM is found rather than
    made. The agent's own DM with a person (`agent_private`) is preferred over a
    person-to-person `dm` it happens to be in. A workspace that shows this agent
    no DM at all gets nothing posted rather than a report in a team channel.
    """
    if isinstance(channels, dict):  # a wrapped list is the other shape this can arrive in
        channels = channels.get("channels")
    if not isinstance(channels, list):
        return None
    for wanted in (_AGENT_DM_KINDS, _DM_KINDS):
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            kinds = {str(channel.get(key) or "").strip().lower() for key in _CHANNEL_KIND_KEYS}
            if kinds & wanted:
                identifier = str(channel.get("id") or channel.get("channel_id") or "").strip()
                if identifier:
                    return identifier
    return None


def _where() -> str:
    """The machine this report is about, by its short hostname and nothing more.

    The person reading it is usually not at the machine, so "which one" is half
    the message. The instance path used to follow it; a home path is private, and
    one runtime per machine is the case the hostname already names.
    """
    return blockers.short_hostname()


async def _post_dm(built: Any, text: str) -> bool:
    """Put `text` in the agent's DM, and say whether it got there."""
    from papaya_agent_client import api_client

    api = getattr(built, "api", None)
    if api is None:
        return False
    try:
        channels = await api_client.list_agent_channels(api)
    except Exception as exc:  # noqa: BLE001 - an unreachable workspace is not a crash
        log.warning("[serve] Could not read this agent's channels: %s", exc)
        return False
    channel_id = dm_channel_id(channels)
    if channel_id is None:
        log.warning(
            "[serve] This agent is in no DM channel (agent_private or dm), so readiness "
            "was not posted; it is on stderr, and posted on the next start that finds one"
        )
        return False
    try:
        await api_client.post_agent_channel_message(api, channel_id, text)
    except Exception as exc:  # noqa: BLE001 - reporting must not stop the listener
        log.warning("[serve] Could not post the readiness report: %s", exc)
        return False
    return True


async def report_readiness(verdict, built, watch: blockers.Watch | None = None) -> None:
    """DM the owner what still needs them — once per distinct situation.

    Once, because the alternative is a message every restart saying the same
    thing, which is how a person learns to ignore the one that is new. The
    fingerprint is over the problem *codes*, so an unchanged situation stays
    quiet and a changed one speaks however soon it appears.

    The blockers the watch has due ride in the same message, with their steps,
    so a start never sends two. Marked as reported only when the post actually
    landed: a workspace that was unreachable at start-up is not a person who has
    been told. And nothing here can stop the listener going up — the state this
    reads on is the same state a failed self-setup may have been unable to create.
    """
    watch = watch or blockers.Watch(say=functools.partial(_post_dm, built))
    lead = ""
    conn = None
    if verdict.state != readiness.READY:
        who = papaya.identity()
        try:
            conn = db.init_db()
            if not readiness.already_reported(conn, verdict):
                lead = readiness.report(verdict, agent=who.addressed if who else "", where=_where())
        except Exception as exc:  # noqa: BLE001 - an unreadable home is already the verdict
            log.warning("[serve] Could not read what readiness has reported: %s", exc)

    def said() -> None:
        if lead and conn is not None:
            readiness.mark_reported(conn, verdict)
            log.info("[serve] Reported readiness (%s) to the owner's DM", verdict.state)

    try:
        await watch.round(verdict=verdict, lead=lead, on_said=said)
    except Exception as exc:  # noqa: BLE001 - saying it must not be why serve stopped
        log.warning("[serve] Could not report readiness: %s", exc)
    finally:
        if conn is not None:
            conn.close()


def publish_status(built: Any) -> None:
    """Send a supervised host a fresh `status`, so a changed `runtime.blockers` reaches it.

    The client publishes `status` only when its own phase changes; a blocker that
    appears or clears while the connection is healthy would otherwise wait for
    the next reconnect to be seen.
    """
    supervisor = getattr(built, "supervisor", None)
    if supervisor is None:
        return
    supervisor.writer.send("status", phase=supervisor.phase)


def _announce_readiness(verdict, built, *, stderr) -> None:
    """Say once, at start, that this runtime cannot dispatch anything yet, and what needs a person.

    Once: the runner repeats the same sentence to every job it declines, and a
    host that heard it at start does not need it again on a timer. Non-fatal,
    because `serve` still runs — a manager that refused to start because no
    repository was registered would be unreachable at exactly the moment somebody
    wanted to register one. The blockers, with their steps, are printed whatever
    the verdict: a signed-out forge stops delivery without blocking the rest.
    """
    found = blockers.from_verdict(verdict)
    if found:
        print("ppy serve: setup needed on this machine:", file=stderr)
        for line in blockers.render_text(found).splitlines():
            print(f"  {line}", file=stderr)
    if verdict.state != readiness.BLOCKED:
        return
    line = readiness.headline(verdict)
    log.error("[serve] %s", line)
    print(f"ppy serve: {line}", file=stderr)
    supervisor = getattr(built, "supervisor", None)
    if supervisor is not None:
        supervisor.error(RUNTIME_NOT_READY, line, fatal=False)


async def run(
    options: ServeOptions,
    *,
    stdout,
    stderr,
    extra: dict[str, Any],
    runner: TicketRunner | None = None,
    server: Any = None,
    sweep_sleep: Any = None,
    rounds_seams: dict[str, Any] | None = None,
    self_report: deficiencies.Reporter | None = None,
    blocker_seams: dict[str, Any] | None = None,
) -> int:
    """Set this checkout up, build the listener, report once, sweep, run until stopped.

    ``runner`` is for tests, which hand in a :class:`TicketRunner` whose harness,
    Papaya API and worker pool are fakes; a real start builds the default one.

    `server` is the supervisor this process runs, when it runs one: `ppy sweep`
    reaches the sweeper through it. `sweep_sleep` is the sweep timer's seam for
    tests, the way `renew_sleep` is the loop's. `rounds_seams` are keyword seams for
    the manager's rounds (:class:`~papaya_agent_runtime.rounds.Rounds`): its timer,
    clock, forge and worktree hygiene. `self_report` is the reporter that opens GitHub
    issues about the runtime's own deficiencies; a test hands in one with a fake `gh`.
    `blocker_seams` are keyword seams for the blocker watch
    (:class:`~papaya_agent_runtime.blockers.Watch`): its timer, clock and GitHub
    device flow.
    """
    reporter = self_report or deficiencies.Reporter()
    deficiencies.add_listener(reporter.flush_soon)
    try:
        return await _run(
            options,
            stdout=stdout,
            stderr=stderr,
            extra=extra,
            runner=runner,
            server=server,
            sweep_sleep=sweep_sleep,
            rounds_seams=rounds_seams,
            blocker_seams=blocker_seams,
        )
    finally:
        deficiencies.remove_listener(reporter.flush_soon)
        await asyncio.to_thread(reporter.wait, 30.0)


def announce_deficiencies(*, stderr) -> None:
    """One line at start when self-reported deficiencies are waiting to open as issues."""
    counts = deficiencies.summary()
    waiting = counts["waiting"]
    if not waiting:
        return
    enabled = deficiencies.settings().enabled
    line = (
        f"{waiting} self-reported deficienc{'y is' if waiting == 1 else 'ies are'} waiting "
        + ("to open as issues" if enabled else "in the ledger (self_report.enabled = false)")
        + " — `ppy deficiency list`"
    )
    log.info("[serve] %s", line)
    print(f"ppy serve: {line}", file=stderr)


def unremedied_readiness(verdict: readiness.Readiness) -> list[readiness.Problem]:
    """Blocking findings the runtime owns that its start-up remedies left standing.

    `serve` runs first-run setup before it checks; a finding the runtime calls its
    own that is still blocking afterwards has no remedy in code. When a person's
    blocking finding stands beside it, the runtime's may be downstream of theirs
    (setup cannot finish with no signed-in harness), so nothing is recorded then.
    """
    if any(p.blocking and p.owner == readiness.USER for p in verdict.problems):
        return []
    return [p for p in verdict.problems if p.blocking and p.owner == readiness.RUNTIME]


async def _run(
    options: ServeOptions,
    *,
    stdout,
    stderr,
    extra: dict[str, Any],
    runner: TicketRunner | None,
    server: Any,
    sweep_sleep: Any,
    rounds_seams: dict[str, Any] | None,
    blocker_seams: dict[str, Any] | None = None,
) -> int:
    from papaya_agent_client.embed import ListenerSetupError

    from papaya_agent_runtime import rounds

    # Before anything is said to Papaya: a connection whose runtime has never been
    # configured is the silent failure this whole sequence exists to end.
    await asyncio.to_thread(self_setup, stderr=stderr)
    await asyncio.to_thread(keep_config_right, stderr=stderr)
    await asyncio.to_thread(announce_deficiencies, stderr=stderr)
    deficiencies.notify()
    # Checked after the runtime has applied its own remedies and before the listener
    # is built, so the `hello` a supervised host gets already carries only the
    # blockers a person has to close.
    verdict = await asyncio.to_thread(readiness.check)
    with contextlib.suppress(Exception):
        await asyncio.to_thread(blockers.update, verdict)
    runner = runner or TicketRunner()
    try:
        built = await _build(options, runner, stdout=stdout, extra=extra)
    except ListenerSetupError as exc:
        # Supervised, this has already gone down the protocol as a fatal `error`;
        # the status is the client's own for this failure, so `serve` exits the
        # way `papaya-agent listen` would have.
        print(f"ppy serve: {exc.message}", file=stderr)
        if exc.advice:
            print(f"  {exc.advice}", file=stderr)
        return exc.status

    for problem in unremedied_readiness(verdict):
        await asyncio.to_thread(
            deficiencies.record,
            deficiencies.READINESS_UNREMEDIED,
            problem.code,
            evidence={"code": problem.code, "error": problem.summary},
        )
    _announce_readiness(verdict, built, stderr=stderr)
    # Readiness re-runs every round, so a blocker a person closes clears on its own
    # — nothing to restart — and its owner hears that once.
    watch = blockers.Watch(
        say=functools.partial(_post_dm, built),
        publish=functools.partial(publish_status, built),
        interval=options.rounds_interval or rounds.DEFAULT_ROUNDS_INTERVAL,
        **(blocker_seams or {}),
    )
    await report_readiness(verdict, built, watch)
    watching = asyncio.create_task(watch.run())

    event_loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError, OSError):
            event_loop.add_signal_handler(sig, built.loop.request_stop)
    if not options.supervised:
        print(
            f"ppy serve: listening as {built.agent_ref or 'this connection'} "
            f"(session {built.session_id}); Ctrl-C to stop",
            file=stderr,
        )

    # The sweep shares this event loop with the listener: every offer goes into
    # the loop the listener is running, so it is a task beside `loop.run()` rather
    # than a thread of its own. It starts with a sweep straight away — a start is
    # exactly when an assignment missed while the machine was off is waiting.
    #
    # The rounds share it too, and one lock with the sweep: a round re-offering a
    # ticket and a sweep offering it at the same moment would each see it unheld.
    # The rounds task is created first, so the reclaim on start runs before the
    # start sweep and the sweep finds the reclaimed tickets already running.
    lock = asyncio.Lock()
    manager_rounds = rounds.Rounds(
        built,
        runner,
        interval=options.rounds_interval,
        stderr=stderr,
        lock=lock,
        **(rounds_seams or {}),
    )
    walking = asyncio.create_task(manager_rounds.run())
    sweeper = sweep.Sweeper(
        built, interval=options.sweep_interval, stderr=stderr, sleep=sweep_sleep, lock=lock
    )
    sweeping = asyncio.create_task(sweeper.run())
    if server is not None:
        server.sweep_handler = functools.partial(sweeper.sweep_from_thread, event_loop)
    try:
        await built.loop.run()
    finally:
        if server is not None:
            server.sweep_handler = None
        for background in (walking, sweeping, watching):
            background.cancel()
        for background in (walking, sweeping, watching):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await background
        await watch.close()
        # A sweep that was mid-offer while the listener shut down can have started
        # a run after `shutdown` took its list of what to release. Shutting down
        # again is safe (a released subject is never released twice) and is the
        # only way that run's lease is let go rather than left to expire.
        if built.loop.running_subjects:
            await built.loop.shutdown()
    return 0


def serve(argv: list[str] | None = None, *, stdout=None, stderr=None, **extra: Any) -> int:
    """Run the manager until it is told to stop. The whole of `ppy serve`.

    `extra` is passed straight through to the client's builders; the tests use its
    `events_factory` and `loop_factory` seams, and nothing else should.
    """
    from papaya_agent_runtime.supervisor.server import SupervisorOwned, SupervisorServer

    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    options = parse_args(list(argv or []))
    for flag in options.ignored:
        print(
            f"ppy serve: ignoring {flag}; it is a `papaya-agent listen` flag "
            "this runtime does not take",
            file=stderr,
        )
    if options.invalid_arguments and not options.supervised:
        # Unsupervised there is no protocol to report it on, and running with an
        # argument we could not honour would be the worse of the two answers.
        print(f"ppy serve: {options.invalid_arguments}", file=stderr)
        return 2

    # Logs on stderr, always: under `--supervised` stdout carries the protocol and
    # one stray log line on it is a parse error in the host.
    logging.basicConfig(stream=stderr, level=logging.INFO, format="%(message)s")

    server = SupervisorServer()
    try:
        server.start_background()
    except SupervisorOwned as exc:
        print(f"refusing to start: {exc}", file=stderr)
        return 1
    try:
        return asyncio.run(run(options, stdout=stdout, stderr=stderr, extra=extra, server=server))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # Caught at the very top: said, recorded with its traceback, and an exit
        # status rather than a stack trace down a supervised host's pipe.
        log.exception("[serve] Stopped on an unhandled exception")
        print(f"ppy serve: stopped on an unhandled exception: {exc}", file=stderr)
        deficiencies.record_exception("ppy serve", exc)
        return 1
    finally:
        # The listener has already stopped by the time `run` returns (its own
        # `shutdown` releases every subject it holds), so the supervisor is the
        # last thing down and nothing is working a repository while it goes.
        server.stop()


__all__ = [
    "HARNESSES",
    "PHASE_BLOCKED",
    "PHASE_BRIEFING",
    "PHASE_DECLINED",
    "PHASE_DELIVERING",
    "PHASE_DISPATCHED",
    "PHASE_HANDED_BACK",
    "PHASE_PICKED_UP",
    "PHASE_RELEASED",
    "PHASE_REPORTED",
    "PHASE_REVIEWING",
    "PHASE_STALLED",
    "GATE_STEERS",
    "LIVENESS_SECONDS",
    "RUNTIME_NOT_READY",
    "SENT_BACK_LINE",
    "WAIT_FIRST_SECONDS",
    "WAIT_MAX_SECONDS",
    "WORKER_STOPPED",
    "WORKING_PHASES",
    "Declined",
    "HandBack",
    "Held",
    "ServeOptions",
    "TicketRunner",
    "Worker",
    "brief_has_goals",
    "default_worker_capacity",
    "dm_channel_id",
    "find_worker",
    "gate_line",
    "gate_steer_message",
    "parse_args",
    "phase_for_stop",
    "phase_history",
    "protocol_writer",
    "publish_status",
    "read_run",
    "record_phase",
    "remember_session_id",
    "report_readiness",
    "rerun_delay",
    "resumable_phase",
    "runtime_descriptor",
    "self_setup",
    "sent_back_before",
    "serve",
    "session_id_for",
    "stalled_resume_phase",
    "steer_worker",
    "stored_session_ids",
    "turn_environment",
    "turn_transcript_path",
    "waiting_reason",
    "worker_activity",
    "worker_session_live",
]
