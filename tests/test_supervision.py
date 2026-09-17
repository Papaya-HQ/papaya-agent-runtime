"""Check-ins and gate follow-up are one decision, made the same way in both modes.

`ppy serve`'s rounds and ticket runner used to own both; an interactive session made
neither. The decision now lives in `supervision`, serve calls it for its held tickets,
and a session reads the same decision on the heartbeat, in the Stop hook and through
`ppy checkin` / `ppy followup`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from papaya_agent_runtime import board, gate, hooks, owed, rounds, supervision, watch
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.manager.launch import MANAGER_TURN_ENV
from papaya_agent_runtime.state import init_db, store

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def _result(*, green: bool, summary: str = "2 failed", head: str = "abc123") -> gate.GateResult:
    return gate.GateResult(
        repo="app",
        command="make test",
        full=False,
        exit_code=0 if green else 1,
        duration_seconds=12.0,
        summary=summary,
        head_sha=head,
        output_path="/tmp/gate.txt",
        started_at="2026-09-17T11:00:00+00:00",
        finished_at="2026-09-17T11:00:12+00:00",
        failing_tests=[] if green else ["tests/test_x.py::test_y"],
    )


# ── gate follow-up ──────────────────────────────────────────────────────────


def test_green_is_reviewable_whether_it_stopped_or_said_done() -> None:
    verdict = gate.Verdict(gate.GREEN, "abc123", _result(green=True, summary="10 passed"))
    for stopped in (True, False):
        decision = supervision.decide_gate(verdict, stopped=stopped, detail="", worker_id=7)
        assert decision.action == supervision.REVIEW


def test_red_is_sent_back_with_the_gates_own_summary() -> None:
    verdict = gate.Verdict(gate.RED, "abc123", _result(green=False))

    decision = supervision.decide_gate(verdict, stopped=False, detail="", worker_id=7)

    assert decision.action == supervision.STEER
    assert "/tmp/gate.txt" in decision.message and "ppy gate run --task 7" in decision.message


def test_no_gate_after_a_stop_is_sent_to_run_it_and_after_done_is_reviewed() -> None:
    none = gate.Verdict(gate.NONE, "abc123")

    stopped = supervision.decide_gate(none, stopped=True, detail="turn ended", worker_id=7)
    done = supervision.decide_gate(none, stopped=False, detail="", worker_id=7)

    assert stopped.action == supervision.STEER and "ppy gate run --task 7" in stopped.message
    assert done.action == supervision.REVIEW


def test_the_same_red_twice_is_a_persons_decision() -> None:
    red = _result(green=False)
    verdict = gate.Verdict(gate.RED, "abc123", red, repeated=(red, red))

    decision = supervision.decide_gate(verdict, stopped=False, detail="", worker_id=7)

    assert decision.action == supervision.PERSON and decision.message == ""


def test_uncommitted_work_is_sent_back_before_any_gate_decision() -> None:
    assert supervision.decide_commit([], "ppy/task-7") is None
    decision = supervision.decide_commit(["src/a.py", "src/b.py"], "ppy/task-7")
    assert decision is not None and decision.action == supervision.STEER
    assert "git push origin HEAD:ppy/task-7" in decision.message


def test_a_session_sees_the_follow_up_serve_would_send(ppy_home, monkeypatch) -> None:
    conn = init_db()
    task_id = store.add_task(conn, run_id=store.create_run(conn, "r"), title="build")
    store.set_task_status(conn, task_id, "worker_stopped")
    monkeypatch.setattr(
        supervision,
        "gate_followup",
        lambda tid, *, stopped, detail="": supervision.decide_gate(
            gate.Verdict(gate.RED, "abc", _result(green=False)),
            stopped=stopped,
            detail=detail,
            worker_id=tid,
        ),
    )

    [item] = owed.collect(conn)

    assert f"ppy followup {task_id} --send" in item.next_step
    assert "make test" in item.reason


# ── check-ins ───────────────────────────────────────────────────────────────


def _live_worker(conn) -> int:
    task_id = store.add_task(conn, run_id=store.create_run(conn, "r"), title="build")
    store.set_task_status(conn, task_id, "in_progress")
    return task_id


def _quiet_look(task_id: int) -> rounds.WorkerLook:
    return rounds.WorkerLook(
        task_id=task_id,
        status="in_progress",
        branch="ppy/task-1",
        created_at=NOW - timedelta(minutes=30),
        verdict="quiet",
        silent_seconds=25 * 60,
        last_event_id=10,
        progress=[(5, "2026-09-17T11:35:00+00:00", "implement", "working")],
        question=None,
        stopped=None,
        last_acted_id=0,
        last_tool_at=NOW - timedelta(minutes=25),
    )


@pytest.fixture
def quiet_world(ppy_home, monkeypatch):
    monkeypatch.delenv(MANAGER_TURN_ENV, raising=False)
    monkeypatch.delenv("PPY_DEV", raising=False)
    conn = init_db()
    task_id = _live_worker(conn)
    monkeypatch.setattr(rounds, "look_at_worker", lambda tid, *, now, quiet_after: _quiet_look(tid))
    monkeypatch.setattr(rounds, "gate_state", lambda tid: rounds.GateState(False, "no gate"))
    monkeypatch.setattr(rounds, "push_state", lambda tid: None)
    monkeypatch.setattr(owed, "watch_running", lambda: True)
    return conn, task_id


def test_serve_and_a_session_reach_the_same_check_in(quiet_world) -> None:
    _conn, task_id = quiet_world
    look = _quiet_look(task_id)
    waits = rounds.worker_budgets(None)

    served = rounds.Rounds._checkins_due(look, NOW, [], waits, None)
    [session] = supervision.worker_checkins(now=NOW)

    assert served and session.due == tuple(served)
    assert served[0][0] == "quiet"


def test_the_heartbeat_names_a_due_check_in(quiet_world) -> None:
    conn, task_id = quiet_world

    line = watch.render(watch.tick(conn, now=NOW))

    assert f"check in: t{task_id} (silent for" in line


def test_a_session_turn_does_not_end_while_a_check_in_is_due(quiet_world) -> None:
    conn, task_id = quiet_world
    board.add("next", task_id=task_id, conn=conn)

    blocked = hooks.handle_hook_stdin("Stop", "{}")
    assert blocked["decision"] == "block" and f"ppy checkin {task_id}" in blocked["reason"]

    assert main(["checkin", str(task_id), "--ok", "reading its log; it is mid-test"]) == 0
    assert supervision.worker_checkins() == []
    assert "decision" not in hooks.handle_hook_stdin("Stop", "{}")
    records = rounds.round_records(task_id)
    assert records and records[-1][1]["by"] == "person"
    assert json.loads(json.dumps(records[-1][1]))["note"].startswith("reading")


def test_a_persons_fresh_steer_holds_the_check_in_back_in_both_modes(
    quiet_world, monkeypatch
) -> None:
    _conn, task_id = quiet_world
    steer = [{"event_id": 20, "at": NOW.isoformat(), "kind": "steer", "message": "go on"}]
    monkeypatch.setattr(rounds, "person_steers", lambda tid: steer)
    look = _quiet_look(task_id)

    assert rounds.Rounds._person_has_it(look, steer, [], NOW, rounds.worker_budgets(None))
    assert supervision.worker_checkins(now=NOW) == []


# ── pull request repair ─────────────────────────────────────────────────────


def _delivered(conn, *, ticket_phase: str | None = None) -> int:
    from papaya_agent_runtime import papaya_events

    run_id = store.create_run(conn, "r")
    worker = store.add_task(conn, run_id=run_id, title="build")
    store.set_task_status(conn, worker, "delivered")
    store.append_event(
        conn, kind="delivered", payload={"task_id": worker}, run_id=run_id, task_id=worker
    )
    if ticket_phase is not None:
        ticket = store.add_task(conn, run_id=run_id, title="ticket")
        store.set_task_phase(conn, ticket, ticket_phase)
        store.set_task_env(
            conn, ticket, papaya_events.PAPAYA_EVENT_METADATA, json.dumps({"work_item_id": "w"})
        )
    return worker


def _red(worker: int, head: str = "h1") -> dict:
    return {
        "known": True,
        "task_id": worker,
        "pr": 12,
        "url": "https://github.com/o/r/pull/12",
        "status": "delivered",
        "state": "OPEN",
        "ci": "fail",
        "failing": ["unit"],
        "head": head,
        "base": "main",
    }


def test_a_red_pull_request_with_no_ticket_is_repaired_once_on_the_reserve_lane(ppy_home) -> None:
    from papaya_agent_runtime import reconcile

    conn = init_db()
    worker = _delivered(conn)
    steered: list[tuple[int, str]] = []

    lines = supervision.repair_untracked(
        [_red(worker)], NOW, steer=lambda t, m: steered.append((t, m)), pr_details=lambda t, e: {}
    )

    assert steered and steered[0][0] == worker and "CI failing on PR #12" in steered[0][1]
    assert any("repairing" in line for line in lines)
    assert len(reconcile.open_lane()) == 1
    # The same fingerprint is not raised again while the attempt runs.
    supervision.repair_untracked(
        [_red(worker)], NOW, steer=lambda t, m: steered.append((t, m)), pr_details=lambda t, e: {}
    )
    assert len(steered) == 1


def test_a_live_tickets_pull_request_is_left_to_its_ticket(ppy_home) -> None:
    conn = init_db()
    worker = _delivered(conn, ticket_phase="reviewing")
    steered: list = []

    supervision.repair_untracked(
        [_red(worker)], NOW, steer=lambda t, m: steered.append(t), pr_details=lambda t, e: {}
    )

    assert steered == []


def test_a_handed_over_tickets_pull_request_is_repaired_like_one_with_no_ticket(ppy_home) -> None:
    conn = init_db()
    worker = _delivered(conn, ticket_phase="handed_over")
    steered: list = []

    supervision.repair_untracked(
        [_red(worker)], NOW, steer=lambda t, m: steered.append(t), pr_details=lambda t, e: {}
    )

    assert steered == [worker]


def test_two_failed_repairs_at_one_head_wait_on_the_manager(ppy_home) -> None:
    from papaya_agent_runtime import reconcile

    conn = init_db()
    worker = _delivered(conn)
    run = lambda: supervision.repair_untracked(  # noqa: E731
        [_red(worker)], NOW, steer=lambda t, m: None, pr_details=lambda t, e: {}
    )
    for _ in range(2):
        run()
        [attempt] = reconcile.open_lane()
        reconcile.record(
            worker,
            reconcile.FINISHED,
            started_event_id=attempt.started_event_id,
            fingerprint=attempt.payload.get("fingerprint"),
            outcome=reconcile.OUTCOME_FAILED,
        )
    run()

    items = [i for i in owed.collect(init_db()) if i.task_id == worker]
    assert items and items[0].status == "pr_needs_a_person"


def test_the_heartbeat_repairs_only_while_no_serve_is_running(ppy_home, monkeypatch) -> None:
    conn = init_db()
    calls: list = []
    monkeypatch.setattr(watch, "pr_states", lambda c, **k: [{"status": "delivered"}])
    monkeypatch.setattr(
        supervision, "repair_untracked", lambda entries, now, **k: calls.append(entries) or ["x"]
    )

    monkeypatch.setattr(supervision, "serve_running", lambda: True)
    assert watch.repair_step(conn, NOW) == [] and calls == []

    monkeypatch.setattr(supervision, "serve_running", lambda: False)
    assert watch.repair_step(conn, NOW) == ["x"]
