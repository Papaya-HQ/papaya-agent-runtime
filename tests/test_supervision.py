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


# ── merge follow-up ─────────────────────────────────────────────────────────


def _merged_entry(worker: int) -> dict:
    return {
        "known": True,
        "task_id": worker,
        "pr": 12,
        "url": "https://github.com/o/r/pull/12",
        "status": "delivered",
        "state": "MERGED",
        "merged": True,
    }


def test_a_tracked_worker_with_no_ticket_moves_its_work_item_once(ppy_home) -> None:
    """2026-09-18: PAP-247/248/249/251 stayed `todo` after merge; only tickets moved."""
    from papaya_agent_runtime import tracker

    conn = init_db()
    worker = _delivered(conn)
    tracker.link_task(conn, worker, record="PAP-251")
    said: list = []

    def post(target, body, status):
        said.append((target.event().work_item_id, target.task_id, status, body))

    lines = supervision.merged_step([_merged_entry(worker)], post=post)
    supervision.merged_step([_merged_entry(worker)], post=post)

    [(item, task_id, status, body)] = said
    assert (item, task_id, status) == ("PAP-251", worker, "review")
    assert "verified on staging" in body
    assert any("PAP-251 moved to review" in line for line in lines)
    # An untracked worker with no ticket is still nobody's to post to.
    bare = _delivered(conn)
    supervision.merged_step([_merged_entry(bare)], post=post)
    assert len(said) == 1


def test_a_merge_moves_the_item_to_review_until_staging_and_a_rule_wins(ppy_home) -> None:
    from papaya_agent_runtime.config import MMConfig, load_config, save_config

    conn = init_db()
    worker = _delivered(conn, ticket_phase="handed_over")
    said: list = []
    post = lambda ticket, body, status: said.append((body, status))  # noqa: E731

    supervision.merged_step([_merged_entry(worker)], post=post)
    supervision.merged_step([_merged_entry(worker)], post=post)

    [(body, status)] = said
    assert status == "review" and "verified on staging" in body and "?" not in body

    save_config(MMConfig())
    cfg = load_config()
    cfg.delivery.merged_status = "verified"
    save_config(cfg)
    second = _delivered(conn, ticket_phase="released")
    supervision.merged_step([_merged_entry(second)], post=post)
    assert said[-1] == (
        "Merged: https://github.com/o/r/pull/12. Moved to verified, as this workspace asked.",
        "verified",
    )


def test_a_held_tickets_merge_is_its_runners(ppy_home) -> None:
    from papaya_agent_runtime import rounds

    conn = init_db()
    worker = _delivered(conn, ticket_phase="reviewing")
    ticket = rounds.ticket_for_worker(worker)
    said: list = []
    supervision.merged_step(
        [_merged_entry(worker)], post=lambda *a: said.append(a), held={ticket.task_id}
    )
    assert said == []


def test_the_green_clock_records_then_says_or_merges() -> None:
    entry = {"ci": "pass", "head": "h1", "mergeable": "MERGEABLE"}
    start = (1, {"action": "green", "worker_task_id": 5, "head": "h1", "at": NOW.isoformat()})
    later = NOW + timedelta(hours=25)

    assert supervision.green_clock(5, entry, [], NOW, hours=24, auto_merge=False) == "record"
    assert supervision.green_clock(5, entry, [start], NOW, hours=24, auto_merge=False) == "nothing"
    assert supervision.green_clock(5, entry, [start], later, hours=24, auto_merge=False) == "say"
    assert supervision.green_clock(5, entry, [start], later, hours=24, auto_merge=True) == "merge"
    said = (2, {"action": "green_unmerged", "worker_task_id": 5})
    assert (
        supervision.green_clock(5, entry, [start, said], later, hours=24, auto_merge=False)
        == "nothing"
    )
    assert (
        supervision.green_clock(5, {**entry, "ci": "fail"}, [], NOW, hours=24, auto_merge=False)
        == "nothing"
    )


def test_the_heartbeat_follows_up_merges_only_without_serve(ppy_home, monkeypatch) -> None:
    conn = init_db()
    calls: list = []
    monkeypatch.setattr(watch, "pr_states", lambda c, **k: [{"known": True}])
    monkeypatch.setattr(supervision, "merged_step", lambda entries, **k: calls.append("m") or ["m"])
    monkeypatch.setattr(
        supervision, "green_step", lambda entries, now, **k: calls.append("g") or []
    )

    monkeypatch.setattr(supervision, "serve_running", lambda: True)
    assert watch.merge_step(conn, NOW) == [] and calls == []
    monkeypatch.setattr(supervision, "serve_running", lambda: False)
    assert watch.merge_step(conn, NOW) == ["m"] and calls == ["m", "g"]


# ── hygiene, start remedies, blockers ───────────────────────────────────────


def test_hygiene_removes_what_prune_allows_and_records_the_run(ppy_home) -> None:
    from papaya_agent_runtime import rounds

    prune = lambda task_id: {  # noqa: E731
        "removed": [{"task_id": 3, "path": "/wt/3", "repo": "app", "size_bytes": 2048}],
        "skipped": [],
        "reclaimed_bytes": 2048,
    }
    git_calls: list = []

    lines = supervision.hygiene_step(
        None,
        NOW,
        prune=prune,
        git=lambda args, cwd: git_calls.append(args),
        post=None,
        kept_runs={},
    )

    assert lines[0].startswith("removed 1 worktree(s)")
    assert rounds.hygiene_records()[-1]["scope"] == "all"
    assert supervision.last_hygiene_at() is not None


def test_a_blocker_that_appears_or_clears_is_said(ppy_home) -> None:
    from papaya_agent_runtime import readiness

    problem = readiness.Problem(
        code="gh_missing",
        summary="gh is missing",
        fix="install gh",
        owner=readiness.USER,
        blocking=False,
        title="Install gh",
        steps=("brew install gh",),
    )
    appeared = supervision.blocker_step(check=lambda: readiness.Readiness("degraded", [problem]))
    cleared = supervision.blocker_step(check=lambda: readiness.Readiness("ready", []))

    assert appeared == ["new blocker: Install gh"]
    assert cleared == ["blocker cleared: Install gh"]


def test_a_session_starts_with_the_start_remedies_only_when_no_serve_runs(
    ppy_home, monkeypatch
) -> None:
    monkeypatch.delenv("PPY_DEV", raising=False)
    monkeypatch.delenv(MANAGER_TURN_ENV, raising=False)
    monkeypatch.setattr(
        supervision,
        "start_remedies",
        lambda *, stderr: stderr.write("ppy serve: closed 1 runner\n"),
    )

    monkeypatch.setattr(supervision, "serve_running", lambda: True)
    assert hooks.start_remedies_context() is None
    monkeypatch.setattr(supervision, "serve_running", lambda: False)
    said = hooks.start_remedies_context()
    assert said is not None and "- closed 1 runner" in said


def test_the_heartbeat_upkeep_runs_hygiene_hourly_and_blockers_every_quarter_hour(
    ppy_home, monkeypatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(supervision, "serve_running", lambda: False)
    monkeypatch.setattr(supervision, "blocker_step", lambda: calls.append("b") or [])
    monkeypatch.setattr(supervision, "hygiene_step", lambda *a, **k: calls.append("h") or [])
    last: list = [None]
    monkeypatch.setattr(supervision, "last_hygiene_at", lambda: last[0])
    upkeep = watch.UpkeepStep()

    upkeep(NOW)
    last[0] = NOW
    upkeep(NOW + timedelta(minutes=5))
    upkeep(NOW + timedelta(minutes=16))
    upkeep(NOW + timedelta(minutes=61))

    assert calls == ["b", "h", "b", "b", "h"]
    monkeypatch.setattr(supervision, "serve_running", lambda: True)
    assert upkeep(NOW + timedelta(hours=3)) == []


# ── full suite before review, assigned work, turn obligations ───────────────


def test_an_approval_waits_for_the_supervisors_full_suite_at_head(ppy_home, monkeypatch) -> None:
    from papaya_agent_runtime import environment

    monkeypatch.setattr(
        environment,
        "for_repo",
        lambda row: environment.RepoEnvironment(
            repo="app", full_suite_command="make verify", full_suite_owner="supervisor"
        ),
    )
    conn = init_db()
    repo_id = store.add_repo(
        conn, name="app", origin="o", local_path="/x", default_branch="main", base_sha="a" * 40
    )
    task_id = store.add_task(conn, run_id=store.create_run(conn, "r"), title="b", repo_id=repo_id)
    monkeypatch.setattr(gate, "verdict", lambda tid, full=None: gate.Verdict(gate.NONE, "abc"))

    missing = supervision.full_suite_missing(task_id)
    assert missing and f"ppy gate run --task {task_id} --full" in missing
    assert main(["review", "approve", str(task_id)]) == 1

    monkeypatch.setattr(
        gate, "verdict", lambda tid, full=None: gate.Verdict(gate.RED, "abc", _result(green=False))
    )
    assert supervision.full_suite_missing(task_id) is None  # red is the reviewer's to judge


def test_the_sweeps_first_filter_is_shared() -> None:
    from papaya_agent_runtime import sweep

    fresh = {"id": "a", "status": "in_progress", "updated_at": NOW.isoformat()}
    todo = {"id": "b", "status": "todo", "updated_at": NOW.isoformat()}
    common = {"now": NOW, "declined": {}, "stale_after": 3600.0}

    assert sweep.skip_reason(todo, live={"b"}, **common) == "live here"
    assert sweep.skip_reason(fresh, live=set(), **common) == "in progress elsewhere"
    assert sweep.skip_reason(todo, live=set(), **common) is None
    declined = {"b": {"updated_at": NOW.isoformat(), "reason": "no repo"}}
    assert (
        sweep.skip_reason(todo, live=set(), now=NOW, declined=declined, stale_after=3600.0)
        == "declined earlier, unchanged"
    )


def test_a_session_is_shown_assigned_work_nothing_is_working(ppy_home, monkeypatch) -> None:
    from papaya_agent_client import api_client

    async def assigned(api):
        return {
            "items": [
                {"id": "w1", "display_id": "PAP-231", "title": "Route it", "status": "todo"},
                {"id": "w2", "display_id": "PAP-9", "title": "Done", "status": "done"},
            ]
        }

    monkeypatch.setattr(api_client, "list_assigned_work_items", assigned)

    waiting = supervision.assigned_unpicked(now=NOW, api=object())

    assert [i["display_id"] for i in waiting] == ["PAP-231"]


def test_a_delivered_tickets_missing_report_is_owed_until_posted(ppy_home) -> None:
    from papaya_agent_runtime import workitems

    conn = init_db()
    worker = _delivered(conn, ticket_phase="released")
    [entry] = workitems.tracked()
    item = {"title": "T", "acceptance_criteria": "it works"}

    assert workitems.record_obligations(entry, item, [], "agent-1") == [
        f"work item w is missing: the delivery report for worker task {worker}"
    ]
    assert [o["missing"] for o in workitems.obligations_owed()] == [
        [f"the delivery report for worker task {worker}"]
    ]
    report = {
        "id": "c9",
        "author_type": "agent",
        "author_id": "agent-1",
        "body": "PR is up",
        "created_at": "2999-01-01T00:00:00+00:00",
    }
    workitems.record_obligations(entry, item, [report], "agent-1")
    assert workitems.obligations_owed() == []
