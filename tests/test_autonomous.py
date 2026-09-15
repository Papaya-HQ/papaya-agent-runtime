"""Hermetic end-to-end: a blocked worker is auto-answered from a durable decision."""

from __future__ import annotations

import time

import pytest

from conftest import scale
from papaya_agent_runtime import decisions, repos
from papaya_agent_runtime.state import init_db
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


def _wait_status(client, task_id, want, timeout=15.0):
    deadline = time.monotonic() + scale(timeout)
    while time.monotonic() < deadline:
        if client.task_status(task_id)["task"]["status"] == want:
            return
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} never reached {want}")


def test_blocked_worker_auto_answered_from_decision(server, source_repo) -> None:
    _srv, client = server
    added = repos.add_repo(source_repo)

    # A standing decision answers this routine question without a human.
    conn = init_db()
    decisions.record_decision(
        conn, question="which endpoint?", answer="use /v2/users", scope="global"
    )

    resp = client.dispatch_task(
        repo=added.name, title="needs input", instructions="ASK:which endpoint?"
    )
    task_id = resp["task_id"]

    # No human touches it; the supervisor resolves the block and completes.
    _wait_status(client, task_id, "worker_done")

    conn = init_db()
    kinds = [
        e["kind"]
        for e in conn.execute(
            "SELECT kind FROM events WHERE task_id = ? ORDER BY seq", (task_id,)
        ).fetchall()
    ]
    assert "question" in kinds
    assert "auto_answered" in kinds
    assert "worker_done" in kinds


def test_answer_command_records_decision_and_resumes(server, source_repo) -> None:
    _srv, client = server
    added = repos.add_repo(source_repo)
    resp = client.dispatch_task(
        repo=added.name, title="needs input", instructions="ASK:pick a name?"
    )
    task_id = resp["task_id"]
    _wait_status(client, task_id, "blocked")

    ans = client.answer_question(task_id, "call it widget", scope="run")
    assert ans["ok"]
    assert ans["decision_id"] >= 1

    _wait_status(client, task_id, "worker_done")

    # The decision is now reusable for the same question in this run.
    conn = init_db()
    hit = decisions.find_matching(conn, "pick a name?", run_id=resp["run_id"])
    assert hit is not None
    assert hit.answer == "call it widget"
