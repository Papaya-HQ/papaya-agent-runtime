"""Without a Papaya connection the runtime is still a manager, and says once it is better with one.

The suite's machine is already not connected (`conftest._no_real_papaya_connection`),
so every test here is the standalone case without arranging anything. What each one
arranges is that nothing reaches Papaya: the work-item HTTP call is replaced by a spy
that records any attempt, and the tests assert it recorded none.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from types import SimpleNamespace

import pytest

from conftest import approve_with_description, wait_until
from papaya_agent_runtime import (
    cli,
    delivery,
    papaya_events,
    readiness,
    repos,
    rounds,
    serve,
    standalone,
    sweep,
)
from papaya_agent_runtime.config import ManagerProfile, MMConfig, WorkerCeiling, save_config
from papaya_agent_runtime.setup import discovery, doctor
from papaya_agent_runtime.state import init_db
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.server import SupervisorServer
from test_rounds import Timer, WallClock, _seams

NOTHING = {"harnesses": [], "requirements": [], "companions": []}


@pytest.fixture
def invite_on(monkeypatch):
    """The invitation as a person gets it: nobody silenced it."""
    monkeypatch.delenv(standalone.QUIET_ENV, raising=False)


@pytest.fixture
def papaya_calls(monkeypatch) -> list[str]:
    """Every attempt to reach Papaya's work-item API, which should be none."""
    attempts: list[str] = []

    def refuse(request, *args, **kwargs):
        attempts.append(getattr(request, "full_url", str(request)))
        raise AssertionError("a standalone runtime reached for Papaya")

    monkeypatch.setattr(papaya_events.urllib.request, "urlopen", refuse)
    return attempts


def _quiet_discovery(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "discover", lambda: NOTHING)
    monkeypatch.setattr(discovery, "discover", lambda: NOTHING)


def _configured() -> None:
    save_config(
        MMConfig(
            manager=ManagerProfile("claude", "opus", "high"),
            worker=WorkerCeiling("claude", "opus", "medium"),
        )
    )


def test_status_and_doctor_say_the_line_once_and_readiness_calls_it_info(
    ppy_home, invite_on, capsys, monkeypatch
) -> None:
    _quiet_discovery(monkeypatch)

    assert cli.main(["status"]) == 0
    assert capsys.readouterr().out.count(standalone.INVITE_LINE) == 1

    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert out.count(standalone.INVITE_LINE) == 1
    assert "https://trypapaya.ai" in standalone.INVITE_LINE

    # A later command that is not a session start says nothing about Papaya.
    cli.main(["repo", "list"])
    assert standalone.INVITE_LINE not in capsys.readouterr().out

    verdict = readiness.check()
    (note,) = [p for p in verdict.problems if p.code == readiness.PAPAYA_NOT_CONNECTED]
    assert note.info and not note.blocking and not note.steps
    assert note not in verdict.warnings and note not in verdict.blockers

    assert cli.main(["readiness", "--json"]) in (0, 1)
    listed = json.loads(capsys.readouterr().out)["problems"]
    assert {"code": readiness.PAPAYA_NOT_CONNECTED, "info": True}.items() <= next(
        p for p in listed if p["code"] == readiness.PAPAYA_NOT_CONNECTED
    ).items()


def test_the_invite_off_switch_silences_the_line(ppy_home, invite_on, capsys, monkeypatch) -> None:
    _quiet_discovery(monkeypatch)
    monkeypatch.setenv(standalone.QUIET_ENV, "1")
    assert cli.main(["status"]) == 0
    assert standalone.INVITE_LINE not in capsys.readouterr().out

    monkeypatch.delenv(standalone.QUIET_ENV)
    _configured()
    from papaya_agent_runtime.config import load_config

    cfg = load_config()
    cfg.papaya.invite = False
    save_config(cfg)
    assert load_config().papaya.invite is False
    assert cli.main(["status"]) == 0
    assert cli.main(["doctor"]) == 0
    assert standalone.INVITE_LINE not in capsys.readouterr().out
    assert standalone.invitation() is None


def _skipped(task_id: int) -> list[dict]:
    conn = init_db()
    try:
        rows = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id",
            (task_id, standalone.TICKET_STEP_SKIPPED),
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]
    finally:
        conn.close()


def test_a_local_task_goes_brief_to_delivery_with_no_papaya_call(
    ppy_home, source_repo, tmp_path, papaya_calls, capsys, monkeypatch
) -> None:
    brief = tmp_path / "brief.md"
    brief.write_text(
        "# Add a greeting\n\n## Goals\n\n- README says hello.\n\n## Intent\n\nA person asked.\n\n"
        "## In scope\n\nREADME.\n\n## Out of scope\n\nEverything else.\n",
        encoding="utf-8",
    )
    cli.main(["brief", "lint", str(brief)])
    capsys.readouterr()

    # The forge: a `gh` stand-in that opens a pull request and prints its URL.
    fake_gh = tmp_path / "fake-gh"
    fake_gh.write_text("#!/bin/sh\necho https://github.com/acme/source/pull/7\n", encoding="utf-8")
    fake_gh.chmod(0o755)
    monkeypatch.setattr(delivery, "_pr_tool", lambda: str(fake_gh))

    server = SupervisorServer()
    server.start_background()
    try:
        client = SupervisorClient(server.socket_path)
        wait_until(lambda: client.ping().get("ok"), 10, what="the supervisor", interval=0.05)
        added = repos.add_repo(source_repo)
        task_id = client.dispatch_task(
            repo=added.name, title="Add a greeting", instructions=brief.read_text()
        )["task_id"]
        wait_until(
            lambda: client.task_status(task_id)["task"]["status"] == "worker_done",
            15,
            what="the worker to finish",
            interval=0.1,
        )
        approve_with_description(task_id, "looks good")
        result = delivery.deliver(task_id, push=False, open_pr=True)
    finally:
        server.stop()

    assert result.pr_url == "https://github.com/acme/source/pull/7"
    assert papaya_calls == []
    recorded = _skipped(task_id)
    assert [r["phase"] for r in recorded] == [
        standalone.DISPATCHED,
        standalone.REVIEWED,
        standalone.DELIVERED,
    ]
    assert all(r["reason"] == standalone.NO_WORK_ITEM and r["steps"] for r in recorded)
    assert "set the work item to in_review" in recorded[-1]["steps"]


def test_a_ticket_worker_records_no_skipped_steps(ppy_home, source_repo) -> None:
    """Filed in a ticket's run, the ticket runner owns the item's steps: nothing is skipped."""
    added = repos.add_repo(source_repo)
    conn = init_db()
    try:
        from papaya_agent_runtime.state import store

        run_id = store.create_run(conn, "ticket")
        repo = conn.execute("SELECT id FROM repos WHERE name = ?", (added.name,)).fetchone()
        ticket = store.add_task(
            conn, run_id=run_id, title="ticket", repo_id=repo["id"], provider="claude",
            model="opus", reasoning="high",
        )  # fmt: skip
        store.set_task_env(conn, ticket, papaya_events.PAPAYA_EVENT_KEY, "k", source="papaya_event")
        worker = store.add_task(
            conn, run_id=run_id, title="worker", repo_id=repo["id"], provider="claude",
            model="opus", reasoning="high",
        )  # fmt: skip
        assert standalone.skip_if_local(conn, worker, standalone.DELIVERED) is False
    finally:
        conn.close()
    assert _skipped(worker) == []


def test_serve_without_a_connection_runs_rounds_and_says_what_is_off(
    ppy_home, invite_on, papaya_calls, monkeypatch
) -> None:
    _configured()
    monkeypatch.setattr(readiness, "check", lambda: readiness.Readiness(state=readiness.READY))

    def no_sweep(*_args, **_kwargs):
        raise AssertionError("the sweep runs only with a connection")

    async def no_listener(*_args, **_kwargs):
        raise AssertionError("the event loop is built only with a connection")

    reclaims: list[str] = []

    def no_reclaim(*_args, **_kwargs):
        reclaims.append("reclaim")
        raise AssertionError("reclaiming a ticket is a Papaya reserve")

    monkeypatch.setattr(sweep, "Sweeper", no_sweep)
    # The reclaim on connect's Papaya calls all go through this.
    monkeypatch.setattr(sweep, "PapayaReads", no_sweep)
    monkeypatch.setattr(rounds, "reclaimable", no_reclaim)
    monkeypatch.setattr(serve, "_build", no_listener)

    timer, clock, pruned = Timer(), WallClock(), []
    stderr = io.StringIO()
    server = SimpleNamespace(on_shutdown=None, sweep_handler=None)

    async def scenario() -> int:
        task = asyncio.create_task(
            serve.run(
                serve.parse_args([]),
                stdout=io.StringIO(),
                stderr=stderr,
                extra={},
                server=server,
                rounds_seams=_seams(timer, clock, pruned),
            )
        )
        await timer.round()
        assert server.on_shutdown is not None
        server.on_shutdown()
        return await asyncio.wait_for(task, 10)

    started = time.monotonic()
    assert asyncio.run(scenario()) == 0
    assert time.monotonic() - started < 10

    said = stderr.getvalue()
    assert f"ppy serve: {serve.STANDALONE_START}" in said
    assert "sweep, event loop and Papaya DMs off" in said
    assert said.count(standalone.INVITE_LINE) == 1
    # The round ran: hygiene's first pass over every slot.
    assert None in pruned
    assert papaya_calls == []
    # Neither the rounds' reclaim on start and every round, nor the sweep's reclaim on
    # connect, is attempted without a connection.
    assert reclaims == []
    assert "reclaim" not in said


def test_the_session_offers_to_set_papaya_up_and_knows_how(monkeypatch) -> None:
    """Shane, 2026-09-22: help the person set the client up, don't just mention it."""
    from papaya_agent_runtime import hooks

    monkeypatch.delenv("PPY_DEV", raising=False)
    monkeypatch.delenv(standalone.QUIET_ENV, raising=False)
    said = hooks.invitation_context({"source": "startup"})
    assert said is not None
    assert "offer to set it up" in said
    assert "Do not ask the person to connect" not in said
    for step in ("ppy papaya connect", "npx papaya-agent", "`--agent`", "Approve", "/mcp"):
        assert step in said
    assert standalone.INVITE_LINE in said


def test_a_launched_session_is_not_told_twice_but_still_knows_how(monkeypatch) -> None:
    from papaya_agent_runtime import hooks

    monkeypatch.delenv("PPY_DEV", raising=False)
    monkeypatch.delenv(standalone.QUIET_ENV, raising=False)
    monkeypatch.setenv("PPY_MANAGER_SESSION", "1")
    said = hooks.invitation_context({"source": "startup"})
    assert said is not None and "do not repeat it" in said
    assert standalone.INVITE_LINE not in said
    assert "ppy papaya connect" in said
