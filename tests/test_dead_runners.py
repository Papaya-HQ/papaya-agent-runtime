"""A runner row whose process is gone gives its slot back (2026-09-16).

A worker whose supervisor was stopped by hand left a `running` row behind with a
dead pid. Admission counts live rows, so it held one of two worker slots for three
hours: the next supervisor start did not look at it, and the rounds never looked
at a worker whose ticket had ended.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from papaya_agent_runtime import papaya_events, rounds, serve
from papaya_agent_runtime.state import store
from papaya_agent_runtime.state.db import init_db
from papaya_agent_runtime.supervisor.core import LANE_TICKET, Supervisor, SupervisorError
from papaya_agent_runtime.supervisor.server import SupervisorServer
from test_rounds import WallClock


def dead_pid() -> int:
    """A pid that was a process a moment ago and is not one now."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def worker_with_a_dead_runner(*, ticket_phase: str | None = None) -> tuple[int, int, str]:
    """An in-flight worker whose runner row says `running` for a process that is gone."""
    conn = init_db()
    try:
        run_id = store.create_run(conn, "Fix PAP-219")
        ticket = store.add_task(conn, run_id=run_id, title="Fix PAP-219")
        if ticket_phase is not None:
            event = papaya_events.PapayaEvent(
                id="ev-pap-219",
                kind="work_item.assigned",
                subject="work_item:item-219",
                payload={},
                work_item_id="item-219",
            )
            papaya_events.record_task(conn, ticket, event)
            for phase in (serve.PHASE_PICKED_UP, serve.PHASE_DISPATCHED, ticket_phase):
                serve.record_phase(conn, ticket, phase)
        worker = store.add_task(conn, run_id=run_id, title="worker", provider="fake")
        store.update_task_fields(conn, worker, status="in_progress")
        store.upsert_session(
            conn, task_id=worker, provider="fake", provider_session_id="session-15"
        )
        store.register_runner(conn, runner_id="runner-15", task_id=worker, provider="fake")
        store.update_runner(
            conn, "runner-15", pid=dead_pid(), status="running", session_id="session-15"
        )
        # Heard from long ago, the way a row left three hours earlier is.
        conn.execute(
            "UPDATE runners SET started_at = '2026-09-16T19:00:00+00:00', "
            "heartbeat_at = '2026-09-16T19:23:37+00:00' WHERE id = 'runner-15'"
        )
        conn.execute(
            "UPDATE events SET created_at = '2026-09-16T19:00:00+00:00' WHERE task_id = ?",
            (worker,),
        )
        conn.commit()
        return ticket, worker, "runner-15"
    finally:
        conn.close()


def closed_state(worker: int, runner_id: str) -> tuple[str, str, list[dict]]:
    conn = init_db()
    try:
        runner = store.get_runner(conn, runner_id)
        task = store.get_task(conn, worker)
        rows = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = 'worker_stopped'", (worker,)
        ).fetchall()
        payloads = [json.loads(row["payload"]) for row in rows]
        return str(runner["status"]), str(task["status"]), payloads
    finally:
        conn.close()


def admit_one(supervisor: Supervisor) -> None:
    conn = init_db()
    try:
        supervisor._admit(conn, limit=1, task_id=None, lane=LANE_TICKET)
    finally:
        conn.close()


def test_a_supervisor_start_closes_a_dead_runner_and_the_slot_admits_a_dispatch(ppy_home) -> None:
    _ticket, worker, runner_id = worker_with_a_dead_runner()

    # Before any start, the dead row fills a one-slot ceiling.
    with pytest.raises(SupervisorError, match="capacity is full"):
        admit_one(Supervisor())

    server = SupervisorServer()
    server.start_background()
    try:
        runner_status, task_status, stopped = closed_state(worker, runner_id)
        assert runner_status == "exited"
        assert task_status == "worker_stopped"
        (payload,) = stopped
        assert "pid" in payload["summary"] and "is gone" in payload["summary"]
        assert payload["session_id"] == "session-15"  # left resumable
        assert [c.runner_id for c in server.closed_at_start] == [runner_id]
        assert "closed runner runner-15 of task" in server.closed_at_start[0].line()

        admit_one(server.supervisor)
    finally:
        server.stop()


def test_a_round_closes_a_dead_runner_whose_ticket_has_ended(ppy_home) -> None:
    _ticket, worker, runner_id = worker_with_a_dead_runner(ticket_phase=serve.PHASE_HANDED_OVER)
    walker = rounds.Rounds(
        SimpleNamespace(loop=None, agent_config={}),
        SimpleNamespace(held={}),
        clock=WallClock(),
        forge=lambda _conn: [],
        prune=lambda _task_id: {"removed": [], "skipped": [], "reclaimed_bytes": 0},
        git=lambda *_a, **_k: 0,
        papaya_env=dict,
    )

    parts = asyncio.run(walker.round_once())

    runner_status, task_status, stopped = closed_state(worker, runner_id)
    assert runner_status == "exited"
    assert task_status == "worker_stopped"
    assert len(stopped) == 1 and stopped[0]["source"] == "rounds"
    assert any(f"closed runner {runner_id} of task {worker}" in part for part in parts)
    # Said once: the next round finds nothing left to close.
    assert not any("closed runner" in part for part in asyncio.run(walker.round_once()))


def test_a_live_runner_and_a_fresh_starting_row_are_left_alone(ppy_home) -> None:
    from papaya_agent_runtime.supervisor import dead_runners

    conn = init_db()
    try:
        run_id = store.create_run(conn, "live")
        live = store.add_task(conn, run_id=run_id, title="live")
        store.register_runner(conn, runner_id="live", task_id=live, provider="fake")
        store.update_runner(conn, "live", pid=os.getpid(), status="running")
        starting = store.add_task(conn, run_id=run_id, title="starting")
        store.register_runner(conn, runner_id="starting", task_id=starting, provider="fake")

        assert dead_runners.close_dead_runners(conn, dead_after_s=600) == []
        assert {r["id"] for r in store.live_runners(conn)} == {"live", "starting"}
        # A row with no process, unheard of past `supervisor.dead_after`, is dead.
        (closed,) = dead_runners.close_dead_runners(conn, dead_after_s=0)
        assert closed.runner_id == "starting"
        assert "has no process" in closed.cause
    finally:
        conn.close()
