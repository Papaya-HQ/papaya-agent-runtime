"""An interactive manager's supervisor outlives the manager's session (runtime #94, item 7).

A harness-tracked background task is reclaimed under memory pressure and ends with the
session; `ppy supervisor serve` then treats the hangup as a shutdown and stops every
worker. `ppy supervisor start` runs it detached instead. The detached-process tests
spawn the real CLI in separate processes, as Middle Manager's `tests/test_detached.py`
does. The lifeline watcher that stops a dead supervisor's workers is checked and
restarted on the supervisor's own clock, and `ppy serve` notices when a supervisor it
adopted has gone.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import papaya_agent_runtime
from conftest import scale, wait_until
from papaya_agent_runtime import deficiencies, rounds, serve, takeover
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor import lifeline
from papaya_agent_runtime.supervisor.client import SupervisorClient, SupervisorUnavailable
from papaya_agent_runtime.supervisor.server import SupervisorServer

_SRC = os.path.dirname(os.path.dirname(papaya_agent_runtime.__file__))


def _env(home: Path) -> dict[str, str]:
    return dict(os.environ, PPY_HOME=str(home), PYTHONPATH=_SRC)


def _ppy(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "papaya_agent_runtime", *args],
        env=_env(home),
        capture_output=True,
        text=True,
        timeout=scale(60),
        check=False,
    )


def _pid_alive(pid: int) -> bool:
    return takeover.pid_alive(pid)


def _answering_pid() -> int | None:
    try:
        return SupervisorClient().ping().get("pid")
    except SupervisorUnavailable:
        return None


@pytest.fixture
def home(ppy_home):
    ppy_home.mkdir(parents=True, exist_ok=True)
    yield ppy_home
    # Never leave a detached supervisor behind, whatever the test did.
    pid = _answering_pid()
    with contextlib.suppress(SupervisorUnavailable):
        SupervisorClient().shutdown()
    if pid:
        deadline = time.monotonic() + scale(20)
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


def test_supervisor_start_detaches_and_outlives_the_shell_that_started_it(home) -> None:
    out = home / "shell-out.txt"
    # The shape of a harness background task: a shell leading its own process group,
    # which the harness kills as a group when it reclaims it or the session ends.
    with out.open("w") as fh:
        shell = subprocess.Popen(
            [
                "/bin/sh",
                "-c",
                f'"{sys.executable}" -m papaya_agent_runtime supervisor start; sleep 60',
            ],
            env=_env(home),
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        wait_until(
            lambda: "supervisor started detached" in out.read_text(),
            scale(30),
            what="`ppy supervisor start` to report the supervisor",
            interval=0.05,
        )
        said = out.read_text()
        pid = _answering_pid()
        assert pid is not None and f"(pid {pid})" in said
        assert "supervisor.log" in said and str(SupervisorClient().socket_path) in said
        # It holds the owner lock, in a session of its own.
        holder = takeover.inspect(str(home.resolve()))
        assert holder.held and holder.pid == pid
        assert os.getsid(pid) != os.getsid(shell.pid)
        assert os.getpgid(pid) != shell.pid

        # The harness takes the shell's whole group down.
        os.killpg(shell.pid, signal.SIGHUP)
        os.killpg(shell.pid, signal.SIGKILL)
        shell.wait(timeout=scale(10))
        time.sleep(0.5)
        assert _answering_pid() == pid
        assert (home / "run" / "supervisor.log").exists()
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(shell.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            shell.wait(timeout=5)

    # A second start reports the running one and changes nothing.
    again = _ppy(home, "supervisor", "start")
    assert again.returncode == 0, again.stderr
    assert f"supervisor already running (pid {pid})" in again.stdout
    assert _answering_pid() == pid

    stopped = _ppy(home, "supervisor", "stop")
    assert stopped.returncode == 0, stopped.stderr
    wait_until(lambda: not _pid_alive(pid), scale(30), what="the supervisor to exit")


def test_supervisor_serve_is_still_the_foreground_form(home) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "papaya_agent_runtime", "supervisor", "serve"],
        env=_env(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        wait_until(lambda: _answering_pid() == proc.pid, scale(30), what="serve to answer")
        # Blocking: the process that answers is the one this call started.
        assert proc.poll() is None
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=scale(30))


# ── the lifeline watcher ─────────────────────────────────────────────────────


@pytest.fixture
def fake_watcher(tmp_path, monkeypatch):
    """A watcher that writes every registration it receives to a file, one per start."""
    received = tmp_path / "watcher"
    received.mkdir()
    script = (
        "import os, sys\n"
        f"path = os.path.join({str(received)!r}, str(os.getpid()) + '.txt')\n"
        "with open(path, 'a') as fh:\n"
        "    for line in sys.stdin:\n"
        "        fh.write(line); fh.flush()\n"
    )
    monkeypatch.setattr(lifeline, "_command", lambda: [sys.executable, "-c", script])
    yield received
    lifeline.stop(timeout=5)


def _received(folder: Path, pid: int) -> list[str]:
    path = folder / f"{pid}.txt"
    return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def live_worker(ppy_home):
    """A runner row whose pid is a live process leading its own group, as a worker's is."""
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    conn = init_db()
    run_id = store.create_run(conn, "watcher")
    task_id = store.add_task(conn, run_id=run_id, title="a live worker")
    store.set_task_status(conn, task_id, "in_progress")
    store.register_runner(conn, runner_id="live", task_id=task_id, provider="fake", pid=proc.pid)
    store.update_runner(conn, "live", status="running")
    conn.close()
    yield proc.pid
    proc.kill()
    proc.wait()


def _kill_watcher() -> int:
    watcher = lifeline._watcher
    assert watcher is not None
    watcher.kill()
    watcher.wait()
    return watcher.pid


def _round_walker(on_round) -> rounds.Rounds:
    return rounds.Rounds(
        SimpleNamespace(standalone=True, loop=None, api=None, agent_config={}),
        SimpleNamespace(held={}),
        clock=lambda: datetime.now(UTC),
        forge=lambda _conn: [],
        prune=lambda _task_id: {"removed": [], "skipped": [], "reclaimed_bytes": 0},
        git=lambda *_a, **_k: 0,
        papaya_env=dict,
        on_round=on_round,
    )


def _lifeline_deficiencies() -> list:
    return [
        d for d in deficiencies.ledger(include_all=True) if d.kind == deficiencies.LIFELINE_DOWN
    ]


def test_a_killed_watcher_is_restarted_by_the_next_round_with_live_groups_registered(
    ppy_home, fake_watcher, live_worker
) -> None:
    assert lifeline.start()
    first = lifeline._watcher.pid
    killed = _kill_watcher()
    assert lifeline.status()["status"] == lifeline.MISSING

    keeper = serve.SupervisorKeeper(object(), stderr=io.StringIO())
    parts = asyncio.run(_round_walker(keeper.round).round_once())

    assert any("lifeline watcher had exited; restarted it" in p for p in parts), parts
    second = lifeline._watcher.pid
    assert second != killed and lifeline._watcher.poll() is None
    wait_until(
        lambda: f"+g {live_worker}" in _received(fake_watcher, second),
        scale(10),
        what="the restarted watcher to receive the live runner's group",
    )
    assert _received(fake_watcher, first) == []
    # Said, not silent: a deficiency, and `ppy health` shows the restart.
    assert _lifeline_deficiencies()
    seen = lifeline.status()
    assert seen["status"] == lifeline.RESTARTED and seen["restarts"] == 1
    assert "restarted 1 time(s)" in lifeline.describe(seen)
    # The next round finds it alive and says nothing.
    assert not any("lifeline" in p for p in asyncio.run(_round_walker(keeper.round).round_once()))


def test_the_supervisor_tick_restarts_a_watcher_with_no_rounds(
    ppy_home, fake_watcher, live_worker
) -> None:
    """`ppy supervisor start` runs a bare supervisor: its own tick keeps the watcher."""
    assert lifeline.start()
    _kill_watcher()
    server = SupervisorServer()
    server._tick_health()
    assert lifeline._watcher.poll() is None
    second = lifeline._watcher.pid
    wait_until(
        lambda: f"+g {live_worker}" in _received(fake_watcher, second),
        scale(10),
        what="the tick's watcher to receive the live runner's group",
    )


def test_a_process_that_never_started_a_watcher_does_not_start_one(ppy_home) -> None:
    assert lifeline.check().status == lifeline.NOT_OWNED
    assert lifeline._watcher is None
    assert lifeline.status()["status"] == "no owner"


def test_a_watcher_that_cannot_start_is_recorded(ppy_home, monkeypatch) -> None:
    monkeypatch.setattr(lifeline, "_command", lambda: ["/nonexistent/lifeline-watcher"])
    try:
        assert lifeline.start_or_record("ppy supervisor serve") is False
        (found,) = _lifeline_deficiencies()
        assert "could not start its lifeline watcher" in found.detail
        seen = lifeline.status()
        assert seen["status"] == lifeline.MISSING
        assert "MISSING" in lifeline.describe(seen)
    finally:
        lifeline.stop(timeout=1)


def test_health_shows_the_watcher(ppy_home, fake_watcher, capsys) -> None:
    from papaya_agent_runtime.cli import main

    main(["health"])
    assert "lifeline watcher: none" in capsys.readouterr().out
    assert lifeline.start()
    main(["health"])
    assert f"lifeline watcher: alive (pid {lifeline._watcher.pid}" in capsys.readouterr().out
    _kill_watcher()
    assert main(["health"]) == 1
    assert "lifeline watcher: MISSING" in capsys.readouterr().out


# ── G5: an adopted supervisor that goes ───────────────────────────────────────


def test_serve_takes_over_when_the_supervisor_it_adopted_has_gone(ppy_home) -> None:
    """Pinned 2026-09-18: before this, serve ran its rounds with no supervisor at all."""
    stderr = io.StringIO()
    adopted = SupervisorServer(role="serve")
    adopted.start_background()
    taken = None
    try:
        # Another start of the same build adopts it rather than owning a second one.
        server, status = serve.take_supervisor(stderr=stderr)
        assert (server, status) == (None, None)
        assert "adopted the running supervisor" in stderr.getvalue()

        started: list[str] = []
        wired: list = []
        keeper = serve.SupervisorKeeper(
            None, stderr=stderr, start_lifeline=lambda where: started.append(where) or True
        )
        keeper.wire = wired.append
        # While it answers, a round leaves it alone.
        assert asyncio.run(keeper.round()) == []
        assert keeper.server is None

        adopted.stop()
        assert _answering_pid() is None

        parts = asyncio.run(_round_walker(keeper.round).round_once())
        taken = keeper.server
        assert taken is not None and wired == [taken]
        assert started == ["ppy serve"]
        assert any("took over and owns the supervisor now" in p for p in parts), parts
        assert _answering_pid() == os.getpid()
        assert takeover.inspect(str(ppy_home.resolve())).held
    finally:
        adopted.stop()
        if taken is not None:
            taken.shutdown(timeout=5)
