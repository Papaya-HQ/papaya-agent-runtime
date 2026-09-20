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

import contextlib
import json
import logging
import os
import signal
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
#: Asking the remote what it holds is a network round trip and nothing more.
LS_REMOTE_SECONDS = 60.0


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
        repo = conn.execute("SELECT name FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
    except (sqlite3.Error, IndexError, KeyError):
        return
    if repo is None or not push_is_gated(conn, task):
        return
    budgets.observe(
        repo["name"],
        budgets.FULL_SUITE,
        seconds,
        task_id=int(task["id"]),
        outcome="pass" if returncode == 0 else "fail",
        conn=conn,
    )


def push_is_gated(conn, task) -> bool:
    """Does this task's repository stop a worker pushing? THE predicate, used by all.

    Three things used to ask this question in two different ways: the dispatch rules
    (any registered `PreToolUse` Bash hook) against the push and its timeout (the
    recorded column). A repository matching one and not the other told its worker not
    to push and then had nothing gate-aware to push for it, so the finished branch
    fell through to the ungated rescue path. One answer now, from
    `environment.push_is_gated`, which is itself the OR of the two sources.
    """
    from papaya_agent_runtime import environment

    try:
        repo_row = conn.execute("SELECT * FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
    except (sqlite3.Error, IndexError, KeyError):
        return False
    if repo_row is None:
        return False
    return environment.push_is_gated(repo_row, task["worktree_path"])


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
        repo = conn.execute("SELECT name FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
    except (sqlite3.Error, IndexError, KeyError):
        return PUSH_SECONDS
    if repo is None or not push_is_gated(conn, task):
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
    # The REMOTE, not the local tracking ref. `git` is in the worker's profile, so a
    # worker can `git update-ref refs/remotes/origin/<branch> <sha>` and make an
    # unpushed branch look delivered; the tracking ref is a cache, not evidence.
    found = subprocess.run(
        ["git", "-C", worktree, "ls-remote", "--exit-code", remote, f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=LS_REMOTE_SECONDS,
    )
    if found.returncode != 0:
        return False
    line = found.stdout.split("\n", 1)[0].split("\t")
    return bool(line) and line[0].strip() == sha


#: How long the group gets to go down politely before it is killed outright.
PUSH_TERM_GRACE_SECONDS = 10.0


def _run_push(
    args: list[str], wait: float, env: dict[str, str] | None
) -> tuple[int, str, str, bool]:
    """Run a push in its OWN process group, and take the whole group down on timeout.

    `subprocess.run(timeout=)` kills only the process it started. A repository's
    pre-push hook runs `make verify`, which starts a compiler, a test runner, Docker
    and a database; killing `git` orphans every one of them, and the runtime would
    record "was stopped" while the machine stayed busy for another twenty minutes.

    Returns ``(returncode, stdout, stderr, timed_out)``.
    """
    proc = subprocess.Popen(  # noqa: S603 - argv is built here, never from a worker
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,  # its own process group, so the group can be signalled
    )
    try:
        out, err = proc.communicate(timeout=wait)
        return proc.returncode, out or "", err or "", False
    except subprocess.TimeoutExpired:
        _end_group(proc)
        out, err = proc.communicate()
        return proc.returncode or 1, out or "", err or "", True


def _end_group(proc: subprocess.Popen) -> None:
    """Terminate, then kill, everything the push started. Never raises."""
    for signal_number, grace in (
        (signal.SIGTERM, PUSH_TERM_GRACE_SECONDS),
        (signal.SIGKILL, 0.0),
    ):
        try:
            os.killpg(os.getpgid(proc.pid), signal_number)
        except (ProcessLookupError, PermissionError, OSError):
            # Already gone, or no group to signal: fall back to the process itself.
            with contextlib.suppress(OSError):
                proc.kill()
            return
        if grace:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=grace)
                return


def _push_env(conn, task) -> dict[str, str] | None:
    """The environment the push gets: the same one this task's gate would get.

    A repository that gates pushes runs its suite from the pre-push hook, and that
    suite needs the task's private database stack and ports — the very thing
    `gate.gate_env` builds. Without it the hook would run against the shared stack
    and could pass or fail for reasons belonging to another task.

    ``None`` when nothing is recorded for the repository, which means "inherit", the
    behaviour every push had before.
    """
    from papaya_agent_runtime import environment, gate

    try:
        repo_row = conn.execute("SELECT * FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
        if repo_row is None:
            return None
        values = environment.render_task_env(conn, repo_row, int(task["id"]))
        return gate.gate_env(dict(os.environ), values, environment.for_repo(repo_row))
    except Exception as exc:  # noqa: BLE001 - a push must not fail on a missing stack
        log.warning("[turn_end] Could not build task %s's push environment: %s", task["id"], exc)
        return None


#: What each push event's summary says, so one push is one event with the right words.
_PUSH_SUMMARY = {
    PUSHED_BY_MANAGER: (
        "the worker left {short} on {branch} without pushing it; the harness pushed "
        "that branch to {remote} itself"
    ),
    PUSHED_AFTER_GATE: (
        "the worker finished at {short} and this repository gates pushes, so the runtime "
        "pushed {branch} to {remote} itself after its own gate was green at that head"
    ),
}


def push_lease_branch(
    conn,
    task_id: int,
    *,
    timeout: float | None = None,
    record_kind: str = PUSHED_BY_MANAGER,
    expect_sha: str | None = None,
) -> PushResult:
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
    if not sha:
        return PushResult(
            task_id=task_id,
            pushed=False,
            branch=branch,
            remote=remote,
            stderr="the worktree has no readable HEAD commit to push",
        )
    if expect_sha and sha != expect_sha:
        # Something committed between the decision and here, so what the caller
        # judged is not what would go up. Nothing is sent.
        return PushResult(
            task_id=task_id,
            pushed=False,
            branch=branch,
            remote=remote,
            sha=sha,
            stderr=(
                f"the worktree moved from {expect_sha[:8]} to {sha[:8]} after the decision "
                "to push it, so nothing was pushed"
            ),
        )
    started = time.monotonic()
    wait = push_wait_seconds(conn, task) if timeout is None else timeout
    # The VERIFIED sha, not `HEAD`: the ref that was checked against the remote is the
    # ref that goes up, so nothing can move underneath the check. Still no force and
    # no `--no-verify` — every hook the repository has runs.
    code, out, err, timed_out = _run_push(
        ["git", "-C", worktree, "push", remote, f"{sha}:refs/heads/{branch}"],
        wait,
        _push_env(conn, task),
    )
    if timed_out:
        _observe_push_hook(conn, task, time.monotonic() - started, 1)
        return PushResult(
            task_id=task_id,
            pushed=False,
            branch=branch,
            remote=remote,
            sha=sha,
            stderr=(
                f"the push was still running after {int(wait)}s and was stopped, with "
                "everything it had started. "
                + (_last_lines(err) or _last_lines(out) or "It printed nothing.")
            ),
        )
    proc = subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=err)
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
        # ONE event per push. The caller says which, so a gated delivery is not also
        # recorded as a rescue: two events for one push read as two pushes.
        kind=record_kind,
        payload={
            "task_id": task_id,
            "branch": branch,
            "remote": remote,
            "head_sha": sha,
            "summary": _PUSH_SUMMARY.get(record_kind, _PUSH_SUMMARY[PUSHED_BY_MANAGER]).format(
                short=(sha or "")[:8], branch=branch, remote=remote
            ),
        },
        run_id=task["run_id"],
        task_id=task_id,
    )
    return result


def deliver_after_turn(
    conn, task_id: int, verdict: StopVerdict
) -> tuple[StopVerdict, PushResult | None]:
    """**The** push decision for a worker's ending. Exactly one push, or none.

    There used to be two, run back to back: `rescue_unpushed` pushed whenever the
    newest note was terminal and the branch was ahead — with no gate check at all —
    and `deliver_finished_branch` then found the branch already there. In a gated
    repository the worker never pushes, so "ahead" is always true, and a red-gate,
    stale-gate or never-gated head went to the remote. Worse, a refused rescue was
    followed immediately by a second push: two full pre-push suites for one ending.

    So the branch is chosen once, here:

    - a repository that **gates pushes** (:func:`push_is_gated`) goes through
      :func:`deliver_finished_branch`, which requires the gate verdict to be green at
      the worktree's exact SHA;
    - any other repository keeps :func:`rescue_unpushed` exactly as it was — the
      worker pushes there itself, and this is only the safety net for one that
      finished and did not.
    """
    task = store.get_task(conn, task_id)
    if task is None:
        return verdict, None
    if push_is_gated(conn, task):
        return deliver_finished_branch(conn, task_id, verdict)
    return rescue_unpushed(conn, task_id, verdict)


def deliver_finished_branch(
    conn, task_id: int, verdict: StopVerdict | None = None
) -> tuple[StopVerdict, PushResult | None]:
    """Push a finished worker's branch where the repository gates pushes.

    Fourteen times (issue #83) a worker finished, ran exactly the push its rules
    prescribed, and the repository's own hook refused it inside the harness. The
    work shipped only because a manager turn happened to push by hand. So the
    runtime pushes here instead, where no Claude hook applies — the repository's own
    git hooks still run and are never bypassed, and the suite the hook exists to
    protect has already run as the runtime's own gate at this exact head.

    Conditions, all of them:

    - the repository gates pushes (:func:`push_is_gated`);
    - the worker's newest note reached the task's own terminal phase — ``done``, or
      ``review`` for a task that ends at review and never files a ``done`` note;
    - the runtime's gate is green at the worktree's **exact** SHA.

    The verdict comes back re-judged after a successful push: in a gated repository
    the worker was told not to push, so "the branch has commits no remote holds" is
    not a reason it stopped, and leaving it in would mark a finished worker stopped.
    A refused push is recorded as a blocker for a person and never retried: the
    remote or a hook said no, and saying it again cannot change the answer.
    """
    from papaya_agent_runtime import gate

    verdict = why_stopped(conn, task_id) if verdict is None else verdict
    task = store.get_task(conn, task_id)
    if task is None:
        return verdict, None
    worktree = task["worktree_path"]
    try:
        repo = conn.execute("SELECT name FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
    except (sqlite3.Error, IndexError, KeyError):
        return verdict, None
    if repo is None or not worktree or not push_is_gated(conn, task):
        return verdict, None
    if _newest_phase(conn, task_id) != verdict.expected_phase:
        return verdict, None
    # At the worktree's exact SHA. `gate.verdict` answers only about the head the
    # worktree is on now, so a green from before the last commit cannot stand in —
    # and the SHA it names is the one the push sends.
    decided = gate.verdict(task_id)
    if decided.state != gate.GREEN:
        return verdict, None
    pushed = push_lease_branch(
        conn, task_id, record_kind=PUSHED_AFTER_GATE, expect_sha=decided.head_sha or None
    )
    if pushed.already or pushed.pushed:
        # Delivered. `push_lease_branch` recorded the one event for it.
        return why_stopped(conn, task_id), pushed
    _record_push_blocker(conn, task, pushed, str(repo["name"]))
    verdict.stopped = True
    verdict.reasons.append(
        f"this repository gates pushes, so the runtime pushed {pushed.branch} to "
        f"{pushed.remote} after its gate was green — and that was refused too:\n{pushed.stderr}"
    )
    return verdict, pushed


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
