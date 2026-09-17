"""Worker health: is every in-flight team member alive, and have we heard from them?

The runner already records everything a worker says (every stream event lands in the
``events`` table) and the pid of its process. Health turns that into a verdict per
in-flight task without a model call:

- ``alive``  — process up and it spoke within the quiet threshold;
- ``quiet``  — process up but silent for longer than the threshold (probably stuck:
  a hung tool, a wedged prompt, a worker waiting on something nobody will give it);
- ``dead``   — no live runner process for an in-flight task (crash, kill, machine
  restart) — ``ppy reconcile`` turns this into ``needs_recovery``.

The supervisor polls this on its maintenance tick and raises an actionable
``worker_quiet`` event the first time a worker goes quiet (and again only after it
has been heard from and gone quiet a second time), so ``ppy run`` / ``ppy wait`` surface
it like any other actionable moment. ``ppy health`` prints the same view on demand.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from papaya_agent_runtime import compose
from papaya_agent_runtime.companions import companion_bin
from papaya_agent_runtime.state import init_db, store

IN_FLIGHT = ("requested", "in_progress")
DEFAULT_QUIET_MINUTES = 15
DEFAULT_PLAN_MINUTES = 10
TREEHOUSE_DEFAULT_MAX_TREES = 16


class DispatchHealthError(Exception):
    """A dispatch was refused before a task or lease was created."""


def _health_policy():
    from papaya_agent_runtime.config import HealthPolicy, load_config

    try:
        return load_config().health
    except Exception:  # noqa: BLE001 - health also runs before setup
        return HealthPolicy()


def _usage_policy():
    from papaya_agent_runtime.config import UsagePolicy, load_config

    try:
        return load_config().usage
    except Exception:  # noqa: BLE001 - advisories also run before setup
        return UsagePolicy()


def _run_treehouse_status(cwd: str | None) -> tuple[int, str]:
    tool = companion_bin("treehouse")
    if tool is None:
        return 127, ""
    try:
        proc = subprocess.run(
            [tool, "status", "--json"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return proc.returncode, proc.stdout


def _treehouse_max_trees(cwd: str | None) -> int | None:
    """Load the same repo/user config precedence used by Treehouse itself.

    Treehouse finds the repository root with ``git rev-parse --show-toplevel``.
    A repo-local ``treehouse.toml`` owns ``max_trees`` when present; otherwise
    ``~/.config/treehouse/config.toml`` may supply it. Both files decode over
    Treehouse's built-in default of 16. An unreadable or malformed selected
    config is unknown rather than a reason to close the dispatch gate.
    """
    if not cwd:
        return None
    root = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if root.returncode != 0 or not root.stdout.strip():
        return None
    repo_config = Path(root.stdout.strip()) / "treehouse.toml"
    user_config = Path.home() / ".config" / "treehouse" / "config.toml"
    selected = repo_config if repo_config.exists() else user_config
    if not selected.exists():
        return TREEHOUSE_DEFAULT_MAX_TREES
    try:
        data = tomllib.loads(selected.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    value = data.get("max_trees", TREEHOUSE_DEFAULT_MAX_TREES)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def pool_capacity(repo: str | None = None, *, conn: sqlite3.Connection | None = None) -> dict:
    """The selected Treehouse pool's free slots, or an explicit unknown result.

    The git backend creates worktrees on demand and therefore has no fixed slot
    ceiling. Treehouse reports every slot and its state; only literal idle/free
    states count as capacity, so malformed scalar lookalikes cannot open the gate.
    """
    from papaya_agent_runtime.worktree.lease import resolve_backend

    backend = resolve_backend(None)
    if backend != "treehouse":
        return {"backend": backend, "known": False, "free_slots": None, "total_slots": None}
    conn = conn or init_db()
    cwd = None
    if repo:
        row = store.get_repo(conn, repo)
        cwd = row["local_path"] if row is not None else None
    code, output = _run_treehouse_status(cwd)
    try:
        slots = json.loads(output)
    except (TypeError, ValueError):
        slots = None
    if code != 0 or not isinstance(slots, list):
        return {"backend": backend, "known": False, "free_slots": None, "total_slots": None}
    if any(not isinstance(slot, dict) for slot in slots):
        return {"backend": backend, "known": False, "free_slots": None, "total_slots": None}
    states = [slot.get("status") for slot in slots]
    if any(not isinstance(state, str) for state in states):
        return {"backend": backend, "known": False, "free_slots": None, "total_slots": None}
    max_trees = _treehouse_max_trees(cwd)
    if max_trees is None:
        return {"backend": backend, "known": False, "free_slots": None, "total_slots": None}
    reusable = sum(state.lower() in {"idle", "free", "available"} for state in states)
    growable = max(0, max_trees - len(states))
    return {
        "backend": backend,
        "known": True,
        "free_slots": reusable + growable,
        "total_slots": max_trees,
    }


def _repo_uses_compose(conn: sqlite3.Connection, repo: str | None) -> bool | None:
    if repo is None:
        return None
    row = store.get_repo(conn, repo)
    if row is None:
        return False
    return bool(row["compose_stack"])


def dispatch_snapshot(
    repo: str | None = None, *, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    """Capacity facts used by both ``ppy health`` and the dispatch refusal."""
    conn = conn or init_db()
    uses_compose = _repo_uses_compose(conn, repo)
    stale = None if uses_compose is False else len(compose.prunable_stacks(conn))
    return {
        "pool": pool_capacity(repo, conn=conn),
        "stale_stacks": stale,
        "max_stale_stacks": _health_policy().max_stale_stacks,
    }


def require_dispatch_capacity(repo: str | None = None) -> dict[str, Any]:
    """Refuse a dispatch before it creates state when a recoverable resource is full."""
    snapshot = dispatch_snapshot(repo)
    pool = snapshot["pool"]
    if pool["known"] and pool["free_slots"] == 0:
        raise DispatchHealthError(
            "the worktree pool has no free slot; run `ppy worktree prune` to reclaim finished "
            "or orphaned worktrees before dispatching"
        )
    stale = snapshot["stale_stacks"]
    ceiling = snapshot["max_stale_stacks"]
    if stale is not None and stale > ceiling:
        raise DispatchHealthError(
            f"{stale} stale compose stacks exceed health.max_stale_stacks={ceiling}; run "
            "`ppy task close` for abandoned tasks or `ppy worktree prune` to tear finished "
            "task stacks down"
        )
    return snapshot


def usage_advisories(
    conn: sqlite3.Connection | None = None, *, task_id: int | None = None
) -> list[dict[str, Any]]:
    """Tasks above the configured task/reviewer input ceiling."""
    conn = conn or init_db()
    where = "WHERE t.id = ?" if task_id is not None else ""
    params = (task_id,) if task_id is not None else ()
    rows = conn.execute(
        f"""
        SELECT t.id, t.title, t.role, COALESCE(SUM(u.input_tokens), 0) AS input_tokens
        FROM tasks t LEFT JOIN usage u ON u.task_id = t.id
        {where}
        GROUP BY t.id, t.title, t.role ORDER BY t.id
        """,
        params,
    ).fetchall()
    policy = _usage_policy()
    out = []
    for row in rows:
        review = row["role"] == "reviewer"
        ceiling = policy.input_ceiling_per_review if review else policy.input_ceiling_per_task
        used = int(row["input_tokens"])
        if used > ceiling:
            out.append(
                {
                    "task_id": int(row["id"]),
                    "title": row["title"],
                    "role": row["role"],
                    "input_tokens": used,
                    "ceiling": ceiling,
                    "kind": "review" if review else "task",
                }
            )
    return out


def describe_usage_advisory(entry: dict[str, Any]) -> str:
    return (
        f'usage advisory: task {entry["task_id"]} "{entry["title"]}" used '
        f"{entry['input_tokens']:,} input tokens, above the {entry['kind']} ceiling of "
        f"{entry['ceiling']:,}"
    )


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def last_heard(conn: sqlite3.Connection, task_id: int) -> datetime | None:
    """When the worker last produced anything — its newest event, else the task row."""
    row = conn.execute(
        "SELECT MAX(created_at) AS at FROM events WHERE task_id = ?", (task_id,)
    ).fetchone()
    heard = _parse(row["at"] if row else None)
    if heard is None:
        task = store.get_task(conn, task_id)
        heard = _parse(task["updated_at"]) if task else None
    return heard


def _latest_runner(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runners WHERE task_id = ? ORDER BY started_at DESC LIMIT 1", (task_id,)
    ).fetchone()


def session_alive(conn: sqlite3.Connection, task_id: int) -> bool:
    """Is this task's newest runner process still running? `check`'s not-`dead`, alone."""
    runner = _latest_runner(conn, task_id)
    return _pid_alive(runner["pid"] if runner else None)


def check(
    conn: sqlite3.Connection | None = None,
    *,
    quiet_after: timedelta | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """One health entry per in-flight task, oldest silence first."""
    conn = conn or init_db()
    quiet_after = quiet_after or timedelta(minutes=DEFAULT_QUIET_MINUTES)
    now = now or datetime.now(UTC)
    repos = {r["id"]: r["name"] for r in store.list_repos(conn)}
    marks = ",".join("?" for _ in IN_FLIGHT)
    # Worker tasks only (`store.WORKER_TASK`): a ticket task is `ppy serve`'s record of a
    # hold, never a process, and read as a worker it was a dead one forever.
    rows = conn.execute(
        f"SELECT * FROM tasks WHERE status IN ({marks}) AND {store.WORKER_TASK} ORDER BY id",
        IN_FLIGHT,
    ).fetchall()

    out: list[dict[str, Any]] = []
    for task in rows:
        runner = _latest_runner(conn, task["id"])
        pid = runner["pid"] if runner else None
        alive = _pid_alive(pid)
        heard = last_heard(conn, task["id"])
        silent = (now - heard) if heard else None
        if not alive:
            verdict = "dead"
        elif silent is not None and silent > quiet_after:
            verdict = "quiet"
        else:
            verdict = "alive"
        out.append(
            {
                "task_id": task["id"],
                "title": task["title"],
                "repo": repos.get(task["repo_id"]),
                "status": task["status"],
                "provider": task["provider"],
                "run_id": task["run_id"],
                "pid": pid,
                "process_alive": alive,
                "last_heard": heard.isoformat(timespec="seconds") if heard else None,
                "silent_seconds": int(silent.total_seconds()) if silent else None,
                "verdict": verdict,
            }
        )
    out.sort(key=lambda e: -(e["silent_seconds"] or 0))
    return out


def claude_tool_profile() -> dict[str, Any]:
    """The tool profile a Claude worker would be launched with right now.

    A Claude worker with no allowed tools has no shell, and that used to be
    invisible until a dispatch produced a worker that could not run a command.
    Health answers it before anyone dispatches.
    """
    from papaya_agent_runtime.providers.claude import effective_allowed_tools

    tools, source = effective_allowed_tools()
    return {
        "tools": tools,
        "count": len(tools),
        "source": source,
        "ok": bool(tools),
    }


def describe_claude_tools(profile: dict[str, Any]) -> str:
    if not profile["ok"]:
        return (
            f"claude worker tools: NONE ({profile['source']} is empty) — a dispatch would be "
            "refused; run `ppy config claude --reset`"
        )
    shown = ", ".join(profile["tools"][:6])
    more = f", +{profile['count'] - 6} more" if profile["count"] > 6 else ""
    return (
        f"claude worker tools: {profile['count']} patterns from {profile['source']} — {shown}{more}"
    )


def humanize(seconds: float | None) -> str:
    # Callers hand this both ints (event ages) and floats (`running_seconds`), so it
    # rounds once here rather than at every call site: a float reached the `h%02dm`
    # format and crashed `ppy status --team` (2026-09-17).
    if seconds is None:
        return "never heard from"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 120:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def describe(entry: dict[str, Any]) -> str:
    where = f" ({entry['repo']})" if entry.get("repo") else ""
    silence = humanize(entry["silent_seconds"])
    head = f'task {entry["task_id"]} "{entry["title"]}"{where}'
    if entry["verdict"] == "dead":
        return f"{head}: DEAD — no runner process; run `ppy reconcile`, then `ppy resume`"
    if entry["verdict"] == "quiet":
        return (
            f"{head}: QUIET for {silence} — process up but silent; peek at its progress "
            "log, then `ppy steer`/`ppy resume` or `ppy reconcile` if it's wedged"
        )
    return f"{head}: alive (heard {silence} ago)"


def flag_quiet_workers(
    conn: sqlite3.Connection,
    *,
    quiet_after: timedelta,
    already_flagged: set[int],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Raise one actionable ``worker_quiet`` event per quiet episode.

    ``already_flagged`` is the caller's memory of which tasks were flagged; a task is
    removed from it as soon as it is heard from again so a relapse is flagged anew.
    Returns the entries flagged on this call.
    """
    flagged: list[dict[str, Any]] = []
    entries = check(conn, quiet_after=quiet_after, now=now)
    seen = {e["task_id"] for e in entries}
    for stale in [t for t in already_flagged if t not in seen]:
        already_flagged.discard(stale)  # task left in-flight; forget it
    for e in entries:
        if e["verdict"] != "quiet":
            already_flagged.discard(e["task_id"])
            continue
        if e["task_id"] in already_flagged:
            continue
        already_flagged.add(e["task_id"])
        store.append_event(
            conn,
            kind="worker_quiet",
            payload={
                "task_id": e["task_id"],
                "silent_seconds": e["silent_seconds"],
                "last_heard": e["last_heard"],
                "summary": (
                    f"no output from the worker for {humanize(e['silent_seconds'])}; "
                    "it may be stuck — check its progress log and steer, resume, or reconcile"
                ),
            },
            run_id=e["run_id"],
            task_id=e["task_id"],
        )
        flagged.append(e)
    return flagged


def quiet_threshold() -> timedelta:
    """The configured quiet threshold, defaulting when there is no config yet."""
    try:
        from papaya_agent_runtime.config import load_config

        minutes = load_config().health.quiet_minutes
    except Exception:  # noqa: BLE001 - health must work before setup
        minutes = DEFAULT_QUIET_MINUTES
    return timedelta(minutes=minutes)


def plan_grace() -> timedelta:
    """How long a worker may run before a missing plan report is flagged."""
    try:
        from papaya_agent_runtime.config import load_config

        minutes = load_config().health.plan_minutes
    except Exception:  # noqa: BLE001 - health must work before setup
        minutes = DEFAULT_PLAN_MINUTES
    return timedelta(minutes=minutes)


def flag_missing_plans(
    conn: sqlite3.Connection,
    *,
    grace: timedelta,
    already_flagged: set[int],
    now: datetime | None = None,
) -> list[int]:
    """Raise one actionable ``plan_missing`` event per task that never posted a plan.

    Workers are told to report ``ppy progress <id> --phase plan`` before implementing.
    A task that has been ``in_progress`` longer than ``grace`` with no plan report is
    flagged once so the manager peeks and steers early instead of reviewing a finished
    diff that went the wrong way.
    """
    now = now or datetime.now(UTC)
    flagged: list[int] = []
    rows = conn.execute("SELECT * FROM tasks WHERE status = 'in_progress' ORDER BY id").fetchall()
    live = {row["id"] for row in rows}
    for stale in [t for t in already_flagged if t not in live]:
        already_flagged.discard(stale)
    for task in rows:
        if task["id"] in already_flagged:
            continue
        if store.has_progress_phase(conn, task["id"], "plan"):
            already_flagged.add(task["id"])  # satisfied; never flag
            continue
        started = _parse(task["updated_at"])
        runner = _latest_runner(conn, task["id"])
        if runner is not None and _parse(runner["started_at"]):
            started = _parse(runner["started_at"])
        if started is None or now - started < grace:
            continue
        already_flagged.add(task["id"])
        store.append_event(
            conn,
            kind="plan_missing",
            payload={
                "task_id": task["id"],
                "running_seconds": int((now - started).total_seconds()),
                "summary": (
                    f"worker has run {humanize(int((now - started).total_seconds()))} without "
                    "posting a plan (`ppy progress --phase plan`); peek at it and steer early"
                ),
            },
            run_id=task["run_id"],
            task_id=task["id"],
        )
        flagged.append(task["id"])
    return flagged
