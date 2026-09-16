"""What this machine needs from its owner reaches its owner, and nothing private does.

Every test here breaks the machine through `readiness.machine` (the conftest's
:class:`HealthyMachine`, with a failure set on it), fakes GitHub and the DM, and
moves time by hand. The ledger itself is the real file under ``PPY_HOME``.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import test_serve
from conftest import leaked, scale
from papaya_agent_runtime import blockers, papaya_events, readiness, serve, solicit
from papaya_agent_runtime.config import ManagerProfile, MMConfig, WorkerCeiling, save_config
from papaya_agent_runtime.state import store
from papaya_agent_runtime.state.db import init_db
from test_serve import (
    EVENT,
    SUBJECT,
    FakeEvents,
    FakePapaya,
    FakeTurns,
    Harness,
    Host,
    Ticks,
    _runner,
    _until,
)

globals().update({name: getattr(test_serve, name) for name in ("client_home", "dm", "harnesses")})

GH_STATUS = ("gh", "auth", "status")
#: What a job carries to reach Papaya (the comment route needs all three).
JOB_ENV = {
    "PAPAYA_API_URL": "http://papaya.test",
    "PAPAYA_WORKSPACE_ID": "ws-1",
    "PAPAYA_AGENT_TOKEN": "pagc_test_token",
}
T0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


class WallClock:
    """An aware clock moved by hand."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class Owner:
    """The owner's DM: every message that landed."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def say(self, text: str) -> bool:
        self.messages.append(text)
        return True


@pytest.fixture
def github_repo(ppy_home, monkeypatch) -> str:
    """`acme/runtime`, registered with its GitHub forge, onboarding stubbed out."""
    conn = init_db()
    store.add_repo(
        conn,
        name="runtime",
        origin="https://github.com/acme/runtime",
        local_path=str(ppy_home / "repos" / "runtime"),
        default_branch="main",
        base_sha="a" * 40,
        forge_url="https://github.com/acme/runtime",
    )
    conn.close()
    monkeypatch.setattr(
        papaya_events.solicit,
        "ensure",
        lambda spec, **kw: solicit.Ensured("runtime", "acme/runtime", False, False, ""),
    )
    return "runtime"


@pytest.fixture
def set_up(ppy_home, harnesses) -> None:
    """A configured runtime with Claude signed in: only the machine checks can fail."""
    harnesses("claude")
    save_config(
        MMConfig(
            manager=ManagerProfile("claude", "opus", "high"),
            worker=WorkerCeiling("claude", "opus", "medium"),
        )
    )


def _forge_only(monkeypatch) -> None:
    """Readiness with every check but the machine's quiet, so a test sees one blocker."""
    for name in ("_harness_problems", "_papaya_problems", "_config_problems", "_repo_problems"):
        monkeypatch.setattr(readiness, name, lambda problems: None)


# ── the forge is a readiness check, and its blocker is a ledger entry ─────────


def test_a_signed_out_forge_is_a_blocker_with_login_steps_and_its_clearing_is_said_once(
    github_repo, machine, monkeypatch
) -> None:
    _forge_only(monkeypatch)
    machine.failing.append(GH_STATUS)

    verdict = readiness.check()
    (problem,) = [p for p in verdict.problems if p.steps]
    assert problem.code == readiness.FORGE_UNAUTHENTICATED
    assert problem.scope == "github.com" and problem.repos == ("runtime",)
    # Not blocking the verdict: the sweep and work elsewhere go on.
    assert verdict.state != readiness.BLOCKED
    assert problem.steps[:3] == (
        "gh auth login --hostname github.com --git-protocol https --web",
        "gh auth setup-git",
        "gh auth status --hostname github.com",
    )
    assert "Nothing to restart" in problem.steps[-1]
    assert (["gh", "auth", "status", "--hostname", "github.com"], None) in machine.calls

    owner, clock = Owner(), WallClock()
    watch = blockers.Watch(say=owner.say, clock=clock, client_id=lambda: "", host="studio")

    async def scenario() -> None:
        await watch.round()
        assert len(owner.messages) == 1
        assert "Setup needed on studio" in owner.messages[0]
        assert "1. gh auth login --hostname github.com" in owner.messages[0]

        # The person signs in. The next round clears it, and says so.
        machine.failing.remove(GH_STATUS)
        clock.advance(minutes=5)
        await watch.round()
        assert len(owner.messages) == 2
        assert owner.messages[1] == "Fixed on studio: GitHub is not signed in on this machine."

        # And never again.
        clock.advance(minutes=5)
        await watch.round()
        assert len(owner.messages) == 2

    asyncio.run(scenario())
    assert blockers.current() == []


def test_a_blocker_is_not_said_again_within_a_day_unless_its_steps_change() -> None:
    ledger = blockers.Ledger()

    def verdict(*steps: str) -> readiness.Readiness:
        problem = readiness.Problem(
            code=readiness.DISK_LOW,
            summary="low disk",
            fix="free some",
            title="This machine is almost out of disk space",
            steps=steps,
        )
        return readiness.Readiness(state=readiness.BLOCKED, problems=[problem])

    ledger.observe(verdict("ppy worktree prune"), T0)
    opened, _ = ledger.due(T0)
    assert [b.code for b in opened] == [readiness.DISK_LOW]
    ledger.mark_reported(opened, [], T0)

    # Still there an hour, and twenty-three hours, later: nothing new to say.
    for hours in (1, 23):
        ledger.observe(verdict("ppy worktree prune"), T0 + timedelta(hours=hours))
        assert ledger.due(T0 + timedelta(hours=hours)) == ([], [])

    # The remedy changed: that is news at once.
    ledger.observe(verdict("ppy worktree prune", "df -h ~"), T0 + timedelta(hours=23))
    opened, _ = ledger.due(T0 + timedelta(hours=23))
    assert [b.steps for b in opened] == [["ppy worktree prune", "df -h ~"]]
    ledger.mark_reported(opened, [], T0 + timedelta(hours=23))
    assert ledger.due(T0 + timedelta(hours=46)) == ([], [])

    # A day after it was last said, it is said again.
    later = T0 + timedelta(hours=47)
    ledger.observe(verdict("ppy worktree prune", "df -h ~"), later)
    assert [b.code for b in ledger.due(later)[0]] == [readiness.DISK_LOW]


# ── the three ways it reaches the owner ─────────────────────────────────────


def test_blockers_ride_on_the_answered_hello_and_on_status_redacted(
    set_up, github_repo, client_home, machine, monkeypatch, privacy_leaks
) -> None:
    """The desktop app's "Setup needed on this Mac" card, from both messages."""
    machine.failing.append(GH_STATUS)
    real_steps = readiness._login_steps
    # A step that picked up a home path from somewhere must not carry it out.
    monkeypatch.setattr(
        readiness,
        "_login_steps",
        lambda host: [*real_steps(host), f"ls {privacy_leaks['home']}/.config/gh"],
    )

    stdout_read, stdout_write = os.pipe()
    stdin_read, stdin_write = os.pipe()
    stdout = os.fdopen(stdout_write, "w", buffering=1)
    host = Host(stdout_read, stdin_write)
    host.start()
    harness = Harness(FakeEvents([]))
    ticks = Ticks()
    options = serve.parse_args(["--supervised", "--working-directory", str(client_home.work_dir)])

    def statuses_with(code: str | None) -> list[dict[str, Any]]:
        found = []
        for message in host.of_type("status"):
            codes = [b["code"] for b in message.get("runtime", {}).get("blockers", [])]
            if (code in codes) if code else not codes:
                found.append(message)
        return found

    async def scenario() -> int:
        runner = asyncio.create_task(
            serve.run(
                options,
                stdout=stdout,
                stderr=io.StringIO(),
                extra={**harness.extra(), "stdin_fd": stdin_read},
                blocker_seams={"sleep": ticks.sleep, "client_id": lambda: ""},
            )
        )
        await _until(lambda: statuses_with(readiness.FORGE_UNAUTHENTICATED), what="a status")
        # Signed in; the next round clears it and the host hears a fresh status.
        machine.failing.remove(GH_STATUS)
        ticks.tick()
        await _until(lambda: statuses_with(None), what="a status with no blockers")
        os.close(stdin_write)
        return await runner

    try:
        assert asyncio.run(scenario()) == 0
    finally:
        stdout.close()
        host.join(timeout=scale(5.0))

    hello = host.of_type("hello")[0]
    (blocker,) = hello["runtime"]["blockers"]
    assert set(blocker) == {"code", "title", "steps", "since"}
    assert blocker["code"] == readiness.FORGE_UNAUTHENTICATED
    assert blocker["title"] == "GitHub is not signed in on this machine"
    assert blocker["steps"][0] == "gh auth login --hostname github.com --git-protocol https --web"
    assert "ls ~/code/secret-project/.config/gh" in blocker["steps"]
    assert statuses_with(readiness.FORGE_UNAUTHENTICATED)[0]["runtime"]["blockers"] == [blocker]
    assert leaked(json.dumps(host.messages)) == []


def test_a_pickup_on_a_signed_out_forge_comments_neutrally_and_hands_the_item_back(
    set_up, github_repo, client_home, machine
) -> None:
    machine.failing.append(GH_STATUS)
    harness = Harness(FakeEvents([EVENT]))
    papaya_api = FakePapaya()
    turns = FakeTurns()

    async def scenario() -> int:
        runner = test_serve._serve_ticket(harness, client_home, _runner(turns, papaya_api))
        await _until(lambda: harness.results, what="the job to be refused")
        harness.loop.request_stop()
        return await runner

    assert asyncio.run(scenario()) == 0

    # Handed back: released as declined, so the hosted agent or another machine keeps it.
    assert harness.results[0]["exit_code"] == 75
    assert harness.events.releases == [(SUBJECT, harness.loop.session_id, True)]
    assert turns.calls == [], "a turn ran for work this machine cannot deliver"
    conn = init_db()
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    conn.close()

    # One comment, and it names nothing: not the blocker, the forge, or a command.
    assert papaya_api.comments() == [("item-9", blockers.TICKET_COMMENT)]
    comment = papaya_api.comments()[0][1].lower()
    for word in ("gh", "github", "forge", "sign", "auth", "login", blockers.short_hostname()):
        assert word.lower() not in comment.split() and word.lower() not in comment

    # Offered again while the same blocker stands: refused, and no second comment.
    runner = _runner(FakeTurns(), papaya_api)
    event_file = client_home.path / "again.json"
    event_file.write_text(json.dumps(EVENT), encoding="utf-8")
    again = runner.take(SimpleNamespace(event_file=str(event_file), env=JOB_ENV))
    assert isinstance(again, serve.Declined) and again.setup
    runner._setup_comment(again.event, again.verdict, JOB_ENV)
    assert len(papaya_api.comments()) == 1


# ── a GitHub sign-in without a terminal ─────────────────────────────────────


class FakeGitHub:
    """GitHub's device-flow endpoints: a code, then pending once, then a token."""

    TOKEN = "gho_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"

    def __init__(self) -> None:
        self.polls = 0
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, fields: dict[str, str]) -> dict[str, Any]:
        self.requests.append((url, dict(fields)))
        if url.endswith("/login/device/code"):
            return {
                "device_code": "dev-123",
                "user_code": "WDJB-MJHT",
                "verification_uri": "https://github.com/login/device",
                "interval": 5,
                "expires_in": 900,
            }
        self.polls += 1
        if self.polls == 1:
            return {"error": "authorization_pending"}
        return {"access_token": self.TOKEN, "token_type": "bearer"}


def test_the_device_flow_reports_the_code_polls_installs_the_token_and_clears_the_blocker(
    github_repo, machine, monkeypatch, caplog
) -> None:
    _forge_only(monkeypatch)
    machine.failing.append(GH_STATUS)
    github = FakeGitHub()
    real_run = machine.run

    def gh(argv, timeout: float = 20.0, input: str | None = None):
        status = real_run(argv, timeout, input)
        if argv[:3] == ["gh", "auth", "login"] and input == FakeGitHub.TOKEN:
            machine.failing.remove(GH_STATUS)  # gh now holds the sign-in
        return status

    machine.run = gh
    owner = Owner()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    watch = blockers.Watch(
        say=owner.say,
        clock=WallClock(),
        sleep=sleep,
        client_id=lambda: "Iv1.papaya",
        device_flow=lambda client_id, host: blockers.DeviceFlow(
            client_id, host, http=github, run=machine.run
        ),
        host="studio",
    )

    async def scenario() -> None:
        await watch.round()
        await watch.settle()

    asyncio.run(scenario())

    # Reported: the code and where to enter it, instead of the manual sequence.
    first = owner.messages[0]
    assert "open https://github.com/login/device" in first
    assert "enter the code WDJB-MJHT" in first
    assert "gh auth login" not in first
    # Polled at GitHub's interval until authorised.
    assert github.polls == 2 and slept[:2] == [5.0, 5.0]
    assert github.requests[0][1] == {"client_id": "Iv1.papaya", "scope": "repo read:org workflow"}
    # Installed into gh on stdin, and nowhere else.
    login = ["gh", "auth", "login", "--hostname", "github.com", "--with-token"]
    assert (login, FakeGitHub.TOKEN) in machine.calls
    # Cleared, and said once.
    assert owner.messages[-1] == "Fixed on studio: GitHub is not signed in on this machine."
    assert blockers.current() == []
    for surface in (*owner.messages, blockers.ledger_path().read_text(), caplog.text):
        assert FakeGitHub.TOKEN not in surface
    assert all(FakeGitHub.TOKEN not in " ".join(argv) for argv, _ in machine.calls)


# ── nothing private in any of it ────────────────────────────────────────────


def test_the_privacy_fixture_never_reaches_the_dm_the_protocol_or_the_comment(
    ppy_home, client_home, dm, privacy_leaks
) -> None:
    leaks = privacy_leaks
    leaky = readiness.Problem(
        code=readiness.FORGE_UNAUTHENTICATED,
        summary=f"gh said: token {leaks['token']} for {leaks['email']} in {leaks['home']}",
        fix=f"see {leaks['home']}",
        owner=readiness.USER,
        title=f"GitHub is not signed in for {leaks['email']}",
        steps=(
            f"gh auth login --with-token {leaks['token']}",
            f"cd {leaks['home']}",
            f"git config user.email {leaks['email']}",
            leaks["diff"],
        ),
        scope="github.com",
        repos=("runtime",),
    )
    verdict = readiness.Readiness(state=readiness.BLOCKED, problems=[leaky])
    # The leaks are really there to be caught.
    assert leaked(" ".join(leaky.steps)) != []

    # The DM: the start-up readiness report and the blockers, in one message.
    async def start() -> None:
        await serve.report_readiness(verdict, SimpleNamespace(api=object()))

    asyncio.run(start())
    assert len(dm.posts) == 1
    assert leaked(dm.posts[0][1]) == [], dm.posts[0][1]
    assert "[redacted]" in dm.posts[0][1]

    # The protocol field, on `hello` and `status`.
    stream = io.StringIO()
    writer = serve.protocol_writer(stream)
    writer.send("hello", protocol=1)
    writer.send("status", phase="listening")
    messages = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert all(m["runtime"]["blockers"] for m in messages)
    assert leaked(stream.getvalue()) == [], stream.getvalue()

    # The comment on a ticket refused for it.
    papaya_api = FakePapaya()
    runner = _runner(FakeTurns(), papaya_api)
    event = papaya_events.PapayaEvent(
        id=101,
        kind="work_item.assigned",
        subject="work_item:item-9",
        payload={},
        work_item_id="item-9",
    )
    runner._setup_comment(event, verdict, JOB_ENV)
    assert [body for _item, body in papaya_api.comments()] == [blockers.TICKET_COMMENT]
    assert leaked(json.dumps(papaya_api.comments())) == []


# ── locally visible ─────────────────────────────────────────────────────────


def test_ppy_blockers_and_ppy_doctor_list_each_blocker_with_its_steps(
    github_repo, machine, monkeypatch, capsys
) -> None:
    from papaya_agent_runtime import cli
    from papaya_agent_runtime.setup import doctor

    _forge_only(monkeypatch)
    machine.failing.append(GH_STATUS)

    assert cli.main(["blockers"]) == 1
    out = capsys.readouterr().out
    assert "forge_unauthenticated: GitHub is not signed in on this machine" in out
    assert "  1. gh auth login --hostname github.com --git-protocol https --web" in out

    monkeypatch.setattr(doctor, "discover", lambda: test_serve._harness_report(["claude"]))
    text = doctor.render_text(doctor.collect())
    assert "setup needed" in text
    assert "gh auth setup-git" in text

    machine.failing.remove(GH_STATUS)
    assert cli.main(["blockers"]) == 0
    assert "no blockers" in capsys.readouterr().out
