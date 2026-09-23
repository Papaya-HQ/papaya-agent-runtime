"""A worker can write in its own worktree, and nowhere a repository widens it to.

The Papaya plugin's PreToolUse write guard reads `PAPAYA_ALLOWED_WORKING_DIRECTORIES`
when it is set and the connection's `allowed_working_directories` otherwise, which
`connect --working-directory` sets to the runtime directory. A worker launched without
the variable could therefore write only in the runtime checkout: every edit in its
worktree was refused (2026-09-22, the owner's runtime and a local end-to-end run).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from conftest import wait_until
from papaya_agent_runtime import instructions, papaya_events, repos
from papaya_agent_runtime.paths import ppy_home
from papaya_agent_runtime.providers.fake import FakeProvider
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor import runner as runner_module
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.runner import RunnerGuardian, worker_env
from papaya_agent_runtime.supervisor.server import SupervisorServer
from test_runner import _dispatch_spec

ROOTS = runner_module.WRITE_ROOTS_ENV


class _Recording:
    """`subprocess` as the runner sees it, keeping the environment each worker got."""

    def __init__(self) -> None:
        self.launches: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(subprocess, name)

    def Popen(self, argv, **kwargs):  # noqa: N802 - stands in for subprocess.Popen
        self.launches.append({"cwd": kwargs.get("cwd"), "env": dict(kwargs.get("env") or {})})
        return subprocess.Popen(argv, **kwargs)


@pytest.fixture
def launches(monkeypatch) -> list[dict[str, Any]]:
    recording = _Recording()
    monkeypatch.setattr(runner_module, "subprocess", recording)
    return recording.launches


def _roots(env: dict[str, str]) -> list[str]:
    return json.loads(env[ROOTS])


def test_the_worker_env_pins_its_worktree_and_the_ppy_home(ppy_home, tmp_path) -> None:
    env = worker_env({"PATH": "/usr/bin"}, worktree=str(tmp_path / "wt"))
    assert _roots(env) == [str(tmp_path / "wt"), str(ppy_home)]


def test_a_repo_task_value_cannot_widen_a_workers_roots(ppy_home, tmp_path) -> None:
    """Matrix row 2: set after the task's values, so a repository's setting is overridden."""
    wide = json.dumps(["/"])
    env = worker_env(
        {"PATH": "/usr/bin", ROOTS: json.dumps(["/runtime"])},
        task_values={ROOTS: wide},
        worktree=str(tmp_path / "wt"),
    )
    assert _roots(env) == [str(tmp_path / "wt"), str(ppy_home)]


def test_a_launched_worker_gets_exactly_its_worktree_and_the_ppy_home(
    ppy_home, source_repo, launches
) -> None:
    """The launch itself, with the manager's runtime root inherited and a repo value set."""
    spec, _run, _task = _dispatch_spec(source_repo)
    spec.process_env = {**spec.process_env, ROOTS: json.dumps(["/"])}
    adapter = FakeProvider()
    inherited = adapter.child_env()
    # Launched from a manager turn's shell, which carries the runtime root.
    adapter.child_env = lambda: {**inherited, ROOTS: json.dumps(["/runtime"])}
    result = RunnerGuardian(adapter).run(spec)
    assert result.status == "completed"
    (launch,) = launches
    assert launch["cwd"] == spec.worktree_path
    assert _roots(launch["env"]) == [spec.worktree_path, str(ppy_home)]


@pytest.fixture
def server(ppy_home):
    srv = SupervisorServer()
    srv.start_background()
    client = SupervisorClient(srv.socket_path)
    wait_until(lambda: _pings(client), 5, what="the supervisor socket", interval=0.05)
    yield srv, client
    srv.stop()


def _pings(client: SupervisorClient) -> bool:
    try:
        return bool(client.ping().get("ok"))
    except Exception:  # noqa: BLE001 - not listening yet
        return False


def _instruction_ticket(repo: str) -> tuple[int, papaya_events.Instruction]:
    instruction = papaya_events.instruction_from(
        {
            "instruction_id": "3f2c1a7e-0b8d-4c55-9e61-2a7f4d9b1c03",
            "short_id": "MI-7",
            "title": "Fix the flaky test",
            "instruction": "Fix the flaky test",
            "reply": {
                "kind": "agent_dm_reply",
                "path": "/api/v1/workspaces/ws-1/polyweave-agents/me/dm-conversations/c/replies",
                "result_path": "/api/v1/workspaces/ws-1/machine-instructions/MI-7/result",
            },
        }
    )
    conn = init_db()
    try:
        event = papaya_events.PapayaEvent(
            id=None, kind="machine.instruction", subject=instruction.subject, payload={}
        )
        _task, run_id, _existed = instructions.record_ticket(conn, event, instruction, repo)
    finally:
        conn.close()
    return run_id, instruction


@pytest.mark.parametrize("dispatched_for", ["work item", "instruction"])
def test_every_dispatch_launches_its_worker_bounded_to_its_worktree(
    server, source_repo, launches, dispatched_for
) -> None:
    """Goal 2, through the supervisor's dispatch (what `ppy dispatch` asks it to do).

    A work item's worker and an instruction's work-path worker (dispatched into the
    ticket's run with the composed brief, as `serve.dispatch_instruction` does).
    """
    _srv, client = server
    added = repos.add_repo(source_repo)
    if dispatched_for == "work item":
        extra: dict[str, Any] = {
            "papaya_event_key": "papaya:event:event-17",
            "papaya_event_metadata": '{"id":"event-17"}',
        }
    else:
        run_id, instruction = _instruction_ticket(added.name)
        extra = {
            "run_id": run_id,
            "instructions": instructions.compose_brief(instruction, added.name),
            "ends_at": "done",
        }
    resp = client.dispatch_task(repo=added.name, title="bounded", **extra)
    assert resp["ok"], resp
    task_id = resp["task_id"]
    wait_until(
        lambda: client.task_status(task_id)["task"]["status"] in {"worker_done", "failed"},
        15,
        what=f"task {task_id} to finish",
        interval=0.1,
    )
    conn = init_db()
    try:
        worktree = store.get_task(conn, task_id)["worktree_path"]
    finally:
        conn.close()
    (launch,) = launches
    assert worktree and launch["cwd"] == worktree
    assert _roots(launch["env"]) == [worktree, str(ppy_home())]
    assert Path(worktree).is_dir()
