"""A delivered pull request is followed until it merges.

The rounds are driven one at a time against a fake forge (a list of `watch.pr_states`
entries a test edits between rounds), a fake clock and a fake Papaya. The reconcile
lane's admission and the reconciler's launch are driven on a real in-process
`Supervisor` with the fake provider, with the provider process itself replaced by a
recorder of the spec it would have run.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import test_serve
from conftest import wait_until
from papaya_agent_runtime import (
    budgets,
    delivery,
    papaya_events,
    prompts,
    reconcile,
    repos,
    rounds,
    serve,
)
from papaya_agent_runtime.config import WorkerCeiling
from papaya_agent_runtime.providers.command_rules import command_rules
from papaya_agent_runtime.state import store
from papaya_agent_runtime.state.db import init_db
from papaya_agent_runtime.supervisor.core import LANE_RECONCILE, Supervisor, SupervisorError
from test_rounds import WallClock, events_of
from test_serve import FakePapaya, dispatch_worker, worker_event

globals().update({name: getattr(test_serve, name) for name in ("registered_repo",)})

ENV = {
    "PAPAYA_API_URL": "http://papaya.test",
    "PAPAYA_WORKSPACE_ID": "ws-1",
    "PAPAYA_AGENT_TOKEN": "pagc_test_token",
}


# ── the world ───────────────────────────────────────────────────────────────


def delivered(item: str = "item-9", *, requires_up_to_date: bool | None = None) -> tuple[int, int]:
    """A ticket whose worker delivered a pull request and whose hold ended."""
    conn = init_db()
    try:
        run_id = store.create_run(conn, f"Fix {item}")
        ticket = store.add_task(conn, run_id=run_id, title=f"Fix {item}")
        event = papaya_events.PapayaEvent(
            id=f"ev-{item}",
            kind="work_item.assigned",
            subject=f"work_item:{item}",
            payload={},
            work_item_id=item,
        )
        papaya_events.record_task(conn, ticket, event)
        for phase in (
            serve.PHASE_PICKED_UP,
            serve.PHASE_BRIEFING,
            serve.PHASE_DISPATCHED,
            serve.PHASE_REVIEWING,
            serve.PHASE_DELIVERING,
            serve.PHASE_REPORTED,
            serve.PHASE_RELEASED,
        ):
            serve.record_phase(conn, ticket, phase)
    finally:
        conn.close()
    worker = dispatch_worker(run_id)
    worker_event(
        worker,
        "delivered",
        status="delivered",
        pr_url=f"https://github.com/acme/runtime/pull/{worker}",
        requires_up_to_date=requires_up_to_date,
    )
    return ticket, worker


def pr(worker: int, **fields: Any) -> dict[str, Any]:
    return {
        "known": True,
        "task_id": worker,
        "status": "delivered",
        "branch": f"ppy/task-{worker}",
        "pr": worker,
        "url": f"https://github.com/acme/runtime/pull/{worker}",
        "base": "main",
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "merge_state": "CLEAN",
        "ci": "pass",
        "failing": [],
        "merged": False,
        "review": "",
        "head": "a" * 40,
        "created_at": "2026-09-16T10:00:00Z",
        "threads": [],
        "comments": [],
        **fields,
    }


class World:
    """The rounds, with the forge, clock, Papaya and merges in a test's hands."""

    def __init__(self, **seams: Any) -> None:
        self.forge: list[dict[str, Any]] = []
        self.clock = WallClock()
        self.papaya = FakePapaya()
        self.walker = rounds.Rounds(
            SimpleNamespace(loop=None, agent_config={}),
            SimpleNamespace(held={}, _opener_kwargs=lambda: {"opener": self.papaya}),
            clock=self.clock,
            forge=lambda _conn: [dict(entry) for entry in self.forge],
            prune=lambda _task_id: {"removed": [], "skipped": [], "reclaimed_bytes": 0},
            git=lambda *_a, **_k: 0,
            pr_details=lambda _worker, _entry: {"log_tail": "FAILED test_things"},
            papaya_env=lambda: dict(ENV),
            **seams,
        )

    def round(self) -> list[str]:
        return asyncio.run(self.walker.round_once())

    def comments(self) -> list[str]:
        return [body for _item, body in self.papaya.comments()]


def attention(worker: int) -> list[dict[str, Any]]:
    return events_of(worker, serve.PR_ATTENTION)


def the_worker_fixed_it(ticket: int, worker: int) -> None:
    """What a finished attempt leaves: the worker delivered again and the hold ended."""
    worker_event(worker, "delivered", status="delivered")
    conn = init_db()
    try:
        serve.record_phase(conn, ticket, serve.PHASE_REPORTED)
        serve.record_phase(conn, ticket, serve.PHASE_RELEASED)
    finally:
        conn.close()


def phase(task_id: int) -> str | None:
    conn = init_db()
    try:
        return store.task_phase(conn, task_id)
    finally:
        conn.close()


# ── reasons, and when they are raised again ─────────────────────────────────


def test_a_conflicting_pr_raises_once_per_head(ppy_home) -> None:
    ticket, worker = delivered()
    world = World()
    world.forge = [pr(worker, mergeable="CONFLICTING", merge_state="DIRTY")]

    first = world.round()
    ((raised,),) = [attention(worker)]
    assert "rebase onto main and resolve conflicts" in raised["summary"]
    assert raised["head"] == "a" * 40
    assert phase(ticket) == serve.PHASE_DISPATCHED
    assert any("rebase onto main" in line for line in first)

    # Same conflict, same head: nothing new, and nothing said.
    assert world.round() == []
    assert len(attention(worker)) == 1

    # The worker pushed; the new head still conflicts. That is news.
    the_worker_fixed_it(ticket, worker)
    world.forge = [pr(worker, mergeable="CONFLICTING", merge_state="DIRTY", head="b" * 40)]
    world.round()
    assert [a["head"] for a in attention(worker)] == ["a" * 40, "b" * 40]
    (finished,) = events_of(worker, reconcile.FINISHED)
    assert finished["outcome"] == reconcile.OUTCOME_FIXED


@pytest.mark.parametrize("required", [True, False], ids=["up-to-date-required", "not-required"])
def test_a_behind_pr_raises_only_where_delivery_recorded_the_requirement(
    ppy_home, required
) -> None:
    _ticket, worker = delivered(requires_up_to_date=required)
    world = World()
    world.forge = [pr(worker, merge_state="BEHIND")]

    world.round()

    if required:
        (raised,) = attention(worker)
        assert raised["reasons"] == [
            f"PR #{worker} is behind main, which requires up-to-date branches: update the branch"
        ]
    else:
        assert attention(worker) == []


def test_a_merge_refused_for_an_out_of_date_branch_counts_as_the_requirement(ppy_home) -> None:
    _ticket, worker = delivered()
    worker_event(worker, "merge_refused", reason="up_to_date_required")
    world = World()
    world.forge = [pr(worker, merge_state="BEHIND")]

    world.round()

    assert "update the branch" in attention(worker)[0]["summary"]


def test_an_unresolved_review_thread_raises_with_its_file_and_first_line(ppy_home) -> None:
    _ticket, worker = delivered()
    world = World()
    thread = {
        "id": "PRRT_1",
        "path": "src/things.py",
        "line": 12,
        "author": "shane",
        "body": "This should be a 404, not a 500.\nAnd log it.",
    }
    world.forge = [pr(worker, threads=[thread])]

    world.round()
    world.round()

    (raised,) = attention(worker)
    assert raised["reasons"] == [
        f"unresolved review thread on PR #{worker} at src/things.py:12 from shane: "
        "This should be a 404, not a 500."
    ]
    assert raised["threads"] == [thread]


def test_a_check_pending_past_the_learned_ci_budget_raises_once(ppy_home, registered_repo) -> None:
    _ticket, worker = delivered()
    for seconds in (400, 420, 440):
        budgets.observe(registered_repo, budgets.CI, seconds)
    budget = reconcile.ci_budget_seconds(worker)
    assert budget is not None and budgets.budget(registered_repo, budgets.CI).source == "derived"
    world = World()
    started = world.clock.now - timedelta(seconds=budget - 60)
    world.forge = [
        pr(
            worker,
            ci="pending",
            pending_checks=[{"name": "unit", "started_at": started.isoformat()}],
        )
    ]

    world.round()
    assert attention(worker) == [], "raised inside the budget"
    world.clock.advance(minutes=2)
    world.round()
    world.clock.advance(minutes=5)
    world.round()

    (raised,) = attention(worker)
    assert raised["summary"].startswith(f"CI stuck on PR #{worker}: unit pending for ")
    assert raised["summary"].endswith("budget: rerun or look")


# ── green and nobody merging ────────────────────────────────────────────────


@pytest.mark.parametrize("auto_merge", [False, True], ids=["comment", "auto-merge"])
def test_green_and_unmerged_for_a_day_comments_once_or_merges(
    ppy_home, registered_repo, auto_merge
) -> None:
    if auto_merge:
        repos.set_settings(registered_repo, auto_merge=True, merge_method="rebase")
    ticket, worker = delivered()
    merges: list[tuple[int, str]] = []

    def merge(task_id: int, _entry: dict[str, Any], method: str) -> Any:
        merges.append((task_id, method))
        return SimpleNamespace(merged=True, detail="merged")

    world = World(merge=merge)
    world.forge = [pr(worker)]

    world.round()
    world.clock.advance(minutes=23 * 60)
    world.round()
    assert world.comments() == [] and merges == []
    world.clock.advance(minutes=2 * 60)
    world.round()
    world.clock.advance(minutes=60)
    world.round()

    url = f"https://github.com/acme/runtime/pull/{worker}"
    if auto_merge:
        assert merges == [(worker, "rebase")]
        assert phase(ticket) == serve.PHASE_DONE
        [said] = world.comments()
        assert said.startswith(f"Merged: {url}.") and "Should it move" in said
    else:
        assert merges == []
        assert world.comments() == [f"PR {worker} has been green and unmerged for a day: {url}"]
        assert phase(ticket) == serve.PHASE_RELEASED


# ── the rule the worker is given ────────────────────────────────────────────


def test_the_brief_the_environment_block_and_the_rules_carry_the_pr_follow_rule() -> None:
    from papaya_agent_runtime import environment

    def flat(text: str) -> str:
        return " ".join(text.split())

    block = environment.render(
        environment.RepoEnvironment(repo="app"),
        task_id=7,
        evidence_path="/tmp/wt/.ppy-evidence",
        branch="ppy/task-7-abc",
    )
    texts = {
        "brief.md": prompts.load(prompts.BRIEF),
        "environment block": block,
        "command rules": command_rules("claude", "ppy/task-7-abc"),
    }
    for where, text in texts.items():
        assert flat(prompts.PR_FOLLOW_RULE) in flat(text), where
        assert flat(prompts.TEN_MINUTE_RULE) in flat(text), where
        assert flat(prompts.PUSH_MILESTONE_RULE) in flat(text), where


# ── the reconcile lane ──────────────────────────────────────────────────────


@pytest.fixture
def supervisor(ppy_home):
    built: list[Supervisor] = []

    def make(**kwargs: Any) -> Supervisor:
        sup = Supervisor(**kwargs)
        built.append(sup)
        return sup

    yield make
    for sup in built:
        sup.close()


def recorded_specs(sup: Supervisor, *, running: list[Any] | None = None) -> list[Any]:
    """Stand in for the provider process: keep the spec, and give the slot back at once.

    With ``running``, the execution is kept there instead, as a process still going.
    """
    specs: list[Any] = []

    def run_task(_runner: Any, spec: Any, execution: Any = None) -> None:
        specs.append(spec)
        if running is None:
            sup._release(execution)
        else:
            running.append(execution)

    sup._run_task = run_task  # type: ignore[method-assign]
    return specs


def delivered_worker(sup: Supervisor, repo: str) -> dict[str, Any]:
    """A fake worker that finished, and the delivery `ppy deliver` would have recorded."""
    resp = sup.dispatch_task(
        repo=repo, title="the endpoint", instructions="build it", provider="fake"
    )
    task_id = int(resp["task_id"])
    wait_until(
        lambda: store.get_task(init_db(), task_id)["status"] == "worker_done",
        15,
        what="the fake worker to finish",
    )
    wait_until(
        lambda: not store.live_runners_for_task(init_db(), task_id), 15, what="its runner to exit"
    )
    worker_event(task_id, "delivered", status="delivered", pr_url="https://github.com/a/b/pull/7")
    worker_event(
        task_id,
        serve.PR_ATTENTION,
        summary="merge conflicts on PR #7: rebase onto main and resolve conflicts",
        reasons=["merge conflicts on PR #7: rebase onto main and resolve conflicts"],
        url="https://github.com/a/b/pull/7",
        base="main",
        conflicting_files=["src/things.py"],
        threads=[{"path": "src/things.py", "line": 3, "author": "shane", "body": "Why 500?"}],
    )
    return resp


def test_pr_attention_runs_in_the_lane_with_every_ticket_slot_busy_and_queues_by_readiness(
    supervisor, source_repo
) -> None:
    added = repos.add_repo(source_repo)
    sup = supervisor()
    first = delivered_worker(sup, added.name)
    second = delivered_worker(sup, added.name)
    for n in range(WorkerCeiling().max_concurrent):
        sup.dispatch_task(
            repo=added.name, title=f"ticket {n}", instructions="HOLD:30", provider="fake"
        )
    with pytest.raises(SupervisorError, match="worker capacity is full"):
        sup.dispatch_task(repo=added.name, title="one ticket too many", provider="fake")

    # Every ticket slot busy: the pull request fix is admitted at once, in the lane.
    fixing: list[Any] = []
    specs = recorded_specs(sup, running=fixing)
    resumed = sup.steer_task(first["task_id"], "Rebase onto main.")
    assert resumed["lane"] == LANE_RECONCILE
    assert [s.task_id for s in specs] == [first["task_id"]]
    # The lane is one slot: a second fix waits for it, and a ticket is still refused by
    # ticket capacity, never admitted in the lane.
    with pytest.raises(SupervisorError, match="reconcile lane is full"):
        sup.steer_task(second["task_id"], "Update the branch.")
    with pytest.raises(SupervisorError, match="worker capacity is full"):
        sup.dispatch_task(repo=added.name, title="one ticket too many", provider="fake")
    for execution in fixing:
        sup._release(execution)
    sup.steer_task(second["task_id"], "Update the branch.")
    assert [s.task_id for s in specs] == [first["task_id"], second["task_id"]]

    # The rounds' side: with the lane busy, two pull requests queue, and the one closest
    # to merging (behind only) starts first when the lane frees, though it queued last.
    busy_ticket, busy_worker = delivered("item-busy")
    threads_ticket, threads_worker = delivered("item-threads")
    behind_ticket, behind_worker = delivered("item-behind", requires_up_to_date=True)
    world = World()
    world.forge = [pr(busy_worker, ci="fail", failing=["unit"])]
    world.round()
    assert len(attention(busy_worker)) == 1
    thread = {"id": "T1", "path": "a.py", "line": 1, "author": "shane", "body": "Rename it."}
    world.forge.append(pr(threads_worker, threads=[thread]))
    world.round()
    world.forge.append(pr(behind_worker, merge_state="BEHIND"))
    lines = world.round()
    assert attention(threads_worker) == [] and attention(behind_worker) == []
    assert any("waits its turn" in line for line in lines)
    assert [p["task_id"] for _id, p in reconcile.pending_queue()] == [threads_worker, behind_worker]

    the_worker_fixed_it(busy_ticket, busy_worker)
    world.forge[0] = pr(busy_worker, head="c" * 40)
    world.round()
    assert len(attention(behind_worker)) == 1 and attention(threads_worker) == []
    assert phase(behind_ticket) == serve.PHASE_DISPATCHED
    assert phase(threads_ticket) == serve.PHASE_RELEASED
    status = reconcile.lane_status()
    assert status.startswith(f"reconciling https://github.com/acme/runtime/pull/{behind_worker}")
    assert status.endswith(", 1 queued")


def test_a_resumable_session_is_resumed_and_a_dead_one_gets_a_reconciler_on_the_same_branch(
    supervisor, source_repo
) -> None:
    added = repos.add_repo(source_repo)

    live = supervisor()
    resumable = delivered_worker(live, added.name)
    live_specs = recorded_specs(live)
    live.steer_task(resumable["task_id"], "Rebase onto main and resolve src/things.py.")
    (resumed,) = live_specs
    assert resumed.resume_session_id
    assert "Rebase onto main" in (resumed.steer_message or "")

    dead = supervisor(session_resumable=lambda _task, _session: False)
    gone = delivered_worker(dead, added.name)
    dead_specs = recorded_specs(dead)
    dead.steer_task(gone["task_id"], "Rebase onto main and resolve src/things.py.")
    (reconciler,) = dead_specs
    assert reconciler.resume_session_id is None
    brief = reconciler.instructions
    for fact in (
        "# Reconcile: fix one pull request",
        "- pull request: https://github.com/a/b/pull/7",
        "- base to rebase onto: main",
        f"- branch to push to: {gone['branch']}",
        "src/things.py",
        "src/things.py:3 (shane): Why 500?",
        "Never open another pull request.",
        "`ppy gate run --full`",
        "Rebase onto main and resolve src/things.py.",
    ):
        assert fact in brief, fact

    # Same task, same branch, same worktree: both push where the pull request reads.
    assert resumed.branch == resumable["branch"] and resumed.task_id == resumable["task_id"]
    assert reconciler.branch == gone["branch"] and reconciler.task_id == gone["task_id"]
    assert reconciler.worktree_path == gone["worktree_path"]
    events = [
        json.loads(r["payload"])
        for r in init_db()
        .execute("SELECT payload FROM events WHERE kind = 'resumed' ORDER BY id")
        .fetchall()
    ]
    assert [(e["lane"], e["reconciler"]) for e in events] == [
        (LANE_RECONCILE, False),
        (LANE_RECONCILE, True),
    ]


def test_a_merge_on_the_record_closes_the_lane_the_forge_stopped_listing(ppy_home) -> None:
    """`ppy deliver --merged` ends the attempt: nothing later can, so nothing else will."""
    _ticket, worker = delivered()
    world = World()
    world.forge = [pr(worker, ci="fail", failing=["unit"])]
    world.round()
    assert len(reconcile.open_lane()) == 1

    # The merge on the row takes the branch out of `watch.pr_states` for good, so no
    # round after this one can say where the head landed.
    delivery.record_merged(worker, "c" * 40)
    world.forge = []

    assert reconcile.open_lane() == []
    (finished,) = events_of(worker, reconcile.FINISHED)
    assert finished["outcome"] == reconcile.OUTCOME_MERGED
    assert reconcile.lane_status() == "idle"

    # Run it again on the same merge: idempotent, and it says the lane was already free.
    assert "closed" not in delivery.record_merged(worker, "c" * 40).note
    assert len(events_of(worker, reconcile.FINISHED)) == 1


def test_deliver_merged_again_clears_a_lane_an_earlier_merge_left_open(ppy_home) -> None:
    """The merge recorded first, the attempt left open: re-running --merged frees it."""
    _ticket, worker = delivered()
    world = World()
    world.forge = [pr(worker, ci="fail", failing=["unit"])]
    world.round()
    (attempt,) = reconcile.open_lane()

    delivery.record_merged(worker, "c" * 40)
    # An open attempt that predates the merge: what task 30 left behind, for 8h.
    reconcile.record(
        worker,
        reconcile.STARTED,
        fingerprint=attempt.payload.get("fingerprint"),
        head=attempt.payload.get("head"),
        pr=worker,
        at="2026-09-17T12:21:26+00:00",
    )
    assert len(reconcile.open_lane()) == 1

    note = delivery.record_merged(worker, "c" * 40).note

    assert "closed 1 open reconcile attempt(s)" in note
    assert reconcile.open_lane() == []
    assert reconcile.lane_status() == "idle"


def test_an_attempt_the_forge_stops_listing_does_not_hold_the_lane(ppy_home) -> None:
    """A pull request gone from the forge closes its attempt, rather than a slot forever."""
    ticket, worker = delivered()
    world = World()
    world.forge = [pr(worker, ci="fail", failing=["unit"])]
    world.round()
    assert len(reconcile.open_lane()) == 1

    the_worker_fixed_it(ticket, worker)
    world.forge = []  # closed, merged elsewhere, or no longer watched
    world.round()

    (finished,) = events_of(worker, reconcile.FINISHED)
    assert finished["outcome"] == reconcile.OUTCOME_ENDED
    assert reconcile.lane_status() == "idle"


def test_two_failed_attempts_at_one_head_mark_needs_a_person_once_and_stop(ppy_home) -> None:
    ticket, worker = delivered()
    world = World()
    world.forge = [pr(worker, ci="fail", failing=["unit"])]

    for _attempt in range(2):
        world.round()
        # The attempt ends with nothing pushed: the head is where it was.
        the_worker_fixed_it(ticket, worker)
    for _later in range(3):
        world.round()

    assert len(attention(worker)) == 2
    outcomes = [f["outcome"] for f in events_of(worker, reconcile.FINISHED)]
    assert outcomes == [reconcile.OUTCOME_FAILED, reconcile.OUTCOME_FAILED]
    assert len(events_of(worker, reconcile.NEEDS_A_PERSON)) == 1
    assert phase(ticket) == serve.PHASE_NEEDS_A_PERSON
    (said,) = world.comments()
    assert said.startswith(f"https://github.com/acme/runtime/pull/{worker} needs a person")
    assert f"- CI failing on PR #{worker}: unit" in said

    # A new head is a new state: it is raised again.
    world.forge = [pr(worker, ci="fail", failing=["unit"], head="d" * 40)]
    world.round()
    assert len(attention(worker)) == 3
