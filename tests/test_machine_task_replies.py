"""Work routed to this machine hears back where it was asked, in any tool (backend #1077).

Shane, 2026-09-25: work started with "@agent start working on this" in a Papaya thread
(PAP-319) was done by his machine, and every update landed only as work-item comments.
Papaya now carries a `machine_task` block on `work_item.assigned` and
`machine.instruction` — the task's id, where it was asked (`origin`) and one reply
route that takes four milestones — and `ppy serve` says those milestones there, once
each, while still commenting on the work item as before.

Driven through the client's own loop with the same fakes as `test_serve.py`:
`FakePapaya` now answers the machine-task route the way Papaya does (a resend of a
milestone answers `replayed`).
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
from typing import Any

import pytest

import test_instruction_serve
import test_serve
import test_supervision
from papaya_agent_runtime import (
    board,
    instructions,
    machine_tasks,
    outreach,
    papaya_events,
    prompts,
    serve,
    supervision,
)
from papaya_agent_runtime.state import store
from papaya_agent_runtime.state.db import init_db

globals().update(
    {name: getattr(test_serve, name) for name in ("client_home", "ready", "registered_repo")}
)
FakeEvents = test_serve.FakeEvents
FakePapaya = test_serve.FakePapaya
FakeTurns = test_serve.FakeTurns
Harness = test_serve.Harness
Turn = test_serve.Turn
_until = test_serve._until

WS = "ws-1"
TASK = "7a1d9e0c-5b2f-4f3e-8c11-0d6b2e9f4a10"
THREAD = {
    "kind": "papaya_thread",
    "ref": {
        "channel_id": "11111111-1111-4111-8111-111111111111",
        "thread_root_id": "22222222-2222-4222-8222-222222222222",
        "message_id": "33333333-3333-4333-8333-333333333333",
    },
}
LINEAR = {
    "kind": "provider_object",
    "ref": {"provider": "linear", "object_type": "issue", "object_id": "PAP-319"},
    "url": "https://linear.app/papaya/issue/PAP-319",
}
PICKED_UP_LINE = "Picked up; choosing the repository and writing the brief."
#: What a call through the job's connection is made with, outside a hold.
ENV = {
    "PAPAYA_API_URL": "http://papaya.test",
    "PAPAYA_AGENT_TOKEN": "pagc_test_token",
    "PAPAYA_WORKSPACE_ID": WS,
}


def block(task: str = TASK, origin: dict[str, Any] | None = THREAD) -> dict[str, Any]:
    """The `machine_task` block an event carries (backend `machine_task_block`)."""
    return {
        "id": task,
        "origin": origin,
        "reply": {
            "method": "POST",
            "path": f"/api/v1/workspaces/{WS}/machine-tasks/{task}/reply",
            "tool": "reply_to_machine_task",
            "milestones": list(papaya_events.MILESTONES),
            "delivers_to": origin["kind"] if origin else "work_item_comment",
        },
    }


def routed(
    event_id: int,
    item: str,
    title: str,
    *,
    task: str = TASK,
    origin: dict[str, Any] | None = THREAD,
    key: str = "PAP-319",
) -> dict[str, Any]:
    """A `work_item.assigned` event for work Papaya routed to this machine."""
    event = test_serve._assigned(event_id, item, title)
    event["payload"]["work_item"]["display_id"] = key
    event["payload"]["machine_task"] = block(task, origin)
    return event


def plain(event_id: int, item: str, title: str) -> dict[str, Any]:
    event = test_serve._assigned(event_id, item, title)
    event["payload"]["work_item"]["display_id"] = f"PAP-{event_id}"
    return event


def _nothing_to_build(turn: Turn) -> str | None:
    if turn.name == prompts.BRIEF:
        return "Checked the item.\n\nNOTHING TO BUILD: PR #729 merged and QA passed it"
    return None


def _serve_until(harness: Harness, client_home, runner: serve.TicketRunner, ends: int) -> int:
    async def scenario() -> int:
        task = test_serve._serve_ticket(harness, client_home, runner)
        await _until(lambda: len(harness.results) == ends, what="the tickets to end")
        harness.loop.request_stop()
        return await task

    return asyncio.run(scenario())


# ── work items ──────────────────────────────────────────────────────────────


def test_a_routed_assignment_hears_its_pickup_and_its_pull_request_where_it_was_asked(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """Goal 1: picked up and the PR reach the origin; the item's comments are as before."""

    def act(turn: Turn) -> None:
        if turn.name == prompts.BRIEF:
            test_serve.dispatch_worker(turn.run_id)
        elif turn.name == prompts.REVIEW:
            test_serve._deliver(turn)

    papaya_api = FakePapaya()
    harness = Harness(FakeEvents([routed(101, "item-9", "Fix the thing")]))

    async def scenario() -> int:
        task = test_serve._serve_ticket(
            harness, client_home, test_serve._runner(FakeTurns(act), papaya_api)
        )
        await _until(lambda: serve.PHASE_DISPATCHED in test_serve.history(), what="the dispatch")
        (worker,) = test_serve.workers_in(int(test_serve.ticket_task()["run_id"]))
        test_serve.worker_event(worker, "worker_done", status="worker_done", summary="done")
        await _until(lambda: harness.results, what="the ticket to be released")
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0

    assert papaya_api.milestones() == [
        (TASK, "picked_up", f"PAP-319: {PICKED_UP_LINE}"),
        (TASK, "delivered", "PAP-319: Pull request open: https://github.com/acme/runtime/pull/7"),
    ]
    # Still written to the work item as today: the pickup line and the turn's report.
    comments = [body for item, body in papaya_api.comments() if item == "item-9"]
    assert comments[0] == PICKED_UP_LINE
    assert test_serve.REPORT in comments
    # The block is kept on the ticket, so what offers it again later can still answer.
    conn = init_db()
    try:
        kept = machine_tasks.block_of(conn, int(test_serve.ticket_task()["id"]))
    finally:
        conn.close()
    assert kept is not None and kept["id"] == TASK and kept["origin"] == THREAD


def test_nothing_to_build_says_done_and_a_hand_back_says_blocked_there(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """Goal 1: done and the blocker that needs the person, each where its work was asked."""
    finished, stuck = "item-10", "item-11"
    other = "0b5f2c7d-9e41-4a3a-b6d2-5c8e1f7a9d22"

    def act(turn: Turn) -> str | None:
        return _nothing_to_build(turn) if turn.item() == finished else None

    papaya_api = FakePapaya()
    harness = Harness(
        FakeEvents(
            [
                routed(102, finished, "Newest first sort", key="PAP-320"),
                routed(103, stuck, "Something nobody can place", task=other, origin=LINEAR),
            ]
        )
    )

    assert (
        _serve_until(harness, client_home, test_serve._runner(FakeTurns(act), papaya_api), 2) == 0
    )

    said = papaya_api.milestones()
    assert [(t, m, x) for t, m, x in said if t == TASK] == [
        (TASK, "picked_up", f"PAP-320: {PICKED_UP_LINE}"),
        (TASK, "done", "PAP-320: Nothing to build: PR #729 merged and QA passed it"),
    ]
    assert [(t, m, x) for t, m, x in said if t == other] == [
        (other, "picked_up", f"PAP-319: {PICKED_UP_LINE}"),
        (
            other,
            "blocked",
            "PAP-319: Handed back: the manager turn ended 2 times without dispatching a worker",
        ),
    ]
    # The item still gets its hand-back comment, exactly as before.
    assert [b for i, b in papaya_api.comments() if i == stuck][-1].startswith("handed back:")


def test_waiting_on_a_person_is_the_blocker_said_where_it_was_asked(
    ppy_home, client_home, ready, registered_repo
) -> None:
    def act(turn: Turn) -> None:
        if turn.name != prompts.BRIEF:
            return
        if len(turns.calls) == 1:
            board.add(
                "Is this the desktop app or the web app?",
                task_id=int(test_serve.ticket_task()["id"]),
                blocked_on="user",
            )
        else:
            test_serve.dispatch_worker(turn.run_id)

    papaya_api = FakePapaya()
    turns = FakeTurns(act)
    harness = Harness(FakeEvents([routed(101, "item-9", "Fix the thing")]))

    async def scenario() -> int:
        task = test_serve._serve_ticket(harness, client_home, test_serve._runner(turns, papaya_api))
        await _until(
            lambda: ("item-9", "blocked") in papaya_api.statuses(), what="the wait on a person"
        )
        papaya_api.updated_at = "2026-09-16T11:00:00Z"  # someone answered on the item
        await _until(lambda: serve.PHASE_DISPATCHED in test_serve.history(), what="the dispatch")
        harness.jobs[0].stop.set()
        await _until(lambda: harness.results, what="the hold to end")
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0
    assert [(m, x) for _t, m, x in papaya_api.milestones()] == [
        ("picked_up", f"PAP-319: {PICKED_UP_LINE}"),
        (
            "blocked",
            "PAP-319: Blocked: Waiting on a person: Is this the desktop app or the web app?",
        ),
    ]


def test_an_assignment_with_no_block_or_no_origin_behaves_exactly_as_today(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """Goal 3: nothing goes to the reply route, and the item's comments are unchanged.

    A block with a null origin is a plain assignment routed to a machine: Papaya would
    keep such a reply as another comment on the item, beside the runtime's own.
    """
    papaya_api = FakePapaya()
    harness = Harness(
        FakeEvents(
            [
                plain(102, "item-10", "Newest first sort"),
                routed(103, "item-11", "Oldest first sort", origin=None),
            ]
        )
    )

    runner = test_serve._runner(FakeTurns(_nothing_to_build), papaya_api)
    assert _serve_until(harness, client_home, runner, 2) == 0

    assert papaya_api.milestones() == []
    assert not [call for call in papaya_api.calls if "/machine-tasks/" in call[1]]
    for item in ("item-10", "item-11"):
        assert [b for i, b in papaya_api.comments() if i == item] == [PICKED_UP_LINE]
    conn = init_db()
    try:
        assert machine_tasks.block_of(conn, int(test_serve.ticket_task(103)["id"])) is None
    finally:
        conn.close()


# ── once each, across resumes and restarts ──────────────────────────────────


def _ticket_with(conn, payload: dict[str, Any], item: str = "item-9") -> int:
    run_id = store.create_run(conn, "ticket")
    task_id = store.add_task(conn, run_id=run_id, title="ticket")
    store.set_task_env(
        conn,
        task_id,
        papaya_events.PAPAYA_EVENT_METADATA,
        json.dumps({"work_item_id": item}),
    )
    store.set_task_env(conn, task_id, papaya_events.WORK_ITEM_KEY, "PAP-319")
    machine_tasks.remember(conn, task_id, payload, item)
    return task_id


def test_a_milestone_is_sent_once_whoever_asks_and_however_often(ppy_home) -> None:
    """Goal 2: the ledger keeps the once — a resumed hold, a restart, a second ticket."""
    papaya_api = FakePapaya()
    conn = init_db()
    try:
        first = _ticket_with(conn, {"machine_task": block()})
        # An offer (after a hand-back, say) makes a new ticket and carries no block.
        second = _ticket_with(conn, {"work_item": {"id": "item-9"}})
    finally:
        conn.close()

    def send(task_id: int, milestone: str) -> bool:
        return machine_tasks.send(
            task_id, milestone, PICKED_UP_LINE, environ=ENV, opener=papaya_api
        )

    assert send(first, machine_tasks.PICKED_UP) is True
    # A new connection each call, as a restarted process would open.
    assert send(first, machine_tasks.PICKED_UP) is False
    assert send(second, machine_tasks.PICKED_UP) is False
    assert send(second, machine_tasks.BLOCKED) is True
    assert [(t, m) for t, m, _x in papaya_api.milestones()] == [
        (TASK, "picked_up"),
        (TASK, "blocked"),
    ]


def test_a_papaya_that_cannot_be_reached_is_tried_again_and_a_refusal_is_settled(
    ppy_home, caplog
) -> None:
    calls: list[str] = []

    def unreachable(request, timeout):
        calls.append(request.full_url)
        raise urllib.error.URLError("connection refused")

    def refusing(request, timeout):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 404, "not found", {}, None)

    conn = init_db()
    try:
        task_id = _ticket_with(conn, {"machine_task": block()})
    finally:
        conn.close()

    for _ in range(2):
        assert not machine_tasks.send(task_id, "done", "Merged.", environ=ENV, opener=unreachable)
    assert len(calls) == 2  # not recorded, so tried again
    for _ in range(2):
        assert not machine_tasks.send(task_id, "done", "Merged.", environ=ENV, opener=refusing)
    assert len(calls) == 3  # a 404 will not change on a retry: settled
    assert "Could not say done" in caplog.text and "refused the done reply" in caplog.text


def test_the_reply_route_must_be_in_this_workspace(ppy_home) -> None:
    papaya_api = FakePapaya()
    elsewhere = block()
    elsewhere["reply"]["path"] = f"/api/v1/workspaces/ws-other/machine-tasks/{TASK}/reply"
    conn = init_db()
    try:
        task_id = _ticket_with(conn, {"machine_task": elsewhere})
    finally:
        conn.close()
    assert not machine_tasks.send(task_id, "done", "Merged.", environ=ENV, opener=papaya_api)
    assert papaya_api.calls == []


@pytest.mark.parametrize(
    "block_value",
    [
        None,
        "MI-4",
        {"id": TASK, "origin": None, "reply": {"path": "/x"}},
        {"id": TASK, "origin": {"kind": ""}, "reply": {"path": "/x"}},
        {"id": "", "origin": THREAD, "reply": {"path": "/x"}},
        {"id": TASK, "origin": THREAD, "reply": {}},
    ],
)
def test_only_a_block_with_an_origin_and_a_route_is_one_to_answer(block_value) -> None:
    assert machine_tasks.block_from({"machine_task": block_value}) is None
    assert machine_tasks.block_from({}) is None
    assert machine_tasks.block_from({"machine_task": block()}) == block()


# ── done, when the pull request merges: both modes ──────────────────────────


def test_a_merge_says_done_where_it_was_asked_once_in_either_mode(ppy_home, monkeypatch) -> None:
    """Serve's rounds pass their own connection; a session's heartbeat its agent's."""
    papaya_api = FakePapaya()
    conn = init_db()
    try:
        worker = test_supervision._delivered(conn, ticket_phase="handed_over")
        ticket = int(
            conn.execute(
                "SELECT id FROM tasks WHERE run_id = (SELECT run_id FROM tasks WHERE id = ?) "
                "AND phase IS NOT NULL",
                (worker,),
            ).fetchone()[0]
        )
        store.set_task_env(conn, ticket, papaya_events.WORK_ITEM_KEY, "PAP-319")
        machine_tasks.remember(conn, ticket, {"machine_task": block()})
    finally:
        conn.close()
    posted: list[str] = []

    def post(_ticket, body: str, _status: str | None) -> None:
        posted.append(body)

    # The session's default: its own connection (`papaya.agent_env`).
    from papaya_agent_runtime import papaya

    monkeypatch.setattr(papaya, "agent_env", lambda: dict(ENV))
    real_send = machine_tasks.send
    monkeypatch.setattr(
        machine_tasks,
        "send",
        lambda *a, **k: real_send(*a, **{**k, "opener": papaya_api}),
    )
    entry = test_supervision._merged_entry(worker)
    supervision.merged_step([entry], post=post, reply=machine_tasks.send_as_agent)
    supervision.merged_step([entry], post=post)

    (body,) = posted
    assert papaya_api.milestones() == [(TASK, "done", f"PAP-319: {body}")]


def test_a_reply_that_fails_never_makes_the_merge_comment_go_out_twice(ppy_home) -> None:
    conn = init_db()
    try:
        worker = test_supervision._delivered(conn, ticket_phase="handed_over")
    finally:
        conn.close()
    posted: list[str] = []

    def broken(*_args: Any) -> bool:
        raise RuntimeError("no connection")

    entry = test_supervision._merged_entry(worker)
    for _ in range(2):
        supervision.merged_step(
            [entry], post=lambda _t, body, _s: posted.append(body), reply=broken
        )
    assert len(posted) == 1


# ── instructions asked from a connected tool ────────────────────────────────


def provider_instruction(text: str) -> dict[str, Any]:
    """A `machine.instruction` asked from a Linear issue: its reply is the machine-task route."""
    event = test_instruction_serve.instruction_event(text, intent="work")
    payload = event["payload"]
    payload["origin"] = {"kind": "provider"}
    payload["reply"] = {
        "kind": "machine_task_reply",
        "tool": "reply_to_machine_task",
        "method": "POST",
        "path": f"/api/v1/workspaces/{WS}/machine-tasks/MI-42/reply",
        "parent_id": None,
        "conversation_id": None,
        "result_path": f"/api/v1/workspaces/{WS}/machine-instructions/MI-42/result",
    }
    payload["machine_task"] = block("MI-42", LINEAR)
    return event


def test_a_work_instruction_from_a_connected_tool_is_answered_there_with_milestones(
    ppy_home, client_home, ready, registered_repo
) -> None:
    """Before this, `reply_paths` refused the kind: nothing was said and nothing reported."""
    runs: list[int] = []

    def dispatch(repo: str, brief: str, run_id: int, title: str) -> None:
        assert "(a connected tool)" in brief
        runs.append(run_id)
        test_serve.dispatch_worker(run_id, repo=repo)

    def act(turn: Turn) -> str:
        assert turn.name == prompts.REVIEW
        (worker,) = test_serve.workers_in(turn.run_id)
        test_serve.worker_event(worker, "reviewed", verdict="approved")
        test_serve.worker_event(
            worker,
            "delivered",
            status="delivered",
            branch=f"ppy/task-{worker}",
            pr_url="https://github.com/acme/runtime/pull/7",
        )
        return "Approved and delivered.\nOUTCOME: done\nCSV export is in, off by default."

    routes = test_instruction_serve.Routes()
    harness = test_instruction_serve.InstructionHarness(
        FakeEvents([provider_instruction("Implement CSV export in runtime and open the PR")])
    )
    the_runner = test_instruction_serve.runner(
        FakeTurns(act), routes, instruction_dispatch=dispatch, branch_ahead=lambda _task: True
    )

    async def scenario() -> int:
        task = test_serve._serve_ticket(harness, client_home, the_runner)
        await _until(lambda: runs and test_serve.workers_in(runs[0]), what="the dispatch")
        (worker,) = test_serve.workers_in(runs[0])
        test_serve.worker_event(worker, "worker_done", status="worker_done", summary="done")
        await _until(lambda: harness.results, what="the instruction to be released", timeout=10)
        harness.loop.request_stop()
        return await task

    assert asyncio.run(scenario()) == 0

    replies = routes.replies()
    # Only milestones reach the route: "On it" is picked up, the answer (naming the PR) is
    # done. Progress lines that are not a milestone ("Dispatched…") are not sent there.
    assert [r["milestone"] for r in replies] == ["picked_up", "done"]
    assert replies[0]["text"] == "On it — working in runtime."
    assert replies[1]["text"] == (
        "CSV export is in, off by default.\n\n"
        "Pull request open: https://github.com/acme/runtime/pull/7"
    )
    assert all(set(r) == {"text", "milestone"} for r in replies)
    (result,) = routes.results()
    assert result["status"] == "done"


def test_post_instruction_reply_sends_only_milestones_on_the_machine_task_route() -> None:
    routes = test_instruction_serve.Routes()
    reply = provider_instruction("x")["payload"]["reply"]

    progress = papaya_events.post_instruction_reply(
        reply, "Dispatched a worker.", environ=ENV, opener=routes, kind="progress"
    )
    blocked = papaya_events.post_instruction_reply(
        reply, "Which repo?", environ=ENV, opener=routes, kind="progress", milestone="blocked"
    )
    final = papaya_events.post_instruction_reply(
        reply, "Done.", environ=ENV, opener=routes, kind="final"
    )

    assert progress is None
    assert blocked == "" and final == ""  # replied; this route has no message id to report
    assert routes.replies() == [
        {"text": "Which repo?", "milestone": "blocked"},
        {"text": "Done.", "milestone": "done"},
    ]
    assert papaya_events.reply_paths(reply, ENV)[1].endswith("/machine-instructions/MI-42/result")


def test_an_ask_waiting_on_the_person_is_the_blocker_at_a_tools_origin(
    ppy_home, monkeypatch
) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    instruction = papaya_events.instruction_from(provider_instruction("x")["payload"])
    monkeypatch.setattr(instructions, "instruction_of", lambda _conn, _task: instruction)

    def post(_reply, text: str, *, environ: dict[str, str], **kwargs: Any) -> str:
        posted.append((text, kwargs))
        return ""

    assert outreach.post_origin(7, "Which repository?", environ=ENV, post=post)
    assert posted == [("Which repository?", {"kind": "progress", "milestone": "blocked"})]
