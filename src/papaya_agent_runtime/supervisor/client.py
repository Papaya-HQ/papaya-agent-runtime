"""Client for the supervisor Unix socket."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
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
    log_path = supervisor_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    import papaya_agent_runtime

    # `python -m papaya_agent_runtime` needs the package's `src` on the path, which
    # `bin/ppy` sets for itself and nothing guarantees for this child.
    src = os.path.dirname(os.path.dirname(papaya_agent_runtime.__file__))
    env = dict(os.environ)
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    with log_path.open("ab") as log:
        # A session of its own, no terminal and no parent a harness tracks: a harness
        # reclaiming its background tasks, or ending with its session, cannot reach it.
        subprocess.Popen(  # noqa: S603 - fixed interpreter/module argv
            [sys.executable, "-m", "papaya_agent_runtime", "supervisor", "serve"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
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


def supervisor_log_path() -> Path:
    return run_dir() / "supervisor.log"


#: How long `ppy resume` and an interrupting `ppy steer` wait to see the worker.
DEFAULT_VERIFY_SECONDS = 20.0

#: Task statuses that mean the resumed worker already ran and recorded its end.
_FINISHED = ("worker_done", "worker_stopped", "blocked", "failed", "delivered", "needs_recovery")


@dataclass(frozen=True)
class Liveness:
    """What a resume came to: ``alive``, ``finished`` or ``missing`` (an incident)."""

    verdict: str
    task_status: str | None
    pid: int | None
    runner_status: str | None

    def describe(self, task_id: int) -> str:
        if self.verdict == "alive":
            return f"task {task_id}: worker alive (pid {self.pid})"
        if self.verdict == "finished":
            return f"task {task_id}: worker already finished — status {self.task_status}"
        return (
            f"INCIDENT: task {task_id}: no live worker process seen after the resume (task "
            f"status {self.task_status}, runner {self.runner_status or 'none'}); run "
            "`ppy health` and check the supervisor log before resuming again"
        )


def verify_worker(
    task_id: int,
    *,
    since: str,
    timeout: float = DEFAULT_VERIFY_SECONDS,
    poll: float = 0.1,
) -> Liveness:
    """Wait up to ``timeout`` for the task's newest runner to have a live pid, or a result.

    Only a runner row started at or after ``since`` (an ISO UTC stamp taken before the
    request) counts: the row of the session a resume replaces says nothing about it.
    """
    from papaya_agent_runtime.state import init_db, store
    from papaya_agent_runtime.supervisor.dead_runners import pid_alive

    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        conn = init_db()
        try:
            task = store.get_task(conn, task_id)
            row = conn.execute(
                "SELECT * FROM runners WHERE task_id = ? AND started_at >= ? "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (task_id, since),
            ).fetchone()
        finally:
            conn.close()
        status = task["status"] if task is not None else None
        pid = row["pid"] if row is not None else None
        runner_status = row["status"] if row is not None else None
        if row is not None and runner_status in ("starting", "running") and pid_alive(pid):
            return Liveness("alive", status, pid, runner_status)
        if row is not None and row["result_recorded"] and status in _FINISHED:
            return Liveness("finished", status, pid, runner_status)
        if time.monotonic() >= deadline:
            return Liveness("missing", status, pid, runner_status)
        time.sleep(poll)


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
                f"no supervisor at {self.socket_path} (start with `ppy supervisor start`)"
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
