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

Heavy gates run one at a time per repository (2026-09-17 00:05 UTC: two backend
workers ran `make verify` at once, 1.3 GB and 0.9 GB resident, and the machine killed
other processes to make room). A full suite, or a gate whose learned duration in its
repository is past ``gate.parallel_ceiling_seconds``, takes one of the repository's
``gate.full_slots_per_repo`` slots before it starts; the rest queue in arrival order,
and a queued gate answers its caller with what it is queued behind. At the head of the
queue it also waits for free memory: at least the repository's p90 of gate peak memory
(or ``gate.min_free_mb``). Every gate's peak resident memory is sampled while it runs
and kept with its result and as a ``gate_memory`` observation. Scoped gates never
queue. Time spent queued is neither the gate's duration nor the worker's silence.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from papaya_agent_runtime import environment
from papaya_agent_runtime.paths import ppy_home
from papaya_agent_runtime.state import init_db, store

#: The ledger event a finished gate leaves on its task.
GATE_RESULT = "gate_result"
#: The ledger event a gate leaves when it starts, so a run that never finished shows.
GATE_STARTED = "gate_started"
#: The ledger event a gate leaves when a signal ended it before it could finish.
GATE_KILLED = "gate_killed"
#: The ledger event a heavy gate leaves when it has to wait for a slot or for memory.
GATE_QUEUED = "gate_queued"
#: The ledger event a queued gate leaves when it stops waiting (to start, or not at all).
GATE_UNQUEUED = "gate_unqueued"

#: How often a queued gate looks at free memory again when nothing else woke it.
QUEUE_POLL_SECONDS = 5.0
#: How often a running gate's process group is sampled for resident memory.
MEMORY_SAMPLE_SECONDS = 5.0

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
    #: The compose project and database the gate's environment points at, when it has one.
    compose_project: str | None = None
    database: str | None = None
    #: A baseline gate runs in a scratch worktree of ``head_sha`` checked out from this
    #: base clone when it starts, and removed (with its stack) when it ends.
    base_clone: str | None = None

    @property
    def baseline(self) -> bool:
        return self.base_clone is not None

    @property
    def key(self) -> str:
        """Two requests for the same gate at the same head share one run."""
        scope = f"task:{self.task_id}" if self.task_id is not None else f"repo:{self.repo}"
        if self.baseline:
            scope += ":baseline"
        return f"{scope}:{'full' if self.full else 'local'}:{self.head_sha}"

    @property
    def label(self) -> str:
        return _label(self.full, self.baseline)


def _label(full: bool, baseline: bool) -> str:
    return ("baseline " if baseline else "") + ("full suite" if full else "local gate")


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
    #: The largest resident memory sampled across the gate's process group, in MB.
    peak_memory_mb: float | None = None
    #: The compose project and database it ran against; ``None`` when nothing private
    #: was set, which means the repository's defaults.
    compose_project: str | None = None
    database: str | None = None
    #: A baseline gate ran at a base commit in a scratch worktree, not at a task's head.
    baseline: bool = False
    #: The tests its output names as failed or errored (pytest's ``FAILED``/``ERROR``).
    failing_tests: list[str] = field(default_factory=list)

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
        return _label(self.full, self.baseline)


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


_FAILING_TEST = re.compile(r"^(?:FAILED|ERROR) (\S+)")
#: The most failing tests kept on one result.
MAX_FAILING_TESTS = 50


def failing_tests(text: str) -> list[str]:
    """The tests a gate's output names as failed or errored, sorted, without the reason."""
    found = {
        match.group(1)
        for line in text.splitlines()
        if (match := _FAILING_TEST.match(line.strip())) is not None
    }
    return sorted(found)[:MAX_FAILING_TESTS]


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


def learned_seconds(repo: str, full: bool) -> float | None:
    """How long this gate takes here: a person's override, else the p90 its history says.

    Not the budget: a budget is floored at the ten-minute tool cap, so every gate with
    history would look heavy.
    """
    from papaya_agent_runtime import budgets

    found = budgets.budget(repo, _budget_kind(full))
    if found.source == budgets.OVERRIDE:
        return found.seconds
    return found.p90 if found.known else None


@dataclass(frozen=True)
class GateSettings:
    """`[gate]` in the config, as the supervisor's queue reads it."""

    parallel_ceiling_seconds: float = 300.0
    full_slots_per_repo: int = 1
    min_free_mb: float = 0.0


def gate_settings() -> GateSettings:
    try:
        from papaya_agent_runtime.config import load_config

        policy = load_config().gate
    except Exception:  # noqa: BLE001 - a supervisor runs gates before setup too
        return GateSettings()
    return GateSettings(
        float(policy.parallel_ceiling_seconds),
        int(policy.full_slots_per_repo),
        float(policy.min_free_mb),
    )


def memory_needed(repo: str, settings: GateSettings) -> tuple[float, str] | None:
    """The free memory a heavy gate in ``repo`` waits for, and whose word that is."""
    if settings.min_free_mb > 0:
        return settings.min_free_mb, "gate.min_free_mb"
    from papaya_agent_runtime import budgets

    found = budgets.memory(repo)
    if not found.known or found.p90_mb is None:
        return None
    return found.p90_mb, "this repository's gate memory p90"


# ── Memory ──────────────────────────────────────────────────────────────────


def process_group_rss_mb(pgid: int) -> float | None:
    """The resident memory of every process in group ``pgid``, in MB, from `ps`.

    A gate starts its own session, so its group is the gate and everything it spawned
    (a test runner's workers, a database it started) that did not leave for a group of
    its own.
    """
    try:
        proc = subprocess.run(
            ["ps", "-A", "-o", "pgid=,rss="],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    total_kb = 0
    found = False
    for line in proc.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not all(field.isdigit() for field in fields):
            continue
        if int(fields[0]) == pgid:
            total_kb += int(fields[1])
            found = True
    return round(total_kb / 1024, 1) if found else None


_VM_STAT_PAGE = re.compile(r"page size of (\d+) bytes")
_VM_STAT_LINE = re.compile(r"^Pages (free|inactive|speculative):\s+(\d+)\.?$")


def free_memory_mb() -> float | None:
    """Memory this machine could give a new process now, in MB; ``None`` when unknown.

    Linux says it directly (`MemAvailable`). On macOS it is free, inactive and
    speculative pages from `vm_stat`: what the kernel hands out before it has to evict.
    """
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        try:
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                if line.startswith("MemAvailable:"):
                    return round(int(line.split()[1]) / 1024, 1)
        except (OSError, ValueError, IndexError):
            return None
        return None
    try:
        proc = subprocess.run(["vm_stat"], capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    page = _VM_STAT_PAGE.search(proc.stdout) if proc.returncode == 0 else None
    if page is None:
        return None
    pages = sum(
        int(match.group(2))
        for match in (_VM_STAT_LINE.match(line.strip()) for line in proc.stdout.splitlines())
        if match is not None
    )
    return round(pages * int(page.group(1)) / (1024 * 1024), 1)


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


def gate_env(
    base: dict[str, str], values: dict[str, str], settings: environment.RepoEnvironment
) -> dict[str, str]:
    """The environment a gate process gets: the worker's, never the supervisor's stack.

    What :func:`~papaya_agent_runtime.supervisor.runner.worker_env` builds (the
    manager's ``VIRTUAL_ENV`` and database URLs dropped, then ``values``), with the
    supervisor's own compose project and database port dropped first as well, so a
    repository with no private stack declared falls back to its own defaults rather
    than to whatever stack the supervisor's shell happened to name.
    """
    from papaya_agent_runtime.supervisor.runner import worker_env

    inherited = dict(base)
    for key in ("COMPOSE_PROJECT_NAME", settings.db_port_variable):
        inherited.pop(key, None)
    return worker_env(inherited, task_values=values)


def commit_in(path: str, ref: str) -> str:
    """The full commit ``ref`` names in the clone at ``path``, or ``""``."""
    proc = subprocess.run(
        ["git", "-C", path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def resolve(
    *,
    task_id: int | None = None,
    repo: str | None = None,
    full: bool = False,
    baseline: str | None = None,
) -> GateSpec:
    """The gate a task (in its worktree) or a repository (in its base clone) runs.

    A task wins: its worktree is where the work is, and its process environment is the
    one the worker's own shell has, rendered here for its task id. A repository named
    alongside a task has to be that task's, so a turn cannot run one repository's gate
    against another's worktree.

    ``baseline`` is a commit: the gate runs in a scratch worktree of it (the task only
    names the repository), in an environment private to that commit and gate, so a
    review's "was this already red on the base?" never touches a task's database or
    the repository's default one. A gate with no task, in the base clone, gets a
    private environment the same way.
    """
    if task_id is None and not repo:
        raise GateError("name the task (`--task <id>`) or the repository to run a gate for")
    conn = init_db()
    try:
        run_id = None
        base_clone = None
        if task_id is not None:
            task = store.get_task(conn, task_id)
            if task is None:
                raise GateError(f"task {task_id} not found")
            row = conn.execute("SELECT * FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
            if row is None:
                raise GateError(f"task {task_id} has no registered repository")
            if repo and repo != row["name"]:
                raise GateError(f"task {task_id} is in {row['name']}, not {repo}")
        else:
            row = store.get_repo(conn, str(repo))
            if row is None:
                raise GateError(f"repo {repo!r} is not registered")
        settings = environment.for_repo(row)
        name = settings.repo
        mode = "full" if full else "local"
        if baseline:
            base_clone = str(row["local_path"] or "")
            if not base_clone or not Path(base_clone).is_dir():
                raise GateError(f"the base clone of {name} is missing; `ppy repo sync {name}`")
            head = commit_in(base_clone, baseline)
            if not head:
                raise GateError(
                    f"{baseline} is not a commit in the base clone of {name}; "
                    f"`ppy repo sync {name}`"
                )
            scope = f"gate_base_{mode}_{head[:12]}"
            cwd = str(ppy_home() / "gates" / "scratch" / f"{name}-{scope}")
            evidence_dir = str(ppy_home() / "gates" / name)
            values = environment.scope_process_env(row, scope)
            task_id = None
        elif task_id is not None:
            cwd = str(task["worktree_path"] or "")
            if not cwd or not Path(cwd).is_dir():
                raise GateError(f"task {task_id} has no accessible worktree")
            values = environment.render_task_env(conn, row, task_id)
            run_id = int(task["run_id"]) if task["run_id"] is not None else None
            head = head_of(cwd)
            evidence_dir = str(Path(cwd) / settings.evidence_dir)
            repeated = repeated_red(_results_at(conn, task_id, head))
            if repeated and (repeated[0].compose_project, repeated[0].database) == (
                values.get("COMPOSE_PROJECT_NAME"),
                environment.database_name(values),
            ):
                raise GateError(
                    f"{repeated_line(repeated)}. A third run in the same environment cannot "
                    "say anything new: this is a decision now. "
                    f"`ppy gate run --task {task_id} --baseline <base sha>` settles whether "
                    "those failures were already on the base."
                )
        else:
            cwd = str(row["local_path"] or "")
            if not cwd or not Path(cwd).is_dir():
                raise GateError(f"the base clone of {name} is missing; `ppy repo sync {name}`")
            head = head_of(cwd)
            values = environment.scope_process_env(row, f"gate_repo_{mode}_{head[:12]}")
            evidence_dir = str(Path(cwd) / settings.evidence_dir)
    finally:
        conn.close()
    command = settings.full_suite_command if full else settings.local_gate
    if not command:
        what = "full suite" if full else "local gate"
        flag = "--full-suite-command" if full else "--local-gate"
        raise GateError(
            f"{name} has no {what} recorded; `ppy repo onboard {name}` "
            f'derives one, or `ppy repo set {name} {flag} "<command>"`'
        )
    if base_clone is None:
        environment.ensure_excluded(cwd, settings.evidence_dir)
    return GateSpec(
        repo=name,
        command=command,
        cwd=cwd,
        evidence_dir=evidence_dir,
        head_sha=head,
        full=full,
        task_id=task_id,
        run_id=run_id,
        env=gate_env(dict(os.environ), values, settings),
        compose_project=values.get("COMPOSE_PROJECT_NAME"),
        database=environment.database_name(values),
        base_clone=base_clone,
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


def _git_quiet(*argv: str) -> bool:
    proc = subprocess.run(["git", *argv], capture_output=True, text=True, check=False)
    return proc.returncode == 0


def _remove_scratch(spec: GateSpec) -> None:
    """Take a baseline's scratch worktree away, whatever state it is in. Never raises."""
    if spec.base_clone is None:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        _git_quiet("-C", spec.base_clone, "worktree", "remove", "--force", spec.cwd)
    shutil.rmtree(spec.cwd, ignore_errors=True)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        _git_quiet("-C", spec.base_clone, "worktree", "prune")


def _add_scratch(spec: GateSpec) -> str | None:
    """Check a baseline's commit out in its scratch worktree; say why not when it fails."""
    if spec.base_clone is None:
        return None
    _remove_scratch(spec)  # one a stopped supervisor left behind
    Path(spec.cwd).parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "-C", spec.base_clone, "worktree", "add", "--detach", spec.cwd, spec.head_sha],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or "git worktree add failed").strip()
    return None


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
    memory: Callable[[int], float | None] = process_group_rss_mb,
    memory_every: float = MEMORY_SAMPLE_SECONDS,
) -> GateResult:
    """Run ``spec`` to the end, with no timeout, and record the result on the ledger.

    Output goes straight to a file in the evidence directory rather than through a
    pipe, so a suite that prints a lot can never stall on a reader that went away.

    ``expected`` is how long this gate should take (by default, the repository's budget
    when history or a person stands behind one): past it, one progress line says the
    gate is taking longer than usual. Its duration is kept as a budget observation.

    ``memory`` reads the resident memory of the gate's process group; it is sampled
    when the gate starts and every ``memory_every`` seconds, and the peak is kept with
    the result and as a ``gate_memory`` observation.

    A baseline gate's scratch worktree is checked out here and removed at the end, and a
    gate that belongs to no task takes its private compose stack down with it: nobody
    else will.
    """
    if expected is _FROM_HISTORY:
        expected = expected_seconds(spec.repo, spec.full)
    output_path = _output_path(spec)
    started_at = _now()
    _record(
        spec,
        GATE_STARTED,
        {
            "key": spec.key,
            "repo": spec.repo,
            "command": spec.command,
            "full": spec.full,
            "head_sha": spec.head_sha,
            "output_path": str(output_path),
            "compose_project": spec.compose_project,
            "database": spec.database,
            "baseline": spec.baseline,
        },
    )
    try:
        return _run_started(
            spec,
            output_path=output_path,
            started_at=started_at,
            on_progress=on_progress,
            on_process=on_process,
            clock=clock,
            sleep=sleep,
            popen=popen,
            progress_every=progress_every,
            poll=poll,
            expected=expected,
            memory=memory,
            memory_every=memory_every,
        )
    finally:
        _remove_scratch(spec)
        if spec.task_id is None and spec.compose_project:
            from papaya_agent_runtime import compose

            compose.down(spec.compose_project)


def _run_started(
    spec: GateSpec,
    *,
    output_path: Path,
    started_at: str,
    on_progress: Callable[[str], None],
    on_process: Callable[[Any], None],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    popen: Callable[..., Any],
    progress_every: float,
    poll: float,
    expected: float | None,
    memory: Callable[[int], float | None],
    memory_every: float,
) -> GateResult:
    started = clock()
    peak: float | None = None

    def sample(pid: Any) -> None:
        nonlocal peak
        if not isinstance(pid, int):
            return
        try:
            value = memory(pid)
        except Exception:  # noqa: BLE001 - a lost sample is a lost data point, not a gate
            return
        if value is not None and (peak is None or value > peak):
            peak = float(value)

    with output_path.open("w", encoding="utf-8") as output:
        scratch_error = _add_scratch(spec)
        if scratch_error:
            output.write(f"could not check out {spec.head_sha} to gate it: {scratch_error}\n")
            proc = None
        else:
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
            sample(getattr(proc, "pid", None))
            last_progress = started
            last_sample = started
            overdue = False
            while proc.poll() is None:
                sleep(poll)
                now = clock()
                if now - last_sample >= memory_every:
                    last_sample = now
                    sample(getattr(proc, "pid", None))
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
        peak_memory_mb=peak,
        compose_project=spec.compose_project,
        database=spec.database,
        baseline=spec.baseline,
        failing_tests=failing_tests(text) if exit_code != 0 else [],
    )
    # A gate killed by a signal (its supervisor stopping) did not fail: it never
    # finished, so it leaves no verdict to be steered on — only a note that it died.
    _record(spec, GATE_KILLED if result.exit_code < 0 else GATE_RESULT, result.as_dict())
    from papaya_agent_runtime import budgets

    # A red gate took as long as it took; one a signal ended never finished.
    outcome = budgets.KILL if result.exit_code < 0 else ("pass" if result.green else "fail")
    budgets.observe(
        spec.repo,
        _budget_kind(spec.full),
        result.duration_seconds,
        task_id=spec.task_id,
        outcome=outcome,
    )
    if peak is not None:
        budgets.observe(spec.repo, budgets.GATE_MEMORY, peak, task_id=spec.task_id, outcome=outcome)
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
    #: When it was asked for.
    arrived: float
    #: Heavy gates take a repository slot; the rest start at once.
    heavy: bool = False
    settings: GateSettings = field(default_factory=GateSettings)
    #: When it left the queue and started; ``None`` while it waits.
    began: float | None = None
    #: While queued: why, and (for a slot) whose gate it is behind.
    queued_reason: str = ""
    done: threading.Event = field(default_factory=threading.Event)
    result: GateResult | None = None
    error: str | None = None
    last_progress: str = ""
    process: Any = None
    thread: threading.Thread | None = None

    @property
    def name(self) -> str:
        what = "full gate" if self.spec.full else "local gate"
        if self.spec.task_id is not None:
            return f"task {self.spec.task_id}'s {what}"
        return f"the {what} for {self.spec.repo}"


def _minutes_ago(seconds: float) -> str:
    return f"{max(0, int(seconds // 60))} min ago"


class Gates:
    """The gates a supervisor is running, one per task, scope and head.

    Held by :class:`~papaya_agent_runtime.supervisor.server.SupervisorServer`, so a gate
    belongs to the long-lived process and not to the session that asked for it. Heavy
    gates (every full suite, and a gate whose learned duration is past
    ``gate.parallel_ceiling_seconds``) share ``gate.full_slots_per_repo`` slots per
    repository, first come first served, and the head of a repository's queue waits
    for free memory too. Scoped gates start at once.
    """

    def __init__(
        self,
        *,
        runner: Callable[..., GateResult] = run,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
        settings: Callable[[], GateSettings] = gate_settings,
        learned: Callable[[str, bool], float | None] = learned_seconds,
        free_memory: Callable[[], float | None] = free_memory_mb,
        needed_memory: Callable[[str, GateSettings], tuple[float, str] | None] = memory_needed,
        poll: float = QUEUE_POLL_SECONDS,
        record: Callable[[GateSpec, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._runner = runner
        self._clock = clock
        self._wall = wall
        self._settings = settings
        self._learned = learned
        self._free_memory = free_memory
        self._needed_memory = needed_memory
        self._poll = poll
        self._record = record or _record
        self._lock = threading.Condition()
        self._runs: dict[str, _Running] = {}
        #: Per repository: heavy gates waiting, in arrival order, and those holding a slot.
        self._queues: dict[str, list[_Running]] = {}
        self._holding: dict[str, list[_Running]] = {}
        #: Bumped whenever a slot frees or the queue closes, so no waiter misses it.
        self._changes = 0
        self._closing = False

    def _is_heavy(self, spec: GateSpec, settings: GateSettings) -> bool:
        if spec.full:
            return True
        try:
            learned = self._learned(spec.repo, spec.full)
        except Exception:  # noqa: BLE001 - no history is not a heavy gate
            return False
        return learned is not None and learned > settings.parallel_ceiling_seconds

    def start(self, spec: GateSpec) -> dict[str, Any]:
        """Start ``spec``, queue it, or attach to the same gate already there at the same head."""
        with self._lock:
            existing = self._runs.get(spec.key)
            if existing is not None and not existing.done.is_set():
                return self._describe(spec.key, existing, attached=True)
        settings = self._settings()
        heavy = self._is_heavy(spec, settings)
        with self._lock:
            existing = self._runs.get(spec.key)
            if existing is not None and not existing.done.is_set():
                return self._describe(spec.key, existing, attached=True)
            running = _Running(spec=spec, arrived=self._clock(), heavy=heavy, settings=settings)
            self._runs[spec.key] = running
            if heavy:
                self._queues.setdefault(spec.repo, []).append(running)
                running.queued_reason = self._slot_wait(running) or ""
            else:
                running.began = running.arrived
        thread = threading.Thread(
            target=self._run, args=(running,), name=f"gate-{spec.key}", daemon=True
        )
        running.thread = thread
        thread.start()
        return self._describe(spec.key, running, attached=False)

    # -- the queue ------------------------------------------------------------

    def _slot_wait(self, running: _Running) -> str | None:
        """Why ``running`` cannot take a slot now, or ``None``. Called with the lock held."""
        queue = self._queues.get(running.spec.repo, [])
        holders = self._holding.get(running.spec.repo, [])
        ahead = queue[: queue.index(running)] if running in queue else []
        if not ahead and len(holders) < running.settings.full_slots_per_repo:
            return None
        now = self._clock()
        if ahead:
            # Only gates that have not started are in the queue.
            blocker = ahead[-1]
            return f"queued behind {blocker.name}, queued {_minutes_ago(now - blocker.arrived)}"
        began = [(r.began, r) for r in holders if r.began is not None]
        if not began:
            return "queued for a gate slot"
        since, blocker = min(began, key=lambda pair: pair[0])
        return f"queued behind {blocker.name}, started {_minutes_ago(now - since)}"

    def _memory_wait(self, running: _Running) -> str | None:
        """Why the head of the queue should not start yet for memory, or ``None``."""
        try:
            needed = self._needed_memory(running.spec.repo, running.settings)
            if needed is None:
                return None
            free = self._free_memory()
        except Exception:  # noqa: BLE001 - memory nobody can read is not a reason to wait
            return None
        if free is None or free >= needed[0]:
            return None
        from papaya_agent_runtime.budgets import megabytes

        return (
            f"queued for memory: {megabytes(free)} free, below {needed[1]} "
            f"of {megabytes(needed[0])}"
        )

    def _leave_queue(self, running: _Running) -> None:
        queue = self._queues.get(running.spec.repo, [])
        if running in queue:
            queue.remove(running)
        self._changes += 1
        self._lock.notify_all()

    def _take_slot(self, running: _Running) -> bool:
        """Wait for ``running``'s turn and a slot, then hold it. False: the supervisor closed."""
        queued_at: datetime | None = None
        taken = False
        while True:
            with self._lock:
                if self._closing:
                    self._leave_queue(running)
                    break
                seen = self._changes
                reason = self._slot_wait(running)
            if reason is None:
                reason = self._memory_wait(running)
                if reason is None:
                    with self._lock:
                        if self._closing:
                            self._leave_queue(running)
                            break
                        if self._slot_wait(running) is None:
                            self._leave_queue(running)
                            self._holding.setdefault(running.spec.repo, []).append(running)
                            running.began = self._clock()
                            running.queued_reason = ""
                            taken = True
                            break
                    continue
            running.queued_reason = reason
            if queued_at is None:
                queued_at = self._wall()
                self._record(
                    running.spec,
                    GATE_QUEUED,
                    {
                        "key": running.spec.key,
                        "repo": running.spec.repo,
                        "command": running.spec.command,
                        "full": running.spec.full,
                        "head_sha": running.spec.head_sha,
                        "reason": reason,
                        "at": queued_at.isoformat(),
                    },
                )
            with self._lock:
                if not self._closing and self._changes == seen:
                    self._lock.wait(self._poll)
        if queued_at is not None:
            left = self._wall()
            self._record(
                running.spec,
                GATE_UNQUEUED,
                {
                    "key": running.spec.key,
                    "started": taken,
                    "at": left.isoformat(),
                    "queued_seconds": round(max(0.0, (left - queued_at).total_seconds()), 3),
                },
            )
        return taken

    def _release(self, running: _Running) -> None:
        with self._lock:
            holders = self._holding.get(running.spec.repo, [])
            if running in holders:
                holders.remove(running)
            self._changes += 1
            self._lock.notify_all()

    def _run(self, running: _Running) -> None:
        def progress(line: str) -> None:
            running.last_progress = line

        def process(proc: Any) -> None:
            running.process = proc

        try:
            if running.heavy and not self._take_slot(running):
                running.error = "the supervisor stopped before the gate could start"
                return
            running.result = self._runner(running.spec, on_progress=progress, on_process=process)
        except Exception as exc:  # noqa: BLE001 - a broken gate is an answer, not a crash
            running.error = str(exc) or exc.__class__.__name__
        finally:
            if running.heavy:
                self._release(running)
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
        now = self._clock()
        began = running.began
        # A heavy gate with a free slot is "starting" for the moment it reads free memory.
        queued = began is None and bool(running.queued_reason) and not running.done.is_set()
        answer: dict[str, Any] = {
            "key": key,
            "attached": attached,
            "repo": spec.repo,
            "command": spec.command,
            "full": spec.full,
            "head_sha": spec.head_sha,
            "running": not running.done.is_set(),
            # How long the gate itself has run: time spent queued is not its duration.
            "elapsed": round(now - began, 3) if began is not None else 0.0,
            "last_progress": running.last_progress,
            "queued": queued,
        }
        if queued:
            answer["queued_reason"] = running.queued_reason
            answer["queued_seconds"] = round(now - running.arrived, 3)
        if running.result is not None:
            answer["result"] = running.result.as_dict()
        if running.error is not None:
            answer["error"] = running.error
        return answer

    def close(self, timeout: float = 3.0) -> None:
        """Stop every gate still running: a gate outliving its supervisor records nothing."""
        with self._lock:
            self._closing = True
            self._changes += 1
            self._lock.notify_all()
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


def environment_line(spec: GateSpec) -> str:
    """Where the gate's database is, said before it runs so a collision is visible."""
    if spec.compose_project is None and spec.database is None:
        return (
            f"environment: no private database stack is declared for {spec.repo}, so the "
            "gate uses the repository's defaults"
        )
    parts = []
    if spec.compose_project:
        parts.append(f"compose project {spec.compose_project}")
    if spec.database:
        parts.append(f"database {spec.database}")
    return "environment: " + ", ".join(parts)


def _result_from(payload: dict[str, Any]) -> GateResult:
    known = GateResult.__dataclass_fields__
    return GateResult(**{key: value for key, value in payload.items() if key in known})


def run_from_cli(
    *,
    task_id: int | None,
    repo: str | None,
    full: bool,
    baseline: str | None = None,
    wait_seconds: float = WAIT_SECONDS,
    out: Callable[[str], None] = print,
    client: Any = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """`ppy gate run`: 0 green, 1 red, :data:`STILL_RUNNING` when it outlasted this call."""
    from papaya_agent_runtime.supervisor.client import SupervisorClient, SupervisorUnavailable

    spec = resolve(task_id=task_id, repo=repo, full=full, baseline=baseline)
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
        out(environment_line(spec))
        result = run(spec, on_progress=out, expected=expected)
        out(result.line())
        return 0 if result.green else 1

    answer = client.gate_start(
        task_id=task_id, repo=repo, full=full, **({"baseline": baseline} if baseline else {})
    )
    if not answer.get("ok"):
        raise GateError(str(answer.get("error") or "the supervisor refused the gate"))
    if answer.get("queued"):
        verb = "still queued: the" if answer.get("attached") else "queued the"
    else:
        verb = "attached to the" if answer.get("attached") else "started the"
    out(
        f"{verb} {spec.label} for {spec.repo} at {spec.head_sha[:8]} under the supervisor: "
        f"`{spec.command}`"
    )
    if answer.get("queued"):
        out(str(answer.get("queued_reason") or "queued"))
    out(environment_line(spec))
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
            if answer.get("queued"):
                out(
                    f"{spec.label} still queued: {answer.get('queued_reason') or 'queued'}. "
                    "It starts under the supervisor when its turn comes and its result is "
                    "recorded when it ends. Run this same command again to keep waiting."
                )
                return STILL_RUNNING
            out(
                f"{spec.label} still running after {_duration(float(answer.get('elapsed') or 0))}; "
                "it keeps running under the supervisor and its result is recorded when it "
                "ends. Run this same command again to keep waiting."
            )
            return STILL_RUNNING
        answer = client.gate_wait(key, timeout=min(PROGRESS_SECONDS, remaining))
        if not answer.get("ok"):
            raise GateError(str(answer.get("error") or "the supervisor lost the gate"))
        if answer.get("queued"):
            out(str(answer.get("queued_reason") or "queued"))
        elif answer.get("running"):
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


def _stamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def queued_seconds(conn: Any, task_id: int, since: datetime, until: datetime) -> float:
    """Seconds between ``since`` and ``until`` that a gate of this task spent queued.

    Read from `gate_queued`/`gate_unqueued` pairs; a gate still queued counts up to
    ``until``. Overlapping queued gates (a local and a full one) count once.
    """
    rows = conn.execute(
        "SELECT kind, payload FROM events WHERE task_id = ? AND kind IN (?, ?) ORDER BY id",
        (task_id, GATE_QUEUED, GATE_UNQUEUED),
    ).fetchall()
    opened: dict[str, datetime] = {}
    spans: list[tuple[datetime, datetime]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            continue
        key = str(payload.get("key") or "")
        at = _stamp(payload.get("at"))
        if at is None:
            continue
        if row["kind"] == GATE_QUEUED:
            opened[key] = at
        elif key in opened:
            spans.append((opened.pop(key), at))
    spans.extend((start, until) for start in opened.values())
    clipped = sorted(
        (max(start, since), min(end, until))
        for start, end in spans
        if end > since and start < until
    )
    total = 0.0
    cursor: datetime | None = None
    for start, end in clipped:
        if cursor is not None and start < cursor:
            start = cursor
        if end > start:
            total += (end - start).total_seconds()
        cursor = end if cursor is None or end > cursor else cursor
    return total


# ── What the runner decides on ──────────────────────────────────────────────


@dataclass(frozen=True)
class Verdict:
    """A task's gate at its current head: green, red, or nothing recorded."""

    state: str
    head_sha: str = ""
    result: GateResult | None = None
    #: The two newest results at this head when both are red the same way
    #: (:func:`repeated_red`): a third run cannot say anything new.
    repeated: tuple[GateResult, ...] = ()


#: How many red results at one head, failing the same way, stop the re-gating.
REPEATED_RED = 2

_TIMING = re.compile(r"\s+in [\d.]+s\b.*$")


def _fails_the_same_way(first: GateResult, second: GateResult) -> bool:
    """Red twice for the same reason, in the same environment.

    The same failing tests when the output names them, else the same summary with its
    timing dropped. A run in a different environment (a private database where the
    last had none) is a different question, so it does not count.
    """
    if first.green or second.green:
        return False
    if (first.compose_project, first.database) != (second.compose_project, second.database):
        return False
    if first.failing_tests or second.failing_tests:
        return first.failing_tests == second.failing_tests
    summary = [_TIMING.sub("", r.summary) for r in (first, second)]
    return bool(summary[0]) and summary[0] == summary[1]


def repeated_red(results: list[GateResult]) -> tuple[GateResult, ...]:
    """The newest :data:`REPEATED_RED` results (newest first) if they fail the same way."""
    finished = [r for r in results if not r.baseline][:REPEATED_RED]
    if len(finished) < REPEATED_RED:
        return ()
    return tuple(finished) if _fails_the_same_way(finished[0], finished[1]) else ()


def _results_at(conn: Any, task_id: int, head: str) -> list[GateResult]:
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id DESC",
        (task_id, GATE_RESULT),
    ).fetchall()
    found = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            continue
        if payload.get("head_sha") == head:
            found.append(_result_from(payload))
    return found


def verdict(task_id: int) -> Verdict:
    """The newest recorded gate result at the task's current head commit."""
    conn = init_db()
    try:
        task = store.get_task(conn, task_id)
        worktree = str(task["worktree_path"] or "") if task is not None else ""
        head = head_of(worktree) if worktree and Path(worktree).is_dir() else ""
        if not head:
            return Verdict(NONE)
        results = _results_at(conn, task_id, head)
    finally:
        conn.close()
    if not results:
        return Verdict(NONE, head)
    result = results[0]
    return Verdict(GREEN if result.green else RED, head, result, repeated_red(results))


def repeated_line(repeated: tuple[GateResult, ...]) -> str:
    """One line naming what failed twice, where, and that it is not being run again."""
    newest = repeated[0]
    what = ", ".join(newest.failing_tests) or newest.summary or newest.command
    return (
        f"the {newest.label} is red {len(repeated)} times at {newest.head_sha[:8]} with the "
        f"same failures ({what}); the runtime is not running it again"
    )


__all__ = [
    "GATE_KILLED",
    "GATE_QUEUED",
    "GATE_RESULT",
    "GATE_STARTED",
    "GATE_UNQUEUED",
    "GREEN",
    "NONE",
    "RED",
    "STILL_RUNNING",
    "GateError",
    "GateResult",
    "GateSettings",
    "GateSpec",
    "Gates",
    "Verdict",
    "free_memory_mb",
    "gate_settings",
    "learned_seconds",
    "memory_needed",
    "process_group_rss_mb",
    "queued_seconds",
    "resolve",
    "run",
    "run_from_cli",
    "summary_line",
    "verdict",
]
