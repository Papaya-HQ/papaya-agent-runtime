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
import logging
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from papaya_agent_runtime.state import store

log = logging.getLogger("papaya_agent_runtime.turn_end")

WORKER_STOPPED = "worker_stopped"
PUSHED_BY_MANAGER = "pushed_by_manager"
#: The runtime's own gate was green at the worker's head and a hook gates pushes
#: there, so the runtime pushed the lease branch without the worker ever trying.
PUSHED_AFTER_GATE = "pushed_after_gate"
#: A push the remote or a hook refused. A blocker for a person, never a retry.
PUSH_REFUSED = "push_refused"

#: How long a plain push may take: network and credentials only.
PUSH_SECONDS = 180.0
#: The least a push may be given where a hook runs a suite behind it, however small
#: the repository's recorded budget is.
GATED_PUSH_FLOOR_SECONDS = 600.0
#: The most any push is waited on, whatever a budget says. A push still running after
#: this is killed and reported, never left holding the worker's slot.
GATED_PUSH_CEILING_SECONDS = 3600.0


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
    #: The remote already held this head, so nothing was pushed and nothing is wrong.
    #: Not a failure: the work is delivered, which is what the caller wanted.
    already: bool = False

    @property
    def delivered(self) -> bool:
        """Is the head on the remote now — whether this call put it there or not?"""
        return self.pushed or self.already

    @property
    def note(self) -> str:
        if self.pushed:
            return f"pushed {(self.sha or '')[:8]} to {self.remote}/{self.branch}"
        target = f"{self.remote}/{self.branch}" if self.branch else "its lease branch"
        if self.already:
            return f"{target} already held {(self.sha or '')[:8]}; nothing to push"
        return f"nothing was pushed to {target} — {self.stderr}"


def _last_lines(text: str, count: int = 20) -> str:
    lines = [line for line in (text or "").strip().splitlines() if line.strip()]
    return "\n".join(lines[-count:])


def _text(raw: object) -> str:
    """`subprocess` hands back bytes or str depending on how it failed."""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return raw if isinstance(raw, str) else ""


def _observe_push_hook(conn, task, seconds: float, returncode: int) -> None:
    """A push through a pre-push hook that runs the full suite is a full-suite run."""
    from papaya_agent_runtime import budgets

    try:
        repo = conn.execute(
            "SELECT name, push_hook_runs_full_suite FROM repos WHERE id = ?", (task["repo_id"],)
        ).fetchone()
    except (sqlite3.Error, IndexError, KeyError):
        return
    if repo is None or not repo["push_hook_runs_full_suite"]:
        return
    budgets.observe(
        repo["name"],
        budgets.FULL_SUITE,
        seconds,
        task_id=int(task["id"]),
        outcome="pass" if returncode == 0 else "fail",
        conn=conn,
    )


def push_wait_seconds(conn, task) -> float:
    """How long to wait on this repository's push, from what its gate history says.

    A plain push is network and credentials. A push in a repository that gates
    pushes may have a whole suite behind it, so a fixed short timeout would kill a
    perfectly healthy one part-way; the repository's own recorded full-suite budget
    is the honest number, floored so a repository with no history still gets a fair
    wait and capped so nothing holds a worker's slot indefinitely.
    """
    from papaya_agent_runtime import gate

    try:
        repo = conn.execute(
            "SELECT name, push_hook_runs_full_suite FROM repos WHERE id = ?", (task["repo_id"],)
        ).fetchone()
    except (sqlite3.Error, IndexError, KeyError):
        return PUSH_SECONDS
    if repo is None or not repo["push_hook_runs_full_suite"]:
        return PUSH_SECONDS
    budget = gate.expected_seconds(str(repo["name"]), full=True) or 0.0
    return min(max(budget, GATED_PUSH_FLOOR_SECONDS), GATED_PUSH_CEILING_SECONDS)


def _remote_has(worktree: str, remote: str, branch: str, sha: str | None) -> bool:
    """Is ``branch`` on ``remote`` already at ``sha``? Then there is nothing to push.

    Read from the worktree's own remote-tracking ref, which every push through this
    module updates — no network, and no second push of a head already delivered.
    """
    if not sha:
        return False
    found = subprocess.run(
        ["git", "-C", worktree, "rev-parse", "--verify", f"refs/remotes/{remote}/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return found.returncode == 0 and found.stdout.strip() == sha


def push_lease_branch(conn, task_id: int, *, timeout: float | None = None) -> PushResult:
    """Push a task's worktree head to its own lease branch. Never forces.

    The branch is named after the task and nothing else writes to it, so this is
    the one push the harness can make on a worker's behalf without deciding whose
    commits survive. A rejected push is not raised: it comes back as a result
    carrying the remote's own words, which is what the manager needs to read.

    A branch the remote already holds at this head is not pushed again, so calling
    this twice — the rounds, a resume, a person — costs one `rev-parse` and changes
    nothing. ``timeout`` defaults to what the repository's gate history says
    (:func:`push_wait_seconds`); a push that outlasts it is killed and reported.
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
    if _remote_has(worktree, remote, branch, sha):
        return PushResult(
            task_id=task_id,
            pushed=False,
            branch=branch,
            remote=remote,
            sha=sha,
            already=True,
            stderr=f"{remote}/{branch} is already at {(sha or '')[:8]}",
        )
    started = time.monotonic()
    wait = push_wait_seconds(conn, task) if timeout is None else timeout
    try:
        proc = subprocess.run(
            ["git", "-C", worktree, "push", remote, f"HEAD:{branch}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=wait,
        )
    except subprocess.TimeoutExpired as expired:
        _observe_push_hook(conn, task, time.monotonic() - started, 1)
        return PushResult(
            task_id=task_id,
            pushed=False,
            branch=branch,
            remote=remote,
            sha=sha,
            stderr=(
                f"the push was still running after {int(wait)}s and was stopped. "
                + (_last_lines(_text(expired.stderr)) or "It printed nothing.")
            ),
        )
    _observe_push_hook(conn, task, time.monotonic() - started, proc.returncode)
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
    try:
        from papaya_agent_runtime import blockers

        blockers.clear_push_refused(task_id, branch)
    except Exception as exc:  # noqa: BLE001 - a stale card must not fail the push
        log.warning("[turn_end] Could not clear task %s's push blocker: %s", task_id, exc)
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


def deliver_finished_branch(conn, task_id: int) -> PushResult | None:
    """Push a finished worker's branch where the repository gates pushes.

    Fourteen times (issue #83) a worker finished, ran exactly the push its rules
    prescribed, and the repository's own hook refused it inside the harness. The
    work shipped only because a manager turn happened to push by hand. So the
    runtime pushes here instead, where no Claude hook applies — the repository's own
    git hooks still run and are never bypassed, and the suite the hook exists to
    protect has already run as the runtime's own gate at this exact head.

    Conditions, all of them:

    - the repository gates pushes (``repos.push_hook_runs_full_suite``);
    - the worker's newest note says ``done``;
    - the runtime's gate is green at the worktree's exact HEAD.

    Returns None when a condition is not met — that is the ordinary case and not a
    failure. A refused push is recorded as a blocker for a person, never retried:
    the remote or a hook said no, and saying it again cannot change the answer.
    """
    from papaya_agent_runtime import gate

    task = store.get_task(conn, task_id)
    if task is None:
        return None
    worktree = task["worktree_path"]
    try:
        repo = conn.execute(
            "SELECT name, push_hook_runs_full_suite FROM repos WHERE id = ?", (task["repo_id"],)
        ).fetchone()
    except (sqlite3.Error, IndexError, KeyError):
        return None
    if repo is None or not repo["push_hook_runs_full_suite"] or not worktree:
        return None
    if _newest_phase(conn, task_id) != "done":
        return None
    # At the worktree's exact HEAD: `gate.verdict` only answers about the head the
    # worktree is on now, so a green from before the last commit cannot stand in.
    if gate.verdict(task_id).state != gate.GREEN:
        return None
    pushed = push_lease_branch(conn, task_id)
    if pushed.already:
        return pushed
    if pushed.pushed:
        store.append_event(
            conn,
            kind=PUSHED_AFTER_GATE,
            payload={
                "task_id": task_id,
                "branch": pushed.branch,
                "remote": pushed.remote,
                "head_sha": pushed.sha,
                "repo": str(repo["name"]),
                "summary": (
                    f"the worker finished at {(pushed.sha or '')[:8]} and this repository "
                    f"gates pushes, so the runtime pushed {pushed.branch} to {pushed.remote} "
                    "itself after its own gate was green at that head"
                ),
            },
            run_id=task["run_id"],
            task_id=task_id,
        )
        return pushed
    _record_push_blocker(conn, task, pushed, str(repo["name"]))
    return pushed


def _newest_phase(conn, task_id: int) -> str | None:
    """The phase of the worker's newest progress NOTE, not of any stream line.

    `store.progress_events` is notes only and newest first, which is the whole point:
    a `worker_progress` stream line the runner records after the done note used to
    read as "no done note was ever filed" (CI run 35132060840).
    """
    for event in store.progress_events(conn, task_id=task_id):
        try:
            payload = json.loads(event["payload"])
        except (TypeError, ValueError):
            continue
        phase = payload.get("phase")
        if phase:
            return str(phase)
    return None


def _record_push_blocker(conn, task, pushed: PushResult, repo: str) -> None:
    """A refused push is a person's decision, so it is recorded once and left alone."""
    store.append_event(
        conn,
        kind=PUSH_REFUSED,
        payload={
            "task_id": int(task["id"]),
            "branch": pushed.branch,
            "remote": pushed.remote,
            "head_sha": pushed.sha,
            "repo": repo,
            "reason": pushed.stderr,
            "summary": (
                f"the runtime's push of {pushed.branch} to {pushed.remote} was refused; "
                "the work is committed and unpushed until a person decides"
            ),
        },
        run_id=task["run_id"],
        task_id=int(task["id"]),
    )
    try:
        from papaya_agent_runtime import blockers

        blockers.set_push_refused(int(task["id"]), repo, pushed.branch or "", pushed.stderr)
    except Exception as exc:  # noqa: BLE001 - the event above is the record that matters
        log.warning("[turn_end] Could not raise a blocker for task %s's push: %s", task["id"], exc)


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
    if pushed.delivered:
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
