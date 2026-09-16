"""A new `ppy serve` takes over from the old one without a person (task 272).

On 2026-09-16 the previous build's supervisor survived the app quitting with a worker
under it, and every restart of the new build refused to start until somebody sent
it a signal by hand. These drive the real supervisor with the fake provider's
`HOLD:` worker — a genuinely live session that an interrupt ends — so "recorded
stopped, session intact" is what the runner actually records, not a stub's word.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import test_serve
from conftest import scale, wait_until
from papaya_agent_runtime import papaya_events, repos, rounds, serve, takeover, turn_end
from papaya_agent_runtime.config import load_config, save_config
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.server import SupervisorServer, checkout_root
from test_serve import SUBJECT, FakeEvents, Harness

globals().update(
    {name: getattr(test_serve, name) for name in ("assigned", "client_home", "dm", "ready")}
)

HOME_BUILD = "0.0.0+feedfacecafe"


def _home() -> str:
    from papaya_agent_runtime.paths import ppy_home

    return str(ppy_home().resolve())


def _dispatch_holding(client: SupervisorClient, repo: str, *, run_id: int | None = None) -> int:
    """A live worker session, under `client`'s supervisor, that holds until interrupted."""
    answer = client.dispatch_task(repo=repo, title="hold", instructions="HOLD:120", run_id=run_id)
    task_id = int(answer["task_id"])

    def running_with_a_session() -> str | None:
        conn = init_db()
        try:
            rows = [r for r in store.task_runners(conn, task_id) if r["status"] == "running"]
            return rows[0]["session_id"] if rows and rows[0]["session_id"] else None
        finally:
            conn.close()

    wait_until(running_with_a_session, 20, what=f"worker task {task_id} to be running")
    return task_id


def _session_of(task_id: int) -> str:
    conn = init_db()
    try:
        row = conn.execute(
            "SELECT provider_session_id FROM sessions WHERE task_id = ? ORDER BY id DESC",
            (task_id,),
        ).fetchone()
        return str(row["provider_session_id"]) if row else ""
    finally:
        conn.close()


def _events(task_id: int, kind: str) -> list[dict[str, Any]]:
    conn = init_db()
    try:
        rows = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]
    finally:
        conn.close()


def _status(task_id: int) -> str:
    conn = init_db()
    try:
        return str(store.get_task(conn, task_id)["status"])
    finally:
        conn.close()


def _runners(task_id: int) -> list[Any]:
    conn = init_db()
    try:
        return store.task_runners(conn, task_id)
    finally:
        conn.close()


def _lines(stderr: io.StringIO) -> list[str]:
    return [line for line in stderr.getvalue().splitlines() if line.strip()]


# ── stopping serve stops what it started ─────────────────────────────────────


def test_serve_on_sigterm_stops_its_workers_recorded_stopped_with_sessions_intact(
    ppy_home, client_home, ready, assigned, dm, source_repo
) -> None:
    cfg = load_config()
    cfg.supervisor.stop_timeout = 20
    save_config(cfg)
    repo = repos.add_repo(source_repo).name
    harness = Harness(FakeEvents([]))
    seen: dict[str, Any] = {}

    def drive() -> None:
        try:
            wait_until(
                lambda: (
                    harness.loop is not None
                    and signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
                ),
                20,
                what="serve to be listening with its signal handlers installed",
            )
            seen["task"] = _dispatch_holding(SupervisorClient(), repo)
            seen["session"] = _session_of(seen["task"])
            seen["sent"] = time.monotonic()
            os.kill(os.getpid(), signal.SIGTERM)
        except BaseException as exc:  # noqa: BLE001 - reported by the test body below
            seen["error"] = exc
            os.kill(os.getpid(), signal.SIGINT)

    driver = threading.Thread(target=drive, daemon=True)
    driver.start()
    stderr = io.StringIO()
    status = serve.serve(
        ["--working-directory", str(client_home.work_dir)], stderr=stderr, **harness.extra()
    )
    returned = time.monotonic()
    driver.join(timeout=5)

    assert "error" not in seen, seen.get("error")
    assert status == 0, stderr.getvalue()
    assert returned - seen["sent"] < 20, "serve did not exit within supervisor.stop_timeout"
    task = seen["task"]
    assert _status(task) == turn_end.WORKER_STOPPED
    (stopped,) = _events(task, turn_end.WORKER_STOPPED)
    assert stopped["session_id"] == seen["session"] != ""
    assert "the supervisor shut down" in stopped["summary"]
    assert _session_of(task) == seen["session"], "the session was not kept for a resume"
    assert _events(task, "error") == []
    # The lock is free and nothing claims to hold it.
    assert not takeover.lock_held(_home())
    assert takeover.read_record(_home()) is None


_OWNER = """
import subprocess, sys, time
from papaya_agent_runtime.supervisor import lifeline
lifeline.start()
worker = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True
)
lifeline.watch_group(worker.pid)
print(worker.pid, flush=True)
time.sleep(120)
"""


def test_an_owner_killed_outright_takes_its_workers_down_with_it(tmp_path) -> None:
    """`kill -9` runs no Python in the owner; the lifeline stops what it started anyway."""
    owner = subprocess.Popen(
        [sys.executable, "-c", _OWNER],
        env={**os.environ, "PYTHONPATH": os.path.join(checkout_root(), "src")},
        stdout=subprocess.PIPE,
        text=True,
    )
    assert owner.stdout is not None
    worker = int(owner.stdout.readline())
    try:
        assert takeover.pid_alive(worker)
        # No wait for the watcher: what the owner wrote stays in the pipe after it dies.
        owner.kill()
        owner.wait(timeout=10)

        wait_until(lambda: not takeover.pid_alive(worker), 15, what="the orphaned worker to die")
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(worker, signal.SIGKILL)


# ── adopt ────────────────────────────────────────────────────────────────────


def test_a_live_supervisor_of_this_build_is_adopted_and_its_workers_are_untouched(
    ppy_home, source_repo
) -> None:
    repo = repos.add_repo(source_repo).name
    owner = SupervisorServer(role="serve")
    owner.start_background()
    try:
        client = SupervisorClient(owner.socket_path)
        task = _dispatch_holding(client, repo)
        record = takeover.read_record(_home())
        assert record is not None
        assert record["build_id"] == takeover.checkout_build(checkout_root())["build_id"]

        stderr = io.StringIO()
        server, status = serve.take_supervisor(stderr=stderr, seams={"kill": _never_kill})

        assert (server, status) == (None, None)
        (line,) = _lines(stderr)
        assert "adopted the running supervisor" in line and record["build_id"] in line
        assert not owner._stop.is_set(), "the adopted supervisor was asked to stop"
        assert client.ping()["pid"] == os.getpid()
        assert _status(task) == "in_progress"
        assert _events(task, turn_end.WORKER_STOPPED) == []
    finally:
        owner.stop()


def _never_kill(pid: int, sig: int) -> None:
    raise AssertionError(f"signal {sig} sent to pid {pid}")


# ── retire ───────────────────────────────────────────────────────────────────


def _seed_ticket() -> tuple[int, int]:
    """A ticket an earlier `serve` was working: picked up, briefed, dispatched."""
    conn = init_db()
    try:
        run_id = store.create_run(conn, "Fix the thing")
        ticket = store.add_task(conn, run_id=run_id, title="Fix the thing")
        event = papaya_events.PapayaEvent(
            id="77", kind="work_item.assigned", subject=SUBJECT, payload={}, work_item_id="item-9"
        )
        papaya_events.record_task(conn, ticket, event)
        for phase in (serve.PHASE_PICKED_UP, serve.PHASE_BRIEFING, serve.PHASE_DISPATCHED):
            serve.record_phase(conn, ticket, phase)
        return ticket, run_id
    finally:
        conn.close()


class OfferingLoop:
    """The client loop a round offers a reclaimed ticket to."""

    def __init__(self) -> None:
        self.offered: list[dict[str, Any]] = []
        self.running_subjects: set[str] = set()

    async def offer(self, envelope: dict[str, Any]) -> str:
        self.offered.append(envelope)
        return "pending"


def test_a_supervisor_of_another_build_is_retired_and_the_rounds_resume_its_worker(
    ppy_home, source_repo
) -> None:
    repo = repos.add_repo(source_repo).name
    ticket, run_id = _seed_ticket()
    old = SupervisorServer(role="serve")
    old.start_background()
    worker = _dispatch_holding(SupervisorClient(old.socket_path), repo, run_id=run_id)
    session = _session_of(worker)
    record = takeover.read_record(_home())
    assert record is not None
    record["build_id"] = HOME_BUILD  # the checkout has been pulled since it started
    takeover.write_record(
        _home(),
        pid=record["pid"],
        role=record["role"],
        socket_path=record["socket"],
        build={k: record[k] for k in ("git_head", "version", "build_id")},
    )

    stderr = io.StringIO()
    server, status = serve.take_supervisor(stderr=stderr, seams={"kill": _never_kill})
    try:
        assert status is None and server is not None, stderr.getvalue()
        assert old._stop.is_set()
        (line,) = _lines(stderr)
        assert "retired supervisor" in line and HOME_BUILD in line
        assert f"task {worker}" in line and "rounds resume" in line

        # The new supervisor is this build's, and the old worker was stopped, not failed.
        now = takeover.read_record(_home())
        assert now is not None and now["build_id"] != HOME_BUILD
        assert _status(worker) == turn_end.WORKER_STOPPED
        (stopped,) = _events(worker, turn_end.WORKER_STOPPED)
        assert stopped["session_id"] == session

        # The rounds' reclaim takes the ticket back up and sees a stopped worker to resume.
        assert [t.task_id for t in rounds.reclaimable(rounds.ticket_tasks())] == [ticket]
        loop = OfferingLoop()
        manager_rounds = rounds.Rounds(
            SimpleNamespace(loop=loop, agent_config={}),
            SimpleNamespace(held={}),
            forge=lambda _conn: [],
        )
        asyncio.run(manager_rounds._reclaim(rounds.ticket_tasks()))
        assert [e["payload"]["work_item"]["id"] for e in loop.offered] == ["item-9"]
        look = rounds.look_at_worker(
            worker, now=datetime.now(UTC), quiet_after=timedelta(minutes=15)
        )
        assert look is not None and look.status == turn_end.WORKER_STOPPED and look.stopped

        # And the resume is by that session, in its worktree, under the new supervisor.
        server.supervisor.resume_task(worker, "Pick it back up.")
        wait_until(
            lambda: any(r["session_id"] == session for r in _runners(worker)[1:]),
            20,
            what="the worker to resume under its own session",
        )
    finally:
        if server is not None:
            server.shutdown(timeout=scale(10))
        old.stop()


# ── a stale lock ─────────────────────────────────────────────────────────────


def test_a_stale_lock_is_taken_with_one_line(ppy_home) -> None:
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait(timeout=10)
    run = ppy_home / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "supervisor.lock").write_text(str(gone.pid))
    (run / "supervisor.pid").write_text(str(gone.pid))

    stderr = io.StringIO()
    server, status = serve.take_supervisor(stderr=stderr)
    try:
        assert status is None and server is not None
        assert _lines(stderr) == [
            f"ppy serve: took the supervisor lock from pid {gone.pid}, which is no longer running"
        ]
        assert (run / "supervisor.pid").read_text().strip() == str(os.getpid())
    finally:
        if server is not None:
            server.stop()
