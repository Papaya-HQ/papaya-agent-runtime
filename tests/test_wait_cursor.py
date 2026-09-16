"""`ppy wait --after-seq` hears only what is new, so watchers stop re-reporting old news."""

from __future__ import annotations

import time

import pytest

from papaya_agent_runtime import repos
from papaya_agent_runtime.state import store
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


def test_wait_with_a_cursor_skips_events_already_handled(server, source_repo) -> None:
    srv, client = server
    added = repos.add_repo(source_repo)
    run_id = client.dispatch_task(repo=added.name, title="ship it")["run_id"]

    first = client.wait_actionable(run_id, timeout=10)
    assert first["actionable"], "the finished worker is actionable news the first time"
    handled = max(e["seq"] for e in first["actionable"])

    # Same run, cursor past everything seen: nothing is re-reported. The run is
    # terminal, so the call returns immediately rather than timing out.
    again = client.wait_actionable(run_id, timeout=5, after_seq=handled)
    assert again["actionable"] == []
    assert again["all_terminal"] is True

    # Without the cursor the same old event comes back — the behaviour the cursor exists to avoid.
    stale = client.wait_actionable(run_id, timeout=5)
    assert [e["seq"] for e in stale["actionable"]] == [e["seq"] for e in first["actionable"]]


def test_a_turn_that_ends_between_the_two_reads_is_still_heard(ppy_home, monkeypatch) -> None:
    """CI run 35152229126: the wait answered "all terminal, nothing new" for a finished worker.

    The worker's end is one commit (status and event). It used to land after the wait
    read the events and before it read the statuses; forced here into exactly that gap.
    """
    from papaya_agent_runtime.state.db import init_db
    from papaya_agent_runtime.supervisor.core import Supervisor

    conn = init_db()
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title="worker")
    store.set_task_status(conn, task_id, "in_progress")
    store.register_runner(conn, runner_id="runner-1", task_id=task_id, provider="fake")
    conn.commit()
    conn.close()

    real_read = store.actionable_events
    ended: list[bool] = []

    def read_then_the_turn_ends(conn, run):
        events = real_read(conn, run)
        if not ended:
            ended.append(True)
            writer = init_db()
            try:
                store.record_turn_result(
                    writer,
                    run_id=run_id,
                    task_id=task_id,
                    runner_id="runner-1",
                    task_status="worker_done",
                    kind="worker_done",
                    payload={"task_id": task_id, "summary": "done"},
                    exit_code=0,
                )
            finally:
                writer.close()
        return events

    monkeypatch.setattr(store, "actionable_events", read_then_the_turn_ends)
    supervisor = Supervisor()
    try:
        answer = supervisor.wait_actionable(run_id, timeout=5)
    finally:
        supervisor.close()

    assert ended, "the turn never ended inside the wait"
    assert [e["kind"] for e in answer["actionable"]] == ["worker_done"]
    assert answer["all_terminal"] is True
