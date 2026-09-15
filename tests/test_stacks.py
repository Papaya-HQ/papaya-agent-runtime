"""Stacks are first-class: a chain of tasks, a view of it, and cascade awareness.

Tasks 88-100 on the backend monorepo (2026-09-03) were each dispatched from
``main`` and merged serially, costing a rebase and a CI rerun per merge, because
the only stack mechanics `ppy` had were "remember the previous branch name and
pass it as --base". These tests pin the four things that replace that.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime

import pytest

from conftest import scale
from papaya_agent_runtime import delivery, repos, stacks
from papaya_agent_runtime.config import MMConfig, save_config
from papaya_agent_runtime.review import record_review
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


def _git(path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(path, name: str, message: str) -> str:
    with open(f"{path}/{name}", "w", encoding="utf-8") as fh:
        fh.write(message + "\n")
    _git(path, "add", "-A")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)
    return _git(path, "rev-parse", "HEAD")


def _wait_terminal(client, task_id, timeout=15.0):
    deadline = time.monotonic() + scale(timeout)
    while time.monotonic() < deadline:
        status = client.task_status(task_id)["task"]["status"]
        if status in {"worker_done", "blocked", "failed"}:
            return status
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} never finished")


# --------------------------------------------------------------------------- #
# 1. `ppy dispatch --stack-on <task>` derives the base and records the chain
# --------------------------------------------------------------------------- #


def test_stack_on_starts_from_the_parent_task_s_branch_and_records_the_chain(
    server, source_repo
) -> None:
    srv, client = server
    added = repos.add_repo(source_repo)

    first = client.dispatch_task(repo=added.name, title="bottom layer")
    _wait_terminal(client, first["task_id"])
    bottom = client.task_status(first["task_id"])["task"]
    # The bottom layer's work has to be on the remote before anything can build on it.
    subprocess.run(
        ["git", "-C", bottom["worktree_path"], "push", "-q", "origin", f"HEAD:{bottom['branch']}"],
        check=True,
    )
    bottom_tip = _git(bottom["worktree_path"], "rev-parse", "HEAD")

    second = client.dispatch_task(
        repo=added.name, title="next layer", run_id=first["run_id"], stack_on=first["task_id"]
    )
    assert second["ok"], second
    _wait_terminal(client, second["task_id"])
    task = client.task_status(second["task_id"])["task"]

    assert task["stacked_on"] == bottom["branch"], "the base came from the parent's lease branch"
    assert task["stacked_on_task"] == first["task_id"]
    assert task["base_sha"] == bottom_tip
    subprocess.run(
        ["git", "-C", task["worktree_path"], "merge-base", "--is-ancestor", bottom_tip, "HEAD"],
        check=True,
    )


def test_stack_on_accepts_a_parent_whose_branch_has_not_been_pushed_yet(
    server, source_repo
) -> None:
    """The next layer is briefed with the previous one; it must not wait for a push.

    On 2026-09-05 (issue #57) ``--stack-on 127`` was refused two minutes after
    task 127 was dispatched: its lease branch existed only in its worktree, and
    the child's start did ``git fetch <forge> <branch>`` and nothing else. The
    branch is a local ref in the shared base clone the whole time.
    """
    from papaya_agent_runtime.worktree import LeaseManager

    srv, client = server
    added = repos.add_repo(source_repo)

    # A parent still in progress: a real lease worktree on the registered clone
    # with one commit of its own and no push. (A finished worker no longer models
    # this — the harness pushes a done-but-unpushed lease branch for it.)
    conn = init_db()
    repo_row = store.get_repo(conn, added.name)
    run_id = store.create_run(conn, "stack")
    parent_id = store.add_task(conn, run_id=run_id, title="bottom layer", repo_id=repo_row["id"])
    lease = LeaseManager().acquire(
        repo_path=repo_row["local_path"], repo_id=repo_row["id"], task_id=parent_id
    )
    store.update_task_fields(
        conn,
        parent_id,
        branch=lease.branch,
        worktree_path=lease.worktree_path,
        base_sha=lease.base_sha,
        lease_id=lease.id,
    )
    store.set_task_status(conn, parent_id, "in_progress")
    bottom_tip = _commit(lease.worktree_path, "bottom.txt", "work the forge has not seen")
    on_remote = _git(source_repo, "ls-remote", "--heads", ".", lease.branch)
    assert on_remote == "", "the premise: the parent's branch is not on the forge"

    second = client.dispatch_task(
        repo=added.name, title="next layer", run_id=run_id, stack_on=parent_id
    )
    assert second["ok"], second
    task = client.task_status(second["task_id"])["task"]
    assert task["stacked_on"] == lease.branch
    assert task["stacked_on_task"] == parent_id
    assert task["base_sha"] == bottom_tip, "the child starts from the parent's local tip"
    subprocess.run(
        ["git", "-C", task["worktree_path"], "merge-base", "--is-ancestor", bottom_tip, "HEAD"],
        check=True,
    )
    # Naming the lease branch directly, instead of the task, is accepted the same way.
    third = client.dispatch_task(
        repo=added.name, title="by branch", run_id=run_id, base=lease.branch
    )
    assert third["ok"], third
    assert client.task_status(third["task_id"])["task"]["base_sha"] == bottom_tip


def test_stacking_on_a_task_with_no_branch_is_refused_plainly(server, source_repo) -> None:
    srv, client = server
    added = repos.add_repo(source_repo)
    conn = init_db()
    run_id = store.create_run(conn, "stack")
    orphan = store.add_task(conn, run_id=run_id, title="never leased")

    resp = client.dispatch_task(repo=added.name, title="on nothing", stack_on=orphan)
    assert not resp.get("ok")
    assert "has no branch yet" in resp["error"]

    resp = client.dispatch_task(repo=added.name, title="on nobody", stack_on=99999)
    assert not resp.get("ok")
    assert "no such task" in resp["error"]


# --------------------------------------------------------------------------- #
# 2. `ppy stack` renders the stack bottom-up
# --------------------------------------------------------------------------- #


def _two_layer_stack(conn) -> tuple[int, int, int]:
    run_id = store.create_run(conn, "stack")
    bottom = store.add_task(conn, run_id=run_id, title="bottom layer")
    store.update_task_fields(conn, bottom, branch="ppy/task-1-aaa")
    top = store.add_task(conn, run_id=run_id, title="top layer")
    store.update_task_fields(
        conn, top, branch="ppy/task-2-bbb", stacked_on="ppy/task-1-aaa", stacked_on_task=bottom
    )
    return run_id, bottom, top


def test_stack_renders_bottom_up_with_pr_base_and_cascade_state(ppy_home, monkeypatch) -> None:
    conn = init_db()
    run_id, bottom, top = _two_layer_stack(conn)
    prs = {
        "ppy/task-1-aaa": {"number": 11, "baseRefName": "main", "state": "OPEN", "mergedAt": None},
        # The top layer still targets the branch below it: the cascade has not run.
        "ppy/task-2-bbb": {
            "number": 12,
            "baseRefName": "ppy/task-1-aaa",
            "state": "OPEN",
            "mergedAt": None,
        },
    }
    monkeypatch.setattr(stacks, "pull_request_for", lambda branch, **kw: prs.get(branch))

    # Naming either layer renders the same stack, bottom-up.
    for identifier in (bottom, top):
        layers = stacks.stack_layers(identifier)
        assert [layer.task_id for layer in layers] == [bottom, top]
    layers = stacks.stack_layers(top)
    assert layers[0].pr_number == 11
    assert layers[1].pr_number == 12
    assert layers[1].parent_task_id == bottom
    assert layers[1].pr_base == "ppy/task-1-aaa"
    assert layers[1].cascade_pending is False, "the base still matches the layer below"
    assert layers[1].review == "not reviewed"

    rendered = stacks.render_stack(layers)
    assert rendered.index("bottom layer") < rendered.index("top layer")
    assert "PR #12 (OPEN) -> base ppy/task-1-aaa" in rendered

    # After the cascade the top layer's pull request targets main instead.
    prs["ppy/task-2-bbb"]["baseRefName"] = "main"
    layers = stacks.stack_layers(top)
    assert layers[1].cascade_pending is True
    assert any("cascade" in note for note in layers[1].notes)

    # A run id renders the run's tasks; a missing gh is tolerated, not fatal.
    monkeypatch.setattr(stacks, "pull_request_for", lambda branch, **kw: None)
    layers = stacks.stack_layers(run_id)
    assert [layer.task_id for layer in layers] == [bottom, top]
    assert layers[0].pr_number is None
    assert any("no pull request" in note for note in layers[0].notes)


def test_stack_cli_renders_and_refuses_an_unknown_id(ppy_home, monkeypatch, capsys) -> None:
    from papaya_agent_runtime.cli import main

    conn = init_db()
    _run_id, _bottom, top = _two_layer_stack(conn)
    monkeypatch.setattr(stacks, "pull_request_for", lambda branch, **kw: None)

    assert main(["stack", str(top)]) == 0
    out = capsys.readouterr().out
    assert "bottom layer" in out
    assert out.index("bottom layer") < out.index("top layer")

    assert main(["stack", "424242"]) == 1
    assert "no task or run" in capsys.readouterr().err


def test_native_stack_merge_retargets_then_stops_on_the_next_red_check(
    ppy_home, source_repo, monkeypatch
) -> None:
    cfg = MMConfig()
    cfg.authority.merge = True
    save_config(cfg)
    added = repos.add_repo(source_repo)
    conn = init_db()
    repo = store.get_repo(conn, added.name)
    run_id = store.create_run(conn, "three layers")
    bottom = store.add_task(conn, run_id=run_id, title="bottom", repo_id=repo["id"])
    middle = store.add_task(conn, run_id=run_id, title="middle", repo_id=repo["id"])
    top = store.add_task(conn, run_id=run_id, title="top", repo_id=repo["id"])
    store.update_task_fields(
        conn, bottom, branch="stack/bottom", worktree_path=source_repo, base_sha="1" * 40
    )
    store.update_task_fields(
        conn,
        middle,
        branch="stack/middle",
        worktree_path=source_repo,
        base_sha="2" * 40,
        stacked_on="stack/bottom",
        stacked_on_task=bottom,
    )
    store.update_task_fields(
        conn,
        top,
        branch="stack/top",
        worktree_path=source_repo,
        base_sha="3" * 40,
        stacked_on="stack/middle",
        stacked_on_task=middle,
    )
    prs = {
        "stack/bottom": {
            "number": 11,
            "baseRefName": "main",
            "state": "OPEN",
            "mergedAt": None,
            "mergeCommit": None,
        },
        "stack/middle": {
            "number": 12,
            "baseRefName": "stack/bottom",
            "state": "OPEN",
            "mergedAt": None,
            "mergeCommit": None,
        },
        "stack/top": {
            "number": 13,
            "baseRefName": "stack/middle",
            "state": "OPEN",
            "mergedAt": None,
            "mergeCommit": None,
        },
    }
    by_number = {pr["number"]: branch for branch, pr in prs.items()}

    def fake_forge(argv, *, cwd=None):
        if argv[1:3] == ["pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(prs[argv[3]]), "")
        if argv[1:3] == ["pr", "checks"]:
            number = int(argv[3])
            checks = (
                [{"name": "Integration", "bucket": "fail"}]
                if number == 12
                else [{"name": "Unit", "bucket": "pass"}]
            )
            return subprocess.CompletedProcess(
                argv, 1 if number == 12 else 0, json.dumps(checks), ""
            )
        if argv[1:3] == ["pr", "merge"]:
            number = int(argv[3])
            pr = prs[by_number[number]]
            pr.update(
                state="MERGED",
                mergedAt="2026-09-11T18:00:00Z",
                mergeCommit={"oid": "a" * 40},
            )
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1:3] == ["pr", "edit"]:
            number = int(argv[3])
            prs[by_number[number]]["baseRefName"] = argv[5]
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)

    monkeypatch.setattr(stacks, "_pr_tool", lambda: "gh")
    monkeypatch.setattr(stacks, "_run_forge", fake_forge)
    result = stacks.merge_stack(bottom, all_layers=True, sleep=lambda _seconds: None)

    assert [item["task_id"] for item in result.merged] == [bottom]
    assert prs["stack/middle"]["baseRefName"] == "main"
    assert prs["stack/top"]["baseRefName"] == "stack/middle"
    assert result.stopped and "Integration" in result.stopped
    assert store.get_task(conn, bottom)["merged_sha"] == "a" * 40


def test_deliver_refuses_composed_open_task_commits_but_not_a_merged_sibling(
    server, source_repo
) -> None:
    _srv, client = server
    added = repos.add_repo(source_repo)
    bottom_resp = client.dispatch_task(repo=added.name, title="bottom")
    _wait_terminal(client, bottom_resp["task_id"])
    top_resp = client.dispatch_task(
        repo=added.name,
        title="top",
        run_id=bottom_resp["run_id"],
        stack_on=bottom_resp["task_id"],
    )
    _wait_terminal(client, top_resp["task_id"])
    record_review(top_resp["task_id"], "approved")

    with pytest.raises(delivery.DeliveryError) as refused:
        delivery.deliver(top_resp["task_id"], push=False, open_pr=False, base="main")
    message = str(refused.value)
    assert f"task {top_resp['task_id']}" in message
    assert f"task {bottom_resp['task_id']}" in message

    conn = init_db()
    store.update_task_fields(
        conn,
        bottom_resp["task_id"],
        merged_sha="b" * 40,
        merged_at=datetime.now(UTC).isoformat(),
    )
    assert (
        delivery.deliver(top_resp["task_id"], push=False, open_pr=False, base="main").task_id
        == top_resp["task_id"]
    )


# --------------------------------------------------------------------------- #
# 3. Cascade awareness before resume and deliver
# --------------------------------------------------------------------------- #


def _task_on_a_pushed_branch(client, source_repo, title="layer") -> dict:
    added = repos.add_repo(source_repo)
    resp = client.dispatch_task(repo=added.name, title=title)
    _wait_terminal(client, resp["task_id"])
    task = client.task_status(resp["task_id"])["task"]
    subprocess.run(
        ["git", "-C", task["worktree_path"], "push", "-q", "origin", f"HEAD:{task['branch']}"],
        check=True,
    )
    return task


def _cascade_remote_branch(source_repo, branch: str) -> str:
    """Stand in for GitHub's cascade: the branch is rebased onto a moved default.

    This is the shape that matters — every commit on the branch is *replayed*
    under a new SHA. A check that counts SHAs sees that as unpushed work and
    refuses; the branch is not diverged, it is exactly one rebase ahead.
    """
    with open(f"{source_repo}/moved.txt", "w", encoding="utf-8") as fh:
        fh.write("the layer below merged\n")
    _git(source_repo, "add", "-A")
    _git(source_repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "main moved")
    worktree = f"{source_repo}-cascade"
    _git(source_repo, "worktree", "add", "-q", worktree, branch)
    _git(worktree, "-c", "user.name=t", "-c", "user.email=t@t", "rebase", "-q", "main")
    rewritten = _git(worktree, "rev-parse", "HEAD")
    _git(source_repo, "worktree", "remove", "--force", worktree)
    return rewritten


def test_a_rebased_remote_branch_moves_the_worktree_and_records_an_event(
    server, source_repo
) -> None:
    srv, client = server
    task = _task_on_a_pushed_branch(client, source_repo)
    rewritten = _cascade_remote_branch(source_repo, task["branch"])

    result = stacks.sync_worktree_with_remote(task["id"])
    assert result["action"] == "reset"
    assert _git(task["worktree_path"], "rev-parse", "HEAD") == rewritten
    conn = init_db()
    events = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = 'worktree_cascaded'",
        (task["id"],),
    ).fetchall()
    assert len(events) == 1
    assert rewritten in events[0]["payload"]

    # Second call: nothing left to do.
    assert stacks.sync_worktree_with_remote(task["id"])["action"] == "none"


def test_a_diverged_worktree_is_refused_with_both_commits_named(server, source_repo) -> None:
    srv, client = server
    task = _task_on_a_pushed_branch(client, source_repo)
    rewritten = _cascade_remote_branch(source_repo, task["branch"])
    local = _commit(task["worktree_path"], "local.txt", "work the remote never saw")

    with pytest.raises(stacks.StackError) as exc:
        stacks.sync_worktree_with_remote(task["id"])
    message = str(exc.value)
    assert local[:8] in message
    assert rewritten[:8] in message
    assert "force-push" in message
    # Nothing was moved or destroyed.
    assert _git(task["worktree_path"], "rev-parse", "HEAD") == local


def test_resume_and_deliver_refuse_a_diverged_worktree(server, source_repo) -> None:
    from papaya_agent_runtime.review import record_review

    srv, client = server
    task = _task_on_a_pushed_branch(client, source_repo)
    _cascade_remote_branch(source_repo, task["branch"])
    _commit(task["worktree_path"], "local.txt", "work the remote never saw")
    record_review(task["id"], "approved")

    resp = client.resume_task(task["id"], "carry on")
    assert not resp.get("ok")
    assert "have both moved" in resp["error"]

    with pytest.raises(delivery.DeliveryError) as exc:
        delivery.deliver(task["id"], open_pr=False)
    assert "have both moved" in str(exc.value)


def test_deliver_cascades_a_stale_worktree_and_then_wants_the_new_head_reviewed(
    server, source_repo
) -> None:
    """The cascade lands first; the exact-HEAD gate then does its job on the new commit."""
    from papaya_agent_runtime.review import record_review

    srv, client = server
    task = _task_on_a_pushed_branch(client, source_repo)
    stale_head = _git(task["worktree_path"], "rev-parse", "HEAD")
    record_review(task["id"], "approved")
    rewritten = _cascade_remote_branch(source_repo, task["branch"])
    assert rewritten != stale_head

    with pytest.raises(delivery.DeliveryError) as exc:
        delivery.deliver(task["id"], open_pr=False)
    assert "re-review" in str(exc.value)
    # The worktree moved onto the cascade before the gate spoke.
    assert _git(task["worktree_path"], "rev-parse", "HEAD") == rewritten

    record_review(task["id"], "approved")
    result = delivery.deliver(task["id"], open_pr=False)
    assert result.head_sha == rewritten
    assert _git(source_repo, "rev-parse", task["branch"]) == rewritten


# --------------------------------------------------------------------------- #
# 4. A layer whose parent has merged targets the default branch
# --------------------------------------------------------------------------- #


def test_deliver_targets_main_when_the_layer_below_merged(ppy_home, monkeypatch) -> None:
    from types import SimpleNamespace

    conn = init_db()
    run_id, bottom, top = _two_layer_stack(conn)
    store.update_task_fields(conn, top, worktree_path="/tmp/wt")
    calls: list[list[str]] = []

    monkeypatch.setattr(
        delivery,
        "_run",
        lambda argv, cwd=None: (
            calls.append(argv)
            or SimpleNamespace(returncode=0, stdout="https://example/pr/12\n", stderr="")
        ),
    )
    monkeypatch.setattr(delivery, "is_approved_at_head", lambda tid: (True, ""))
    monkeypatch.setattr(delivery, "head_sha", lambda wt: "f" * 40)
    monkeypatch.setattr(delivery, "_pr_tool", lambda: "gh")

    # The layer below is still open: the pull request targets it, as before.
    monkeypatch.setattr(
        stacks,
        "pull_request_for",
        lambda branch, **kw: {"number": 11, "baseRefName": "main", "mergedAt": None},
    )
    res = delivery.deliver(top)
    pr_argv = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    assert pr_argv[pr_argv.index("--base") + 1] == "ppy/task-1-aaa"

    # Once it has merged, its branch is no base for a new pull request.
    calls.clear()
    monkeypatch.setattr(
        stacks,
        "pull_request_for",
        lambda branch, **kw: {
            "number": 11,
            "baseRefName": "main",
            "mergedAt": "2026-09-03T00:00:00Z",
        },
    )
    res = delivery.deliver(top)
    pr_argv = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    assert pr_argv[pr_argv.index("--base") + 1] == "main"
    assert "has merged" in res.note
    assert "cascade has not retargeted it" in res.note


# --------------------------------------------------------------------------- #
# 5. An upper layer is not opened past an unmerged parent (issue #61)
# --------------------------------------------------------------------------- #


def _deliverable(monkeypatch, calls: list[list[str]]) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        delivery,
        "_run",
        lambda argv, cwd=None: (
            calls.append(argv)
            or SimpleNamespace(returncode=0, stdout="https://example/pr/12\n", stderr="")
        ),
    )
    monkeypatch.setattr(delivery, "is_approved_at_head", lambda tid: (True, ""))
    monkeypatch.setattr(delivery, "head_sha", lambda wt: "f" * 40)
    monkeypatch.setattr(delivery, "_pr_tool", lambda: "gh")


def test_deliver_refuses_an_upper_layer_against_main_while_its_parent_is_unmerged(
    ppy_home, monkeypatch
) -> None:
    conn = init_db()
    _run_id, bottom, top = _two_layer_stack(conn)
    # The recorded starting branch is main — the shape a hand-repaired row takes —
    # while the chain still says this layer sits on the bottom one.
    store.update_task_fields(conn, top, worktree_path="/tmp/wt", stacked_on="main")
    calls: list[list[str]] = []
    _deliverable(monkeypatch, calls)
    monkeypatch.setattr(stacks, "pull_request_for", lambda branch, **kw: None)

    with pytest.raises(delivery.DeliveryError) as exc:
        delivery.deliver(top)
    message = str(exc.value)
    assert f'task {bottom} "bottom layer" (branch ppy/task-1-aaa)' in message
    assert "has not merged" in message
    assert "--base main" in message
    assert not any(a[:3] == ["gh", "pr", "create"] for a in calls), "nothing was opened"

    # --base is the override: the manager said main and meant it.
    delivery.deliver(top, base="main")
    pr_argv = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    assert pr_argv[pr_argv.index("--base") + 1] == "main"

    # A parent recorded as merged is no longer in the way.
    calls.clear()
    store.set_task_status(conn, top, "worker_done")
    store.update_task_fields(conn, bottom, merged_at="2026-09-06T00:00:00+00:00")
    delivery.deliver(top)
    assert any(a[:3] == ["gh", "pr", "create"] for a in calls)


def test_deliver_refuses_an_upper_layer_whose_parent_branch_is_not_on_the_forge(
    ppy_home, monkeypatch
) -> None:
    """The case #57 makes common: layer 2 finishes before layer 1 has pushed."""
    conn = init_db()
    _run_id, bottom, top = _two_layer_stack(conn)
    store.update_task_fields(conn, top, worktree_path="/tmp/wt")
    calls: list[list[str]] = []
    _deliverable(monkeypatch, calls)
    monkeypatch.setattr(stacks, "pull_request_for", lambda branch, **kw: None)
    monkeypatch.setattr(stacks, "_branch_on_remote", lambda wt, remote, branch: False)

    with pytest.raises(delivery.DeliveryError) as exc:
        delivery.deliver(top)
    message = str(exc.value)
    assert f'task {bottom} "bottom layer" (branch ppy/task-1-aaa)' in message
    assert "has not been pushed to the forge" in message
    assert f"ppy task push {bottom}" in message
    assert not any(a[:3] == ["gh", "pr", "create"] for a in calls)

    # Once the parent's branch is on the forge, the layer opens against it as before.
    monkeypatch.setattr(stacks, "_branch_on_remote", lambda wt, remote, branch: True)
    store.set_task_status(conn, top, "worker_done")
    delivery.deliver(top)
    pr_argv = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    assert pr_argv[pr_argv.index("--base") + 1] == "ppy/task-1-aaa"
