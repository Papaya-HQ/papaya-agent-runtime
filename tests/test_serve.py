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
import dataclasses
import io
import json
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from conftest import make_git_repo, scale, timed_out
from papaya_agent_runtime import (
    gate,
    papaya,
    papaya_events,
    progress,
    prompts,
    readiness,
    serve,
    solicit,
)
from papaya_agent_runtime.config import ManagerProfile, MMConfig, WorkerCeiling
from papaya_agent_runtime.manager.launch import TurnResult, TurnTools, repo_root
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

    # The shape `GET /workspaces/{id}/channels` answers an agent token with: public
    # channels, plus the ones this agent is a member of — a person-to-person `dm`
    # it was added to, and its own DM with its owner, `agent_private`.
    fake = FakeDM(
        channels=[
            {"id": "chan-team", "channel_type": "public", "name": "engineering"},
            {"id": "chan-dm", "channel_type": "dm", "name": "dm:user-1:user-2"},
            {"id": "chan-agent-dm", "channel_type": "agent_private", "name": "agent-dm:u:a"},
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
        #: Subjects another session holds: reserving one is Papaya's 409.
        self.held: set[str] = set()
        #: Subjects Papaya kept with the agent in Papaya: a 409 naming no machine here.
        self.not_routed: set[str] = set()

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
        if subject in self.held:
            from papaya_agent_client.api_client import SubjectHeld

            raise SubjectHeld(
                subject, {"connection_id": "conn-other", "session_id": "sess-other"}, None
            )
        if subject in self.not_routed:
            from papaya_agent_client.api_client import SubjectHeld

            holder = {
                "connection_id": "papaya-hosted",
                "connection_name": "Engineering Agent in Papaya",
                "session_id": "not-routed-to-this-machine",
            }
            raise SubjectHeld(subject, holder, None)
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
    raise timed_out(what)


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

    def __init__(self, act: Callable[[Turn], str | None] | None = None) -> None:
        self._act = act or (lambda _turn: None)
        self.calls: list[Turn] = []

    def __call__(self, launch: Any, *, should_stop, transcript_path=None) -> TurnResult:
        turn = Turn(_which_turn(launch.seed_prompt), launch)
        self.calls.append(turn)
        # An act may return the turn's last message, which ends its transcript.
        said = self._act(turn)
        transcript = f"{turn.name} transcript #{len(self.calls)}"
        return TurnResult(exit_code=0, transcript=f"{transcript}\n{said}" if said else transcript)

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
    """Papaya's work-item routes, as an `urlopen` stand-in that records every call.

    Comments are kept the way Papaya keeps them — a list per item, each with an id
    and an author — because the runner now reads them back to check a turn's work.
    Everything posted through a job's agent token is written by the agent.
    """

    #: The instance a test made last, so a fake turn can post "through MCP" to it.
    latest: FakePapaya | None = None

    def __init__(self, *, acceptance_criteria: str | None = "Done when the thing works.") -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self._lock = threading.Lock()
        #: Bumped by a test to stand for a person replying on the item.
        self.updated_at = "2026-09-16T10:00:00Z"
        self.acceptance_criteria = acceptance_criteria
        self.stored: dict[str, list[dict[str, Any]]] = {}
        FakePapaya.latest = self

    def __call__(self, request, timeout):
        body = json.loads(request.data) if request.data else None
        path = urllib.parse.unquote(urllib.parse.urlparse(request.full_url).path)
        with self._lock:
            self.calls.append((request.method, path, body))
            item = self._item(path)
            if path.endswith("/comments"):
                thread = self.stored.setdefault(item, [])
                if request.method == "POST":
                    comment = {
                        "id": f"comment-{len(thread) + 1}",
                        "author_type": "agent",
                        "body": body["body"],
                    }
                    thread.append(comment)
                    return _Body(json.dumps(comment).encode())
                return _Body(json.dumps(thread).encode())
        if request.method == "GET":
            record = {
                "id": item,
                "repo": "acme/runtime",
                "updated_at": self.updated_at,
                "acceptance_criteria": self.acceptance_criteria,
            }
            return _Body(json.dumps(record).encode())
        return _Body(b"")

    def comment_from(
        self, item: str, body: str, *, author_type: str = "user", author_id: str = "user-1"
    ) -> dict[str, Any]:
        """Somebody commenting on the item in the app, not through this agent's token."""
        with self._lock:
            thread = self.stored.setdefault(item, [])
            comment = {
                "id": f"comment-{len(thread) + 1}",
                "author_type": author_type,
                "author_id": author_id,
                "body": body,
            }
            thread.append(comment)
            return comment

    def comment_reads(self) -> int:
        with self._lock:
            return len(
                [
                    1
                    for method, path, _ in self.calls
                    if method == "GET" and path.endswith("/comments")
                ]
            )

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


def _no_tools(_provider: str, _env: dict[str, str], **_kwargs: Any) -> TurnTools:
    """`prepare_turn_tools` without the client subprocess, for tests about something else."""
    return TurnTools()


def _no_steer(task_id: int, message: str) -> None:
    raise AssertionError(f"the runner steered worker task {task_id} unasked: {message}")


def _runner(
    turns: Any,
    papaya_api: FakePapaya,
    *,
    capacity=None,
    turn_tools=_no_tools,
    clock=None,
    steer=_no_steer,
    gate_verdict=None,
    gate_state=None,
    uncommitted=lambda _task_id: [],
    agent_record=lambda _env: None,
    full_suite=lambda _task_id: None,
    review_base=lambda _task_id: "",
) -> serve.TicketRunner:
    from papaya_agent_runtime import rounds

    return serve.TicketRunner(
        # Papaya's agent record is not asked for; a test that needs one passes it.
        agent_record=agent_record,
        full_suite=full_suite,
        # No forge is fetched before a review turn in these tests.
        review_base=review_base,
        # No supervisor answers in these tests; a liveness check never asks the socket.
        gate_state=gate_state or (lambda _task_id: rounds.GateState(False, "no gate running")),
        uncommitted=uncommitted,
        run_turn=turns,
        config=_manager_config,
        opener=papaya_api,
        worker_capacity=capacity or (lambda: (0, 2)),
        poll_seconds=0.01,
        turn_tools=turn_tools,
        clock=clock,
        steer=steer,
        gate_verdict=gate_verdict,
    )


class Clock:
    """The runner's clock for "a minute since the comments were read", moved by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float = serve.COMMENT_POLL_SECONDS) -> None:
        self.now += seconds


def post_as_agent(turn: Turn, body: str) -> None:
    """What a turn's `papaya` MCP comment call leaves on the item: a comment by the agent."""
    papaya_events.post_work_item_comment(
        papaya_events.PapayaEvent(
            id=None, kind="", subject="", payload={}, work_item_id=turn.item()
        ),
        body,
        environ=turn.launch.env,
        opener=FakePapaya.latest,
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
    assert (cfg.worker.max_concurrent, cfg.worker.reconcile_slots) == (3, 1)
    assert "(up to 3 at once, plus 1 for pull-request fixes)" in stderr.getvalue()


@pytest.mark.parametrize("configured", [None, 5])
def test_the_listener_declares_the_configured_worker_count_as_its_subjects(
    ppy_home, client_home, ready, configured
) -> None:
    """Papaya is told the tickets this machine holds at once, and the loop holds that many.

    The loop used to be built without it, so it took the client's own default
    whatever `worker.max_concurrent` said.
    """
    from papaya_agent_runtime.config import load_config, save_config

    if configured is not None:
        cfg = load_config()
        cfg.worker.max_concurrent = configured
        save_config(cfg)
    workers = load_config().worker.max_concurrent
    assert workers == (configured or 3)
    harness = Harness(FakeEvents([]))
    options = serve.parse_args(["--working-directory", str(client_home.work_dir)])

    async def scenario() -> int:
        runner = asyncio.create_task(
            serve.run(options, stdout=io.StringIO(), stderr=io.StringIO(), extra=harness.extra())
        )
        await _until(lambda: harness.events.connection, what="the capabilities to be announced")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert harness.events.connection[0]["capabilities"]["max_concurrent_subjects"] == workers
    assert harness.loop.max_concurrent == workers


def _listen_briefly(harness: Harness, client_home: ClientHome) -> int:
    """Run `serve` until its connection has announced itself, then stop it."""
    options = serve.parse_args(["--working-directory", str(client_home.work_dir)])

    async def scenario() -> int:
        runner = asyncio.create_task(
            serve.run(options, stdout=io.StringIO(), stderr=io.StringIO(), extra=harness.extra())
        )
        await _until(lambda: harness.events.connection, what="the capabilities to be announced")
        harness.loop.request_stop()
        return await runner

    return asyncio.run(scenario())


def test_a_client_that_takes_extra_capabilities_is_told_this_runtime_answers_questions(
    ppy_home, client_home, ready, monkeypatch, caplog
) -> None:
    """Papaya routes a no-card question only to a connection that says it takes `ask`.

    This runtime answers one read-only, so it says so, next to `work`, whenever the
    client has a way to register it.
    """
    from papaya_agent_client import embed

    original = embed.build_listener
    registered: list[Any] = []

    async def build_listener(*, extra_capabilities: dict[str, Any] | None = None, **kwargs):
        registered.append(extra_capabilities)
        return await original(**kwargs)

    monkeypatch.setattr(embed, "build_listener", build_listener)
    harness = Harness(FakeEvents([]))
    caplog.set_level(logging.WARNING, logger="papaya_agent_runtime.serve")

    assert _listen_briefly(harness, client_home) == 0

    assert registered == [{"instruction_intents": ["ask", "work"]}]
    assert serve.OLD_CLIENT_CAPABILITIES not in caplog.text


def test_a_client_with_no_extra_capabilities_still_listens_and_says_questions_are_refused(
    ppy_home, client_home, ready, monkeypatch, caplog
) -> None:
    """A client older than the parameter would raise on it: nothing new is passed.

    The machine still starts and still does work; one line says why Papaya will not
    send it questions yet.
    """
    from papaya_agent_client import embed

    original = embed.build_listener
    passed: list[dict[str, Any]] = []

    async def build_listener(**kwargs):
        passed.append(kwargs)
        return await original(**kwargs)

    monkeypatch.setattr(embed, "build_listener", build_listener)
    harness = Harness(FakeEvents([]))
    caplog.set_level(logging.WARNING, logger="papaya_agent_runtime.serve")

    assert _listen_briefly(harness, client_home) == 0

    assert len(passed) == 1 and serve.EXTRA_CAPABILITIES not in passed[0]
    assert "instruction_intents" not in harness.events.connection[0]["capabilities"]
    said = [r for r in caplog.records if r.getMessage() == serve.OLD_CLIENT_CAPABILITIES]
    assert len(said) == 1
    assert "refuse to send questions" in said[0].getMessage()


def test_extra_capabilities_are_offered_to_either_builder_only_when_it_names_them(
    caplog,
) -> None:
    """Supervised or not, the check is the builder's own signature, never its version."""

    async def supervised(writer, *, stdin_fd=None, extra_capabilities=None, runner=None):
        return None

    async def old_supervised(writer, *, stdin_fd=None, runner=None):
        return None

    caplog.set_level(logging.WARNING, logger="papaya_agent_runtime.serve")

    assert serve.extra_capabilities(supervised) == {
        "extra_capabilities": {"instruction_intents": ["ask", "work"]}
    }
    assert caplog.records == []
    assert serve.extra_capabilities(old_supervised) == {}
    assert [r.getMessage() for r in caplog.records] == [serve.OLD_CLIENT_CAPABILITIES]


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
    assert channel_id == "chan-agent-dm", "not the agent's own DM with its owner"
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
    # The agent's own DM with a person wins over a person-to-person DM it is in.
    channels = [
        {"id": "human", "channel_type": "dm"},
        {"id": "agent", "channel_type": "agent_private"},
    ]
    assert serve.dm_channel_id(channels) == "agent"


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
    # A machine with nothing a person has to do says so with an empty list.
    assert hello["runtime"] == {
        "name": "papaya-agent-runtime",
        "version": __version__,
        "blockers": [],
    }
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


def test_it_refuses_to_start_when_a_supervisor_will_not_let_go(ppy_home) -> None:
    """Adopting and retiring are `tests/test_serve_takeover.py`; this is the last resort."""
    from papaya_agent_runtime import takeover
    from papaya_agent_runtime.supervisor.server import SupervisorServer

    owner = SupervisorServer()
    owner.start_background()
    # A holder from no recorded build that ignores `shutdown` and cannot be signalled.
    owner.request_shutdown = lambda: None  # type: ignore[method-assign]
    takeover.remove_record(str(ppy_home.resolve()), os.getpid())
    stderr = io.StringIO()
    try:
        status = serve.serve(
            [],
            stderr=stderr,
            takeover_seams={"grace": 0.2, "shutdown": lambda _path: False},
        )
    finally:
        owner.stop()

    assert status == takeover.EXIT_CANNOT_START
    (line,) = [line for line in stderr.getvalue().splitlines() if line.strip()]
    assert line.startswith("ppy serve: cannot start:") and "still holds" in line
    record = takeover.start_failure(str(ppy_home.resolve()))
    assert record is not None and record["line"] in line


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
        serve.PHASE_HANDED_OVER,
        serve.PHASE_DONE,
        serve.PHASE_NEEDS_A_PERSON,
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


REPORT = "Approved and delivered: the thing works now, with tests."


def _deliver(turn: Turn, *, report: bool = True) -> None:
    """What `ppy review approve`, `ppy deliver` and the turn's MCP report leave behind."""
    if report:
        post_as_agent(turn, REPORT)
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
    # The ticket that could not be placed says it was picked up and is briefing, and
    # why it was handed back — the pickup line once, however many attempts it took.
    assert [body for item, body in papaya_api.comments() if item == handed_back] == [
        "Picked up; choosing the repository and writing the brief.",
        "handed back: the manager turn ended 2 times without dispatching a worker; no branch",
    ]


def test_a_brief_turn_with_nothing_to_build_ends_the_hold_without_a_hand_back(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """Shane, 2026-09-18: finished items were handed back as "didn't run this" and reset
    to `todo`, again on every restart. Nothing to build is an ending, not a miss."""
    item = "item-10"

    def act(turn: Turn) -> str | None:
        if turn.name == prompts.BRIEF:
            return "Checked the item.\n\nNOTHING TO BUILD: PR #729 merged and QA passed it"
        return None

    papaya_api = FakePapaya()
    turns = FakeTurns(act)
    harness = Harness(FakeEvents([_assigned(102, item, "Newest first sort")]))

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, papaya_api))
        await _until(lambda: len(harness.results) == 1, what="the ticket to end")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0
    assert turns.names() == [prompts.BRIEF]
    assert [s for i, s in papaya_api.statuses() if i == item] == ["in_progress"]
    assert [b for i, b in papaya_api.comments() if i == item] == [
        "Picked up; choosing the repository and writing the brief."
    ]
    phases = history(102)
    assert serve.PHASE_REPORTED in phases and serve.PHASE_DECLINED not in phases
    conn = init_db()
    try:
        assert serve.resumable_phase(conn, int(ticket_task(102)["id"])) is None
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("transcript", "reason"),
    [
        ("NOTHING TO BUILD: merged in #729", "merged in #729"),
        ("looked\n**NOTHING TO BUILD: waits on a merge**", "waits on a merge"),
        ("There is NOTHING TO BUILD: here, maybe.", None),
        ("", None),
    ],
)
def test_nothing_to_build_reads_its_line(transcript: str, reason: str | None) -> None:
    assert serve.nothing_to_build(TurnResult(exit_code=0, transcript=transcript)) == reason


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


# ── listening to the ticket while the work is in flight ─────────────────────


def _brief_dispatches(turn: Turn) -> None:
    if turn.name == prompts.BRIEF:
        dispatch_worker(turn.run_id)


def handled_comment(event_id: int = 101) -> str | None:
    record = serve.last_handled_comment(int(ticket_task(event_id)["id"]))
    return record.get("comment_id") if record is not None else None


async def _end_hold(harness: Harness, runner: asyncio.Task) -> int:
    harness.jobs[0].stop.set()
    await _until(lambda: harness.results, what="the hold to end")
    harness.loop.request_stop()
    return await runner


def test_a_persons_comment_while_dispatched_runs_the_answer_turn_within_a_minute(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    """PAP-213: the manager asked on the ticket, the person answered, and nobody heard."""
    papaya_api, clock = FakePapaya(), Clock()
    turns = FakeTurns(_brief_dispatches)
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, papaya_api, clock=clock))
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        # Read at the brief turn's launch, and again the moment it ended.
        await _until(lambda: papaya_api.comment_reads() >= 2, what="the read after the turn")
        comment = papaya_api.comment_from("item-9", "Read the image and attach it.")

        clock.advance(serve.COMMENT_POLL_SECONDS - 1)
        await asyncio.sleep(scale(0.1))
        assert turns.names() == [prompts.BRIEF], "read before the minute was up"

        clock.advance(1)
        await _until(lambda: len(turns.calls) == 2, what="the answer turn")
        assert handled_comment() == comment["id"]
        return await _end_hold(harness, runner)

    assert asyncio.run(scenario()) == 0

    assert turns.names() == [prompts.BRIEF, prompts.ANSWER]
    assert "Read the image and attach it." in turns.calls[1].prompt
    answering = [detail for _s, _p, detail in progress_lines if detail.startswith("Answering")]
    assert answering == ["Answering a comment from user user-1"]
    # The comment turn is not a phase change: the ticket stayed dispatched throughout.
    assert history()[-2:] == [serve.PHASE_DISPATCHED, serve.PHASE_RELEASED]


def test_the_agents_own_comment_on_the_ticket_wakes_nothing(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    papaya_api, clock = FakePapaya(), Clock()
    turns = FakeTurns(_brief_dispatches)
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, papaya_api, clock=clock))
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        await _until(lambda: papaya_api.comment_reads() >= 2, what="the read after the turn")
        # A phase line, written by this agent (`agent-1` is the connected agent).
        papaya_api.comment_from(
            "item-9", "Worker task 2 is still going.", author_type="agent", author_id="agent-1"
        )
        for reads in (3, 4):
            clock.advance()
            await _until(lambda n=reads: papaya_api.comment_reads() >= n, what="another read")
        await asyncio.sleep(scale(0.05))
        return await _end_hold(harness, runner)

    assert asyncio.run(scenario()) == 0

    assert turns.names() == [prompts.BRIEF]
    assert not [detail for _s, _p, detail in progress_lines if detail.startswith("Answering")]


def test_comments_made_during_a_turn_are_answered_after_it_in_one_turn(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    papaya_api, clock = FakePapaya(), Clock()

    def act(turn: Turn) -> None:
        if turn.name == prompts.BRIEF:
            # The person answers twice while the brief turn is still running.
            papaya_api.comment_from("item-9", "Read the image.")
            papaya_api.comment_from("item-9", "And attach it too.")
            dispatch_worker(turn.run_id)

    turns = FakeTurns(act)
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, papaya_api, clock=clock))
        await _until(lambda: len(turns.calls) == 2, what="the answer turn")
        reads = papaya_api.comment_reads()
        for _ in range(2):
            clock.advance()
            reads += 1
            await _until(lambda n=reads: papaya_api.comment_reads() >= n, what="another read")
        await asyncio.sleep(scale(0.05))
        return await _end_hold(harness, runner)

    assert asyncio.run(scenario()) == 0

    assert turns.names() == [prompts.BRIEF, prompts.ANSWER], "each comment was answered once"
    answer = turns.calls[1].prompt
    assert "Read the image." in answer and "And attach it too." in answer
    answering = [detail for _s, _p, detail in progress_lines if detail.startswith("Answering")]
    assert len(answering) == 2


def test_a_restart_answers_only_comments_newer_than_the_last_one_handled(
    ppy_home, client_home, ready, registered_repo
) -> None:
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
    for phase in (serve.PHASE_PICKED_UP, serve.PHASE_BRIEFING, serve.PHASE_DISPATCHED):
        serve.record_phase(conn, task_id, phase)
    conn.close()
    dispatch_worker(run_id)

    papaya_api, clock = FakePapaya(), Clock()
    papaya_api.comment_from("item-9", "An old question, answered before the restart.")
    handled = papaya_api.comment_from("item-9", "The last one the previous serve handled.")
    serve.record_comment_handled(task_id, handled)

    turns = FakeTurns()
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, papaya_api, clock=clock))
        await _until(lambda: papaya_api.comment_reads() >= 1, what="the first read")
        await asyncio.sleep(scale(0.05))
        assert turns.names() == [], "an old comment was answered again"

        papaya_api.comment_from("item-9", "A new reply, after the restart.")
        clock.advance()
        await _until(lambda: len(turns.calls) == 1, what="the answer turn")
        return await _end_hold(harness, runner)

    assert asyncio.run(scenario()) == 0

    assert turns.names() == [prompts.ANSWER]
    prompt = turns.calls[0].prompt
    assert "A new reply, after the restart." in prompt
    assert "old question" not in prompt and "previous serve" not in prompt
    assert handled_comment() == "comment-3"


def test_what_counts_as_new_and_as_somebody_else() -> None:
    old = {"id": "c1", "created_at": "2026-09-16T09:34:49Z", "author_type": "agent"}
    reply = {"id": "c2", "created_at": "2026-09-16T09:38:23Z", "author_type": "user"}
    comments = [old, reply]
    assert serve.comments_after(comments, {"comment_id": "c1"}) == [reply]
    assert serve.comments_after(comments, {"comment_id": None}) == comments
    # The handled comment is gone: its timestamp still places the record.
    gone = {"comment_id": "c0", "created_at": "2026-09-16T09:35:00+00:00"}
    assert serve.comments_after(comments, gone) == [reply]
    assert serve.comments_after(comments, {"comment_id": "c0"}) is None

    assert serve.is_own_comment({"author_type": "agent", "author_id": "agent-1"}, "agent-1")
    assert serve.is_own_comment({"author_type": "agent"}, "agent-1")
    assert not serve.is_own_comment({"author_type": "agent", "author_id": "agent-2"}, "agent-1")
    assert not serve.is_own_comment(reply, "agent-1")


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


# ── a turn is the agent: tools, transcript, and what the record shows ──────


def _one_ticket(harness: Harness, client_home: ClientHome, runner: serve.TicketRunner) -> int:
    """Serve until the one ticket in `harness` ends, then stop."""

    async def scenario() -> int:
        task = _serve_ticket(harness, client_home, runner)
        await _until(lambda: harness.results, what="the ticket to end")
        harness.loop.request_stop()
        return await task

    return asyncio.run(scenario())


def _dispatch_then_finish(turn: Turn) -> None:
    """A brief that dispatches a worker which is done at once."""
    worker = dispatch_worker(turn.run_id)
    worker_event(worker, "worker_done", status="worker_done", summary="done at abc123")


def test_a_turn_is_launched_with_the_agents_mcp_server_and_plugin(
    ppy_home, client_home, ready, registered_repo, tmp_path, monkeypatch
) -> None:
    """The first real run's turns had no MCP at all, so every Papaya call was refused.

    The config comes from the client itself (`PAPAYA_AGENT_BIN mcp runner-config`),
    exactly as its bundled Claude runner gets it, and the session is strict about it.
    """
    calls = tmp_path / "agent-bin-calls.txt"
    agent_bin = tmp_path / "papaya-agent"
    agent_bin.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{calls}'\n"
        'echo \'{"mcpServers": {"papaya": {"command": "papaya-agent"}}}\'\n',
        encoding="utf-8",
    )
    agent_bin.chmod(0o755)
    monkeypatch.setenv("PAPAYA_AGENT_BIN", str(agent_bin))

    turns = FakeTurns(lambda turn: dispatch_worker(turn.run_id))
    harness = Harness(FakeEvents([EVENT]))
    job_env: dict[str, str] = {}

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, FakePapaya(), turn_tools=None))
        await _until(lambda: turns.calls, what="the brief turn to launch")
        job_env.update(harness.jobs[0].env)
        harness.jobs[0].stop.set()
        await _until(lambda: harness.results, what="the hold to end")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    runtime_dir = str(Path(repo_root()).resolve())
    launch = turns.calls[0].launch
    argv = launch.argv
    assert "--strict-mcp-config" in argv
    config_file = Path(argv[argv.index("--mcp-config") + 1])
    assert json.loads(config_file.read_text(encoding="utf-8"))["mcpServers"]["papaya"]
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert "mcp__papaya__*" in argv[argv.index("--allowedTools") + 1 :]
    assert "Bash(./bin/ppy:*)" in argv, "`ppy` stopped being callable"
    # The plugin the client gave the job — it carries the write-boundary hook.
    assert argv[argv.index("--plugin-dir") + 1] == job_env["PAPAYA_PLUGIN_DIR"]
    # Produced by the client binary the job names, for this agent, in the runtime dir.
    (invocation,) = calls.read_text(encoding="utf-8").splitlines()
    assert invocation == (
        f"mcp runner-config --harness claude-code --agent @tester --working-directory {runtime_dir}"
    )
    assert launch.env["PAPAYA_AGENT_BIN"] == str(agent_bin)
    # And the boundary is still the runtime directory, with the job's files beside it.
    assert json.loads(launch.env["PAPAYA_ALLOWED_WORKING_DIRECTORIES"]) == [runtime_dir]
    for key in ("PAPAYA_CONTEXT_FILE", "PAPAYA_DECLINE_FILE"):
        assert launch.env[key] == job_env[key], key


def test_a_turns_transcript_is_kept_where_the_progress_report_says(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    """Every turn's output survives it, at a path a person can be pointed at."""
    import sys

    from papaya_agent_runtime.manager.launch import run_turn

    def harness_that_dispatches(launch: Any, *, should_stop, transcript_path=None) -> TurnResult:
        # The real `run_turn`, with a stand-in harness that says something and
        # does what a brief turn's `ppy dispatch` would.
        dispatch_worker(int(launch.env[papaya_events.TICKET_RUN_ENV]))
        launch.argv = [
            sys.executable,
            "-c",
            "import sys; print('briefed and dispatched'); print('a warning', file=sys.stderr)",
        ]
        return run_turn(launch, should_stop=should_stop, transcript_path=transcript_path)

    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(harness_that_dispatches, FakePapaya()))
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        harness.jobs[0].stop.set()
        await _until(lambda: harness.results, what="the hold to end")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    (named,) = [
        detail.split("transcript: ", 1)[1]
        for _subject, _phase, detail in progress_lines
        if "transcript: " in detail
    ]
    run_id = int(ticket_task()["run_id"])
    assert Path(named) == ppy_home / "runs" / str(run_id) / "turns" / "brief-1.log"
    kept = Path(named).read_text(encoding="utf-8")
    assert "briefed and dispatched" in kept
    assert "a warning" in kept, "stderr was not kept"


def test_the_ticket_thread_carries_decisions_and_the_report_not_review_bookkeeping(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    """PAP-218: thirteen agent comments, five of them review internals, two after the report.

    Two reviews send the worker back and a third delivers with its own report. The
    thread gets pickup, dispatched, one sent-back line, and the report — last.
    """

    def act(turn: Turn) -> None:
        if turn.name == prompts.BRIEF:
            worker = dispatch_worker(turn.run_id)
            progress.record(worker, phase="plan", note="Add the endpoint behind the flag.")
            worker_event(worker, "worker_done", status="worker_done", summary="done")
        elif turn.name == prompts.REVIEW:
            if turns.names().count(prompts.REVIEW) <= 2:
                (worker,) = workers_in(turn.run_id)
                worker_event(worker, "steer", message="The flag is not checked.")
                worker_event(worker, "worker_done", status="worker_done", summary="fixed")
            else:
                _deliver(turn)

    papaya_api = FakePapaya()
    turns = FakeTurns(act)
    assert _one_ticket(Harness(FakeEvents([EVENT])), client_home, _runner(turns, papaya_api)) == 0

    assert turns.names() == [prompts.BRIEF, prompts.REVIEW, prompts.REVIEW, prompts.REVIEW]
    worker = workers_in(int(ticket_task()["run_id"]))[0]
    bodies = [body for _item, body in papaya_api.comments()]
    # Pickup and briefing are one comment: they happen within the same second.
    assert bodies == [
        "Picked up; choosing the repository and writing the brief.",
        f"Dispatched worker task {worker} in runtime.",
        serve.SENT_BACK_LINE,
        REPORT,  # the review turn's own, through MCP, and nothing after it
    ]
    assert all("\n" not in body for body in bodies)
    assert not any("Add the endpoint" in body for body in bodies), "worker progress on the ticket"
    # The internals still reach the host as progress.
    details = [detail for _s, _p, detail in progress_lines]
    assert details.count(f"Reviewing worker task {worker} at its head.") == 3
    assert details.count(f"Worker task {worker} sent back with findings.") == 2
    assert "Pull request open: https://github.com/acme/runtime/pull/7" in details
    assert "Result reported on this item." in details


def test_a_brief_that_left_no_acceptance_criteria_is_rerun_once_then_goes_on(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    """Define done on the record is checked, not hoped for — and never a dead end."""
    from papaya_agent_runtime.preflight import archive_brief

    def act(turn: Turn) -> None:
        if turn.name != prompts.BRIEF or workers_in(turn.run_id):
            return  # the rerun writes nothing either
        worker = dispatch_worker(turn.run_id)
        archive_brief("runtime", worker, "# Task\n\n## Goals\n\n1. The thing works.\n")

    papaya_api = FakePapaya(acceptance_criteria=None)
    turns = FakeTurns(act)
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, papaya_api))
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        harness.jobs[0].stop.set()
        await _until(lambda: harness.results, what="the hold to end")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert turns.names() == [prompts.BRIEF, prompts.BRIEF]
    assert prompts.ACCEPTANCE_ADDENDUM not in turns.calls[0].prompt
    assert f"- {prompts.ADDENDUM_FACT}: {prompts.ACCEPTANCE_ADDENDUM}" in turns.calls[1].prompt
    assert any("still has no acceptance criteria" in detail for _s, _p, detail in progress_lines)
    # The ticket went on: dispatched, one worker, not handed back.
    assert history()[:3] == [serve.PHASE_PICKED_UP, serve.PHASE_BRIEFING, serve.PHASE_DISPATCHED]
    assert len(workers_in(int(ticket_task()["run_id"]))) == 1
    assert harness.results[0]["exit_code"] == 0


def test_a_resumed_ticket_knows_its_worker_was_already_sent_back(ppy_home) -> None:
    """The sent-back comment is once per ticket, not once per hold."""
    conn = init_db()
    run_id = store.create_run(conn, "Fix the thing")
    task_id = store.add_task(conn, run_id=run_id, title="Fix the thing")
    serve.record_phase(conn, task_id, serve.PHASE_DISPATCHED, "Dispatched worker task 9.")
    serve.record_phase(conn, task_id, serve.PHASE_REVIEWING, "Reviewing worker task 9 at its head.")
    conn.close()
    assert not serve.sent_back_before(task_id)

    conn = init_db()
    serve.record_phase(
        conn, task_id, serve.PHASE_DISPATCHED, "Worker task 9 sent back with findings."
    )
    conn.close()
    assert serve.sent_back_before(task_id)


def _review_ticket(act_on_review, papaya_api: FakePapaya) -> FakeTurns:
    def act(turn: Turn) -> str | None:
        if turn.name == prompts.BRIEF:
            _dispatch_then_finish(turn)
        elif turn.name == prompts.REVIEW:
            return act_on_review(turn)
        return None

    return FakeTurns(act)


def test_a_review_turn_that_reports_on_the_item_is_believed(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    papaya_api = FakePapaya()
    turns = _review_ticket(_deliver, papaya_api)

    assert _one_ticket(Harness(FakeEvents([EVENT])), client_home, _runner(turns, papaya_api)) == 0

    assert turns.names() == [prompts.BRIEF, prompts.REVIEW]
    bodies = [body for _item, body in papaya_api.comments()]
    assert bodies[-1] == REPORT
    assert not any(body.startswith("Pull request open") for body in bodies)


def test_a_review_turn_that_delivers_silently_is_rerun_to_report(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    papaya_api = FakePapaya()

    def review(turn: Turn) -> None:
        if prompts.REPORT_ADDENDUM in turn.prompt:
            post_as_agent(turn, REPORT)
        else:
            _deliver(turn, report=False)

    turns = _review_ticket(review, papaya_api)

    assert _one_ticket(Harness(FakeEvents([EVENT])), client_home, _runner(turns, papaya_api)) == 0

    assert turns.names() == [prompts.BRIEF, prompts.REVIEW, prompts.REVIEW]
    assert f"- {prompts.ADDENDUM_FACT}: {prompts.REPORT_ADDENDUM}" in turns.calls[2].prompt
    bodies = [body for _item, body in papaya_api.comments()]
    assert bodies[-1] == REPORT
    assert not any(body.startswith("Pull request open") for body in bodies)


def test_a_review_turn_that_never_reports_gets_the_runners_fallback_line(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    """PAP-217: the runner said "Result posted" and nothing was. Never again."""
    papaya_api = FakePapaya()

    def review(turn: Turn) -> None:
        if prompts.REPORT_ADDENDUM not in turn.prompt:
            _deliver(turn, report=False)

    turns = _review_ticket(review, papaya_api)

    assert _one_ticket(Harness(FakeEvents([EVENT])), client_home, _runner(turns, papaya_api)) == 0

    assert turns.names() == [prompts.BRIEF, prompts.REVIEW, prompts.REVIEW]
    bodies = [body for _item, body in papaya_api.comments()]
    worker = workers_in(int(ticket_task()["run_id"]))[0]
    # The fallback is the runner's one line after dispatch, and the last.
    assert bodies == [
        "Picked up; choosing the repository and writing the brief.",
        f"Dispatched worker task {worker} in runtime.",
        "Pull request open: https://github.com/acme/runtime/pull/7; "
        "see the pull request for details.",
    ]
    assert (serve.PHASE_REPORTED, "reported (fallback)") in [
        (phase, detail) for _s, phase, detail in progress_lines
    ]
    assert history()[-2:] == [serve.PHASE_REPORTED, serve.PHASE_RELEASED]


def test_a_review_at_a_head_with_a_dirty_worktree_reports_the_count_and_steers(
    ppy_home, client_home, ready, registered_repo, tmp_path, progress_lines
) -> None:
    """PAP-219: the reviewer reads the branch; a worktree full of changes is not on it."""
    worktree = Path(make_git_repo(tmp_path / "worker-worktree"))
    papaya_api = FakePapaya()
    steers: list[tuple[int, str]] = []
    reviews: list[str] = []

    def brief(turn: Turn) -> None:
        worker = dispatch_worker(turn.run_id)
        conn = init_db()
        try:
            store.update_task_fields(conn, worker, worktree_path=str(worktree))
        finally:
            conn.close()
        (worktree / "README.md").write_text("# changed\n")
        (worktree / "endpoint.py").write_text("def things(): ...\n")
        (worktree / "test_endpoint.py").write_text("def test_things(): ...\n")
        worker_event(worker, "worker_done", status="worker_done", summary="done, not committed")

    def steer(task_id: int, message: str) -> None:
        steers.append((task_id, message))
        # What the steered worker does: commit it all and say done again.
        for args in (["add", "-A"], ["commit", "-qm", "Goal 1: the endpoint"]):
            subprocess.run(["git", "-C", str(worktree), *args], check=True, capture_output=True)
        worker_event(task_id, "worker_done", status="worker_done", summary="done at its head")

    def act(turn: Turn) -> None:
        if turn.name == prompts.BRIEF:
            brief(turn)
        elif turn.name == prompts.REVIEW:
            reviews.append(turn.prompt)
            _deliver(turn)

    turns = FakeTurns(act)
    runner = _runner(turns, papaya_api, steer=steer, uncommitted=serve.uncommitted_files)

    assert _one_ticket(Harness(FakeEvents([EVENT])), client_home, runner) == 0

    ((worker, message),) = steers
    assert "uncommitted work in the worktree: 3 files" in message
    assert "`endpoint.py`" in message and f"git push origin HEAD:ppy/task-{worker}" in message
    assert "commit" in message and "discard" in message
    # Reviewed only once the worktree was clean, and the finding was not carried into it.
    assert turns.names() == [prompts.BRIEF, prompts.REVIEW]
    assert "uncommitted work" not in reviews[0].split(prompts.FACTS_HEADING)[1]
    phases = history()
    reviewing = phases.index(serve.PHASE_REVIEWING)
    assert phases[reviewing + 1 : reviewing + 3] == [serve.PHASE_DISPATCHED, serve.PHASE_REVIEWING]
    assert any("uncommitted work in the worktree: 3 files" in d for _s, _p, d in progress_lines)
    # The review prompt says the same, for a turn that sees the finding itself.
    review_prompt = " ".join(prompts.load(prompts.REVIEW).split())
    assert "uncommitted work in the worktree: <n> files" in review_prompt
    assert "Review the remote branch, never the worktree." in review_prompt


# ── waiting on a gate ───────────────────────────────────────────────────────


def test_a_review_turn_that_ends_waiting_is_rerun_later_and_never_declined(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    """PAP-213: the review turn was still running the suite, and the ticket was declined."""
    papaya_api, clock = FakePapaya(), Clock()

    def review(turn: Turn) -> str | None:
        if len([call for call in turns.calls if call.name == prompts.REVIEW]) <= 3:
            return "Started the backend suite at abc123.\nWAITING: full suite\nIt is still running."
        _deliver(turn)
        return None

    turns = _review_ticket(review, papaya_api)
    harness = Harness(FakeEvents([EVENT]))

    def reviews() -> int:
        return turns.names().count(prompts.REVIEW)

    def waiting() -> list[tuple[str, str]]:
        return [(p, d) for _s, p, d in progress_lines if d.startswith("The review turn is waiting")]

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, papaya_api, clock=clock))
        for number, minutes in enumerate((5, 10, 20), start=1):
            await _until(lambda n=number: len(waiting()) == n, what=f"wait {number}")
            assert waiting()[-1] == (
                serve.PHASE_REVIEWING,
                f"The review turn is waiting: full suite; running it again in {minutes} minutes.",
            )
            clock.advance(minutes * 60 - 1)
            await asyncio.sleep(scale(0.1))
            assert reviews() == number, "rerun before the delay was up"
            assert not harness.results, "a waiting review ended the hold"
            assert history()[-1] == serve.PHASE_REVIEWING
            clock.advance(1)
            await _until(lambda n=number: reviews() == n + 1, what=f"rerun {number}")
        await _until(lambda: harness.results, what="the ticket to be delivered")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert turns.names() == [prompts.BRIEF] + [prompts.REVIEW] * 4
    # Each rerun is given the tail of the waiting turn before it.
    assert "review transcript #2" in turns.calls[2].prompt
    assert "WAITING: full suite" in turns.calls[2].prompt
    assert "review transcript #4" in turns.calls[4].prompt
    assert not any("The turn ended without" in d for _s, _p, d in progress_lines)
    assert harness.results[0]["exit_code"] == 0
    assert serve.PHASE_DECLINED not in history()
    assert history()[-3:] == [serve.PHASE_DELIVERING, serve.PHASE_REPORTED, serve.PHASE_RELEASED]


def test_a_review_turn_that_ends_with_nothing_twice_is_declined(
    ppy_home, client_home, ready, registered_repo
) -> None:
    papaya_api = FakePapaya()
    turns = _review_ticket(lambda _turn: "I looked at it.", papaya_api)
    harness = Harness(FakeEvents([EVENT]))

    assert _one_ticket(harness, client_home, _runner(turns, papaya_api)) == 0

    assert turns.names() == [prompts.BRIEF, prompts.REVIEW, prompts.REVIEW]
    assert "review transcript #2" in turns.calls[2].prompt
    result = harness.results[0]
    assert result["exit_code"] == 75
    assert "2 times without approving and delivering, or steering" in result["output"]
    assert store.task_phase(init_db(), int(ticket_task()["id"])) == serve.PHASE_DECLINED


def test_a_worker_stopped_mid_gate_is_sent_back_and_reviewed_only_once_done(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    """A long gate is the worker's: the runner steers it, and the review waits for `worker_done`."""
    steers: list[tuple[int, str]] = []

    def steer(task_id: int, message: str) -> None:
        steers.append((task_id, message))
        # What `ppy steer` on a worker with no live turn leaves: the session resumed.
        worker_event(task_id, "resumed", status="in_progress", message=message)

    turns = FakeTurns(
        lambda turn: _brief_dispatches(turn) if turn.name == prompts.BRIEF else _deliver(turn)
    )
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, FakePapaya(), steer=steer))
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        (worker,) = workers_in(int(ticket_task()["run_id"]))
        worker_event(
            worker,
            "worker_stopped",
            status="worker_stopped",
            summary="worker stopped before done: no done note was ever filed",
            reasons=["the last thing the session did was background `make test`"],
        )
        await _until(lambda: steers, what="the runner to send the worker back")
        await asyncio.sleep(scale(0.1))
        assert turns.names() == [prompts.BRIEF], "reviewed before the worker finished its gate"
        assert history()[-1] == serve.PHASE_DISPATCHED

        worker_event(worker, "worker_done", status="worker_done", summary="gate: 900 passed")
        await _until(lambda: harness.results, what="the ticket to be delivered")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert turns.names() == [prompts.BRIEF, prompts.REVIEW]
    ((steered, message),) = steers
    assert steered == workers_in(int(ticket_task()["run_id"]))[0]
    assert "in the foreground" in message and "no done note was ever filed" in message
    assert any("sent back to run its gate to completion" in d for _s, _p, d in progress_lines)
    # The review turn ran on `worker_done`, not on the failure.
    assert "what stopped the worker" not in turns.calls[1].prompt
    assert harness.results[0]["exit_code"] == 0


PLAN_BRIEF = """\
# Make it blue

## Goals
- Blue.

## Plan-note gate

blocking: stop after posting and wait for the manager's reply.
"""


def _stopped_at_its_plan(worker: int, *, brief: str | None = PLAN_BRIEF) -> None:
    """The exact shape of PAP-278's task 187: a plan note, then the turn ends."""
    from papaya_agent_runtime import preflight

    worker_event(
        worker,
        "worker_progress",
        phase="plan",
        note="H1 confirmed; here is what I intend to build",
    )
    if brief is not None:
        preflight.archive_brief("runtime", worker, brief)
    worker_event(
        worker,
        "worker_stopped",
        status="worker_stopped",
        summary="worker stopped before done: the latest progress note is 'plan', not a done note",
    )


def test_a_worker_that_stopped_at_its_plan_is_answered_never_sent_to_a_gate(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    """PAP-278: task 187 posted its plan and was told to run a gate on code that did not exist."""
    steers: list[tuple[int, str]] = []

    def steer(task_id: int, message: str) -> None:
        steers.append((task_id, message))
        worker_event(task_id, "resumed", status="in_progress", message=message)

    def act(turn: Turn) -> str | None:
        if turn.name == prompts.BRIEF:
            _brief_dispatches(turn)
            return None
        if turn.name == prompts.ANSWER:
            return "PLAN-REPLY: Approved as posted; build it."
        _deliver(turn)
        return None

    turns = FakeTurns(act)
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, FakePapaya(), steer=steer))
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        (worker,) = workers_in(int(ticket_task()["run_id"]))
        _stopped_at_its_plan(worker)
        await _until(lambda: steers, what="the runner to answer the plan")
        worker_event(worker, "worker_done", status="worker_done", summary="gate: 900 passed")
        await _until(lambda: harness.results, what="the ticket to be delivered")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert turns.names() == [prompts.BRIEF, prompts.ANSWER, prompts.REVIEW]
    ((steered, message),) = steers
    assert steered == workers_in(int(ticket_task()["run_id"]))[0]
    # Verbatim, and labelled so the worker knows what it is answering.
    assert message == "Manager reply to your plan note: Approved as posted; build it."
    assert "ppy gate run" not in message and "verification gate finished" not in message
    facts = turns.calls[1].prompt
    assert "- the worker stopped at its plan note:" in facts
    assert "H1 confirmed; here is what I intend to build" in facts
    assert "blocking: the worker was told to wait for your reply" in facts
    assert any("resumed with the manager's reply to its plan" in d for _s, _p, d in progress_lines)


def test_a_plan_answer_turn_that_says_nothing_twice_hands_the_ticket_back(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """No `PLAN-REPLY:` line is a missed turn: the worker is never resumed with a guess."""
    steers: list[tuple[int, str]] = []
    turns = FakeTurns(lambda turn: _brief_dispatches(turn) if turn.name == prompts.BRIEF else None)
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = _serve_ticket(
            harness,
            client_home,
            _runner(turns, FakePapaya(), steer=lambda t, m: steers.append((t, m))),
        )
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        (worker,) = workers_in(int(ticket_task()["run_id"]))
        _stopped_at_its_plan(worker)
        await _until(lambda: harness.results, what="the ticket to be handed back")
        harness.loop.request_stop()
        return await runner

    asyncio.run(scenario())

    assert steers == []
    assert turns.names() == [prompts.BRIEF, prompts.ANSWER, prompts.ANSWER]
    assert serve.PHASE_DECLINED in history()


def test_a_refused_gate_steer_gives_the_review_turn_the_failure(
    ppy_home, client_home, ready, registered_repo
) -> None:
    def steer(_task_id: int, _message: str) -> None:
        raise RuntimeError("no supervisor")

    turns = FakeTurns(
        lambda turn: _brief_dispatches(turn) if turn.name == prompts.BRIEF else _deliver(turn)
    )
    harness = Harness(FakeEvents([EVENT]))

    async def scenario() -> int:
        runner = _serve_ticket(harness, client_home, _runner(turns, FakePapaya(), steer=steer))
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        (worker,) = workers_in(int(ticket_task()["run_id"]))
        worker_event(worker, "worker_stopped", status="worker_stopped", summary="cut short")
        await _until(lambda: harness.results, what="the ticket to be delivered")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0
    assert turns.names() == [prompts.BRIEF, prompts.REVIEW]
    assert "worker_stopped: cut short" in turns.calls[1].prompt


# ── the recorded gate decides ───────────────────────────────────────────────


def _recorded(exit_code: int, summary: str) -> gate.Verdict:
    result = gate.GateResult(
        repo="runtime",
        command="make test",
        full=False,
        exit_code=exit_code,
        duration_seconds=720.0,
        summary=summary,
        head_sha="c0ffee" * 6 + "abcd",
        output_path="/wt/.ppy-evidence/gate-local-c0ffeec0.txt",
        started_at="2026-09-16T10:00:00+00:00",
        finished_at="2026-09-16T10:12:00+00:00",
    )
    return gate.Verdict(gate.GREEN if exit_code == 0 else gate.RED, result.head_sha, result)


def test_a_stopped_worker_whose_head_has_a_green_gate_goes_to_review(
    ppy_home, client_home, ready, registered_repo, progress_lines
) -> None:
    """The gate outlived the session under the supervisor, and its record is the gate."""
    turns = FakeTurns(
        lambda turn: _brief_dispatches(turn) if turn.name == prompts.BRIEF else _deliver(turn)
    )
    harness = Harness(FakeEvents([EVENT]))
    runner = _runner(
        turns,
        FakePapaya(),
        steer=_no_steer,
        gate_verdict=lambda _task_id: _recorded(0, "900 passed in 712.00s"),
    )

    async def scenario() -> int:
        task = _serve_ticket(harness, client_home, runner)
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        (worker,) = workers_in(int(ticket_task()["run_id"]))
        worker_event(worker, "worker_stopped", status="worker_stopped", summary="cut short")
        await _until(lambda: harness.results, what="the ticket to be delivered")
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0
    assert turns.names() == [prompts.BRIEF, prompts.REVIEW]
    review = turns.calls[1].prompt
    assert "900 passed in 712.00s" in review
    # Reviewed as finished work, not as a failure to steer on.
    assert "what stopped the worker" not in review
    assert any("its local gate green" in d for _s, _p, d in progress_lines)


def test_the_review_turn_reads_the_full_suite_run_once_at_the_head_and_is_told_not_to_rerun(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """The full suite runs once per head, before the review turn, never inside it."""
    turns = FakeTurns(
        lambda turn: _brief_dispatches(turn) if turn.name == prompts.BRIEF else _deliver(turn)
    )
    harness = Harness(FakeEvents([EVENT]))
    full = _recorded(0, "10432 passed in 961.00s")
    full = gate.Verdict(
        gate.GREEN,
        full.head_sha,
        replace(full.result, command="make verify", full=True),
    )
    asked: list[int] = []

    def full_suite(task_id: int) -> gate.Verdict:
        asked.append(task_id)
        return full

    runner = _runner(turns, FakePapaya(), full_suite=full_suite)

    async def scenario() -> int:
        task = _serve_ticket(harness, client_home, runner)
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        (worker,) = workers_in(int(ticket_task()["run_id"]))
        worker_event(worker, "worker_done", status="worker_done", summary="scoped gate green")
        await _until(lambda: harness.results, what="the ticket to be delivered")
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0
    assert turns.names() == [prompts.BRIEF, prompts.REVIEW]
    (worker,) = workers_in(int(ticket_task()["run_id"]))
    assert asked == [worker]
    review = " ".join(turns.calls[1].prompt.split())
    fact = review.partition(f"- {serve.FULL_SUITE_FACT}: ")[2].partition(" - ")[0]
    assert full.result.line() in fact
    assert "already run once at this head; do not run it again" in fact
    assert "Do not run `ppy gate run --full` again at a head that has one" in review


def test_a_worker_whose_head_has_a_red_gate_is_steered_with_the_summary(
    ppy_home, client_home, ready, registered_repo
) -> None:
    steers: list[tuple[int, str]] = []
    verdicts = [_recorded(2, "3 failed, 897 passed in 700.10s")]

    def steer(task_id: int, message: str) -> None:
        steers.append((task_id, message))
        verdicts.append(_recorded(0, "900 passed in 705.00s"))
        worker_event(task_id, "resumed", status="in_progress", message=message)

    turns = FakeTurns(
        lambda turn: _brief_dispatches(turn) if turn.name == prompts.BRIEF else _deliver(turn)
    )
    harness = Harness(FakeEvents([EVENT]))
    runner = _runner(turns, FakePapaya(), steer=steer, gate_verdict=lambda _id: verdicts[-1])

    async def scenario() -> int:
        task = _serve_ticket(harness, client_home, runner)
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        (worker,) = workers_in(int(ticket_task()["run_id"]))
        # Said done, but the gate on the record at its head is red.
        worker_event(worker, "worker_done", status="worker_done", summary="all good")
        await _until(lambda: steers, what="the runner to steer the red gate")
        await asyncio.sleep(scale(0.1))
        assert turns.names() == [prompts.BRIEF], "reviewed a red gate"
        worker_event(worker, "worker_done", status="worker_done", summary="fixed")
        await _until(lambda: harness.results, what="the ticket to be delivered")
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0
    ((_steered, message),) = steers
    assert "3 failed, 897 passed in 700.10s" in message
    assert "`ppy gate run --task" in message
    assert turns.names() == [prompts.BRIEF, prompts.REVIEW]


def test_a_gate_red_twice_the_same_way_is_a_persons_not_a_third_run(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """PAP-219 (2026-09-17): one red gate at one head was run four times."""
    steers: list[tuple[int, str]] = []
    red = _recorded(2, "1 failed, 12379 passed in 869.31s").result
    runs = [
        dataclasses.replace(red, failing_tests=["tests/test_a.py::test_b"], summary=summary)
        for summary in ("1 failed, 12379 passed in 869.31s", "1 failed, 12379 passed in 902.10s")
    ]
    repeated = gate.repeated_red(runs)
    assert len(repeated) == 2
    verdict = gate.Verdict(gate.RED, red.head_sha, runs[0], repeated)

    def steer(task_id: int, message: str) -> None:
        steers.append((task_id, message))

    papaya = FakePapaya()
    turns = FakeTurns(
        lambda turn: _brief_dispatches(turn) if turn.name == prompts.BRIEF else _deliver(turn)
    )
    harness = Harness(FakeEvents([EVENT]))
    runner = _runner(turns, papaya, steer=steer, gate_verdict=lambda _id: verdict)

    async def scenario() -> int:
        task = _serve_ticket(harness, client_home, runner)
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        (worker,) = workers_in(int(ticket_task()["run_id"]))
        worker_event(worker, "worker_done", status="worker_done", summary="gate red again")
        await _until(lambda: harness.results, what="the review to decide")
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0
    assert steers == [], "the worker was sent back to run the same red gate a third time"
    assert serve.PHASE_NEEDS_A_PERSON in history()
    conn = init_db()
    try:
        rows = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = ?",
            (ticket_task()["id"], serve.GATE_NEEDS_A_PERSON),
        ).fetchall()
    finally:
        conn.close()
    (record,) = [json.loads(row["payload"]) for row in rows]
    assert record["head_sha"] == red.head_sha and len(record["results"]) == 2
    said = [c["body"] for thread in papaya.stored.values() for c in thread]
    gate_comments = [body for body in said if "failed twice the same way" in body]
    assert len(gate_comments) == 1
    assert "tests/test_a.py::test_b" in gate_comments[0] and red.head_sha[:8] in gate_comments[0]
    review = turns.calls[1].prompt
    assert "red 2 times" in review and "--baseline" in review


def test_a_stopped_worker_with_no_recorded_gate_is_steered_to_run_ppy_gate_run(
    ppy_home, client_home, ready, registered_repo
) -> None:
    steers: list[tuple[int, str]] = []

    def steer(task_id: int, message: str) -> None:
        steers.append((task_id, message))
        worker_event(task_id, "resumed", status="in_progress", message=message)

    turns = FakeTurns(
        lambda turn: _brief_dispatches(turn) if turn.name == prompts.BRIEF else _deliver(turn)
    )
    harness = Harness(FakeEvents([EVENT]))
    runner = _runner(
        turns, FakePapaya(), steer=steer, gate_verdict=lambda _id: gate.Verdict(gate.NONE)
    )

    async def scenario() -> int:
        task = _serve_ticket(harness, client_home, runner)
        await _until(lambda: serve.PHASE_DISPATCHED in history(), what="the dispatch")
        (worker,) = workers_in(int(ticket_task()["run_id"]))
        worker_event(worker, "worker_stopped", status="worker_stopped", summary="cut short")
        await _until(lambda: steers, what="the runner to send the worker to its gate")
        worker_event(worker, "worker_done", status="worker_done", summary="gate: 900 passed")
        await _until(lambda: harness.results, what="the ticket to be delivered")
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0
    ((worker, message),) = steers
    assert f"`ppy gate run --task {worker}`" in message
    assert "no gate result is recorded at your head" in message
    assert " ".join(prompts.TEN_MINUTE_RULE.split()) in " ".join(message.split())


@pytest.mark.parametrize(
    ("transcript", "reason"),
    [
        ("WAITING: full suite", "full suite"),
        ("ran make test\n\n**WAITING: backend gate**\nstill going", "backend gate"),
        ("WAITING:", "(no reason given)"),
        ("I approved it. Nothing is WAITING: here.", None),
        ("", None),
    ],
)
def test_waiting_reason_reads_the_first_line_contract(transcript: str, reason: str | None) -> None:
    assert serve.waiting_reason(TurnResult(exit_code=0, transcript=transcript)) == reason


def test_the_rerun_delay_doubles_from_five_minutes_to_a_cap_of_thirty() -> None:
    assert [serve.rerun_delay(n) / 60 for n in range(1, 6)] == [5, 10, 20, 30, 30]


# ── the prompts ─────────────────────────────────────────────────────────────


def test_turns_and_workers_are_told_to_run_gates_in_the_foreground_and_say_waiting() -> None:
    def flat(text: str) -> str:
        return " ".join(text.split())

    for turn in (prompts.REVIEW, prompts.BRIEF):
        text = flat(prompts.load(turn))
        assert "in the foreground" in text, turn
        assert "never in the background" in text, turn
        assert f"`{prompts.WAITING_PREFIX} <what you are waiting for>`" in text, turn
    assert "re-check" in prompts.load(prompts.REVIEW)
    skill = flat((Path(serve.__file__).parents[2] / prompts.BRIEF_SKILL).read_text("utf-8"))
    assert "run the gate in the foreground" in skill
    assert "never end the session with it still running" in skill


def test_every_prompt_carries_the_ten_minute_rule_and_the_review_rechecks_with_ppy_gate_run() -> (
    None
):
    """PAP-213: no wording makes a twelve-minute suite finish inside a ten-minute tool call."""
    from papaya_agent_runtime.providers.command_rules import command_rules

    def flat(text: str) -> str:
        return " ".join(text.split())

    rule = flat(prompts.TEN_MINUTE_RULE)
    assert rule == (
        "A command that may run longer than ten minutes must not be run as a tool call; use "
        "`ppy gate run`, or push and let the hook run it. Never background a gate and wait."
    )
    for turn in (prompts.BRIEF, prompts.REVIEW):
        assert rule in flat(prompts.load(turn)), turn
    assert "`ppy gate run --task <worker task id>`" in flat(prompts.load(prompts.REVIEW))
    assert rule in flat(command_rules("claude", "ppy/task-7"))
    assert rule in flat(serve.gate_steer_message("cut short", 7))


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
    tail = rendered.split(prompts.FACTS_HEADING, 1)[1]
    assert "- work item id: item-9" in tail
    assert "repository" not in tail
    assert "```\nWhich route?\nv1 or v2?\n```" in tail


# ── the sweep: looking for work as well as waiting for it ───────────────────


def _item(n: int, status: str = "todo") -> dict[str, Any]:
    return {"id": f"item-{n}", "title": f"Ticket {n}", "status": status, "repo": "acme/runtime"}


@dataclass
class Assigned:
    """What Papaya says is assigned to this agent, and how often it was asked."""

    items: list[dict[str, Any]]
    calls: int = 0


@pytest.fixture
def assigned(monkeypatch) -> Assigned:
    """The client's `list_assigned_work_items`, answering from a list the test owns."""
    from papaya_agent_client import api_client

    fake = Assigned(items=[])

    async def list_assigned_work_items(_api: Any, *, status: str | None = None) -> list[dict]:
        fake.calls += 1
        return [dict(item) for item in fake.items]

    monkeypatch.setattr(api_client, "list_assigned_work_items", list_assigned_work_items)
    return fake


def _summaries(stderr: io.StringIO) -> list[str]:
    return [line for line in stderr.getvalue().splitlines() if "ppy serve: sweep" in line]


def _ticket_histories() -> dict[str, list[str]]:
    """Every ticket task's phase history, by the work item it was recorded for."""
    conn = init_db()
    try:
        rows = conn.execute(
            "SELECT tasks.id, task_env.value FROM tasks JOIN task_env "
            "ON task_env.task_id = tasks.id WHERE task_env.key = ? ORDER BY tasks.id",
            (papaya_events.PAPAYA_EVENT_METADATA,),
        ).fetchall()
        return {
            json.loads(row["value"])["work_item_id"]: serve.phase_history(conn, int(row["id"]))
            for row in rows
        }
    finally:
        conn.close()


def _existing_task(item_id: str, phase: str, *, event_id: str = "55") -> None:
    """A task this runtime recorded for `item_id` earlier, from an ordinary event."""
    conn = init_db()
    try:
        event = papaya_events.PapayaEvent(
            id=event_id,
            kind="work_item.assigned",
            subject=f"work_item:{item_id}",
            payload={},
            work_item_id=item_id,
        )
        task_id = store.add_task(conn, run_id=store.create_run(conn, "earlier"), title="earlier")
        papaya_events.record_task(conn, task_id, event)
        store.set_task_phase(conn, task_id, phase)
    finally:
        conn.close()


def _reserved(harness: Harness) -> list[str]:
    return [subject for subject, _session in harness.events.reserves]


def _holding_runner() -> serve.TicketRunner:
    """A runner whose brief turn dispatches a worker, so a swept ticket is held."""
    return _runner(FakeTurns(lambda turn: dispatch_worker(turn.run_id)), FakePapaya())


async def _serving(
    harness: Harness,
    client_home: ClientHome,
    stderr: io.StringIO,
    *,
    args: tuple[str, ...] = (),
    sweep_sleep=None,
    server=None,
    runner: serve.TicketRunner | None = None,
    max_concurrent: int | None = None,
) -> asyncio.Task[int]:
    """`serve.run` sweeping; the pool is the config's worker count unless `max_concurrent`."""
    options = serve.parse_args(["--working-directory", str(client_home.work_dir), *args])
    extra = harness.extra()
    if max_concurrent is not None:
        extra["max_concurrent"] = max_concurrent
    return asyncio.create_task(
        serve.run(
            options,
            stdout=io.StringIO(),
            stderr=stderr,
            extra=extra,
            runner=runner or _holding_runner(),
            server=server,
            sweep_sleep=sweep_sleep or Ticks().sleep,
        )
    )


def test_a_start_sweep_offers_every_open_assigned_item_nothing_has_picked_up(
    ppy_home, client_home, ready, registered_repo, assigned
) -> None:
    assigned.items = [_item(1), _item(2, "in_progress"), _item(3, "changes_requested")]
    # Started once and untouched for days: left, not in progress elsewhere.
    assigned.items[1]["updated_at"] = "2026-09-03T09:00:00+00:00"
    # Not open, so not found: a finished ticket is nobody's work.
    assigned.items.append(_item(4, "done"))
    harness = Harness(FakeEvents([]))
    stderr = io.StringIO()

    async def scenario() -> int:
        runner = await _serving(harness, client_home, stderr)
        await _until(lambda: len(harness.jobs) == 3, what="three swept jobs")
        await _until(
            lambda: (
                len(_ticket_histories()) == 3
                and all(h and h[0] == serve.PHASE_PICKED_UP for h in _ticket_histories().values())
            ),
            what="three tasks picked up",
        )
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert sorted(_reserved(harness)) == [
        "work_item:item-1",
        "work_item:item-2",
        "work_item:item-3",
    ]
    assert sorted(_ticket_histories()) == ["item-1", "item-2", "item-3"]
    assert _summaries(stderr) == ["ppy serve: sweep found 3: 3 offered"]
    # The job a swept item became is the one an event would have become.
    assert harness.jobs[0].event["kind"] == "work_item.assigned"


def test_an_item_with_a_live_task_is_not_offered_again(
    ppy_home, client_home, ready, registered_repo, assigned
) -> None:
    """Live by work item id, whatever event created the task."""
    _existing_task("item-2", serve.PHASE_PICKED_UP)
    assigned.items = [_item(1), _item(2), _item(3)]
    harness = Harness(FakeEvents([]))
    stderr = io.StringIO()

    async def scenario() -> int:
        runner = await _serving(harness, client_home, stderr)
        await _until(lambda: _summaries(stderr), what="the start sweep")
        await _until(lambda: len(harness.jobs) == 2, what="two swept jobs")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert sorted(_reserved(harness)) == ["work_item:item-1", "work_item:item-3"]
    assert _summaries(stderr) == ["ppy serve: sweep found 3: 1 already taken, 2 offered"]


def test_an_item_whose_only_task_was_handed_back_is_offered_again(
    ppy_home, client_home, ready, registered_repo, assigned
) -> None:
    """Handed back while the machine was off, and still assigned: still work."""
    _existing_task("item-1", serve.PHASE_HANDED_BACK)
    assigned.items = [_item(1)]
    harness = Harness(FakeEvents([]))
    stderr = io.StringIO()

    async def scenario() -> int:
        runner = await _serving(harness, client_home, stderr)
        await _until(lambda: harness.jobs, what="the swept job")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert _reserved(harness) == ["work_item:item-1"]
    assert _summaries(stderr) == ["ppy serve: sweep found 1: 1 offered"]


def test_an_item_held_by_another_session_is_skipped_and_not_remembered(
    ppy_home, client_home, ready, registered_repo, assigned
) -> None:
    assigned.items = [_item(1), _item(2), _item(3)]
    harness = Harness(FakeEvents([]))
    harness.events.held.add("work_item:item-2")
    stderr = io.StringIO()
    clock = Ticks()

    async def scenario() -> int:
        runner = await _serving(harness, client_home, stderr, sweep_sleep=clock.sleep)
        await _until(lambda: len(_summaries(stderr)) == 1, what="the start sweep")
        # A 409 is not a race to come back to: the loop does not keep it for a
        # re-bid, and the next sweep simply asks again.
        assert harness.loop.skipped_subjects == []
        clock.tick()
        await _until(lambda: len(_summaries(stderr)) == 2, what="the timed sweep")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert _summaries(stderr)[0] == "ppy serve: sweep found 3: 1 already taken, 2 offered"
    assert _reserved(harness).count("work_item:item-2") == 2
    assert "item-2" not in _ticket_histories()


def test_a_full_pool_ends_the_round_and_the_next_sweep_offers_the_rest(
    ppy_home, client_home, ready, registered_repo, assigned
) -> None:
    assigned.items = [_item(1), _item(2)]
    harness = Harness(FakeEvents([]))
    stderr = io.StringIO()
    clock = Ticks()

    async def scenario() -> int:
        runner = await _serving(
            harness, client_home, stderr, sweep_sleep=clock.sleep, max_concurrent=1
        )
        await _until(lambda: len(_summaries(stderr)) == 1, what="the start sweep")
        assert _reserved(harness) == ["work_item:item-1"], "the full pool was asked again"

        # The first ticket ends and is no longer open; its slot is free again.
        await _until(lambda: harness.jobs, what="the first job")
        harness.jobs[0].stop.set()
        await _until(lambda: not harness.loop.running_subjects, what="the slot to free up")
        assigned.items[0]["status"] = "done"

        clock.tick()
        await _until(lambda: len(_summaries(stderr)) == 2, what="the next sweep")
        await _until(lambda: len(harness.jobs) == 2, what="the second job")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert _summaries(stderr) == [
        "ppy serve: sweep found 2: 1 offered; 1 left for the next sweep (every slot is busy)",
        "ppy serve: sweep found 1: 1 offered",
    ]
    assert _reserved(harness) == ["work_item:item-1", "work_item:item-2"]


def test_a_default_pool_sweeps_three_tickets_and_leaves_the_fourth(
    ppy_home, client_home, ready, registered_repo, assigned
) -> None:
    """Three workers by default, so the sweep fills three slots from one round."""
    assigned.items = [_item(1), _item(2), _item(3), _item(4)]
    harness = Harness(FakeEvents([]))
    stderr = io.StringIO()

    async def scenario() -> int:
        runner = await _serving(harness, client_home, stderr)
        await _until(lambda: len(_summaries(stderr)) == 1, what="the start sweep")
        await _until(lambda: len(harness.jobs) == 3, what="three swept jobs")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert _summaries(stderr) == [
        "ppy serve: sweep found 4: 3 offered; 1 left for the next sweep (every slot is busy)",
    ]
    assert _reserved(harness) == ["work_item:item-1", "work_item:item-2", "work_item:item-3"]


def test_a_zero_sweep_interval_sweeps_once_at_start_and_never_again(
    ppy_home, client_home, ready, registered_repo, assigned
) -> None:
    harness = Harness(FakeEvents([]))
    stderr = io.StringIO()
    waits: list[float] = []

    async def never(seconds: float) -> None:
        waits.append(seconds)
        await asyncio.sleep(0)

    async def scenario() -> int:
        runner = await _serving(
            harness, client_home, stderr, args=("--sweep-interval", "0"), sweep_sleep=never
        )
        await _until(lambda: _summaries(stderr), what="the start sweep")
        # Long enough for a timer that slept for zero seconds to have swept again.
        await asyncio.sleep(0.2)
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert assigned.calls == 1
    assert waits == []
    assert _summaries(stderr) == ["ppy serve: sweep found 0: 0 offered"]


def test_the_sweep_interval_comes_from_the_flag_then_the_environment(monkeypatch) -> None:
    from papaya_agent_runtime import sweep

    assert serve.parse_args([]).sweep_interval == sweep.DEFAULT_SWEEP_INTERVAL == 300.0
    monkeypatch.setenv(sweep.SWEEP_INTERVAL_ENV, "60")
    assert serve.parse_args([]).sweep_interval == 60.0
    assert serve.parse_args(["--sweep-interval", "0"]).sweep_interval == 0.0

    bad = serve.parse_args(["--sweep-interval", "-5"])
    assert bad.invalid_arguments is not None and "--sweep-interval" in bad.invalid_arguments
    monkeypatch.setenv(sweep.SWEEP_INTERVAL_ENV, "soon")
    assert sweep.SWEEP_INTERVAL_ENV in (serve.parse_args([]).invalid_arguments or "")


def test_ppy_sweep_asks_the_running_serve_and_prints_the_summary(
    ppy_home, client_home, ready, registered_repo, assigned, capsys
) -> None:
    from papaya_agent_runtime import cli
    from papaya_agent_runtime.supervisor.server import SupervisorServer

    harness = Harness(FakeEvents([]))
    stderr = io.StringIO()
    server = SupervisorServer()
    server.start_background()

    async def scenario() -> tuple[int, int]:
        runner = await _serving(
            harness, client_home, stderr, args=("--sweep-interval", "0"), server=server
        )
        await _until(lambda: _summaries(stderr), what="the start sweep")
        assigned.items = [_item(7)]
        # On a thread: the request is answered on this event loop, which must be
        # free to run the sweep while the command waits for it.
        exit_code = await asyncio.to_thread(cli.main, ["sweep"])
        harness.loop.request_stop()
        return exit_code, await runner

    try:
        assert asyncio.run(scenario()) == (0, 0)
    finally:
        server.stop()

    assert capsys.readouterr().out.strip() == "sweep found 1: 1 offered"
    assert _reserved(harness) == ["work_item:item-7"]
    assert server.sweep_handler is None, "the handler outlived the listener"


def _unplaceable(monkeypatch) -> None:
    """Every ticket names a repository this runtime cannot register, so it is declined."""
    monkeypatch.setattr(
        papaya_events.solicit,
        "ensure",
        _raise(solicit.SolicitError("acme/runtime is not in any account you belong to")),
    )


def test_a_declined_item_is_not_offered_again_until_asked_by_hand(
    ppy_home, client_home, ready, assigned, monkeypatch, capsys
) -> None:
    """Otherwise the person is asked, and the ticket declined, every five minutes."""
    from papaya_agent_runtime import cli, sweep
    from papaya_agent_runtime.supervisor.server import SupervisorServer

    _unplaceable(monkeypatch)
    assigned.items = [{**_item(1), "updated_at": "2026-09-16T10:00:00Z"}]
    harness = Harness(FakeEvents([]))
    stderr = io.StringIO()
    clock = Ticks()
    server = SupervisorServer()
    server.start_background()

    async def scenario() -> tuple[int, int]:
        runner = await _serving(
            harness, client_home, stderr, sweep_sleep=clock.sleep, server=server
        )
        await _until(lambda: harness.results, what="the first pickup to be declined")
        await _until(lambda: not harness.loop.running_subjects, what="the decline to finish")

        clock.tick()
        await _until(lambda: len(_summaries(stderr)) == 2, what="the next sweep")

        # By hand, a person can still ask for it.
        exit_code = await asyncio.to_thread(cli.main, ["sweep", "--include-declined"])
        await _until(lambda: len(harness.results) == 2, what="the by-hand pickup")
        harness.loop.request_stop()
        return exit_code, await runner

    try:
        assert asyncio.run(scenario()) == (0, 0)
    finally:
        server.stop()

    remembered = sweep.declined_items()["item-1"]
    assert remembered["updated_at"] == "2026-09-16T10:00:00Z"
    assert "not in any account" in remembered["reason"]
    assert _summaries(stderr)[1] == ("ppy serve: sweep found 1: 1 declined earlier, 0 offered")
    assert capsys.readouterr().out.strip() == "sweep found 1: 1 offered"
    assert _reserved(harness) == ["work_item:item-1", "work_item:item-1"]


def test_a_declined_item_changed_since_is_offered_again(
    ppy_home, client_home, ready, assigned, monkeypatch
) -> None:
    """A newer `updated_at` is a person editing or commenting: worth another look."""
    _unplaceable(monkeypatch)
    assigned.items = [{**_item(1), "updated_at": "2026-09-16T10:00:00Z"}]
    harness = Harness(FakeEvents([]))
    stderr = io.StringIO()
    clock = Ticks()

    async def scenario() -> int:
        runner = await _serving(harness, client_home, stderr, sweep_sleep=clock.sleep)
        await _until(lambda: harness.results, what="the first pickup to be declined")
        await _until(lambda: not harness.loop.running_subjects, what="the decline to finish")

        assigned.items[0]["updated_at"] = "2026-09-16T10:05:00Z"
        clock.tick()
        await _until(lambda: len(harness.results) == 2, what="the second pickup")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert _summaries(stderr)[1] == "ppy serve: sweep found 1: 1 offered"
    assert _reserved(harness) == ["work_item:item-1", "work_item:item-1"]


def test_work_papaya_keeps_elsewhere_is_left_alone_until_ppy_sweep_include_kept(
    ppy_home, client_home, ready, registered_repo, assigned, capsys
) -> None:
    """Through the client's real loop: the refusal is seen, remembered, and re-asked by hand."""
    from papaya_agent_runtime import cli, sweep
    from papaya_agent_runtime.supervisor.server import SupervisorServer

    assigned.items = [_item(1), _item(2)]
    harness = Harness(FakeEvents([]))
    harness.events.not_routed.update({"work_item:item-1", "work_item:item-2"})
    stderr = io.StringIO()
    clock = Ticks()
    server = SupervisorServer()
    server.start_background()

    async def scenario() -> tuple[int, int]:
        runner = await _serving(
            harness, client_home, stderr, sweep_sleep=clock.sleep, server=server
        )
        await _until(lambda: len(_summaries(stderr)) == 1, what="the start sweep")
        # The timed sweep holds the sweeper's lock once it wakes, so the by-hand
        # request below is answered after it.
        clock.tick()
        await _until(lambda: clock.count == 1, what="the timed sweep")
        exit_code = await asyncio.to_thread(cli.main, ["sweep", "--include-kept"])
        harness.loop.request_stop()
        return exit_code, await runner

    try:
        assert asyncio.run(scenario()) == (0, 0)
    finally:
        server.stop()

    # Nothing in this test answers Papaya's reservation route, and work that cannot be
    # shown idle counts as being worked, so the memory holds.
    kept = (
        "sweep found 2: 2 kept by Engineering Agent in Papaya and being worked "
        "(use Run on this Mac to route one here), 0 offered"
    )
    assert _summaries(stderr)[0] == f"ppy serve: {kept}"
    assert sorted(sweep.kept_items()) == ["item-1", "item-2"]
    assert capsys.readouterr().out.strip() == kept
    # Asked at start, not on the timed sweep, and again by hand.
    assert sorted(_reserved(harness)) == ["work_item:item-1"] * 2 + ["work_item:item-2"] * 2


def test_ppy_sweep_with_nothing_serving_lists_the_waiting_work_itself(
    ppy_home, capsys, monkeypatch
) -> None:
    """With no serve to offer work to, a session is told what is assigned and waiting."""
    from papaya_agent_runtime import cli, supervision
    from papaya_agent_runtime.supervisor.server import SupervisorServer

    waiting = [{"id": "w1", "display_id": "PAP-231", "title": "Route it"}]
    monkeypatch.setattr(supervision, "assigned_unpicked", lambda **k: list(waiting))

    assert cli.main(["sweep"]) == 0
    assert "assigned and waiting: PAP-231 Route it" in capsys.readouterr().out

    # A bare supervisor is running, but no listener: the same answer.
    server = SupervisorServer()
    server.start_background()
    try:
        waiting.clear()
        assert cli.main(["sweep"]) == 0
    finally:
        server.stop()
    assert "no assigned work is waiting" in capsys.readouterr().out


def test_a_ticket_handed_back_by_its_turns_is_not_swept_into_them_again(
    ppy_home, client_home, ready, registered_repo, assigned
) -> None:
    """A `declined` task row is not live, so without the memory the turns would rerun."""
    from papaya_agent_runtime import sweep

    assigned.items = [{**_item(1), "updated_at": "2026-01-01T00:00:00Z"}]
    turns = FakeTurns()  # every brief turn ends without dispatching
    harness = Harness(FakeEvents([]))
    stderr = io.StringIO()
    clock = Ticks()

    async def scenario() -> int:
        runner = await _serving(
            harness,
            client_home,
            stderr,
            sweep_sleep=clock.sleep,
            runner=_runner(turns, FakePapaya()),
        )
        await _until(lambda: harness.results, what="the ticket to be handed back")
        await _until(lambda: not harness.loop.running_subjects, what="the hand-back to finish")

        clock.tick()
        await _until(lambda: len(_summaries(stderr)) == 2, what="the next sweep")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    assert turns.names() == [prompts.BRIEF, prompts.BRIEF], "the turns ran again"
    assert "without dispatching" in sweep.declined_items()["item-1"]["reason"]
    assert _summaries(stderr)[1] == ("ppy serve: sweep found 1: 1 declined earlier, 0 offered")
