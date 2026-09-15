"""A turn that ended mid-gate is not `worker_done`.

Task 103 (2026-09-04): the Claude worker started the full backend suite, the
tool's foreground cap sent it to the background, the worker ended its turn to
wait for it, and the harness read that as `worker_done`. The suite died with the
session, the branch was never pushed, and no done note was ever filed. Task 104
had the same shape with browser smoke retries. The manager only noticed because
`ppy review show` had a "test" note as the newest report and nothing was pushed.

The push half of that is no longer the manager's job to finish by hand. A worker
that files its done note and still cannot get its commits onto the remote has its
own lease branch pushed for it (issue #50); only a refusal from the remote itself
leaves the task stopped, and then in the remote's words.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from papaya_agent_runtime import repos, turn_end
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.server import SupervisorServer


@pytest.fixture
def server(ppy_home):
    srv = SupervisorServer()
    srv.start_background()
    client = SupervisorClient(srv.socket_path)
    for _ in range(50):
        try:
            if client.ping().get("ok"):
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    yield srv, client
    srv.stop()


def _wait_status(client, task_id, wanted, timeout=20.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = client.task_status(task_id)["task"]["status"]
        if last in wanted:
            return last
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} never reached {wanted} (last {last})")


def _events(task_id, kind):
    conn = init_db()
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def _dispatch(client, source_repo, instructions: str) -> dict:
    added = repos.add_repo(source_repo)
    return client.dispatch_task(repo=added.name, title="run the gate", instructions=instructions)


def _git(path, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _branch_sha(remote_path, branch: str | None) -> str | None:
    """What the remote holds for a branch, or None when it has never seen it."""
    proc = subprocess.run(
        ["git", "-C", str(remote_path), "rev-parse", "--verify", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def _reject_pushes(source_repo, message: str) -> Path:
    """Give the remote a pre-receive hook that refuses, the way a real one does."""
    hook = Path(source_repo) / ".git" / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\necho '{message}' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    return hook


# --------------------------------------------------------------------------- #
# The compliant worker still finishes
# --------------------------------------------------------------------------- #


def test_a_worker_that_pushed_and_filed_a_done_note_is_done(server, source_repo) -> None:
    srv, client = server
    resp = _dispatch(client, source_repo, "")
    _wait_status(client, resp["task_id"], {"worker_done"})
    assert not _events(resp["task_id"], turn_end.WORKER_STOPPED)


def test_review_terminal_phase_needs_review_note_and_does_not_need_a_push(
    ppy_home, source_repo
) -> None:
    from papaya_agent_runtime import progress

    added = repos.add_repo(source_repo)
    conn = init_db()
    run_id = store.create_run(conn, "review it")
    task_id = store.add_task(
        conn,
        run_id=run_id,
        title="review handoff",
        repo_id=store.get_repo(conn, added.name)["id"],
        ends_at="review",
    )
    store.update_task_fields(
        conn,
        task_id,
        worktree_path=source_repo,
        branch="never-pushed",
        base_sha=_git(source_repo, "rev-parse", "HEAD"),
    )
    progress.record(task_id, phase="review", note="ready for the manager", conn=conn)

    verdict = turn_end.why_stopped(conn, task_id)
    assert verdict.stopped is False
    assert verdict.expected_phase == "review"
    assert verdict.unpushed == 0


# --------------------------------------------------------------------------- #
# Each trigger, on its own
# --------------------------------------------------------------------------- #


def test_no_done_note_stops_the_task_with_that_reason(server, source_repo) -> None:
    srv, client = server
    resp = _dispatch(client, source_repo, "NODONE")
    _wait_status(client, resp["task_id"], {turn_end.WORKER_STOPPED})

    stopped = _events(resp["task_id"], turn_end.WORKER_STOPPED)[-1]
    assert "no done note was ever filed" in stopped["summary"]
    assert not _events(resp["task_id"], "worker_done"), "a stopped turn is never also done"


def test_a_note_that_never_reached_done_stops_the_task(server, source_repo) -> None:
    """The incident's own shape: the newest report said `test`, and the turn ended."""
    srv, client = server
    resp = _dispatch(client, source_repo, "NODONE")
    from papaya_agent_runtime import progress

    _wait_status(client, resp["task_id"], {turn_end.WORKER_STOPPED})
    progress.record(resp["task_id"], phase="test", note="running the suite")
    conn = init_db()
    verdict = turn_end.why_stopped(conn, resp["task_id"])
    assert verdict.stopped
    assert "'test', not a done note" in verdict.reasons[0]


def test_a_backgrounded_last_command_stops_the_task(server, source_repo) -> None:
    srv, client = server
    resp = _dispatch(client, source_repo, "BACKGROUND")
    _wait_status(client, resp["task_id"], {turn_end.WORKER_STOPPED})

    stopped = _events(resp["task_id"], turn_end.WORKER_STOPPED)[-1]
    assert stopped["background_command"] == "make test"
    assert "killed when the turn ended" in stopped["summary"]
    assert stopped["phase"] == "done" and stopped["unpushed"] == 0, (
        "the other two signals were clean: the backgrounded command alone is enough"
    )


def test_a_cascaded_branch_is_not_mistaken_for_unpushed_work(server, source_repo) -> None:
    """A rebase upstream renames every commit; that is not work the worker withheld."""
    from papaya_agent_runtime import stacks

    srv, client = server
    resp = _dispatch(client, source_repo, "")
    _wait_status(client, resp["task_id"], {"worker_done"})
    task = client.task_status(resp["task_id"])["task"]

    # GitHub's cascade: main moves, the branch is rebased onto it upstream.
    with open(f"{source_repo}/moved.txt", "w", encoding="utf-8") as fh:
        fh.write("the layer below merged\n")
    _git(source_repo, "add", "-A")
    _git(source_repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "main moved")
    cascade = f"{source_repo}-cascade"
    _git(source_repo, "worktree", "add", "-q", cascade, task["branch"])
    _git(cascade, "-c", "user.name=t", "-c", "user.email=t@t", "rebase", "-q", "main")
    _git(source_repo, "worktree", "remove", "--force", cascade)

    conn = init_db()
    row = store.get_task(conn, resp["task_id"])
    assert stacks.unpushed_commits(conn, row) == 0
    assert turn_end.why_stopped(conn, resp["task_id"]).stopped is False


# --------------------------------------------------------------------------- #
# The one thing the harness finishes for the worker: the push
# --------------------------------------------------------------------------- #


def test_a_done_note_with_unpushed_commits_gets_its_branch_pushed_for_it(
    server, source_repo
) -> None:
    """The worker finished and only the push was missing, so the harness pushed it."""
    srv, client = server
    resp = _dispatch(client, source_repo, "NOPUSH")
    task_id = resp["task_id"]
    _wait_status(client, task_id, {"worker_done"})

    task = client.task_status(task_id)["task"]
    pushed = _events(task_id, turn_end.PUSHED_BY_MANAGER)
    assert len(pushed) == 1, "the branch is pushed once, by the harness, and named as such"
    assert pushed[0]["branch"] == task["branch"]
    assert _branch_sha(source_repo, task["branch"]) == pushed[0]["head_sha"]
    assert not _events(task_id, turn_end.WORKER_STOPPED), (
        "nothing was left for the manager to finish by hand"
    )


def test_a_worker_that_never_filed_a_done_note_is_not_pushed_for(server, source_repo) -> None:
    """A turn that stopped mid-gate is missing more than a push; pushing it would lie."""
    srv, client = server
    resp = _dispatch(client, source_repo, "NODONE NOPUSH")
    task_id = resp["task_id"]
    _wait_status(client, task_id, {turn_end.WORKER_STOPPED})

    task = client.task_status(task_id)["task"]
    assert not _events(task_id, turn_end.PUSHED_BY_MANAGER)
    assert _branch_sha(source_repo, task["branch"]) is None


def test_a_remote_that_refuses_the_push_keeps_the_task_stopped_in_its_own_words(
    server, source_repo
) -> None:
    srv, client = server
    _reject_pushes(source_repo, "the advisory scan is already failing on the default branch")
    resp = _dispatch(client, source_repo, "NOPUSH")
    task_id = resp["task_id"]
    _wait_status(client, task_id, {turn_end.WORKER_STOPPED})

    stopped = _events(task_id, turn_end.WORKER_STOPPED)[-1]
    assert "the advisory scan is already failing on the default branch" in stopped["summary"], (
        "the hook's own message is what tells the manager what to fix"
    )
    assert stopped["phase"] == "done" and stopped["unpushed"] >= 1
    assert not _events(task_id, turn_end.PUSHED_BY_MANAGER)


def test_pushing_a_stopped_task_by_hand_finishes_the_job(server, source_repo) -> None:
    """`ppy task push` is the same push, for when the hook that refused has been fixed."""
    from papaya_agent_runtime.cli import main

    srv, client = server
    hook = _reject_pushes(source_repo, "the advisory scan is already failing")
    resp = _dispatch(client, source_repo, "NOPUSH")
    task_id = resp["task_id"]
    _wait_status(client, task_id, {turn_end.WORKER_STOPPED})
    assert main(["task", "push", str(task_id)]) == 1, "while the hook refuses, so does ppy"

    hook.unlink()
    assert main(["task", "push", str(task_id)]) == 0
    task = client.task_status(task_id)["task"]
    assert _branch_sha(source_repo, task["branch"]) == _git(
        task["worktree_path"], "rev-parse", "HEAD"
    )
    assert _events(task_id, turn_end.PUSHED_BY_MANAGER)[-1]["branch"] == task["branch"]


# --------------------------------------------------------------------------- #
# What the manager sees, and what a bare resume sends
# --------------------------------------------------------------------------- #


def test_run_and_task_snapshots_name_the_reason(server, source_repo, capsys) -> None:
    from papaya_agent_runtime.cli import main

    srv, client = server
    resp = _dispatch(client, source_repo, "NODONE")
    task_id, run_id = resp["task_id"], resp["run_id"]
    _wait_status(client, task_id, {turn_end.WORKER_STOPPED})

    assert main(["run", str(run_id)]) == 0
    out = capsys.readouterr().out
    assert "worker_stopped" in out
    assert "no done note was ever filed" in out, "the kind alone is not actionable"

    assert main(["task", str(task_id)]) == 0
    out = capsys.readouterr().out
    assert "worker_stopped" in out
    assert "no done note was ever filed" in out
    assert "ppy resume" in out


def test_a_bare_resume_tells_the_worker_what_was_cut_short(server, source_repo) -> None:
    srv, client = server
    resp = _dispatch(client, source_repo, "NODONE NOPUSH")
    task_id = resp["task_id"]
    _wait_status(client, task_id, {turn_end.WORKER_STOPPED})

    assert client.resume_task(task_id)["ok"]
    resumed = _events(task_id, "resumed")[-1]
    message = resumed["message"]
    assert message, "a bare resume must not hand the worker silence"
    assert "commit(s) no remote holds" in message
    assert "foreground" in message and "background" in message
    assert "push your branch" in message

    # An explicit message still wins.
    _wait_status(client, task_id, {"worker_done", turn_end.WORKER_STOPPED})
    client.resume_task(task_id, "do this instead")
    assert _events(task_id, "resumed")[-1]["message"] == "do this instead"


def test_worker_stopped_is_a_real_lifecycle_status() -> None:
    from papaya_agent_runtime.lifecycle import TASK_STATUSES, TERMINAL_STATUSES

    assert turn_end.WORKER_STOPPED in TASK_STATUSES
    assert turn_end.WORKER_STOPPED not in TERMINAL_STATUSES, "a stopped turn still needs finishing"
    assert turn_end.WORKER_STOPPED in store.ACTIONABLE_KINDS
