"""A continuation that admission refuses stays pending and is delivered later, once (issue #71).

A worker ending releases its slot before its automatic continuation — a queued
checkpoint steer, a stored-decision answer, the resume after a steer's
interrupt — and a competing dispatch can take that slot. The continuation used
to be marked delivered before the resume was admitted, and then vanish.
"""

from __future__ import annotations

import json
import time

import pytest

from papaya_agent_runtime import decisions, repos
from papaya_agent_runtime.config import MMConfig, WorkerCeiling, save_config
from papaya_agent_runtime.providers.fake import FakeProvider
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor import core
from papaya_agent_runtime.supervisor.core import Supervisor


def _config(limit: int = 1, max_reasoning: str = "xhigh") -> None:
    default_reasoning = "high" if max_reasoning == "xhigh" else max_reasoning
    save_config(
        MMConfig(
            worker=WorkerCeiling(
                "codex", "gpt-5.6-sol", max_reasoning, "gpt-5.6-sol", default_reasoning, limit
            )
        )
    )


def _wait_for(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("timed out waiting")


def _status(task_id: int) -> str:
    return store.get_task(init_db(), task_id)["status"]


def _events(task_id: int, kind: str) -> list[dict]:
    rows = (
        init_db()
        .execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id",
            (task_id, kind),
        )
        .fetchall()
    )
    return [json.loads(r["payload"]) for r in rows]


def _live(task_id: int) -> bool:
    return bool(store.live_runners_for_task(init_db(), task_id))


@pytest.fixture
def team(ppy_home, source_repo):
    """Capacity 1, and a competitor that takes a slot the moment it is asked to."""
    _config(1)
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()

    def competitor() -> int:
        resp = supervisor.dispatch_task(
            repo=added.name, title="competitor", instructions="HOLD:0.8", provider="fake"
        )
        _wait_for(lambda: _live(resp["task_id"]))
        return resp["task_id"]

    return supervisor, added, competitor


# --------------------------------------------------------------------------- #
# Checkpoint steer
# --------------------------------------------------------------------------- #


def test_a_checkpoint_steer_refused_by_capacity_stays_queued_and_lands_once_later(
    team, monkeypatch
) -> None:
    supervisor, added, competitor = team
    task = supervisor.dispatch_task(
        repo=added.name, title="long turn", instructions="HOLD:0.4", provider="fake"
    )
    task_id = task["task_id"]
    _wait_for(lambda: _live(task_id))
    queued = supervisor.steer_task(task_id, "apply me")
    assert queued["mode"] == "checkpoint_pending"

    # The competitor takes the slot in the gap between release and checkpoint.
    real_apply = supervisor._apply_pending_steer
    taken: list[int] = []

    def steal_then_apply(spec):
        if spec.task_id == task_id and not taken:
            taken.append(competitor())
        return real_apply(spec)

    monkeypatch.setattr(supervisor, "_apply_pending_steer", steal_then_apply)

    _wait_for(lambda: bool(_events(task_id, "continuation_deferred")))
    deferred = _events(task_id, "continuation_deferred")[-1]
    assert deferred["kind"] == "steer"
    assert "capacity is full" in deferred["reason"]
    steer_id = [e for e in _events(task_id, "steer") if e.get("mode") == "checkpoint_pending"]
    assert deferred["steer_events"] == [
        int(r["id"])
        for r in init_db()
        .execute("SELECT id FROM events WHERE task_id = ? AND kind = 'steer'", (task_id,))
        .fetchall()
    ]
    assert len(steer_id) == 1
    # Nothing claimed delivery, and the status tells the truth: the turn ended.
    assert _events(task_id, "steer_applied") == []
    assert _events(task_id, "resumed") == []
    assert _status(task_id) == "worker_done"
    assert supervisor._pending_checkpoint_steers(init_db(), task_id) != []

    # The competitor finishes; its release retries the pending steer.
    _wait_for(lambda: _status(taken[0]) == "worker_done")
    _wait_for(lambda: _events(task_id, "resumed") != [])
    [resumed] = _events(task_id, "resumed")
    assert resumed["message"] == "apply me"
    assert resumed["steer_events"] == deferred["steer_events"]
    _wait_for(lambda: _status(task_id) == "worker_done" and not _live(task_id))
    assert len(_events(task_id, "steer_applied")) == 1
    assert len(_events(task_id, "resumed")) == 1
    assert supervisor._pending_checkpoint_steers(init_db(), task_id) == []
    # Another retry pass finds nothing to do.
    assert supervisor.retry_deferred_continuations() == []


# --------------------------------------------------------------------------- #
# Stored-decision auto-answer
# --------------------------------------------------------------------------- #


def test_a_stored_answer_refused_by_capacity_stays_pending_and_is_applied_once(
    team, monkeypatch
) -> None:
    supervisor, added, competitor = team
    decisions.record_decision(
        init_db(), question="which endpoint?", answer="use /v2/users", scope="global"
    )
    task = supervisor.dispatch_task(
        repo=added.name, title="asks", instructions="ASK:which endpoint?", provider="fake"
    )
    task_id = task["task_id"]

    real_answer = supervisor._maybe_auto_answer
    taken: list[int] = []

    def steal_then_answer(spec, question):
        if spec.task_id == task_id and not taken:
            taken.append(competitor())
        return real_answer(spec, question)

    monkeypatch.setattr(supervisor, "_maybe_auto_answer", steal_then_answer)

    _wait_for(lambda: bool(_events(task_id, "continuation_deferred")))
    deferred = _events(task_id, "continuation_deferred")[-1]
    assert deferred["kind"] == "answer"
    assert deferred["answer"] == "use /v2/users"
    assert deferred["question"] == "which endpoint?"
    assert _events(task_id, "auto_answered") == []
    assert _status(task_id) == "blocked"  # still actionable for a human, truthfully
    assert supervisor._auto_answered.get(task_id, set()) == set()

    _wait_for(lambda: _status(taken[0]) == "worker_done")
    _wait_for(lambda: _events(task_id, "resumed") != [])
    [resumed] = _events(task_id, "resumed")
    assert resumed["message"] == "use /v2/users"
    assert resumed["answered_question"] == "which endpoint?"
    _wait_for(lambda: _status(task_id) == "worker_done")
    assert len(_events(task_id, "auto_answered")) == 1
    assert decisions.fingerprint("which endpoint?") in supervisor._auto_answered[task_id]


# --------------------------------------------------------------------------- #
# Interrupt steer whose auto-resume is refused
# --------------------------------------------------------------------------- #


def test_an_interrupted_worker_whose_resume_is_refused_is_worker_stopped_not_in_progress(
    team, monkeypatch
) -> None:
    supervisor, added, competitor = team
    monkeypatch.setattr(FakeProvider, "supports_interrupt_steer", lambda self: True, raising=False)
    task = supervisor.dispatch_task(
        repo=added.name, title="interruptible", instructions="HOLD:5", provider="fake"
    )
    task_id = task["task_id"]
    _wait_for(lambda: _live(task_id))

    real_resume = supervisor._resume_after_interrupt
    taken: list[int] = []

    def steal_then_resume(tid, message, runner_ids):
        _wait_for(lambda: supervisor._active_executions() == [])
        if not taken:
            taken.append(competitor())
        return real_resume(tid, message, runner_ids)

    monkeypatch.setattr(supervisor, "_resume_after_interrupt", steal_then_resume)
    resp = supervisor.steer_task(task_id, "turn left")
    assert resp["mode"] == "interrupt_resume"

    _wait_for(lambda: bool(_events(task_id, "continuation_deferred")))
    assert _status(task_id) == "worker_stopped"  # no worker is running; say so
    assert not _live(task_id)
    [queued] = [e for e in _events(task_id, "steer") if e.get("mode") == "checkpoint_pending"]
    assert queued["message"] == "turn left" and queued["after"] == "interrupt"
    assert "resume was refused" in _events(task_id, "error")[-1]["summary"]

    _wait_for(lambda: _status(taken[0]) == "worker_done")
    _wait_for(lambda: _events(task_id, "resumed") != [])
    [resumed] = _events(task_id, "resumed")
    assert resumed["message"] == "turn left"
    _wait_for(lambda: _status(task_id) == "worker_done")
    assert len(_events(task_id, "resumed")) == 1


# --------------------------------------------------------------------------- #
# Restart and ceilings
# --------------------------------------------------------------------------- #


def test_a_pending_continuation_survives_a_supervisor_restart(team, monkeypatch) -> None:
    supervisor, added, competitor = team
    task = supervisor.dispatch_task(
        repo=added.name, title="long turn", instructions="HOLD:0.4", provider="fake"
    )
    task_id = task["task_id"]
    _wait_for(lambda: _live(task_id))
    supervisor.steer_task(task_id, "after restart")
    real_apply = supervisor._apply_pending_steer
    taken: list[int] = []

    def steal_then_apply(spec):
        if spec.task_id == task_id and not taken:
            taken.append(competitor())
        return real_apply(spec)

    monkeypatch.setattr(supervisor, "_apply_pending_steer", steal_then_apply)
    _wait_for(lambda: bool(_events(task_id, "continuation_deferred")))
    # The old supervisor is gone before its competitor ends: nothing it does counts.
    monkeypatch.setattr(supervisor, "retry_deferred_continuations", lambda: [])
    _wait_for(lambda: _status(taken[0]) == "worker_done")

    restarted = Supervisor()
    result = restarted.reconcile()
    assert [c["task_id"] for c in result["continuations"]] == [task_id]
    [resumed] = _events(task_id, "resumed")
    assert resumed["message"] == "after restart"
    _wait_for(lambda: _status(task_id) == "worker_done" and not _live(task_id))
    assert len(_events(task_id, "resumed")) == 1


def test_a_newly_lowered_ceiling_refuses_the_retry_until_it_is_raised_again(
    ppy_home, source_repo, monkeypatch
) -> None:
    _config(2)
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    monkeypatch.setattr(core, "_adapter_for", lambda provider: FakeProvider())
    task = supervisor.dispatch_task(
        repo=added.name, title="codex profile", instructions="HOLD:0.4", provider="codex"
    )
    task_id = task["task_id"]
    _wait_for(lambda: _live(task_id))
    supervisor.steer_task(task_id, "under the new ceiling")
    _config(2, max_reasoning="medium")  # the task's `high` now exceeds the ceiling

    _wait_for(lambda: bool(_events(task_id, "continuation_deferred")))
    deferred = _events(task_id, "continuation_deferred")[-1]
    assert "worker ceiling" in deferred["reason"]
    assert _events(task_id, "resumed") == []
    # Retrying under the same ceiling is refused again, quietly: same reason, no new event.
    assert supervisor.retry_deferred_continuations()[0]["reason"] == deferred["reason"]
    assert len(_events(task_id, "continuation_deferred")) == 1

    _config(2, max_reasoning="xhigh")
    delivered = supervisor.retry_deferred_continuations()
    assert delivered and delivered[0]["task_id"] == task_id
    assert _events(task_id, "resumed")[0]["message"] == "under the new ceiling"
    _wait_for(lambda: _status(task_id) == "worker_done" and not _live(task_id))
    assert len(_events(task_id, "resumed")) == 1
