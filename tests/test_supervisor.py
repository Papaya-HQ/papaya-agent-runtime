"""End-to-end supervisor test over the real Unix socket transport."""

from __future__ import annotations

import time

import pytest

from conftest import wait_until
from papaya_agent_runtime import repos
from papaya_agent_runtime.state import init_db
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
    def finished():
        status = client.task_status(task_id)["task"]["status"]
        return status if status in {"worker_done", "blocked", "failed"} else None

    return wait_until(finished, timeout, what=f"task {task_id} to finish", interval=0.1)


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


def test_dispatch_deduplicates_one_papaya_event_at_the_supervisor_boundary(
    server, source_repo
) -> None:
    _srv, client = server
    added = repos.add_repo(source_repo)
    key = "papaya:event:event-17"

    first = client.dispatch_task(
        repo=added.name,
        title="take it once",
        papaya_event_key=key,
        papaya_event_metadata='{"id":"event-17"}',
    )
    second = client.dispatch_task(
        repo=added.name,
        title="take it once",
        papaya_event_key=key,
        papaya_event_metadata='{"id":"event-17"}',
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["deduplicated"] is True
    assert second["task_id"] == first["task_id"]
    count = init_db().execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    assert count == 1


def test_ensure_supervisor_starts_a_detached_owner_and_waits_for_its_socket(
    ppy_home, monkeypatch
) -> None:
    from papaya_agent_runtime.supervisor import client as client_module

    class FakeClient:
        def __init__(self):
            self.pings = 0

        def ping(self):
            self.pings += 1
            if self.pings == 1:
                raise client_module.SupervisorUnavailable("not up")
            return {"ok": True}

    launched = []

    def popen(argv, **kwargs):
        launched.append((argv, kwargs))
        return object()

    monkeypatch.setattr(client_module, "SupervisorClient", FakeClient)
    monkeypatch.setattr(client_module.subprocess, "Popen", popen)

    client, started = client_module.ensure_supervisor(timeout=1)

    assert client.ping() == {"ok": True}
    assert started is True
    assert launched[0][0][1:] == ["-m", "papaya_agent_runtime", "supervisor", "serve"]
    assert launched[0][1]["start_new_session"] is True
    assert (ppy_home / "run" / "supervisor.log").exists()


def test_ensure_supervisor_reuses_a_running_owner_without_launching(monkeypatch) -> None:
    from papaya_agent_runtime.supervisor import client as client_module

    class FakeClient:
        def ping(self):
            return {"ok": True}

    monkeypatch.setattr(client_module, "SupervisorClient", FakeClient)
    monkeypatch.setattr(
        client_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("a second owner was launched"),
    )

    client, started = client_module.ensure_supervisor()

    assert client.ping() == {"ok": True}
    assert started is False


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
