"""Hermetic tests for cross-repo rollout ordering."""

from __future__ import annotations

from papaya_agent_runtime.crossrepo import rollout_plan
from papaya_agent_runtime.state import init_db, store


def _task(conn, run_id, title) -> int:
    return store.add_task(conn, run_id=run_id, title=title)


def test_linear_order(ppy_home) -> None:
    conn = init_db()
    run_id = store.create_run(conn, "obj")
    a = _task(conn, run_id, "a")
    b = _task(conn, run_id, "b")
    c = _task(conn, run_id, "c")
    store.add_dependency(conn, b, a)  # b depends on a
    store.add_dependency(conn, c, b)  # c depends on b
    plan = rollout_plan(run_id)
    assert plan.ok
    assert plan.order == [a, b, c]


def test_cycle_detected(ppy_home) -> None:
    conn = init_db()
    run_id = store.create_run(conn, "obj")
    a = _task(conn, run_id, "a")
    b = _task(conn, run_id, "b")
    store.add_dependency(conn, a, b)
    store.add_dependency(conn, b, a)
    plan = rollout_plan(run_id)
    assert not plan.ok
    assert plan.cycles
    assert set(plan.cycles[0]) == {a, b}


def test_missing_dependency(ppy_home) -> None:
    conn = init_db()
    run_id = store.create_run(conn, "obj")
    a = _task(conn, run_id, "a")
    # A dependency on a task that belongs to a different run is "missing" here.
    other = store.create_run(conn, "other")
    ghost = store.add_task(conn, run_id=other, title="ghost")
    store.add_dependency(conn, a, ghost)
    plan = rollout_plan(run_id)
    assert not plan.ok
    assert (a, ghost) in plan.missing
