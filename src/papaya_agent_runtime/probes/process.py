"""Subprocess helpers for live probes.

Spawns a provider CLI in its own process group so the probe can deliver a
signal to the whole group (the CLI plus any tool child), stream structured
output line by line, optionally interrupt at a detected boundary, and enforce a
grace period before escalating to SIGKILL.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class StreamResult:
    argv: list[str]
    exit_code: int | None
    timed_out: bool
    interrupted: bool
    signal_sent: str | None
    lines: list[str] = field(default_factory=list)
    stderr: str = ""
    duration_s: float = 0.0
    interrupt_at_line: int | None = None

    @property
    def stdout(self) -> str:
        return "\n".join(self.lines)


def run_streaming(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str] | None = None,
    stdin_data: str | None = None,
    interrupt_predicate: Callable[[str], bool] | None = None,
    interrupt_after_s: float | None = None,
    interrupt_signal: signal.Signals = signal.SIGINT,
    interrupt_delay_s: float = 0.0,
    grace_s: float = 8.0,
    timeout_s: float = 120.0,
) -> StreamResult:
    """Run ``argv`` streaming stdout, optionally interrupting the process group.

    The process is interrupted when ``interrupt_predicate`` first returns True
    for an output line, or after ``interrupt_after_s`` seconds, whichever comes
    first. After the signal, the process gets ``grace_s`` to exit before a
    SIGKILL escalation. A run that never exits is SIGKILLed at ``timeout_s``.
    """

    full_env = {**os.environ, **(env or {})}
    start = time.monotonic()

    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=full_env,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,  # own process group for group-wide signals
    )

    if stdin_data is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_data)
            proc.stdin.flush()
            proc.stdin.close()
        except (BrokenPipeError, ValueError):
            pass

    lines: list[str] = []
    stderr_chunks: list[str] = []
    state: dict[str, object] = {
        "interrupted": False,
        "signal": None,
        "interrupt_at_line": None,
        "fired_at": None,
    }
    lock = threading.Lock()

    def send_interrupt(at_line: int | None) -> None:
        with lock:
            if state["interrupted"]:
                return
            state["interrupted"] = True
        if interrupt_delay_s:
            time.sleep(interrupt_delay_s)
        try:
            os.killpg(os.getpgid(proc.pid), interrupt_signal)
            state["signal"] = interrupt_signal.name
            state["interrupt_at_line"] = at_line
            state["fired_at"] = time.monotonic()
        except (ProcessLookupError, PermissionError):
            pass

    def read_stdout() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            if interrupt_predicate is not None and not state["interrupted"]:
                try:
                    if interrupt_predicate(line):
                        send_interrupt(len(lines) - 1)
                except Exception:  # noqa: BLE001 - predicate must never crash the reader
                    pass

    def read_stderr() -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            stderr_chunks.append(raw)

    t_out = threading.Thread(target=read_stdout, daemon=True)
    t_err = threading.Thread(target=read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    timer: threading.Timer | None = None
    if interrupt_after_s is not None:
        timer = threading.Timer(interrupt_after_s, lambda: send_interrupt(None))
        timer.daemon = True
        timer.start()

    timed_out = False
    deadline = start + timeout_s
    try:
        while True:
            if proc.poll() is not None:
                break
            now = time.monotonic()
            fired_at = state["fired_at"]
            if isinstance(fired_at, float) and now - fired_at > grace_s:
                _kill_group(proc, signal.SIGKILL)
                break
            if now > deadline:
                timed_out = True
                _kill_group(proc, signal.SIGKILL)
                break
            time.sleep(0.1)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_group(proc, signal.SIGKILL)
            proc.wait(timeout=5)
    finally:
        if timer is not None:
            timer.cancel()

    t_out.join(timeout=5)
    t_err.join(timeout=5)

    return StreamResult(
        argv=argv,
        exit_code=proc.returncode,
        timed_out=timed_out,
        interrupted=bool(state["interrupted"]),
        signal_sent=state["signal"] if isinstance(state["signal"], str) else None,
        lines=lines,
        stderr="".join(stderr_chunks),
        duration_s=time.monotonic() - start,
        interrupt_at_line=(
            state["interrupt_at_line"] if isinstance(state["interrupt_at_line"], int) else None
        ),
    )


def _kill_group(proc: subprocess.Popen, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), sig)


def capture(argv: list[str], *, cwd: str | None = None, timeout_s: float = 30.0) -> str:
    """Run a short command and return combined stdout (empty on failure)."""
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "") + (proc.stderr or "")
