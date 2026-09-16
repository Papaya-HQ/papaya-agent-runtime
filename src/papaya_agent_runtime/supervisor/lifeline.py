"""What a supervisor started dies with it, however the supervisor dies.

An orderly stop of `ppy serve` stops its workers itself (`SupervisorServer.close`).
An abrupt one — the app crashing, `kill -9` — runs no Python at all, and a worker is
not a child the kernel takes down with its parent: every worker, gate and turn runs
in a process group of its own, so an interrupt reaches the tools it started and
never the supervisor (or the desktop app whose group `serve` may share). On
2026-09-16 exactly that kind of survivor held the supervisor lock for a build that
no longer existed.

So the owner of a supervisor starts one small watcher, in a session of its own so a
signal to the owner's group cannot take it first. It reads, on a pipe only the owner
holds, which process groups and pids to answer for. When that pipe reaches end of
file — the owner exited, by any route — it sends SIGTERM to everything still listed,
waits a few seconds, and SIGKILLs what is left. An orderly stop has already stopped
and released everything, so the watcher finds nothing and exits.

The watcher is standard-library only and knows nothing else about the runtime.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time

#: How long the watcher gives SIGTERM before SIGKILL.
TERM_GRACE_SECONDS = 5.0

_lock = threading.Lock()
_watcher: subprocess.Popen | None = None


def start() -> bool:
    """Start this process's watcher once. False when it could not be started."""
    global _watcher
    with _lock:
        if _watcher is not None and _watcher.poll() is None:
            return True
        import papaya_agent_runtime

        src = os.path.dirname(os.path.dirname(papaya_agent_runtime.__file__))
        env = dict(os.environ)
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        try:
            _watcher = subprocess.Popen(  # noqa: S603 - fixed interpreter/module argv
                [sys.executable, "-m", "papaya_agent_runtime.supervisor.lifeline"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                text=True,
                start_new_session=True,
            )
        except OSError:
            _watcher = None
            return False
        return True


def _send(line: str) -> None:
    with _lock:
        watcher = _watcher
        if watcher is None or watcher.stdin is None:
            return
        with contextlib.suppress(OSError, ValueError):
            watcher.stdin.write(line + "\n")
            watcher.stdin.flush()


def watch_group(pgid: int) -> None:
    """Answer for this process group if the owner dies. A no-op without a watcher."""
    _send(f"+g {int(pgid)}")


def release_group(pgid: int) -> None:
    _send(f"-g {int(pgid)}")


def watch_pid(pid: int) -> None:
    """Answer for one process (one that shares the owner's group) if the owner dies."""
    _send(f"+p {int(pid)}")


def release_pid(pid: int) -> None:
    _send(f"-p {int(pid)}")


def stop(timeout: float = TERM_GRACE_SECONDS + 2.0) -> None:
    """Close the pipe as an orderly exit would, and wait for the watcher to finish."""
    global _watcher
    with _lock:
        watcher, _watcher = _watcher, None
    if watcher is None:
        return
    with contextlib.suppress(OSError, ValueError):
        if watcher.stdin is not None:
            watcher.stdin.close()
    try:
        watcher.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        watcher.kill()


# ── the watcher ─────────────────────────────────────────────────────────


def _alive(kind: str, target: int) -> bool:
    try:
        if kind == "g":
            os.killpg(target, 0)
        else:
            os.kill(target, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal(kind: str, target: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if kind == "g":
            os.killpg(target, sig)
        else:
            os.kill(target, sig)


def watch(stream, *, grace: float = TERM_GRACE_SECONDS) -> set[tuple[str, int]]:
    """Read registrations until end of file, then stop what is still registered."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    listed: set[tuple[str, int]] = set()
    for raw in stream:
        parts = raw.split()
        if len(parts) != 2 or parts[0][:1] not in "+-" or parts[0][1:] not in ("g", "p"):
            continue
        try:
            entry = (parts[0][1:], int(parts[1]))
        except ValueError:
            continue
        if entry[1] <= 1 or entry == ("g", os.getpgrp()):
            continue
        if parts[0][0] == "+":
            listed.add(entry)
        else:
            listed.discard(entry)
    left = {entry for entry in listed if _alive(*entry)}
    for kind, target in left:
        _signal(kind, target, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while left and time.monotonic() < deadline:
        time.sleep(0.1)
        left = {entry for entry in left if _alive(*entry)}
    for kind, target in left:
        _signal(kind, target, signal.SIGKILL)
    return listed


if __name__ == "__main__":
    watch(sys.stdin)
