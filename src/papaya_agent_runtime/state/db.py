"""SQLite schema and connection management.

The schema is created idempotently and versioned via ``PRAGMA user_version`` so
future milestones can migrate forward. WAL mode and foreign keys are enabled for
concurrent supervisor/runner access.
"""

from __future__ import annotations

import math
import os
import sqlite3
from pathlib import Path

from papaya_agent_runtime.paths import db_path

SCHEMA_VERSION = 24

# Five seconds is SQLite's driver default, but this runtime has a supervisor,
# guardian threads, and worker processes writing concurrently. Thirty seconds
# rides out a slow writer on a loaded machine while still surfacing a real lock
# leak promptly instead of leaving an operator waiting indefinitely.
SQLITE_BUSY_TIMEOUT_SECONDS = 30.0
# Environment values use seconds to match sqlite3.connect; connect() converts
# that same value to whole milliseconds for SQLite's PRAGMA.
SQLITE_BUSY_TIMEOUT_ENV = "PPY_SQLITE_BUSY_TIMEOUT"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    origin TEXT NOT NULL,
    local_path TEXT NOT NULL,
    default_branch TEXT,
    -- Set by `ppy repo set --default-branch`: the forge's HEAD no longer decides.
    default_branch_locked INTEGER,
    base_sha TEXT,
    forge_url TEXT,
    -- Opt-in, per repo: a command to run in a fresh worktree before the worker
    -- starts, and a path to a virtualenv in the base clone worth reusing.
    provision_command TEXT,
    provision_venv TEXT,
    -- Where this repo keeps its database migrations, so dispatch and review can
    -- see two tasks adding one off the same head. Unset means the default glob.
    migrations_glob TEXT,
    -- The environment block a worker is handed at dispatch (issue #60): whether
    -- this repo runs a per-task compose stack (and which file), the base the
    -- task's database port is derived from, whether the pre-push hook runs the
    -- full suite, the scoped local gate, who owns the full suite, and where
    -- receipts go inside the worktree. All optional; unset reads as the default.
    compose_stack TEXT,
    db_port_base INTEGER,
    -- The variable name the port travels under in make overrides and the root
    -- .env (default DB_PORT; papaya-backend uses PAPAYA_DB_PORT).
    db_port_variable TEXT,
    push_hook_runs_full_suite INTEGER,
    local_gate TEXT,
    full_suite_owner TEXT,
    -- The command `ppy gate run --full` runs (the repository declares it).
    full_suite_command TEXT,
    -- Where each gate answer came from: `<file>:<line>` the repository says it in,
    -- `person` (`ppy repo set`), or `heuristic` (the old derivation, cleared at start).
    local_gate_source TEXT,
    full_suite_command_source TEXT,
    full_suite_owner_source TEXT,
    evidence_dir TEXT,
    db_url_template TEXT,
    test_db_url_template TEXT,
    source_line_ceiling INTEGER,
    needs_elevated_localhost INTEGER,
    -- Opt-in, per repo: the runtime merges a delivered pull request that has sat green,
    -- mergeable and unrequested for `delivery.merge_after_hours`, with this method.
    auto_merge INTEGER,
    merge_method TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    objective TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    repo_id INTEGER REFERENCES repos(id),
    role TEXT NOT NULL DEFAULT 'implementer',
    title TEXT NOT NULL,
    contract_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'requested',
    provider TEXT,
    model TEXT,
    reasoning TEXT,
    base_sha TEXT,
    branch TEXT,
    worktree_path TEXT,
    lease_id TEXT,
    stacked_on TEXT,
    stacked_on_task INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    merged_sha TEXT,
    merged_at TEXT,
    ends_at TEXT NOT NULL DEFAULT 'done',
    -- Where this task is in the *manager's* handling of it, which is not the same
    -- question as `status`. A ticket picked up by `ppy serve` is held for as long
    -- as its lease lasts and has no worker yet; `phase` is what that hold says.
    phase TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Small facts a worker or the manager attaches to a task that the schema does not
-- model: the name of the compose stack the task brought up, and whatever comes
-- next. Read at teardown, so a task's side effects can be found after it ends.
CREATE TABLE IF NOT EXISTS task_env (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (task_id, key)
);

CREATE TABLE IF NOT EXISTS task_deps (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on)
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_session_id TEXT,
    start_token TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    acked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events(run_id, seq);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    rationale TEXT,
    scope TEXT NOT NULL DEFAULT 'task',
    author TEXT NOT NULL DEFAULT 'user',
    standing INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT,
    context TEXT,
    invalidated_at TEXT,
    superseded_by INTEGER REFERENCES decisions(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT,
    reasoning TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    base_sha TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    verdict TEXT NOT NULL,
    findings TEXT,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    session_state TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
    id TEXT PRIMARY KEY,
    repo_id INTEGER REFERENCES repos(id) ON DELETE SET NULL,
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    branch TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    base_sha TEXT,
    backend TEXT NOT NULL DEFAULT 'git',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    released_at TEXT
);

CREATE TABLE IF NOT EXISTS runners (
    id TEXT PRIMARY KEY,
    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    pid INTEGER,
    provider TEXT,
    session_id TEXT,
    status TEXT NOT NULL DEFAULT 'starting',
    started_at TEXT NOT NULL,
    heartbeat_at TEXT,
    exit_code INTEGER,
    result_recorded INTEGER NOT NULL DEFAULT 0,
    superseded_at TEXT,
    supersede_reason TEXT
);

CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    blocked_on TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    done_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status, position, id);

-- What is waiting on a person and where it has been said (`outreach.py`): a
-- decision recorded against a task, a capability request policy left to a person,
-- a pull request the reconcile lane gave up on. One row per open ask; `said_at`
-- and `said_count` are what stop it being said twice in a round and what make
-- every repeat say which reminder it is. Resolved rows stay as the record.
CREATE TABLE IF NOT EXISTS outreach (
    key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    task_id INTEGER,
    work_item_id TEXT,
    text TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    said_at TEXT,
    said_count INTEGER NOT NULL DEFAULT 0,
    said_via TEXT,
    said_fingerprint TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_outreach_open ON outreach(resolved_at, first_seen_at);

-- The newest comment (or event) timestamp the manager has processed on an
-- external record — a ticket URL or id — so a sweep asks only for what is newer
-- and an empty sweep costs one list call, not a re-read. Same shape `ppy watch`
-- keeps per pull request, for records the harness does not own.
CREATE TABLE IF NOT EXISTS watermarks (
    key TEXT PRIMARY KEY,
    watermark TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    note TEXT
);

-- Which readiness verdicts the connection owner has already been told about, so a
-- runtime that cannot work says so once rather than on every wake. Keyed on the set
-- of problems, not the clock: the same problem tomorrow is not news, a different one
-- is however soon it appears.
CREATE TABLE IF NOT EXISTS readiness_reports (
    fingerprint TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    reported_at TEXT NOT NULL
);

-- How long things have actually taken, per repository: a gate run, a worker
-- session, a plan phase, the silence between two progress notes, a manager turn,
-- a CI run. Written where each one ends, never sampled; `budgets.py` derives every
-- per-repo wait from it. `outcome` `stall` or `kill` keeps a row out of the derivation.
CREATE TABLE IF NOT EXISTS repo_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    kind TEXT NOT NULL,
    seconds REAL NOT NULL,
    at TEXT NOT NULL,
    task_id INTEGER,
    outcome TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_repo_observations ON repo_observations(repo, kind, at);

-- A person's word on a budget (`ppy repo set <name> --budget <kind>=<seconds>`): it
-- wins over whatever the observations derive.
CREATE TABLE IF NOT EXISTS repo_budget_overrides (
    repo TEXT NOT NULL,
    kind TEXT NOT NULL,
    seconds REAL NOT NULL,
    set_at TEXT NOT NULL,
    PRIMARY KEY (repo, kind)
);

-- Structural deficiencies the runtime found in itself (`deficiencies.py`): one row per
-- fingerprint (kind + normalised detail), with every occurrence's redacted evidence and
-- the GitHub issue `ppy serve` opened about it. Never the user's code.
CREATE TABLE IF NOT EXISTS deficiencies (
    fingerprint TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    evidence TEXT NOT NULL DEFAULT '[]',
    issue_url TEXT,
    -- watching (below its threshold), pending (due an issue), reported (has one)
    status TEXT NOT NULL DEFAULT 'watching',
    opened_at TEXT,
    -- How many occurrences the issue and its comments already carry.
    reported_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_deficiencies_status ON deficiencies(status, first_seen);

CREATE TABLE IF NOT EXISTS assessment_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    window_start TEXT,
    window_end TEXT NOT NULL,
    evidence TEXT NOT NULL,
    summary TEXT,
    strengths TEXT,
    weaknesses TEXT,
    user_response TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    aligned_at TEXT
);

CREATE TABLE IF NOT EXISTS improvement_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id INTEGER NOT NULL REFERENCES assessment_cycles(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'practice',
    observation TEXT,
    likely_cause TEXT,
    baseline TEXT,
    target TEXT,
    measurement TEXT,
    requires_approval INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'proposed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assessment_cycles_status
    ON assessment_cycles(status, created_at);
CREATE INDEX IF NOT EXISTS idx_improvement_actions_assessment
    ON improvement_actions(assessment_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_assessment
    ON assessment_cycles ((status IN ('ready', 'awaiting_user')))
    WHERE status IN ('ready', 'awaiting_user');
"""


def _busy_timeout_seconds() -> float:
    configured = os.environ.get(SQLITE_BUSY_TIMEOUT_ENV)
    if configured is None:
        return SQLITE_BUSY_TIMEOUT_SECONDS
    try:
        seconds = float(configured)
    except ValueError:
        return SQLITE_BUSY_TIMEOUT_SECONDS
    if not math.isfinite(seconds) or seconds <= 0 or round(seconds * 1000) <= 0:
        return SQLITE_BUSY_TIMEOUT_SECONDS
    return seconds


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    timeout_seconds = _busy_timeout_seconds()
    conn = sqlite3.connect(str(p), timeout=timeout_seconds)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # The driver timeout installs a busy handler, while the pragma also covers
    # SQLite operations whose busy handler may be replaced after connection.
    conn.execute(f"PRAGMA busy_timeout = {round(timeout_seconds * 1000)}")
    return conn


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn: sqlite3.Connection) -> None:
    """Forward-only column additions for databases created by earlier versions.

    ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so new columns
    on a pre-existing DB are added here. Additions are idempotent (guarded by
    ``PRAGMA table_info``) so this is safe to run on fresh databases too.
    """
    repo_cols = _column_names(conn, "repos")
    if "forge_url" not in repo_cols:
        # Where pull requests are opened. A repo registered from a local path has
        # a local `origin`, which is no forge at all: delivery could push but had
        # nowhere to open the PR (2026-09-04, papaya-infra).
        conn.execute("ALTER TABLE repos ADD COLUMN forge_url TEXT")
    task_cols = _column_names(conn, "tasks")
    if "stacked_on" not in task_cols:
        # The branch a stacked task started from; delivery targets it by default.
        conn.execute("ALTER TABLE tasks ADD COLUMN stacked_on TEXT")
    if "stacked_on_task" not in task_cols:
        # The task this one is stacked on. A stack is a chain of tasks, not a
        # chain of branch names the manager has to remember.
        conn.execute("ALTER TABLE tasks ADD COLUMN stacked_on_task INTEGER")
    if "ends_at" not in task_cols:
        # The phase at which the worker hands control back. Existing tasks retain
        # the historical behaviour: workers finish with a done report.
        conn.execute("ALTER TABLE tasks ADD COLUMN ends_at TEXT NOT NULL DEFAULT 'done'")
    if "phase" not in task_cols:
        # How far `ppy serve` has got with the ticket behind this task. Nullable
        # rather than defaulted: a task that predates the manager was never picked
        # up by it, and "no phase" is the honest answer for it.
        conn.execute("ALTER TABLE tasks ADD COLUMN phase TEXT")
    for col in ("merged_sha", "merged_at"):
        # Where the work landed. Set once, terminal: the heartbeat stops asking
        # the forge about a branch whose merge is already on the record.
        if col not in task_cols:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
    runner_cols = _column_names(conn, "runners")
    for col in ("superseded_at", "supersede_reason"):
        # A runner superseded by a resume must not stamp the task when it finally
        # exits: its late exit code belongs to a session nobody is waiting on.
        if col not in runner_cols:
            conn.execute(f"ALTER TABLE runners ADD COLUMN {col} TEXT")
    repo_cols = _column_names(conn, "repos")
    for col in ("provision_command", "provision_venv"):
        # Opt-in worktree provisioning; a repo with neither set behaves as before.
        if col not in repo_cols:
            conn.execute(f"ALTER TABLE repos ADD COLUMN {col} TEXT")
    if "migrations_glob" not in repo_cols:
        # Where this repo's migrations live. Unset reads as the default glob, so
        # every already-registered repo gets the collision advisory for free.
        conn.execute("ALTER TABLE repos ADD COLUMN migrations_glob TEXT")
    for col, kind in (
        ("compose_stack", "TEXT"),
        ("db_port_base", "INTEGER"),
        ("db_port_variable", "TEXT"),
        ("push_hook_runs_full_suite", "INTEGER"),
        ("local_gate", "TEXT"),
        ("full_suite_owner", "TEXT"),
        ("full_suite_command", "TEXT"),
        ("evidence_dir", "TEXT"),
        ("db_url_template", "TEXT"),
        ("test_db_url_template", "TEXT"),
        ("source_line_ceiling", "INTEGER"),
        ("needs_elevated_localhost", "INTEGER"),
        ("auto_merge", "INTEGER"),
        ("merge_method", "TEXT"),
        ("default_branch_locked", "INTEGER"),
        ("local_gate_source", "TEXT"),
        ("full_suite_command_source", "TEXT"),
        ("full_suite_owner_source", "TEXT"),
    ):
        # The per-repo environment block (issue #60). Unset means the default, so
        # every already-registered repo gets the evidence-directory and local/CI
        # lines for free and declares a compose stack or a push hook when it has one.
        if col not in repo_cols:
            conn.execute(f"ALTER TABLE repos ADD COLUMN {col} {kind}")
    review_cols = _column_names(conn, "reviews")
    if "note" not in review_cols:
        # The reviewer's own words, bound to the SHA they read; delivery quotes them.
        conn.execute("ALTER TABLE reviews ADD COLUMN note TEXT")
    decision_cols = _column_names(conn, "decisions")
    for col in ("context", "invalidated_at"):
        if col not in decision_cols:
            conn.execute(f"ALTER TABLE decisions ADD COLUMN {col} TEXT")
    if "improvement_actions" in {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }:
        action_cols = _column_names(conn, "improvement_actions")
        for col in ("observation", "likely_cause", "measurement"):
            if col not in action_cols:
                conn.execute(f"ALTER TABLE improvement_actions ADD COLUMN {col} TEXT")
    if "said_fingerprint" not in _column_names(conn, "outreach"):
        # What an ask said when it was last said: unchanged, it is never said again.
        conn.execute("ALTER TABLE outreach ADD COLUMN said_fingerprint TEXT")
    conn.commit()


def init_db(path: Path | None = None) -> sqlite3.Connection:
    """Create the schema idempotently, migrate, and stamp the version."""
    conn = connect(path)
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])
