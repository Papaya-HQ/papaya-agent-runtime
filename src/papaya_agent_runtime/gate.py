"""Run a repository's gate outside a harness tool call, and record what it said.

PAP-213 (2026-09-16): a worker ran the backend suite as a tool call. The harness caps a
tool call at ten minutes and moves anything longer to the background; the worker
ended its turn to "wait for it", the session ended, the harness killed the suite, and
the review turn then did the same thing twice. No wording in a prompt can make a
twelve-minute suite finish inside a ten-minute call.

So a gate is run by something that has no tool timeout: the supervisor. ``ppy gate
run`` asks the supervisor to start the repository's local gate (or its full suite,
``--full``) as the supervisor's own subprocess, then waits on it a minute at a time,
printing a progress line each time, and returns well inside a tool call. A gate still
going when it returns is still going: the same command again attaches to it rather
than starting another, and when it finishes its result is recorded whether or not
anyone is still waiting. With no supervisor answering, the gate runs in the calling
process, which is the same run with nobody else to outlive the caller.

The result lives in the ledger, as a ``gate_result`` event on the task, with the
command, how long it took, its summary line, its exit code and the head commit it ran
at. That is what :func:`verdict` reads and what ``ppy serve`` decides on: a worker whose
head has a green gate is reviewable, a red one is steered with the summary, and one
with none is steered to run the gate.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from papaya_agent_runtime import environment
from papaya_agent_runtime.state import init_db, store

#: The ledger event a finished gate leaves on its task.
GATE_RESULT = "gate_result"
#: The ledger event a gate leaves when it starts, so a run that never finished shows.
GATE_STARTED = "gate_started"
#: The ledger event a gate leaves when a signal ended it before it could finish.
GATE_KILLED = "gate_killed"

#: How often a waiting caller is told the gate is still going.
PROGRESS_SECONDS = 60
#: How often the runner checks whether the gate process has exited.
POLL_SECONDS = 1.0
#: How long one `ppy gate run` waits before handing back "still running": under the
#: harness's ten-minute tool cap with room to spare.
WAIT_SECONDS = 540
#: Exit status for "the gate is still running; call again" (EX_TEMPFAIL).
STILL_RUNNING = 75

GREEN = "green"
RED = "red"
NONE = "none"

#: The widest summary line kept on the record.
MAX_SUMMARY = 240

#: `run`'s default for ``expected``: read the repository's budget.
_FROM_HISTORY: Any = object()

_SUMMARY_LINE = re.compile(
    r"\b\d+ (?:passed|failed|errors?|tests?|skipped)\b|test result:|^(?:ok|FAIL|PASS)\b"
    r"|\bTests?:\s",
    re.IGNORECASE,
)


class GateError(Exception):
    """A gate could not be resolved or started."""


@dataclass(frozen=True)
class GateSpec:
    """One gate to run: what, where, with what environment, and for whom."""

    repo: str
    command: str
    cwd: str
    evidence_dir: str
    head_sha: str
    full: bool = False
    task_id: int | None = None
    run_id: int | None = None
    env: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def key(self) -> str:
        """Two requests for the same gate at the same head share one run."""
        scope = f"task:{self.task_id}" if self.task_id is not None else f"repo:{self.repo}"
        return f"{scope}:{'full' if self.full else 'local'}:{self.head_sha}"

    @property
    def label(self) -> str:
        return "full suite" if self.full else "local gate"


@dataclass(frozen=True)
class GateResult:
    """What a finished gate said, as it is recorded."""

    repo: str
    command: str
    full: bool
    exit_code: int
    duration_seconds: float
    summary: str
    head_sha: str
    output_path: str
    started_at: str
    finished_at: str
    task_id: int | None = None

    @property
    def green(self) -> bool:
        return self.exit_code == 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def line(self) -> str:
        """The one line a caller prints and a steer quotes."""
        state = "green" if self.green else f"red (exit {self.exit_code})"
        summary = self.summary or "(no output)"
        return (
            f"{self.label} {state} at {self.head_sha[:8]} after "
            f"{_duration(self.duration_seconds)}: `{self.command}` — {summary}"
        )

    @property
    def label(self) -> str:
        return "full suite" if self.full else "local gate"


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, rest = divmod(seconds, 60)
    return f"{minutes}m{rest:02d}s" if minutes else f"{rest}s"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def head_of(path: str) -> str:
    """The commit checked out at ``path``, or ``""`` when git cannot say."""
    proc = subprocess.run(
        ["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def summary_line(text: str) -> str:
    """The line of a gate's output that says how it went.

    The last line that reads like a test summary (pytest's ``3 passed in 1.2s``, cargo's
    ``test result:``, go's ``ok``/``FAIL``), with the decoration stripped; otherwise the
    last non-empty line, which is usually the error that stopped it.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    chosen = next((line for line in reversed(lines) if _SUMMARY_LINE.search(line)), None)
    if chosen is None:
        chosen = lines[-1] if lines else ""
    return chosen.strip("=- ").strip()[:MAX_SUMMARY]


def _last_line(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 4096))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    return lines[-1][:MAX_SUMMARY] if lines else ""


def progress_line(label: str, elapsed: float, last: str) -> str:
    return f"{label} still running: {_duration(elapsed)} elapsed" + (
        f"; last output: {last}" if last else ""
    )


def _budget_kind(full: bool) -> str:
    from papaya_agent_runtime import budgets

    return budgets.FULL_SUITE if full else budgets.GATE


def expected_seconds(repo: str, full: bool) -> float | None:
    """This repository's budget for the gate, when its own history stands behind one."""
    from papaya_agent_runtime import budgets

    found = budgets.budget(repo, _budget_kind(full))
    return found.seconds if found.derived or found.source == budgets.OVERRIDE else None


def expectation_line(label: str, repo: str, full: bool) -> str | None:
    """What a caller is told to expect, from how long this gate has taken here."""
    from papaya_agent_runtime import budgets

    found = budgets.budget(repo, _budget_kind(full))
    if not found.known or found.p90 is None:
        return None
    return (
        f"the {label} here has taken up to {_duration(found.p90)} "
        f"(p90 of its last {found.observations} runs); its budget is {_duration(found.seconds)}"
    )


def longer_than_usual_line(label: str, elapsed: float, budget_seconds: float) -> str:
    return (
        f"{label} is taking longer than usual: {_duration(elapsed)} elapsed, past this "
        f"repository's budget of {_duration(budget_seconds)}"
    )


# ── Resolving what to run ───────────────────────────────────────────────────


def resolve(*, task_id: int | None = None, repo: str | None = None, full: bool = False) -> GateSpec:
    """The gate a task (in its worktree) or a repository (in its base clone) runs.

    A task wins: its worktree is where the work is, and its process environment is the
    one the worker's own shell has. A repository named alongside a task has to be that
    task's, so a turn cannot run one repository's gate against another's worktree.
    """
    from papaya_agent_runtime.supervisor.runner import worker_env

    if task_id is None and not repo:
        raise GateError("name the task (`--task <id>`) or the repository to run a gate for")
    conn = init_db()
    try:
        if task_id is not None:
            task = store.get_task(conn, task_id)
            if task is None:
                raise GateError(f"task {task_id} not found")
            row = conn.execute("SELECT * FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
            if row is None:
                raise GateError(f"task {task_id} has no registered repository")
            if repo and repo != row["name"]:
                raise GateError(f"task {task_id} is in {row['name']}, not {repo}")
            cwd = str(task["worktree_path"] or "")
            if not cwd or not Path(cwd).is_dir():
                raise GateError(f"task {task_id} has no accessible worktree")
            env = worker_env(
                dict(os.environ), task_values=environment.task_process_env(conn, row, task_id)
            )
            run_id = int(task["run_id"]) if task["run_id"] is not None else None
        else:
            row = store.get_repo(conn, str(repo))
            if row is None:
                raise GateError(f"repo {repo!r} is not registered")
            cwd = str(row["local_path"] or "")
            if not cwd or not Path(cwd).is_dir():
                raise GateError(f"the base clone of {repo} is missing; `ppy repo sync {repo}`")
            env = dict(os.environ)
            run_id = None
        settings = environment.for_repo(row)
    finally:
        conn.close()
    command = settings.full_suite_command if full else settings.local_gate
    if not command:
        what = "full suite" if full else "local gate"
        flag = "--full-suite-command" if full else "--local-gate"
        raise GateError(
            f"{settings.repo} has no {what} recorded; `ppy repo onboard {settings.repo}` "
            f'derives one, or `ppy repo set {settings.repo} {flag} "<command>"`'
        )
    environment.ensure_excluded(cwd, settings.evidence_dir)
    return GateSpec(
        repo=settings.repo,
        command=command,
        cwd=cwd,
        evidence_dir=str(Path(cwd) / settings.evidence_dir),
        head_sha=head_of(cwd),
        full=full,
        task_id=task_id,
        run_id=run_id,
        env=env,
    )


# ── Running one ─────────────────────────────────────────────────────────────


def _output_path(spec: GateSpec) -> Path:
    directory = Path(spec.evidence_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"gate-{'full' if spec.full else 'local'}-{(spec.head_sha or 'nohead')[:8]}"
    candidate = directory / f"{stem}.txt"
    suffix = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{suffix}.txt"
        suffix += 1
    return candidate


def _record(spec: GateSpec, kind: str, payload: dict[str, Any]) -> None:
    conn = init_db()
    try:
        store.append_event(
            conn, kind=kind, payload=payload, run_id=spec.run_id, task_id=spec.task_id
        )
    finally:
        conn.close()


def _lifeline(action: str, proc: Any) -> None:
    """A gate is its own process group; the supervisor's lifeline answers for it."""
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int):
        from papaya_agent_runtime.supervisor import lifeline

        getattr(lifeline, action)(pid)


def run(
    spec: GateSpec,
    *,
    on_progress: Callable[[str], None] = lambda _line: None,
    on_process: Callable[[Any], None] = lambda _proc: None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    popen: Callable[..., Any] = subprocess.Popen,
    progress_every: float = PROGRESS_SECONDS,
    poll: float = POLL_SECONDS,
    expected: Any = _FROM_HISTORY,
) -> GateResult:
    """Run ``spec`` to the end, with no timeout, and record the result on the ledger.

    Output goes straight to a file in the evidence directory rather than through a
    pipe, so a suite that prints a lot can never stall on a reader that went away.

    ``expected`` is how long this gate should take (by default, the repository's budget
    when history or a person stands behind one): past it, one progress line says the
    gate is taking longer than usual. Its duration is kept as a budget observation.
    """
    if expected is _FROM_HISTORY:
        expected = expected_seconds(spec.repo, spec.full)
    output_path = _output_path(spec)
    started_at = _now()
    _record(
        spec,
        GATE_STARTED,
        {
            "repo": spec.repo,
            "command": spec.command,
            "full": spec.full,
            "head_sha": spec.head_sha,
            "output_path": str(output_path),
        },
    )
    started = clock()
    with output_path.open("w", encoding="utf-8") as output:
        try:
            proc = popen(
                ["/bin/sh", "-c", spec.command],
                cwd=spec.cwd,
                env=spec.env or None,
                stdout=output,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            output.write(f"could not start `{spec.command}`: {exc}\n")
            proc = None
        if proc is not None:
            on_process(proc)
            _lifeline("watch_group", proc)
            last_progress = started
            overdue = False
            while proc.poll() is None:
                sleep(poll)
                now = clock()
                if expected is not None and not overdue and now - started > expected:
                    overdue = True
                    on_progress(longer_than_usual_line(spec.label, now - started, expected))
                if now - last_progress >= progress_every:
                    last_progress = now
                    output.flush()
                    on_progress(progress_line(spec.label, now - started, _last_line(output_path)))
            _lifeline("release_group", proc)
    exit_code = proc.returncode if proc is not None else 127
    try:
        text = output_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    result = GateResult(
        repo=spec.repo,
        command=spec.command,
        full=spec.full,
        exit_code=int(exit_code),
        duration_seconds=round(max(0.0, clock() - started), 3),
        summary=summary_line(text),
        head_sha=spec.head_sha,
        output_path=str(output_path),
        started_at=started_at,
        finished_at=_now(),
        task_id=spec.task_id,
    )
    # A gate killed by a signal (its supervisor stopping) did not fail: it never
    # finished, so it leaves no verdict to be steered on — only a note that it died.
    _record(spec, GATE_KILLED if result.exit_code < 0 else GATE_RESULT, result.as_dict())
    from papaya_agent_runtime import budgets

    # A red gate took as long as it took; one a signal ended never finished.
    budgets.observe(
        spec.repo,
        _budget_kind(spec.full),
        result.duration_seconds,
        task_id=spec.task_id,
        outcome=budgets.KILL if result.exit_code < 0 else ("pass" if result.green else "fail"),
    )
    with (
        contextlib.suppress(OSError),
        (Path(spec.evidence_dir) / "receipts.txt").open("a", encoding="utf-8") as receipts,
    ):
        receipts.write(
            f"{result.finished_at} | ppy gate run: {spec.command} | exit={result.exit_code} | "
            f"{result.duration_seconds:.3f}s | head={spec.head_sha}\n"
        )
    return result


# ── The supervisor's side ───────────────────────────────────────────────────


@dataclass
class _Running:
    spec: GateSpec
    started: float
    done: threading.Event = field(default_factory=threading.Event)
    result: GateResult | None = None
    error: str | None = None
    last_progress: str = ""
    process: Any = None
    thread: threading.Thread | None = None


class Gates:
    """The gates a supervisor is running, one per task, scope and head.

    Held by :class:`~papaya_agent_runtime.supervisor.server.SupervisorServer`, so a gate
    belongs to the long-lived process and not to the session that asked for it.
    """

    def __init__(
        self,
        *,
        runner: Callable[..., GateResult] = run,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._clock = clock
        self._lock = threading.Lock()
        self._runs: dict[str, _Running] = {}

    def start(self, spec: GateSpec) -> dict[str, Any]:
        """Start ``spec``, or attach to the same gate already running at the same head."""
        with self._lock:
            existing = self._runs.get(spec.key)
            if existing is not None and not existing.done.is_set():
                return self._describe(spec.key, existing, attached=True)
            running = _Running(spec=spec, started=self._clock())
            self._runs[spec.key] = running
        thread = threading.Thread(
            target=self._run, args=(running,), name=f"gate-{spec.key}", daemon=True
        )
        running.thread = thread
        thread.start()
        return self._describe(spec.key, running, attached=False)

    def _run(self, running: _Running) -> None:
        def progress(line: str) -> None:
            running.last_progress = line

        def process(proc: Any) -> None:
            running.process = proc

        try:
            running.result = self._runner(running.spec, on_progress=progress, on_process=process)
        except Exception as exc:  # noqa: BLE001 - a broken gate is an answer, not a crash
            running.error = str(exc) or exc.__class__.__name__
        finally:
            running.done.set()

    def wait(self, key: str, timeout: float) -> dict[str, Any]:
        """Block up to ``timeout`` seconds for the gate under ``key``; say where it stands."""
        with self._lock:
            running = self._runs.get(key)
        if running is None:
            raise GateError(f"no gate {key!r} is running here")
        running.done.wait(max(0.0, timeout))
        return self._describe(key, running, attached=True)

    def _describe(self, key: str, running: _Running, *, attached: bool) -> dict[str, Any]:
        spec = running.spec
        answer: dict[str, Any] = {
            "key": key,
            "attached": attached,
            "repo": spec.repo,
            "command": spec.command,
            "full": spec.full,
            "head_sha": spec.head_sha,
            "running": not running.done.is_set(),
            "elapsed": round(self._clock() - running.started, 3),
            "last_progress": running.last_progress,
        }
        if running.result is not None:
            answer["result"] = running.result.as_dict()
        if running.error is not None:
            answer["error"] = running.error
        return answer

    def close(self, timeout: float = 3.0) -> None:
        """Stop every gate still running: a gate outliving its supervisor records nothing."""
        with self._lock:
            runs = list(self._runs.values())
        for running in runs:
            proc = running.process
            if proc is not None and not running.done.is_set():
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.killpg(proc.pid, 15)
        for running in runs:
            if running.thread is not None:
                running.thread.join(timeout)


# ── The caller's side ───────────────────────────────────────────────────────


def _result_from(payload: dict[str, Any]) -> GateResult:
    known = GateResult.__dataclass_fields__
    return GateResult(**{key: value for key, value in payload.items() if key in known})


def run_from_cli(
    *,
    task_id: int | None,
    repo: str | None,
    full: bool,
    wait_seconds: float = WAIT_SECONDS,
    out: Callable[[str], None] = print,
    client: Any = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """`ppy gate run`: 0 green, 1 red, :data:`STILL_RUNNING` when it outlasted this call."""
    from papaya_agent_runtime.supervisor.client import SupervisorClient, SupervisorUnavailable

    spec = resolve(task_id=task_id, repo=repo, full=full)
    expectation = expectation_line(spec.label, spec.repo, spec.full)
    if expectation:
        out(expectation)
    expected = expected_seconds(spec.repo, spec.full)
    client = client or SupervisorClient()
    try:
        client.ping()
    except SupervisorUnavailable:
        out(
            f"no supervisor is running, so the {spec.label} runs in this process: "
            f"`{spec.command}` in {spec.cwd}"
        )
        result = run(spec, on_progress=out, expected=expected)
        out(result.line())
        return 0 if result.green else 1

    answer = client.gate_start(task_id=task_id, repo=repo, full=full)
    if not answer.get("ok"):
        raise GateError(str(answer.get("error") or "the supervisor refused the gate"))
    verb = "attached to the" if answer.get("attached") else "started the"
    out(
        f"{verb} {spec.label} for {spec.repo} at {spec.head_sha[:8]} under the supervisor: "
        f"`{spec.command}`"
    )
    deadline = clock() + max(0.0, wait_seconds)
    key = str(answer["key"])
    overdue = False
    while True:
        if answer.get("error"):
            raise GateError(f"the gate could not run: {answer['error']}")
        if answer.get("result"):
            result = _result_from(answer["result"])
            out(result.line())
            out(f"output: {result.output_path}")
            return 0 if result.green else 1
        remaining = deadline - clock()
        if remaining <= 0:
            out(
                f"{spec.label} still running after {_duration(float(answer.get('elapsed') or 0))}; "
                "it keeps running under the supervisor and its result is recorded when it "
                "ends. Run this same command again to keep waiting."
            )
            return STILL_RUNNING
        answer = client.gate_wait(key, timeout=min(PROGRESS_SECONDS, remaining))
        if not answer.get("ok"):
            raise GateError(str(answer.get("error") or "the supervisor lost the gate"))
        if answer.get("running"):
            elapsed = float(answer.get("elapsed") or 0)
            if expected is not None and not overdue and elapsed > expected:
                overdue = True
                out(longer_than_usual_line(spec.label, elapsed, expected))
            out(
                progress_line(
                    spec.label,
                    float(answer.get("elapsed") or 0),
                    str(answer.get("last_progress") or "").partition("last output: ")[2],
                )
            )


# ── What the runner decides on ──────────────────────────────────────────────


@dataclass(frozen=True)
class Verdict:
    """A task's gate at its current head: green, red, or nothing recorded."""

    state: str
    head_sha: str = ""
    result: GateResult | None = None


def verdict(task_id: int) -> Verdict:
    """The newest recorded gate result at the task's current head commit."""
    conn = init_db()
    try:
        task = store.get_task(conn, task_id)
        worktree = str(task["worktree_path"] or "") if task is not None else ""
        head = head_of(worktree) if worktree and Path(worktree).is_dir() else ""
        if not head:
            return Verdict(NONE)
        rows = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id DESC",
            (task_id, GATE_RESULT),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            continue
        if payload.get("head_sha") == head:
            result = _result_from(payload)
            return Verdict(GREEN if result.green else RED, head, result)
    return Verdict(NONE, head)


__all__ = [
    "GATE_RESULT",
    "GATE_STARTED",
    "GREEN",
    "NONE",
    "RED",
    "STILL_RUNNING",
    "GateError",
    "GateResult",
    "GateSpec",
    "Gates",
    "Verdict",
    "resolve",
    "run",
    "run_from_cli",
    "summary_line",
    "verdict",
]
