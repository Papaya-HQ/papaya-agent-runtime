"""`ppy wait --after-seq` hears only what is new, so watchers stop re-reporting old news."""

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
