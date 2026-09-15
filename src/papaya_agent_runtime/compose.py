"""Per-task compose stacks, and taking them down when the task is over.

Backend tasks bring up their own database with a per-task project name
(``COMPOSE_PROJECT_NAME=task_<n>``) so two workers never fight over one port. On
2026-09-03 fifteen of those stacks — every one belonging to a task that had
already been delivered — were still running with their volumes, and Docker hit
its 33-network ceiling: new stacks stopped starting, so a backend dispatch could
no longer run its own tests. Nothing in the harness had ever torn one down.

The stack a task created is a fact about the task, so it is recorded on the task
(``ppy task env set <id> compose_project=<name>``, or picked up automatically when
a worker names it in a progress note) and read back at the three moments a task's
worktree is given up: ``ppy deliver --merged``, ``ppy task close``, and
``ppy worktree prune``.

What gets recorded *automatically* is deliberately narrow. Teardown destroys
volumes, and this machine runs shared stacks (``chat-with-agents``) and the
user's own (``radar_phase6_7``) beside the per-task ones. A worker's progress note
is prose, so a name learned from one is accepted only when it is that task's own
per-task name; any other stack a note happens to mention is dropped with an event
saying so. A name the manager types with ``ppy task env set`` is taken as given —
that is a decision, not a mention.

Teardown is best-effort by construction. Docker may not be installed, may not be
running, and the stack may already be gone — none of which is a reason to fail a
delivery or leave a lease held. Every failure here is reported and swallowed.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess

from papaya_agent_runtime.state import init_db, store

#: The task-scoped key holding the compose project name.
COMPOSE_PROJECT_KEY = "compose_project"

#: How a worker names its stack in a progress note, and what docker accepts as a
#: project name (lowercase letters, digits, dash, underscore, dot).
_NOTE_RE = re.compile(r"COMPOSE_PROJECT_NAME\s*[=:]\s*[\"']?([A-Za-z0-9][A-Za-z0-9._-]*)")
_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")

#: A stack this harness created for a task: ``task_<n>``, or ``<anything>_task_<n>``
#: for the compose files that prefix the project with a service name.
_PROJECT_TASK_RE = re.compile(r"(?:\A|_)task_(\d+)\Z")

#: A task in one of these is over, so its stack is nobody's working state.
TERMINAL_STATUSES = ("delivered", "closed", "cancelled")

COMPOSE_TIMEOUT = 120


class ComposeError(Exception):
    """A compose project name was refused. Teardown itself never raises."""


def docker_bin() -> str | None:
    """The docker executable, or ``None`` when this machine has none."""
    return shutil.which("docker")


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    """Every docker call goes through here, so tests can stand in for the daemon."""
    return subprocess.run(
        argv, capture_output=True, text=True, check=False, timeout=COMPOSE_TIMEOUT
    )


# --------------------------------------------------------------------------- #
# Recording which stack belongs to which task
# --------------------------------------------------------------------------- #


def detect_project(text: str | None) -> str | None:
    """The compose project a worker named in prose, if it named one.

    Workers already write ``COMPOSE_PROJECT_NAME=task_12`` into their progress
    notes because that is how they tell the manager which stack to look at; that
    sentence is enough to record it, so nobody has to remember a second command.
    """
    match = _NOTE_RE.search(text or "")
    return match.group(1) if match else None


def record_project(
    task_id: int,
    project: str,
    *,
    source: str = "manual",
    conn: sqlite3.Connection | None = None,
) -> str:
    """Remember the compose stack this task owns."""
    project = (project or "").strip()
    if not _NAME_RE.match(project):
        raise ComposeError(
            f"{project!r} is not a compose project name (letters, digits, dash, dot, underscore)"
        )
    conn = conn or init_db()
    if store.get_task(conn, task_id) is None:
        raise ComposeError(f"task {task_id} not found")
    store.set_task_env(conn, task_id, COMPOSE_PROJECT_KEY, project, source=source)
    return project


def project_for(task_id: int, *, conn: sqlite3.Connection | None = None) -> str | None:
    conn = conn or init_db()
    return store.get_task_env(conn, task_id, COMPOSE_PROJECT_KEY)


def note_project(task_id: int, note: str, *, conn: sqlite3.Connection | None = None) -> str | None:
    """Record a compose project a progress note mentions, if it is this task's own.

    A progress note is prose a worker wrote, and what it arms here is
    ``docker compose down -v`` — which destroys volumes. This machine runs shared
    stacks (``chat-with-agents``) and the user's own (``radar_phase6_7``) beside
    the per-task ones, so a worker that merely *mentions* one must never arm its
    teardown. A name learned this way is therefore accepted only when it is the
    per-task name for **this** task: ``task_<id>``, or ``<prefix>_task_<id>``.
    Anything else is recorded as an ignored-name event and dropped.

    ``ppy task env set`` stays free-form on purpose: that one the manager typed
    deliberately, and it is the escape hatch this refusal points at.
    """
    project = detect_project(note)
    if project is None:
        return None
    try:
        conn = conn or init_db()
        if task_id_of(project) != task_id:
            task = store.get_task(conn, task_id)
            store.append_event(
                conn,
                kind="compose_project_ignored",
                payload={
                    "task_id": task_id,
                    "project": project,
                    "summary": (
                        f"a progress note named compose project {project!r}, which is not "
                        f"task {task_id}'s own stack (task_{task_id} or <prefix>_task_{task_id}) "
                        "— not recorded, so nothing here will tear it down. If that really is "
                        f"the stack to remove, say so deliberately with `ppy task env set "
                        f"{task_id} compose_project={project}`."
                    ),
                },
                run_id=task["run_id"] if task else None,
                task_id=task_id,
            )
            return None
        return record_project(task_id, project, source="progress-note", conn=conn)
    except (ComposeError, sqlite3.Error):
        return None


# --------------------------------------------------------------------------- #
# Teardown
# --------------------------------------------------------------------------- #


def down_argv(project: str) -> list[str]:
    """The teardown command. Volumes and orphans go too, or nothing is reclaimed."""
    return ["docker", "compose", "-p", project, "down", "-v", "--remove-orphans"]


def _removed_lines(proc: subprocess.CompletedProcess) -> list[str]:
    """What compose says it removed. It reports progress on stderr, not stdout."""
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    return [line.strip() for line in text.splitlines() if "Remov" in line and line.strip()]


def down(project: str) -> dict:
    """Take a compose stack down with its volumes. Never raises."""
    result: dict = {"project": project, "ran": False, "ok": False, "removed": [], "detail": ""}
    docker = docker_bin()
    if docker is None:
        result["detail"] = "docker is not installed here; left the stack alone"
        return result
    argv = down_argv(project)
    try:
        proc = _run([docker, *argv[1:]])
    except (OSError, subprocess.SubprocessError) as exc:
        result["detail"] = f"docker could not be run: {exc}"
        return result
    result["ran"] = True
    result["removed"] = _removed_lines(proc)
    result["ok"] = proc.returncode == 0
    if proc.returncode == 0:
        result["detail"] = (
            f"removed {len(result['removed'])} object(s)"
            if result["removed"]
            else "nothing left to remove"
        )
    else:
        result["detail"] = (proc.stderr or proc.stdout or "").strip()[:200] or "docker failed"
    return result


def teardown_for_task(
    task_id: int, *, trigger: str, conn: sqlite3.Connection | None = None
) -> dict | None:
    """Take down the stack this task owns, if it owns one. Never raises.

    Returns ``None`` when no compose project was ever recorded — the common case,
    and not something to report.
    """
    try:
        conn = conn or init_db()
        project = project_for(task_id, conn=conn)
        if not project:
            return None
        result = down(project)
        result["task_id"] = task_id
        result["trigger"] = trigger
        task = store.get_task(conn, task_id)
        store.append_event(
            conn,
            kind="compose_down",
            payload=result,
            run_id=task["run_id"] if task else None,
            task_id=task_id,
        )
        return result
    except Exception as exc:  # noqa: BLE001 - teardown never fails its parent command
        return {
            "task_id": task_id,
            "trigger": trigger,
            "project": None,
            "ran": False,
            "ok": False,
            "removed": [],
            "detail": f"compose teardown skipped: {exc}",
        }


def describe(result: dict | None) -> str:
    """One line a command can print after tearing a stack down."""
    if not result:
        return ""
    project = result.get("project")
    if not project:
        return str(result.get("detail") or "")
    if result.get("ok") and result.get("removed"):
        return f"compose stack {project}: removed " + "; ".join(result["removed"])
    if result.get("ok"):
        return f"compose stack {project}: nothing left to remove"
    return f"compose stack {project}: not torn down — {result.get('detail')}"


# --------------------------------------------------------------------------- #
# What is still running that nobody needs
# --------------------------------------------------------------------------- #


def task_id_of(project: str) -> int | None:
    """The task a compose project belongs to, from its name."""
    match = _PROJECT_TASK_RE.search(project or "")
    return int(match.group(1)) if match else None


def list_stacks() -> list[dict]:
    """Every compose project docker knows about. Empty when docker is unavailable."""
    docker = docker_bin()
    if docker is None:
        return []
    try:
        proc = _run([docker, "compose", "ls", "--all", "--format", "json"])
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return []
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def prunable_stacks(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Running stacks named for a task that is over. Never raises.

    This is the disk-and-network equivalent of a prunable worktree slot: nothing
    will use it again, and it is holding a network out of a pool of 33.
    """
    try:
        conn = conn or init_db()
        out: list[dict] = []
        for stack in list_stacks():
            project = str(stack.get("Name") or "")
            task_id = task_id_of(project)
            if task_id is None:
                continue
            task = store.get_task(conn, task_id)
            if task is None or task["status"] not in TERMINAL_STATUSES:
                continue
            out.append(
                {
                    "project": project,
                    "task_id": task_id,
                    "task_status": task["status"],
                    "state": stack.get("Status") or "",
                }
            )
        return out
    except Exception:  # noqa: BLE001 - a docker probe must not break `ppy health`
        return []


def describe_prunable(stacks: list[dict]) -> str:
    if not stacks:
        return "compose stacks: none left over from finished tasks"
    names = ", ".join(f"{s['project']} (task {s['task_id']} {s['task_status']})" for s in stacks)
    return (
        f"compose stacks: {len(stacks)} still up for finished tasks — {names}; "
        "`ppy task close` or `ppy worktree prune` takes them down, or "
        "`docker compose -p <name> down -v --remove-orphans` by hand"
    )
