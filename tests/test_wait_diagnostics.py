"""A polling deadline that expires explains itself (task 259).

Worker lifecycle tests failed on CI with nothing but "timed out waiting". The shared
wait helper now writes the database, spools, threads and processes to the evidence
directory and puts the same report in the failure.
"""

from __future__ import annotations

import pytest

import conftest
from conftest import wait_until
from papaya_agent_runtime.state import init_db, store


def test_a_deadline_writes_and_prints_what_the_world_looked_like(
    ppy_home, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(conftest, "EVIDENCE_DIR", tmp_path / "evidence")
    conn = init_db()
    run_id = store.create_run(conn, "stuck")
    task_id = store.add_task(conn, run_id=run_id, title="never finishes")
    store.set_task_status(conn, task_id, "in_progress")
    store.register_runner(conn, runner_id="quiet-runner", task_id=task_id, provider="fake")
    store.append_event(conn, kind="dispatched", payload={"task_id": task_id}, run_id=run_id)

    with pytest.raises(AssertionError) as raised:
        wait_until(lambda: False, 0.05, what="task 1 to finish", interval=0.01)

    message = str(raised.value)
    assert message.startswith("timed out waiting for task 1 to finish (dump: ")
    for expected in ("### tasks", "in_progress", "quiet-runner", "dispatched", "## threads"):
        assert expected in message
    [dump] = list((tmp_path / "evidence").iterdir())
    assert "quiet-runner" in dump.read_text(encoding="utf-8")


def test_a_predicate_that_holds_returns_its_value_without_a_dump(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(conftest, "EVIDENCE_DIR", tmp_path / "evidence")
    assert wait_until(lambda: "worker_done", 1) == "worker_done"
    assert not (tmp_path / "evidence").exists()
