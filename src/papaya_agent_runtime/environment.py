"""The per-repository environment block a worker is handed at dispatch.

Between 2026-09-01 and 09-07, 48 of 102 worker reflections lost a cycle to a fact
about the environment rather than to the task: a pre-push hook that runs the full
suite and denies the push (21), an evidence directory under ``/private/tmp`` that
the worker's shell cannot reach (21), a shared Postgres container recreated by a
sibling worktree mid-run (12), and a 10,000-test run killed by the worker's tool
timeout at about 77 percent, leaving the database poisoned for the next run. The
private-database recipe (a per-task compose project and port, ``make`` variable
overrides, a root ``.env`` pin) existed in repo notes and in
``ppy task env set <id> compose_project=...``, but nothing put it in front of the
worker; briefs restated environment facts by hand and drifted.

So the facts live on the repository (``ppy repo set <name> --compose-stack ...
--db-port-base ... --push-hook-runs-full-suite ... --local-gate ... --evidence-dir
...``) and the runtime renders them, once, identically, for every dispatch:

- the evidence directory is inside the worktree and excluded from git through the
  repository's ``info/exclude`` (shared by every worktree of the base clone), so a
  worker's shell can always reach it and ``ppy review show`` always lists it;
- a repository that declares a compose stack gets ``compose_project=task_<n>`` and
  a host port derived from ``db_port_base`` assigned at dispatch and recorded with
  the task-env mechanism the teardown on deliver/close/prune already reads;
- the worker process receives those resolved values, registered database URLs and
  writable tool caches rather than merely prose describing them;
- a repository whose pre-push hook runs the full suite tells the worker to stop at
  the code-level gates and file its done note with the head SHA — the harness's
  push-on-behalf path (:func:`papaya_agent_runtime.turn_end.rescue_unpushed`) then
  publishes the lease branch, exactly as it does for any done-but-unpushed turn.
"""

from __future__ import annotations

import shlex
import socket
import sqlite3
import string
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from papaya_agent_runtime import compose, prompts
from papaya_agent_runtime.paths import cache_dir, uv_cache_dir
from papaya_agent_runtime.state import store

HEADING = "Environment for this repository"
END_PHASES = ("review", "done")

#: Where receipts land when a repository names nothing else: inside the worktree,
#: so the worker's shell reaches it, and ignored by git, so it never ships.
DEFAULT_EVIDENCE_DIR = ".ppy-evidence"
#: Who runs the full suite when a repository names nobody.
DEFAULT_FULL_SUITE_OWNER = "ci"
#: The task-scoped key holding the database host port assigned at dispatch.
DB_PORT_KEY = "db_port"
#: The default variable name the recipe uses for that port. Compose files read it
#: from the environment or the root ``.env``; Makefiles take it as a variable
#: override. A repository whose Makefile spells it differently (papaya-backend
#: reads ``PAPAYA_DB_PORT``) sets ``--db-port-variable``.
DB_PORT_VARIABLE = "DB_PORT"
#: The columns ``ppy repo set`` owns for this block.
REPO_COLUMNS = (
    "compose_stack",
    "db_port_base",
    "db_port_variable",
    "push_hook_runs_full_suite",
    "local_gate",
    "full_suite_owner",
    "full_suite_command",
    "evidence_dir",
    "db_url_template",
    "test_db_url_template",
    "source_line_ceiling",
    "needs_elevated_localhost",
    "auto_merge",
    "merge_method",
)

#: The gate answers a repository gives (or a person overrides), each with a
#: ``<column>_source`` beside it: :data:`SOURCE_PERSON` (``ppy repo set``, never
#: replaced by any remedy or onboarding), ``repo:<file>:<line>`` (the repository says
#: it, quoted from there), or ``observed:<what>`` (the runtime saw it, e.g. the CI
#: workflow line that runs the full suite). :data:`SOURCE_HEURISTIC` marks a value the
#: runtime once guessed, which the start remedy clears (`solicit.keep_gate_policies_right`).
#: `solicit` spells the two prefixes itself; keep them equal.
GATE_ANSWERS = ("local_gate", "full_suite_command", "full_suite_owner")
SOURCE_PERSON = "person"
SOURCE_HEURISTIC = "heuristic"
SOURCE_REPO = "repo:"
SOURCE_OBSERVED = "observed:"
#: Who may run the full suite, besides CI: the supervisor, once at the delivered head.
OWNER_SUPERVISOR = "supervisor"


def source_column(answer: str) -> str:
    return f"{answer}_source"


#: How `gh pr merge` may merge a pull request on a repo that opted into `auto_merge`.
MERGE_METHODS = ("squash", "merge", "rebase")
DEFAULT_MERGE_METHOD = "squash"

MAX_PORT = 65535
MIN_PORT_BASE = 1024
_TRUE_WORDS = frozenset({"1", "yes", "y", "true", "on"})
_FALSE_WORDS = frozenset({"0", "no", "n", "false", "off", ""})


class RepoEnvironmentError(Exception):
    """A repo environment setting was refused."""


def validate_ends_at(value: str) -> str:
    if value not in END_PHASES:
        raise RepoEnvironmentError(
            f"--ends-at must be one of {', '.join(END_PHASES)}, got {value!r}"
        )
    return value


def _cell(row, column: str):
    if row is None:
        return None
    try:
        keys = row.keys()
    except AttributeError:
        return row.get(column) if isinstance(row, dict) else None
    return row[column] if column in keys else None  # noqa: SIM118 - sqlite3.Row


def parse_bool(value: str | bool | None) -> bool | None:
    """``yes``/``no`` and friends to a bool; None (or "") clears the setting."""
    if value is None or isinstance(value, bool):
        return value
    word = str(value).strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return None
    raise RepoEnvironmentError(f"expected yes or no, got {value!r}")


def parse_compose_stack(value: str | None) -> str | None:
    """``yes`` (the repository's default compose file), ``no`` (clear), or a file path."""
    if value is None:
        return None
    word = value.strip()
    if word.lower() in _FALSE_WORDS:
        return None
    if word.lower() in _TRUE_WORDS:
        return "yes"
    return word


def parse_port_base(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise RepoEnvironmentError(f"--db-port-base wants a port number, got {value!r}") from exc
    if port == 0:
        return None
    if not MIN_PORT_BASE <= port < MAX_PORT:
        raise RepoEnvironmentError(
            f"--db-port-base must be between {MIN_PORT_BASE} and {MAX_PORT - 1}, got {port}"
        )
    return port


def parse_source_line_ceiling(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        ceiling = int(value)
    except (TypeError, ValueError) as exc:
        raise RepoEnvironmentError(
            f"--source-line-ceiling wants a positive integer, got {value!r}"
        ) from exc
    if ceiling == 0:
        return None
    if ceiling < 1:
        raise RepoEnvironmentError(f"--source-line-ceiling wants a positive integer, got {ceiling}")
    return ceiling


def parse_url_template(value: str | None, option: str) -> str | None:
    """Validate the small, explicit template language used for task database URLs."""
    if value is None:
        return None
    template = value.strip()
    if not template:
        return None
    allowed = {"port", "name", "task_id"}
    try:
        fields = {
            field_name
            for _literal, field_name, _format_spec, _conversion in string.Formatter().parse(
                template
            )
            if field_name is not None
        }
    except ValueError as exc:
        raise RepoEnvironmentError(f"{option} is not a valid format template: {exc}") from exc
    unknown = fields - allowed
    if unknown:
        raise RepoEnvironmentError(
            f"{option} supports only {{port}}, {{name}}, and {{task_id}}; got "
            + ", ".join(sorted(unknown))
        )
    return template


@dataclass
class RepoEnvironment:
    """What a repository has declared about its environment, defaults filled in."""

    repo: str
    compose_stack: str | None = None
    db_port_base: int | None = None
    db_port_variable: str = DB_PORT_VARIABLE
    push_hook_runs_full_suite: bool = False
    local_gate: str | None = None
    full_suite_owner: str = DEFAULT_FULL_SUITE_OWNER
    full_suite_command: str | None = None
    evidence_dir: str = DEFAULT_EVIDENCE_DIR
    db_url_template: str | None = None
    test_db_url_template: str | None = None
    source_line_ceiling: int | None = None
    needs_elevated_localhost: bool = False
    auto_merge: bool = False
    merge_method: str = DEFAULT_MERGE_METHOD
    local_gate_source: str | None = None
    full_suite_command_source: str | None = None
    full_suite_owner_source: str | None = None

    @property
    def supervisor_runs_full_suite(self) -> bool:
        """Does the runtime run the full suite once before delivery (rather than CI)?"""
        return bool(self.full_suite_command) and self.full_suite_owner.lower().startswith(
            OWNER_SUPERVISOR
        )

    def sourced(self, answer: str) -> str:
        """A gate answer with where it came from, or ``unknown``."""
        value = getattr(self, answer)
        if not value:
            return "unknown"
        source = getattr(self, source_column(answer))
        return f"`{value}` ({source})" if source else f"`{value}`"

    def gate_lines(self) -> list[str]:
        """Each gate answer on its own line, its source beside it (`ppy repo show`)."""
        owner = (
            f"{self.full_suite_owner} ({self.full_suite_owner_source or 'default'})"
            if self.full_suite_command
            else "unknown (no full suite)"
        )
        return [
            f"scoped gate: {self.sourced('local_gate')}",
            f"full suite: {self.sourced('full_suite_command')}",
            f"full suite owner: {owner}",
        ]

    @property
    def compose_file(self) -> str | None:
        """The compose file named, or None when the repository's default applies."""
        if not self.compose_stack or self.compose_stack == "yes":
            return None
        return self.compose_stack

    def describe(self) -> list[str]:
        """One line per fact, the way ``ppy repo set <name>`` prints them."""
        if not self.compose_stack:
            stack = "none declared"
        else:
            stack = self.compose_file or "the repository's default compose file"
        port = (
            f"from {self.db_port_base} + task id, as {self.db_port_variable}"
            if self.db_port_base
            else "not assigned (set --db-port-base)"
        )
        return [
            f"compose stack: {stack}; database port {port}",
            "push hook runs the full suite: "
            + (
                "yes (workers stop at code-level gates; the harness pushes)"
                if self.push_hook_runs_full_suite
                else "no"
            ),
            f"scoped gate: {self.sourced('local_gate')}; "
            f"full suite: {self.sourced('full_suite_command')}; "
            f"full suite owner: {self.full_suite_owner}"
            + (f" ({self.full_suite_owner_source})" if self.full_suite_owner_source else ""),
            f"evidence directory: {self.evidence_dir} (inside each worktree, excluded from git)",
            "database URL templates: "
            + (
                f"DATABASE_URL={self.db_url_template or 'not set'}, "
                f"TEST_DATABASE_URL={self.test_db_url_template or 'not set'}"
            ),
            f"source line ceiling: {self.source_line_ceiling or 'not set'}",
            "elevated localhost path: "
            + ("needed" if self.needs_elevated_localhost else "not set"),
            "auto merge: "
            + (
                f"yes ({self.merge_method}, once green and unmerged past "
                "delivery.merge_after_hours)"
                if self.auto_merge
                else "no"
            ),
        ]


def for_repo(row) -> RepoEnvironment:
    """A registered repository's environment, from its row. Unset reads as the default."""
    name = _cell(row, "name") or ""
    evidence = str(_cell(row, "evidence_dir") or "").strip()
    owner = str(_cell(row, "full_suite_owner") or "").strip()
    gate = str(_cell(row, "local_gate") or "").strip()
    stack = str(_cell(row, "compose_stack") or "").strip()
    base = _cell(row, "db_port_base")
    variable = str(_cell(row, "db_port_variable") or "").strip()
    db_url_template = str(_cell(row, "db_url_template") or "").strip()
    test_db_url_template = str(_cell(row, "test_db_url_template") or "").strip()
    source_line_ceiling = _cell(row, "source_line_ceiling")
    return RepoEnvironment(
        repo=name,
        compose_stack=stack or None,
        db_port_base=int(base) if base else None,
        db_port_variable=variable or DB_PORT_VARIABLE,
        push_hook_runs_full_suite=bool(_cell(row, "push_hook_runs_full_suite")),
        local_gate=gate or None,
        full_suite_owner=owner or DEFAULT_FULL_SUITE_OWNER,
        full_suite_command=str(_cell(row, "full_suite_command") or "").strip() or None,
        evidence_dir=evidence.strip("/") or DEFAULT_EVIDENCE_DIR,
        db_url_template=db_url_template or None,
        test_db_url_template=test_db_url_template or None,
        source_line_ceiling=int(source_line_ceiling) if source_line_ceiling else None,
        needs_elevated_localhost=bool(_cell(row, "needs_elevated_localhost")),
        auto_merge=bool(_cell(row, "auto_merge")),
        merge_method=str(_cell(row, "merge_method") or "").strip() or DEFAULT_MERGE_METHOD,
        **{
            source_column(answer): str(_cell(row, source_column(answer)) or "").strip() or None
            for answer in GATE_ANSWERS
        },
    )


def set_fields(
    conn: sqlite3.Connection, name: str, *, sources: dict[str, str] | None = None, **values
) -> None:
    """Store the environment columns ``ppy repo set`` passed. Only named ones change.

    A gate answer is stored with its source: ``sources[answer]`` when the caller read
    it from the repository, otherwise :data:`SOURCE_PERSON`. Clearing one clears its
    source too.
    """
    fields = {}
    for answer in GATE_ANSWERS:
        if values.get(answer) is not None:
            given = str(values[answer]).strip()
            fields[source_column(answer)] = (
                ((sources or {}).get(answer) or SOURCE_PERSON) if given else None
            )
    for key, value in values.items():
        if key not in REPO_COLUMNS:
            raise RepoEnvironmentError(f"{key!r} is not an environment setting")
        if value is None:
            continue
        if key == "compose_stack":
            fields[key] = parse_compose_stack(value)
        elif key == "db_port_base":
            fields[key] = parse_port_base(value)
        elif key == "source_line_ceiling":
            fields[key] = parse_source_line_ceiling(value)
        elif key in ("push_hook_runs_full_suite", "needs_elevated_localhost", "auto_merge"):
            parsed = parse_bool(value)
            fields[key] = 1 if parsed else None
        elif key == "merge_method":
            method = str(value).strip().lower()
            if method and method not in MERGE_METHODS:
                raise RepoEnvironmentError(
                    f"--merge-method must be one of {', '.join(MERGE_METHODS)}, got {value!r}"
                )
            fields[key] = method or None
        elif key in ("db_url_template", "test_db_url_template"):
            fields[key] = parse_url_template(value, "--" + key.replace("_", "-"))
        elif key == "evidence_dir":
            fields[key] = str(value).strip().strip("/") or None
        else:
            fields[key] = str(value).strip() or None
    if fields:
        store.update_repo_fields(conn, name, **fields)


# --------------------------------------------------------------------------- #
# Per-task assignment
# --------------------------------------------------------------------------- #


def compose_project_for(task_id: int) -> str:
    """The per-task compose project: the shape teardown recognises as the task's own."""
    return f"task_{task_id}"


def derive_port(base: int | None, task_id: int) -> int | None:
    """A host port for this task: ``base + task_id``, wrapped back into range.

    Deterministic (same task, same port, every dispatch and every resume) and
    unique per task as long as fewer than ``65535 - base`` tasks are in flight
    at once, which is every machine this runs on.
    """
    if not base:
        return None
    span = MAX_PORT - base
    return base + (task_id % (span + 1))


@dataclass
class PreparedEnvironment:
    """What dispatch settled for one task, rendered and recorded."""

    block: str
    evidence_path: str
    compose_project: str | None = None
    db_port: int | None = None
    process_env: dict[str, str] | None = None


def task_cache_dir(task_id: int | str) -> Path:
    """Writable cache root private to one task's (or one gate scope's) processes."""
    return cache_dir() / "tasks" / str(task_id)


def _resolved_variables(
    env: RepoEnvironment,
    *,
    task_id: int | str,
    compose_project: str | None,
    db_port: int | None,
) -> dict[str, str]:
    variables: dict[str, str] = {}
    if compose_project:
        variables["COMPOSE_PROJECT_NAME"] = compose_project
        if db_port is not None:
            variables[env.db_port_variable] = str(db_port)
        values = {"port": db_port or "", "name": env.repo, "task_id": task_id}
        if env.db_url_template:
            variables["DATABASE_URL"] = env.db_url_template.format(**values)
        if env.test_db_url_template:
            variables["TEST_DATABASE_URL"] = env.test_db_url_template.format(**values)
    variables.update(
        {
            "UV_CACHE_DIR": str(uv_cache_dir()),
            "RUFF_CACHE_DIR": str(task_cache_dir(task_id) / "ruff"),
            "MYPY_CACHE_DIR": str(task_cache_dir(task_id) / "mypy"),
        }
    )
    return variables


def task_process_env(conn: sqlite3.Connection, repo_row, task_id: int) -> dict[str, str]:
    """Resolved values the supervisor and ``ppy receipt`` put in a task process."""
    env = for_repo(repo_row)
    project = store.get_task_env(conn, task_id, compose.COMPOSE_PROJECT_KEY)
    raw_port = store.get_task_env(conn, task_id, DB_PORT_KEY)
    return _resolved_variables(
        env,
        task_id=task_id,
        compose_project=project if env.compose_stack else None,
        db_port=int(raw_port) if env.compose_stack and raw_port else None,
    )


def render_task_env(conn: sqlite3.Connection, repo_row, task_id: int) -> dict[str, str]:
    """A task's resolved process values for a process that has no runner: a gate.

    What :func:`task_process_env` reads, except that a compose repository's project and
    port are settled here when dispatch did not record them (the stack was declared
    after the task was dispatched), exactly as dispatch would have, and recorded, so
    teardown finds the stack a gate brought up. Without this a gate for such a task
    ran against the repository's default database (issue #47).
    """
    env = for_repo(repo_row)
    project = port = None
    if env.compose_stack:
        project = store.get_task_env(conn, task_id, compose.COMPOSE_PROJECT_KEY)
        if not project:
            project = compose.record_project(
                task_id, compose_project_for(task_id), source="gate", conn=conn
            )
        raw_port = store.get_task_env(conn, task_id, DB_PORT_KEY)
        port = int(raw_port) if raw_port else derive_port(env.db_port_base, task_id)
        if port is not None and not raw_port:
            store.set_task_env(conn, task_id, DB_PORT_KEY, str(port), source="gate")
    return _resolved_variables(env, task_id=task_id, compose_project=project, db_port=port)


def free_port() -> int:
    """A host port nothing is listening on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def scope_process_env(repo_row, scope: str) -> dict[str, str]:
    """Process values private to a gate that belongs to no task (a baseline, a base clone).

    ``scope`` stands in for the task: it is the compose project and the ``{task_id}`` in
    the database URL templates. The port is one nothing holds now, since no task id
    derives it.
    """
    env = for_repo(repo_row)
    project = scope if env.compose_stack else None
    port = free_port() if project and env.db_port_base else None
    return _resolved_variables(env, task_id=scope, compose_project=project, db_port=port)


def database_name(values: dict[str, str]) -> str | None:
    """The database a process with these values runs its tests against, when a URL says."""
    for key in ("TEST_DATABASE_URL", "DATABASE_URL"):
        name = urlsplit(values.get(key) or "").path.lstrip("/")
        if name:
            return name
    return None


def isolation_gaps(env: RepoEnvironment, *, has_compose_file: bool) -> list[str]:
    """Why two gates on this repository could share a database; empty when they cannot.

    A repository with a compose file but no declared stack gives every gate the
    repository's default stack; a declared one needs a port base, so each task's stack
    listens on its own port, and URL templates carrying ``{task_id}``, so each task's
    database has its own name.
    """
    if not env.compose_stack:
        return ["ships a compose file but declares no compose stack"] if has_compose_file else []
    gaps = []
    if not env.db_port_base:
        gaps.append("no database port base")
    templates = [t for t in (env.db_url_template, env.test_db_url_template) if t]
    if not templates:
        gaps.append("no database URL template")
    elif any("{task_id}" not in template for template in templates):
        gaps.append("a database URL template without {task_id}")
    return gaps


def finish_instruction(task_id: int | str, ends_at: str) -> str:
    if ends_at == "review":
        return (
            f'stop at `ppy progress {task_id} --phase review --note "..."`; do not call done. '
            "The manager reviews and delivers."
        )
    return f'finish with `ppy progress {task_id} --phase done --note "..."`.'


def ensure_excluded(worktree: str | Path, evidence_dir: str) -> bool:
    """Add ``evidence_dir`` to the repository's ``info/exclude`` so it never ships.

    ``info/exclude`` lives in the common git dir, so one line covers the base clone
    and every worktree leased off it. Idempotent; a worktree git cannot read is
    left alone (the block still names the directory, and the auto-commit's own
    exclusion list holds it back regardless).
    """
    pattern = f"/{evidence_dir.strip('/')}/"
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--git-path", "info/exclude"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return False
        target = Path(proc.stdout.strip())
        if not target.is_absolute():
            target = Path(worktree) / target
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        if pattern in existing.splitlines():
            return True
        with target.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(f"{pattern}\n")
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def prepare(
    conn: sqlite3.Connection,
    repo_row,
    *,
    task_id: int,
    worktree: str,
    branch: str | None,
    ends_at: str = "done",
) -> PreparedEnvironment:
    """Settle the environment for a task at dispatch and render its block.

    A repository with a compose stack gets its project and port recorded on the
    task here (``source="dispatch"``), through the same keys ``ppy task env set``
    writes and teardown reads. Nothing here raises for a repository that declared
    nothing: every task gets at least the evidence directory and the local/CI
    split.
    """
    env = for_repo(repo_row)
    project = port = None
    if env.compose_stack:
        project = compose.record_project(
            task_id, compose_project_for(task_id), source="dispatch", conn=conn
        )
        port = derive_port(env.db_port_base, task_id)
        if port is not None:
            store.set_task_env(conn, task_id, DB_PORT_KEY, str(port), source="dispatch")
    ensure_excluded(worktree, env.evidence_dir)
    evidence_path = str(Path(worktree) / env.evidence_dir)
    process_env = _resolved_variables(env, task_id=task_id, compose_project=project, db_port=port)
    from papaya_agent_runtime import budgets

    block = render(
        env,
        task_id=task_id,
        evidence_path=evidence_path,
        compose_project=project,
        db_port=port,
        branch=branch,
        ends_at=ends_at,
        process_env=process_env,
        gate_timing=budgets.gate_timing_line(env.repo, conn=conn),
    )
    return PreparedEnvironment(
        block=block,
        evidence_path=evidence_path,
        compose_project=project,
        db_port=port,
        process_env=process_env,
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render(
    env: RepoEnvironment,
    *,
    task_id: int,
    evidence_path: str,
    compose_project: str | None = None,
    db_port: int | None = None,
    branch: str | None = None,
    ends_at: str = "done",
    process_env: dict[str, str] | None = None,
    gate_timing: str | None = None,
) -> str:
    """The block itself. Markdown, one bullet per fact, no prose to drift.

    ``gate_timing`` is how long this repository's gates have actually taken
    (:func:`budgets.gate_timing_line`), said only when there is history behind it.
    """
    lines = [f"## {HEADING}", ""]
    lines.append(
        f"- **Evidence directory: `{evidence_path}/`** — inside your worktree and excluded "
        "from git. Receipts are `.txt` files. Create it with your own commands and write "
        "every receipt there (gate "
        "output, screenshots, dumps), then name the paths in your final progress report; "
        "`ppy review show` lists that directory for the reviewer. Never write receipts "
        "under `/private/tmp`: that path is not reachable from your shell, and tests "
        "never write receipts at all (they use `tmp_path`)."
    )
    lines.append(f"- **Three tiers:** {prompts.GATE_TIERS_RULE}")
    lines.append(
        "- **Targeted checks:** the repository says how to test what you change (its "
        "`AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `Makefile` or package scripts); pick the "
        "tests nearest your diff from that, and run them as often as you like."
    )
    if env.local_gate:
        scoped = f"{env.sourced('local_gate')}"
    else:
        scoped = "not recorded for this repository (the brief quotes the repository's own)"
    lines.append(
        f"- **Scoped gate: {scoped}.** That is what you run before handing the task back, in "
        "the foreground, waiting for it to finish: never in the background, and never end "
        "your session with it still running, because a backgrounded command dies with the "
        "session and its result is lost."
    )
    full = env.sourced("full_suite_command") if env.full_suite_command else "not recorded"
    owner = (
        "CI runs it on your pull request"
        if not env.supervisor_runs_full_suite
        else "the supervisor runs it once, at the head that will be delivered"
    )
    lines.append(
        f"- **Full suite: {full}.** {owner}. Do not run it here: "
        f"{command_rules_full_suite_refusal()} A run that outlasts your tool timeout is "
        "killed part-way and can leave the database poisoned for the next run."
    )
    lines.append(
        f"- **Gates longer than a tool call:** {prompts.TEN_MINUTE_RULE} "
        f"`ppy gate run --task {task_id}` runs the scoped gate as the supervisor's own "
        "process, prints a progress line every minute, and records the result against your "
        "head commit; the manager reads that record, not your note. If it answers that the "
        "gate is still running, run the same command again: it attaches to the run already "
        "going rather than starting another."
    )
    if gate_timing:
        lines.append(
            f"- **How long gates take here:** {gate_timing}. Plan your waits around that, "
            "and use `ppy gate run` for anything near or past ten minutes."
        )
    push_to = f" (`{branch}`)" if branch else ""
    lines.append(
        f"- **Your work reaches the remote as you go:** {prompts.PUSH_MILESTONE_RULE} "
        f"A restart strands whatever is only in this worktree; the lease branch{push_to} is "
        "what survives it, and what the reviewer reads."
    )
    lines.append(f"- **After delivery:** {prompts.PR_FOLLOW_RULE}")
    resolved = process_env or _resolved_variables(
        env, task_id=task_id, compose_project=compose_project, db_port=db_port
    )
    if env.local_gate:
        assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in resolved.items())
        lines.append(f"- **Exact scoped gate:** `{assignments} {env.local_gate}`")
    if compose_project:
        port_text = (
            f" and host port **{db_port}** (`{env.db_port_variable}`)"
            if db_port is not None
            else ""
        )
        file_flag = f" -f {env.compose_file}" if env.compose_file else ""
        overrides = f"COMPOSE_PROJECT_NAME={compose_project}"
        pin = [f"COMPOSE_PROJECT_NAME={compose_project}"]
        if db_port is not None:
            overrides += f" {env.db_port_variable}={db_port}"
            pin.append(f"{env.db_port_variable}={db_port}")
        lines.append(
            f"- **Private database stack: compose project `{compose_project}`{port_text}.** "
            "This task owns that stack and nothing else; the shared container other "
            "worktrees use is not yours to start, stop, or recreate. Every `make` or "
            "`docker compose` call that touches the database carries these values:"
        )
        lines.append(f"    - `make <target> {overrides}` (make variable overrides);")
        lines.append(
            f"    - `docker compose -p {compose_project}{file_flag} up -d` "
            "(and the same `-p` on every other compose command);"
        )
        lines.append(
            "    - pin them in the worktree's root `.env` so anything that reads it agrees: "
            + ", ".join(f"`{line}`" for line in pin)
            + "."
        )
        lines.append(
            "  Never run `docker compose down` on any other project name. The harness takes "
            "this stack down when the task is delivered or closed."
        )
        if env.db_url_template or env.test_db_url_template:
            url_parts = [
                f"`{key}={value}`"
                for key, value in resolved.items()
                if key in ("DATABASE_URL", "TEST_DATABASE_URL")
            ]
            lines.append("- **Resolved database URLs:** " + ", ".join(url_parts) + ".")
        else:
            lines.append(
                "- **Database URL templates are not registered.** `DATABASE_URL` and "
                "`TEST_DATABASE_URL` are absent from the worker process."
            )
    if env.needs_elevated_localhost:
        lines.append(
            "- **Sandbox:** localhost database access and Git object writes may require the "
            "elevated execution path registered for this repository."
        )
    if env.source_line_ceiling:
        lines.append(
            f"- **Source ceiling:** no source file may exceed {env.source_line_ceiling} lines; "
            "run the repository's harness check before the scoped gate."
        )
    if env.push_hook_runs_full_suite:
        target = f"`{branch}`" if branch else "your lease branch"
        ending = (
            "Stop at the code-level gates (the scoped gate, lint, format), commit, and "
            + finish_instruction(task_id, ends_at)
            if ends_at == "review"
            else "Stop at the code-level gates (the scoped gate, lint, format), commit, and file "
            f'your done report with `ppy progress {task_id} --phase done --note "..."` naming the '
            "head SHA from `git rev-parse HEAD`."
        )
        delivery = (
            f"The manager reviews and delivers from {target}."
            if ends_at == "review"
            else f"The runtime then pushes {target} itself, once its own gate is green at "
            f"your exact head, and records it; `ppy task push {task_id}` is the same push "
            "by hand."
        )
        lines.append(
            "- **Push hook: this repository gates pushes with its own hook, which runs the "
            "full suite, so do not push.** The runtime pushes outside the harness, where "
            "that hook does not apply, so nothing about it is skipped or weakened for you. "
            f"{ending} {delivery}"
        )
    elif ends_at == "review":
        lines.append(
            "- **Review handoff:** commit, do not push, and " + finish_instruction(task_id, ends_at)
        )
    return "\n".join(lines) + "\n"


def denied_tools(repo_row) -> list[str]:
    """What a worker in this repository is refused on top of its allowlist."""
    from papaya_agent_runtime.providers.command_rules import denied_tools as patterns

    return patterns(for_repo(repo_row).full_suite_command) if repo_row is not None else []


def push_is_gated(repo_row, worktree: str | None = None) -> bool:
    """Does this repository stop a worker's own `git push`?

    Two sources, and either is enough. The recorded one
    (``repos.push_hook_runs_full_suite``) is what onboarding read from the
    repository; the observed one is a ``PreToolUse`` hook on ``Bash`` registered in
    the worktree, which is what actually refused every push in issues #83 and #116.

    When it is true the worker is told not to push and the runtime pushes the lease
    branch itself after its own gate. Telling the worker to push anyway is telling it
    to be refused — the command rules and the environment block used to say opposite
    things, and the worker followed the rules.
    """
    if repo_row is not None and for_repo(repo_row).push_hook_runs_full_suite:
        return True
    from papaya_agent_runtime.providers.claude import registered_hooks

    return bool(registered_hooks(worktree, "Bash"))


def command_rules_full_suite_refusal() -> str:
    from papaya_agent_runtime.providers.command_rules import FULL_SUITE_REFUSAL

    return FULL_SUITE_REFUSAL


def evidence_path_for(repo_row, worktree: str | None) -> str | None:
    """Where this task's receipts are, for the reviewer. None without a worktree."""
    if not worktree:
        return None
    return str(Path(worktree) / for_repo(repo_row).evidence_dir)
