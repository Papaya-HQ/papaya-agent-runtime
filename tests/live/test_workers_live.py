"""Opt-in live worker tests (mixed-provider). Excluded from the default run.

Run with `make test-live`. Spawns real Claude and Codex workers in isolated
fixture repos and requires authenticated harnesses. Each worker is asked to make
a tiny, cheap edit; the runtime commits and records a reviewable head SHA.
"""

from __future__ import annotations

import shutil
import time

import pytest

from papaya_agent_runtime import repos
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.server import SupervisorServer

pytestmark = pytest.mark.live


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


def _setup_config(ppy_home):
    from papaya_agent_runtime.setup import discovery, wizard

    report = discovery.discover()
    usable = discovery.usable_harnesses(report)
    if not usable:
        pytest.skip("no authenticated harness")
    wizard.build_config  # noqa: B018
    from papaya_agent_runtime.config import save_config
    from papaya_agent_runtime.setup.wizard import build_config

    cfg = build_config({"manager_provider": usable[0], "worker_provider": usable[-1]})
    save_config(cfg)
    return usable


def _wait_terminal(client, task_id, timeout=180.0):
    deadline = time.monotonic() + timeout
    terminal = {"worker_done", "blocked", "failed"}
    while time.monotonic() < deadline:
        status = client.task_status(task_id)["task"]["status"]
        if status in terminal:
            return status
        time.sleep(0.5)
    raise AssertionError(f"task {task_id} did not finish")


@pytest.mark.parametrize("provider,model", [("claude", "haiku"), ("codex", None)])
def test_live_worker_makes_a_commit(server, source_repo, ppy_home, provider, model) -> None:
    if not shutil.which(provider):
        pytest.skip(f"{provider} not installed")
    _setup_config(ppy_home)
    srv, client = server
    added = repos.add_repo(source_repo)

    instructions = (
        "Create a file named HELLO.md containing exactly the line 'hello from "
        f"{provider}'. Then stop."
    )
    resp = client.dispatch_task(
        repo=added.name,
        title=f"{provider} smoke edit",
        instructions=instructions,
        provider=provider,
        model=model,
        reasoning="low" if provider == "codex" else None,
    )
    assert resp["ok"], resp
    status = _wait_terminal(client, resp["task_id"])
    assert status == "worker_done", f"{provider} ended {status}"

    rs = client.run_status(resp["run_id"])
    assert rs["usage"]["input_tokens"] >= 0
