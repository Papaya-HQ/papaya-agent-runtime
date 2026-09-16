"""Thin data-access helpers over the SQLite schema.

Kept deliberately small: milestone M1 needs repos, runs, tasks, and the event
stream. Later milestones extend this module rather than reaching into SQL from
across the codebase.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import UTC, datetime


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# Repositories
# --------------------------------------------------------------------------- #


def add_repo(
    conn: sqlite3.Connection,
    *,
    name: str,
    origin: str,
    local_path: str,
    default_branch: str | None,
    base_sha: str | None,
    forge_url: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO repos
            (name, origin, local_path, default_branch, base_sha, forge_url, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (name, origin, local_path, default_branch, base_sha, forge_url, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_repo(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM repos WHERE name = ?", (name,)).fetchone()


def list_repos(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM repos ORDER BY name").fetchall())


def update_repo_base_sha(conn: sqlite3.Connection, name: str, base_sha: str) -> None:
    conn.execute("UPDATE repos SET base_sha = ? WHERE name = ?", (base_sha, name))
    conn.commit()


def update_repo_fields(conn: sqlite3.Connection, name: str, **fields: object) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE repos SET {cols} WHERE name = ?", [*fields.values(), name])
    conn.commit()


# --------------------------------------------------------------------------- #
# Runs and tasks
# --------------------------------------------------------------------------- #


def create_run(conn: sqlite3.Connection, objective: str) -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO runs (objective, status, created_at, updated_at) VALUES (?,?,?,?)",
        (objective, "requested", now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def set_run_status(conn: sqlite3.Connection, run_id: int, status: str) -> None:
    conn.execute(
        "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
        (status, _now(), run_id),
    )
    conn.commit()


def add_task(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    title: str,
    repo_id: int | None = None,
    role: str = "implementer",
    provider: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
    ends_at: str = "done",
) -> int:
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO tasks
            (run_id, repo_id, role, title, status, provider, model, reasoning, ends_at,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, 'requested', ?, ?, ?, ?, ?, ?)
        """,
        (run_id, repo_id, role, title, provider, model, reasoning, ends_at, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def list_tasks(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM tasks WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    )


def set_task_status(conn: sqlite3.Connection, task_id: int, status: str) -> None:
    conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
        (status, _now(), task_id),
    )
    conn.commit()


#: The phases `ppy serve` records while it holds a ticket's lease. The first seven
#: are how far the work has got, in the order a ticket normally passes through
#: them (`blocked` is a detour off `dispatched` and back). The last four are how
#: the hold ended, in the client's own vocabulary rather than a second one
#: invented here — the reason on `Job.stop` chooses between the first three.
TASK_PHASES = (
    "picked_up",
    "briefing",
    "dispatched",
    "blocked",
    "reviewing",
    "delivering",
    "reported",
    "released",
    "handed_back",
    "stalled",
    "declined",
)

#: The event kind every phase change is also written as, so the order a ticket
#: went through its phases survives the column only holding the latest one.
TICKET_PHASE_EVENT = "ticket_phase"


def set_task_phase(conn: sqlite3.Connection, task_id: int, phase: str) -> None:
    """Record how far the manager has got with this task's ticket.

    Deliberately separate from :func:`set_task_status`: `status` is the lifecycle
    the control plane enforces (requested, in_progress, delivered), and a ticket
    held by `ppy serve` has not entered it yet. Writing the two independently is
    what lets a held ticket say what it is doing without claiming a worker.
    """
    conn.execute(
        "UPDATE tasks SET phase = ?, updated_at = ? WHERE id = ?",
        (phase, _now(), task_id),
    )
    conn.commit()


def task_phase(conn: sqlite3.Connection, task_id: int) -> str | None:
    row = conn.execute("SELECT phase FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row["phase"] if row else None


def update_task_fields(conn: sqlite3.Connection, task_id: int, **fields: object) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = [*fields.values(), _now(), task_id]
    conn.execute(f"UPDATE tasks SET {cols}, updated_at = ? WHERE id = ?", values)
    conn.commit()


def set_task_env(
    conn: sqlite3.Connection, task_id: int, key: str, value: str, *, source: str = "manual"
) -> None:
    """Attach a small fact to a task, replacing any earlier value for the same key."""
    now = _now()
    conn.execute(
        """
        INSERT INTO task_env (task_id, key, value, source, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id, key) DO UPDATE SET
            value = excluded.value, source = excluded.source, updated_at = excluded.updated_at
        """,
        (task_id, key, value, source, now, now),
    )
    conn.commit()


def get_task_env(conn: sqlite3.Connection, task_id: int, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM task_env WHERE task_id = ? AND key = ?", (task_id, key)
    ).fetchone()
    return row["value"] if row else None


def task_env(conn: sqlite3.Connection, task_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM task_env WHERE task_id = ? ORDER BY key", (task_id,)).fetchall()
    )


def add_dependency(conn: sqlite3.Connection, task_id: int, depends_on: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO task_deps (task_id, depends_on) VALUES (?, ?)",
        (task_id, depends_on),
    )
    conn.commit()


def dependencies_of(conn: sqlite3.Connection, task_id: int) -> list[int]:
    rows = conn.execute("SELECT depends_on FROM task_deps WHERE task_id = ?", (task_id,)).fetchall()
    return [int(r[0]) for r in rows]


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


def next_seq(conn: sqlite3.Connection, run_id: int | None) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id IS ?",
        (run_id,),
    ).fetchone()
    return int(row[0]) + 1


def append_event(
    conn: sqlite3.Connection,
    *,
    kind: str,
    payload: dict,
    run_id: int | None = None,
    task_id: int | None = None,
) -> int:
    seq = next_seq(conn, run_id)
    cur = conn.execute(
        """
        INSERT INTO events (run_id, task_id, seq, kind, payload, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, task_id, seq, kind, json.dumps(payload), _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_turn_result(
    conn: sqlite3.Connection,
    *,
    run_id: int | None,
    task_id: int,
    runner_id: str,
    task_status: str,
    kind: str,
    payload: dict,
    exit_code: int,
) -> int:
    """Record how a worker's turn ended as one commit: task status, event, runner row.

    Written one at a time, a reader could see the task ``blocked`` while its runner
    row still said ``running``, and a resume sent the moment the status turned was
    refused as a duplicate of a worker that had already exited (task 259). Together
    they also keep ``result_recorded`` — the crash-reconciliation boundary — in the
    same transaction as the state it vouches for.
    """
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO events (run_id, task_id, seq, kind, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, task_id, next_seq(conn, run_id), kind, json.dumps(payload), now),
        )
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (task_status, now, task_id)
        )
        conn.execute(
            "UPDATE runners SET status = 'exited', exit_code = ?, result_recorded = 1 WHERE id = ?",
            (exit_code, runner_id),
        )
        conn.commit()
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.rollback()
        raise
    return int(cur.lastrowid)


def events_after(conn: sqlite3.Connection, run_id: int | None, after_seq: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM events WHERE run_id IS ? AND seq > ? ORDER BY seq",
            (run_id, after_seq),
        ).fetchall()
    )


# Event kinds that require manager or human attention (used by `ppy wait`). A
# completed worker is actionable because the manager must review it next.
ACTIONABLE_KINDS = (
    "worker_done",
    "worker_stopped",
    "question",
    "blocked",
    "review_requested",
    "run_done",
    "error",
    "worker_quiet",
    "plan_missing",
    "continuation_deferred",
)


def actionable_events(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    marks = ",".join("?" for _ in ACTIONABLE_KINDS)
    return list(
        conn.execute(
            f"SELECT * FROM events WHERE run_id = ? AND kind IN ({marks}) ORDER BY seq",
            (run_id, *ACTIONABLE_KINDS),
        ).fetchall()
    )


# --------------------------------------------------------------------------- #
# Leases
# --------------------------------------------------------------------------- #


def add_lease(
    conn: sqlite3.Connection,
    *,
    lease_id: str,
    repo_id: int | None,
    task_id: int | None,
    branch: str,
    worktree_path: str,
    base_sha: str | None,
    backend: str,
) -> None:
    conn.execute(
        """
        INSERT INTO leases
            (id, repo_id, task_id, branch, worktree_path, base_sha, backend,
             status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        (lease_id, repo_id, task_id, branch, worktree_path, base_sha, backend, _now()),
    )
    conn.commit()


def release_lease(conn: sqlite3.Connection, lease_id: str) -> None:
    conn.execute(
        "UPDATE leases SET status = 'released', released_at = ? WHERE id = ?",
        (_now(), lease_id),
    )
    conn.commit()


def active_leases(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM leases WHERE status = 'active' ORDER BY created_at"))


# --------------------------------------------------------------------------- #
# Runners
# --------------------------------------------------------------------------- #


def register_runner(
    conn: sqlite3.Connection,
    *,
    runner_id: str,
    task_id: int,
    provider: str,
    pid: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO runners (id, task_id, pid, provider, status, started_at, heartbeat_at)
        VALUES (?, ?, ?, ?, 'starting', ?, ?)
        """,
        (runner_id, task_id, pid, provider, _now(), _now()),
    )
    conn.commit()


def update_runner(conn: sqlite3.Connection, runner_id: str, **fields: object) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = [*fields.values(), runner_id]
    conn.execute(f"UPDATE runners SET {cols} WHERE id = ?", values)
    conn.commit()


def get_runner(conn: sqlite3.Connection, runner_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runners WHERE id = ?", (runner_id,)).fetchone()


def live_runners(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM runners WHERE status IN ('starting','running') ORDER BY started_at"
        )
    )


def task_runners(conn: sqlite3.Connection, task_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM runners WHERE task_id = ? ORDER BY started_at, id", (task_id,)
        ).fetchall()
    )


def live_runners_for_task(conn: sqlite3.Connection, task_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM runners WHERE task_id = ? AND status IN ('starting','running') "
            "ORDER BY started_at",
            (task_id,),
        ).fetchall()
    )


def supersede_runners(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    reason: str,
    except_runner_id: str | None = None,
) -> list[dict]:
    """Retire a task's live runners so their late exit cannot rewrite task status.

    A resumed task has a *new* session; the superseded session's exit code (often
    ``1`` from the interrupt that ended it) arrives afterwards and used to overwrite
    ``in_progress`` with ``failed`` while the resumed worker was still running.
    Superseded runners are recorded by session id and ignored at finalization.
    """
    retired: list[dict] = []
    for row in live_runners_for_task(conn, task_id):
        if except_runner_id is not None and row["id"] == except_runner_id:
            continue
        conn.execute(
            "UPDATE runners SET superseded_at = ?, supersede_reason = ? WHERE id = ?",
            (_now(), reason, row["id"]),
        )
        retired.append({"runner": row["id"], "session_id": row["session_id"], "pid": row["pid"]})
    conn.commit()
    return retired


def is_runner_superseded(conn: sqlite3.Connection, runner_id: str) -> bool:
    row = conn.execute("SELECT superseded_at FROM runners WHERE id = ?", (runner_id,)).fetchone()
    return bool(row is not None and row["superseded_at"])


# --------------------------------------------------------------------------- #
# Sessions and usage
# --------------------------------------------------------------------------- #


def upsert_session(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    provider: str,
    provider_session_id: str | None,
    status: str = "active",
) -> int:
    now = _now()
    existing = conn.execute(
        "SELECT id FROM sessions WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()
    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO sessions
                (task_id, provider, provider_session_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (task_id, provider, provider_session_id, status, now, now),
        )
        conn.commit()
        return int(cur.lastrowid)
    conn.execute(
        "UPDATE sessions SET provider_session_id = ?, status = ?, updated_at = ? WHERE id = ?",
        (provider_session_id, status, now, existing["id"]),
    )
    conn.commit()
    return int(existing["id"])


def record_usage(
    conn: sqlite3.Connection,
    *,
    run_id: int | None,
    task_id: int | None,
    provider: str,
    model: str | None,
    reasoning: str | None,
    input_tokens: int,
    output_tokens: int,
) -> None:
    conn.execute(
        """
        INSERT INTO usage
            (run_id, task_id, provider, model, reasoning, input_tokens,
             output_tokens, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, task_id, provider, model, reasoning, input_tokens, output_tokens, _now()),
    )
    conn.commit()


def usage_by_profile(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT provider, model, reasoning,
               COALESCE(SUM(input_tokens),0) AS input,
               COALESCE(SUM(output_tokens),0) AS output,
               COUNT(*) AS calls
        FROM usage WHERE run_id = ?
        GROUP BY provider, model, reasoning
        ORDER BY provider, model
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            "provider": r["provider"],
            "model": r["model"],
            "reasoning": r["reasoning"],
            "input_tokens": int(r["input"]),
            "output_tokens": int(r["output"]),
            "calls": int(r["calls"]),
        }
        for r in rows
    ]


def usage_totals(conn: sqlite3.Connection, run_id: int) -> dict:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(input_tokens),0) AS input,
               COALESCE(SUM(output_tokens),0) AS output
        FROM usage WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    return {"input_tokens": int(row["input"]), "output_tokens": int(row["output"])}


# --------------------------------------------------------------------------- #
# Todos — the manager's intent ledger
# --------------------------------------------------------------------------- #

TODO_STATUSES = ("open", "done", "dropped")


def add_todo(
    conn: sqlite3.Connection,
    text: str,
    *,
    run_id: int | None = None,
    task_id: int | None = None,
    blocked_on: str | None = None,
) -> int:
    now = _now()
    row = conn.execute("SELECT COALESCE(MAX(position), 0) FROM todos").fetchone()
    position = int(row[0]) + 1
    cur = conn.execute(
        """
        INSERT INTO todos (run_id, task_id, text, status, blocked_on, position, created_at,
                           updated_at)
        VALUES (?, ?, ?, 'open', ?, ?, ?, ?)
        """,
        (run_id, task_id, text, blocked_on, position, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_todo(conn: sqlite3.Connection, todo_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()


def list_todos(
    conn: sqlite3.Connection,
    *,
    status: str | None = "open",
    run_id: int | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    clauses, params = [], []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order = "ORDER BY position, id" if status == "open" else "ORDER BY updated_at DESC, id DESC"
    sql = f"SELECT * FROM todos {where} {order}"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql, params).fetchall())


def update_todo(conn: sqlite3.Connection, todo_id: int, **fields: object) -> None:
    if not fields:
        return
    if fields.get("status") == "done":
        fields.setdefault("done_at", _now())
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = [*fields.values(), _now(), todo_id]
    conn.execute(f"UPDATE todos SET {cols}, updated_at = ? WHERE id = ?", values)
    conn.commit()


def open_todo_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM todos WHERE status = 'open'").fetchone()
    return int(row[0])


# --------------------------------------------------------------------------- #
# Watermarks — the newest processed timestamp per external record
# --------------------------------------------------------------------------- #


def set_watermark(
    conn: sqlite3.Connection, key: str, watermark: str, *, note: str | None = None
) -> None:
    """Record the newest timestamp processed on ``key``, replacing any earlier one."""
    conn.execute(
        """
        INSERT INTO watermarks (key, watermark, recorded_at, note)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            watermark = excluded.watermark,
            recorded_at = excluded.recorded_at,
            note = excluded.note
        """,
        (key, watermark, _now(), note),
    )
    conn.commit()


def get_watermark(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM watermarks WHERE key = ?", (key,)).fetchone()


def list_watermarks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM watermarks ORDER BY key").fetchall())


def delete_watermark(conn: sqlite3.Connection, key: str) -> bool:
    cur = conn.execute("DELETE FROM watermarks WHERE key = ?", (key,))
    conn.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Worker progress — structured phase reports (events of kind ``worker_progress``)
# --------------------------------------------------------------------------- #


def progress_events(
    conn: sqlite3.Connection, *, task_id: int | None = None, repo_id: int | None = None
) -> list[sqlite3.Row]:
    """Progress reports, newest first, for one task or every task of a repo."""
    if task_id is not None:
        return list(
            conn.execute(
                "SELECT * FROM events WHERE task_id = ? AND kind = 'worker_progress' "
                "ORDER BY id DESC",
                (task_id,),
            ).fetchall()
        )
    if repo_id is not None:
        return list(
            conn.execute(
                "SELECT e.* FROM events e JOIN tasks t ON t.id = e.task_id "
                "WHERE t.repo_id = ? AND e.kind = 'worker_progress' ORDER BY e.id DESC",
                (repo_id,),
            ).fetchall()
        )
    return list(
        conn.execute(
            "SELECT * FROM events WHERE kind = 'worker_progress' ORDER BY id DESC"
        ).fetchall()
    )


def latest_progress(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    rows = progress_events(conn, task_id=task_id)
    return rows[0] if rows else None


def has_progress_phase(conn: sqlite3.Connection, task_id: int, phase: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM events WHERE task_id = ? AND kind = 'worker_progress' "
        "AND json_extract(payload, '$.phase') = ? LIMIT 1",
        (task_id, phase),
    ).fetchone()
    return row is not None
