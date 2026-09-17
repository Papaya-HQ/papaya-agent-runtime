"""Owed work reaches somebody whichever way the runtime is running.

2026-09-17: two workers dispatched by hand crashed on their first denied command, one
finished and one stopped, and nobody heard for 8 to 21 hours: `ppy serve` acted only on
held tickets, the heartbeat ignored `failed`, and the interactive session had no
heartbeat running and a Stop hook satisfied by any todo at all.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from papaya_agent_runtime import board, hooks, owed, papaya_events, readiness, team, watch
from papaya_agent_runtime.config import ManagerProfile, MMConfig, WorkerCeiling
from papaya_agent_runtime.manager import build_launch
from papaya_agent_runtime.manager.launch import MANAGER_TURN_ENV
from papaya_agent_runtime.state import init_db, store


@pytest.fixture
def home(ppy_home, monkeypatch):
    monkeypatch.delenv("PPY_DEV", raising=False)
    monkeypatch.delenv(MANAGER_TURN_ENV, raising=False)
    return ppy_home


def _worker(conn, status, *, title="build", run_id=None):
    run_id = run_id or store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title=title)
    store.set_task_status(conn, task_id, status)
    return run_id, task_id


def _crashed(conn):
    run_id, task_id = _worker(conn, "failed")
    store.append_event(
        conn,
        kind="error",
        payload={"task_id": task_id, "summary": "runner crashed: AttributeError('boom')"},
        run_id=run_id,
        task_id=task_id,
    )
    return run_id, task_id


def _ticket(conn, run_id, phase):
    ticket = store.add_task(conn, run_id=run_id, title="ticket")
    store.set_task_phase(conn, ticket, phase)
    store.set_task_env(
        conn, ticket, papaya_events.PAPAYA_EVENT_METADATA, json.dumps({"work_item_id": "w1"})
    )
    return ticket


def _age(conn, task_id, minutes):
    stamp = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
    conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (stamp, task_id))
    conn.commit()


# ── the one list ────────────────────────────────────────────────────────────


def test_a_crashed_worker_is_owed_with_its_error_and_the_next_step(home) -> None:
    conn = init_db()
    _, task_id = _crashed(conn)

    [item] = owed.collect(conn)

    assert (item.task_id, item.status) == (task_id, "failed")
    assert "runner crashed" in item.reason
    assert f"ppy resume {task_id}" in item.next_step
    assert not item.serve_owns


def test_every_waiting_status_is_owed_and_running_work_is_not(home) -> None:
    conn = init_db()
    for status in ("worker_done", "worker_stopped", "blocked", "needs_recovery", "failed"):
        _worker(conn, status)
    _worker(conn, "in_progress")
    _worker(conn, "delivered")

    assert sorted(i.status for i in owed.collect(conn)) == sorted(owed.OWED_STATUSES)
    assert owed.running_count(conn) == 1


def test_a_live_ticket_owns_its_worker_and_an_ended_one_does_not(home) -> None:
    conn = init_db()
    live_run, live_task = _worker(conn, "worker_done")
    _ticket(conn, live_run, "dispatched")
    ended_run, ended_task = _worker(conn, "worker_done")
    _ticket(conn, ended_run, "handed_back")

    items = {i.task_id: i for i in owed.collect(conn)}

    assert items[live_task].serve_owns
    assert not items[ended_task].serve_owns
    # A ticket task itself is not a worker.
    assert set(items) == {live_task, ended_task}


# ── interactive: the heartbeat and the hooks ────────────────────────────────


def test_the_heartbeat_names_a_failed_worker_and_why(home) -> None:
    conn = init_db()
    _, task_id = _crashed(conn)

    line = watch.render(watch.tick(conn))

    assert f"needs me: t{task_id} failed (runner crashed" in line


def test_a_running_heartbeat_is_visible_to_the_hooks_and_forgotten_when_it_ends(home) -> None:
    seen = []

    def sleep(_seconds):
        seen.append(owed.watch_running())
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        watch.run(1, out=io.StringIO(), sleep=sleep)

    assert seen == [True]
    assert not owed.watch_running()


def test_session_start_lists_owed_work_and_asks_for_a_heartbeat(home, monkeypatch) -> None:
    monkeypatch.setattr(hooks, "readiness_context", lambda: None)
    conn = init_db()
    _, task_id = _crashed(conn)

    context = hooks.handle_hook_stdin("SessionStart", "{}")["hookSpecificOutput"][
        "additionalContext"
    ]

    assert "WORKERS WAITING ON YOU (1)" in context
    assert f"worker task {task_id} failed: runner crashed" in context
    assert "NO HEARTBEAT IS RUNNING" in context


def test_stop_needs_a_next_step_against_each_owed_task(home, monkeypatch) -> None:
    monkeypatch.setattr(owed, "watch_running", lambda: True)
    conn = init_db()
    _, task_id = _crashed(conn)
    board.add("something else entirely", conn=conn)

    first = hooks.handle_hook_stdin("Stop", "{}")
    assert first["decision"] == "block"
    assert f"worker task {task_id} failed" in first["reason"]

    # An open next step against the task is not enough: the worker still waits on a
    # turn only this session can take (#72). Deferring it with a reason is.
    board.add("resume it with the fix", task_id=task_id, conn=conn)
    assert hooks.handle_hook_stdin("Stop", "{}")["decision"] == "block"
    board.add(
        "decide whether the fix is worth a redo", task_id=task_id, blocked_on="user", conn=conn
    )
    # The decision is chased through Papaya (`outreach`); not connected, the turn is
    # held once so the ask goes in the reply, then it ends.
    held = hooks.handle_hook_stdin("Stop", "{}")
    assert held["decision"] == "block" and "waiting on a person" in held["reason"]
    assert "decision" not in hooks.handle_hook_stdin("Stop", "{}")


def test_stop_needs_a_heartbeat_while_a_worker_runs(home, monkeypatch) -> None:
    conn = init_db()
    _worker(conn, "in_progress")
    board.add("read its plan note", conn=conn)

    monkeypatch.setattr(owed, "watch_running", lambda: False)
    blocked = hooks.handle_hook_stdin("Stop", "{}")
    assert blocked["decision"] == "block"
    assert "ppy watch" in blocked["reason"]

    monkeypatch.setattr(owed, "watch_running", lambda: True)
    assert "decision" not in hooks.handle_hook_stdin("Stop", "{}")


def test_a_headless_serve_turn_is_held_only_to_its_own_ticket(home, monkeypatch) -> None:
    monkeypatch.setattr(owed, "watch_running", lambda: False)
    monkeypatch.setenv(MANAGER_TURN_ENV, "1")
    conn = init_db()
    _crashed(conn)
    _worker(conn, "in_progress")
    board.add("next", conn=conn)

    assert "decision" not in hooks.handle_hook_stdin("Stop", "{}")
    assert hooks.owed_context(conn) is None


def test_only_a_headless_turn_carries_the_turn_marker() -> None:
    cfg = MMConfig(
        manager=ManagerProfile(provider="claude", model="opus", reasoning="high"),
        worker=WorkerCeiling(provider="codex", max_model="gpt-5-codex", max_reasoning="medium"),
    )
    base = {"PATH": "/usr/bin", MANAGER_TURN_ENV: "1"}

    interactive = build_launch(config=cfg, root="/repo", base_env=base)
    turn = build_launch(config=cfg, root="/repo", base_env=base, turn="review task 3")

    assert MANAGER_TURN_ENV not in interactive.env
    assert turn.env[MANAGER_TURN_ENV] == "1"


# ── serve, or nobody at the terminal: a person hears ────────────────────────


def test_owed_work_nobody_took_up_is_a_persons_problem_after_the_grace(home) -> None:
    conn = init_db()
    _, fresh = _crashed(conn)
    _, stale = _crashed(conn)
    _age(conn, stale, 20)
    live_run, covered = _worker(conn, "worker_done")
    _ticket(conn, live_run, "reviewing")
    _age(conn, covered, 60)

    problems: list[readiness.Problem] = []
    readiness._owed_problems(problems)

    assert [p.scope for p in problems] == [f"task:{stale}:failed"]
    [problem] = problems
    assert problem.code == owed.PROBLEM_CODE
    assert problem.owner == readiness.USER and not problem.blocking
    assert problem.steps and "runner crashed" in problem.steps[0]
    assert fresh != stale


def test_the_team_view_lists_overdue_owed_work_as_waiting_on_a_person(home) -> None:
    conn = init_db()
    _, task_id = _crashed(conn)
    _age(conn, task_id, 30)

    waiting = team.snapshot(conn)["waiting_on_a_person"]

    assert any(w["task_id"] == task_id and "runner crashed" in w["text"] for w in waiting)
