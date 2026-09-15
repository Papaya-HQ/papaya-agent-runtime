"""End-to-end supervisor test over the real Unix socket transport."""

from __future__ import annotations

import time

import pytest

from papaya_agent_runtime import repos
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.server import SupervisorServer


@pytest.fixture
def server(ppy_home):
    srv = SupervisorServer()
    srv.start_background()
    # Wait until the socket accepts connections.
    client = SupervisorClient(srv.socket_path)
    for _ in range(50):
        try:
            if client.ping().get("ok"):
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    yield srv, client
    srv.stop()


def _wait_terminal(client, task_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    terminal = {"worker_done", "blocked", "failed"}
    while time.monotonic() < deadline:
        status = client.task_status(task_id)["task"]["status"]
        if status in terminal:
            return status
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} did not reach a terminal state")


def test_dispatch_completes_via_socket(server, source_repo) -> None:
    srv, client = server
    added = repos.add_repo(source_repo)

    resp = client.dispatch_task(repo=added.name, title="ship it")
    assert resp["ok"]
    task_id = resp["task_id"]
    run_id = resp["run_id"]

    assert _wait_terminal(client, task_id) == "worker_done"

    waited = client.wait_actionable(run_id, timeout=5)
    assert waited["all_terminal"] is True

    rs = client.run_status(run_id)
    assert rs["usage"]["input_tokens"] == 250
    kinds = [a["kind"] for a in rs["actionable"]]
    assert "worker_done" in kinds


def test_dispatch_wires_repo_memory_into_worker(server, source_repo, monkeypatch) -> None:
    from papaya_agent_runtime import memory

    added = repos.add_repo(source_repo)
    seen: list[str] = []
    real = memory.worker_context
    monkeypatch.setattr(
        memory, "worker_context", lambda name, **kw: seen.append(name) or real(name, **kw)
    )

    srv, client = server
    resp = client.dispatch_task(repo=added.name, title="wire memory")
    assert resp["ok"]
    assert _wait_terminal(client, resp["task_id"]) == "worker_done"

    # Dispatch pointed the worker at this repo's shared memory (its progress log).
    assert seen == [added.name]
    assert memory.repo_tasks_path(added.name).exists()


def test_run_and_task_snapshots_are_nonblocking(server, source_repo, capsys) -> None:
    from papaya_agent_runtime.cli import main

    srv, client = server
    added = repos.add_repo(source_repo)
    resp = client.dispatch_task(repo=added.name, title="snap it")
    task_id, run_id = resp["task_id"], resp["run_id"]
    assert _wait_terminal(client, task_id) == "worker_done"

    # `ppy run` is a non-blocking snapshot: run + tasks + actionable events.
    assert main(["run", str(run_id)]) == 0
    out = capsys.readouterr().out
    assert f"run {run_id}" in out
    assert f"task {task_id}" in out
    assert "worker_done" in out  # actionable event surfaced without waiting

    # `ppy task` is a non-blocking single-task snapshot.
    assert main(["task", str(task_id)]) == 0
    out = capsys.readouterr().out
    assert f"task {task_id}" in out
    assert "worker_done" in out


def test_wait_timeout_zero_returns_immediately(server, source_repo) -> None:
    """The blocking primitive drains without blocking when --timeout 0."""
    from papaya_agent_runtime.cli import main

    srv, client = server
    added = repos.add_repo(source_repo)
    run_id = client.dispatch_task(repo=added.name, title="drain")["run_id"]
    start = time.monotonic()
    assert main(["wait", str(run_id), "--timeout", "0"]) == 0
    assert time.monotonic() - start < 2.0  # did not sit in a long poll


def test_reconcile_reports_nothing_when_clean(server, source_repo) -> None:
    srv, client = server
    added = repos.add_repo(source_repo)
    resp = client.dispatch_task(repo=added.name, title="clean")
    _wait_terminal(client, resp["task_id"])
    rec = client.reconcile()
    assert rec["ok"]
    assert rec["reconciled"] == []


def test_unknown_repo_errors(server) -> None:
    srv, client = server
    resp = client.dispatch_task(repo="nope", title="x")
    assert resp["ok"] is False
    assert "not registered" in resp["error"]
