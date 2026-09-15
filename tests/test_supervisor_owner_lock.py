"""One supervisor per PPY_HOME, enforced across processes and across a start race (issue #69).

The connect-before-bind probe could not protect two simultaneous starts, nor a
bound-but-not-listening window, and a losing stop could delete the winner's
socket and pid file. These tests run real separate processes.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

import papaya_agent_runtime
from conftest import scale
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.server import (
    SupervisorOwned,
    SupervisorServer,
    default_socket_path,
    owner_lock_path,
)

_SRC = os.path.dirname(os.path.dirname(papaya_agent_runtime.__file__))

# A child that starts a supervisor once a "go" file appears, reports the
# outcome on one line, then serves until its stdin closes.
_CHILD = """
import os, sys, time
from papaya_agent_runtime.supervisor.server import SupervisorServer, SupervisorOwned
go = sys.argv[1]
server = SupervisorServer()
print("waiting", flush=True)
while not os.path.exists(go):
    time.sleep(0.005)
try:
    server.start_background()
except SupervisorOwned as exc:
    print("refused: " + str(exc), flush=True)
    sys.exit(3)
print("owner " + str(os.getpid()), flush=True)
sys.stdin.read()
server.stop()
"""


def _spawn(ppy_home: Path, go: Path) -> subprocess.Popen:
    env = dict(os.environ, PPY_HOME=str(ppy_home), PYTHONPATH=_SRC)
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(go)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "waiting"
    return proc


def _outcome(proc: subprocess.Popen) -> str:
    assert proc.stdout is not None
    return proc.stdout.readline().strip()


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        with __import__("contextlib").suppress(OSError):
            proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _pid_of(outcome: str) -> int:
    assert outcome.startswith("owner "), outcome
    return int(outcome.split()[1])


@pytest.fixture
def home(ppy_home):
    ppy_home.mkdir(parents=True, exist_ok=True)
    return ppy_home


def test_two_processes_racing_to_start_leave_exactly_one_owner(home, tmp_path) -> None:
    go = tmp_path / "go"
    first, second = _spawn(home, go), _spawn(home, go)
    try:
        go.touch()
        outcomes = sorted([_outcome(first), _outcome(second)])
        owners = [o for o in outcomes if o.startswith("owner ")]
        refused = [o for o in outcomes if o.startswith("refused:")]
        assert len(owners) == 1 and len(refused) == 1, outcomes
        owner_pid = _pid_of(owners[0])
        assert f"pid {owner_pid}" in refused[0]
        assert "one supervisor per PPY_HOME" in refused[0]

        # The winner is still up, on its own pid, and its files are its own.
        assert SupervisorClient().ping()["pid"] == owner_pid
        assert (home / "run" / "supervisor.pid").read_text().strip() == str(owner_pid)
        assert Path(default_socket_path()).exists()
    finally:
        _stop(first)
        _stop(second)


def test_a_late_starter_refuses_without_touching_the_owners_files(home, tmp_path) -> None:
    go = tmp_path / "go"
    owner = _spawn(home, go)
    try:
        go.touch()
        owner_pid = _pid_of(_outcome(owner))
        socket_stat = os.stat(default_socket_path())
        pid_text = (home / "run" / "supervisor.pid").read_text()

        late = _spawn(home, tmp_path / "go2")
        (tmp_path / "go2").touch()
        assert _outcome(late).startswith("refused:")
        assert late.wait(timeout=10) == 3

        assert os.stat(default_socket_path()).st_ino == socket_stat.st_ino
        assert (home / "run" / "supervisor.pid").read_text() == pid_text
        assert SupervisorClient().ping()["pid"] == owner_pid
    finally:
        _stop(owner)


def test_an_orderly_stop_hands_the_home_to_the_next_start(home, tmp_path) -> None:
    first = _spawn(home, tmp_path / "go")
    (tmp_path / "go").touch()
    first_pid = _pid_of(_outcome(first))
    _stop(first)
    assert not Path(default_socket_path()).exists()
    assert not (home / "run" / "supervisor.pid").exists()

    second = _spawn(home, tmp_path / "go2")
    try:
        (tmp_path / "go2").touch()
        second_pid = _pid_of(_outcome(second))
        assert second_pid != first_pid
        assert SupervisorClient().ping()["pid"] == second_pid
    finally:
        _stop(second)


def test_a_crashed_owner_is_replaced_and_its_surviving_child_is_still_counted(
    home, tmp_path
) -> None:
    owner = _spawn(home, tmp_path / "go")
    (tmp_path / "go").touch()
    owner_pid = _pid_of(_outcome(owner))

    # A recorded worker process that outlives its supervisor.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    conn = init_db()
    run_id = store.create_run(conn, "survivor")
    task_id = store.add_task(conn, run_id=run_id, title="survivor")
    store.set_task_status(conn, task_id, "in_progress")
    store.register_runner(conn, runner_id="survivor", task_id=task_id, provider="fake")
    store.update_runner(conn, "survivor", pid=child.pid, status="running")

    os.kill(owner_pid, signal.SIGKILL)
    owner.wait(timeout=10)
    assert Path(default_socket_path()).exists()  # the crash left its files behind
    assert Path(owner_lock_path()).exists()

    replacement = _spawn(home, tmp_path / "go2")
    try:
        (tmp_path / "go2").touch()
        new_pid = _pid_of(_outcome(replacement))
        assert new_pid != owner_pid
        client = SupervisorClient()
        assert client.ping()["pid"] == new_pid
        assert (home / "run" / "supervisor.pid").read_text().strip() == str(new_pid)

        # Owning the lock says nothing about the children: the live one is
        # still a live runner to the replacement, and only its death changes that.
        assert client.reconcile()["reconciled"] == []
        assert store.get_runner(init_db(), "survivor")["status"] == "running"
        child.kill()
        child.wait(timeout=10)
        deadline = time.monotonic() + scale(5)
        while store.get_runner(init_db(), "survivor")["status"] == "running":
            assert time.monotonic() < deadline
            client.reconcile()
            time.sleep(0.05)
        assert store.get_runner(init_db(), "survivor")["status"] == "orphaned"
    finally:
        _stop(replacement)
        if child.poll() is None:
            child.kill()


def test_a_custom_socket_path_does_not_escape_the_owner_lock(home) -> None:
    # AF_UNIX paths are short on macOS; pytest's tmp_path is not.
    short = Path(tempfile.mkdtemp(prefix="ppy-", dir="/tmp" if os.path.isdir("/tmp") else None))
    a, b = short / "a.sock", short / "b.sock"
    first = SupervisorServer(socket_path=str(a))
    first.start_background()
    try:
        with pytest.raises(SupervisorOwned, match="already owns"):
            SupervisorServer(socket_path=str(b)).start_background()
        assert not b.exists()
        assert a.exists()
    finally:
        first.stop()
    assert not a.exists()
    shutil.rmtree(short, ignore_errors=True)


def test_the_lock_lives_under_the_canonical_home(home, monkeypatch) -> None:
    link = home.parent / "home-link"
    link.symlink_to(home)
    monkeypatch.setenv("PPY_HOME", str(link))
    assert owner_lock_path() == str(home.resolve() / "run" / "supervisor.lock")
