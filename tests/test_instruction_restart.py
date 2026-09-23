"""A person's request that a restart interrupted, what is said about it, and what it is called.

Task 372, from the local end-to-end run of 2026-09-23:

- a restart released a held instruction and no round took it back up, so Papaya kept it
  `picked_up` with nobody holding it, and its stale blocker kept being reported;
- outreach about an instruction went "nowhere it could reach" instead of where the
  person asked;
- a finished work instruction said "Pull request open" twice, and its final reply was
  the worker's raw closeout;
- `MI-<n>` reached a pull request's title and the published status report.

The restart rows run the real `ppy serve` loop over a ledger left as a restart leaves
it: its start reclaim offers the request back, and the hold resumes from its state.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import test_instruction_serve as tis
import test_serve
from papaya_agent_runtime import (
    delivery,
    instructions,
    machine_status,
    outreach,
    papaya_events,
    pr_body,
    prompts,
    rounds,
    serve,
    sweep,
)
from papaya_agent_runtime.state import store
from papaya_agent_runtime.state.db import init_db

globals().update(
    {name: getattr(test_serve, name) for name in ("client_home", "ready", "registered_repo")}
)

Routes = tis.Routes
FakeEvents = test_serve.FakeEvents
FakeTurns = test_serve.FakeTurns
_until = test_serve._until
PR = "https://github.com/acme/runtime/pull/7"
SPIKE = "Quick spike on CSV export in runtime: implement it and show me the PR"


def _seed(
    text: str,
    found: instructions.Classification,
    *,
    phases: tuple[str, ...],
    said: tuple[str, ...] = (),
    origin: str = "channel",
    intent: str | None = None,
) -> tuple[int, int, papaya_events.Instruction]:
    """A ticket as a hold leaves it when the process under it goes away."""
    payload = tis.instruction_event(text, origin=origin, intent=intent)["payload"]
    instruction = papaya_events.instruction_from(payload)
    event = papaya_events.PapayaEvent(
        id="301", kind=papaya_events.MACHINE_INSTRUCTION, subject=tis.SUBJECT, payload=payload
    )
    conn = init_db()
    try:
        task_id, run_id, _existed = instructions.record_ticket(conn, event, instruction, found.repo)
        instructions.record_classified(conn, task_id, found)
        for phase in phases:
            serve.record_phase(conn, task_id, phase)
        for key in said:
            instructions.record_said(conn, task_id, key)
    finally:
        conn.close()
    return task_id, run_id, instruction


def _phase(task_id: int) -> str | None:
    conn = init_db()
    try:
        return store.task_phase(conn, task_id)
    finally:
        conn.close()


def _unfinished() -> list[rounds.InstructionTicket]:
    conn = init_db()
    try:
        return rounds.unfinished_instructions(conn)
    finally:
        conn.close()


def _todo(task_id: int, text: str, blocked_on: str = "user") -> int:
    conn = init_db()
    try:
        return store.add_todo(conn, text, task_id=task_id, blocked_on=blocked_on)
    finally:
        conn.close()


def _open_todos() -> list[str]:
    conn = init_db()
    try:
        return [
            str(row["text"])
            for row in conn.execute("SELECT text FROM todos WHERE status = 'open'").fetchall()
        ]
    finally:
        conn.close()


def _serve_empty(harness, client_home, the_runner, *, during=None) -> int:
    """`ppy serve` with nothing in the stream: only its start reclaim can take work."""

    async def scenario() -> int:
        task = test_serve._serve_ticket(harness, client_home, the_runner)
        if during is not None:
            await during()
        await _until(lambda: harness.results, what="the request to be released", timeout=10)
        harness.loop.request_stop()
        return await task

    return asyncio.run(scenario())


# ── Goal 1: a restart does not lose a request ────────────────────────────────


def test_restart_while_a_work_instructions_worker_runs_resumes_it_and_says_nothing_twice(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """Matrix row 1: reclaimed, the worker resumed, one "On it" never repeated."""
    found = instructions.Classification(instructions.WORK, repo="runtime", reason="work in runtime")
    task_id, run_id, _inst = _seed(
        SPIKE,
        found,
        phases=(serve.PHASE_PICKED_UP, serve.PHASE_BRIEFING, serve.PHASE_DISPATCHED),
        said=(serve.SAID_ON_IT, serve.PHASE_DISPATCHED),
    )
    worker = test_serve.dispatch_worker(run_id, repo="runtime")
    conn = init_db()
    try:
        instructions.mark_worker(conn, worker, "MI-42")
        # The shutdown's cancel: the hold ended, the request did not.
        serve.record_phase(conn, task_id, serve.PHASE_RELEASED)
    finally:
        conn.close()
    assert [t.task_id for t in _unfinished()] == [task_id]

    def act(turn: test_serve.Turn) -> str:
        assert turn.name == prompts.REVIEW
        test_serve.worker_event(worker, "reviewed", verdict="approved")
        test_serve.worker_event(worker, "delivered", status="delivered", pr_url=PR)
        return "OUTCOME: done\nCSV export is in behind a flag; the tests pass."

    dispatched: list[Any] = []
    routes = Routes()
    harness = tis.InstructionHarness(FakeEvents([]))
    the_runner = tis.runner(
        FakeTurns(act),
        routes,
        instruction_dispatch=lambda *args: dispatched.append(args),
        branch_ahead=lambda _task: True,
    )

    async def worker_finishes() -> None:
        await _until(lambda: harness.events.reserves, what="the reclaim's reserve")
        test_serve.worker_event(worker, "worker_done", status="worker_done", summary="done")

    assert _serve_empty(harness, client_home, the_runner, during=worker_finishes) == 0
    # Re-reserved as the instruction it came as, and resumed on its own ticket and worker.
    assert harness.events.reserves[0][0] == tis.SUBJECT
    assert dispatched == []
    said = tis.contents(routes)
    assert said == [
        "Reviewing the work.",
        f"CSV export is in behind a flag; the tests pass.\n\nPull request open: {PR}",
    ]
    assert len(routes.results()) == 1 and routes.results()[0]["status"] == "done"
    assert _unfinished() == []


def test_restart_while_an_asks_turn_runs_is_answered_once_and_its_stale_blocker_cleared(
    ppy_home, client_home, ready
) -> None:
    """Matrix row 2: taken back up and answered, never left `picked_up`."""
    found = instructions.Classification(instructions.ANSWER, reason="answered from its state")
    # The process died under the answer turn: nothing after `picked_up` was written.
    task_id, run_id, _inst = _seed(
        "What are you working on?", found, phases=(serve.PHASE_PICKED_UP,)
    )
    _todo(task_id, "MI-42 blocked: the gate is red")
    turns = FakeTurns(lambda _turn: "OUTCOME: done\nNothing else is running.")
    routes = Routes()
    harness = tis.InstructionHarness(FakeEvents([]))
    assert _serve_empty(harness, client_home, tis.runner(turns, routes)) == 0
    assert turns.names() == [prompts.INSTRUCTION]
    assert tis.contents(routes) == ["Nothing else is running."]
    assert [r["status"] for r in routes.results()] == ["done"]
    assert _unfinished() == []
    # Goal 1: the block it recorded no longer applies once the request is answered.
    assert _open_todos() == []
    assert _phase(task_id) == serve.PHASE_RELEASED


class OfferLoop:
    """The client loop a reclaim offers to, answering as told; a refusal names its holder."""

    def __init__(self, answer: str, holder: dict[str, Any] | None = None) -> None:
        self.answer = answer
        self.holder = holder
        self.offered: list[dict[str, Any]] = []
        self.running_subjects: set[str] = set()
        self._events = SimpleNamespace(reserve=self._reserve)

    async def _reserve(self, subject: str, *_args: Any, **_kwargs: Any) -> Any:
        from papaya_agent_client.api_client import SubjectHeld

        raise SubjectHeld(subject, self.holder, None)

    async def offer(self, envelope: dict[str, Any]) -> str:
        from papaya_agent_client.api_client import SubjectHeld

        self.offered.append(envelope)
        if self.holder is not None:
            # Papaya's 409, as the client meets it: the offer answers `done`.
            try:
                await self._events.reserve(envelope["subject"], "session-1")
            except SubjectHeld:
                return "done"
        return self.answer


def _walker(loop: OfferLoop, routes: Routes) -> rounds.Rounds:
    async def published(_why: str) -> None:
        return None

    return rounds.Rounds(
        SimpleNamespace(loop=loop, agent_config={"agent_id": "agent-1", "workspace_id": tis.WS}),
        SimpleNamespace(held={}),
        papaya_env=lambda: dict(tis.JOB_ENV),
        status_publisher=SimpleNamespace(publish=published),
        instruction_post=functools.partial(papaya_events.post_instruction_reply, opener=routes),
        instruction_report=functools.partial(
            papaya_events.report_instruction_result, opener=routes
        ),
    )


def test_the_reclaim_offers_the_instruction_as_it_came(ppy_home) -> None:
    found = instructions.Classification(instructions.ANSWER, reason="answered")
    task_id, _run, instruction = _seed(
        "What are you working on?", found, phases=(serve.PHASE_PICKED_UP, serve.PHASE_RELEASED)
    )
    loop, routes = OfferLoop(sweep.OFFER_PENDING), Routes()
    parts = asyncio.run(_walker(loop, routes)._reclaim_instructions())
    (envelope,) = loop.offered
    assert envelope["kind"] == papaya_events.MACHINE_INSTRUCTION
    assert envelope["subject"] == tis.SUBJECT
    assert envelope["agent_id"] == "agent-1" and envelope["workspace_id"] == tis.WS
    assert papaya_events.instruction_from(envelope["payload"]) == instruction
    assert parts == [f'took request "What are you working on?" back up (ticket task {task_id})']
    assert routes.calls == []
    # A busy loop is tried again next round; nothing is closed.
    loop.answer = sweep.OFFER_BLOCKED
    assert asyncio.run(_walker(loop, routes)._reclaim_instructions()) == []
    assert routes.calls == [] and [t.task_id for t in _unfinished()] == [task_id]


@pytest.mark.parametrize(
    "holder", [None, {"connection_id": "conn-2", "session_id": "s-2"}], ids=["done", "held"]
)
def test_a_refused_reclaim_tells_the_person_once_and_closes_the_ticket(ppy_home, holder) -> None:
    """Matrix row 3: Papaya refuses the re-reserve; closed here, said once, reported."""
    found = instructions.Classification(instructions.WORK, repo=None, reason="work")
    task_id, _run, _inst = _seed(
        "fix the flaky test", found, phases=(serve.PHASE_PICKED_UP, serve.PHASE_RELEASED)
    )
    _todo(task_id, "MI-42 blocked: the gate is red. MI-7 write guard did not block it.")
    loop, routes = OfferLoop("done", holder), Routes()
    walker = _walker(loop, routes)
    parts = asyncio.run(walker._reclaim_instructions())
    assert parts == [
        'could not take request "fix the flaky test" back up: told the person and '
        f"closed ticket task {task_id}"
    ]
    (said,) = tis.contents(routes)
    held_by = " (it is held by " if holder else ""
    assert said.startswith(
        'I couldn\'t finish "fix the flaky test": this machine restarted while working on '
        "it and could not take it back up" + held_by
    )
    assert "It was waiting on: your request blocked: the gate is red." in said
    assert not re.search(r"MI-\d", said) and "write guard" not in said
    (reply,) = routes.replies()
    assert reply["parent_id"] == "root-1"
    (result,) = routes.results()
    assert result["status"] == "failed"
    assert _phase(task_id) == serve.PHASE_DONE
    assert _open_todos() == []
    # Nothing is said twice: the next round finds nothing to take back up.
    assert asyncio.run(walker._reclaim_instructions()) == []
    assert len(routes.replies()) == 1 and len(loop.offered) == 1


# ── Goal 2: what waits on the person is said where they asked ────────────────


@pytest.mark.parametrize("origin", ["dm", "channel"])
def test_an_ask_about_a_request_is_said_where_it_was_asked_once_per_change(
    ppy_home, origin
) -> None:
    """Matrix row 4: a DM origin and a channel origin, as a progress reply."""
    found = instructions.Classification(instructions.WORK, repo=None, reason="work")
    task_id, run_id, _inst = _seed(
        "fix the flaky test", found, phases=(serve.PHASE_PICKED_UP,), origin=origin, intent="work"
    )
    conn = init_db()
    try:
        worker = store.add_task(conn, run_id=run_id, title="worker")
    finally:
        conn.close()
    todo = _todo(worker, "MI-42 blocked: which fixture should the test use?")
    routes = Routes()
    post = functools.partial(papaya_events.post_instruction_reply, opener=routes)

    def origin_post(request: int, body: str) -> bool:
        return outreach.post_origin(request, body, environ=dict(tis.JOB_ENV), post=post)

    def nowhere(*_args: Any) -> bool:
        raise AssertionError("an ask about a request is said only where it was asked")

    def step(now: datetime) -> list[str]:
        conn = init_db()
        try:
            return outreach.step(
                conn,
                now=now,
                host="mac",
                dm=nowhere,
                ticket=nowhere,
                desktop=nowhere,
                origin=origin_post,
            )
        finally:
            conn.close()

    lines = step(datetime(2026, 9, 23, 10, 0, tzinfo=UTC))
    assert "said to a person (origin): MI-42 blocked: which fixture should the test use?" in lines
    (reply,) = routes.replies()
    assert reply["kind"] == papaya_events.REPLY_PROGRESS
    body = reply.get("content") or reply.get("text")
    assert body == (
        "This needs you before I can go on:\n"
        "- your request blocked: which fixture should the test use?\n"
        "  Reply here with your answer."
    )
    path = routes.calls[0][1]
    if origin == "dm":
        assert path.endswith("/dm-conversations/conv-1/replies")
    else:
        assert path.endswith("/channels/chan-1/messages") and reply["parent_id"] == "root-1"
    # Unchanged, it is never said again, however soon the next round.
    step(datetime(2026, 9, 23, 10, 5, tzinfo=UTC))
    assert len(routes.replies()) == 1
    # Changed, it is said again, once, without waiting out the DM's interval.
    conn = init_db()
    try:
        store.update_todo(conn, todo, text="MI-42 blocked: which fixture, now that A is gone?")
    finally:
        conn.close()
    step(datetime(2026, 9, 23, 10, 10, tzinfo=UTC))
    step(datetime(2026, 9, 23, 10, 15, tzinfo=UTC))
    assert len(routes.replies()) == 2
    assert _is_origin_only(task_id)


def _is_origin_only(task_id: int) -> bool:
    conn = init_db()
    try:
        rows = conn.execute("SELECT said_via FROM outreach").fetchall()
    finally:
        conn.close()
    return all(json.loads(row[0] or "[]") == [outreach.VIA_ORIGIN] for row in rows)


def test_the_rounds_say_a_requests_ask_at_its_origin(ppy_home) -> None:
    """The same, through `ppy serve`'s outreach lane: never "nowhere it could reach"."""
    found = instructions.Classification(instructions.WORK, repo=None, reason="work")
    task_id, _run, _inst = _seed("fix the flaky test", found, phases=(serve.PHASE_PICKED_UP,))
    _todo(task_id, "Which fixture should the test use?")
    routes = Routes()
    walker = _walker(OfferLoop(sweep.OFFER_PENDING), routes)
    lines = asyncio.run(walker._outreach_lane(datetime(2026, 9, 23, 10, 0, tzinfo=UTC)))
    assert "said to a person (origin): Which fixture should the test use?" in lines
    assert not any("nowhere" in line for line in lines)
    assert tis.contents(routes)[0].startswith("This needs you before I can go on:")


# ── Goal 3 and 5: one pull request line, one summary ─────────────────────────


def test_a_work_instruction_whose_review_wrote_no_summary_still_says_the_pr_once(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """Matrix row 5: no work-item read, no fallback, no worker closeout."""
    runs: list[int] = []
    titles: list[str] = []

    def dispatch(repo: str, brief: str, run_id: int, title: str) -> None:
        runs.append(run_id)
        titles.append(title)
        test_serve.dispatch_worker(run_id, repo=repo)

    def act(turn: test_serve.Turn) -> None:
        (worker,) = test_serve.workers_in(turn.run_id)
        test_serve.worker_event(worker, "reviewed", verdict="approved")
        test_serve.worker_event(worker, "delivered", status="delivered", pr_url=PR)

    closeout = (
        "Pushed ce6cf54 to ppy/task-6-csv, ready for PR. Evidence: .ppy-evidence/pytest.txt. "
        "MI-3 write guard did not block this worktree."
    )
    routes = Routes()
    harness = tis.InstructionHarness(FakeEvents([tis.instruction_event(SPIKE)]))
    the_runner = tis.runner(
        FakeTurns(act), routes, instruction_dispatch=dispatch, branch_ahead=lambda _t: True
    )

    async def scenario() -> int:
        task = test_serve._serve_ticket(harness, client_home, the_runner)
        await _until(lambda: runs and test_serve.workers_in(runs[0]), what="the dispatch")
        (worker,) = test_serve.workers_in(runs[0])
        from papaya_agent_runtime import progress

        progress.record(worker, phase="done", note=closeout)
        test_serve.worker_event(worker, "worker_done", status="worker_done", summary="done")
        await _until(lambda: harness.results, what="the request to be released", timeout=10)
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0
    said = tis.contents(routes)
    assert said[-1] == (f'The work on "{SPIKE}" is done and reviewed.\n\nPull request open: {PR}')
    assert sum(text.count("Pull request open") for text in said) == 1
    assert not any("ce6cf54" in text or "write guard" in text for text in said)
    # The report step read no work item: an instruction has none.
    assert all(path.endswith("/follow-ups") for path in routes.reads())
    # Goal 4: the worker, and so its pull request, is titled as the person titled it.
    assert titles == [SPIKE]


# ── Goal 4: no `MI-<n>` where a person reads ──────────────────────────────────


def _instruction_worker(title: str, *, key: str | None = "MI-4") -> int:
    conn = init_db()
    try:
        run = store.create_run(conn, "run")
        worker = store.add_task(conn, run_id=run, title=title)
        if key:
            store.set_task_env(conn, worker, instructions.INSTRUCTION_KEY, key)
        return worker
    finally:
        conn.close()


def test_a_pull_request_for_a_request_names_no_request_id(ppy_home) -> None:
    """Matrix row 6, the pull request half: its title and the body a person reads."""
    worker = _instruction_worker("MI-4: In the sandbox repo, update word_count")
    other = _instruction_worker("MI-12: the tracker's own key", key=None)
    conn = init_db()
    try:
        assert delivery._pr_title(conn, worker, "MI-4: In the sandbox repo, update word_count") == (
            "In the sandbox repo, update word_count"
        )
        body = pr_body.for_request(conn, worker, 'Done for MI-4.\n\nTracked as "MI-4: x".')
        assert "MI-" not in body
        # Elsewhere `MI-12` may be another tracker's key: left as it is.
        assert delivery._pr_title(conn, other, "MI-12: the tracker's own key") == (
            "MI-12: the tracker's own key"
        )
    finally:
        conn.close()


def test_the_published_status_report_names_requests_by_title(ppy_home) -> None:
    """Matrix row 6, the status half: in flight, waiting, blocked and finished rows."""
    found = instructions.Classification(instructions.ANSWER, reason="answered")
    task_id, _run, _inst = _seed("What are you working on?", found, phases=(serve.PHASE_PICKED_UP,))
    _todo(task_id, "MI-3 blocked: the gate is red")
    _todo(task_id, "MI-3 is waiting on the CI runner", blocked_on="ci")
    old = _instruction_worker("MI-4: In the sandbox repo, update word_count")
    conn = init_db()
    try:
        store.update_task_fields(conn, old, status="in_progress")
        body = machine_status.build(
            conn, health=machine_status.Health("ready", "All good."), max_concurrent=2
        )
    finally:
        conn.close()
    dumped = json.dumps(body)
    assert not re.search(r"MI-\d", dumped), dumped
    titles = {row["title"] for row in body["in_flight"]}
    assert "What are you working on?" in titles
    assert "In the sandbox repo, update word_count" in titles
    request = next(r for r in body["in_flight"] if r["title"] == "What are you working on?")
    assert request["ref"] == f"task-{task_id}"
    assert request["about"]["short_id"] == "What are you working on?"


# ── Goal 5: `for_person` drops, never garbles ────────────────────────────────


def test_for_person_drops_a_sentence_about_another_request_and_keeps_the_rest() -> None:
    instruction = papaya_events.instruction_from(tis.instruction_event("fix it")["payload"])
    text = (
        "MI-42: Fixed the export. Tests pass. MI-3 write guard did not block this worktree.\n"
        "\n"
        "MI-7 is next.\n"
        "\n"
        "Done with MI-42."
    )
    assert instructions.for_person(text, instruction) == (
        "Fixed the export. Tests pass.\n\nDone with your request."
    )
    assert "another request" not in instructions.for_person(text, instruction)
    # A path with a dot in it is not a sentence end.
    assert (
        instructions.for_person("Evidence: .ppy-evidence/pytest.txt. MI-3 was fine.", instruction)
        == "Evidence: .ppy-evidence/pytest.txt."
    )
