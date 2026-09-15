"""A task's status tells the truth across steer, interrupt, and resume.

Three runtime lies this pins shut:

1. ``ppy steer`` on a live Claude worker interrupted the turn, the interrupted
   process exited non-zero, and the harness stamped the task ``failed`` — with no
   auto-resume (2026-09-02, task 78).
2. A superseded session's late exit overwrote ``in_progress`` with ``failed``
   while the resumed runner was alive and working (codex, 2026-09-01).
3. ``ppy resume`` left the task showing its old terminal status until the resumed
   worker happened to emit something.
"""

from __future__ import annotations

import json
import time

import pytest

from papaya_agent_runtime import repos
from papaya_agent_runtime.providers.fake import FakeProvider
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


def _wait_status(client, task_id, wanted, timeout=20.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = client.task_status(task_id)["task"]["status"]
        if last in wanted:
            return last
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} never reached {wanted} (last {last})")


def _events(task_id, kind):
    conn = init_db()
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def _wait_live_runner(task_id, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conn = init_db()
        rows = store.live_runners_for_task(conn, task_id)
        if rows and rows[0]["pid"]:
            return rows[0]
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} never got a live runner")


def _wait_for(predicate, timeout=20.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


# --------------------------------------------------------------------------- #
# (a) A steer on a live interrupt-capable worker delivers and stays in_progress
# --------------------------------------------------------------------------- #


def test_steer_on_a_live_worker_stays_in_progress_and_delivers(server, source_repo, monkeypatch):
    monkeypatch.setattr(FakeProvider, "supports_interrupt_steer", lambda self: True, raising=False)
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = client.dispatch_task(repo=added.name, title="hold", instructions="HOLD:30")["task_id"]
    _wait_live_runner(task_id)

    resp = client.steer_task(task_id, "do it the other way")
    assert resp["mode"] == "interrupt_resume"
    assert resp["status"] == "in_progress"
    assert resp["superseded_runners"], "the interrupted runner must be recorded as superseded"

    # The interrupted turn's non-zero exit lands against the retired session, not
    # the task: the task never passes through `failed`.
    assert client.task_status(task_id)["task"]["status"] == "in_progress"
    _wait_for(lambda: _events(task_id, "runner_superseded"), what="the superseded runner's exit")

    # The steer is auto-resumed into the session and the worker finishes with it.
    _wait_status(client, task_id, {"worker_done"})
    assert not _events(task_id, "error")
    resumed = _events(task_id, "resumed")
    assert resumed and resumed[-1]["message"] == "do it the other way"


def test_steer_records_the_interrupt_mode_and_not_a_bare_interrupt(
    server, source_repo, monkeypatch
):
    monkeypatch.setattr(FakeProvider, "supports_interrupt_steer", lambda self: True, raising=False)
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = client.dispatch_task(repo=added.name, title="hold", instructions="HOLD:30")["task_id"]
    _wait_live_runner(task_id)
    client.steer_task(task_id, "pivot")
    steers = _events(task_id, "steer")
    assert [s["mode"] for s in steers] == ["interrupt_resume"]
    assert steers[0]["status"] == "in_progress"
    _wait_status(client, task_id, {"worker_done"})


# --------------------------------------------------------------------------- #
# (b) A duplicate resume never replaces a live execution
# --------------------------------------------------------------------------- #


def test_duplicate_resume_does_not_replace_a_live_task(server, source_repo):
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = client.dispatch_task(repo=added.name, title="hold", instructions="HOLD:30")["task_id"]
    old = _wait_live_runner(task_id)
    old_runner_id = old["id"]

    # A repeated resume used to supersede the current process and briefly run two
    # provider calls for one task. Admission now refuses before session or task
    # state changes.
    resp = client.resume_task(task_id, "carry on")
    assert not resp["ok"]
    assert "live execution" in resp["error"]
    assert client.task_status(task_id)["task"]["status"] == "in_progress"
    assert not _events(task_id, "resumed")
    assert [row["id"] for row in store.live_runners_for_task(init_db(), task_id)] == [old_runner_id]


def test_terminal_events_carry_the_session_they_came_from(server, source_repo):
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = client.dispatch_task(repo=added.name, title="ship")["task_id"]
    _wait_status(client, task_id, {"worker_done"})
    done = _events(task_id, "worker_done")
    assert done and done[-1]["session_id"], "an exit must be attributable to its session"


# --------------------------------------------------------------------------- #
# (c) `ppy resume` flips the task back to in_progress immediately
# --------------------------------------------------------------------------- #


def test_resume_flips_status_to_in_progress_before_the_worker_speaks(
    server, source_repo, monkeypatch
):
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = client.dispatch_task(repo=added.name, title="ship")["task_id"]
    _wait_status(client, task_id, {"worker_done"})
    conn = init_db()
    store.set_task_status(conn, task_id, "failed")

    # No worker actually runs: the status must be truthful the instant resume
    # returns, not once the resumed process happens to emit its first event.
    monkeypatch.setattr(srv.supervisor, "_run_task", lambda runner, spec, **kw: None)
    resp = client.resume_task(task_id, "pick it back up")
    assert resp["status"] == "in_progress"
    assert client.task_status(task_id)["task"]["status"] == "in_progress"
