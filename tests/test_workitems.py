"""Comments and edits on a work item reach its manager, in `ppy serve` and in a session.

Serve read comments only for a ticket it held and no mode read edits, so a worker could
build against a description rewritten an hour ago. `workitems` is the one check.
"""

from __future__ import annotations

import json

import pytest

from papaya_agent_runtime import owed, papaya, papaya_events, supervision, watch, workitems
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.state import init_db, store

AGENT = "agent-1"


def _ticket(conn, *, worker_status="in_progress", phase="handed_over"):
    run_id = store.create_run(conn, "r")
    ticket = store.add_task(conn, run_id=run_id, title="ticket")
    store.set_task_phase(conn, ticket, phase)
    store.set_task_env(
        conn,
        ticket,
        papaya_events.PAPAYA_EVENT_METADATA,
        json.dumps({"work_item_id": "w1", "subject": "work_item:w1", "kind": "work_item.assigned"}),
    )
    worker = store.add_task(conn, run_id=run_id, title="build")
    store.set_task_status(conn, worker, worker_status)
    return ticket, worker


@pytest.fixture
def papaya_world(ppy_home, monkeypatch):
    world = {
        "comments": [{"id": "c1", "author_type": "user", "author_id": "u1", "body": "first"}],
        "item": {"title": "T", "description": "build the thing", "status": "in_progress"},
    }
    monkeypatch.setattr(
        papaya_events,
        "list_work_item_comments",
        lambda event, environ=None: list(world["comments"]),
    )
    monkeypatch.setattr(
        papaya_events,
        "hydrate_work_item",
        lambda event, environ=None: papaya_events.PapayaEvent(
            event.id,
            event.kind,
            event.subject,
            {"work_item": dict(world["item"])},
            event.work_item_id,
        ),
    )
    return world


def _check():
    return workitems.check_untracked(env={"PAPAYA_AGENT_TOKEN": "t"}, agent_id=AGENT)


def test_the_first_look_wakes_nothing_and_a_later_comment_is_owed_work(papaya_world) -> None:
    conn = init_db()
    ticket, worker = _ticket(conn)

    assert _check() == []
    papaya_world["comments"].append(
        {"id": "c2", "author_type": "user", "author_id": "u2", "body": "use the blue button"}
    )
    [line] = _check()

    assert "use the blue button" in line
    [item] = [i for i in owed.collect(init_db()) if i.status == "work_item_changed"]
    assert item.task_id == ticket and str(worker) in item.reason
    assert _check() == []  # heard once


def test_an_edit_to_the_description_is_heard_and_status_moves_are_not(papaya_world) -> None:
    conn = init_db()
    _ticket(conn)
    _check()

    papaya_world["item"]["status"] = "review"
    assert _check() == []
    papaya_world["item"]["description"] = "build the other thing"
    [line] = _check()

    assert "description changed" in line and "build the other thing" in line


def test_this_agents_own_comments_wake_nothing(papaya_world) -> None:
    conn = init_db()
    _ticket(conn)
    _check()
    papaya_world["comments"].append(
        {"id": "c3", "author_type": "agent", "author_id": AGENT, "body": "PR is up"}
    )
    assert _check() == []


def test_a_held_ticket_is_left_to_its_answer_turn(papaya_world) -> None:
    conn = init_db()
    ticket, _worker = _ticket(conn, phase="dispatched")
    workitems.check_untracked(env={}, held={ticket}, agent_id=AGENT)
    assert workitems.tracked()[0].ticket_task_id == ticket
    assert workitems.check_untracked(env={}, held={ticket}, agent_id=AGENT) == []


def test_a_merged_or_closed_workers_item_is_no_longer_tracked(papaya_world) -> None:
    conn = init_db()
    _ticket(conn, worker_status="closed")
    assert workitems.tracked() == []


def test_heard_or_a_steer_on_the_worker_clears_it(papaya_world) -> None:
    conn = init_db()
    ticket, worker = _ticket(conn)
    _check()
    papaya_world["comments"].append({"id": "c4", "author_type": "user", "body": "one"})
    _check()
    assert workitems.unheard()

    assert main(["heard", str(ticket), "--note", "a question for the PM, not the worker"]) == 0
    assert workitems.unheard() == []

    papaya_world["comments"].append({"id": "c5", "author_type": "user", "body": "two"})
    _check()
    store.append_event(conn, kind="steer", payload={"message": "use blue"}, task_id=worker)
    assert workitems.unheard() == []


def test_the_heartbeat_listens_only_without_serve_and_with_a_connection(
    ppy_home, monkeypatch
) -> None:
    calls = []
    monkeypatch.setattr(workitems, "check_untracked", lambda **k: calls.append(k) or ["x"])
    monkeypatch.setattr(papaya, "identity", lambda: None)

    monkeypatch.setattr(supervision, "serve_running", lambda: True)
    assert watch.listen_step(None) == []
    monkeypatch.setattr(supervision, "serve_running", lambda: False)
    monkeypatch.setattr(papaya, "agent_env", lambda: {})
    assert watch.listen_step(None) == []
    monkeypatch.setattr(papaya, "agent_env", lambda: {"PAPAYA_AGENT_TOKEN": "t"})
    assert watch.listen_step(None) == ["x"] and len(calls) == 1
