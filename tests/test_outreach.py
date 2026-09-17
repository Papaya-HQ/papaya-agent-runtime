"""Anything waiting on a person is chased until it is answered, in both modes.

2026-09-17: the PAP-242 pill copy was posted on its ticket as a decision already made,
filed as a todo "waiting on user", and mentioned in passing by the next session; Shane
found it by asking. Under `ppy serve` there is no session to ask in. The outreach
procedure (`outreach.py`) is one decision both modes run: what waits on a person, whether
they were told, where, and when to say it again.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from papaya_agent_runtime import (
    blockers,
    capability_requests,
    hooks,
    outreach,
    papaya_events,
    readiness,
    rounds,
    supervision,
    watch,
)
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.state import init_db, store

NOW = datetime(2026, 9, 17, 19, 25, tzinfo=UTC)


@pytest.fixture
def home(ppy_home, monkeypatch):
    monkeypatch.delenv("PPY_DEV", raising=False)
    monkeypatch.delenv(outreach.REPEAT_ENV, raising=False)
    monkeypatch.setattr(supervision, "serve_running", lambda: False)
    monkeypatch.setattr(outreach, "notify_desktop", lambda text: False)
    return ppy_home


class Channels:
    """Fake channels: what was said where, and whether each lands."""

    def __init__(self, *, dm: bool = True, ticket: bool = True, desktop: bool = False) -> None:
        self.dm_lands, self.ticket_lands, self.desktop_lands = dm, ticket, desktop
        self.dms: list[str] = []
        self.tickets: list[tuple[str, str]] = []
        self.desktop: list[str] = []

    def post_dm(self, text: str) -> bool:
        self.dms.append(text)
        return self.dm_lands

    def post_ticket(self, item: str, body: str, *, environ=None) -> bool:
        self.tickets.append((item, body))
        return self.ticket_lands

    def notify(self, text: str) -> bool:
        self.desktop.append(text)
        return self.desktop_lands


def _ticket(conn, work_item: str = "PAP-242") -> int:
    """A ticket task (phase set) for a work item, the way serve records one."""
    run_id = store.create_run(conn, f"ticket {work_item}")
    task_id = store.add_task(conn, run_id=run_id, title=f"ticket {work_item}")
    store.update_task_fields(conn, task_id, phase="dispatched")
    store.set_task_env(
        conn,
        task_id,
        papaya_events.PAPAYA_EVENT_METADATA,
        json.dumps({"work_item_id": work_item, "subject": f"work_item:{work_item}"}),
    )
    return task_id


def _worker(conn, run_id: int | None = None) -> int:
    run_id = run_id or store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title="build the pill")
    store.set_task_status(conn, task_id, "in_progress")
    return task_id


# ── what is waiting on a person ──────────────────────────────────────────────


def test_a_todo_blocked_on_the_user_is_an_ask_on_its_work_item(home) -> None:
    conn = init_db()
    ticket = _ticket(conn)
    todo = store.add_todo(
        conn, "confirm the pill copy table", task_id=ticket, blocked_on="user:wording"
    )
    asks = outreach.collect(conn)
    assert [a.key for a in asks] == [f"todo:{todo}"]
    ask = asks[0]
    assert ask.kind == outreach.DECISION
    assert ask.work_item_id == "PAP-242"
    assert "confirm the pill copy table" in ask.text and "wording" in ask.text
    assert f"ppy todo done {todo}" in ask.how


def test_a_worker_under_a_ticket_reaches_the_ticket_work_item(home) -> None:
    conn = init_db()
    ticket = _ticket(conn, "PAP-239")
    worker = _worker(conn, run_id=store.get_task(conn, ticket)["run_id"])
    assert outreach.work_item_of(conn, worker) == "PAP-239"


def test_a_tracked_task_reaches_its_papaya_record(home) -> None:
    from papaya_agent_runtime import tracker

    conn = init_db()
    worker = _worker(conn)
    tracker.link_task(conn, worker, record="PAP-241", provider="papaya")
    assert outreach.work_item_of(conn, worker) == "PAP-241"
    assert outreach.work_item_of(conn, 9999) is None


def test_a_pending_capability_request_is_an_ask(home, monkeypatch) -> None:
    conn = init_db()
    worker = _worker(conn)
    monkeypatch.setattr(capability_requests, "decide", lambda program: capability_requests.PENDING)
    made = capability_requests.request(worker, "xcodegen", why="regenerate the project")
    asks = outreach.collect(init_db())
    ask = next(a for a in asks if a.kind == outreach.CAPABILITY)
    assert ask.key == f"capability:{made.id}"
    assert "`xcodegen`" in ask.text and "regenerate the project" in ask.text
    assert f"ppy capability approve {made.id}" in ask.how
    assert ask.since is not None


def test_a_decided_request_is_no_longer_an_ask(home, monkeypatch) -> None:
    conn = init_db()
    worker = _worker(conn)
    monkeypatch.setattr(capability_requests, "decide", lambda program: capability_requests.PENDING)
    made = capability_requests.request(worker, "xcodegen", why="")
    monkeypatch.setattr(capability_requests, "_tell_worker", lambda found: None)
    monkeypatch.setattr(capability_requests, "_grant_for_install", lambda g, by: None)
    capability_requests.decide_request(made.id, approve=True, always=False, reason="")
    assert [a for a in outreach.collect(init_db()) if a.kind == outreach.CAPABILITY] == []


def test_a_request_on_a_task_that_is_over_is_nobody_s_to_answer(home, monkeypatch) -> None:
    conn = init_db()
    worker = _worker(conn)
    monkeypatch.setattr(capability_requests, "decide", lambda program: capability_requests.PENDING)
    capability_requests.request(worker, "chrome-devtools-axi", why="")
    conn = init_db()
    assert [a.kind for a in outreach.collect(conn)] == [outreach.CAPABILITY]
    store.set_task_status(conn, worker, "delivered")
    assert outreach.collect(conn) == []


def test_a_pull_request_the_lane_gave_up_on_is_an_ask(home, monkeypatch) -> None:
    conn = init_db()
    worker = _worker(conn)
    store.set_task_status(conn, worker, "delivered")
    monkeypatch.setattr(supervision, "prs_needing_a_person", lambda: [(worker, "review requested")])
    asks = outreach.collect(conn)
    assert [a.kind for a in asks] == [outreach.PULL_REQUEST]
    assert asks[0].key == f"pr:{worker}" and "review requested" in asks[0].text


# ── the ledger: said once per round, again on a clock, resolved when gone ────


def test_a_new_ask_is_said_the_round_it_appears_on_the_ticket_and_in_the_dm(home) -> None:
    conn = init_db()
    ticket = _ticket(conn)
    todo = store.add_todo(conn, "confirm the pill copy table", task_id=ticket, blocked_on="user")
    channels = Channels()
    lines = outreach.step(
        conn, now=NOW, host="reptar", dm=channels.post_dm, ticket=channels.post_ticket
    )
    assert channels.tickets and channels.tickets[0][0] == "PAP-242"
    assert "confirm the pill copy table" in channels.tickets[0][1]
    assert f"ppy todo done {todo}" in channels.tickets[0][1]
    assert len(channels.dms) == 1
    assert "Waiting on you (reptar)" in channels.dms[0]
    assert "[PAP-242] confirm the pill copy table" in channels.dms[0]
    assert any(line.startswith("said to a person (dm, ticket)") for line in lines)
    row = outreach.open_rows(conn)[0]
    assert row["said_count"] == 1 and json.loads(row["said_via"]) == ["dm", "ticket"]
    said = conn.execute(
        "SELECT payload FROM events WHERE kind = ?", (outreach.SAID_EVENT,)
    ).fetchall()
    assert len(said) == 1


def test_an_ask_already_said_is_not_said_again_until_the_repeat_interval(home) -> None:
    conn = init_db()
    store.add_todo(conn, "which wording", blocked_on="user")
    channels = Channels()
    outreach.step(conn, now=NOW, dm=channels.post_dm, ticket=channels.post_ticket)
    outreach.step(
        conn, now=NOW + timedelta(minutes=30), dm=channels.post_dm, ticket=channels.post_ticket
    )
    assert len(channels.dms) == 1
    later = NOW + timedelta(seconds=outreach.REPEAT_AFTER_SECONDS)
    outreach.step(conn, now=later, dm=channels.post_dm, ticket=channels.post_ticket)
    assert len(channels.dms) == 2
    assert "reminder 1" in channels.dms[1]
    assert outreach.open_rows(conn)[0]["said_count"] == 2


def test_the_repeat_interval_comes_from_the_environment(home, monkeypatch) -> None:
    monkeypatch.setenv(outreach.REPEAT_ENV, "600")
    assert outreach.repeat_after_seconds() == 600.0
    monkeypatch.setenv(outreach.REPEAT_ENV, "5")
    assert outreach.repeat_after_seconds() == 60.0  # never a flood
    monkeypatch.setenv(outreach.REPEAT_ENV, "soon")
    assert outreach.repeat_after_seconds() == outreach.REPEAT_AFTER_SECONDS


def test_an_answered_ask_is_resolved_and_never_said_again(home) -> None:
    conn = init_db()
    todo = store.add_todo(conn, "which wording", blocked_on="user")
    channels = Channels()
    outreach.step(conn, now=NOW, dm=channels.post_dm, ticket=channels.post_ticket)
    store.update_todo(conn, todo, status="done")
    lines = outreach.step(
        conn,
        now=NOW + timedelta(days=1),
        dm=channels.post_dm,
        ticket=channels.post_ticket,
    )
    assert any(line.startswith("no longer waiting on a person") for line in lines)
    assert outreach.open_rows(conn) == []
    assert len(channels.dms) == 1
    assert outreach.summary(conn) == []


def test_a_channel_that_did_not_land_is_not_recorded_as_said(home) -> None:
    conn = init_db()
    store.add_todo(conn, "which wording", blocked_on="user")
    channels = Channels(dm=False, ticket=False)
    lines = outreach.step(conn, now=NOW, dm=channels.post_dm, ticket=channels.post_ticket)
    assert outreach.open_rows(conn)[0]["said_count"] == 0
    assert any("nowhere it could reach" in line for line in lines)
    assert any("ppy papaya connect" in line for line in lines)
    # The next round tries again rather than waiting out the repeat interval.
    channels.dm_lands = True
    outreach.step(
        conn, now=NOW + timedelta(minutes=1), dm=channels.post_dm, ticket=channels.post_ticket
    )
    assert outreach.open_rows(conn)[0]["said_count"] == 1


def test_a_session_counts_as_a_channel_only_when_nothing_remote_landed(home) -> None:
    conn = init_db()
    store.add_todo(conn, "which wording", blocked_on="user")
    channels = Channels(dm=False, ticket=False)
    outreach.step(conn, now=NOW, dm=channels.post_dm, ticket=channels.post_ticket, session=True)
    assert json.loads(outreach.open_rows(conn)[0]["said_via"]) == ["session"]
    conn2 = init_db()
    store.add_todo(conn2, "another", blocked_on="user")
    landed = Channels()
    outreach.step(conn2, now=NOW, dm=landed.post_dm, ticket=landed.post_ticket, session=True)
    rows = {r["key"]: json.loads(r["said_via"]) for r in outreach.open_rows(conn2)}
    assert "session" not in rows[[k for k in rows if k != "todo:1"][0]]


def test_the_message_names_the_machine_the_wait_and_how_to_unblock_each(home, monkeypatch) -> None:
    conn = init_db()
    worker = _worker(conn)
    monkeypatch.setattr(capability_requests, "decide", lambda program: capability_requests.PENDING)
    made = capability_requests.request(worker, "xcodegen", why="regenerate")
    conn = init_db()
    todo = store.add_todo(conn, "ship without contributors?", task_id=worker, blocked_on="user")
    conn.execute("UPDATE todos SET created_at = ? WHERE id = ?", (NOW.isoformat(), todo))
    conn.execute("UPDATE events SET created_at = ? WHERE id = ?", (NOW.isoformat(), made.id))
    conn.commit()
    asks = outreach.collect(conn)
    outreach.observe(conn, asks, now=NOW)
    text = outreach.message(conn, asks, now=NOW + timedelta(hours=2), host="reptar")
    assert text.startswith("Waiting on you (reptar): 2 things nothing else can move.")
    assert "since 2h" in text
    assert f"ppy capability approve {made.id}" in text
    assert "ship without contributors?" in text
    assert "I will say this again every 2h until each is answered." in text


def test_nothing_private_leaves_in_the_words(home) -> None:
    conn = init_db()
    store.add_todo(
        conn, "token=ghp_abcdefghijklmnopqrstuvwxyz leaked in /Users/shane/x", blocked_on="user"
    )
    asks = outreach.collect(conn)
    outreach.observe(conn, asks, now=NOW)
    text = outreach.message(conn, asks, now=NOW, host="reptar")
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in text and "/Users/shane" not in text


# ── both modes reach it ──────────────────────────────────────────────────────


def test_the_heartbeat_says_what_is_due_and_shows_what_waits(home, monkeypatch) -> None:
    conn = init_db()
    store.add_todo(conn, "which wording", blocked_on="user")
    channels = Channels()
    monkeypatch.setattr(outreach, "post_dm", channels.post_dm)
    monkeypatch.setattr(outreach, "post_ticket", channels.post_ticket)
    lines = watch.outreach_step(conn, NOW)
    assert channels.dms and any(line.startswith("said to a person") for line in lines)
    snapshot = watch.tick(conn, now=NOW)
    assert snapshot["waiting_on_a_person"][0]["text"] == "which wording"
    assert snapshot["waiting_on_a_person"][0]["said_count"] == 1
    assert "waiting on a person: which wording (said 1x)" in watch.render(snapshot)


def test_the_heartbeat_leaves_it_to_a_running_serve(home, monkeypatch) -> None:
    conn = init_db()
    store.add_todo(conn, "which wording", blocked_on="user")
    monkeypatch.setattr(supervision, "serve_running", lambda: True)
    channels = Channels()
    monkeypatch.setattr(outreach, "post_dm", channels.post_dm)
    assert watch.outreach_step(conn, NOW) == []
    assert channels.dms == []


def test_the_session_start_hook_lists_what_waits_on_a_person(home) -> None:
    conn = init_db()
    store.add_todo(conn, "which wording", blocked_on="user")
    context = hooks.outreach_context(conn)
    assert context is not None
    assert context.startswith("WAITING ON A PERSON (1)")
    assert "which wording" in context and "not said yet" in context
    assert hooks.outreach_context(init_db()) is not None
    conn.execute("UPDATE todos SET status = 'done'")
    conn.commit()
    outreach.step(init_db(), now=NOW, dm=lambda t: False, ticket=lambda i, b, environ=None: False)
    assert hooks.outreach_context(init_db()) is None


def test_the_stop_hook_bounces_once_with_what_nothing_remote_reached(home, monkeypatch) -> None:
    conn = init_db()
    store.add_todo(conn, "which wording", blocked_on="user")
    channels = Channels(dm=False, ticket=False)
    monkeypatch.setattr(outreach, "post_dm", channels.post_dm)
    monkeypatch.setattr(outreach, "post_ticket", channels.post_ticket)
    reason = hooks.outreach_stop_step(conn)
    assert reason is not None and "which wording" in reason
    assert "Put each in your reply" in reason
    # Said in the session now: the next Stop is not held for it.
    assert hooks.outreach_stop_step(conn) is None
    # And a Stop with the workspace reachable says it there and holds nothing.
    conn2 = init_db()
    store.add_todo(conn2, "another", blocked_on="user")
    landed = Channels()
    monkeypatch.setattr(outreach, "post_dm", landed.post_dm)
    monkeypatch.setattr(outreach, "post_ticket", landed.post_ticket)
    assert hooks.outreach_stop_step(conn2) is None
    assert landed.dms


def test_the_stop_hook_reason_reaches_the_harness(home, monkeypatch) -> None:
    conn = init_db()
    store.add_todo(conn, "which wording", blocked_on="user")
    monkeypatch.setattr(outreach, "post_dm", lambda text: False)
    monkeypatch.setattr(outreach, "post_ticket", lambda item, body, environ=None: False)
    monkeypatch.setattr(hooks, "owed_stop_reasons", lambda conn: [])
    result = hooks.handle_hook("stop", {})
    assert result.get("decision") == "block" and "which wording" in result["reason"]


def test_ppy_outreach_lists_and_runs(home, monkeypatch, capsys) -> None:
    conn = init_db()
    store.add_todo(conn, "which wording", blocked_on="user")
    channels = Channels()
    monkeypatch.setattr(outreach, "post_dm", channels.post_dm)
    monkeypatch.setattr(outreach, "post_ticket", channels.post_ticket)
    assert main(["outreach"]) == 0
    out = capsys.readouterr().out
    assert "which wording" in out and "not said yet" in out
    assert main(["outreach", "run"]) == 0
    out = capsys.readouterr().out
    assert channels.dms and "said 1x via dm" in out
    assert main(["outreach", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["said_count"] == 1 and data[0]["said_via"] == ["dm"]
    assert main(["status"]) == 0
    assert "asks:    1 waiting on a person" in capsys.readouterr().out


def test_serve_rounds_say_it_through_their_own_connection(home, monkeypatch) -> None:
    conn = init_db()
    ticket = _ticket(conn)
    store.add_todo(conn, "confirm the pill copy table", task_id=ticket, blocked_on="user")
    said: dict[str, list] = {"dm": [], "tickets": []}

    async def post_dm(built, text):
        said["dm"].append(text)
        return True

    def post_ticket(item, body, *, environ=None):
        said["tickets"].append((item, body, environ))
        return True

    monkeypatch.setattr(rounds.serve, "_post_dm", post_dm)
    monkeypatch.setattr(rounds.serve, "_where", lambda: "reptar")
    monkeypatch.setattr(outreach, "post_ticket", post_ticket)

    class Runner:
        held: dict = {}

    lane = rounds.Rounds(
        built=object(),
        runner=Runner(),
        clock=lambda: NOW,
        papaya_env=lambda: {"PAPAYA_AGENT_TOKEN": "t"},
    )
    lines = asyncio.run(lane._outreach_lane(NOW))
    assert said["dm"] and "Waiting on you (reptar)" in said["dm"][0]
    assert said["tickets"][0][0] == "PAP-242"
    assert said["tickets"][0][2] == {"PAPAYA_AGENT_TOKEN": "t"}
    assert any(line.startswith("said to a person (dm, ticket)") for line in lines)
    assert outreach.open_rows(init_db())[0]["said_count"] == 1
    # The next round says nothing new.
    assert asyncio.run(lane._outreach_lane(NOW + timedelta(minutes=1))) == []
    assert len(said["dm"]) == 1


def test_the_ticket_path_does_not_say_it_a_second_time(home) -> None:
    conn = init_db()
    store.add_todo(conn, "which wording", blocked_on="user")
    assert rounds._outreach_said("todo:1") is False
    outreach.step(conn, now=NOW, dm=lambda t: True, ticket=lambda i, b, environ=None: False)
    assert rounds._outreach_said("todo:1") is True


def test_capability_requests_leave_the_blocker_dm_to_outreach(home) -> None:
    problem = readiness.Problem(
        code="capability_request_pending",
        summary="worker task 1 needs `xcodegen`",
        fix="approve",
        owner=readiness.USER,
        blocking=False,
        title="A worker needs `xcodegen`",
        steps=("ppy capability approve 1",),
    )
    other = readiness.Problem(
        code="forge_unauthenticated",
        summary="gh signed out",
        fix="gh auth login",
        owner=readiness.USER,
        title="gh is signed out",
        steps=("gh auth login",),
    )
    ledger = blockers.Ledger()
    ledger.observe(readiness.Readiness(state=readiness.DEGRADED, problems=[problem, other]), NOW)
    opened, _cleared = ledger.due(NOW)
    assert [b.code for b in opened] == ["forge_unauthenticated"]
    # Still in the ledger for the desktop app's card.
    assert {b.code for b in ledger.open.values()} == {
        "capability_request_pending",
        "forge_unauthenticated",
    }


def test_the_parity_registry_names_it_shared() -> None:
    from papaya_agent_runtime import parity

    found = next(c for c in parity.CAPABILITIES if c.name == "person_outreach")
    assert found.kind == parity.SHARED
    assert found.shared == "papaya_agent_runtime.outreach"
    assert "Rounds._outreach_lane" in found.serve
