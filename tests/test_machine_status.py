"""The machine's status snapshot: built inside the wire's bounds, published once and on change.

The wire is backend task 350's `MachineStatusSnapshot`, reproduced as :func:`validate`
below: every key, type and bound the backend's 422 enforces. The builder is held to it
against a fixture ledger with a bit of everything, and the publisher to its contract:
one PUT a round, one on change, never the same body twice in thirty seconds, a 422
logged once and never retried with the same body, a network error logged and survived.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from conftest import PRIVACY_LEAKS, leaked
from papaya_agent_runtime import (
    capability_requests,
    instructions,
    machine_status,
    papaya_events,
    progress,
)
from papaya_agent_runtime.state import store
from papaya_agent_runtime.state.db import init_db

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
DEGRADED = machine_status.Health("degraded", "Can take work, with gaps (no_repos).")

# ── the wire, as a validator ────────────────────────────────────────────────

_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:\d{2}|Z)$")


def _string(value: Any, lo: int, hi: int, where: str, *, one_line: bool = False) -> None:
    assert isinstance(value, str), f"{where} is not a string"
    assert lo <= len(value) <= hi, f"{where} is {len(value)} long, not {lo}-{hi}"
    if one_line:
        assert "\n" not in value and "\r" not in value, f"{where} is not one line"


def _time(value: Any, where: str) -> None:
    assert isinstance(value, str) and _TIME.match(value), f"{where} is not an aware time"


def _url(value: Any, where: str) -> None:
    if value is None:
        return
    _string(value, 1, 2000, where)
    assert re.match(r"^https?://", value), f"{where} is not http(s)"


def _keys(row: dict[str, Any], allowed: set[str], required: set[str], where: str) -> None:
    assert set(row) <= allowed, f"{where} has unknown keys {set(row) - allowed}"
    assert required <= set(row), f"{where} lacks {required - set(row)}"


def _int(value: Any, where: str) -> None:
    assert type(value) is int and 0 <= value <= 1000, f"{where} is not a strict int 0-1000"


def _need(row: dict[str, Any], where: str) -> None:
    _keys(
        row, {"kind", "ref", "text", "how", "since"}, {"kind", "ref", "text", "how", "since"}, where
    )
    assert row["kind"] in machine_status.NEED_KINDS, where
    _string(row["ref"], 1, 200, f"{where}.ref")
    _string(row["text"], 1, 500, f"{where}.text")
    _string(row["how"], 1, 300, f"{where}.how")
    _time(row["since"], f"{where}.since")


def validate(body: dict[str, Any]) -> None:
    """Every rule the backend's `MachineStatusSnapshot` enforces (`extra="forbid"`)."""
    top = {"as_of", "summary", "in_flight", "needs_you", "blocked", "recently_finished"}
    top |= {"capacity", "health"}
    _keys(body, top, {"as_of", "summary", "capacity", "health"}, "snapshot")
    _time(body["as_of"], "as_of")
    _string(body["summary"], 1, 300, "summary", one_line=True)
    assert len(body.get("in_flight", [])) <= 25
    for i, row in enumerate(body.get("in_flight", [])):
        where = f"in_flight[{i}]"
        _keys(
            row,
            {"ref", "title", "about", "phase", "since"},
            {"ref", "title", "phase", "since"},
            where,
        )
        _string(row["ref"], 1, 200, f"{where}.ref")
        _string(row["title"], 1, 300, f"{where}.title", one_line=True)
        _string(row["phase"], 1, 100, f"{where}.phase")
        _time(row["since"], f"{where}.since")
        about = row.get("about")
        if about is not None:
            _keys(about, {"kind", "short_id", "url"}, {"kind", "short_id"}, f"{where}.about")
            assert about["kind"] in ("work_item", "machine_instruction")
            _string(about["short_id"], 1, 40, f"{where}.about.short_id")
            _url(about.get("url"), f"{where}.about.url")
    for name in ("needs_you", "blocked"):
        assert len(body.get(name, [])) <= 25
        for i, row in enumerate(body.get(name, [])):
            _need(row, f"{name}[{i}]")
    assert len(body.get("recently_finished", [])) <= 10
    for i, row in enumerate(body.get("recently_finished", [])):
        where = f"recently_finished[{i}]"
        _keys(
            row, {"ref", "title", "outcome", "url", "at"}, {"ref", "title", "outcome", "at"}, where
        )
        _string(row["ref"], 1, 200, f"{where}.ref")
        _string(row["title"], 1, 300, f"{where}.title", one_line=True)
        _string(row["outcome"], 1, 300, f"{where}.outcome")
        _url(row.get("url"), f"{where}.url")
        _time(row["at"], f"{where}.at")
    capacity = body["capacity"]
    _keys(capacity, {"max_concurrent", "in_use"}, {"max_concurrent", "in_use"}, "capacity")
    _int(capacity["max_concurrent"], "capacity.max_concurrent")
    _int(capacity["in_use"], "capacity.in_use")
    health = body["health"]
    _keys(health, {"state", "headline"}, {"state", "headline"}, "health")
    assert health["state"] in ("ready", "degraded", "blocked")
    _string(health["headline"], 1, 300, "health.headline", one_line=True)
    assert machine_status.size(body) <= 65_536


# ── a fixture ledger with a bit of everything ───────────────────────────────


def _repo(conn) -> int:
    store.add_repo(
        conn,
        name="runtime",
        origin="https://github.com/acme/runtime",
        local_path="/tmp/runtime",
        default_branch="main",
        base_sha="a" * 40,
    )
    return int(store.get_repo(conn, "runtime")["id"])


def _worker(conn, run_id: int, title: str, status: str, repo_id: int | None = None) -> int:
    task_id = store.add_task(conn, run_id=run_id, title=title, repo_id=repo_id)
    store.update_task_fields(conn, task_id, status=status)
    return task_id


def _ticket(conn, title: str, *, item: str, key: str, url: str | None = None) -> int:
    """A ticket task (phase set) whose work item a worker in its run is for."""
    run_id = store.create_run(conn, title)
    ticket = store.add_task(conn, run_id=run_id, title=title)
    store.set_task_phase(conn, ticket, "dispatched")
    store.set_task_env(
        conn, ticket, papaya_events.PAPAYA_EVENT_METADATA, json.dumps({"work_item_id": item})
    )
    store.set_task_env(conn, ticket, papaya_events.WORK_ITEM_KEY, key)
    if url:
        store.set_task_env(conn, ticket, papaya_events.WORK_ITEM_URL, url)
    return run_id


@pytest.fixture
def ledger(ppy_home) -> dict[str, int]:
    """2 running, 1 reviewable, 1 delivered PR, 1 pending capability request,
    1 escalated decision (a todo waiting on the user), 1 blocked todo."""
    conn = init_db()
    try:
        repo_id = _repo(conn)
        run_a = _ticket(
            conn,
            "Snapshot route",
            item="item-a",
            key="PPY-120",
            url="https://app.trypapaya.ai/w/ppy/items/PPY-120",
        )
        first = _worker(conn, run_a, "Snapshot route", "in_progress", repo_id)
        run_mi = store.create_run(conn, "MI-7: spike")
        mi_ticket = store.add_task(conn, run_id=run_mi, title="MI-7: spike")
        store.set_task_phase(conn, mi_ticket, "dispatched")
        store.set_task_env(conn, mi_ticket, instructions.INSTRUCTION_KEY, "MI-7")
        second = _worker(conn, run_mi, "Spike the digest", "in_progress", repo_id)
        run_c = store.create_run(conn, "plain run")
        reviewable = _worker(conn, run_c, "Digest renderer", "worker_done", repo_id)
        delivered = _worker(conn, run_c, "Fleet view fields", "delivered", repo_id)
        store.append_event(
            conn,
            kind="delivered",
            payload={"task_id": delivered, "pr_url": "https://github.com/acme/runtime/pull/1024"},
            run_id=run_c,
            task_id=delivered,
        )
        decision = store.add_todo(conn, "Which pill copy ships?", task_id=first, blocked_on="user")
        blocked = store.add_todo(conn, "Rebase after 41 lands", blocked_on=f"task:{first}")
    finally:
        conn.close()
    progress.record(first, phase="implement", note="Route and tests written.")
    request = capability_requests.request(second, "psql", why="read the fixture database")
    return {
        "first": first,
        "second": second,
        "reviewable": reviewable,
        "delivered": delivered,
        "decision": decision,
        "blocked": blocked,
        "request": request.id,
    }


def _build(**kwargs: Any) -> dict[str, Any]:
    conn = init_db()
    try:
        return machine_status.build(
            conn,
            now=NOW,
            health=kwargs.pop("health", DEGRADED),
            max_concurrent=kwargs.pop("max_concurrent", 3),
            blockers_now=kwargs.pop("blockers_now", []),
            **kwargs,
        )
    finally:
        conn.close()


# ── (a) the builder ─────────────────────────────────────────────────────────


def test_a_the_fixture_ledger_builds_a_snapshot_inside_every_wire_bound(ledger) -> None:
    body = _build()
    validate(body)

    refs = [row["ref"] for row in body["in_flight"]]
    assert refs == [
        f"task-{ledger['first']}",
        f"task-{ledger['second']}",
        f"task-{ledger['reviewable']}",
    ]
    about = {row["ref"]: row["about"] for row in body["in_flight"]}
    assert about[f"task-{ledger['first']}"] == {
        "kind": "work_item",
        "short_id": "PPY-120",
        "url": "https://app.trypapaya.ai/w/ppy/items/PPY-120",
    }
    assert about[f"task-{ledger['second']}"] == {
        "kind": "machine_instruction",
        "short_id": "MI-7",
        "url": None,
    }
    assert about[f"task-{ledger['reviewable']}"] is None
    phases = {row["ref"]: row["phase"] for row in body["in_flight"]}
    assert phases[f"task-{ledger['first']}"] == "implement"
    assert phases[f"task-{ledger['reviewable']}"] == "waiting on review"

    needs = {row["kind"]: row for row in body["needs_you"]}
    assert needs["capability"]["how"].startswith(f"Send me: approve capability {ledger['request']}")
    assert needs["decision"]["how"] == f"Reply here with your decision (todo {ledger['decision']})"
    assert "pill copy" in needs["decision"]["text"]

    (blocked,) = body["blocked"]
    assert blocked["kind"] == "blocker" and blocked["ref"] == f"todo-{ledger['blocked']}"

    (finished,) = body["recently_finished"]
    assert finished == {
        "ref": f"task-{ledger['delivered']}",
        "title": "Fleet view fields",
        "outcome": "PR opened",
        "url": "https://github.com/acme/runtime/pull/1024",
        "at": finished["at"],
    }
    # Two running workers hold slots; the one waiting on review holds none.
    assert body["capacity"] == {"max_concurrent": 3, "in_use": 2}
    assert body["health"] == {"state": "degraded", "headline": DEGRADED.headline}
    assert body["summary"] == "3 tasks in flight; 2 waiting on you; 1 blocked; health degraded."


def test_a_every_ask_says_what_a_person_can_send_back(ledger, monkeypatch) -> None:
    """A pull request waiting on a person, both ways this install's merge authority can be."""
    from papaya_agent_runtime import supervision

    monkeypatch.setattr(
        supervision, "prs_needing_a_person", lambda: [(ledger["delivered"], "CI is red")]
    )
    off = {row["kind"]: row for row in _build(merge_allowed=False)["needs_you"]}
    assert off["pull_request"]["ref"] == "pr-1024"
    assert off["pull_request"]["how"] == "Review and merge it on GitHub, or send me: hold PR 1024"
    on = {row["kind"]: row for row in _build(merge_allowed=True)["needs_you"]}
    assert on["pull_request"]["how"] == "Send me: merge PR 1024, or hold PR 1024"
    for row in off.values():
        assert row["how"].startswith(("Send me:", "Reply here", "Review and merge"))


def test_a_recently_finished_keeps_the_ten_newest(ppy_home) -> None:
    conn = init_db()
    try:
        run = store.create_run(conn, "many")
        for n in range(14):
            _worker(conn, run, f"finished {n}", "closed")
    finally:
        conn.close()
    body = _build()
    validate(body)
    assert len(body["recently_finished"]) == 10


def test_a_over_long_strings_are_cut_to_their_bound_with_an_ellipsis(ppy_home) -> None:
    conn = init_db()
    try:
        run = store.create_run(conn, "long")
        _worker(conn, run, "T" * 900 + "\nsecond line", "in_progress")
        store.add_todo(conn, "Q" * 900, blocked_on="user")
    finally:
        conn.close()
    body = _build(health=machine_status.Health("ready", "H" * 400))
    validate(body)
    title = body["in_flight"][0]["title"]
    assert len(title) == 300 and title.endswith("…")
    assert "\n" not in title
    assert len(body["needs_you"][0]["text"]) == 500
    assert body["needs_you"][0]["text"].endswith("…")
    assert body["health"]["headline"].endswith("…") and len(body["health"]["headline"]) == 300


def test_a_a_body_over_64_kib_drops_rows_rather_than_being_refused(ppy_home) -> None:
    body = {
        "as_of": "2026-09-22T12:00:00+00:00",
        "summary": "Busy.",
        "in_flight": [
            {
                "ref": "x" * 200,
                "title": "t" * 300,
                "about": None,
                "phase": "p",
                "since": "2026-09-22T12:00:00+00:00",
            }
        ]
        * 25,
        "needs_you": [
            {
                "kind": "decision",
                "ref": "r" * 200,
                "text": "é" * 500,
                "how": "h" * 300,
                "since": "2026-09-22T12:00:00+00:00",
            }
        ]
        * 25,
        "blocked": [
            {
                "kind": "blocker",
                "ref": "r" * 200,
                "text": "é" * 500,
                "how": "h" * 300,
                "since": "2026-09-22T12:00:00+00:00",
            }
        ]
        * 25,
        "recently_finished": [],
        "capacity": {"max_concurrent": 3, "in_use": 1},
        "health": {"state": "ready", "headline": "All good."},
    }
    assert machine_status.size(body) > 65_536
    fitted = machine_status.fit(json.loads(json.dumps(body)))
    validate(fitted)
    assert len(fitted["blocked"]) < 25 or len(fitted["needs_you"]) < 25


def test_the_snapshot_never_carries_a_token_or_a_home_path(ppy_home, monkeypatch) -> None:
    """Matrix row 1: no tokens, no paths under home, no transcripts."""
    conn = init_db()
    try:
        run = store.create_run(conn, "leaky")
        task = _worker(
            conn, run, f"Use {PRIVACY_LEAKS['token']} in {PRIVACY_LEAKS['home']}", "in_progress"
        )
        store.add_todo(
            conn,
            f"Is pagc_live_secret_token right? see {Path.home()}/notes "
            f"and {PRIVACY_LEAKS['email']}",
            task_id=task,
            blocked_on="user",
        )
    finally:
        conn.close()
    progress.record(task, phase="implement", note="n")
    body = _build(
        blockers_now=[
            {
                "code": "gh_auth",
                "title": f"gh token {PRIVACY_LEAKS['token']} expired",
                "steps": [f"run gh auth login in {PRIVACY_LEAKS['home']}"],
                "since": "2026-09-22T11:00:00+00:00",
            }
        ]
    )
    validate(body)
    text = json.dumps(body)
    assert leaked(text) == []
    assert "pagc_live_secret_token" not in text
    assert str(Path.home()) not in text


def test_a_snapshot_is_one_read_even_while_a_worker_writes(ledger, monkeypatch) -> None:
    """Matrix row 3: a write landing mid-build is not half in the snapshot."""
    real = machine_status._recently_finished
    wrote = threading.Event()

    def mid_build(conn, now):
        # Another process finishes the first worker and delivers it, mid-build.
        def write() -> None:
            other = init_db()
            try:
                store.update_task_fields(other, ledger["first"], status="delivered")
            finally:
                other.close()
            wrote.set()

        thread = threading.Thread(target=write)
        thread.start()
        thread.join()
        return real(conn, now)

    monkeypatch.setattr(machine_status, "_recently_finished", mid_build)
    body = _build()
    assert wrote.is_set()
    in_flight = {row["ref"] for row in body["in_flight"]}
    finished = {row["ref"] for row in body["recently_finished"]}
    first = f"task-{ledger['first']}"
    # Read before the write: running, and not also finished.
    assert first in in_flight and first not in finished
    # And the next build sees the write whole.
    after = _build()
    assert first not in {row["ref"] for row in after["in_flight"]}
    assert first in {row["ref"] for row in after["recently_finished"]}


# ── (b) the publisher ───────────────────────────────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Puts:
    """The PUT route: records every body; raises what it is told to."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.fail: Exception | None = None

    def __call__(self, body: dict[str, Any]) -> bool:
        if self.fail is not None:
            raise self.fail
        self.bodies.append(body)
        return True


def _body(summary: str = "One running.") -> dict[str, Any]:
    return {
        "as_of": datetime.now(UTC).isoformat(timespec="seconds"),
        "summary": summary,
        "capacity": {"max_concurrent": 3, "in_use": 1},
        "health": {"state": "ready", "headline": "All good."},
    }


def test_b_an_identical_body_within_thirty_seconds_is_not_sent_again() -> None:
    puts, clock = Puts(), Clock()
    state = {"summary": "One running."}
    publisher = machine_status.Publisher(
        build=lambda: _body(state["summary"]), put=puts, clock=clock
    )

    async def scenario() -> list[str]:
        said = [await publisher.publish()]
        clock.now = 10
        said.append(await publisher.publish())  # same state, as_of moved: not sent
        state["summary"] = "Two running."
        said.append(await publisher.publish())  # changed: sent at once
        clock.now = 45
        said.append(await publisher.publish())  # same again, 35 s later: sent
        return said

    assert asyncio.run(scenario()) == ["sent", "unchanged", "sent", "sent"]
    assert [b["summary"] for b in puts.bodies] == ["One running.", "Two running.", "Two running."]


def test_b_a_422_is_logged_once_with_the_field_and_that_body_is_never_retried(caplog) -> None:
    puts = Puts()
    puts.fail = papaya_events.PapayaHTTPError(
        "refused",
        code=422,
        detail={"detail": [{"loc": ["body", "in_flight", 0, "title"], "msg": "one line"}]},
    )
    publisher = machine_status.Publisher(build=_body, put=puts, clock=Clock())
    caplog.set_level(logging.WARNING, logger="papaya_agent_runtime.machine_status")

    async def scenario() -> list[str]:
        return [await publisher.publish(), await publisher.publish(), await publisher.publish()]

    assert asyncio.run(scenario()) == ["refused", "refused", "refused"]
    said = [r.getMessage() for r in caplog.records if "422" in r.getMessage()]
    assert said == [
        "[status] Papaya refused the status snapshot (422) on in_flight.0.title; "
        "this body is not sent again"
    ]


def test_b_a_network_error_is_logged_and_the_round_goes_on(caplog) -> None:
    puts = Puts()
    puts.fail = papaya_events.PapayaEventError("Papaya could not be reached; retry")
    publisher = machine_status.Publisher(build=_body, put=puts, clock=Clock())
    caplog.set_level(logging.WARNING, logger="papaya_agent_runtime.machine_status")
    assert asyncio.run(publisher.publish()) == "failed"
    assert any("could not be reached" in r.getMessage() for r in caplog.records)
    puts.fail = None
    assert asyncio.run(publisher.publish()) == "sent"


def test_b_the_real_put_carries_a_422_field_from_papayas_body(monkeypatch) -> None:
    """`put_connection_status` through `_papaya_request`: the HTTP error keeps its body."""
    import io

    calls: list[tuple[str, str, Any]] = []

    def opener(request, timeout):
        calls.append((request.method, request.full_url, json.loads(request.data)))
        raise urllib.error.HTTPError(
            request.full_url,
            422,
            "Unprocessable",
            {},
            io.BytesIO(json.dumps({"detail": [{"loc": ["body", "summary"]}]}).encode()),
        )

    env = {
        "PAPAYA_API_URL": "https://papaya.example/api/v1",
        "PAPAYA_WORKSPACE_ID": "ws-1",
        "PAPAYA_AGENT_TOKEN": "pagc_x",
    }
    with pytest.raises(papaya_events.PapayaHTTPError) as refused:
        papaya_events.put_connection_status(_body(), environ=env, opener=opener)
    assert refused.value.code == 422 and refused.value.fields() == ["summary"]
    method, url, _body_sent = calls[0]
    assert method == "PUT"
    assert (
        url == "https://papaya.example/api/v1/workspaces/ws-1/polyweave-agents/me/connection/status"
    )


def test_b_every_round_publishes_once(ppy_home) -> None:
    from types import SimpleNamespace

    from papaya_agent_runtime import rounds

    puts = Puts()
    state = {"n": 0}

    def build() -> dict[str, Any]:
        state["n"] += 1
        return _body(f"round {state['n']}")

    walker = rounds.Rounds(
        SimpleNamespace(loop=None, agent_config={}),
        SimpleNamespace(held={}),
        forge=lambda _conn: [],
        prune=lambda _task_id: {"removed": [], "skipped": [], "reclaimed_bytes": 0},
        git=lambda *_a, **_k: 0,
        papaya_env=lambda: {},
        status_publisher=machine_status.Publisher(build=build, put=puts, clock=Clock()),
    )
    asyncio.run(walker.round_once())
    assert [b["summary"] for b in puts.bodies] == ["round 1"]
    asyncio.run(walker.round_once())
    assert [b["summary"] for b in puts.bodies] == ["round 1", "round 2"]


def test_b_a_standalone_round_publishes_nothing(ppy_home) -> None:
    from types import SimpleNamespace

    from papaya_agent_runtime import rounds

    puts = Puts()
    walker = rounds.Rounds(
        SimpleNamespace(loop=None, agent_config={}, standalone=True),
        SimpleNamespace(held={}),
        forge=lambda _conn: [],
        prune=lambda _task_id: {"removed": [], "skipped": [], "reclaimed_bytes": 0},
        git=lambda *_a, **_k: 0,
        status_publisher=machine_status.Publisher(build=_body, put=puts, clock=Clock()),
    )
    asyncio.run(walker.round_once())
    assert puts.bodies == []


def test_b_two_publishers_never_interleave_and_the_last_body_wins() -> None:
    """Matrix row 2: the round and the change watch share one lock."""
    order: list[str] = []
    state = {"n": 0}
    gate = threading.Event()

    def build() -> dict[str, Any]:
        state["n"] += 1
        n = state["n"]
        order.append(f"build {n}")
        if n == 1:
            gate.wait(2)  # the first build is slow; the second waits for the lock
        return _body(f"state {n}")

    def put(body: dict[str, Any]) -> bool:
        order.append(f"put {body['summary']}")
        return True

    publisher = machine_status.Publisher(build=build, put=put, clock=Clock())

    async def scenario() -> None:
        first = asyncio.create_task(publisher.publish("round"))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(publisher.publish("change"))
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())
    assert order == ["build 1", "put state 1", "build 2", "put state 2"]
    assert publisher.sent[-1]["summary"] == "state 2"


def test_b_a_change_on_the_board_is_published_at_once(ledger) -> None:
    """Phase change, ask change and capability change each publish before the next round."""
    puts = Puts()
    publisher = machine_status.Publisher(
        build=lambda: _build(), put=puts, clock=Clock(), dedup_seconds=0
    )
    ticks: asyncio.Queue[None] = asyncio.Queue()
    looked: list[int] = []

    async def sleep(_seconds: float) -> None:
        await ticks.get()

    def mark() -> Any:
        looked.append(1)
        return machine_status.mark_now()

    async def tick() -> None:
        seen = len(looked)
        ticks.put_nowait(None)
        while len(looked) == seen:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

    def phase_change() -> None:
        conn = init_db()
        try:
            from papaya_agent_runtime import serve

            ticket = store.add_task(conn, run_id=store.create_run(conn, "t"), title="t")
            serve.record_phase(conn, ticket, "picked_up")
        finally:
            conn.close()

    def ask_change() -> None:
        conn = init_db()
        try:
            store.add_todo(conn, "Ship it?", blocked_on="user")
        finally:
            conn.close()

    async def scenario() -> list[int]:
        watching = asyncio.create_task(publisher.watch(mark, sleep=sleep, interval=10))
        counts = []
        await tick()  # the first look only takes the mark
        counts.append(len(puts.bodies))
        await tick()  # nothing changed
        counts.append(len(puts.bodies))
        phase_change()
        await tick()
        counts.append(len(puts.bodies))
        ask_change()
        await tick()
        counts.append(len(puts.bodies))
        capability_requests.decide_request(
            ledger["request"], approve=False, reason="not on this task"
        )
        await tick()
        counts.append(len(puts.bodies))
        watching.cancel()
        return counts

    assert asyncio.run(scenario()) == [0, 0, 1, 2, 3]
