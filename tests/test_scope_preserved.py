"""A resume or steer packet carries the brief's four outcome sections (issue #77).

A continuation replaces the worker's instructions for the turn. Without the
brief's boundaries repeated in it, a replacement packet silently erases them.
"""

from __future__ import annotations

import json
import time

from papaya_agent_runtime import brief_lint, preflight, repos
from papaya_agent_runtime.providers.fake import FakeProvider
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor import core
from papaya_agent_runtime.supervisor.core import Supervisor

BRIEF = """# Show the delivery state

## Goals

The order page shows the delivery state; `make test` green.

## Intent

Agents answer "where is my order" from the page.

## In scope

`web/orders/page.py` and its tests.

## Out of scope

No courier integration; no schema change.

## Verification

`make test`.
"""


def _wait_for(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("timed out waiting")


def _status(task_id: int) -> str:
    return store.get_task(init_db(), task_id)["status"]


def _resumed(task_id: int) -> list[dict]:
    rows = (
        init_db()
        .execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = 'resumed' ORDER BY id",
            (task_id,),
        )
        .fetchall()
    )
    return [json.loads(r["payload"]) for r in rows]


def _capture_specs(supervisor: Supervisor, monkeypatch) -> list:
    captured = []
    monkeypatch.setattr(core, "_adapter_for", lambda provider: FakeProvider())

    def capture(runner, spec, execution=None):
        captured.append(spec)
        supervisor._release(execution)

    monkeypatch.setattr(supervisor, "_run_task", capture)
    return captured


def _blocked_task_with_brief(supervisor, added) -> int:
    resp = supervisor.dispatch_task(
        repo=added.name, title="delivery state", instructions="ASK: which table?", provider="fake"
    )
    task_id = resp["task_id"]
    _wait_for(lambda: _status(task_id) == "blocked")
    preflight.archive_brief(added.name, task_id, BRIEF)
    return task_id


def test_a_steer_packet_carries_the_four_sections_verbatim(ppy_home, source_repo, monkeypatch):
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    task_id = _blocked_task_with_brief(supervisor, added)
    specs = _capture_specs(supervisor, monkeypatch)

    supervisor.resume_task(task_id, "Render a return as 'Returned' too")
    _wait_for(lambda: len(specs) == 1)
    [spec] = specs
    packet = spec.steer_message
    assert packet.startswith("Render a return as 'Returned' too\n\n")
    assert packet.endswith(brief_lint.standing_scope(BRIEF))
    assert spec.instructions == packet
    for name in ("## Goals", "## Intent", "## In scope", "## Out of scope"):
        assert name in packet
    assert "No courier integration; no schema change." in packet
    assert "## Verification" not in packet  # only the four sections travel
    [event] = _resumed(task_id)
    assert event["message"] == "Render a return as 'Returned' too"  # the manager's words
    assert event["scope_preserved"] is True


def test_a_queued_checkpoint_steer_is_delivered_with_the_scope(ppy_home, source_repo, monkeypatch):
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    task_id = _blocked_task_with_brief(supervisor, added)
    conn = init_db()
    run_id = store.get_task(conn, task_id)["run_id"]
    for message in ("A: baseline probe", "B: deadline probe"):
        store.append_event(
            conn,
            kind="steer",
            payload={"task_id": task_id, "mode": "checkpoint_pending", "message": message},
            run_id=run_id,
            task_id=task_id,
        )
    specs = _capture_specs(supervisor, monkeypatch)
    supervisor._deliver_pending_steers(conn, task_id, run_id)
    _wait_for(lambda: len(specs) == 1)
    packet = specs[0].steer_message
    assert "A: baseline probe" in packet and "B: deadline probe" in packet
    assert packet.index("B: deadline probe") < packet.index("--- Standing scope")
    assert "## Out of scope\nNo courier integration; no schema change." in packet


def test_a_bare_resume_and_an_unarchived_brief_only_add_the_terminal_instruction(
    ppy_home, source_repo, monkeypatch
):
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    resp = supervisor.dispatch_task(
        repo=added.name, title="no brief", instructions="ASK: which table?", provider="fake"
    )
    task_id = resp["task_id"]
    _wait_for(lambda: _status(task_id) == "blocked")
    specs = _capture_specs(supervisor, monkeypatch)

    supervisor.resume_task(task_id, "use the events table")
    _wait_for(lambda: len(specs) == 1)
    assert specs[0].steer_message == (
        f"use the events table\n\nTerminal instruction: finish with "
        f'`ppy progress {task_id} --phase done --note "..."`.'
    )
    assert _resumed(task_id)[-1]["scope_preserved"] is False

    preflight.archive_brief(added.name, task_id, "# Old brief\n\nJust do it.\n")
    supervisor.resume_task(task_id, "and again")
    _wait_for(lambda: len(specs) == 2)
    assert specs[1].steer_message == (
        f"and again\n\nTerminal instruction: finish with "
        f'`ppy progress {task_id} --phase done --note "..."`.'
    )

    supervisor.resume_task(task_id, None)
    _wait_for(lambda: len(specs) == 3)
    assert specs[2].steer_message == (
        f'Terminal instruction: finish with `ppy progress {task_id} --phase done --note "..."`.'
    )
