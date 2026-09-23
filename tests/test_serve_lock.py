"""One `ppy serve` per home: a second start takes over from the first (task 368).

On 2026-09-22 a terminal `ppy serve` connected as @engineering_agent ran beside the
desktop's, connected as @shanes_eng_assistant, over one state database: the second
adopted the first's supervisor, both listened, and for seven hours the machine worked
every ticket twice and handed tickets over to itself. These pin the rule that ended
it: a serve holds ``run/serve.lock`` for its whole life, and the newest start retires
whichever serve holds it — asked first, then SIGTERM, then SIGKILL — or does not run.
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import test_serve
from conftest import scale, wait_until
from papaya_agent_runtime import papaya, rounds, serve, takeover
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.server import checkout_root
from test_serve import EVENT, FakeEvents, FakePapaya, FakeTurns, Harness

globals().update(
    {
        name: getattr(test_serve, name)
        for name in ("assigned", "client_home", "dm", "ready", "registered_repo")
    }
)

_ENV = {**os.environ, "PYTHONPATH": os.path.join(checkout_root(), "src")}

# A serve reduced to its lock: take it the way `serve` does, say so, hold it. `mode`
# is how it ends: `hold` (until SIGTERM, which it handles as serve does), `deaf`
# (ignores SIGTERM), or an exit path — `exit`, `raise`, `default-term`, `sigkill`.
_HOLDER = r"""
import os, signal, sys, time
from papaya_agent_runtime import takeover

home, log, name, mode = sys.argv[1:5]
held = {}

def say(what):
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(f"{time.time():.6f} {os.getpid()} {name} {what}\n")

def stop(*_):
    if "lock" in held:
        say("stop")
        held["lock"].release()
    sys.exit(0)

if mode == "hold":
    signal.signal(signal.SIGTERM, stop)
elif mode == "deaf":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
taken = takeover.take_serve(home, {"agent_handle": name, "connection_id": "c-" + name},
                            timeout=float(os.environ.get("HOLD_TIMEOUT", "5")), grace=5.0)
if not taken.ok:
    say("failed " + taken.line)
    sys.exit(1)
held["lock"] = taken.lock
say("start " + taken.line)
print("held", flush=True)
if mode == "exit":
    taken.lock.release()
    sys.exit(0)
if mode == "raise":
    raise RuntimeError("serve fell over")
if mode == "default-term":
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTERM)
if mode == "sigkill":
    os.kill(os.getpid(), signal.SIGKILL)
time.sleep(120)
"""


def _home() -> str:
    from papaya_agent_runtime.paths import ppy_home

    return str(ppy_home().resolve())


def _holder(home: str, log: Path, name: str, mode: str = "hold") -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _HOLDER, home, str(log), name, mode],
        env=_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _held(child: subprocess.Popen) -> None:
    assert child.stdout is not None
    line = child.stdout.readline()
    assert line.strip() == "held", child.stderr.read() if child.stderr else line


def _reap(*children: subprocess.Popen) -> None:
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def _log(path: Path) -> list[tuple[float, int, str, str]]:
    rows = []
    for line in path.read_text().splitlines():
        at, pid, name, what = line.split(" ", 3)
        rows.append((float(at), int(pid), name, what))
    return rows


def _lock_free(home: str) -> bool:
    """Could anyone take ``serve.lock`` now? Looks without writing to it."""
    import fcntl

    fd = os.open(takeover.serve_lock_path(home), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def _lines(stderr: io.StringIO) -> list[str]:
    return [line for line in stderr.getvalue().splitlines() if line.strip()]


class Ticking:
    """A clock that moves by what is slept, sleeping a little for real so children run."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        time.sleep(0.01)


# ── Goal 1: held for the whole life, and said who holds it ─────────────────────


def test_a_serve_holds_the_lock_and_names_itself_in_serve_json(ppy_home, monkeypatch) -> None:
    monkeypatch.setattr(
        papaya,
        "identity",
        lambda: papaya.Identity(handle="shanes_eng_assistant", connection_id="conn-2"),
    )
    stderr = io.StringIO()
    lock, status = serve.hold_serve(stderr=stderr)
    try:
        assert status is None and lock is not None
        assert _lines(stderr) == [], "a free lock is taken without a word"
        assert not _lock_free(_home())
        record = takeover.read_serve_record(_home())
        assert record is not None
        assert record["pid"] == os.getpid()
        assert record["connection_id"] == "conn-2"
        assert record["agent_handle"] == "shanes_eng_assistant"
        assert record["started_at"]
    finally:
        assert lock is not None
        lock.release()
    assert _lock_free(_home())
    assert takeover.read_serve_record(_home()) is None


@pytest.mark.parametrize("argv", [[], ["--supervised"]], ids=["terminal", "desktop"])
def test_every_serve_takes_the_lock_before_its_supervisor(ppy_home, monkeypatch, argv) -> None:
    """Goal 5: the desktop's supervised serve and a terminal serve follow the one rule."""
    seen: dict[str, Any] = {}

    def take_supervisor(**_kwargs: Any) -> tuple[None, int]:
        seen["free"] = _lock_free(_home())
        seen["record"] = takeover.read_serve_record(_home())
        return None, 7

    monkeypatch.setattr(serve, "take_supervisor", take_supervisor)
    assert serve.serve(argv, stderr=io.StringIO()) == 7
    assert seen["free"] is False
    assert seen["record"]["pid"] == os.getpid()
    assert _lock_free(_home()) and takeover.read_serve_record(_home()) is None


# ── Goal 2 / matrix: two starts, newest wins, never both ───────────────────────


@pytest.mark.parametrize("gap", [0.0, 0.05, 0.1], ids=["same-instant", "50ms", "100ms"])
def test_two_serves_started_together_never_run_at_once(ppy_home, tmp_path, gap) -> None:
    log = tmp_path / "serves.log"
    home = _home()
    first = _holder(home, log, "engineering_agent")
    time.sleep(gap)
    second = _holder(home, log, "shanes_eng_assistant")
    try:

        def settled() -> bool:
            if not log.exists():
                return False
            started = [r for r in _log(log) if r[3].startswith("start")]
            return len(started) == 2 and [first.poll(), second.poll()].count(None) == 1

        wait_until(settled, scale(30), what="one serve to have retired the other")
        gone, kept = (first, second) if first.poll() is not None else (second, first)
        assert gone.returncode == 0, "the retired serve was not stopped the orderly way"
        assert kept.poll() is None
        rows = _log(log)
        starts = [r for r in rows if r[3].startswith("start")]
        stops = [r for r in rows if r[3] == "stop"]
        assert [r[1] for r in stops] == [gone.pid]
        # Exactly one ran at any moment: the retired one stopped before the other started.
        (retired_start,) = [r for r in starts if r[1] == gone.pid]
        (winner_start,) = [r for r in starts if r[1] == kept.pid]
        assert retired_start[0] <= stops[0][0] <= winner_start[0]
        assert winner_start[3] == (
            f"start Took over from the runtime connected as @{retired_start[2]} (pid {gone.pid})."
        )
        assert takeover.read_serve_record(home)["pid"] == kept.pid
    finally:
        _reap(first, second)


def test_a_serve_that_ignores_the_request_gets_sigterm_then_sigkill(ppy_home, tmp_path) -> None:
    home = _home()
    deaf = _holder(home, tmp_path / "serves.log", "engineering_agent", "deaf")
    try:
        _held(deaf)
        # It owns the supervisor, so it is asked over the supervisor's socket first.
        takeover.write_record(
            home,
            pid=deaf.pid,
            role="serve",
            socket_path=str(tmp_path / "no.sock"),
            build={"git_head": "", "version": "", "build_id": ""},
        )
        steps: list[str] = []

        def shutdown(socket_path: str) -> bool:
            steps.append("asked")
            return True

        def kill(pid: int, sig: int) -> None:
            assert pid == deaf.pid
            steps.append(signal.Signals(sig).name)
            os.kill(pid, sig)

        clock = Ticking()
        taken = takeover.take_serve(
            home,
            {"agent_handle": "shanes_eng_assistant"},
            timeout=30.0,
            grace=20.0,
            clock=clock,
            sleep=clock.sleep,
            kill=kill,
            shutdown=shutdown,
        )
        try:
            assert taken.ok, taken.line
            assert steps == ["asked", "SIGTERM", "SIGKILL"]
            assert taken.line == (
                f"Took over from the runtime connected as @engineering_agent (pid {deaf.pid}); "
                "it did not stop until SIGKILL."
            )
            # Bounded: the ask got stop_timeout + 5, each signal its grace.
            assert clock.now <= 30.0 + 5.0 + 20.0 + 20.0 + 1.0
            deaf.wait(timeout=10)
            assert deaf.returncode == -signal.SIGKILL
        finally:
            assert taken.lock is not None
            taken.lock.release()
    finally:
        _reap(deaf)


def test_a_holder_that_survives_everything_means_no_start(ppy_home, tmp_path, monkeypatch) -> None:
    """Goal 4: exit 1 with one sentence and a blocker, never a second serve alongside."""
    home = _home()
    deaf = _holder(home, tmp_path / "serves.log", "engineering_agent", "deaf")
    try:
        _held(deaf)
        clock = Ticking()
        sent: list[int] = []
        stderr = io.StringIO()

        def take_supervisor(**_kwargs: Any) -> Any:
            raise AssertionError("a serve that could not take the lock went on to start")

        monkeypatch.setattr(serve, "take_supervisor", take_supervisor)
        status = serve.serve(
            [],
            stderr=stderr,
            serve_seams={
                "clock": clock,
                "sleep": clock.sleep,
                # A process this user cannot signal: nothing it is sent lands.
                "kill": lambda _pid, sig: sent.append(sig),
                "grace": 1.0,
            },
        )
        assert status == 1
        (line,) = _lines(stderr)
        assert line.startswith(
            f"ppy serve: cannot start: the runtime connected as @engineering_agent "
            f"(pid {deaf.pid}) still holds"
        )
        assert "sent SIGTERM, sent SIGKILL" in line and f"kill -9 {deaf.pid}" in line
        assert sent == [signal.SIGTERM, signal.SIGKILL]
        assert deaf.poll() is None, "the holder was left running, as it must be"
        failure = takeover.start_failure(home)
        assert failure is not None and failure["line"] in line
        ledger = json.loads(Path(home, "blockers.json").read_text())
        assert [b["code"] for b in ledger["open"].values()] == [takeover.START_FAILURE_CODE]
    finally:
        _reap(deaf)


# ── Goal 2 / matrix: a held ticket is left for the rounds' reclaim ─────────────


def test_retiring_a_serve_that_holds_a_ticket_leaves_it_for_the_reclaim(
    ppy_home, client_home, ready, assigned, dm, registered_repo, monkeypatch
) -> None:
    harness = Harness(FakeEvents([EVENT]))
    turns = FakeTurns(lambda turn: test_serve.dispatch_worker(turn.run_id))
    # `serve.serve` builds its own runner; hand it the fake one the serve tests use.
    runner = test_serve._runner(turns, FakePapaya())
    monkeypatch.setattr(serve, "TicketRunner", lambda: runner)
    retirer: dict[str, Any] = {}

    def retire() -> None:
        try:
            wait_until(
                lambda: (
                    serve.PHASE_DISPATCHED in test_serve.history()
                    and signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
                ),
                20,
                what="the old serve to be holding a dispatched ticket",
            )
            stderr = io.StringIO()
            # The second start. Its holder is this very process, so it is never
            # signalled: the supervisor socket's `shutdown` is all it gets, and must do.
            retirer["lock"], retirer["status"] = serve.hold_serve(
                stderr=stderr, seams={"kill": _never_kill}
            )
            retirer["lines"] = _lines(stderr)
        except BaseException as exc:  # noqa: BLE001 - reported by the test body
            retirer["error"] = exc
            if not retirer.get("serve_returned"):
                os.kill(os.getpid(), signal.SIGINT)

    thread = threading.Thread(target=retire, daemon=True)
    thread.start()
    stderr = io.StringIO()
    status = serve.serve(
        ["--working-directory", str(client_home.work_dir)], stderr=stderr, **harness.extra()
    )
    retirer["serve_returned"] = True
    thread.join(timeout=scale(60))
    try:
        assert "error" not in retirer, retirer.get("error")
        assert status == 0, stderr.getvalue()
        assert retirer["status"] is None and retirer["lock"] is not None
        (line,) = retirer["lines"]
        assert line.startswith("ppy serve: Took over from the runtime")
        assert line.endswith(f"(pid {os.getpid()}).")
        conn = init_db()
        try:
            ticket = test_serve.ticket_task(101)
            assert ticket is not None
            task_id = int(ticket["id"])
            phases = serve.phase_history(conn, task_id)
            assert store.task_phase(conn, task_id) == serve.PHASE_RELEASED
        finally:
            conn.close()
        # Not declined, not handed back: the rounds take it up again.
        assert serve.PHASE_DECLINED not in phases and serve.PHASE_HANDED_BACK not in phases
        assert [t.task_id for t in rounds.reclaimable(rounds.ticket_tasks())] == [task_id]
    finally:
        if retirer.get("lock") is not None:
            retirer["lock"].release()


def _never_kill(pid: int, sig: int) -> None:
    raise AssertionError(f"signal {sig} sent to pid {pid}")


# ── Goal 3: a stale lock ───────────────────────────────────────────────────────


def test_a_crashed_serves_lock_is_taken_with_one_line(ppy_home) -> None:
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait(timeout=10)
    run = ppy_home / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "serve.lock").write_text(str(gone.pid))
    (run / "serve.json").write_text(
        json.dumps({"pid": gone.pid, "agent_handle": "engineering_agent"})
    )

    stderr = io.StringIO()
    lock, status = serve.hold_serve(stderr=stderr, seams={"kill": _never_kill})
    try:
        assert status is None and lock is not None
        assert _lines(stderr) == [
            f"ppy serve: took the serve lock from pid {gone.pid} (the runtime connected as "
            "@engineering_agent), which is no longer running"
        ]
        assert (run / "serve.lock").read_text() == str(os.getpid())
        assert takeover.read_serve_record(_home())["pid"] == os.getpid()
    finally:
        assert lock is not None
        lock.release()


# ── matrix: the lock is let go on every exit path ──────────────────────────────


@pytest.mark.parametrize("mode", ["exit", "raise", "default-term", "sigkill"])
def test_the_lock_is_let_go_however_a_serve_ends(ppy_home, tmp_path, mode) -> None:
    home = _home()
    child = _holder(home, tmp_path / "serves.log", "engineering_agent", mode)
    try:
        _held(child)
        child.wait(timeout=scale(20))
        assert _lock_free(home)
        stderr = io.StringIO()
        lock, status = serve.hold_serve(stderr=stderr, seams={"kill": _never_kill})
        assert status is None and lock is not None
        lock.release()
        lines = _lines(stderr)
        if mode == "exit":
            # An orderly exit leaves nothing behind to call a crash.
            assert lines == []
        else:
            (line,) = lines
            assert f"took the serve lock from pid {child.pid}" in line
    finally:
        _reap(child)


@pytest.mark.parametrize("ending", ["return", "exception"])
def test_serve_lets_go_of_the_lock_when_it_returns_or_raises(ppy_home, monkeypatch, ending) -> None:
    def holding(*_args: Any, **_kwargs: Any) -> int:
        assert not _lock_free(_home())
        if ending == "exception":
            raise RuntimeError("fell over")
        return 0

    monkeypatch.setattr(serve, "_serve_holding", holding)
    if ending == "exception":
        with pytest.raises(RuntimeError):
            serve.serve([], stderr=io.StringIO())
    else:
        assert serve.serve([], stderr=io.StringIO()) == 0
    assert _lock_free(_home())
    assert takeover.read_serve_record(_home()) is None
    assert Path(takeover.serve_lock_path(_home())).read_text() == ""
