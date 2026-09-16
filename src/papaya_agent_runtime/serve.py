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
review turn with the failure attached. How it knows a turn did its job is
mechanical too — a worker row in the ticket's run, or an answer, steer or
delivery event since the turn began. A turn that did not is retried once with the
tail of its transcript; a second miss hands the ticket back.

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
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from papaya_agent_runtime import capabilities, papaya, papaya_events, prompts, readiness, sweep
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

#: How many attempts a turn gets at its job before the ticket is handed back.
TURN_ATTEMPTS = 2

#: Event kinds on a worker task that mean the manager has acted on it, so the
#: trigger before them is spent. Used both to tell whether a turn did its job and
#: to stop a replayed history re-triggering a turn that already ran.
ACTED_KINDS = ("answer", "steer", "resumed", "auto_answered", "review_requested", "delivered")


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
    return ServeOptions(
        supervised=bool(known.supervised),
        harness=known.harness,
        approval_timeout=known.approval_timeout,
        working_directory=known.working_directory,
        sweep_interval=interval,
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


def protocol_writer(stream: Any) -> Any:
    """A `ProtocolWriter` whose `hello` also names the runtime behind the client.

    The client's `hello` describes the *client*: its protocol version, its own
    version, the home and the working directory. A host that exec'd a runtime
    needs one more fact — which runtime answered — and it is added here, on the
    one message it belongs on. Everything else goes through untouched.

    0.15.1 has the destination but not the road: `Supervisor.runtime` is a field
    the client deliberately never sets ("a runtime the client handed the process
    to fills it"), but `build_supervised_listener` constructs the `Supervisor`
    itself, takes no `runtime=` argument, and calls `hello()` before it returns —
    so there is no moment at which a host can reach the field. Injecting on the
    message is the seam that exists. When the builder forwards `runtime=`, delete
    this and pass it.
    """
    from papaya_agent_client.supervisor import ProtocolWriter

    class _RuntimeWriter(ProtocolWriter):
        def send(self, message_type: str, **fields: Any) -> dict[str, Any]:
            if message_type == "hello":
                fields.setdefault("runtime", runtime_descriptor())
            return super().send(message_type, **fields)

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


@dataclass(frozen=True)
class Declined:
    """A ticket this manager is not the right machine for, and why."""

    reason: str


@dataclass(frozen=True)
class HandBack:
    """The work on a held ticket cannot go on here, and why."""

    reason: str


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
    (Papaya's HTTP API), ``worker_capacity`` (the worker pool) and ``config``.
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

    async def __call__(self, job: Any) -> dict[str, Any]:
        outcome = await asyncio.to_thread(self.take, job)
        if isinstance(outcome, Declined):
            log.info("[serve] Declining %s: %s", job.job_id, outcome.reason)
            job.decline(outcome.reason)
            return _result(job, _declined_exit_code(), outcome.reason)

        held = outcome
        ticket = Ticket(held=held, job=job)
        where = f" in {held.repo}" if held.repo else ""
        log.info("[serve] Holding %s for task %d%s", job.subject, held.task_id, where)
        if held.resume_from is None:
            await self._status(ticket, papaya_events.STATUS_IN_PROGRESS)
            _report_progress(job, PHASE_PICKED_UP, f"Recorded as task {held.task_id}{where}.")
            said = f"Picked up; working in {held.repo}." if held.repo else "Picked up."
            await self._say(ticket, PHASE_PICKED_UP, said)
        else:
            # A redelivered ticket that was already being worked goes back to where
            # it was. Nothing is picked up twice: no second brief, no second status,
            # and no second comment for the phase the earlier hold already announced.
            ticket.said = held.resume_from
            ticket.quiet_until = await asyncio.to_thread(_max_event_id)
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
            return await self._hand_back(ticket, ending.reason)
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
                if read.trigger.phase == PHASE_BLOCKED:
                    step = await self._answer(ticket)
                    if isinstance(step, HandBack):
                        return step
                    continue
                return read.trigger.phase
            if ticket.worker is None:
                # The ticket was dispatched, but its worker never made it into the
                # ledger (a resume from `dispatched` after the row was lost). Only a
                # new brief can put one there.
                return PHASE_BRIEFING
            await self._sleep(ticket)

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
            mark = await asyncio.to_thread(_max_event_id)
            result = await self._turn(ticket, prompts.ANSWER, self._answer_facts(ticket, tail))
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
        while True:
            await self._wait_on_person(ticket)
            failure = ticket.trigger is not None and ticket.trigger.failure
            detail = (
                f"Worker task {worker_id} stopped short; reviewing what stopped it."
                if failure
                else f"Reviewing worker task {worker_id} at its head."
            )
            await self._enter(ticket, PHASE_REVIEWING, detail, say=detail)
            mark = await asyncio.to_thread(_max_event_id)
            # Taken before the turn and after the runner's own comment: nothing but
            # the turn writes on the item while it runs, so a new agent comment
            # after it is the turn's report.
            before = await asyncio.to_thread(self._comments, ticket)
            result = await self._turn(ticket, prompts.REVIEW, self._review_facts(ticket, tail))
            if await asyncio.to_thread(delivered_since, worker_id, mark):
                ticket.trigger = None
                ticket.reported = await self._check_reported(ticket, before)
                return PHASE_DELIVERING
            if await asyncio.to_thread(acted_since, worker_id, mark):
                ticket.trigger = None
                sent_back = f"Worker task {worker_id} sent back with findings."
                await self._enter(ticket, PHASE_DISPATCHED, sent_back, say=sent_back)
                return PHASE_DISPATCHED
            if await self._wait_on_person(ticket):
                continue
            outcome = self._missed(ticket, misses, "approving and delivering, or steering", result)
            if isinstance(outcome, HandBack):
                return outcome
            tail = outcome

    async def _deliver(self, ticket: Ticket) -> str:
        """The pull request is open: say so, move the item to review, and finish."""
        held = ticket.held
        worker = ticket.worker or await asyncio.to_thread(find_worker, held)
        pr_url = await asyncio.to_thread(pull_request_url, worker.task_id) if worker else None
        opened = f"Pull request open: {pr_url}" if pr_url else "Delivered."
        await self._enter(ticket, PHASE_DELIVERING, opened, say=opened)
        await self._status(ticket, papaya_events.STATUS_REVIEW)
        if ticket.reported is False:
            # The review turn's report was looked for and is not there, twice. Say
            # the one thing the runner knows for certain, and say that it did.
            where = pr_url or "the pull request on the worker's branch"
            fallback = f"Pull request open: {where}; see the pull request for details."
            await self._enter(ticket, PHASE_REPORTED, "reported (fallback)", say=fallback)
        elif ticket.reported:
            reported = "Result reported on this item."
            await self._enter(ticket, PHASE_REPORTED, reported, say=reported)
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

    # -- waiting, without ever blocking the loop ------------------------------

    async def _wait_on_person(self, ticket: Ticket) -> bool:
        """Hold in `blocked` while a turn's question waits on a person's reply.

        A turn that needs a person says so by recording a todo blocked on `user`
        against the ticket task — an existing, local primitive, so the runner can
        read it without interpreting the transcript. The client drops a new event
        for a subject this session already holds, so the reply cannot arrive as a
        `work_item.comment`; the runner watches the work item itself instead, and
        resumes when it changes (or the todo is closed here).

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
            return HandBack(f"the manager turn ended {attempt} times without {job_of_turn}")
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
        result = await asyncio.to_thread(
            runner, launch, should_stop=ticket.should_stop, transcript_path=transcript
        )
        self._check_stop(ticket)
        return result

    def _root(self) -> str:
        from papaya_agent_runtime.manager.launch import repo_root

        return str(Path(self._runtime_dir or repo_root()).resolve())

    def _brief_facts(self, ticket: Ticket, tail: str) -> dict[str, object]:
        held = ticket.held
        return {
            **_ticket_facts(held),
            "repository named by the item": held.repo,
            "previous attempt's transcript (tail)": tail,
        }

    def _answer_facts(self, ticket: Ticket, tail: str) -> dict[str, object]:
        worker = ticket.worker
        return {
            **_ticket_facts(ticket.held),
            **_worker_facts(worker),
            "the worker's question": ticket.trigger.detail if ticket.trigger else "",
            "previous attempt's transcript (tail)": tail,
        }

    def _review_facts(self, ticket: Ticket, tail: str) -> dict[str, object]:
        trigger = ticket.trigger
        return {
            **_ticket_facts(ticket.held),
            **_worker_facts(ticket.worker),
            "what stopped the worker": trigger.detail if trigger and trigger.failure else "",
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
        worker's progress stays in the app and the log. Never fatal.
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

    async def _hand_back(self, ticket: Ticket, reason: str) -> dict[str, Any]:
        """Give the ticket back: declined, status `todo`, one comment, branch kept."""
        held, job = ticket.held, ticket.job
        log.info("[serve] Handing back %s: %s", job.subject, reason)
        await asyncio.to_thread(self._record_phase, held.task_id, PHASE_DECLINED, reason)
        job.decline(reason)
        await self._status(ticket, papaya_events.STATUS_TODO)
        await self._comment(ticket, reason)
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
        """The client ended the hold. Record why; a stall is a hand-back of ours."""
        held, job = ticket.held, ticket.job
        phase = phase_for_stop(job.stop.reason)
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

        conn = db.init_db()
        try:
            return self._record(conn, event, ensured.name if ensured is not None else None)
        finally:
            conn.close()

    @staticmethod
    def _decline(event: papaya_events.PapayaEvent, reason: str) -> Declined:
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
        return Declined(reason)

    def _record(self, conn, event: papaya_events.PapayaEvent, repo_name: str | None) -> Held:
        """The task row for this event, found or created, and where to take it up.

        Found *or* created: the event key is what makes a redelivered event
        harmless, and a second pick-up of the same ticket has to land on the task
        the first one made rather than fork a new one beside it. A found task that
        was mid-work resumes from its working phase instead of being picked up
        again; anything else starts over at `picked_up`.
        """
        existing = papaya_events.find_existing_task(conn, papaya_events.event_key(event))
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
            task_id=task_id, run_id=run_id, repo=repo_name, event=event, resume_from=resume_from
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


def resumable_phase(conn, task_id: int) -> str | None:
    """The working phase a redelivered ticket should pick up from, if any.

    A ticket mid-work resumes. So does one whose hold ended as `released` — a lost
    lease or this process shutting down — from the working phase before it, since
    nobody gave that work away. A ticket that was handed back, stalled, declined,
    or finished starts over.
    """
    phase = store.task_phase(conn, task_id)
    if phase in WORKING_PHASES:
        return phase
    if phase != PHASE_RELEASED:
        return None
    for earlier in reversed(phase_history(conn, task_id)):
        if earlier == PHASE_RELEASED:
            continue
        return earlier if earlier in WORKING_PHASES else None
    return None


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
            if kind == "worker_progress":
                progress.append(
                    (
                        cursor,
                        task_id,
                        str(payload.get("phase") or ""),
                        str(payload.get("note") or ""),
                    )
                )
            elif kind == "worker_done":
                trigger = Trigger(PHASE_REVIEWING, cursor, str(payload.get("summary") or ""))
            elif kind in ("question", "blocked"):
                trigger = Trigger(PHASE_BLOCKED, cursor, str(payload.get("question") or ""))
            elif kind in ("worker_stopped", "error"):
                trigger = Trigger(PHASE_REVIEWING, cursor, _failure(kind, payload), failure=True)
            elif kind == "delivered":
                trigger = Trigger(PHASE_DELIVERING, cursor)
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


def delivered_since(worker_id: int, mark: int) -> bool:
    """Is this worker's work delivered — by an event since ``mark``, or by its status?"""
    conn = db.init_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM events WHERE task_id = ? AND id > ? AND kind = 'delivered' LIMIT 1",
            (worker_id, mark),
        ).fetchone()
        if row is not None:
            return True
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
    return await build_supervised_listener(protocol_writer(stdout), **{**supervised, **extra})


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
    """The machine and the instance this report is about.

    The person reading it is usually not at the machine, so "which one" is half
    the message.
    """
    import socket

    from papaya_agent_runtime.paths import ppy_home

    try:
        host = socket.gethostname().split(".", 1)[0].strip()
    except OSError:
        host = ""
    return f"{host}:{ppy_home()}" if host else str(ppy_home())


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


async def report_readiness(verdict, built) -> None:
    """DM the owner what still needs them — once per distinct situation.

    Once, because the alternative is a message every restart saying the same
    thing, which is how a person learns to ignore the one that is new. The
    fingerprint is over the problem *codes*, so an unchanged situation stays
    quiet and a changed one speaks however soon it appears.

    Marked as reported only when the post actually landed: a workspace that was
    unreachable at start-up is not a person who has been told. And nothing here
    can stop the listener going up — the state this reads on is the same state a
    failed self-setup may have been unable to create.
    """
    if verdict.state == readiness.READY:
        return
    who = papaya.identity()
    text = readiness.report(verdict, agent=who.addressed if who else "", where=_where())
    try:
        conn = db.init_db()
    except Exception as exc:  # noqa: BLE001 - an unreadable home is already the verdict
        log.warning("[serve] Could not record a readiness report: %s", exc)
        return
    try:
        if readiness.already_reported(conn, verdict):
            return
        if await _post_dm(built, text):
            readiness.mark_reported(conn, verdict)
            log.info("[serve] Reported readiness (%s) to the owner's DM", verdict.state)
    except Exception as exc:  # noqa: BLE001 - saying it must not be why serve stopped
        log.warning("[serve] Could not record a readiness report: %s", exc)
    finally:
        conn.close()


def _announce_readiness(verdict, built, *, stderr) -> None:
    """Say once, at start, that this runtime cannot dispatch anything yet.

    Once: the runner repeats the same sentence to every job it declines, and a
    host that heard it at start does not need it again on a timer. Non-fatal,
    because `serve` still runs — a manager that refused to start because no
    repository was registered would be unreachable at exactly the moment somebody
    wanted to register one.
    """
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
) -> int:
    """Set this checkout up, build the listener, report once, sweep, run until stopped.

    ``runner`` is for tests, which hand in a :class:`TicketRunner` whose harness,
    Papaya API and worker pool are fakes; a real start builds the default one.

    `server` is the supervisor this process runs, when it runs one: `ppy sweep`
    reaches the sweeper through it. `sweep_sleep` is the sweep timer's seam for
    tests, the way `renew_sleep` is the loop's.
    """
    from papaya_agent_client.embed import ListenerSetupError

    # Before anything is said to Papaya: a connection whose runtime has never been
    # configured is the silent failure this whole sequence exists to end.
    await asyncio.to_thread(self_setup, stderr=stderr)
    try:
        built = await _build(options, runner or TicketRunner(), stdout=stdout, extra=extra)
    except ListenerSetupError as exc:
        # Supervised, this has already gone down the protocol as a fatal `error`;
        # the status is the client's own for this failure, so `serve` exits the
        # way `papaya-agent listen` would have.
        print(f"ppy serve: {exc.message}", file=stderr)
        if exc.advice:
            print(f"  {exc.advice}", file=stderr)
        return exc.status

    verdict = readiness.check()
    _announce_readiness(verdict, built, stderr=stderr)
    await report_readiness(verdict, built)

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
    sweeper = sweep.Sweeper(
        built, interval=options.sweep_interval, stderr=stderr, sleep=sweep_sleep
    )
    sweeping = asyncio.create_task(sweeper.run())
    if server is not None:
        server.sweep_handler = functools.partial(sweeper.sweep_from_thread, event_loop)
    try:
        await built.loop.run()
    finally:
        if server is not None:
            server.sweep_handler = None
        sweeping.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sweeping
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
    "RUNTIME_NOT_READY",
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
    "parse_args",
    "phase_for_stop",
    "phase_history",
    "protocol_writer",
    "read_run",
    "record_phase",
    "remember_session_id",
    "report_readiness",
    "resumable_phase",
    "runtime_descriptor",
    "self_setup",
    "serve",
    "session_id_for",
    "stored_session_ids",
    "turn_environment",
    "turn_transcript_path",
]
