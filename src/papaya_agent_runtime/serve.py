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

What the runner does, today
---------------------------
Deliberately little, and the little it does is the whole point: for each approved
job it records the ticket, writes the phase ``picked_up``, and then **waits**. It
holds the lease for as long as the client will let it, and when the client stops
the run — a hand-back from the app, a lease lost to a person, the stall grace, or
this process shutting down — it records why and answers what `run_command`
answers. Nothing is briefed, dispatched, reviewed or delivered here; that is the
next task. A ticket this runtime cannot place (no repository it can resolve, or an
event carrying no work item at all) is declined through the client's own decline
path, so a peer that *can* do the work may still take it.

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
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass
from typing import Any

from papaya_agent_runtime import capabilities, papaya, papaya_events, readiness
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

#: The phases this runner writes to a task. `picked_up` the moment the ticket is
#: taken; the rest are how the hold ended, chosen by the client's own stop reason.
PHASE_PICKED_UP = "picked_up"
PHASE_RELEASED = "released"
PHASE_HANDED_BACK = "handed_back"
PHASE_STALLED = "stalled"
PHASE_DECLINED = "declined"


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
    return ServeOptions(
        supervised=bool(known.supervised),
        harness=known.harness,
        approval_timeout=known.approval_timeout,
        working_directory=known.working_directory,
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
    repo: str


@dataclass(frozen=True)
class Declined:
    """A ticket this manager is not the right machine for, and why."""

    reason: str


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
    """Take one approved job's ticket, then hold its lease until told to stop.

    The holding is the feature. Everything before it — reading the envelope,
    hydrating the work item, resolving the repository, recording the task — is
    ordinary blocking work (it can clone a repository over the network), so it
    runs on a thread. Doing it inline would stall the event loop the renewals run
    on, and a manager that stops renewing while it clones loses the very lease it
    just won.
    """

    def __init__(self, *, check_readiness=None) -> None:
        # Checked per job rather than once, so a runtime that is set up *while*
        # `serve` is running starts taking work without a restart.
        self._check_readiness = check_readiness or readiness.check

    async def __call__(self, job: Any) -> dict[str, Any]:
        outcome = await asyncio.to_thread(self.take, job)
        if isinstance(outcome, Declined):
            log.info("[serve] Declining %s: %s", job.job_id, outcome.reason)
            job.decline(outcome.reason)
            return _result(job, _declined_exit_code(), outcome.reason)

        log.info("[serve] Holding %s for task %d in %s", job.subject, outcome.task_id, outcome.repo)
        # Stamped once, here, and then never again: nothing else is happening, and
        # a hold that quietly faked liveness would be a lease nobody could ever
        # take back. The stall grace expiring is a legitimate end to a hold, not a
        # failure to work around.
        job.touch_activity()
        _report_progress(
            job, PHASE_PICKED_UP, f"Recorded as task {outcome.task_id} in {outcome.repo}."
        )

        try:
            await job.stop.wait()
        except asyncio.CancelledError:
            await asyncio.to_thread(self._record_phase, outcome.task_id, PHASE_RELEASED)
            raise
        phase = phase_for_stop(job.stop.reason)
        await asyncio.to_thread(self._record_phase, outcome.task_id, phase)
        log.info("[serve] Released %s for task %d (%s)", job.subject, outcome.task_id, phase)
        return _result(job, 0, f"task {outcome.task_id} {phase}")

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
            event = papaya_events.hydrate_work_item(event, environ=job.env)
        except papaya_events.PapayaEventError as exc:
            # Best effort, and deliberately not a decline of its own. The envelope
            # is thinner than the full record — no description, no repository
            # metadata — so a failed read usually surfaces one line later as a
            # repository that cannot be resolved, and *that* is the sentence worth
            # handing back. Declining here would hide it behind a network error.
            log.warning("[serve] Could not read the full work item for %s: %s", job.subject, exc)
        try:
            ensured = papaya_events.ensure_repository(event)
        except papaya_events.PapayaEventError as exc:
            return self._decline(event, str(exc))

        conn = db.init_db()
        try:
            return Held(task_id=self._record(conn, event, ensured), repo=ensured.name)
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
        """
        from papaya_agent_runtime.paths import db_path

        if db_path().exists():
            with contextlib.suppress(Exception):
                conn = db.init_db()
                try:
                    existing = papaya_events.find_existing_task(
                        conn, papaya_events.event_key(event)
                    )
                    if existing is not None:
                        store.set_task_phase(conn, int(existing["id"]), PHASE_DECLINED)
                finally:
                    conn.close()
        return Declined(reason)

    def _record(self, conn, event: papaya_events.PapayaEvent, ensured) -> int:
        """The task row for this event, found or created, marked `picked_up`.

        Found *or* created: the event key is what makes a redelivered event
        harmless, and a second pick-up of the same ticket has to land on the task
        the first one made rather than fork a new one beside it.
        """
        existing = papaya_events.find_existing_task(conn, papaya_events.event_key(event))
        if existing is not None:
            task_id = int(existing["id"])
        else:
            title = _title(event)
            repo = store.get_repo(conn, ensured.name)
            run_id = store.create_run(conn, title)
            task_id = store.add_task(
                conn,
                run_id=run_id,
                title=title,
                repo_id=int(repo["id"]) if repo is not None else None,
            )
            papaya_events.record_task(conn, task_id, event)
        store.set_task_phase(conn, task_id, PHASE_PICKED_UP)
        return task_id

    @staticmethod
    def _record_phase(task_id: int, phase: str) -> None:
        conn = db.init_db()
        try:
            store.set_task_phase(conn, task_id, phase)
        finally:
            conn.close()


def _declined_exit_code() -> int:
    """75, read from the client rather than restated, so the two cannot drift."""
    from papaya_agent_client.command_runner import DECLINED_EXIT_CODE

    return int(DECLINED_EXIT_CODE)


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
_DM_KINDS = frozenset({"dm", "direct", "direct_message", "directmessage"})


def dm_channel_id(channels: Any) -> str | None:
    """The DM to speak into, out of everything this agent can see.

    The client's API module has no call that addresses a person's DM by itself
    (see `list_agent_channels` / `post_agent_channel_message` — channels, by id).
    So the DM is found rather than named: the agent's channel list is asked for,
    and the first channel that says it is a direct message is the one the owner
    reads. A workspace that shows this agent no DM at all gets nothing posted
    rather than a readiness report in a team channel.
    """
    if isinstance(channels, dict):  # a wrapped list is the other shape this can arrive in
        channels = channels.get("channels")
    if not isinstance(channels, list):
        return None
    for channel in channels:
        if not isinstance(channel, dict):
            continue
        kinds = {str(channel.get(key) or "").strip().lower() for key in _CHANNEL_KIND_KEYS}
        if kinds & _DM_KINDS:
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
        log.warning("[serve] No direct-message channel to report readiness in; said on stderr only")
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


async def run(options: ServeOptions, *, stdout, stderr, extra: dict[str, Any]) -> int:
    """Set this checkout up, build the listener, report once, run until stopped."""
    from papaya_agent_client.embed import ListenerSetupError

    # Before anything is said to Papaya: a connection whose runtime has never been
    # configured is the silent failure this whole sequence exists to end.
    await asyncio.to_thread(self_setup, stderr=stderr)
    try:
        built = await _build(options, TicketRunner(), stdout=stdout, extra=extra)
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
    await built.loop.run()
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
        return asyncio.run(run(options, stdout=stdout, stderr=stderr, extra=extra))
    except KeyboardInterrupt:
        return 0
    finally:
        # The listener has already stopped by the time `run` returns (its own
        # `shutdown` releases every subject it holds), so the supervisor is the
        # last thing down and nothing is working a repository while it goes.
        server.stop()


__all__ = [
    "HARNESSES",
    "PHASE_DECLINED",
    "PHASE_HANDED_BACK",
    "PHASE_PICKED_UP",
    "PHASE_RELEASED",
    "PHASE_STALLED",
    "RUNTIME_NOT_READY",
    "Declined",
    "Held",
    "ServeOptions",
    "TicketRunner",
    "dm_channel_id",
    "parse_args",
    "phase_for_stop",
    "protocol_writer",
    "remember_session_id",
    "report_readiness",
    "runtime_descriptor",
    "self_setup",
    "serve",
    "session_id_for",
    "stored_session_ids",
]
