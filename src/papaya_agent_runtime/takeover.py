"""A new `ppy serve` takes over from the old one without a person.

On 2026-09-16 Shane pulled a new build and reopened the app. The previous build's
supervisor had survived the app quitting, with a worker under it; the new launcher
refused to sync while that pid held the supervisor lock, exited 75 on every restart,
and the only way out was somebody sending the old process a signal and running the
launcher by hand. This module is the decision that person made, made by the runtime.

When `ppy serve` starts, the lock at ``<PPY_HOME>/run/supervisor.lock`` is in one of
four states, and each has exactly one answer:

- **free** — nobody is running. Start.
- **stale** — free, but the lock file (or ``supervisor.pid``) names a pid that is no
  longer running: a crashed holder. Take it, clear what it left, say so in one line.
- **adopt** — held by a live supervisor whose recorded build (``supervisor.json``: git
  head, package version, start time) is this checkout's. Connect to it and carry on;
  its workers keep running.
- **retire** — held by a live supervisor from another build (the checkout was pulled),
  or one that wrote no build at all (every build before this one), or whenever the
  environment has to be rebuilt under it. Ask it to shut down over its socket, wait for
  it to let go — a supervisor of this build first waits for its workers to be recorded
  stopped, sessions intact — then SIGTERM, then SIGKILL, and start fresh. The rounds'
  reclaim resumes each stopped worker by its session. One line says what happened.

`bin/ppy` runs this before the environment sync, so the sync's refusal can only ever
be about a supervisor this start chose to keep; `serve` runs the same decision in
Python when it finds the lock held anyway. A start that cannot go on — the holder
would not let go even to SIGKILL — prints one sentence, records it for the blockers
ledger, and exits 1. Never 75: that status means "somebody else has this", and the
condition here is one the runtime either fixed or could not.

Standard library only and 3.9-compatible, like `envsync.py`: it runs before the
environment is known to import.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

FREE = "free"
STALE = "stale"
ADOPT = "adopt"
RETIRE = "retire"

#: Exit statuses of `python -m papaya_agent_runtime.takeover`.
EXIT_START = 0
EXIT_CANNOT_START = 1
EXIT_ADOPTED = 3

#: How long a supervisor waits for its workers when nobody configured it.
DEFAULT_STOP_TIMEOUT = 30

#: After the polite request's wait, how long each signal is given to work.
SIGNAL_GRACE_SECONDS = 10.0

#: The blocker code a start that could not go on is recorded under.
START_FAILURE_CODE = "serve_cannot_start"

#: What a person reads after the steps: the runtime takes it from there.
AFTER = "then leave it: the app starts `ppy serve` again by itself"


# ── where things are ───────────────────────────────────────────────────────


def home_dir() -> str:
    home = os.environ.get("PPY_HOME") or os.path.join(os.getcwd(), ".ppy")
    return os.path.realpath(home)


def lock_path(home: str) -> str:
    return os.path.join(home, "run", "supervisor.lock")


def record_path(home: str) -> str:
    return os.path.join(home, "run", "supervisor.json")


def pid_path(home: str) -> str:
    return os.path.join(home, "run", "supervisor.pid")


def start_failure_path(home: str) -> str:
    return os.path.join(home, "run", "start-failure.json")


def default_socket_path(home: str) -> str:
    """The same short path `supervisor.server.default_socket_path` derives."""
    key = hashlib.sha1(os.path.realpath(home).encode()).hexdigest()[:10]
    base = "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()
    return os.path.join(base, f"ppy-{key}.sock")


def _now() -> str:
    # `timezone.utc`, not `datetime.UTC`: this module runs under 3.9 interpreters.
    return datetime.now(timezone.utc).isoformat(timespec="seconds")  # noqa: UP017


# ── the build ────────────────────────────────────────────────────────────


def checkout_build(root: str) -> dict[str, str]:
    """This checkout's code version: git head and package version, and the id made of them."""
    head = ""
    try:
        proc = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0:
            head = proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        head = ""
    try:
        from papaya_agent_runtime import __version__ as version
    except ImportError:  # pragma: no cover - the package is what is running this
        version = "unknown"
    # The build id drops the version's local segment. Since the version became
    # git-derived, that segment carries `.dirty`, and a build id that moved
    # whenever the working tree did would read as "another build" to `decide()` —
    # so editing one file under a running supervisor would retire it, workers and
    # all. The commit is already named here; the release is all the version needs
    # to contribute, and it does not flicker.
    public = str(version).split("+", 1)[0]
    return {
        "git_head": head,
        "version": str(version),
        "build_id": f"{public}+{head[:12] or 'unknown'}",
    }


def write_record(
    home: str, *, pid: int, role: str, socket_path: str, build: dict[str, str]
) -> None:
    """``supervisor.json``: who holds the lock, from which build, since when."""
    path = record_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "pid": pid,
        "role": role,
        "socket": socket_path,
        "started_at": _now(),
        **build,
    }
    temp = f"{path}.{os.getpid()}.tmp"
    with open(temp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(temp, path)


def read_record(home: str) -> dict[str, Any] | None:
    try:
        with open(record_path(home), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def remove_record(home: str, pid: int) -> None:
    """Remove ``supervisor.json`` if it is ``pid``'s — never a successor's."""
    record = read_record(home)
    if record is not None and record.get("pid") == pid:
        with contextlib.suppress(OSError):
            os.unlink(record_path(home))


# ── the holder ─────────────────────────────────────────────────────────


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pid(path: str) -> int | None:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read(32).strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def lock_held(home: str) -> bool:
    """Is the owner lock held by a live process? Takes and drops it to find out."""
    path = lock_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


@dataclass
class Holder:
    """What the lock, its pid and ``supervisor.json`` say about who runs this home."""

    held: bool
    pid: int | None = None
    alive: bool = False
    record: dict[str, Any] | None = None
    #: A recorded pid that is not running any more: what a crash leaves behind.
    dead_pids: list[int] = field(default_factory=list)

    @property
    def build_id(self) -> str | None:
        if self.record is None or self.record.get("pid") != self.pid:
            return None
        return self.record.get("build_id")

    @property
    def socket_path(self) -> str | None:
        return (self.record or {}).get("socket")


def inspect(home: str) -> Holder:
    held = lock_held(home)
    lock_pid = _read_pid(lock_path(home))
    record = read_record(home)
    pid = lock_pid or (record or {}).get("pid") or _read_pid(pid_path(home))
    own = os.getpid()
    dead = []
    for candidate in (lock_pid, _read_pid(pid_path(home)), (record or {}).get("pid")):
        if candidate and candidate != own and candidate not in dead and not pid_alive(candidate):
            dead.append(candidate)
    return Holder(
        held=held,
        pid=pid if isinstance(pid, int) else None,
        alive=pid_alive(pid) if isinstance(pid, int) else False,
        record=record,
        dead_pids=dead,
    )


def decide(holder: Holder, build: dict[str, str], *, environment_ok: bool = True) -> str:
    if not holder.held:
        return STALE if holder.dead_pids else FREE
    if environment_ok and holder.alive and holder.build_id == build.get("build_id"):
        return ADOPT
    return RETIRE


def clear_stale(home: str, dead_pids: list[int], socket_path: str | None = None) -> None:
    """Remove what crashed holders left: their pid in the lock file, pid file, record, socket."""
    path = lock_path(home)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # somebody live has it now; nothing of theirs is ours to clear
        try:
            if _read_pid(path) in dead_pids:
                os.ftruncate(fd, 0)
            if _read_pid(pid_path(home)) in dead_pids:
                with contextlib.suppress(OSError):
                    os.unlink(pid_path(home))
            record = read_record(home)
            if record is not None and record.get("pid") in dead_pids:
                socket_path = socket_path or record.get("socket")
                with contextlib.suppress(OSError):
                    os.unlink(record_path(home))
            socket_path = socket_path or default_socket_path(home)
            if os.path.exists(socket_path) and not _answers(socket_path):
                with contextlib.suppress(OSError):
                    os.unlink(socket_path)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def stale_line(dead_pids: list[int]) -> str:
    pids = ", ".join(str(pid) for pid in dead_pids)
    return f"took the supervisor lock from pid {pids}, which is no longer running"


# ── asking it to stop ──────────────────────────────────────────────────────


def _answers(socket_path: str) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        probe.connect(socket_path)
    except OSError:
        return False
    finally:
        probe.close()
    return True


def request_shutdown(socket_path: str, timeout: float = 5.0) -> bool:
    """Send the supervisor socket's `shutdown` (what `ppy supervisor stop` sends)."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(socket_path)
    except OSError:
        return False
    try:
        sock.sendall((json.dumps({"cmd": "shutdown"}) + "\n").encode("utf-8"))
        answer = b""
        while b"\n" not in answer:
            chunk = sock.recv(4096)
            if not chunk:
                break
            answer += chunk
        return bool(json.loads(answer.split(b"\n", 1)[0] or b"{}").get("ok"))
    except (OSError, ValueError):
        return False
    finally:
        sock.close()


def live_worker_tasks(home: str) -> list[int]:
    """Tasks with a live runner row: the workers a retirement is about to stop."""
    return _query_ids(
        home,
        "SELECT DISTINCT task_id FROM runners WHERE status IN ('starting','running') "
        "AND task_id IS NOT NULL ORDER BY task_id",
        (),
    )


def stopped_tasks(home: str, task_ids: list[int]) -> list[int]:
    """Of ``task_ids``, the ones recorded stopped: what the rounds will resume."""
    if not task_ids:
        return []
    marks = ",".join("?" for _ in task_ids)
    return _query_ids(
        home,
        f"SELECT id FROM tasks WHERE id IN ({marks}) AND status = 'worker_stopped' ORDER BY id",
        tuple(task_ids),
    )


def _query_ids(home: str, sql: str, params: tuple) -> list[int]:
    path = os.path.join(home, "state.db")
    if not os.path.exists(path):
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        return [int(row[0]) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


@dataclass
class Retired:
    ok: bool
    line: str
    #: The tasks whose workers were running under the old supervisor and are now stopped.
    tasks: list[int] = field(default_factory=list)


def retire(
    home: str,
    holder: Holder,
    build: dict[str, str],
    *,
    timeout: float,
    grace: float = SIGNAL_GRACE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    kill: Callable[[int, int], None] = os.kill,
    shutdown: Callable[[str], bool] = request_shutdown,
    reason: str = "",
) -> Retired:
    """Stop the supervisor holding the lock, as politely as it will allow."""
    running = live_worker_tasks(home)
    pid = holder.pid if holder.alive else None
    own = os.getpid()
    old = holder.build_id or "no recorded build"
    why = reason or (
        f"it runs {old} and this checkout is {build.get('build_id')}"
        if holder.build_id != build.get("build_id")
        else "the environment it runs from has to be rebuilt"
    )
    how = []

    def wait(seconds: float, done: Callable[[], bool]) -> bool:
        deadline = clock() + seconds
        while True:
            if done():
                return True
            if clock() >= deadline:
                return False
            sleep(0.1)

    def released() -> bool:
        return not lock_held(home)

    socket_path = holder.socket_path or default_socket_path(home)
    asked = shutdown(socket_path)
    if asked:
        how.append("asked it to shut down")
    # A supervisor of this build waits `stop_timeout` for its workers; give it that,
    # and a little. One that did not even answer is not going to let go by itself.
    let_go = wait(timeout + 5.0 if asked else 1.0, released)
    for sig, name in ((signal.SIGTERM, "SIGTERM"), (signal.SIGKILL, "SIGKILL")):
        if let_go or pid is None or pid == own:
            break
        with contextlib.suppress(ProcessLookupError, PermissionError):
            kill(pid, sig)
        how.append(f"sent {name}")
        let_go = wait(grace, released)
    if not let_go:
        who = f"pid {pid}" if pid else "a process this runtime cannot name"
        return Retired(
            False,
            f"cannot start: {who} still holds {lock_path(home)} after "
            + (", ".join(how) or "being asked to shut down")
            + "; stop it (`kill "
            + (str(pid) if pid else "<pid>")
            + "`) and the app starts serve again",
        )
    # An older `serve` let go of the lock when its supervisor thread stopped but
    # kept its listener running. It is retired too: one manager per home.
    if pid is not None and pid != own and not wait(grace, lambda: not pid_alive(pid)):
        for sig, name in ((signal.SIGTERM, "SIGTERM"), (signal.SIGKILL, "SIGKILL")):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                kill(pid, sig)
            how.append(f"sent {name}")
            if wait(grace, lambda: not pid_alive(pid)):
                break
    stopped = stopped_tasks(home, running)
    label = f"pid {pid}" if pid else "the previous supervisor"
    line = f"retired supervisor {label} ({why}): " + ", ".join(how or ["it had already stopped"])
    if running:
        line += "; its workers for task " + ", ".join(str(t) for t in running) + " were stopped"
        if stopped:
            line += " and the rounds resume task " + ", ".join(str(t) for t in stopped)
            line += " from " + ("its session" if len(stopped) == 1 else "their sessions")
    return Retired(True, line, stopped)


# ── one serve per home ─────────────────────────────────────────────────────
#
# On 2026-09-22 a `ppy serve` started from a terminal ran beside the one the desktop
# app had started, over one state database: the second adopted the first's
# supervisor, both listened, and for seven hours the machine worked every ticket
# twice and handed tickets over to itself. The supervisor lock makes the supervisor
# single; this lock makes `serve` single. A `serve` takes ``<PPY_HOME>/run/serve.lock``
# before anything else and holds it for its whole life — the kernel drops a `flock`
# however the process ends, SIGKILL included. The newest start wins: the owner
# switches agents by starting `ppy serve` again, so a second start retires the
# running one (whatever agent it is connected as) rather than giving way to it.


def serve_lock_path(home: str) -> str:
    return os.path.join(home, "run", "serve.lock")


def serve_record_path(home: str) -> str:
    return os.path.join(home, "run", "serve.json")


def read_serve_record(home: str) -> dict[str, Any] | None:
    try:
        with open(serve_record_path(home), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def runtime_label(record: dict[str, Any] | None) -> str:
    """ "the runtime connected as @handle", or as much of that as the record knows."""
    handle = str((record or {}).get("agent_handle") or "").lstrip("@")
    return f"the runtime connected as @{handle}" if handle else "the runtime"


@dataclass
class ServeLock:
    """This process's hold on ``serve.lock``, and the ``serve.json`` that says who holds it."""

    home: str
    fd: int
    pid: int

    def release(self) -> None:
        """Let go: the record if it is still ours, the pid in the lock file, the lock."""
        if self.fd < 0:
            return
        record = read_serve_record(self.home)
        if record is not None and record.get("pid") == self.pid:
            with contextlib.suppress(OSError):
                os.unlink(serve_record_path(self.home))
        # An orderly exit leaves no pid behind, so the next start does not call it a crash.
        with contextlib.suppress(OSError):
            os.ftruncate(self.fd, 0)
        with contextlib.suppress(OSError):
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self.fd)
        self.fd = -1


def _try_serve_lock(home: str) -> tuple[int, int | None] | None:
    """The lock's fd and the pid a previous holder left in it, or None while it is held."""
    path = serve_lock_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # O_CLOEXEC: a worker or turn this serve starts must not inherit the lock and
    # keep it after the serve has gone.
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    left = _read_pid(path)
    os.ftruncate(fd, 0)
    os.pwrite(fd, str(os.getpid()).encode(), 0)
    return fd, left


def _write_serve_record(home: str, identity: dict[str, str]) -> None:
    _write_json(
        serve_record_path(home),
        {
            "pid": os.getpid(),
            "connection_id": identity.get("connection_id") or "",
            "agent_handle": (identity.get("agent_handle") or "").lstrip("@"),
            "started_at": _now(),
        },
    )


def serve_holder_pid(home: str) -> int | None:
    """The pid holding ``serve.lock``: the lock file names it, ``serve.json`` as a fallback."""
    pid = _read_pid(serve_lock_path(home))
    if pid:
        return pid
    record = read_serve_record(home) or {}
    return record.get("pid") if isinstance(record.get("pid"), int) else None


@dataclass
class ServeTaken:
    """What :func:`take_serve` did: the lock (None when it could not), and its one line."""

    lock: ServeLock | None
    line: str = ""
    #: The pid that held the lock and had to be sent a signal or asked to stop.
    retired_pid: int | None = None

    @property
    def ok(self) -> bool:
        return self.lock is not None


def take_serve(
    home: str,
    identity: dict[str, str] | None = None,
    *,
    timeout: float,
    grace: float = SIGNAL_GRACE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    kill: Callable[[int, int], None] = os.kill,
    shutdown: Callable[[str], bool] = request_shutdown,
    own_pid: int | None = None,
) -> ServeTaken:
    """Take this home's serve lock, retiring whichever `serve` holds it.

    Free, it is taken with no line. Left by a pid that is no longer running, it is
    taken with one line. Held, the holder is asked to stop the way a supervisor
    retire asks (the supervisor socket's `shutdown`, when that serve owns the
    supervisor: its listener stops, its workers are recorded stopped with their
    sessions kept, and its tickets are left for the rounds' reclaim), then sent
    SIGTERM — `serve`'s own orderly stop — then SIGKILL, each with a bounded wait.
    Between every step this tries to take the lock rather than merely looking at
    it, so two starts racing each other end with exactly one holder. A holder that
    survives all of it leaves ``lock`` None and a one-sentence ``line``.

    ``timeout`` is ``supervisor.stop_timeout``: what an orderly stop may take.
    ``own_pid`` is for tests, whose holder is often the test process itself: this
    never signals its own pid.
    """
    identity = identity or {}
    own = os.getpid() if own_pid is None else own_pid

    def taken(result: tuple[int, int | None]) -> ServeLock:
        _write_serve_record(home, identity)
        return ServeLock(home, result[0], os.getpid())

    first = _try_serve_lock(home)
    if first is not None:
        left = first[1]
        if left and left != os.getpid() and not pid_alive(left):
            stale = runtime_label(read_serve_record_of(home, left))
            return ServeTaken(
                taken(first),
                f"took the serve lock from pid {left} ({stale}), which is no longer running",
            )
        return ServeTaken(taken(first))

    how: list[str] = []
    got: tuple[int, int | None] | None = None

    def wait(seconds: float) -> bool:
        nonlocal got
        deadline = clock() + seconds
        while True:
            got = _try_serve_lock(home)
            if got is not None:
                return True
            if clock() >= deadline:
                return False
            sleep(0.1)

    # A holder that has only just taken the lock may not have written its pid yet.
    pid = serve_holder_pid(home)
    if pid is None and not wait(min(grace, 2.0)):
        pid = serve_holder_pid(home)
    record = read_serve_record_of(home, pid) if pid else None
    if got is None:
        supervisor = read_record(home) or {}
        asked = False
        if pid is not None and supervisor.get("pid") == pid:
            asked = shutdown(supervisor.get("socket") or default_socket_path(home))
            if asked:
                how.append("asked it to stop")
        if not (asked and wait(timeout + 5.0)):
            for sig, name, seconds in (
                (signal.SIGTERM, "SIGTERM", grace if asked else timeout + 5.0),
                (signal.SIGKILL, "SIGKILL", grace),
            ):
                if pid is None or pid == own:
                    wait(grace)
                    break
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    kill(pid, sig)
                how.append(f"sent {name}")
                if wait(seconds):
                    break
    who = runtime_label(record)
    if got is None:
        named = f"pid {pid}" if pid else "a process this runtime cannot name"
        return ServeTaken(
            None,
            f"cannot start: {who} ({named}) still holds {serve_lock_path(home)} after "
            + (", ".join(how) or "waiting for it to let go")
            + "; stop it (`kill -9 "
            + (str(pid) if pid else "<pid>")
            + "`) and start `ppy serve` again",
            retired_pid=pid,
        )
    line = f"Took over from {who}" + (f" (pid {pid})." if pid else ".")
    if "sent SIGKILL" in how:
        line = line[:-1] + "; it did not stop until SIGKILL."
    return ServeTaken(taken(got), line, retired_pid=pid)


def read_serve_record_of(home: str, pid: int) -> dict[str, Any] | None:
    """``serve.json`` if it is ``pid``'s."""
    record = read_serve_record(home)
    return record if record is not None and record.get("pid") == pid else None


# ── when it cannot start ───────────────────────────────────────────────────


def record_start_failure(home: str, line: str, steps: list[str] | None = None) -> None:
    """Keep the sentence for readiness and put it in the blockers ledger (`blockers.py`).

    Readiness turns the record into a problem with steps, so the next `serve` that
    does start reports it to the owner through the app card and their DM, and says
    once that it cleared. The ledger entry is written here as well, in the ledger's
    own shape, so `ppy blockers` shows it while nothing is running to observe it.
    """
    steps = list(steps or []) + [AFTER]
    record = {"line": line, "steps": steps, "at": _now()}
    os.makedirs(os.path.join(home, "run"), exist_ok=True)
    _write_json(start_failure_path(home), record)
    fingerprint = hashlib.sha256(f"{START_FAILURE_CODE}:".encode()).hexdigest()[:16]
    ledger_file = os.path.join(home, "blockers.json")
    try:
        with open(ledger_file, encoding="utf-8") as fh:
            ledger = json.load(fh)
        if not isinstance(ledger, dict):
            ledger = {}
    except (OSError, ValueError):
        ledger = {}
    opened = ledger.setdefault("open", {})
    ledger.setdefault("cleared", {})
    ledger.setdefault("commented", {})
    existing = opened.get(fingerprint) or {}
    opened[fingerprint] = {
        "fingerprint": fingerprint,
        "code": START_FAILURE_CODE,
        "title": title_for(line),
        "steps": steps,
        "first_seen": existing.get("first_seen") or record["at"],
        "last_seen": record["at"],
        "scope": "",
        "repos": [],
        "reported_at": existing.get("reported_at"),
        "reported_steps": existing.get("reported_steps"),
        "cleared_at": None,
    }
    _write_json(ledger_file, ledger)


def title_for(line: str) -> str:
    return f"`ppy serve` could not start on this machine: {line}"


def start_failure(home: str) -> dict[str, Any] | None:
    try:
        with open(start_failure_path(home), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("line") else None


def clear_start_failure(home: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(start_failure_path(home))


def _write_json(path: str, data: Any) -> None:
    temp = f"{path}.{os.getpid()}.tmp"
    with open(temp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(temp, path)


def stop_timeout(home: str) -> float:
    """`supervisor.stop_timeout` from the instance config, read without the runtime's loader."""
    path = os.path.join(home, "config.toml")
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return float(DEFAULT_STOP_TIMEOUT)
    try:
        import tomllib  # 3.11+

        value = tomllib.loads(raw.decode("utf-8")).get("supervisor", {}).get("stop_timeout")
    except ImportError:
        value = _stop_timeout_by_hand(raw.decode("utf-8", "replace"))
    except ValueError:
        value = None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return float(DEFAULT_STOP_TIMEOUT)
    return float(value)


def _stop_timeout_by_hand(text: str) -> int | None:
    section = ""
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.strip("[] ")
        elif section == "supervisor" and stripped.startswith("stop_timeout"):
            value = stripped.split("=", 1)[-1].strip()
            return int(value) if value.isdigit() else None
    return None


# ── the launcher's step ───────────────────────────────────────────────────


def take_over(
    home: str,
    root: str,
    *,
    environment_ok: bool = True,
    stderr=None,
    **seams: Any,
) -> int:
    """Decide and act; one line for anything but a free lock. Returns an exit status."""
    stderr = stderr or sys.stderr
    build = checkout_build(root)
    holder = inspect(home)
    decision = decide(holder, build, environment_ok=environment_ok)
    if decision == FREE:
        return EXIT_START
    if decision == STALE:
        clear_stale(home, holder.dead_pids)
        print(f"ppy serve: {stale_line(holder.dead_pids)}", file=stderr)
        return EXIT_START
    if decision == ADOPT:
        return EXIT_ADOPTED
    retired = retire(home, holder, build, timeout=stop_timeout(home), **seams)
    print(f"ppy serve: {retired.line}", file=stderr)
    if not retired.ok:
        record_start_failure(
            home,
            retired.line,
            [f"kill {holder.pid}" if holder.pid else "ppy supervisor stop"],
        )
        return EXIT_CANNOT_START
    return EXIT_START


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ppy serve (takeover)")
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--environment-broken",
        action="store_true",
        help="the environment must be rebuilt, so no running supervisor may be kept",
    )
    args = parser.parse_args(argv)
    return take_over(
        home_dir(), os.path.abspath(args.project), environment_ok=not args.environment_broken
    )


if __name__ == "__main__":
    sys.exit(main())
