"""Hermetic tests for the lifecycle hooks' pickup context and the stop gate."""

from __future__ import annotations

import json

import pytest

from papaya_agent_runtime import board, handoff, hooks
from papaya_agent_runtime.state import init_db, store


@pytest.fixture
def ppy_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    monkeypatch.delenv("PPY_DEV", raising=False)
    monkeypatch.setattr(
        handoff, "supervisor_status", lambda: {"reachable": False, "socket": "/nowhere"}
    )
    return tmp_path / ".ppy"


def _open_task(conn, status="worker_done"):
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title="build")
    store.set_task_status(conn, task_id, status)
    return run_id, task_id


def test_session_start_injects_pickup_context(ppy_home) -> None:
    conn = init_db()
    run_id, task_id = _open_task(conn)
    board.add("review task's diff and deliver", run_id=run_id, conn=conn)

    response = hooks.handle_hook_stdin("SessionStart", json.dumps({"source": "compact"}))

    context = response["hookSpecificOutput"]["additionalContext"]
    assert "Papaya Agent Runtime pickup context" in context
    assert "- Next (todo ledger): #1 review task's diff and deliver" in context
    assert f'Run {run_id} "ship it": task {task_id} "build" — worker_done' in context
    assert "finished and waiting on review" in context
    assert response["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_session_start_is_quiet_when_nothing_is_open(ppy_home) -> None:
    init_db()
    response = hooks.handle_hook_stdin("SessionStart", "{}")
    assert response == {}


def test_stop_blocks_once_when_work_is_open_and_no_next_step(ppy_home) -> None:
    conn = init_db()
    _open_task(conn)

    first = hooks.handle_hook_stdin("Stop", "{}")
    assert first["decision"] == "block"
    assert "ppy todo add" in first["reason"]

    # The harness marks the continuation; we never loop.
    again = hooks.handle_hook_stdin("Stop", json.dumps({"stop_hook_active": True}))
    assert "decision" not in again

    # Once a next step is recorded, stopping is fine.
    board.add("deliver task 1", conn=conn)
    assert "decision" not in hooks.handle_hook_stdin("Stop", "{}")


def test_stop_does_not_block_without_open_work_or_in_dev(ppy_home, monkeypatch) -> None:
    conn = init_db()
    assert "decision" not in hooks.handle_hook_stdin("Stop", "{}")

    _open_task(conn)
    monkeypatch.setenv("PPY_DEV", "1")
    assert "decision" not in hooks.handle_hook_stdin("Stop", "{}")
