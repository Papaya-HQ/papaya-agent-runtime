"""Unix-domain-socket transport for the supervisor.

A thin request/response server around :class:`Supervisor`. Each connection is
handled in its own thread so a blocking ``wait_actionable`` never stalls other
control requests. Startable in a background thread (tests) or blocking (CLI).

One supervisor per ``PPY_HOME`` is enforced with an exclusive OS lock, not with
a probe. The probe — connect to the socket, refuse if something answers — was
the only guard until issue #69, and it protects nothing during a simultaneous
start: two processes can both find no listener, both unlink the entry, both
bind, and the second one's ``unlink`` takes the first one's socket with it.
Two live supervisors then admit workers independently, and whichever stops
first deletes the other's socket and pid file. So :meth:`_bind` first takes
``flock(LOCK_EX | LOCK_NB)`` on ``<PPY_HOME>/run/supervisor.lock`` and holds it
until the server stops. The loser sees ``EWOULDBLOCK`` and refuses without
having touched the socket or the pid file. A crashed owner's lock dies with
its process, so its stale files are replaceable by the next start — which is
also the only start allowed to remove them.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import socket
import tempfile
import threading
import time

from papaya_agent_runtime.paths import ensure_layout, ppy_home, run_dir
from papaya_agent_runtime.supervisor.core import Supervisor, SupervisorError
from papaya_agent_runtime.supervisor.protocol import encode, read_request


class SupervisorOwned(RuntimeError):
    """Another process owns this ``PPY_HOME``'s supervisor; nothing was changed."""


def owner_lock_path() -> str:
    """Where the owner lock for this ``PPY_HOME`` lives — one per canonical home."""
    return str(ppy_home().resolve() / "run" / "supervisor.lock")


def default_socket_path() -> str:
    # AF_UNIX paths are limited to ~104 bytes on macOS, so a deep .ppy path under a
    # temp dir overflows. Derive a short, stable path from the .ppy home instead.
    key = hashlib.sha1(str(ppy_home().resolve()).encode()).hexdigest()[:10]
    base = "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()
    return os.path.join(base, f"ppy-{key}.sock")


#: How often the serving loop polls worker health and prepares due assessments.
TICK_SECONDS = 60


class SupervisorServer:
    def __init__(self, socket_path: str | None = None) -> None:
        ensure_layout()
        self.socket_path = socket_path or default_socket_path()
        self.supervisor = Supervisor()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        #: The open owner lock; held from a successful :meth:`_bind` to cleanup.
        self._lock_fd: int | None = None
        # None means "never ticked", which is not the same as "ticked at zero".
        # `time.monotonic()` is time since boot on Linux, so a 0.0 sentinel made the
        # first minute of a machine's uptime look like a tick that had just happened:
        # on a freshly booted CI runner neither poll ran at all, silently. Found by
        # CI on 2026-09-15, where it looked like a flaky test.
        self._last_assessment_tick: float | None = None
        self._last_health_tick: float | None = None
        self._quiet_flagged: set[int] = set()
        self._plan_flagged: set[int] = set()

    def start_background(self) -> None:
        self._bind()
        self._thread = threading.Thread(target=self._serve_loop, daemon=True)
        self._thread.start()

    def serve_forever(self) -> None:
        self._bind()
        self._serve_loop()

    def _acquire_owner_lock(self) -> None:
        """Become the one supervisor for this ``PPY_HOME``, or refuse having touched nothing."""
        path = owner_lock_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            owner = self._recorded_owner_pid()
            os.close(fd)
            raise SupervisorOwned(
                f"a supervisor already owns {ppy_home()}"
                + (f" (pid {owner})" if owner else "")
                + "; one supervisor per PPY_HOME is supported — stop that one first"
            ) from exc
        # The lock is ours; the pid inside is advisory, for the loser's message.
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._lock_fd = fd

    @staticmethod
    def _recorded_owner_pid() -> int | None:
        try:
            with open(owner_lock_path(), encoding="utf-8") as fh:
                return int(fh.read().strip() or 0) or None
        except (OSError, ValueError):
            return None

    def _bind(self) -> None:
        self._acquire_owner_lock()
        try:
            self._bind_socket()
        except BaseException:
            self._release_owner_lock()
            raise

    def _bind_socket(self) -> None:
        if os.path.exists(self.socket_path):
            # Holding the lock, an existing entry is a crashed owner's — unless a
            # supervisor from before the lock existed is still serving on it, in
            # which case refuse rather than pull its socket out from under it.
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            try:
                probe.connect(self.socket_path)
            except OSError:
                os.unlink(self.socket_path)
            else:
                raise SupervisorOwned(
                    f"a supervisor is already listening at {self.socket_path}; "
                    "one supervisor per PPY_HOME is supported"
                )
            finally:
                probe.close()
        os.makedirs(os.path.dirname(self.socket_path), exist_ok=True)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.socket_path)
        self._sock.listen(16)
        self._sock.settimeout(0.5)
        # Persist the listening pid for `ppy supervisor status` / reconcile.
        with open(run_dir() / "supervisor.pid", "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))

    def _release_owner_lock(self) -> None:
        fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)  # closing drops the flock

    def _serve_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            self._tick_assessments()
            self._tick_health()
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        self._cleanup()

    def _tick_assessments(self) -> None:
        """Prepare due reviews while the supervisor is alive, without a model call."""
        now = time.monotonic()
        if (
            self._last_assessment_tick is not None
            and now - self._last_assessment_tick < TICK_SECONDS
        ):
            return
        self._last_assessment_tick = now
        try:
            from papaya_agent_runtime.assessments import ensure_due

            ensure_due()
        except Exception:  # noqa: BLE001 - maintenance must never take down dispatch
            return

    def _tick_health(self) -> None:
        """Poll in-flight workers; flag the ones we haven't heard from in a while."""
        now = time.monotonic()
        if self._last_health_tick is not None and now - self._last_health_tick < TICK_SECONDS:
            return
        self._last_health_tick = now
        try:
            from papaya_agent_runtime import health
            from papaya_agent_runtime.state import init_db

            conn = init_db()
            health.flag_quiet_workers(
                conn,
                quiet_after=health.quiet_threshold(),
                already_flagged=self._quiet_flagged,
            )
            health.flag_missing_plans(
                conn, grace=health.plan_grace(), already_flagged=self._plan_flagged
            )
            # A continuation a refusal left pending has no other trigger after a
            # restart than this tick; between restarts it is a cheap retry.
            self.supervisor.retry_deferred_continuations()
        except Exception:  # noqa: BLE001 - maintenance must never take down dispatch
            return

    def _handle(self, conn: socket.socket) -> None:
        with conn, conn.makefile("r") as rf:
            try:
                request = read_request(rf)
            except (ValueError, OSError):
                conn.sendall(encode({"ok": False, "error": "bad request"}))
                return
            if not request:
                return
            response = self._dispatch(request)
            with contextlib.suppress(OSError):
                conn.sendall(encode(response))

    def _dispatch(self, request: dict) -> dict:
        cmd = request.get("cmd")
        sup = self.supervisor
        try:
            if cmd == "ping":
                return {"ok": True, "pid": os.getpid()}
            if cmd == "dispatch_task":
                return {
                    "ok": True,
                    **sup.dispatch_task(
                        repo=request["repo"],
                        title=request["title"],
                        instructions=request.get("instructions", ""),
                        # No provider named: the supervisor resolves the configured
                        # one. Never defaulted here, and never to `fake`.
                        provider=request.get("provider"),
                        run_id=request.get("run_id"),
                        model=request.get("model"),
                        reasoning=request.get("reasoning"),
                        base=request.get("base"),
                        stack_on=request.get("stack_on"),
                        ends_at=request.get("ends_at") or "done",
                        papaya_event_key=request.get("papaya_event_key"),
                        papaya_event_metadata=request.get("papaya_event_metadata"),
                    ),
                }
            if cmd == "task_status":
                return {"ok": True, "task": sup.task_status(int(request["task_id"]))}
            if cmd == "run_status":
                return {"ok": True, **sup.run_status(int(request["run_id"]))}
            if cmd == "wait_actionable":
                return {
                    "ok": True,
                    **sup.wait_actionable(
                        int(request["run_id"]),
                        float(request.get("timeout", 30.0)),
                        int(request.get("after_seq", 0) or 0),
                    ),
                }
            if cmd == "resume_task":
                return {
                    "ok": True,
                    **sup.resume_task(
                        int(request["task_id"]),
                        request.get("message"),
                        ends_at=request.get("ends_at"),
                    ),
                }
            if cmd == "steer_task":
                return {
                    "ok": True,
                    **sup.steer_task(
                        int(request["task_id"]),
                        request["message"],
                        delivery=request.get("delivery") or "append",
                    ),
                }
            if cmd == "answer_question":
                return {
                    "ok": True,
                    **sup.answer_question(
                        int(request["task_id"]),
                        request["answer"],
                        scope=request.get("scope", "run"),
                        rationale=request.get("rationale"),
                    ),
                }
            if cmd == "reconcile":
                return {"ok": True, **sup.reconcile()}
            if cmd == "shutdown":
                sup.shutdown()
                self._stop.set()
                return {"ok": True, "shutdown": True}
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        except (SupervisorError, KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._cleanup()

    def _cleanup(self) -> None:
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
        # Only the owner removes the socket and pid file, and only while it still
        # holds the lock — a stop that lost the race must not delete the winner's.
        if self._lock_fd is not None:
            for path in (self.socket_path, str(run_dir() / "supervisor.pid")):
                if os.path.exists(path):
                    with contextlib.suppress(OSError):
                        os.unlink(path)
        self._release_owner_lock()
