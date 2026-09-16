"""`ppy serve`: picking a ticket up and working it through its phases to the end.

Every test here drives the *client's own loop* with a fake events API, the way
the client's `tests/test_embed.py` does, rather than a stand-in for it. That is
the point of the whole design: the cursor, the acquire-or-extend reserve, the
renewal cadence and the supervised protocol are the client's, and a test that
faked them would be testing a copy nobody ships.

What *is* faked is everything outside this process: the manager harness (a
:class:`FakeTurns` that does to the ledger what a real turn's `ppy` calls would),
Papaya's HTTP API (a :class:`FakePapaya` opener) and the worker pool. The ledger
itself is real SQLite, and a worker's progress is written the way `ppy progress`
writes it.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from conftest import scale
from papaya_agent_runtime import papaya, papaya_events, progress, prompts, readiness, serve, solicit
from papaya_agent_runtime.config import ManagerProfile, MMConfig, WorkerCeiling
from papaya_agent_runtime.manager.launch import TurnResult, repo_root
from papaya_agent_runtime.state import store
from papaya_agent_runtime.state.db import init_db

CONNECTION_ID = "conn-1"
SUBJECT = "work_item:item-9"


def _assigned(event_id: int, item_id: str, title: str) -> dict[str, Any]:
    return {
        "id": event_id,
        "kind": "work_item.assigned",
        "subject": f"work_item:{item_id}",
        "agent_id": "agent-1",
        "workspace_id": "ws-1",
        # Papaya marks the events it will grant a lease on. Without it the playbook
        # degrades `act` to `acknowledge`, and nothing is ever reserved.
        "reservable": True,
        "payload": {"work_item": {"id": item_id, "title": title, "repo": "acme/runtime"}},
    }


EVENT = _assigned(101, "item-9", "Fix the thing")


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


# ── the world outside the process: the harness, Papaya, the worker ─────────


def _which_turn(prompt: str) -> str:
    """Which of the three turns a prompt is, by its reviewed heading."""
    for turn in prompts.TURNS:
        if prompts.load(turn).splitlines()[0] in prompt:
            return turn
    raise AssertionError(f"not a turn prompt: {prompt[:80]!r}")


@dataclass
class Turn:
    """One launch the fake harness received."""

    name: str
    launch: Any

    @property
    def prompt(self) -> str:
        return self.launch.seed_prompt

    @property
    def run_id(self) -> int:
        return int(self.launch.env[papaya_events.TICKET_RUN_ENV])

    def item(self) -> str:
        for line in self.prompt.splitlines():
            if line.startswith("- work item id: "):
                return line.removeprefix("- work item id: ")
        raise AssertionError("the turn was not told which work item it is for")


class FakeTurns:
    """The manager harness. `act` does to the ledger what the turn's `ppy` calls would.

    A turn that should do nothing (a missed turn) simply leaves `act` a no-op for
    it; the runner has to notice that from the ledger, exactly as it would have to
    for a real session that ended without dispatching.
    """

    def __init__(self, act: Callable[[Turn], None] | None = None) -> None:
        self._act = act or (lambda _turn: None)
        self.calls: list[Turn] = []

    def __call__(self, launch: Any, *, should_stop) -> TurnResult:
        turn = Turn(_which_turn(launch.seed_prompt), launch)
        self.calls.append(turn)
        self._act(turn)
        return TurnResult(exit_code=0, transcript=f"{turn.name} transcript #{len(self.calls)}")

    def names(self) -> list[str]:
        return [turn.name for turn in self.calls]


class _Body:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


class FakePapaya:
    """Papaya's work-item routes, as an `urlopen` stand-in that records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self._lock = threading.Lock()
        #: Bumped by a test to stand for a person replying on the item.
        self.updated_at = "2026-09-16T10:00:00Z"

    def __call__(self, request, timeout):
        body = json.loads(request.data) if request.data else None
        path = urllib.parse.unquote(urllib.parse.urlparse(request.full_url).path)
        with self._lock:
            self.calls.append((request.method, path, body))
        if request.method == "GET":
            item = path.rstrip("/").rsplit("/", 1)[-1]
            record = {"id": item, "repo": "acme/runtime", "updated_at": self.updated_at}
            return _Body(json.dumps(record).encode())
        return _Body(b"")

    @staticmethod
    def _item(path: str) -> str:
        return path.split("/work-items/", 1)[1].split("/", 1)[0]

    def statuses(self) -> list[tuple[str, str]]:
        with self._lock:
            return [
                (self._item(path), body["status"])
                for method, path, body in self.calls
                if method == "PATCH"
            ]

    def comments(self) -> list[tuple[str, str]]:
        with self._lock:
            return [
                (self._item(path), body["body"])
                for method, path, body in self.calls
                if method == "POST" and path.endswith("/comments")
            ]


def _manager_config() -> MMConfig:
    return MMConfig(
        manager=ManagerProfile(provider="claude", model=None, reasoning=None),
        worker=WorkerCeiling(provider="codex", max_model="gpt-5-codex", max_reasoning="medium"),
    )


def _runner(turns: FakeTurns, papaya_api: FakePapaya, *, capacity=None) -> serve.TicketRunner:
    return serve.TicketRunner(
        run_turn=turns,
        config=_manager_config,
        opener=papaya_api,
        worker_capacity=capacity or (lambda: (0, 2)),
        poll_seconds=0.01,
    )


def dispatch_worker(run_id: int, *, repo: str = "runtime") -> int:
    """What `ppy dispatch --run-id` leaves in the ledger: a task and a `dispatched` event."""
    conn = init_db()
    try:
        repo_row = store.get_repo(conn, repo)
        task_id = store.add_task(
            conn, run_id=run_id, title="worker", repo_id=int(repo_row["id"]) if repo_row else None
        )
        store.update_task_fields(conn, task_id, status="in_progress", branch=f"ppy/task-{task_id}")
        store.append_event(
            conn, kind="dispatched", payload={"task_id": task_id}, run_id=run_id, task_id=task_id
        )
        return task_id
    finally:
        conn.close()


def worker_event(task_id: int, kind: str, *, status: str | None = None, **payload: Any) -> None:
    """A worker (or `ppy answer` / `ppy deliver`) changing the ledger."""
    conn = init_db()
    try:
        task = store.get_task(conn, task_id)
        if status is not None:
            store.set_task_status(conn, task_id, status)
        store.append_event(
            conn,
            kind=kind,
            payload={"task_id": task_id, **payload},
            run_id=int(task["run_id"]),
            task_id=task_id,
        )
    finally:
        conn.close()


def workers_in(run_id: int) -> list[int]:
    conn = init_db()
    try:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE run_id = ? AND phase IS NULL ORDER BY id", (run_id,)
        ).fetchall()
        return [int(row["id"]) for row in rows]
    finally:
        conn.close()


def ticket_task(event_id: int = 101) -> Any:
    conn = init_db()
    try:
        return papaya_events.find_existing_task(conn, f"papaya:event:{event_id}")
    finally:
        conn.close()


def history(event_id: int = 101) -> list[str]:
    task = ticket_task(event_id)
    if task is None:
        return []
    conn = init_db()
    try:
        return serve.phase_history(conn, int(task["id"]))
    finally:
        conn.close()


@pytest.fixture
def progress_lines(monkeypatch) -> list[tuple[str, str, str]]:
    """Every `job.report_progress` the runner makes, as `(subject, phase, detail)`."""
    lines: list[tuple[str, str, str]] = []
    real = serve._report_progress

    def spy(job: Any, phase: str, detail: str) -> None:
        lines.append((job.subject, phase, detail))
        real(job, phase, detail)

    monkeypatch.setattr(serve, "_report_progress", spy)
    return lines


# ── terminal mode ───────────────────────────────────────────────────────────


def test_it_picks_up_one_assignment_holds_it_and_releases_it_on_stop(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """The skeleton, still: take the ticket, hold the lease while a worker runs, let go."""
    harness = Harness(FakeEvents([EVENT]))
    turns = FakeTurns(lambda turn: dispatch_worker(turn.run_id))
    options = serve.parse_args(
        ["--harness", "codex", "--working-directory", str(client_home.work_dir)]
    )
    stderr = io.StringIO()

    async def scenario() -> int:
        runner = asyncio.create_task(
            serve.run(
                options,
                stdout=io.StringIO(),
                stderr=stderr,
                extra=harness.extra(),
                runner=_runner(turns, FakePapaya()),
            )
        )
        await _until(lambda: harness.jobs, what="the job to start")
        await _until(lambda: harness.events.reserves, what="the subject to be reserved")
        await _until(
            lambda: serve.PHASE_DISPATCHED in history(), what="the worker to be dispatched"
        )

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
    turns = FakeTurns(lambda turn: dispatch_worker(turn.run_id))

    async def scenario() -> int:
        runner = asyncio.create_task(
            serve.run(
                options,
                stdout=stdout,
                stderr=io.StringIO(),
                extra=extra,
                runner=_runner(turns, FakePapaya()),
            )
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
    # The store's vocabulary and the runner's are one list, in the same order.
    assert store.TASK_PHASES == (
        serve.PHASE_PICKED_UP,
        serve.PHASE_BRIEFING,
        serve.PHASE_DISPATCHED,
        serve.PHASE_BLOCKED,
        serve.PHASE_REVIEWING,
        serve.PHASE_DELIVERING,
        serve.PHASE_REPORTED,
        serve.PHASE_RELEASED,
        serve.PHASE_HANDED_BACK,
        serve.PHASE_STALLED,
        serve.PHASE_DECLINED,
    )


# ── the phase machine ───────────────────────────────────────────────────────


def _serve_ticket(harness: Harness, client_home: ClientHome, runner: serve.TicketRunner):
    """`serve.run` as a task, with a runner whose outside world is fake."""
    return asyncio.create_task(
        serve.run(
            serve.parse_args(["--working-directory", str(client_home.work_dir)]),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            extra=harness.extra(),
            runner=runner,
        )
    )


def _deliver(turn: Turn) -> None:
    """What `ppy review approve` then `ppy deliver` leave in the ledger."""
    (worker,) = workers_in(turn.run_id)
    worker_event(worker, "reviewed", verdict="approved")
    worker_event(
        worker,
        "delivered",
        status="delivered",
        branch=f"ppy/task-{worker}",
        pr_url="https://github.com/acme/runtime/pull/7",
    )


def test_an_assignment_is_briefed_dispatched_watched_reviewed_delivered_and_released(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    """The whole ticket, in order, on the task row, with the worker's progress relayed."""

    def act(turn: Turn) -> None:
        if turn.name == prompts.BRIEF:
            dispatch_worker(turn.run_id)
        elif turn.name == prompts.REVIEW:
            _deliver(turn)

    turns = FakeTurns(act)
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, FakePapaya()))
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        (worker,) = workers_in(int(ticket_task()["run_id"]))
        # A worker reporting the way `ppy progress` does.
        progress.record(worker, phase="plan", note="Add the endpoint behind the flag.")
        progress.record(worker, phase="implement", note="Endpoint and tests written.")
        await _until(
            lambda: len([line for line in progress_lines if "Worker task" in line[2]]) == 2,
            what="both progress reports to be relayed",
        )
        worker_event(worker, "worker_done", status="worker_done", summary="done at abc123")
        await _until(lambda: harness.results, what="the ticket to be released")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert history() == [
        serve.PHASE_PICKED_UP,
        serve.PHASE_BRIEFING,
        serve.PHASE_DISPATCHED,
        serve.PHASE_REVIEWING,
        serve.PHASE_DELIVERING,
        serve.PHASE_REPORTED,
        serve.PHASE_RELEASED,
    ]
    assert turns.names() == [prompts.BRIEF, prompts.REVIEW]
    relayed = [detail for _subject, _phase, detail in progress_lines if "Worker task" in detail]
    assert relayed == [
        f"Worker task {relayed[0].split()[2]} plan: Add the endpoint behind the flag.",
        f"Worker task {relayed[0].split()[2]} implement: Endpoint and tests written.",
    ]
    assert any("pull/7" in detail for _s, phase, detail in progress_lines if phase == "delivering")
    # Released as done: not declined, exit 0.
    assert harness.results[0]["exit_code"] == 0
    assert harness.events.releases == [(SUBJECT, harness.loop.session_id, False)]


def test_the_items_status_follows_the_work_and_nowhere_else(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """`in_progress` on pickup, `review` when the PR is open, `todo` on hand-back. Once each.

    Two tickets in one listener: one goes all the way to a pull request, and one
    whose brief turn never dispatches is handed back.
    """
    delivered, handed_back = "item-9", "item-10"

    def act(turn: Turn) -> None:
        if turn.item() != delivered:
            return
        if turn.name == prompts.BRIEF:
            dispatch_worker(turn.run_id)
        elif turn.name == prompts.REVIEW:
            _deliver(turn)

    papaya_api = FakePapaya()
    turns = FakeTurns(act)
    harness = Harness(
        FakeEvents([EVENT, _assigned(102, handed_back, "Something nobody can place")])
    )

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, papaya_api))
        await _until(lambda: serve.PHASE_DISPATCHED in history(101), what="the dispatch")
        (worker,) = workers_in(int(ticket_task(101)["run_id"]))
        worker_event(worker, "worker_done", status="worker_done", summary="done")
        await _until(lambda: len(harness.results) == 2, what="both tickets to end")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    statuses = papaya_api.statuses()
    assert [s for item, s in statuses if item == delivered] == ["in_progress", "review"]
    assert [s for item, s in statuses if item == handed_back] == ["in_progress", "todo"]
    assert len(statuses) == len(set(statuses)) == 4
    # The one mechanical comment, on hand-back only, naming the reason.
    assert papaya_api.comments() == [
        (
            handed_back,
            "handed back: the manager turn ended 2 times without dispatching a worker; no branch",
        )
    ]


def test_a_blocked_question_runs_the_answer_turn_and_returns_to_dispatched(
    ppy_home, client_home, ready, registered_repo
) -> None:
    def act(turn: Turn) -> None:
        if turn.name == prompts.BRIEF:
            dispatch_worker(turn.run_id)
        elif turn.name == prompts.ANSWER:
            (worker,) = workers_in(turn.run_id)
            # `ppy answer`: the answer event, and the worker resumed.
            worker_event(worker, "answer", status="in_progress", answer="Use the v2 route.")

    turns = FakeTurns(act)
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, FakePapaya()))
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        (worker,) = workers_in(int(ticket_task()["run_id"]))
        worker_event(
            worker, "question", status="blocked", question="Should this use the v1 or v2 route?"
        )
        await _until(
            lambda: history()[-2:] == [serve.PHASE_BLOCKED, serve.PHASE_DISPATCHED],
            what="the ticket to go blocked and come back",
        )
        harness.jobs[0].stop.set()
        await _until(lambda: harness.results, what="the hold to end")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert turns.names() == [prompts.BRIEF, prompts.ANSWER]
    assert "Should this use the v1 or v2 route?" in turns.calls[1].prompt
    assert history()[-3:] == [serve.PHASE_BLOCKED, serve.PHASE_DISPATCHED, serve.PHASE_RELEASED]


def test_a_turn_that_asks_a_person_holds_blocked_until_the_item_changes(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """Layer five: post the candidates, record the wait, and resume on the reply."""
    from papaya_agent_runtime import board

    def act(turn: Turn) -> None:
        if turn.name != prompts.BRIEF:
            return
        if len(turns.calls) == 1:
            # `ppy todo add "..." --task <ticket> --blocked-on user`, after posting.
            board.add(
                "Is this the desktop app or the web app?",
                task_id=int(ticket_task()["id"]),
                blocked_on="user",
            )
        else:
            dispatch_worker(turn.run_id)

    papaya_api = FakePapaya()
    turns = FakeTurns(act)
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, papaya_api))
        await _until(
            lambda: ("item-9", "blocked") in papaya_api.statuses(), what="the wait on a person"
        )
        await asyncio.sleep(0.1)
        assert turns.names() == [prompts.BRIEF], "the turn ran again with nobody having replied"
        assert history()[-1] == serve.PHASE_BLOCKED

        papaya_api.updated_at = "2026-09-16T11:00:00Z"  # someone answered on the item
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        harness.jobs[0].stop.set()
        await _until(lambda: harness.results, what="the hold to end")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert turns.names() == [prompts.BRIEF, prompts.BRIEF]
    assert [status for _item, status in papaya_api.statuses()] == [
        "in_progress",
        "blocked",
        "in_progress",
    ]
    assert history()[:5] == [
        serve.PHASE_PICKED_UP,
        serve.PHASE_BRIEFING,
        serve.PHASE_BLOCKED,
        serve.PHASE_BRIEFING,
        serve.PHASE_DISPATCHED,
    ]
    assert serve.open_person_wait(int(ticket_task()["id"])) is None


def test_a_brief_turn_that_dispatches_nothing_is_retried_once_then_declined(
    ppy_home, client_home, ready, registered_repo
) -> None:
    turns = FakeTurns()  # every turn ends without doing anything
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, FakePapaya()))
        await _until(lambda: harness.results, what="the ticket to be handed back")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert turns.names() == [prompts.BRIEF, prompts.BRIEF]
    # The retry was given the tail of the first attempt's transcript.
    assert "brief transcript #1" in turns.calls[1].prompt
    assert "brief transcript #1" not in turns.calls[0].prompt

    result = harness.results[0]
    assert result["exit_code"] == 75
    assert "without dispatching a worker" in result["output"]
    assert harness.events.releases == [(SUBJECT, harness.loop.session_id, True)]

    task = ticket_task()
    assert store.task_phase(init_db(), int(task["id"])) == serve.PHASE_DECLINED
    # No second task row: the ticket's own, and no worker beside it.
    assert init_db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_a_full_worker_pool_keeps_the_ticket_dispatched_and_held(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    """Capacity refuses a dispatch before any row exists; that is a wait, not a miss."""
    turns = FakeTurns()  # dispatch is refused: the pool is full
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = _serve_ticket(
            harness, client_home, _runner(turns, FakePapaya(), capacity=lambda: (2, 2))
        )
        await _until(
            lambda: any("Waiting for a worker slot" in line[2] for line in progress_lines),
            what="the wait for a slot to be reported",
        )
        await _until(lambda: harness.events.reserves, what="the subject to be reserved")
        harness.ticks.tick()
        await _until(lambda: harness.ticks.count >= 1, what="the first renewal")
        harness.ticks.tick()
        await _until(lambda: len(harness.events.reserves) >= 3, what="two renewals")

        # Two renewals later: still held, still `dispatched`, never handed back.
        assert not harness.results
        assert history()[-1] == serve.PHASE_DISPATCHED
        assert turns.names() == [prompts.BRIEF]
        assert harness.events.releases == []

        harness.jobs[0].stop.set()
        await _until(lambda: harness.results, what="the hold to end")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0
    assert harness.results[0]["exit_code"] == 0
    assert harness.events.releases == [(SUBJECT, harness.loop.session_id, False)]
    assert "Waiting for a worker slot: 2 of 2 workers are busy." in [
        detail for _s, phase, detail in progress_lines if phase == serve.PHASE_DISPATCHED
    ]


def test_a_ticket_found_in_reviewing_runs_the_review_turn_without_a_new_pickup(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """A restarted `serve` goes back to where the ticket was, not to the beginning."""
    conn = init_db()
    event = papaya_events.PapayaEvent(
        id="101",
        kind="work_item.assigned",
        subject=SUBJECT,
        payload=EVENT["payload"],
        work_item_id="item-9",
    )
    run_id = store.create_run(conn, "Fix the thing")
    task_id = store.add_task(conn, run_id=run_id, title="Fix the thing")
    papaya_events.record_task(conn, task_id, event)
    for phase in (
        serve.PHASE_PICKED_UP,
        serve.PHASE_BRIEFING,
        serve.PHASE_DISPATCHED,
        serve.PHASE_REVIEWING,
    ):
        serve.record_phase(conn, task_id, phase)
    conn.close()
    worker = dispatch_worker(run_id)
    worker_event(worker, "worker_done", status="worker_done", summary="done")

    papaya_api = FakePapaya()
    turns = FakeTurns(lambda turn: _deliver(turn) if turn.name == prompts.REVIEW else None)
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, papaya_api))
        await _until(lambda: harness.results, what="the resumed ticket to finish")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert turns.names() == [prompts.REVIEW]
    phases = history()
    assert phases.count(serve.PHASE_PICKED_UP) == 1, "the ticket was picked up a second time"
    assert phases[4:] == [
        serve.PHASE_REVIEWING,
        serve.PHASE_DELIVERING,
        serve.PHASE_REPORTED,
        serve.PHASE_RELEASED,
    ]
    # Nothing was picked up, so nothing was moved to `in_progress` again.
    assert [status for _item, status in papaya_api.statuses()] == ["review"]


def test_manager_turns_get_the_jobs_environment_and_only_the_runtime_directory(
    ppy_home, client_home, ready, registered_repo
) -> None:
    turns = FakeTurns(lambda turn: dispatch_worker(turn.run_id))
    harness = Harness(FakeEvents([EVENT]))
    job_env: dict[str, str] = {}

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, FakePapaya()))
        await _until(lambda: turns.calls, what="the brief turn to launch")
        job_env.update(harness.jobs[0].env)
        harness.jobs[0].stop.set()
        await _until(lambda: harness.results, what="the hold to end")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    runtime_dir = str(Path(repo_root()).resolve())
    launch = turns.calls[0].launch
    assert launch.cwd == runtime_dir
    # The client's job environment, as a harness session would have been given it.
    for key in (
        "PAPAYA_CONTEXT_FILE",
        "PAPAYA_EVENT_FILE",
        "PAPAYA_DECLINE_FILE",
        "PAPAYA_JOB_ACTIVITY_FILE",
        "PAPAYA_AGENT_TOKEN",
        "PAPAYA_WORKSPACE_ID",
        "PAPAYA_API_URL",
        "PAPAYA_SUBJECT",
    ):
        assert launch.env[key] == job_env[key], key
    if "PAPAYA_PLUGIN_DIR" in job_env:
        assert launch.env["PAPAYA_PLUGIN_DIR"] == job_env["PAPAYA_PLUGIN_DIR"]
    # The write boundary is the runtime directory and nothing else.
    assert json.loads(launch.env["PAPAYA_ALLOWED_WORKING_DIRECTORIES"]) == [runtime_dir]
    assert launch.env["PAPAYA_WORKING_DIRECTORY"] == runtime_dir
    assert launch.env[papaya_events.TICKET_RUN_ENV] == str(ticket_task()["run_id"])
    # Launched through `ppy start`'s own builder, headless.
    assert launch.argv[:2] == ["claude", "-p"]
    assert launch.argv[2] == launch.seed_prompt
    assert f"{runtime_dir}/.agents/skills/brief-a-worker/SKILL.md" in launch.seed_prompt


def test_a_dispatch_from_a_manager_turn_defaults_into_the_tickets_run(monkeypatch) -> None:
    """The link from worker to ticket does not rest on a turn remembering `--run-id`."""
    from papaya_agent_runtime import cli

    monkeypatch.delenv(papaya_events.TICKET_RUN_ENV, raising=False)
    assert cli._ticket_run_id() is None
    monkeypatch.setenv(papaya_events.TICKET_RUN_ENV, "42")
    assert cli._ticket_run_id() == 42
    monkeypatch.setenv(papaya_events.TICKET_RUN_ENV, "not-a-run")
    assert cli._ticket_run_id() is None


# ── the prompts ─────────────────────────────────────────────────────────────


def test_the_turn_prompts_instruct_and_never_template_a_brief() -> None:
    """Reviewed text that points at the skills; no generated Goals, done or brief."""
    for turn in prompts.TURNS:
        text = prompts.load(turn)
        assert prompts.BRIEF_SKILL in text, f"{turn} does not name brief-a-worker"
        assert prompts.REVIEW_SKILL in text, f"{turn} does not name review-a-worker"
        # The only placeholder is where the runtime lives.
        assert text.count("{") == text.count(prompts.RUNTIME_DIR)
        # A brief's own sections are the turn's to write, so none may appear here.
        headings = {
            line.lstrip("#").strip().lower() for line in text.splitlines() if line.startswith("#")
        }
        generated = {
            "goals",
            "intent",
            "in scope",
            "out of scope",
            "acceptance criteria",
            "definition of done",
            "verification",
        }
        assert not headings & generated, (turn, headings & generated)
        assert "acceptance criteria:" not in text.lower()


def test_the_brief_prompt_names_the_six_repository_layers_in_order_and_forbids_guessing() -> None:
    text = prompts.load(prompts.BRIEF)
    layers = [
        "**The item names it.**",
        "**You already know.**",
        "**The code says.** `ppy repo locate",
        "**Nothing registered fits.** `ppy repo discover`",
        "**Still unsure.**",
        "**Once placed.**",
    ]
    positions = [text.index(layer) for layer in layers]
    assert positions == sorted(positions)
    for number, layer in enumerate(layers, start=1):
        assert f"{number}. {layer}" in text
    assert "Never guess." in text
    assert "ppy dispatch --repo <repo> --brief <path> --strict" in text


def test_a_rendered_prompt_resolves_the_skills_and_appends_only_facts(tmp_path) -> None:
    rendered = prompts.render(
        prompts.ANSWER,
        runtime_dir=tmp_path,
        facts={
            "work item id": "item-9",
            "repository": None,
            "the worker's question": "Which route?\nv1 or v2?",
        },
    )
    assert prompts.RUNTIME_DIR not in rendered
    assert f"{tmp_path.resolve()}/.agents/skills/review-a-worker/SKILL.md" in rendered
    tail = rendered.split("## This ticket", 1)[1]
    assert "- work item id: item-9" in tail
    assert "repository" not in tail
    assert "```\nWhich route?\nv1 or v2?\n```" in tail
