"""Finished work reaches the remote where a repository gates pushes.

Issue #83: fourteen times a worker committed, ran the push its rules prescribed,
and the repository's own hook refused it inside the harness. Each time the worker
correctly reported done, and each time the branch reached the remote only because
a manager turn happened to push it by hand. Middle Manager solved this by pushing
from the manager after its own gate; this is that, in the supervisor.

The rule is narrow on purpose. The runtime pushes only when the repository gates
pushes, the worker's newest note reached the task's own terminal phase (``done``,
or ``review`` for a task that ends at review), and the runtime's own gate is green
at the worktree's exact SHA — never with ``--no-verify``, never forced, never
twice, and never again after a refusal. There is exactly ONE push decision per
ending (:func:`turn_end.deliver_after_turn`), which is why several of these tests
drive the real ``RunnerGuardian`` rather than the push alone.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime

import pytest

from papaya_agent_runtime import blockers, gate, turn_end
from papaya_agent_runtime.providers.base import TaskSpec
from papaya_agent_runtime.providers.fake import FakeProvider
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.runner import RunnerGuardian


def _git(*args: str, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def world(tmp_path, ppy_home):
    """A worktree on a lease branch, a bare remote, and a task that finished."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _git("init", "-q", "-b", "main", cwd=worktree)
    _git("config", "user.email", "t@example.com", cwd=worktree)
    _git("config", "user.name", "T", cwd=worktree)
    _git("remote", "add", "origin", str(remote), cwd=worktree)
    (worktree / "a.txt").write_text("one", encoding="utf-8")
    _git("add", "-A", cwd=worktree)
    _git("commit", "-q", "-m", "work", cwd=worktree)
    head = _git("rev-parse", "HEAD", cwd=worktree)

    conn = init_db()
    run_id = store.create_run(conn, "run")
    repo_id = store.add_repo(
        conn,
        name="gated",
        origin=str(remote),
        local_path=str(worktree),
        default_branch="main",
        base_sha=None,
    )
    conn.execute("UPDATE repos SET push_hook_runs_full_suite = 1 WHERE id = ?", (repo_id,))
    task_id = store.add_task(conn, run_id=run_id, title="w", repo_id=repo_id)
    branch = f"ppy/task-{task_id}-abc"
    store.add_lease(
        conn,
        lease_id=f"lease-{task_id}",
        repo_id=repo_id,
        task_id=task_id,
        branch=branch,
        worktree_path=str(worktree),
        base_sha=head,
        backend="git",
    )
    # The task row is authoritative about lease identity (`lifecycle.require_live_lease`).
    conn.execute(
        "UPDATE tasks SET lease_id = ?, worktree_path = ?, branch = ?, base_sha = ? WHERE id = ?",
        (f"lease-{task_id}", str(worktree), branch, head, task_id),
    )
    conn.execute("UPDATE tasks SET ends_at = 'done' WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return {
        "task_id": task_id,
        "run_id": run_id,
        "repo_id": repo_id,
        "worktree": worktree,
        "remote": remote,
        "branch": branch,
        "head": head,
        "spec": TaskSpec(
            task_id=task_id,
            title="w",
            # NOPUSH is what a worker in a gated repository does: its rules tell it
            # not to push, so the branch is only ever ahead when the turn ends.
            instructions="NOPUSH",
            worktree_path=str(worktree),
            base_sha=head,
            provider="fake",
            run_id=run_id,
            branch=branch,
        ),
    }


def _through_the_runner(world) -> list[list[str]]:
    """Run a whole worker ending and return every `git push` argv it attempted.

    The decision under test is made at the END of a run, so testing
    `deliver_finished_branch` alone cannot see a second push made before it — which
    is exactly the defect: `rescue_unpushed` pushed first, with no gate check.
    """
    attempted: list[list[str]] = []
    real = subprocess.Popen

    def watch(args, *a, **kw):
        if isinstance(args, list) and "push" in args:
            attempted.append(list(args))
        return real(args, *a, **kw)

    turn_end.subprocess.Popen = watch
    try:
        RunnerGuardian(FakeProvider()).run(world["spec"])
    finally:
        turn_end.subprocess.Popen = real
    return attempted


def _note(world, phase: str = "done") -> None:
    conn = init_db()
    store.append_event(
        conn,
        kind="worker_progress",
        payload={"phase": phase, "note": f"{phase} note"},
        run_id=world["run_id"],
        task_id=world["task_id"],
    )
    conn.commit()
    conn.close()


def _gate(world, *, green: bool = True, head: str | None = None) -> None:
    now = datetime.now(UTC).isoformat()
    result = gate.GateResult(
        repo="gated",
        command="make test",
        full=False,
        exit_code=0 if green else 1,
        duration_seconds=12.0,
        summary="all good" if green else "3 failed",
        head_sha=head or world["head"],
        output_path="",
        started_at=now,
        finished_at=now,
        task_id=world["task_id"],
    )
    conn = init_db()
    store.append_event(
        conn,
        kind=gate.GATE_RESULT,
        payload=result.as_dict(),
        run_id=world["run_id"],
        task_id=world["task_id"],
    )
    conn.commit()
    conn.close()


def _remote_head(world) -> str | None:
    found = subprocess.run(
        ["git", "rev-parse", "--verify", world["branch"]],
        cwd=str(world["remote"]),
        capture_output=True,
        text=True,
        check=False,
    )
    return found.stdout.strip() if found.returncode == 0 else None


def _events(world, kind: str) -> list[dict]:
    conn = init_db()
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id",
        (world["task_id"], kind),
    ).fetchall()
    conn.close()
    return [json.loads(r["payload"]) for r in rows]


def _deliver(world):
    """The ONE push decision, through the entry point the runner calls."""
    conn = init_db()
    try:
        verdict = turn_end.why_stopped(conn, world["task_id"])
        _, pushed = turn_end.deliver_after_turn(conn, world["task_id"], verdict)
        return pushed
    finally:
        conn.close()


# ── the push itself ─────────────────────────────────────────────────────────


def test_a_finished_worker_with_a_green_gate_gets_its_branch_pushed(world) -> None:
    _note(world)
    _gate(world)

    pushed = _deliver(world)

    assert pushed is not None and pushed.pushed is True
    assert _remote_head(world) == world["head"]
    (recorded,) = _events(world, turn_end.PUSHED_AFTER_GATE)
    assert recorded["head_sha"] == world["head"]
    assert recorded["branch"] == world["branch"]
    assert "after its own gate was green" in recorded["summary"]


def test_a_red_gate_is_not_pushed(world) -> None:
    _note(world)
    _gate(world, green=False)

    assert _deliver(world) is None
    assert _remote_head(world) is None


def test_a_gate_green_at_an_earlier_head_is_not_pushed(world) -> None:
    """The gate has to be green at the head being delivered, not at some head.

    A worker that committed again after its gate has an ungated head, and pushing
    it would deliver code nothing ever ran.
    """
    _note(world)
    _gate(world)
    (world["worktree"] / "b.txt").write_text("two", encoding="utf-8")
    _git("add", "-A", cwd=world["worktree"])
    _git("commit", "-q", "-m", "more", cwd=world["worktree"])

    assert _deliver(world) is None
    assert _remote_head(world) is None


def test_a_worker_that_did_not_report_done_is_not_pushed(world) -> None:
    _note(world, phase="test")
    _gate(world)

    assert _deliver(world) is None
    assert _remote_head(world) is None


def test_a_stream_line_after_the_done_note_does_not_hide_it(world) -> None:
    """`worker_progress` is two things; only a note with a phase is a report.

    A phaseless stream line recorded after the done note used to read as "no done
    note was ever filed" (CI run 35132060840).
    """
    _note(world)
    conn = init_db()
    store.append_event(
        conn,
        kind="worker_progress",
        payload={"chatter": "still going"},
        run_id=world["run_id"],
        task_id=world["task_id"],
    )
    conn.commit()
    conn.close()
    _gate(world)

    pushed = _deliver(world)

    assert pushed is not None and pushed.pushed is True


def test_a_repository_that_does_not_gate_pushes_keeps_the_old_rescue_path(world) -> None:
    """Unchanged where nothing gates pushes: the worker pushes, and this is the net.

    `rescue_unpushed` is what runs there, exactly as it did before this task — it
    asks no gate, because in an ungated repository the worker pushes its own branch
    and this only catches one that finished without doing so.
    """
    conn = init_db()
    conn.execute("UPDATE repos SET push_hook_runs_full_suite = 0 WHERE id = ?", (world["repo_id"],))
    conn.commit()
    conn.close()
    _note(world)
    # Deliberately NO gate result: the ungated path must not start asking for one.

    pushed = _deliver(world)

    assert pushed is not None and pushed.pushed is True
    assert _remote_head(world) == world["head"]
    # The old event, not the gated one: these are different decisions and say so.
    assert _events(world, turn_end.PUSHED_BY_MANAGER)
    assert _events(world, turn_end.PUSHED_AFTER_GATE) == []


# ── doing it twice, and being refused ───────────────────────────────────────


def test_a_branch_already_at_that_head_is_not_pushed_again(world) -> None:
    _note(world)
    _gate(world)
    first = _deliver(world)
    assert first is not None and first.pushed is True

    second = _deliver(world)

    assert second is not None
    assert second.pushed is False
    assert second.already is True
    assert second.delivered is True
    # One push event, not two: a second call is a `rev-parse` and nothing else.
    assert len(_events(world, turn_end.PUSHED_AFTER_GATE)) == 1


def test_a_rejected_push_is_a_recorded_blocker_and_is_never_retried(world) -> None:
    """A remote that says no says no again. It waits for a person, not for a loop."""
    _note(world)
    _gate(world)
    # The remote moves ahead on the same branch, so a non-forced push is rejected.
    _git("push", "-q", "origin", f"HEAD:{world['branch']}", cwd=world["worktree"])
    (world["worktree"] / "c.txt").write_text("three", encoding="utf-8")
    _git("add", "-A", cwd=world["worktree"])
    _git("commit", "-q", "-m", "theirs", cwd=world["worktree"])
    _git("push", "-q", "origin", f"HEAD:{world['branch']}", cwd=world["worktree"])
    _git("reset", "-q", "--hard", world["head"], cwd=world["worktree"])
    ahead = _remote_head(world)

    pushed = _deliver(world)

    assert pushed is not None and pushed.pushed is False
    assert _remote_head(world) == ahead  # nothing was forced over it
    (recorded,) = _events(world, turn_end.PUSH_REFUSED)
    assert recorded["branch"] == world["branch"]
    assert recorded["reason"]
    raised = [b for b in blockers.Ledger.load().open.values() if b.code == blockers.PUSH_REFUSED]
    assert len(raised) == 1
    assert world["branch"] in raised[0].title
    assert any("ppy task push" in step for step in raised[0].steps)
    assert any("Never `--no-verify`" in step for step in raised[0].steps)


def test_a_push_that_succeeds_later_clears_the_blocker(world) -> None:
    _note(world)
    _gate(world)
    blockers.set_push_refused(world["task_id"], "gated", world["branch"], "it said no")
    assert any(b.code == blockers.PUSH_REFUSED for b in blockers.Ledger.load().open.values())

    pushed = _deliver(world)

    assert pushed is not None and pushed.pushed is True
    assert not any(b.code == blockers.PUSH_REFUSED for b in blockers.Ledger.load().open.values())


def test_the_push_is_never_forced_and_never_skips_verification(world, monkeypatch) -> None:
    """The one thing this must not become is a way round a repository's own hook."""
    seen: list[list[str]] = []
    real = subprocess.Popen

    def watch(args, *a, **kw):
        if isinstance(args, list) and "push" in args:
            seen.append(list(args))
        return real(args, *a, **kw)

    _note(world)
    _gate(world)
    monkeypatch.setattr(turn_end.subprocess, "Popen", watch)

    _deliver(world)

    assert seen, "the push did not run"
    for args in seen:
        assert "--no-verify" not in args
        assert "--force" not in args and "-f" not in args
        assert not any(a.startswith("--force") for a in args)
        # The verified SHA, never a moving `HEAD`.
        assert f"{world['head']}:refs/heads/{world['branch']}" in args


# ── how long it waits ───────────────────────────────────────────────────────


def test_a_gated_push_waits_on_the_repositorys_own_budget(world, monkeypatch) -> None:
    """Not a fixed short timeout: a hook with a suite behind it needs the real number.

    Neither repository on record has a git pre-push hook today, so this push is
    network-only — but `solicit` does detect and record one, and a fixed 30s would
    kill a healthy gated push part-way the day a repository grows one.
    """
    monkeypatch.setattr(gate, "expected_seconds", lambda repo, full: 1800.0)
    conn = init_db()
    task = store.get_task(conn, world["task_id"])
    assert turn_end.push_wait_seconds(conn, task) == 1800.0

    # Floored, so a repository with no history still gets a fair wait...
    monkeypatch.setattr(gate, "expected_seconds", lambda repo, full: None)
    assert turn_end.push_wait_seconds(conn, task) == turn_end.GATED_PUSH_FLOOR_SECONDS
    # ...and capped, so nothing waits on a push for ever.
    monkeypatch.setattr(gate, "expected_seconds", lambda repo, full: 99999.0)
    assert turn_end.push_wait_seconds(conn, task) == turn_end.GATED_PUSH_CEILING_SECONDS

    # A repository that does not gate pushes is network and credentials only.
    conn.execute("UPDATE repos SET push_hook_runs_full_suite = 0 WHERE id = ?", (world["repo_id"],))
    conn.commit()
    assert turn_end.push_wait_seconds(conn, task) == turn_end.PUSH_SECONDS
    conn.close()


def test_a_push_that_outlasts_its_wait_is_reported_not_left_running(world) -> None:
    _note(world)
    _gate(world)

    conn = init_db()
    try:
        pushed = turn_end.push_lease_branch(conn, world["task_id"], timeout=0.000001)
    finally:
        conn.close()

    # Either it beat the timeout or it was stopped and said so; never a hang, and
    # never a silent success that left the branch behind.
    assert pushed.pushed or "was still running" in pushed.stderr


# ── ONE decision, seen through the runner ───────────────────────────────────


def test_the_runner_pushes_nothing_when_the_gate_is_red(world) -> None:
    """The defect this replaces: `rescue_unpushed` pushed this before any gate ran.

    It fires on "newest note is terminal and the branch is ahead", and in a gated
    repository the worker never pushes, so "ahead" is always true.
    """
    _note(world)
    _gate(world, green=False)

    attempted = _through_the_runner(world)

    assert attempted == []
    assert _remote_head(world) is None
    assert _events(world, turn_end.PUSHED_BY_MANAGER) == []
    assert _events(world, turn_end.PUSHED_AFTER_GATE) == []


def test_the_runner_pushes_nothing_when_no_gate_ever_ran(world) -> None:
    _note(world)

    attempted = _through_the_runner(world)

    assert attempted == []
    assert _remote_head(world) is None


def test_the_runner_pushes_nothing_when_the_gate_is_green_for_an_older_sha(world) -> None:
    """A worker that committed after its gate has a head nothing has ever run."""
    _note(world)
    _gate(world)  # green at world["head"]
    (world["worktree"] / "late.txt").write_text("after the gate", encoding="utf-8")
    _git("add", "-A", cwd=world["worktree"])
    _git("commit", "-q", "-m", "after the gate", cwd=world["worktree"])

    attempted = _through_the_runner(world)

    assert attempted == []
    assert _remote_head(world) is None


def test_the_runner_pushes_once_when_the_gate_is_green_at_head(world) -> None:
    _note(world)
    # The fake worker commits its own file, so the gate has to be green at whatever
    # head the run ends on — record it after the run and drive the decision again.
    attempted = _through_the_runner(world)
    assert attempted == []  # nothing yet: no gate at that head

    head = _git("rev-parse", "HEAD", cwd=world["worktree"])
    _gate(world, head=head)
    conn = init_db()
    try:
        turn_end.deliver_after_turn(
            conn, world["task_id"], turn_end.why_stopped(conn, world["task_id"])
        )
    finally:
        conn.close()

    assert _remote_head(world) == head
    assert len(_events(world, turn_end.PUSHED_AFTER_GATE)) == 1
    assert _events(world, turn_end.PUSHED_BY_MANAGER) == []


def test_a_refused_push_is_attempted_exactly_once_in_one_ending(world) -> None:
    """Two push paths meant a refusal was followed at once by a second full attempt.

    In these repositories a push runs the repository's suite, so that was two suites
    for one ending.
    """
    _note(world)
    _gate(world)
    # Put the remote ahead of us on the same branch: a non-forced push is rejected.
    _git("push", "-q", "origin", f"HEAD:{world['branch']}", cwd=world["worktree"])
    (world["worktree"] / "theirs.txt").write_text("theirs", encoding="utf-8")
    _git("add", "-A", cwd=world["worktree"])
    _git("commit", "-q", "-m", "theirs", cwd=world["worktree"])
    _git("push", "-q", "origin", f"HEAD:{world['branch']}", cwd=world["worktree"])
    _git("reset", "-q", "--hard", world["head"], cwd=world["worktree"])
    ahead = _remote_head(world)

    attempted: list[list[str]] = []
    real = subprocess.Popen

    def watch(args, *a, **kw):
        if isinstance(args, list) and "push" in args:
            attempted.append(list(args))
        return real(args, *a, **kw)

    turn_end.subprocess.Popen = watch
    try:
        conn = init_db()
        try:
            turn_end.deliver_after_turn(
                conn, world["task_id"], turn_end.why_stopped(conn, world["task_id"])
            )
        finally:
            conn.close()
    finally:
        turn_end.subprocess.Popen = real

    assert len(attempted) == 1, f"one attempt, not {len(attempted)}"
    assert _remote_head(world) == ahead
    assert len(_events(world, turn_end.PUSH_REFUSED)) == 1
    assert _events(world, turn_end.PUSHED_AFTER_GATE) == []


def test_a_task_that_ends_at_review_is_delivered_on_its_review_note(world) -> None:
    """`ends_at=review` never files a `done` note, and its branch still must arrive.

    Keying on the literal phase `done` left every review-ending task in a gated
    repository unpushed.
    """
    conn = init_db()
    conn.execute("UPDATE tasks SET ends_at = 'review' WHERE id = ?", (world["task_id"],))
    conn.commit()
    conn.close()
    _note(world, phase="review")
    _gate(world)

    conn = init_db()
    try:
        verdict = turn_end.why_stopped(conn, world["task_id"])
        _, pushed = turn_end.deliver_after_turn(conn, world["task_id"], verdict)
    finally:
        conn.close()

    assert verdict.expected_phase == "review"
    assert pushed is not None and pushed.pushed is True
    assert _remote_head(world) == world["head"]


def test_a_review_task_that_only_reached_implement_is_not_pushed(world) -> None:
    conn = init_db()
    conn.execute("UPDATE tasks SET ends_at = 'review' WHERE id = ?", (world["task_id"],))
    conn.commit()
    conn.close()
    _note(world, phase="implement")
    _gate(world)

    assert _deliver(world) is None
    assert _remote_head(world) is None


# ── what the push starts, and what it is given ──────────────────────────────


def _hook(world, body: str) -> None:
    """Install a real git pre-push hook in the worktree's repository."""
    hooks = world["worktree"] / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(body, encoding="utf-8")
    hook.chmod(0o755)


def test_a_timed_out_push_takes_the_whole_process_group_with_it(world) -> None:
    """`subprocess.run(timeout=)` kills only git, and orphans everything the hook ran.

    A repository's pre-push hook starts `make verify`, which starts a compiler, a
    test runner, Docker and a database. Killing `git` alone leaves all of them
    running while the runtime records "was stopped" — the machine stays busy for
    another twenty minutes and nothing knows why.
    """
    marker = world["worktree"] / "child-survived.txt"
    _hook(
        world,
        f"#!/bin/sh\n( sleep 5; echo alive > {marker} ) &\nwait\n",
    )
    _note(world)
    _gate(world)

    conn = init_db()
    try:
        pushed = turn_end.push_lease_branch(conn, world["task_id"], timeout=0.5)
    finally:
        conn.close()

    assert pushed.pushed is False
    assert "was still running" in pushed.stderr
    assert "everything it had started" in pushed.stderr
    # The grandchild the hook started must be gone, not merely orphaned.
    time.sleep(6)
    assert not marker.exists(), "the hook's child outlived the push that was stopped"


def test_a_push_hook_is_given_the_tasks_own_environment(world) -> None:
    """The hook runs the repository's suite, which needs the TASK's database stack.

    The gate gets it through `gate.gate_env`; a push that inherited the supervisor's
    shell would run that suite against the shared stack instead, and pass or fail
    for reasons belonging to another task.
    """
    conn = init_db()
    conn.execute(
        "UPDATE repos SET compose_stack = 'docker-compose.yml', db_port_base = 54000, "
        "db_url_template = 'postgresql://localhost:{port}/{name}_{task_id}' WHERE id = ?",
        (world["repo_id"],),
    )
    conn.commit()
    conn.close()

    seen = world["worktree"] / "hook-env.txt"
    _hook(
        world,
        "#!/bin/sh\n"
        f'printf "%s\\n%s\\n" "$COMPOSE_PROJECT_NAME" "$DATABASE_URL" > {seen}\n'
        "exit 0\n",
    )
    _note(world)
    _gate(world)

    pushed = _deliver(world)

    assert pushed is not None and pushed.pushed is True
    said = seen.read_text(encoding="utf-8").splitlines()
    assert said[0] == f"task_{world['task_id']}"
    assert f"/gated_{world['task_id']}" in said[1]
    assert str(54000 + world["task_id"]) in said[1]


def test_a_failing_pre_push_hook_is_a_blocker_with_its_own_words(world) -> None:
    """A repository's git hook still runs and is never bypassed."""
    _hook(world, "#!/bin/sh\necho 'make verify failed: 3 tests red' >&2\nexit 1\n")
    _note(world)
    _gate(world)

    pushed = _deliver(world)

    assert pushed is not None and pushed.pushed is False
    assert "make verify failed: 3 tests red" in pushed.stderr
    assert _remote_head(world) is None
    (recorded,) = _events(world, turn_end.PUSH_REFUSED)
    assert "make verify failed" in recorded["reason"]


def test_the_remote_is_asked_not_the_local_tracking_ref(world) -> None:
    """A worker can move `refs/remotes/origin/<branch>`; `git` is in its profile.

    Trusting that ref would record a branch as delivered that was never pushed.
    """
    _note(world)
    _gate(world)
    # Exactly what a worker could do: claim the branch is already up there.
    _git(
        "update-ref",
        f"refs/remotes/origin/{world['branch']}",
        world["head"],
        cwd=world["worktree"],
    )

    pushed = _deliver(world)

    assert pushed is not None
    assert pushed.already is False, "the local tracking ref was believed"
    assert pushed.pushed is True
    assert _remote_head(world) == world["head"]
