"""Did the worker finish, or did its turn just end?

A Claude worker started the full backend suite, the tool's foreground cap sent it
to the background, the worker ended its turn to wait for it, and the harness read
that turn ending as ``worker_done``: the backgrounded suite was killed with the
session, the branch was never pushed, and no done note was ever filed (task 103,
2026-09-04; task 104 had the same shape with browser smoke retries). The manager
only noticed because the newest progress report said "test" and nothing had been
pushed.

A turn ending is not evidence of completion. This module asks for the evidence:

1. the newest progress note says the worker reached its stored terminal phase;
2. for a ``done`` task, the branch holds nothing the remote does not already have;
3. the session's last tool call was not a backgrounded command, which dies with
   the session that started it.

Any of those failing means the task stopped mid-gate — a real state
(``worker_stopped``) with a named reason and a resume that says what was cut
short, rather than a finished task nobody looks at again.

One of those three is not the worker giving up, though. On 2026-09-04 three of
four workers filed a done note and still could not get their commits onto the
remote: a push hook that fails on advisories already on the default branch, a
push held by the permission layer, a turn that simply ended first. The work was
finished; only the push was missing, and the manager did it by hand every time.
A task's lease branch belongs to that task and nothing else, so pushing it takes
nothing from anyone — :func:`rescue_unpushed` does it here (issue #50). When the
push itself is refused, the refusal's own words become the stop reason, because
a hook that says why is more useful than the harness guessing.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from papaya_agent_runtime.state import store

WORKER_STOPPED = "worker_stopped"
PUSHED_BY_MANAGER = "pushed_by_manager"


@dataclass
class StopVerdict:
    """Whether the turn ended mid-gate, and the reasons in the manager's words."""

    stopped: bool = False
    reasons: list[str] = field(default_factory=list)
    unpushed: int = 0
    phase: str | None = None
    background_command: str | None = None
    expected_phase: str = "done"

    @property
    def summary(self) -> str:
        if not self.stopped:
            return "the worker finished the gate"
        return f"worker stopped before {self.expected_phase}: " + "; ".join(self.reasons)

    def resume_message(self) -> str:
        """The default `ppy resume` message: what was cut short, and what to do now."""
        finish = (
            "file your `ppy progress <task_id> --phase review --note ...` report"
            if self.expected_phase == "review"
            else "file your `ppy progress <task_id> --phase done --note ...` report, and push "
            "your branch"
        )
        return (
            "Your turn ended before the work was finished: "
            + "; ".join(self.reasons)
            + ". Pick it back up in this worktree: run the authoritative verification "
            "suite in the foreground (never as a background task — a backgrounded "
            f"command dies with the session), {finish}."
        )


def _unpushed_reason(count: int) -> str:
    """The one reason :func:`rescue_unpushed` can take back, so it matches exactly."""
    return f"the branch has {count} commit(s) no remote holds — the work was never pushed"


def _latest_phase(conn, task_id: int) -> str | None:
    row = store.latest_progress(conn, task_id)
    if row is None:
        return None
    try:
        return json.loads(row["payload"]).get("phase")
    except (TypeError, ValueError):
        return None


def _tool_calls(payload: object) -> list[dict]:
    """Every tool call in one recorded provider event, in the order they appear.

    Providers wrap tool calls differently — Claude nests ``tool_use`` blocks in an
    assistant message's content — so this walks the payload rather than assuming a
    shape, and accepts anything carrying a name and an input object.
    """
    found: list[dict] = []
    if isinstance(payload, dict):
        is_call = payload.get("type") == "tool_use" or (
            "name" in payload and isinstance(payload.get("input"), dict)
        )
        if is_call and isinstance(payload.get("input"), dict):
            found.append(payload)
        for value in payload.values():
            found.extend(_tool_calls(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_tool_calls(item))
    return found


def last_background_command(conn, task_id: int) -> str | None:
    """The command of the session's last tool call, when that call was backgrounded.

    A backgrounded command is owned by the session that started it; when the turn
    ends, it is killed. If that was the last thing the worker did, whatever it was
    waiting for never finished.
    """
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind LIKE 'worker_%' ORDER BY id DESC",
        (task_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        calls = _tool_calls(payload)
        if not calls:
            continue
        last = calls[-1]
        params = last.get("input") or {}
        if params.get("run_in_background") or params.get("background"):
            return str(params.get("command") or last.get("name") or "a backgrounded command")
        return None
    return None


def why_stopped(conn, task_id: int) -> StopVerdict:
    """Judge a finished turn on evidence. Call it before any supervisor auto-commit.

    The auto-commit is the supervisor's own safety net, not the worker's work, so
    it must not be what makes a task look like it has unpushed commits.
    """
    from papaya_agent_runtime import stacks

    task = store.get_task(conn, task_id)
    if task is None:
        return StopVerdict()
    expected = task["ends_at"] if "ends_at" in task.keys() else "done"  # noqa: SIM118
    verdict = StopVerdict(expected_phase=expected)

    verdict.phase = _latest_phase(conn, task_id)
    if verdict.phase != expected:
        verdict.stopped = True
        verdict.reasons.append(
            f"no {expected} note was ever filed"
            if verdict.phase is None
            else f"the latest progress note is {verdict.phase!r}, not a {expected} note"
        )

    unpushed = stacks.unpushed_commits(conn, task) if expected == "done" else 0
    verdict.unpushed = unpushed
    if unpushed > 0:
        verdict.stopped = True
        verdict.reasons.append(_unpushed_reason(unpushed))

    command = last_background_command(conn, task_id)
    if command:
        verdict.background_command = command
        verdict.stopped = True
        verdict.reasons.append(
            f"the last thing the session did was background `{command[:120]}`, which was "
            "killed when the turn ended"
        )
    return verdict


# --------------------------------------------------------------------------- #
# Pushing the lease branch on the worker's behalf
# --------------------------------------------------------------------------- #


@dataclass
class PushResult:
    """What pushing a task's lease branch did, or why it did nothing."""

    task_id: int
    pushed: bool
    branch: str | None = None
    remote: str = "origin"
    sha: str | None = None
    #: The last lines of the push's stderr, or why there was nothing to push to.
    stderr: str = ""

    @property
    def note(self) -> str:
        if self.pushed:
            return f"pushed {(self.sha or '')[:8]} to {self.remote}/{self.branch}"
        target = f"{self.remote}/{self.branch}" if self.branch else "its lease branch"
        return f"nothing was pushed to {target} — {self.stderr}"


def _last_lines(text: str, count: int = 20) -> str:
    lines = [line for line in (text or "").strip().splitlines() if line.strip()]
    return "\n".join(lines[-count:])


def push_lease_branch(conn, task_id: int) -> PushResult:
    """Push a task's worktree head to its own lease branch. Never forces.

    The branch is named after the task and nothing else writes to it, so this is
    the one push the harness can make on a worker's behalf without deciding whose
    commits survive. A rejected push is not raised: it comes back as a result
    carrying the remote's own words, which is what the manager needs to read.
    """
    from papaya_agent_runtime import lifecycle, stacks

    task = store.get_task(conn, task_id)
    if task is None:
        return PushResult(task_id=task_id, pushed=False, stderr="there is no such task")
    try:
        lifecycle.require_live_lease(conn, task_id)
    except lifecycle.LifecycleError as exc:
        reason = str(exc)
        store.append_event(
            conn,
            kind="push_skipped_no_live_lease",
            payload={
                "task_id": task_id,
                "summary": f"post-turn push skipped: {reason}",
                "reason": reason,
            },
            run_id=task["run_id"],
            task_id=task_id,
        )
        return PushResult(task_id=task_id, pushed=False, stderr=reason)
    worktree, branch = task["worktree_path"], task["branch"]
    if not worktree or not branch:
        return PushResult(
            task_id=task_id,
            pushed=False,
            branch=branch,
            stderr="the task has no lease worktree and branch to push from",
        )
    if not Path(worktree).is_dir():
        return PushResult(
            task_id=task_id,
            pushed=False,
            branch=branch,
            stderr=f"the lease worktree {worktree} is gone, so there is nothing left to push",
        )
    remote = stacks.push_remote(conn, task)
    head = subprocess.run(
        ["git", "-C", worktree, "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    sha = head.stdout.strip() if head.returncode == 0 else None
    proc = subprocess.run(
        ["git", "-C", worktree, "push", remote, f"HEAD:{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return PushResult(
            task_id=task_id,
            pushed=False,
            branch=branch,
            remote=remote,
            sha=sha,
            stderr=_last_lines(proc.stderr) or _last_lines(proc.stdout) or "git push failed",
        )
    result = PushResult(task_id=task_id, pushed=True, branch=branch, remote=remote, sha=sha)
    store.append_event(
        conn,
        kind=PUSHED_BY_MANAGER,
        payload={
            "task_id": task_id,
            "branch": branch,
            "remote": remote,
            "head_sha": sha,
            "summary": (
                f"the worker left {(sha or '')[:8]} on {branch} without pushing it; the harness "
                f"pushed that branch to {remote} itself"
            ),
        },
        run_id=task["run_id"],
        task_id=task_id,
    )
    return result


def rescue_unpushed(
    conn, task_id: int, verdict: StopVerdict
) -> tuple[StopVerdict, PushResult | None]:
    """Push for a worker that said it finished, and judge the turn again.

    Only a turn whose *newest* note is ``done`` is rescued: the worker claimed the
    gate. A turn that stopped mid-gate has more missing than a push, so its
    verdict is left exactly as it was and nothing is pushed.

    Returns the verdict to act on. After a successful push the turn is re-judged
    from scratch — the push may have been the only thing wrong, or a backgrounded
    last command may still hold it open. After a refused push the branch is still
    unpushed, so the reason says so in the remote's own words.
    """
    if not (verdict.stopped and verdict.phase == "done" and verdict.unpushed > 0):
        return verdict, None
    pushed = push_lease_branch(conn, task_id)
    if pushed.pushed:
        return why_stopped(conn, task_id), pushed
    taken_back = _unpushed_reason(verdict.unpushed)
    verdict.reasons = [reason for reason in verdict.reasons if reason != taken_back]
    verdict.reasons.append(
        f"the branch has {verdict.unpushed} commit(s) no remote holds, and pushing it to "
        f"{pushed.remote}/{pushed.branch} on the worker's behalf was refused too:\n{pushed.stderr}"
    )
    return verdict, pushed


def latest_stop_verdict(conn, task_id: int) -> StopVerdict | None:
    """Rebuild the recorded verdict for a task, for `ppy task` and the default resume."""
    row = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (task_id, WORKER_STOPPED),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    return StopVerdict(
        stopped=True,
        reasons=list(payload.get("reasons") or []),
        unpushed=int(payload.get("unpushed") or 0),
        phase=payload.get("phase"),
        background_command=payload.get("background_command"),
        expected_phase=payload.get("expected_phase") or "done",
    )
