"""Steering reaches the worker, or says plainly why it cannot — never vanishes."""

from __future__ import annotations

import json
import time

import pytest

from conftest import scale
from papaya_agent_runtime import repos
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.server import SupervisorServer


@pytest.fixture
def server(ppy_home):
    srv = SupervisorServer()
    srv.start_background()
    client = SupervisorClient(srv.socket_path)
    for _ in range(50):
        try:
            if client.ping().get("ok"):
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    yield srv, client
    srv.stop()


def _wait_status(client, task_id, wanted, timeout=15.0):
    deadline = time.monotonic() + scale(timeout)
    while time.monotonic() < deadline:
        status = client.task_status(task_id)["task"]["status"]
        if status in wanted:
            return status
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} never reached {wanted}")


def _events(task_id, kind):
    conn = init_db()
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def test_steering_a_delivered_task_resumes_it_instead_of_queueing(server, source_repo) -> None:
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = client.dispatch_task(repo=added.name, title="ship it")["task_id"]
    _wait_status(client, task_id, {"worker_done"})

    # Delivered tasks used to fall through to the "queued for checkpoint" branch —
    # a checkpoint that never comes once the turn has ended.
    conn = init_db()
    store.set_task_status(conn, task_id, "delivered")

    resp = client.steer_task(task_id, "one more thing")
    assert resp["mode"] == "resume"
    assert "no live worker turn" in resp["note"]
    resumed = _events(task_id, "resumed")
    assert resumed and resumed[-1]["message"] == "one more thing"
    assert not [e for e in _events(task_id, "steer") if e.get("mode") == "checkpoint_pending"]


def test_queued_checkpoint_steer_is_applied_when_the_turn_ends(server, source_repo, monkeypatch):
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = client.dispatch_task(repo=added.name, title="long turn")["task_id"]
    _wait_status(client, task_id, {"worker_done"})
    conn = init_db()
    run_id = store.get_task(conn, task_id)["run_id"]

    # Seed the situation the fix covers: a steer queued while the turn was live.
    store.append_event(
        conn,
        kind="steer",
        payload={"task_id": task_id, "mode": "checkpoint_pending", "message": "apply me"},
        run_id=run_id,
        task_id=task_id,
    )
    delivered: list[tuple[int, str | None]] = []
    core = srv.supervisor
    monkeypatch.setattr(
        core, "resume_task", lambda tid, msg=None, **kw: delivered.append((tid, msg))
    )

    from papaya_agent_runtime.providers.base import TaskSpec

    core._apply_pending_steer(
        TaskSpec(
            task_id=task_id,
            title="long turn",
            instructions="",
            worktree_path="",
            base_sha="",
            provider="fake",
            run_id=run_id,
        )
    )
    assert delivered == [(task_id, "apply me")]
    assert len(_events(task_id, "steer_applied")) == 1

    # Exactly once: a second checkpoint finds nothing pending.
    core._apply_pending_steer(
        TaskSpec(
            task_id=task_id,
            title="long turn",
            instructions="",
            worktree_path="",
            base_sha="",
            provider="fake",
            run_id=run_id,
        )
    )
    assert delivered == [(task_id, "apply me")]


# --------------------------------------------------------------------------- #
# Additive queueing and explicit replacement (issue #75)
# --------------------------------------------------------------------------- #


def _queue(client, task_id, message, **kw):
    resp = client.steer_task(task_id, message, **kw)
    assert resp["ok"] and resp["mode"] == "checkpoint_pending", resp
    return resp


def _hold(client, added, seconds: float = 1.2) -> int:
    task_id = client.dispatch_task(
        repo=added.name, title="long turn", instructions=f"HOLD:{seconds}"
    )["task_id"]
    deadline = time.monotonic() + scale(10)
    while not store.live_runners_for_task(init_db(), task_id):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return task_id


def test_three_additive_messages_are_delivered_together_in_order(server, source_repo) -> None:
    """The 2026-09-07 shape: A, B and C queued during one turn; only C used to land."""
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = _hold(client, added)
    a = _queue(client, task_id, "A: add the baseline probe")
    assert "delivered when the current turn ends" in a["note"]
    b = _queue(client, task_id, "B: add the deadline probe")
    assert "2 message(s) will be delivered together" in b["note"]
    c = _queue(client, task_id, "C: supplement to the same checkpoint")
    assert [q["will_be"] for q in c["queue"]] == ["delivered"] * 3
    assert [q["preview"] for q in c["queue"]] == [
        "A: add the baseline probe",
        "B: add the deadline probe",
        "C: supplement to the same checkpoint",
    ]

    _wait_status(client, task_id, {"worker_done"}, timeout=25)
    deadline = time.monotonic() + scale(10)
    while not _events(task_id, "steer_applied"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    [resumed] = _events(task_id, "resumed")
    ids = [a["steer_event"], b["steer_event"], c["steer_event"]]
    assert resumed["steer_events"] == ids
    assert resumed["superseded_steer_events"] == []
    text = resumed["message"]
    assert "3 messages were queued" in text
    assert text.index("A: add the baseline") < text.index("B: add the deadline") < text.index("C: ")
    [applied] = _events(task_id, "steer_applied")
    assert applied["steer_events"] == ids and applied["composed"] is True
    _wait_status(client, task_id, {"worker_done"}, timeout=25)
    assert len(_events(task_id, "resumed")) == 1


def test_an_explicit_replacement_supersedes_what_was_queued_before_it(server, source_repo):
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = _hold(client, added)
    a = _queue(client, task_id, "A: old plan")
    b = _queue(client, task_id, "B: old plan detail")
    full = _queue(client, task_id, "FULL: the complete packet", delivery="replace")
    assert "supersedes the 2 message(s) queued before it" in full["note"]
    assert [q["will_be"] for q in full["queue"]] == ["superseded", "superseded", "delivered"]
    # A supplement after the replacement rides along with it.
    d = _queue(client, task_id, "D: one addition to the full packet")
    assert [q["will_be"] for q in d["queue"]] == [
        "superseded",
        "superseded",
        "delivered",
        "delivered",
    ]

    _wait_status(client, task_id, {"worker_done"}, timeout=25)
    deadline = time.monotonic() + scale(10)
    while not _events(task_id, "steer_applied"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    [resumed] = _events(task_id, "resumed")
    assert resumed["steer_events"] == [full["steer_event"], d["steer_event"]]
    assert resumed["superseded_steer_events"] == [a["steer_event"], b["steer_event"]]
    assert "old plan" not in resumed["message"]
    assert "FULL: the complete packet" in resumed["message"]
    assert "D: one addition" in resumed["message"]
    [applied] = _events(task_id, "steer_applied")
    assert applied["superseded_steer_events"] == [a["steer_event"], b["steer_event"]]
    _wait_status(client, task_id, {"worker_done"}, timeout=25)
    # The superseded messages are never replayed at a later checkpoint.
    assert srv.supervisor._pending_checkpoint_steers(init_db(), task_id) == []


def test_a_single_queued_message_is_delivered_verbatim(server, source_repo) -> None:
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = _hold(client, added, seconds=0.5)
    _queue(client, task_id, "just this")
    _wait_status(client, task_id, {"worker_done"}, timeout=25)
    deadline = time.monotonic() + scale(10)
    while not _events(task_id, "resumed"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert _events(task_id, "resumed")[0]["message"] == "just this"


def test_an_unknown_delivery_mode_is_refused(server, source_repo) -> None:
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = _hold(client, added, seconds=0.5)
    resp = client.steer_task(task_id, "x", delivery="prepend")
    assert not resp["ok"] and "delivery must be" in resp["error"]
    _wait_status(client, task_id, {"worker_done"})
