"""A worker steered from a ticket's turn becomes that ticket's worker (first cloud E2E, 2026-10-02).

The second ticket asked to change the first ticket's open pull request. Its brief
turn rightly steered the worker that owned it instead of dispatching another, but
the worker stayed in the first ticket's run: the first ticket was over, the second
had no worker, so it reported "nothing to build" and the worker waited on a
manager that never came.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from papaya_agent_runtime import serve
from papaya_agent_runtime.state import init_db, store


@pytest.fixture
def ledger(ppy_home):
    conn = init_db()
    yield conn
    conn.close()


def _ticket(conn, phase: str) -> tuple[int, int]:
    """A ticket's run and its own task, at ``phase``."""
    run_id = store.create_run(conn, "ticket")
    task_id = store.add_task(conn, run_id=run_id, title="ticket")
    store.update_task_fields(conn, task_id, phase=phase)
    conn.commit()
    return run_id, task_id


def _worker(conn, run_id: int) -> int:
    return store.add_task(conn, run_id=run_id, title="worker")


def _run_of(task_id: int) -> int:
    conn = init_db()
    try:
        return int(store.get_task(conn, task_id)["run_id"])
    finally:
        conn.close()


def test_a_worker_whose_ticket_is_over_moves_to_the_ticket_that_steered_it(ledger) -> None:
    first_run, _ = _ticket(ledger, serve.PHASE_RELEASED)
    worker = _worker(ledger, first_run)
    second_run, second_ticket = _ticket(ledger, serve.PHASE_BRIEFING)

    assert serve.adopt_worker(worker, second_run) is True
    assert _run_of(worker) == second_run
    # It is now the worker the second ticket's runner finds.
    found = serve.find_worker(SimpleNamespace(task_id=second_ticket, run_id=second_run))
    assert found is not None and found.task_id == worker
    events = [e["kind"] for e in store.events_after(init_db(), second_run, 0)]
    assert serve.WORKER_ADOPTED_EVENT in events


@pytest.mark.parametrize("phase", list(serve.WORKING_PHASES) + [serve.PHASE_PICKED_UP])
def test_a_worker_whose_ticket_is_still_worked_is_never_taken(ledger, phase) -> None:
    first_run, _ = _ticket(ledger, phase)
    worker = _worker(ledger, first_run)
    second_run, _ = _ticket(ledger, serve.PHASE_BRIEFING)

    assert serve.adopt_worker(worker, second_run) is False
    assert _run_of(worker) == first_run


def test_work_a_person_dispatched_by_hand_is_never_taken(ledger) -> None:
    manual_run = store.create_run(ledger, "by hand")
    worker = _worker(ledger, manual_run)
    second_run, _ = _ticket(ledger, serve.PHASE_BRIEFING)

    assert serve.adopt_worker(worker, second_run) is False
    assert _run_of(worker) == manual_run


def test_a_ticket_that_already_has_a_worker_takes_no_second(ledger) -> None:
    first_run, _ = _ticket(ledger, serve.PHASE_RELEASED)
    worker = _worker(ledger, first_run)
    second_run, _ = _ticket(ledger, serve.PHASE_DISPATCHED)
    _worker(ledger, second_run)

    assert serve.adopt_worker(worker, second_run) is False


def test_the_same_run_or_an_unknown_task_is_a_no(ledger) -> None:
    run_id, _ = _ticket(ledger, serve.PHASE_BRIEFING)
    worker = _worker(ledger, run_id)

    assert serve.adopt_worker(worker, run_id) is False
    assert serve.adopt_worker(99999, run_id) is False


def test_the_brief_prompt_says_to_steer_the_worker_that_owns_the_pull_request() -> None:
    from papaya_agent_runtime import prompts

    text = prompts.load("brief")
    assert "Steer the worker that owns it" in text
    assert "becomes this ticket's worker" in text


def test_ppy_steer_from_a_ticket_turn_adopts_before_it_steers(
    ppy_home, monkeypatch, capsys
) -> None:
    from papaya_agent_runtime import cli
    from papaya_agent_runtime.papaya_events import TICKET_RUN_ENV
    from papaya_agent_runtime.supervisor import client as client_module

    calls: list[tuple] = []

    class _Supervisor:
        def steer_task(self, task_id, message, delivery="append", by=None):
            calls.append(("steer", task_id))
            return {"ok": True, "mode": "checkpoint_pending"}

    monkeypatch.setattr(client_module, "SupervisorClient", _Supervisor)
    monkeypatch.setattr(
        serve,
        "adopt_worker",
        lambda task_id, run_id: calls.append(("adopt", task_id, run_id)) or True,
    )
    monkeypatch.setenv(TICKET_RUN_ENV, "7")

    assert cli.main(["steer", "5", "--message", "change the line"]) == 0
    assert calls == [("adopt", 5, 7), ("steer", 5)]
    assert "task 5 is now this ticket's worker" in capsys.readouterr().out


def test_ppy_steer_from_a_person_adopts_nothing(ppy_home, monkeypatch) -> None:
    from papaya_agent_runtime import cli
    from papaya_agent_runtime.papaya_events import TICKET_RUN_ENV
    from papaya_agent_runtime.supervisor import client as client_module

    adopted: list[int] = []

    class _Supervisor:
        def steer_task(self, task_id, message, delivery="append", by=None):
            return {"ok": True, "mode": "checkpoint_pending"}

    monkeypatch.setattr(client_module, "SupervisorClient", _Supervisor)
    monkeypatch.setattr(serve, "adopt_worker", lambda task_id, run_id: adopted.append(task_id))
    monkeypatch.delenv(TICKET_RUN_ENV, raising=False)

    assert cli.main(["steer", "5", "--message", "change the line"]) == 0
    assert adopted == []
