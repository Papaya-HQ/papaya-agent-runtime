"""Finished work reaches the remote where a repository gates pushes.

Issue #83: fourteen times a worker committed, ran the push its rules prescribed,
and the repository's own hook refused it inside the harness. Each time the worker
correctly reported done, and each time the branch reached the remote only because
a manager turn happened to push it by hand. Middle Manager solved this by pushing
from the manager after its own gate; this is that, in the supervisor.

The rule is narrow on purpose. The runtime pushes only when the repository gates
pushes, the worker's newest note says ``done``, and the runtime's own gate is
green at the worktree's exact head — never with ``--no-verify``, never forced,
never twice, and never again after a refusal.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

import pytest

from papaya_agent_runtime import blockers, gate, turn_end
from papaya_agent_runtime.state import init_db, store


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
    }


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
    conn = init_db()
    try:
        return turn_end.deliver_finished_branch(conn, world["task_id"])
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


def test_a_repository_that_does_not_gate_pushes_keeps_the_workers_own_push(world) -> None:
    conn = init_db()
    conn.execute("UPDATE repos SET push_hook_runs_full_suite = 0 WHERE id = ?", (world["repo_id"],))
    conn.commit()
    conn.close()
    _note(world)
    _gate(world)

    assert _deliver(world) is None
    assert _remote_head(world) is None


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


def test_the_push_is_never_forced_and_never_skips_verification(world) -> None:
    """The one thing this must not become is a way round a repository's own hook."""
    seen: list[list[str]] = []
    real = subprocess.run

    def watch(args, *a, **kw):
        if isinstance(args, list) and "push" in args:
            seen.append(list(args))
        return real(args, *a, **kw)

    _note(world)
    _gate(world)
    import papaya_agent_runtime.turn_end as module

    original, module.subprocess.run = module.subprocess.run, watch
    try:
        _deliver(world)
    finally:
        module.subprocess.run = original

    assert seen, "the push did not run"
    for args in seen:
        assert "--no-verify" not in args
        assert "--force" not in args and "-f" not in args
        assert not any(a.startswith("--force") for a in args)


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
