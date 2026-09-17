"""Client for the supervisor Unix socket."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from papaya_agent_runtime.paths import ensure_layout, run_dir
from papaya_agent_runtime.supervisor.protocol import send_request
from papaya_agent_runtime.supervisor.server import default_socket_path


class SupervisorUnavailable(Exception):
    pass


def ensure_supervisor(*, timeout: float = 5.0) -> tuple[SupervisorClient, bool]:
    """Return a live client, starting the instance supervisor if necessary.

    The server's owner lock makes concurrent launches safe: only one process can
    own the instance, and every caller waits for the same socket to answer.
    ``bool`` is true when this call launched a process, even if another launch
    won the owner race.
    """
    client = SupervisorClient()
    try:
        client.ping()
    except SupervisorUnavailable:
        pass
    else:
        return client, False

    ensure_layout()
    log_path = run_dir() / "supervisor.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        subprocess.Popen(  # noqa: S603 - fixed interpreter/module argv
            [sys.executable, "-m", "papaya_agent_runtime", "supervisor", "serve"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=dict(os.environ),
            start_new_session=True,
            close_fds=True,
        )

    deadline = time.monotonic() + max(timeout, 0.1)
    while time.monotonic() < deadline:
        try:
            client.ping()
        except SupervisorUnavailable:
            time.sleep(0.05)
            continue
        return client, True
    raise SupervisorUnavailable(
        f"could not start the supervisor within {timeout:g}s; see {Path(log_path)}"
    )


def _by(by: str | None) -> dict:
    """Who is asking, on the wire only when somebody said: an older server takes no `by`."""
    return {"by": by} if by else {}


class SupervisorClient:
    def __init__(self, socket_path: str | None = None) -> None:
        self.socket_path = socket_path or default_socket_path()

    def _call(self, request: dict, timeout: float = 60.0) -> dict:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(self.socket_path)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            raise SupervisorUnavailable(
                f"no supervisor at {self.socket_path} (start with `ppy supervisor serve`)"
            ) from exc
        with sock:
            return send_request(sock, request)

    def ping(self) -> dict:
        return self._call({"cmd": "ping"})

    def dispatch_task(self, **kwargs) -> dict:
        return self._call({"cmd": "dispatch_task", **kwargs})

    def task_status(self, task_id: int) -> dict:
        return self._call({"cmd": "task_status", "task_id": task_id})

    def run_status(self, run_id: int) -> dict:
        return self._call({"cmd": "run_status", "run_id": run_id})

    def wait_actionable(self, run_id: int, timeout: float = 30.0, after_seq: int = 0) -> dict:
        return self._call(
            {
                "cmd": "wait_actionable",
                "run_id": run_id,
                "timeout": timeout,
                "after_seq": after_seq,
            },
            timeout=timeout + 10,
        )

    def resume_task(
        self,
        task_id: int,
        message: str | None = None,
        ends_at: str | None = None,
        by: str | None = None,
    ) -> dict:
        return self._call(
            {
                "cmd": "resume_task",
                "task_id": task_id,
                "message": message,
                "ends_at": ends_at,
                **_by(by),
            }
        )

    def steer_task(
        self, task_id: int, message: str, delivery: str = "append", by: str | None = None
    ) -> dict:
        return self._call(
            {
                "cmd": "steer_task",
                "task_id": task_id,
                "message": message,
                "delivery": delivery,
                **_by(by),
            }
        )

    def answer_question(
        self,
        task_id: int,
        answer: str,
        scope: str = "run",
        rationale: str | None = None,
        by: str | None = None,
    ) -> dict:
        return self._call(
            {
                "cmd": "answer_question",
                "task_id": task_id,
                "answer": answer,
                "scope": scope,
                "rationale": rationale,
                **_by(by),
            }
        )

    def reconcile(self) -> dict:
        return self._call({"cmd": "reconcile"})

    def sweep(self, include_declined: bool = False, timeout: float = 130.0) -> dict:
        return self._call({"cmd": "sweep", "include_declined": include_declined}, timeout=timeout)

    def gate_start(
        self, *, task_id: int | None, repo: str | None, full: bool, baseline: str | None = None
    ) -> dict:
        request = {"cmd": "gate_start", "task_id": task_id, "repo": repo, "full": full}
        if baseline:
            request["baseline"] = baseline
        return self._call(request)

    def gate_wait(self, key: str, timeout: float = 60.0) -> dict:
        return self._call(
            {"cmd": "gate_wait", "key": key, "timeout": timeout}, timeout=timeout + 10
        )

    def shutdown(self) -> dict:
        return self._call({"cmd": "shutdown"})
