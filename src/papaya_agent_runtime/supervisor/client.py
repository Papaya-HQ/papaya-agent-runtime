"""Client for the supervisor Unix socket."""

from __future__ import annotations

import socket

from papaya_agent_runtime.supervisor.protocol import send_request
from papaya_agent_runtime.supervisor.server import default_socket_path


class SupervisorUnavailable(Exception):
    pass


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
        self, task_id: int, message: str | None = None, ends_at: str | None = None
    ) -> dict:
        return self._call(
            {"cmd": "resume_task", "task_id": task_id, "message": message, "ends_at": ends_at}
        )

    def steer_task(self, task_id: int, message: str, delivery: str = "append") -> dict:
        return self._call(
            {"cmd": "steer_task", "task_id": task_id, "message": message, "delivery": delivery}
        )

    def answer_question(
        self, task_id: int, answer: str, scope: str = "run", rationale: str | None = None
    ) -> dict:
        return self._call(
            {
                "cmd": "answer_question",
                "task_id": task_id,
                "answer": answer,
                "scope": scope,
                "rationale": rationale,
            }
        )

    def reconcile(self) -> dict:
        return self._call({"cmd": "reconcile"})

    def shutdown(self) -> dict:
        return self._call({"cmd": "shutdown"})
