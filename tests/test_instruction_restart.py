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
Only what this process lost to its own shutdown (marked on the `released` phase) or a
crash (a holding phase with nobody holding) is taken back; a lost lease, a person's
release, a decline and anything Papaya has closed never are.
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
    capability_requests,
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
#: How a hold ended when this process shut down under it, and when its lease was lost.
SHUTDOWN, LOST_LEASE = "shutdown", "lost lease"


def _seed(
    text: str,
    found: instructions.Classification,
    *,
    phases: tuple[str, ...],
    released: str | None = None,
    said: tuple[str, ...] = (),
    origin: str = "channel",
    intent: str | None = None,
) -> tuple[int, int, papaya_events.Instruction]:
    """A ticket as a hold leaves it when the process under it goes away.

    ``released``: how the hold ended, when it did — :data:`SHUTDOWN` (the listener
    cancelled it) or :data:`LOST_LEASE` (Papaya took it back, or a person released it).
    """
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
        if released is not None:
            detail = instructions.SHUTDOWN if released == SHUTDOWN else ""
            serve.record_phase(conn, task_id, serve.PHASE_RELEASED, detail)
    finally:
        conn.close()
    return task_id, run_id, instruction


def _phase(task_id: int) -> str | None:
    conn = init_db()
    try:
        return store.task_phase(conn, task_id)
    finally:
        conn.close()


def _unfinished() -> list[int]:
    conn = init_db()
    try:
        return [t.task_id for t in rounds.unfinished_instructions(conn)]
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


@pytest.fixture
def open_at_papaya(monkeypatch) -> list[Any]:
    """Papaya's read of an instruction, as `ppy serve`'s own rounds make it: open."""
    reads: list[Any] = []

    def read(reply: Any, **_kwargs: Any) -> str:
        reads.append(reply)
        return "picked_up"

    monkeypatch.setattr(papaya_events, "read_instruction_status", read)
    return reads


# ── Goal 1: a restart does not lose a request ────────────────────────────────


def test_restart_while_a_work_instructions_worker_runs_resumes_it_and_says_nothing_twice(
    ppy_home, client_home, ready, registered_repo, open_at_papaya
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
        serve.record_phase(conn, task_id, serve.PHASE_RELEASED, instructions.SHUTDOWN)
    finally:
        conn.close()
    assert _unfinished() == [task_id]

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
    # Papaya was asked first; then re-reserved as the instruction it came as, and resumed
    # on its own ticket and worker.
    assert len(open_at_papaya) == 1
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
    ppy_home, client_home, ready, open_at_papaya
) -> None:
    """Matrix row 2: a crash under the turn; taken back up and answered, never left."""
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


@pytest.mark.parametrize("phases", [(), (serve.PHASE_DISPATCHED,)], ids=["answer", "work"])
def test_a_lost_lease_or_a_persons_release_is_never_taken_back(ppy_home, phases) -> None:
    """Review 1: `released` with no shutdown mark is Papaya's, or the person's, to decide."""
    found = instructions.Classification(instructions.ANSWER, reason="answered")
    _seed(
        "What are you working on?",
        found,
        phases=(serve.PHASE_PICKED_UP, *phases),
        released=LOST_LEASE,
    )
    assert _unfinished() == []
    loop, routes = OfferLoop(sweep.OFFER_PENDING), Routes()
    assert asyncio.run(_walker(loop, routes)._reclaim_instructions()) == []
    assert loop.offered == [] and routes.calls == []


def test_a_reclaimed_request_declined_for_setup_is_answered_once_and_never_offered_again(
    ppy_home, client_home, ready, registered_repo, open_at_papaya
) -> None:
    """Review 1: a declined pickup of an existing ticket is recorded, said and reported."""
    found = instructions.Classification(instructions.WORK, repo="runtime", reason="work in runtime")
    task_id, _run, _inst = _seed(
        "fix the flaky test in runtime",
        found,
        phases=(serve.PHASE_PICKED_UP, serve.PHASE_BRIEFING),
        released=SHUTDOWN,
        said=(serve.SAID_ON_IT,),
    )
    assert _unfinished() == [task_id]
    routes = Routes()
    harness = tis.InstructionHarness(FakeEvents([]))
    the_runner = tis.runner(FakeTurns(), routes, check_readiness=tis._setup_blocked)
    _serve_empty(harness, client_home, the_runner)
    reason = "this machine needs setup: GitHub is signed out"
    assert harness.results[0]["exit_code"] == 75
    assert _phase(task_id) == serve.PHASE_DECLINED
    (said,) = tis.contents(routes)
    assert said == (
        f'I can\'t take "fix the flaky test in runtime" on this machine: {reason}. '
        "Its owner has been told what to do."
    )
    assert [r["status"] for r in routes.results()] == ["failed"]
    # Persisted, not remembered: a restart neither offers it again nor says it again,
    # and a new process meeting it again declines it without a word.
    assert _unfinished() == []
    again = tis.runner(FakeTurns(), routes, check_readiness=tis._setup_blocked)
    event = papaya_events.PapayaEvent(
        id="offer-2",
        kind=papaya_events.MACHINE_INSTRUCTION,
        subject=tis.SUBJECT,
        payload=tis.instruction_event("fix the flaky test in runtime")["payload"],
    )
    job = SimpleNamespace(env=dict(tis.JOB_ENV), job_id="job-2", subject=tis.SUBJECT)
    assert isinstance(again._take_instruction(job, event), serve.Declined)
    assert len(routes.replies()) == 1 and len(routes.results()) == 1
    assert _phase(task_id) == serve.PHASE_DECLINED


class OfferLoop:
    """The client loop a reclaim offers to, answering as told; a refusal names its holder."""

    def __init__(
        self,
        answer: str,
        holder: dict[str, Any] | None = None,
        *,
        raises: bool = False,
        starts_running: bool = False,
    ) -> None:
        self.answer = answer
        self.holder = holder
        self.raises = raises
        #: The race: the stream delivered the same request while the offer was made.
        self.starts_running = starts_running
        self.offered: list[dict[str, Any]] = []
        self.running_subjects: set[str] = set()
        self._events = SimpleNamespace(reserve=self._reserve)

    async def _reserve(self, subject: str, *_args: Any, **_kwargs: Any) -> Any:
        from papaya_agent_client.api_client import SubjectHeld

        raise SubjectHeld(subject, self.holder, None)

    async def offer(self, envelope: dict[str, Any]) -> str:
        from papaya_agent_client.api_client import SubjectHeld

        self.offered.append(envelope)
        if self.raises:
            raise RuntimeError("the loop is not running")
        if self.starts_running:
            self.running_subjects.add(envelope["subject"])
        if self.holder is not None:
            # Papaya's 409, as the client meets it: the offer answers `done`.
            try:
                await self._events.reserve(envelope["subject"], "session-1")
            except SubjectHeld:
                return "done"
        return self.answer


def _walker(loop: OfferLoop, routes: Routes, *, status: Any = "picked_up") -> rounds.Rounds:
    async def published(_why: str) -> None:
        return None

    def read(_reply: Any, **_kwargs: Any) -> str:
        if isinstance(status, Exception):
            raise status
        return status

    return rounds.Rounds(
        SimpleNamespace(loop=loop, agent_config={"agent_id": "agent-1", "workspace_id": tis.WS}),
        SimpleNamespace(held={}),
        papaya_env=lambda: dict(tis.JOB_ENV),
        status_publisher=SimpleNamespace(publish=published),
        instruction_post=functools.partial(papaya_events.post_instruction_reply, opener=routes),
        instruction_report=functools.partial(
            papaya_events.report_instruction_result, opener=routes
        ),
        read_instruction=read,
    )


def _shut_down(text: str = "What are you working on?") -> int:
    found = instructions.Classification(instructions.ANSWER, reason="answered")
    task_id, _run, _inst = _seed(text, found, phases=(serve.PHASE_PICKED_UP,), released=SHUTDOWN)
    return task_id


def test_the_reclaim_offers_the_instruction_as_it_came(ppy_home) -> None:
    task_id = _shut_down()
    loop, routes = OfferLoop(sweep.OFFER_PENDING), Routes()
    parts = asyncio.run(_walker(loop, routes)._reclaim_instructions())
    (envelope,) = loop.offered
    assert envelope["kind"] == papaya_events.MACHINE_INSTRUCTION
    assert envelope["subject"] == tis.SUBJECT
    assert envelope["agent_id"] == "agent-1" and envelope["workspace_id"] == tis.WS
    assert papaya_events.instruction_from(envelope["payload"]).short_id == "MI-42"
    assert parts == [f'took request "What are you working on?" back up (ticket task {task_id})']
    assert routes.calls == []


@pytest.mark.parametrize(
    "loop",
    [
        OfferLoop("done"),
        OfferLoop(sweep.OFFER_BLOCKED),
        OfferLoop("done", raises=True),
        OfferLoop("done", starts_running=True),
    ],
    ids=["bare-done", "busy", "offer-raised", "already-running"],
)
def test_an_offer_nothing_refused_changes_nothing_and_is_tried_again(ppy_home, loop) -> None:
    """Review 1: only a refusal naming a holder closes a request; the rest wait a round."""
    task_id = _shut_down()
    routes = Routes()
    walker = _walker(loop, routes)
    assert asyncio.run(walker._reclaim_instructions()) == []
    assert routes.calls == []
    assert _phase(task_id) == serve.PHASE_RELEASED and _unfinished() == [task_id]
    # Next round: offered again, unless it is running here now.
    asyncio.run(walker._reclaim_instructions())
    assert len(loop.offered) == (1 if loop.starts_running else 2)


def test_a_refused_reclaim_tells_the_person_once_and_closes_the_ticket(ppy_home) -> None:
    """Matrix row 3: Papaya refuses the re-reserve; closed here, said once, reported."""
    found = instructions.Classification(instructions.WORK, repo=None, reason="work")
    task_id, _run, _inst = _seed(
        "fix the flaky test", found, phases=(serve.PHASE_PICKED_UP,), released=SHUTDOWN
    )
    _todo(task_id, "MI-42 blocked: the gate is red. MI-7 write guard did not block it.")
    loop = OfferLoop("done", {"connection_id": "conn-2", "session_id": "s-2"})
    routes = Routes()
    walker = _walker(loop, routes)
    (part,) = asyncio.run(walker._reclaim_instructions())
    assert part.startswith('could not take request "fix the flaky test" back up (held by ')
    assert part.endswith(f"told the person and closed ticket task {task_id}")
    (said,) = tis.contents(routes)
    assert said == (
        'I couldn\'t finish "fix the flaky test": this machine restarted while working on '
        "it and could not take it back up. It was waiting on: your request blocked: the "
        "gate is red. Send it again if you still want it done."
    )
    (reply,) = routes.replies()
    assert reply["parent_id"] == "root-1"
    (result,) = routes.results()
    assert result["status"] == "failed"
    assert _phase(task_id) == serve.PHASE_DONE
    assert _open_todos() == []
    # Nothing is said twice: the next round finds nothing to take back up.
    assert asyncio.run(walker._reclaim_instructions()) == []
    assert len(routes.replies()) == 1 and len(loop.offered) == 1


@pytest.mark.parametrize("status", ["done", "failed", "cancelled", "not_picked_up"])
def test_a_request_papaya_has_closed_is_closed_here_without_a_word(ppy_home, status) -> None:
    """Review 1: read Papaya's status first; closed there is closed here, silently."""
    task_id = _shut_down()
    _todo(task_id, "Which fixture should the test use?")
    loop, routes = OfferLoop(sweep.OFFER_PENDING), Routes()
    parts = asyncio.run(_walker(loop, routes, status=status)._reclaim_instructions())
    assert parts == [
        f'closed ticket task {task_id} for request "What are you working on?": Papaya has '
        f"it {status}"
    ]
    assert loop.offered == [] and routes.calls == []
    assert _phase(task_id) == serve.PHASE_DONE and _open_todos() == []
    assert _unfinished() == []


def test_a_request_papaya_cannot_be_read_on_is_left_for_the_next_round(ppy_home) -> None:
    task_id = _shut_down()
    loop, routes = OfferLoop(sweep.OFFER_PENDING), Routes()
    error = papaya_events.PapayaEventError("Papaya is unreachable")
    assert asyncio.run(_walker(loop, routes, status=error)._reclaim_instructions()) == []
    assert loop.offered == [] and routes.calls == []
    assert _unfinished() == [task_id]


def test_the_status_read_asks_papaya_beside_the_result_route() -> None:
    reply = tis.instruction_event("x")["payload"]["reply"]
    routes = StatusRoutes("picked_up")
    assert (
        papaya_events.read_instruction_status(reply, environ=dict(tis.JOB_ENV), opener=routes)
        == "picked_up"
    )
    ((method, path, _body),) = routes.calls
    assert method == "GET" and path == f"/api/v1/workspaces/{tis.WS}/machine-instructions/MI-42"


class StatusRoutes(Routes):
    """Papaya's instruction read (`GET .../machine-instructions/<ref>`), with a status."""

    def __init__(self, status: str) -> None:
        super().__init__()
        self.status = status

    def __call__(self, request, timeout):
        if request.method == "GET" and request.full_url.endswith("/machine-instructions/MI-42"):
            self.calls.append((request.method, request.full_url.split("papaya.test")[-1], None))
            return test_serve._Body(json.dumps({"status": self.status}).encode())
        return super().__call__(request, timeout)


# ── Goal 2: what waits on the person is said where they asked ────────────────


def _step(origin_post, now: datetime, *, dm=None) -> list[str]:
    def nowhere(*_args: Any) -> bool:
        raise AssertionError("an ask about a live request is said only where it was asked")

    conn = init_db()
    try:
        return outreach.step(
            conn,
            now=now,
            host="mac",
            dm=dm or nowhere,
            ticket=nowhere,
            desktop=lambda *_a: False,
            origin=origin_post,
        )
    finally:
        conn.close()


def _origin_post(routes: Routes):
    post = functools.partial(papaya_events.post_instruction_reply, opener=routes)

    def origin_post(request: int, body: str) -> bool:
        return outreach.post_origin(request, body, environ=dict(tis.JOB_ENV), post=post)

    return origin_post


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
    origin_post = _origin_post(routes)

    lines = _step(origin_post, datetime(2026, 9, 23, 10, 0, tzinfo=UTC))
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
    _step(origin_post, datetime(2026, 9, 23, 10, 5, tzinfo=UTC))
    assert len(routes.replies()) == 1
    # Changed, it is said again, once, without waiting out the DM's interval.
    conn = init_db()
    try:
        store.update_todo(conn, todo, text="MI-42 blocked: which fixture, now that A is gone?")
    finally:
        conn.close()
    _step(origin_post, datetime(2026, 9, 23, 10, 10, tzinfo=UTC))
    _step(origin_post, datetime(2026, 9, 23, 10, 15, tzinfo=UTC))
    assert len(routes.replies()) == 2
    conn = init_db()
    try:
        rows = conn.execute("SELECT said_via FROM outreach").fetchall()
    finally:
        conn.close()
    assert all(json.loads(row[0] or "[]") == [outreach.VIA_ORIGIN] for row in rows)


def test_a_capability_ask_at_the_origin_is_in_plain_words(ppy_home, monkeypatch) -> None:
    """Review 1: no worker task ids, no command it ran, no `ppy` command to type."""
    found = instructions.Classification(instructions.WORK, repo=None, reason="work")
    _task, run_id, _inst = _seed("fix the flaky test", found, phases=(serve.PHASE_DISPATCHED,))
    conn = init_db()
    try:
        worker = store.add_task(conn, run_id=run_id, title="worker")
        store.update_task_fields(conn, worker, status="in_progress")
    finally:
        conn.close()
    monkeypatch.setattr(capability_requests, "_tell_worker", lambda _found: None)
    request = capability_requests.request(
        worker, "psql", why="read the fixture database", command="psql -h db.internal fixtures"
    )
    capability_requests.escalate(request.id, why="it needs the database's password")
    routes = Routes()
    _step(_origin_post(routes), datetime(2026, 9, 23, 10, 0, tzinfo=UTC))
    (body,) = tis.contents(routes)
    assert body == (
        "This needs you before I can go on:\n"
        "- The work needs your permission to run `psql`, to read the fixture database.\n"
        f'  To allow it, send me a new message saying "approve capability {request.id}". '
        f'To refuse, say "deny capability {request.id} because …".'
    )
    assert "worker task" not in body and "db.internal" not in body and "ppy " not in body


def test_an_ask_after_the_request_was_answered_is_not_said_at_its_origin(ppy_home) -> None:
    """Review 1: the conversation is over; a later ask goes to the owner as usual."""
    found = instructions.Classification(instructions.WORK, repo=None, reason="work")
    task_id, run_id, instruction = _seed(
        "fix the flaky test", found, phases=(serve.PHASE_PICKED_UP,)
    )
    conn = init_db()
    try:
        worker = store.add_task(conn, run_id=run_id, title="worker")
        instructions.answer(
            conn,
            task_id,
            instruction,
            "done",
            "Done.",
            environ={},
            post=lambda *a, **k: "m-1",
            report=lambda *a, **k: True,
        )
        serve.record_phase(conn, task_id, serve.PHASE_REPORTED, "done")
        assert outreach.instruction_ticket_of(conn, worker) is None
    finally:
        conn.close()
    _todo(worker, "The pull request needs a person: CI is red twice.")
    routes = Routes()
    dms: list[str] = []
    _step(
        _origin_post(routes),
        datetime(2026, 9, 23, 10, 0, tzinfo=UTC),
        dm=lambda text: dms.append(text) or True,
    )
    assert routes.calls == []
    assert len(dms) == 1 and "CI is red twice" in dms[0]


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


def _serve_spike(client_home, act, note: str) -> tuple[Routes, list[str]]:
    runs: list[int] = []
    titles: list[str] = []

    def dispatch(repo: str, brief: str, run_id: int, title: str) -> None:
        runs.append(run_id)
        titles.append(title)
        test_serve.dispatch_worker(run_id, repo=repo)

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

        progress.record(worker, phase="done", note=note)
        test_serve.worker_event(worker, "worker_done", status="worker_done", summary="done")
        await _until(lambda: harness.results, what="the request to be released", timeout=10)
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0
    return routes, titles


CLOSEOUT = (
    "Pushed ce6cf54 to ppy/task-6-csv, ready for PR. Evidence: .ppy-evidence/pytest.txt. "
    "MI-3 write guard did not block this worktree."
)


def _delivers(summary: str | None):
    def act(turn: test_serve.Turn) -> str | None:
        (worker,) = test_serve.workers_in(turn.run_id)
        test_serve.worker_event(worker, "reviewed", verdict="approved")
        test_serve.worker_event(worker, "delivered", status="delivered", pr_url=PR)
        return summary

    return act


@pytest.mark.parametrize(
    "summary",
    [None, f"OUTCOME: done\n{CLOSEOUT}"],
    ids=["no-outcome", "outcome-is-the-closeout"],
)
def test_a_work_instruction_says_the_pr_once_and_never_the_worker_closeout(
    ppy_home, client_home, ready, registered_repo, summary
) -> None:
    """Matrix row 5: no work-item read, no fallback line, no worker closeout.

    A review turn that wrote no summary, or pasted the closeout (a SHA, a branch, an
    evidence path), gets the runtime's own sentence instead.
    """
    routes, titles = _serve_spike(client_home, _delivers(summary), CLOSEOUT)
    said = tis.contents(routes)
    assert said[-1] == f'The work on "{SPIKE}" is done and reviewed.\n\nPull request open: {PR}'
    assert sum(text.count("Pull request open") for text in said) == 1
    assert not any("ce6cf54" in text or "write guard" in text for text in said)
    # The report step read no work item: an instruction has none.
    assert all(path.endswith("/follow-ups") for path in routes.reads())
    # Goal 4: the worker, and so its pull request, is titled as the person titled it.
    assert titles == [SPIKE]
    assert routes.results()[0]["status"] == "done"


def test_a_review_that_says_it_failed_is_reported_failed(
    ppy_home, client_home, ready, registered_repo
) -> None:
    routes, _titles = _serve_spike(
        client_home,
        _delivers("OUTCOME: failed\nThe export works, but the tests are red on CI."),
        "Done.",
    )
    assert tis.contents(routes)[-1] == (
        f"The export works, but the tests are red on CI.\n\nPull request open: {PR}"
    )
    assert routes.results()[0]["status"] == "failed"


def test_a_summary_is_cut_to_its_bound() -> None:
    long = instructions.Outcome("done", "word " * 400)
    shown = instructions.person_summary(long)
    assert shown is not None and len(shown.text) == instructions.SUMMARY_MAX
    assert shown.text.endswith("…")
    for leak in ("see /Users/shane/x/y.txt", "at a1b2c3d", "on ppy/task-9-x", ".ppy-evidence/"):
        assert instructions.person_summary(instructions.Outcome("done", leak)) is None
    # A pull request link is not a path.
    assert instructions.person_summary(instructions.Outcome("done", f"See {PR}.")) is not None


def test_a_worker_that_found_rather_than_built_never_posts_its_closeout(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """Review 1: the no-commit path's answer is a turn's, or the runtime's sentence."""
    runs: list[int] = []

    def dispatch(repo: str, brief: str, run_id: int, title: str) -> None:
        runs.append(run_id)
        test_serve.dispatch_worker(run_id, repo=repo)

    # The turn pastes the report: refused, and the runtime's own sentence goes instead.
    turns, routes = FakeTurns(tis.summarising()), Routes()
    event = tis.instruction_event("Investigate why the export is slow in runtime")
    harness = tis.InstructionHarness(FakeEvents([event]))
    the_runner = tis.runner(
        turns, routes, instruction_dispatch=dispatch, branch_ahead=lambda _task: False
    )
    assert tis.serve_work(harness, client_home, the_runner, runs, CLOSEOUT) == 0
    assert turns.names() == [prompts.INSTRUCTION]
    assert tis.contents(routes)[-1] == (
        'I looked into "Investigate why the export is slow in runtime" and made no changes, '
        "but couldn't put what I found into a short answer this time. Ask me about it again."
    )


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
        body = pr_body.for_request(
            conn, worker, 'Done for MI-4. MI-9: its guard held.\n\nTracked as "MI-4: x".'
        )
        assert body == 'Done for this request.\n\nTracked as "x".'
        # Elsewhere `MI-12` may be another tracker's key: left as it is.
        assert delivery._pr_title(conn, other, "MI-12: the tracker's own key") == (
            "MI-12: the tracker's own key"
        )
        # A title only about another request still names something.
        assert delivery._pr_title(conn, worker, "MI-9: something else") == (
            "Work on a request sent to this machine"
        )
    finally:
        conn.close()


def test_the_published_status_report_names_requests_by_title(ppy_home) -> None:
    """Matrix row 6, the status half: in flight, waiting, blocked and finished rows."""
    found = instructions.Classification(instructions.ANSWER, reason="answered")
    task_id, _run, _inst = _seed("What are you working on?", found, phases=(serve.PHASE_PICKED_UP,))
    _todo(task_id, "MI-42 blocked: the gate is red. MI-3 is fine.")
    _todo(task_id, "MI-42 is waiting on the CI runner", blocked_on="ci")
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
    # The wire's `short_id` is an identifier: the ticket's task, not the title or `MI-42`.
    assert request["about"] == {
        "kind": "machine_instruction",
        "short_id": f"task-{task_id}",
        "url": None,
    }
    texts = {row["text"] for row in body["needs_you"] + body["blocked"]}
    assert "this request blocked: the gate is red." in texts
    assert "this request is waiting on the CI runner" in texts


# ── Goal 5: `for_person` drops, never garbles ────────────────────────────────


def _instruction() -> papaya_events.Instruction:
    return papaya_events.instruction_from(tis.instruction_event("fix it")["payload"])


def test_for_person_drops_a_sentence_about_another_request_and_keeps_the_rest() -> None:
    text = (
        "MI-42: Fixed the export. Tests pass. MI-3 write guard did not block this worktree.\n"
        "\n"
        "MI-7 is next.\n"
        "\n"
        "Done with MI-42."
    )
    assert instructions.for_person(text, _instruction()) == (
        "Fixed the export. Tests pass.\n\nDone with your request."
    )
    assert "another request" not in instructions.for_person(text, _instruction())
    # A path with a dot in it is not a sentence end.
    assert (
        instructions.for_person(
            "Evidence: .ppy-evidence/pytest.txt. MI-3 was fine.", _instruction()
        )
        == "Evidence: .ppy-evidence/pytest.txt."
    )


def test_another_requests_label_marks_its_sentence_as_about_another_request() -> None:
    """Review 1: only this request's own label is dropped as a label."""
    assert (
        instructions.for_person(
            "MI-42: Done. MI-7: Rename the flag finished earlier.", _instruction()
        )
        == "Done."
    )


def test_a_reply_only_about_other_requests_is_never_posted_empty() -> None:
    assert instructions.for_person("MI-7 is waiting on its gate.", _instruction()) == (
        instructions.ONLY_OTHER_REQUESTS
    )
    # A progress line only about another request is simply not said.
    assert (
        instructions.for_person("MI-7 is waiting on its gate.", _instruction(), allow_empty=True)
        == ""
    )
