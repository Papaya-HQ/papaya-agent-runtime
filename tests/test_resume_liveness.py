"""A resume that never starts a worker is said at once (runtime #94, item 7).

`ppy resume` used to print "resuming" as soon as the supervisor accepted the request,
whether or not a worker process ever came up. It now waits for the task's newest
runner to have a live pid (or a recorded result) and says an incident otherwise;
`ppy status` names an in-flight worker whose process is gone.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from conftest import wait_until
from papaya_agent_runtime import cli, repos
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor import client as client_mod
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


def _status(task_id: int) -> str:
    conn = init_db()
    try:
        return store.get_task(conn, task_id)["status"]
    finally:
        conn.close()


def _wait_status(task_id: int, wanted: str) -> None:
    wait_until(lambda: _status(task_id) == wanted, 30, what=f"task {task_id} to be {wanted}")


def _ghost_task() -> int:
    conn = init_db()
    run_id = store.create_run(conn, "ghost")
    task_id = store.add_task(conn, run_id=run_id, title="ghost")
    store.set_task_status(conn, task_id, "worker_stopped")
    conn.close()
    return task_id


class GhostClient:
    """A supervisor that accepts every resume and starts nothing."""

    def __init__(self, *_a, **_k) -> None:
        pass

    def resume_task(self, task_id, message=None, ends_at=None, by=None):
        store.set_task_status(init_db(), task_id, "in_progress")
        return {"ok": True, "resumed_session": "s-1", "status": "in_progress", "ends_at": "done"}

    def steer_task(self, task_id, message, delivery="append", by=None):
        store.set_task_status(init_db(), task_id, "in_progress")
        return {"ok": True, "mode": "interrupt_resume", "status": "in_progress", "queue": []}


def test_a_resume_whose_worker_starts_says_it_is_alive(server, source_repo, capsys) -> None:
    _srv, client = server
    added = repos.add_repo(source_repo)
    task_id = client.dispatch_task(repo=added.name, title="asks", instructions="ASK: which?")[
        "task_id"
    ]
    _wait_status(task_id, "blocked")
    # `HOLD:` in the resume message keeps the resumed worker in a live turn while it is
    # looked for (the fake worker reads it from the resume packet).
    assert cli.main(["resume", str(task_id), "--message", "HOLD:5 carry on"]) == 0
    out = capsys.readouterr().out
    assert f"task {task_id}: worker alive (pid " in out
    pid = int(out.split("worker alive (pid ", 1)[1].split(")", 1)[0])
    # The pid it names is the new session's, not the blocked one's.
    rows = store.task_runners(init_db(), task_id)
    assert rows[-1]["pid"] == pid and len(rows) >= 2


def test_a_resume_whose_worker_never_starts_is_an_incident(ppy_home, monkeypatch, capsys) -> None:
    monkeypatch.setattr(client_mod, "SupervisorClient", GhostClient)
    task_id = _ghost_task()
    # An old runner row, from before the resume, says nothing about the new session.
    old = subprocess.Popen(["sleep", "30"])
    try:
        conn = init_db()
        store.register_runner(conn, runner_id="old", task_id=task_id, provider="fake", pid=old.pid)
        store.update_runner(conn, "old", status="running", started_at="2026-01-01T00:00:00+00:00")
        conn.close()

        assert cli.main(["resume", str(task_id), "--verify-seconds", "0.5"]) == 1
        err = capsys.readouterr().err
        assert f"INCIDENT: task {task_id}: no live worker process seen after the resume" in err
    finally:
        old.kill()
        old.wait()

    # 0 skips the check, for a caller that looks for itself.
    assert cli.main(["resume", str(task_id), "--verify-seconds", "0"]) == 0


def test_an_interrupting_steer_is_verified_and_a_queued_one_is_not(
    ppy_home, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(client_mod, "SupervisorClient", GhostClient)
    task_id = _ghost_task()
    assert cli.main(["steer", str(task_id), "--message", "x", "--verify-seconds", "0.3"]) == 1
    assert "INCIDENT: task" in capsys.readouterr().err

    class Queued(GhostClient):
        def steer_task(self, task_id, message, delivery="append", by=None):
            return {"ok": True, "mode": "checkpoint_pending", "queue": []}

    monkeypatch.setattr(client_mod, "SupervisorClient", Queued)
    started = time.monotonic()
    assert cli.main(["steer", str(task_id), "--message", "x"]) == 0
    assert time.monotonic() - started < 5
    assert "INCIDENT" not in capsys.readouterr().err


def test_verify_reads_a_finished_worker_as_finished(ppy_home) -> None:
    task_id = _ghost_task()
    conn = init_db()
    store.register_runner(conn, runner_id="done", task_id=task_id, provider="fake", pid=None)
    store.update_runner(conn, "done", status="exited", result_recorded=1)
    store.set_task_status(conn, task_id, "worker_done")
    conn.close()
    seen = client_mod.verify_worker(task_id, since="2000-01-01T00:00:00+00:00", timeout=0.2)
    assert seen.verdict == "finished"
    assert seen.describe(task_id) == f"task {task_id}: worker already finished — status worker_done"


def test_status_names_an_in_flight_worker_with_no_process(ppy_home, capsys) -> None:
    dead = subprocess.Popen(["true"])
    dead.wait()
    conn = init_db()
    run_id = store.create_run(conn, "r")
    gone = store.add_task(conn, run_id=run_id, title="died")
    store.set_task_status(conn, gone, "in_progress")
    store.register_runner(conn, runner_id="dead", task_id=gone, provider="fake", pid=dead.pid)
    store.update_runner(conn, "dead", status="running")
    conn.close()

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert f"INCIDENT: 1 in-flight worker(s) with no live process — task(s) {gone}" in out


def test_status_says_nothing_of_a_live_worker(ppy_home, capsys) -> None:
    live = subprocess.Popen(["sleep", "30"])
    try:
        conn = init_db()
        run_id = store.create_run(conn, "r")
        task_id = store.add_task(conn, run_id=run_id, title="alive")
        store.set_task_status(conn, task_id, "in_progress")
        store.register_runner(
            conn, runner_id="live", task_id=task_id, provider="fake", pid=live.pid
        )
        store.update_runner(conn, "live", status="running")
        conn.close()
        assert cli.main(["status"]) == 0
        assert "INCIDENT" not in capsys.readouterr().out
    finally:
        live.kill()
        live.wait()
