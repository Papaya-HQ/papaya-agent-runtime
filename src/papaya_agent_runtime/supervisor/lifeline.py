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

The watcher can itself be lost — killed by hand, by a memory reaper, by a crash —
and then the guarantee is gone with nothing said. So the owner checks it on its
own clock (:func:`keep_alive`: every manager round in `ppy serve`, every supervisor
tick): a watcher that has exited is started again and handed every live runner's
process group from the runner rows, and a watcher that cannot be started is
recorded as a deficiency. ``<PPY_HOME>/run/lifeline.json`` says what the owner last
knew, so `ppy health` in another process can show it. Two gaps remain, noted rather
than closed: a manager turn's pid and a gate's group are registered only by the
code that started them and are in no runner row, so a restarted watcher does not
answer for them; and a worker registers only after its process has started.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

#: How long the watcher gives SIGTERM before SIGKILL.
TERM_GRACE_SECONDS = 5.0

#: What :func:`check` found.
ALIVE = "alive"
RESTARTED = "restarted"
MISSING = "missing"
#: This process never started a watcher, so there is nothing of its own to check.
NOT_OWNED = "not owned"

_lock = threading.Lock()
_watcher: subprocess.Popen | None = None
#: Whether this process has ever tried to start a watcher: only then is one its to keep.
_owner = False
_restarts = 0
_last_restart_at: str | None = None
_last_error = ""


def _command() -> list[str]:
    """The watcher's argv. A test seam: a fake watcher that records what it is sent."""
    return [sys.executable, "-m", "papaya_agent_runtime.supervisor.lifeline"]


def start() -> bool:
    """Start this process's watcher once. False when it could not be started."""
    with _lock:
        return _start_locked()


def _start_locked() -> bool:
    global _watcher, _owner, _last_error
    _owner = True
    if _watcher is not None and _watcher.poll() is None:
        return True
    import papaya_agent_runtime

    src = os.path.dirname(os.path.dirname(papaya_agent_runtime.__file__))
    env = dict(os.environ)
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        _watcher = subprocess.Popen(  # noqa: S603 - fixed interpreter/module argv
            _command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        _watcher = None
        _last_error = str(exc) or exc.__class__.__name__
        _write_state()
        return False
    _last_error = ""
    _write_state()
    return True


def start_or_record(where: str) -> bool:
    """:func:`start`, and a deficiency when it fails: cleanup must not vanish unsaid."""
    if start():
        return True
    _record(f"{where} could not start its lifeline watcher", where)
    return False


@dataclass(frozen=True)
class WatcherCheck:
    status: str
    #: The process groups handed to a restarted watcher.
    groups: list[int] = field(default_factory=list)

    def line(self) -> str:
        if self.status == RESTARTED:
            return (
                "the lifeline watcher had exited; restarted it and re-registered "
                f"{len(self.groups)} worker process group(s)"
            )
        if self.status == MISSING:
            return "the lifeline watcher had exited and could not be restarted"
        return ""


def live_runner_groups() -> list[int]:
    """Every live runner's process group: its pid, since a worker leads its own group."""
    from papaya_agent_runtime.state import init_db, store

    conn = init_db()
    try:
        pids = [row["pid"] for row in store.live_runners(conn)]
    finally:
        conn.close()
    return sorted({int(pid) for pid in pids if pid and _alive("g", int(pid))})


def check(*, groups: Callable[[], Iterable[int]] | None = None) -> WatcherCheck:
    """Is this process's watcher running? Restart it, and re-register groups, if not."""
    global _restarts, _last_restart_at
    with _lock:
        if not _owner:
            return WatcherCheck(NOT_OWNED)
        if _watcher is not None and _watcher.poll() is None:
            return WatcherCheck(ALIVE)
        if not _start_locked():
            return WatcherCheck(MISSING)
        _restarts += 1
        _last_restart_at = _now()
        _write_state()
    try:
        found = sorted(set((groups or live_runner_groups)()))
    except Exception:  # noqa: BLE001 - a restarted watcher is better than none
        found = []
    for pgid in found:
        watch_group(pgid)
    return WatcherCheck(RESTARTED, found)


def keep_alive(where: str, *, groups: Callable[[], Iterable[int]] | None = None) -> WatcherCheck:
    """:func:`check`, recording a deficiency for a watcher that had gone."""
    seen = check(groups=groups)
    if seen.status in (RESTARTED, MISSING):
        _record(
            f"{where} found its lifeline watcher exited"
            + ("; it could not be restarted" if seen.status == MISSING else "; restarted it"),
            where,
        )
    return seen


def _record(detail: str, where: str) -> None:
    from papaya_agent_runtime import deficiencies

    deficiencies.record(
        deficiencies.LIFELINE_DOWN, detail, evidence={"where": where, "error": _last_error}
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def state_path() -> str:
    from papaya_agent_runtime.paths import run_dir

    return str(run_dir() / "lifeline.json")


def _write_state() -> None:
    """What the owner knows about its watcher, for `ppy health` in another process."""
    watcher = _watcher
    data = {
        "owner_pid": os.getpid(),
        "pid": watcher.pid if watcher is not None else None,
        "restarts": _restarts,
        "last_restart_at": _last_restart_at,
        "error": _last_error,
        "at": _now(),
    }
    with contextlib.suppress(OSError):
        path = state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp = f"{path}.{os.getpid()}.tmp"
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(temp, path)


def status() -> dict:
    """The watcher as the owner last recorded it: alive, restarted, missing or no owner."""
    try:
        with open(state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = None
    owner = data.get("owner_pid") if isinstance(data, dict) else None
    if not isinstance(owner, int) or owner <= 1 or not _alive("p", owner):
        return {"status": "no owner", "pid": None, "restarts": 0}
    pid = data.get("pid")
    restarts = int(data.get("restarts") or 0)
    if not isinstance(pid, int) or pid <= 1 or not _alive("p", pid):
        state = MISSING
    else:
        state = RESTARTED if restarts else ALIVE
    return {
        "status": state,
        "pid": pid,
        "owner_pid": data.get("owner_pid"),
        "restarts": restarts,
        "last_restart_at": data.get("last_restart_at"),
        "error": data.get("error") or "",
    }


def describe(seen: dict) -> str:
    """One line for `ppy health`."""
    state = seen.get("status")
    if state == ALIVE:
        return (
            f"lifeline watcher: alive (pid {seen['pid']}, for supervisor pid {seen['owner_pid']})"
        )
    if state == RESTARTED:
        return (
            f"lifeline watcher: restarted {seen['restarts']} time(s), last at "
            f"{seen['last_restart_at']}; alive now (pid {seen['pid']})"
        )
    if state == MISSING:
        why = f": {seen['error']}" if seen.get("error") else ""
        return (
            f"lifeline watcher: MISSING under supervisor pid {seen['owner_pid']}{why} — its "
            "workers would outlive an abrupt supervisor death"
        )
    return "lifeline watcher: none (no supervisor that started one is running)"


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
    global _watcher, _owner, _restarts, _last_restart_at, _last_error
    with _lock:
        watcher, _watcher = _watcher, None
        _owner, _restarts, _last_restart_at, _last_error = False, 0, None, ""
        with contextlib.suppress(OSError, ValueError):
            with open(state_path(), encoding="utf-8") as fh:
                mine = json.load(fh).get("owner_pid") == os.getpid()
            if mine:
                os.unlink(state_path())
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
