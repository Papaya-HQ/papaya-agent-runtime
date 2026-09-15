"""Hermetic tests for hardening: restart durability, event replay, and reconcile."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from papaya_agent_runtime import repos
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.core import Supervisor
from papaya_agent_runtime.supervisor.server import SupervisorServer


def _start(server: SupervisorServer) -> SupervisorClient:
    server.start_background()
    client = SupervisorClient(server.socket_path)
    for _ in range(50):
        try:
            if client.ping().get("ok"):
                return client
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    raise AssertionError("supervisor did not come up")


def _wait_status(client, task_id, want, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.task_status(task_id)["task"]["status"] == want:
            return
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} never reached {want}")


def test_state_survives_supervisor_restart(ppy_home, source_repo) -> None:
    srv1 = SupervisorServer()
    client1 = _start(srv1)
    added = repos.add_repo(source_repo)
    resp = client1.dispatch_task(repo=added.name, title="work", instructions="")
    task_id, run_id = resp["task_id"], resp["run_id"]
    _wait_status(client1, task_id, "worker_done")
    srv1.stop()

    # A fresh supervisor over the same PPY_HOME reconstructs obligations from SQLite.
    srv2 = SupervisorServer()
    client2 = _start(srv2)
    rs = client2.run_status(run_id)
    assert rs["tasks"][0]["id"] == task_id
    assert rs["tasks"][0]["status"] == "worker_done"
    assert any(a["kind"] == "worker_done" for a in rs["actionable"])
    srv2.stop()


def test_event_replay_matches_spool(ppy_home, source_repo) -> None:
    srv = SupervisorServer()
    client = _start(srv)
    added = repos.add_repo(source_repo)
    resp = client.dispatch_task(repo=added.name, title="work", instructions="")
    task_id, run_id = resp["task_id"], resp["run_id"]
    _wait_status(client, task_id, "worker_done")
    srv.stop()

    conn = init_db()
    events = store.events_after(conn, run_id, 0)
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)  # ordered, replayable

    # The append-only spool mirrors the worker events in SQLite.
    spool = Path(ppy_home) / "runs" / f"run-{run_id}" / f"task-{task_id}" / "events.jsonl"
    assert spool.exists()
    spool_lines = [line for line in spool.read_text().splitlines() if line.strip()]
    for line in spool_lines:
        json.loads(line)  # each line is a valid event record
    # The spool mirrors the streamed adapter events (kind "worker_<parsed>"), not
    # the control-plane events that share the prefix: the terminal actionable
    # summary the runner appends afterwards, or a progress report the worker filed
    # through `ppy progress`.
    # A streamed event carries the provider's own record (it has a "type"); the
    # control-plane ones do not, which is what tells a worker's `ppy progress` note
    # apart from an adapter "progress" event of the same kind.
    streamed = [
        e
        for e in events
        if e["kind"].startswith("worker_")
        and e["kind"] not in {"worker_done", "worker_stopped"}
        and "type" in json.loads(e["payload"])
    ]
    assert len(spool_lines) == len(streamed)


def test_reconcile_marks_vanished_runner(ppy_home, source_repo) -> None:
    conn = init_db()
    added = repos.add_repo(source_repo)
    run_id = store.create_run(conn, "obj")
    repo_row = store.get_repo(conn, added.name)
    task_id = store.add_task(conn, run_id=run_id, title="t", repo_id=repo_row["id"])
    store.set_task_status(conn, task_id, "in_progress")

    # A runner whose process has already exited (dead pid) without a result.
    dead = subprocess.Popen(["true"])
    dead.wait()
    store.register_runner(conn, runner_id="r1", task_id=task_id, provider="fake", pid=dead.pid)
    store.update_runner(conn, "r1", status="running")

    result = Supervisor().reconcile()
    assert result["reconciled"]
    assert store.get_task(conn, task_id)["status"] == "needs_recovery"
    assert store.get_runner(conn, "r1")["status"] == "orphaned"
    kinds = [e["kind"] for e in store.events_after(conn, run_id, 0)]
    assert "error" in kinds


@pytest.mark.parametrize(
    "recorded,installed,drift", [("2.1.251", "2.1.251", False), ("2.1.251", "2.2.0", True)]
)
def test_semver_drift_detection(recorded, installed, drift) -> None:
    from papaya_agent_runtime.setup.doctor import _installed_version

    assert _installed_version(f"{installed} (Some CLI)") == installed
    assert (_installed_version(f"{installed} (x)") != recorded) == drift
