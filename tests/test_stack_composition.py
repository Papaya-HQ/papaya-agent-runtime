"""The composition check tells a stack apart from work carried across tasks.

On 2026-09-17 a parent task and its child (dispatched with ``--stack-on`` while
the parent's branch had no commits yet) were delivered bottom-up, as the stack
view said to. The child's ``base_sha`` was the default branch's, so once it
rebased onto the parent's pushed head it "owned" the parent's commits, and
delivering the parent was refused as a composition with its own child.

These tests pin both halves: a correctly formed stack delivers bottom-up with no
bookkeeping repair, and a genuine composition is still refused.
"""

from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime

import pytest

from conftest import wait_until
from papaya_agent_runtime import delivery, repos, stacks
from papaya_agent_runtime.review import record_review
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.server import SupervisorServer
from papaya_agent_runtime.worktree import LeaseManager


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


@pytest.fixture(autouse=True)
def no_forge(monkeypatch):
    """No real ``gh`` is ever asked about a pull request from these fixtures."""
    monkeypatch.setattr(stacks, "_pr_tool", lambda: None)
    monkeypatch.setattr(delivery, "_pr_tool", lambda: None)


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


def _push(task) -> None:
    _git(task["worktree_path"], "push", "-q", "-f", "origin", f"HEAD:{task['branch']}")


def _rebase_onto(task, parent) -> None:
    """What a worker does once its parent has pushed: rebase onto the parent's head."""
    worktree = task["worktree_path"]
    _git(worktree, "fetch", "-q", "origin", parent["branch"])
    _git(worktree, "-c", "user.name=t", "-c", "user.email=t@t", "rebase", "-q", "FETCH_HEAD")


def _wait_terminal(client, task_id, timeout=15.0):
    def finished():
        status = client.task_status(task_id)["task"]["status"]
        return status if status in {"worker_done", "blocked", "failed"} else None

    return wait_until(finished, timeout, what=f"task {task_id} to finish", interval=0.1)


def _task(task_id: int):
    return store.get_task(init_db(), task_id)


def _in_progress_task(repo_name: str, title: str, run_id: int | None = None) -> int:
    """A task still being worked on: a real lease worktree whose branch has no commits yet."""
    conn = init_db()
    repo_row = store.get_repo(conn, repo_name)
    run_id = run_id or store.create_run(conn, "stack")
    task_id = store.add_task(conn, run_id=run_id, title=title, repo_id=repo_row["id"])
    lease = LeaseManager().acquire(
        repo_path=repo_row["local_path"], repo_id=repo_row["id"], task_id=task_id
    )
    store.update_task_fields(
        conn,
        task_id,
        branch=lease.branch,
        worktree_path=lease.worktree_path,
        base_sha=lease.base_sha,
        lease_id=lease.id,
    )
    store.set_task_status(conn, task_id, "in_progress")
    return task_id


def _stack_on(client, repo_name: str, parent_id: int, title: str) -> int:
    """Stack a child on a parent whose branch may still be empty.

    That is refused at dispatch since runtime #94 item 3; these fixtures reproduce
    the empty-parent shape on purpose, so they wave the check through on the record.
    """
    parent = _task(parent_id)
    resp = client.dispatch_task(
        repo=repo_name,
        title=title,
        run_id=parent["run_id"],
        stack_on=parent_id,
        accepted_preflight=[{"check": "empty-parent", "overridden": None}],
        preflight_reason="reproducing an empty-parent stack",
    )
    assert resp["ok"], resp
    _wait_terminal(client, resp["task_id"])
    return resp["task_id"]


def _deliver(task_id: int, **kwargs):
    record_review(task_id, "approved")
    return delivery.deliver(task_id, push=False, open_pr=False, **kwargs)


def _stack_dispatched_onto_an_empty_parent(client, source_repo):
    """The 2026-09-17 shape, up to the moment the parent was delivered.

    The child is dispatched onto the parent's still-empty branch, so its
    ``base_sha`` is the default branch's. The parent then pushes two commits,
    and the child rebases onto that pushed head and adds a commit of its own.
    """
    added = repos.add_repo(source_repo)
    main_sha = _git(source_repo, "rev-parse", "main")
    parent_id = _in_progress_task(added.name, "parent layer")
    child_id = _stack_on(client, added.name, parent_id, "child layer")
    assert _task(child_id)["base_sha"] == main_sha, "the premise: the parent had no commits"

    parent = _task(parent_id)
    _commit(parent["worktree_path"], "parent-1.txt", "parent work, first")
    parent_head = _commit(parent["worktree_path"], "parent-2.txt", "parent work, second")
    _push(parent)

    child = _task(child_id)
    _rebase_onto(child, parent)
    child_own = _commit(child["worktree_path"], "child.txt", "child work")
    _push(child)
    return added, parent_id, child_id, parent_head, child_own


# --------------------------------------------------------------------------- #
# G1: a parent is not refused because its child rebased onto it
# --------------------------------------------------------------------------- #


def test_a_parent_delivers_after_its_child_rebased_onto_its_pushed_head(
    server, source_repo, monkeypatch
) -> None:
    _srv, client = server
    added, parent_id, child_id, parent_head, _child_own = _stack_dispatched_onto_an_empty_parent(
        client, source_repo
    )
    main_sha = _git(source_repo, "rev-parse", "main")
    assert _task(child_id)["base_sha"] == main_sha, "the check must not rely on a refreshed base"

    delivered = _deliver(parent_id)
    assert delivered.head_sha == parent_head

    # The child then opens against the parent's branch, not the default branch.
    parent_branch = _task(parent_id)["branch"]
    real_run = delivery._run
    opened: list[list[str]] = []

    def forge(argv, cwd=None):
        if argv[0] == "gh":
            opened.append(argv)
            if argv[1:3] == ["pr", "list"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, "https://example/pull/2\n", "")
        return real_run(argv, cwd=cwd)

    monkeypatch.setattr(delivery, "_pr_tool", lambda: "gh")
    monkeypatch.setattr(delivery, "_run", forge)
    record_review(child_id, "approved")
    result = delivery.deliver(child_id, push=False)
    assert result.pr_url == "https://example/pull/2"
    create = next(argv for argv in opened if argv[1:3] == ["pr", "create"])
    assert create[create.index("--base") + 1] == parent_branch


def test_a_parent_delivers_while_its_child_still_sits_on_the_default_branch(
    server, source_repo
) -> None:
    """The child never rebased: its base is main's and so is its history."""
    _srv, client = server
    added = repos.add_repo(source_repo)
    parent_id = _in_progress_task(added.name, "parent layer")
    child_id = _stack_on(client, added.name, parent_id, "child layer")
    parent = _task(parent_id)
    _commit(parent["worktree_path"], "parent-1.txt", "parent work, first")
    _commit(parent["worktree_path"], "parent-2.txt", "parent work, second")
    _push(parent)

    assert _deliver(parent_id).task_id == parent_id
    assert _task(child_id)["base_sha"] == _git(source_repo, "rev-parse", "main")


def test_every_layer_of_a_three_deep_stack_delivers_bottom_up(server, source_repo) -> None:
    """A <- B <- C, each dispatched onto an empty branch and rebased once the one below pushed."""
    _srv, client = server
    added = repos.add_repo(source_repo)
    a_id = _in_progress_task(added.name, "bottom layer")
    b_id = _stack_on(client, added.name, a_id, "middle layer")
    c_id = _stack_on(client, added.name, b_id, "top layer")

    a = _task(a_id)
    _commit(a["worktree_path"], "a.txt", "bottom work")
    _push(a)
    b = _task(b_id)
    _rebase_onto(b, a)
    _commit(b["worktree_path"], "b.txt", "middle work")
    _push(b)
    c = _task(c_id)
    _rebase_onto(c, b)
    _commit(c["worktree_path"], "c.txt", "top work")
    _push(c)

    conn = init_db()
    for task_id in (a_id, b_id):
        task = _task(task_id)
        base = task["stacked_on"] or None
        head = _git(task["worktree_path"], "rev-parse", "HEAD")
        assert delivery.composition_tasks(conn, task, remote="origin", base=base, head=head) == []
    assert _deliver(a_id).task_id == a_id
    assert _deliver(b_id).task_id == b_id


# --------------------------------------------------------------------------- #
# G2: a real composition is still refused
# --------------------------------------------------------------------------- #


def test_a_parent_carrying_its_child_s_own_commit_is_refused(server, source_repo) -> None:
    """The child's work landed on the parent's branch: delivering the parent would ship it."""
    _srv, client = server
    added, parent_id, child_id, _parent_head, child_own = _stack_dispatched_onto_an_empty_parent(
        client, source_repo
    )
    parent = _task(parent_id)
    _git(parent["worktree_path"], "merge", "-q", "--ff-only", child_own)

    with pytest.raises(delivery.DeliveryError) as refused:
        _deliver(parent_id)
    message = str(refused.value)
    assert "refusing composition" in message
    assert f"task {child_id}" in message


def test_a_task_that_cherry_picked_another_open_task_s_commit_is_refused(
    server, source_repo
) -> None:
    """Two unrelated tasks; one copies the other's commit under a new SHA."""
    _srv, client = server
    added = repos.add_repo(source_repo)
    owner_id = _in_progress_task(added.name, "owner of the change")
    copier_id = _in_progress_task(added.name, "copier of the change")
    owner = _task(owner_id)
    borrowed = _commit(owner["worktree_path"], "shared.txt", "the owner's change")
    _push(owner)
    copier = _task(copier_id)
    _commit(copier["worktree_path"], "own.txt", "the copier's own change")
    _git(copier["worktree_path"], "fetch", "-q", "origin", owner["branch"])
    _git(
        copier["worktree_path"],
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "cherry-pick",
        borrowed,
    )
    assert _git(copier["worktree_path"], "rev-parse", "HEAD") != borrowed, "a new SHA"

    with pytest.raises(delivery.DeliveryError) as refused:
        _deliver(copier_id)
    assert f"task {owner_id}" in str(refused.value)


# --------------------------------------------------------------------------- #
# A merged parent: the child's commits are its own, counted against main
# --------------------------------------------------------------------------- #


def test_after_the_parent_merges_its_commits_are_counted_for_neither_child(
    server, source_repo
) -> None:
    """Two children of one merged parent: delivering either to main does not trip on the other."""
    _srv, client = server
    added = repos.add_repo(source_repo)
    parent_id = _in_progress_task(added.name, "parent layer")
    first_id = _stack_on(client, added.name, parent_id, "first child")
    second_id = _stack_on(client, added.name, parent_id, "second child")
    parent = _task(parent_id)
    _commit(parent["worktree_path"], "parent.txt", "parent work")
    _push(parent)
    for child_id in (first_id, second_id):
        child = _task(child_id)
        _rebase_onto(child, parent)
        _commit(child["worktree_path"], f"child-{child_id}.txt", "child work")
        _push(child)

    # Squash-merged upstream: main gains a new commit, the parent's own SHA stays off main.
    _commit(source_repo, "parent.txt", "parent work (squashed)")
    conn = init_db()
    store.update_task_fields(
        conn, parent_id, merged_sha="b" * 40, merged_at=datetime.now(UTC).isoformat()
    )

    first = _task(first_id)
    head = _git(first["worktree_path"], "rev-parse", "HEAD")
    assert delivery.composition_tasks(conn, first, remote="origin", base="main", head=head) == []
    assert _deliver(first_id, base="main").task_id == first_id


# --------------------------------------------------------------------------- #
# G3: syncing a stacked child records the parent head it is built on
# --------------------------------------------------------------------------- #


def test_syncing_a_child_records_its_parent_s_head_as_its_base(server, source_repo) -> None:
    _srv, client = server
    added, parent_id, child_id, parent_head, _child_own = _stack_dispatched_onto_an_empty_parent(
        client, source_repo
    )

    stacks.sync_worktree_with_remote(child_id)
    assert _task(child_id)["base_sha"] == parent_head
    conn = init_db()
    events = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = 'base_refreshed'", (child_id,)
    ).fetchall()
    assert len(events) == 1 and parent_head in events[0]["payload"]

    # Nothing moved: a second sync records nothing.
    stacks.sync_worktree_with_remote(child_id)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM events WHERE task_id = ? AND kind = 'base_refreshed'",
            (child_id,),
        ).fetchone()[0]
        == 1
    )


def test_a_child_that_has_not_rebased_keeps_its_recorded_base(server, source_repo) -> None:
    """The parent pushed, the child never took it: its base is still what it built on."""
    _srv, client = server
    added = repos.add_repo(source_repo)
    parent_id = _in_progress_task(added.name, "parent layer")
    child_id = _stack_on(client, added.name, parent_id, "child layer")
    recorded = _task(child_id)["base_sha"]
    parent = _task(parent_id)
    _commit(parent["worktree_path"], "parent.txt", "parent work")
    _push(parent)

    stacks.sync_worktree_with_remote(child_id)
    assert _task(child_id)["base_sha"] == recorded


def test_a_child_of_a_merged_parent_keeps_its_recorded_base(server, source_repo) -> None:
    _srv, client = server
    added, parent_id, child_id, _parent_head, _child_own = _stack_dispatched_onto_an_empty_parent(
        client, source_repo
    )
    recorded = _task(child_id)["base_sha"]
    store.update_task_fields(
        init_db(), parent_id, merged_sha="c" * 40, merged_at=datetime.now(UTC).isoformat()
    )

    stacks.sync_worktree_with_remote(child_id)
    assert _task(child_id)["base_sha"] == recorded
