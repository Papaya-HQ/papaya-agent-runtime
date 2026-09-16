"""`ppy serve`: picking one real ticket up, holding it, and letting it go.

Every test here drives the *client's own loop* with a fake events API, the way
the client's `tests/test_embed.py` does, rather than a stand-in for it. That is
the point of the whole design: the cursor, the acquire-or-extend reserve, the
renewal cadence and the supervised protocol are the client's, and a test that
faked them would be testing a copy nobody ships.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from conftest import scale
from papaya_agent_runtime import papaya, papaya_events, readiness, serve, solicit
from papaya_agent_runtime.state import store
from papaya_agent_runtime.state.db import init_db

CONNECTION_ID = "conn-1"
SUBJECT = "work_item:item-9"
EVENT = {
    "id": 101,
    "kind": "work_item.assigned",
    "subject": SUBJECT,
    "agent_id": "agent-1",
    "workspace_id": "ws-1",
    # Papaya marks the events it will grant a lease on. Without it the playbook
    # degrades `act` to `acknowledge`, and nothing is ever reserved.
    "reservable": True,
    "payload": {
        "work_item": {"id": "item-9", "title": "Fix the thing", "repo": "acme/runtime"},
    },
}


# ── the world a listener is built in ────────────────────────────────────────


def _closed_port() -> int:
    """A loopback port nothing is listening on, so every real call fails at once.

    The client reaches for the network in two places while a listener is built
    (`fetch_context` and, supervised, `whoami`) and both are fail-soft. Pointing
    them at a refused connection exercises that softness instead of patching it
    out, and costs microseconds.
    """
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


@dataclass(frozen=True)
class ClientHome:
    """Where the connection lives, and the one directory its jobs may run in."""

    path: Path
    work_dir: Path


@pytest.fixture
def client_home(tmp_path, monkeypatch) -> ClientHome:
    """A connected Papaya client home, and a cursor that is already at the head.

    The cursor matters: a listener with no stored cursor walks to the head of the
    stream first and dispatches nothing on the way, so a test without one would
    watch its own event be skipped as history.
    """
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    home = tmp_path / "client-home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps(
            {
                "agents": {
                    "agent-1": {
                        "agent_id": "agent-1",
                        "agent_ref": "@tester",
                        "agent_handle": "tester",
                        "agent_name": "Tester",
                        "client_token": "pagc_test_token",
                        "workspace_id": "ws-1",
                        "connection_id": CONNECTION_ID,
                        "working_directory": str(work_dir),
                    }
                },
                "connect": {
                    "agent_id": "agent-1",
                    "connection_id": CONNECTION_ID,
                    "harness": "claude",
                    "updated_at": "2026-09-16T00:00:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    (home / f"events-cursor-{CONNECTION_ID}.json").write_text(
        json.dumps({"cursor": 0}), encoding="utf-8"
    )
    monkeypatch.setenv(papaya.HOME_ENV, str(home))
    monkeypatch.setenv("PAPAYA_AGENT_HOME", str(home))
    monkeypatch.setenv("PAPAYA_API_URL", f"http://127.0.0.1:{_closed_port()}")
    return ClientHome(path=home, work_dir=work_dir)


@pytest.fixture
def ready(ppy_home, monkeypatch):
    """An already-set-up runtime whose readiness passes.

    Both halves matter to every test that is about something else. The config
    means `serve`'s own first-run setup finds nothing to do, so no test that is
    measuring a ticket goes probing this machine for signed-in harnesses; the
    verdict means the runner's readiness gate is not what is being measured.
    """
    from papaya_agent_runtime.config import ManagerProfile, MMConfig, WorkerCeiling, save_config

    save_config(
        MMConfig(
            manager=ManagerProfile("claude", "opus", "high"),
            worker=WorkerCeiling("claude", "opus", "medium"),
        )
    )
    monkeypatch.setattr(readiness, "check", lambda: readiness.Readiness(state=readiness.READY))


def _harness_report(usable: list[str]) -> dict:
    """What discovery sees on a machine where `usable` are signed in."""

    def make(name: str) -> dict:
        return {
            "name": name,
            "kind": "harness",
            "path": f"/usr/bin/{name}" if name in usable else None,
            "version": "1.0.0",
            "authenticated": name in usable,
            "available": name in usable,
            "detail": "" if name in usable else f"run `{name} login`",
        }

    # In the order given, so a test can put the *other* harness first and prove
    # that what decided the provider was the connection rather than list position.
    names = [*usable, *[n for n in ("claude", "codex") if n not in usable]]
    return {"harnesses": [make(n) for n in names], "requirements": [], "companions": []}


@pytest.fixture
def harnesses(monkeypatch):
    """Describe this machine to both the setup wizard and readiness.

    Both, because they reach discovery differently: the wizard bound `discover`
    at import, readiness imports it inside the function. A test that patched one
    would have `serve` set itself up against a machine readiness does not agree
    exists.
    """
    from papaya_agent_runtime.setup import discovery, wizard

    def signed_in(*usable: str) -> None:
        report = _harness_report(list(usable))
        monkeypatch.setattr(discovery, "discover", lambda: report)
        monkeypatch.setattr(wizard, "discover", lambda: report)

    return signed_in


@dataclass
class FakeDM:
    """The workspace's channel list, and every message posted into it."""

    channels: list[dict[str, Any]]
    posts: list[tuple[str, str]] = field(default_factory=list)


@pytest.fixture
def dm(monkeypatch) -> FakeDM:
    """The client's API module, answering channel calls without a workspace."""
    from papaya_agent_client import api_client

    fake = FakeDM(
        channels=[
            {"id": "chan-team", "kind": "channel", "name": "engineering"},
            {"id": "chan-dm", "kind": "dm", "name": "Shane"},
        ]
    )

    async def list_agent_channels(_api: Any) -> list[dict[str, Any]]:
        return fake.channels

    async def post_agent_channel_message(
        _api: Any, channel_id: str, content: str, *, parent_id: str | None = None
    ) -> dict[str, Any]:
        fake.posts.append((channel_id, content))
        return {"id": "msg-1"}

    monkeypatch.setattr(api_client, "list_agent_channels", list_agent_channels)
    monkeypatch.setattr(api_client, "post_agent_channel_message", post_agent_channel_message)
    return fake


@pytest.fixture
def registered_repo(ppy_home, monkeypatch) -> str:
    """`acme/runtime` resolves to a registered repo, with no clone and no network."""
    conn = init_db()
    store.add_repo(
        conn,
        name="runtime",
        origin="https://github.com/acme/runtime",
        local_path=str(ppy_home / "repos" / "runtime"),
        default_branch="main",
        base_sha="a" * 40,
    )
    conn.close()
    monkeypatch.setattr(
        papaya_events.solicit,
        "ensure",
        lambda spec, **kw: solicit.Ensured("runtime", "acme/runtime", False, False, ""),
    )
    return "runtime"


class FakeEvents:
    """The events API the client's loop talks to, with nothing behind it.

    Every method the loop calls, and only those: `EventsClient`'s surface is the
    seam `build_listener(events_factory=...)` exists for.
    """

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._queue = list(events)
        self.connection: list[dict[str, Any]] = []
        self.reserves: list[tuple[str, str]] = []
        self.releases: list[tuple[str, str, bool]] = []
        self.acked: list[int] = []
        self.hand_backs: list[str | None] = []

    async def patch_connection(self, **fields: Any) -> dict[str, Any]:
        self.connection.append(fields)
        return {}

    async def list_events(self, since: int = 0, limit: int = 50) -> dict[str, Any]:
        events, self._queue = self._queue, []
        return {"events": events, "has_more": False, "next_cursor": 0}

    async def reserve(
        self, subject: str, session_id: str, ttl_seconds: int | None = None
    ) -> dict[str, Any]:
        self.reserves.append((subject, session_id))
        # Always `renewed`: the loop reads `renewed: false` as a lease taken away
        # and stops the run, which is a different test than this one.
        return {"granted_ttl_seconds": 90, "renewed": True}

    async def release(self, subject: str, session_id: str, declined: bool = False) -> None:
        self.releases.append((subject, session_id, declined))

    async def ack(self, cursor: int) -> None:
        self.acked.append(cursor)

    async def hand_back(self, subject: str | None = None) -> dict[str, Any]:
        self.hand_backs.append(subject)
        return {"handed_back": 1, "released_run": True}


class Ticks:
    """The renewal cadence, fired on demand instead of a fraction of a real TTL."""

    def __init__(self) -> None:
        self._go: asyncio.Queue[None] = asyncio.Queue()
        self.count = 0

    async def sleep(self, _interval: float) -> None:
        await self._go.get()
        self.count += 1

    def tick(self) -> None:
        self._go.put_nowait(None)


@dataclass
class Harness:
    """Everything a test holds on to while `serve.run` drives the real loop."""

    events: FakeEvents
    ticks: Ticks = field(default_factory=Ticks)
    jobs: list[Any] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    loop_kwargs: dict[str, Any] = field(default_factory=dict)
    loop: Any = None

    def extra(self) -> dict[str, Any]:
        return {
            "events_factory": lambda _api: self.events,
            "loop_factory": self._build_loop,
            "poll_interval": 0.05,
        }

    def _build_loop(self, events: Any, **kwargs: Any) -> Any:
        from papaya_agent_client.listener import ListenerLoop

        self.loop_kwargs = dict(kwargs)
        inner = kwargs.pop("runner")

        async def spy(job: Any) -> dict[str, Any]:
            self.jobs.append(job)
            result = await inner(job)
            self.results.append(result)
            return result

        self.loop = ListenerLoop(events, runner=spy, renew_sleep=self.ticks.sleep, **kwargs)
        return self.loop


async def _until(predicate, *, what: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + scale(timeout)
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


# ── terminal mode ───────────────────────────────────────────────────────────


def test_it_picks_up_one_assignment_holds_it_and_releases_it_on_stop(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """The whole skeleton in one run: take the ticket, hold the lease, let go."""
    harness = Harness(FakeEvents([EVENT]))
    options = serve.parse_args(
        ["--harness", "codex", "--working-directory", str(client_home.work_dir)]
    )
    stderr = io.StringIO()

    async def scenario() -> int:
        runner = asyncio.create_task(
            serve.run(options, stdout=io.StringIO(), stderr=stderr, extra=harness.extra())
        )
        await _until(lambda: harness.jobs, what="the job to start")
        await _until(lambda: harness.events.reserves, what="the subject to be reserved")

        # Held across two renewal ticks: the run is still going and the lease has
        # been extended twice by the session that took it.
        harness.ticks.tick()
        await _until(lambda: harness.ticks.count >= 1, what="the first renewal")
        harness.ticks.tick()
        await _until(lambda: len(harness.events.reserves) >= 3, what="two renewals")
        assert not harness.results, "the runner stopped holding before it was told to"

        harness.jobs[0].stop.set()
        await _until(lambda: harness.results, what="the hold to end")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    # The connection announces itself as this runtime, whatever `--harness` says.
    # Registering as a Codex CLI listener is exactly the wrong answer: it is what
    # the app would use to tell a machine running the manager from one running a
    # bare harness, and this run was started as `--harness codex`.
    assert harness.loop_kwargs["runtime_kind"] == "papaya-agent-runtime"
    assert harness.events.connection[0]["runtime_kind"] == "papaya-agent-runtime"
    # And the known passthrough flags were applied, not merely tolerated.
    assert harness.loop_kwargs["working_directory"] == str(client_home.work_dir)

    reserved_by = {session for _subject, session in harness.events.reserves}
    assert harness.events.reserves[0][0] == SUBJECT
    assert len(reserved_by) == 1, "the renewals used a different session than the acquire"
    assert harness.events.releases == [(SUBJECT, harness.loop.session_id, False)]
    assert harness.results[0]["exit_code"] == 0

    conn = init_db()
    task = papaya_events.find_existing_task(conn, "papaya:event:101")
    assert task is not None, "no task was recorded for the event"
    assert task["title"] == "Fix the thing"
    assert store.task_phase(conn, int(task["id"])) == serve.PHASE_RELEASED
    conn.close()


def test_an_event_whose_repository_cannot_be_resolved_is_declined(
    ppy_home, client_home, ready, monkeypatch
) -> None:
    """Declining is the opposite of taking work on, so it leaves no task behind."""
    monkeypatch.setattr(
        papaya_events.solicit,
        "ensure",
        _raise(solicit.SolicitError("acme/runtime is not in any account you belong to")),
    )
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = asyncio.create_task(
            serve.run(
                serve.parse_args(["--working-directory", str(client_home.work_dir)]),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                extra=harness.extra(),
            )
        )
        await _until(lambda: harness.results, what="the job to be declined")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert harness.results[0]["exit_code"] == 75
    assert "not in any account" in harness.results[0]["output"]
    assert harness.events.releases == [(SUBJECT, harness.loop.session_id, True)]

    conn = init_db()
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    conn.close()


# ── a checkout nobody has ever set up ───────────────────────────────────────


def _start(harness: Harness, *, client_home: ClientHome, stderr=None) -> int:
    """Run `serve` far enough to have set up and listened, then stop it."""
    stderr = io.StringIO() if stderr is None else stderr
    options = serve.parse_args(["--working-directory", str(client_home.work_dir)])

    async def scenario() -> int:
        runner = asyncio.create_task(
            serve.run(options, stdout=io.StringIO(), stderr=stderr, extra=harness.extra())
        )
        await _until(lambda: harness.loop is not None, what="the listener to be built")
        harness.loop.request_stop()
        return await runner

    return asyncio.run(scenario())


def test_a_checkout_that_was_never_set_up_configures_itself_before_it_listens(
    ppy_home, client_home, harnesses, dm
) -> None:
    """Clone the runtime, point the app at it, connect. That is the whole procedure.

    Until this, a checkout nobody had run `ppy setup` in stayed `no_config`
    forever: the connection was live, the listener ran, and the first job started
    a harness in a home with no driver profile, no ceiling and no database.
    """
    from papaya_agent_runtime import memory
    from papaya_agent_runtime.config import load_config
    from papaya_agent_runtime.paths import config_path, db_path

    # Codex first, so list position would answer `codex` and only the connection
    # can answer `claude`.
    harnesses("codex", "claude")
    harness = Harness(FakeEvents([]))
    stderr = io.StringIO()
    assert not config_path().exists()

    assert _start(harness, client_home=client_home, stderr=stderr) == 0

    cfg = load_config()
    # This machine connected as a Claude agent, so both roles are Claude: the
    # runtime does not mix agents behind anybody's back.
    assert (cfg.manager.provider, cfg.worker.provider) == ("claude", "claude")
    assert db_path().exists(), "no state database was created"
    assert memory.repos_root().is_dir() and memory.preferences_path().is_file()
    assert harness.loop is not None, "the listener never went up"
    assert "set this runtime up" in stderr.getvalue()


def test_a_machine_with_no_harness_still_listens_and_dms_what_needs_the_user(
    ppy_home, client_home, harnesses, dm
) -> None:
    """Blocked is not a reason to refuse to start — it is a reason to say so.

    A manager that would not come up until somebody signed a harness in would be
    unreachable at exactly the moment they wanted to be told to.
    """
    from papaya_agent_runtime.paths import config_path

    harnesses()
    harness = Harness(FakeEvents([]))
    stderr = io.StringIO()

    assert _start(harness, client_home=client_home, stderr=stderr) == 0

    assert harness.loop is not None, "a blocked runtime refused to start"
    assert not config_path().exists(), "setup wrote a config with no harness to run it"
    verdict = readiness.check()
    assert verdict.state == readiness.BLOCKED
    assert "no_harness" in [p.code for p in verdict.blockers]
    assert "ppy serve: cannot take work" in stderr.getvalue()

    # One message, in the DM rather than the team channel, addressed to the agent
    # this machine is connected as, naming the thing only a person can close.
    assert len(dm.posts) == 1
    channel_id, text = dm.posts[0]
    assert channel_id == "chan-dm"
    assert "@tester" in text
    assert "Needs you" in text
    assert "no signed-in coding harness" in text


def test_an_unchanged_situation_is_not_dmd_on_every_start(
    ppy_home, client_home, harnesses, dm
) -> None:
    """Otherwise a machine that restarts twice a day teaches its owner to ignore it."""
    harnesses()

    assert _start(Harness(FakeEvents([])), client_home=client_home) == 0
    assert _start(Harness(FakeEvents([])), client_home=client_home) == 0

    assert len(dm.posts) == 1, "the second start said the same thing again"


def test_the_readiness_report_goes_to_a_dm_and_never_to_a_team_channel() -> None:
    """A workspace showing this agent no DM gets silence, not a public post."""
    assert serve.dm_channel_id([{"id": "team", "kind": "channel"}]) is None
    assert serve.dm_channel_id([{"id": "a", "kind": "channel"}, {"id": "b", "type": "DM"}]) == "b"
    assert serve.dm_channel_id(None) is None


def _raise(exc: Exception):
    def boom(*_args: Any, **_kwargs: Any):
        raise exc

    return boom


# ── supervised mode ─────────────────────────────────────────────────────────


class Host(threading.Thread):
    """The other end of the supervised protocol, on the far side of two pipes."""

    def __init__(self, stdout_fd: int, stdin_write_fd: int) -> None:
        super().__init__(daemon=True)
        self._stdout = os.fdopen(stdout_fd, "r")
        self._stdin = stdin_write_fd
        self.messages: list[dict[str, Any]] = []

    def run(self) -> None:
        for line in self._stdout:
            line = line.strip()
            if not line:
                continue
            message = json.loads(line)
            self.messages.append(message)
            if message["type"] == "job.request":
                self._answer(message["job_id"])

    def _answer(self, job_id: str) -> None:
        decision = {"type": "job.decision", "job_id": job_id, "allow": True}
        os.write(self._stdin, (json.dumps(decision) + "\n").encode("utf-8"))

    def of_type(self, name: str) -> list[dict[str, Any]]:
        return [message for message in self.messages if message["type"] == name]


def test_supervised_over_a_pipe_says_hello_asks_and_reports_the_outcome(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """A host sees exactly what it sees today, plus which runtime answered."""
    from papaya_agent_runtime import __version__

    stdout_read, stdout_write = os.pipe()
    stdin_read, stdin_write = os.pipe()
    stdout = os.fdopen(stdout_write, "w", buffering=1)
    host = Host(stdout_read, stdin_write)
    host.start()

    harness = Harness(FakeEvents([EVENT]))
    options = serve.parse_args(
        [
            "--supervised",
            "--harness",
            "codex",
            "--working-directory",
            str(client_home.work_dir),
            "--approval-timeout",
            "30",
        ]
    )
    extra = {**harness.extra(), "stdin_fd": stdin_read}

    async def scenario() -> int:
        runner = asyncio.create_task(
            serve.run(options, stdout=stdout, stderr=io.StringIO(), extra=extra)
        )
        await _until(lambda: host.of_type("job.started"), what="the approved job to start")
        await _until(lambda: harness.jobs, what="the runner to take the ticket")
        # A hand back from the app is how a host takes a held ticket away again.
        await harness.loop.hand_back(SUBJECT)
        await _until(lambda: host.of_type("job.finished"), what="the job to be reported finished")
        # Closing the host's end of stdin is how a real host stops a supervised
        # run, and it is answered while the loop is still alive to answer it.
        os.close(stdin_write)
        return await runner

    try:
        assert asyncio.run(scenario()) == 0
    finally:
        stdout.close()
        host.join(timeout=scale(5.0))

    hello = host.of_type("hello")[0]
    assert hello["runtime"] == {"name": "papaya-agent-runtime", "version": __version__}
    assert hello["protocol"] == 1

    request = host.of_type("job.request")[0]
    assert request["event"]["work_item_id"] == "item-9"
    # `--harness` still names the bundled harness a host sees, even though the
    # runtime label sent to Papaya is this runtime's own.
    assert request["harness"]["key"] == "codex"
    assert request["harness"]["runtime_kind"] == "papaya-agent-runtime"
    assert host.of_type("job.started")[0]["job_id"] == request["job_id"]
    # `Job.report_progress` (client 0.15.1) reaches a supervised host as
    # `job.progress`, so the hold says what it is doing rather than going quiet.
    progress = host.of_type("job.progress")[0]
    assert progress["job_id"] == request["job_id"]
    assert progress["phase"] == serve.PHASE_PICKED_UP
    assert host.of_type("job.finished")[0]["outcome"] == "handed_back"

    conn = init_db()
    task = papaya_events.find_existing_task(conn, "papaya:event:101")
    assert store.task_phase(conn, int(task["id"])) == serve.PHASE_HANDED_BACK
    conn.close()


# ── the lease identity across restarts ──────────────────────────────────────


def test_a_second_start_reuses_the_persisted_session_id(ppy_home, client_home, ready) -> None:
    """A fresh id per start would make a restart race the leases it already holds."""
    first = serve.session_id_for(CONNECTION_ID)

    assert serve.stored_session_ids() == {CONNECTION_ID: first}
    assert serve.session_id_for(CONNECTION_ID) == first
    # Another connection on the same machine is a different lease identity.
    assert serve.session_id_for("conn-2") != first

    harness = Harness(FakeEvents([]))

    async def start() -> int:
        runner = asyncio.create_task(
            serve.run(
                serve.parse_args(["--working-directory", str(client_home.work_dir)]),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                extra=harness.extra(),
            )
        )
        await _until(lambda: harness.loop is not None, what="the listener to be built")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(start()) == 0
    assert harness.loop_kwargs["session_id"] == first


# ── arguments ───────────────────────────────────────────────────────────────


def test_unknown_listen_flags_are_ignored_with_a_warning_and_known_ones_are_read() -> None:
    options = serve.parse_args(
        [
            "--supervised",
            "--harness",
            "codex",
            "--approval-timeout",
            "12",
            "--working-directory",
            "/tmp",
            "--match",
            "backend",
            "--match",
            "urgent",
            "--resume",
            "--run",
            "claude -p {prompt}",
        ]
    )

    assert options.supervised is True
    assert options.harness == "codex"
    assert options.approval_timeout == 12.0
    assert options.working_directory == "/tmp"
    assert options.invalid_arguments is None
    # One line each, and a flag given twice is still one line.
    assert options.ignored == ("--match", "--resume", "--run")


def test_a_bad_argument_is_reported_rather_than_crashing_the_process() -> None:
    """Unsupervised there is no protocol to say it on, so it is stderr and exit 2."""
    stderr = io.StringIO()

    assert serve.serve(["--match", "backend", "--harness", "nope"], stderr=stderr) == 2

    lines = stderr.getvalue().splitlines()
    assert any("ignoring --match" in line for line in lines)
    assert any("--harness must be one of claude-code, codex" in line for line in lines)


def test_a_bad_argument_under_supervision_is_carried_to_the_protocol() -> None:
    options = serve.parse_args(["--supervised", "--approval-timeout", "soon"])

    assert options.supervised is True
    assert options.invalid_arguments is not None
    assert "--approval-timeout" in options.invalid_arguments


# ── one manager per PPY_HOME ────────────────────────────────────────────────


def test_it_refuses_to_start_when_another_process_owns_this_home(ppy_home) -> None:
    from papaya_agent_runtime.supervisor.server import SupervisorServer

    owner = SupervisorServer()
    owner.start_background()
    stderr = io.StringIO()
    try:
        assert serve.serve([], stderr=stderr) == 1
    finally:
        owner.stop()

    assert "one supervisor per PPY_HOME is supported" in stderr.getvalue()


# ── phases ──────────────────────────────────────────────────────────────────


def test_every_way_a_hold_ends_has_a_phase() -> None:
    from papaya_agent_client.listener import STOP_HANDED_BACK, STOP_STALLED

    assert serve.phase_for_stop(STOP_HANDED_BACK) == serve.PHASE_HANDED_BACK
    assert serve.phase_for_stop(STOP_STALLED) == serve.PHASE_STALLED
    assert serve.phase_for_stop(None) == serve.PHASE_RELEASED
    assert set(store.TASK_PHASES) == {
        serve.PHASE_PICKED_UP,
        serve.PHASE_RELEASED,
        serve.PHASE_HANDED_BACK,
        serve.PHASE_STALLED,
        serve.PHASE_DECLINED,
    }
