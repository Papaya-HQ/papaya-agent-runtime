"""`ppy worktree list` / `ppy worktree prune`: see the slots, and get them back.

The dispatch disk floor refused work on 2026-09-02 while 20 GB sat in the
worktrees of tasks that were already delivered, with no command to reclaim them.
Reclamation has to be conservative: a full disk must never cost work that only
exists in a leased checkout.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import make_git_repo
from papaya_agent_runtime import preflight, repos
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.paths import worktree_pools_dir
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.worktree.lease import LeaseError, LeaseManager
from papaya_agent_runtime.worktree.reclaim import (
    human_bytes,
    list_worktrees,
    managed_base_clones,
    orphan_slots,
    pool_roots,
    prune,
    reclaimable_bytes,
    remove_orphan_slot,
)


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(ppy_home, source_repo):
    return repos.add_repo(source_repo)


def _lease_a_task(repo, *, status: str, title="work"):
    """Create a task in ``status`` holding a real leased worktree."""
    conn = init_db()
    run_id = store.create_run(conn, title)
    repo_row = store.get_repo(conn, repo.name)
    task_id = store.add_task(conn, run_id=run_id, title=title, repo_id=repo_row["id"])
    lease = LeaseManager("git").acquire(
        repo_path=repo_row["local_path"], repo_id=repo_row["id"], task_id=task_id
    )
    store.update_task_fields(
        conn,
        task_id,
        branch=lease.branch,
        worktree_path=lease.worktree_path,
        lease_id=lease.id,
        base_sha=lease.base_sha,
    )
    store.set_task_status(conn, task_id, status)
    return task_id, lease


def _commit_in(worktree, name="local-only.txt"):
    Path(worktree, name).write_text("work that exists nowhere else\n")
    _git(worktree, "add", "-A")
    _git(worktree, "-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-qm", "local work")


def _events(task_id, kind):
    conn = init_db()
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


# --------------------------------------------------------------------------- #
# ppy worktree list
# --------------------------------------------------------------------------- #


def test_list_reports_slot_task_status_branch_state_and_size(repo):
    delivered_id, delivered = _lease_a_task(repo, status="delivered")
    working_id, working = _lease_a_task(repo, status="in_progress")
    Path(working.worktree_path, "scratch.txt").write_text("uncommitted\n")

    entries = {e.task_id: e for e in list_worktrees()}
    assert set(entries) == {delivered_id, working_id}

    done = entries[delivered_id]
    assert done.slot == Path(delivered.worktree_path).name
    assert done.task_status == "delivered"
    assert done.branch == delivered.branch
    assert done.dirty is False
    assert done.size_bytes > 0
    assert done.reclaimable is True

    live = entries[working_id]
    assert live.dirty is True
    assert live.reclaimable is False
    assert "in_progress" in live.reason


def test_list_filters_by_repo(ppy_home, tmp_path):
    first = repos.add_repo(make_git_repo(tmp_path / "one"))
    second = repos.add_repo(make_git_repo(tmp_path / "two"))
    _lease_a_task(first, status="delivered")
    _lease_a_task(second, status="delivered")

    assert [e.repo for e in list_worktrees("one")] == ["one"]
    assert len(list_worktrees()) == 2


def test_list_prints_a_table(repo, capsys):
    _lease_a_task(repo, status="delivered")
    assert main(["worktree", "list"]) == 0
    out = capsys.readouterr().out
    assert "SLOT" in out and "prunable" in out


# --------------------------------------------------------------------------- #
# ppy worktree prune
# --------------------------------------------------------------------------- #


def test_prune_removes_a_finished_clean_worktree_and_releases_its_lease(repo):
    task_id, lease = _lease_a_task(repo, status="delivered")

    res = prune()

    assert [r["task_id"] for r in res["removed"]] == [task_id]
    assert res["reclaimed_bytes"] > 0
    assert not Path(lease.worktree_path).exists()
    conn = init_db()
    assert conn.execute("SELECT status FROM leases WHERE id = ?", (lease.id,)).fetchone()[0] == (
        "released"
    )
    assert _events(task_id, "worktree_pruned")


def test_prune_keeps_the_branch_it_removed_the_worktree_for(repo):
    task_id, lease = _lease_a_task(repo, status="delivered")
    repo_path = store.get_repo(init_db(), repo.name)["local_path"]
    prune()
    assert lease.branch in _git(repo_path, "branch", "--list", lease.branch)


def test_prune_skips_a_dirty_worktree_and_says_why(repo):
    task_id, lease = _lease_a_task(repo, status="delivered")
    Path(lease.worktree_path, "unsaved.txt").write_text("not committed anywhere\n")

    res = prune()

    assert res["removed"] == []
    assert res["skipped"][0]["task_id"] == task_id
    assert res["skipped"][0]["dirty"] is True
    assert "uncommitted" in res["skipped"][0]["reason"]
    assert Path(lease.worktree_path).exists()


def test_prune_skips_a_worktree_holding_commits_no_remote_has(repo):
    task_id, lease = _lease_a_task(repo, status="delivered")
    _commit_in(lease.worktree_path)

    res = prune()

    assert res["removed"] == []
    skipped = res["skipped"][0]
    assert skipped["unpushed_commits"] == 1
    assert "exist only here" in skipped["reason"]
    assert Path(lease.worktree_path).exists()


def test_prune_reclaims_a_worktree_once_its_commits_are_pushed(repo):
    task_id, lease = _lease_a_task(repo, status="delivered")
    _commit_in(lease.worktree_path)
    _git(lease.worktree_path, "push", "--quiet", "origin", f"HEAD:{lease.branch}")

    res = prune()

    assert [r["task_id"] for r in res["removed"]] == [task_id]
    assert not Path(lease.worktree_path).exists()


def test_prune_leaves_unfinished_tasks_alone(repo):
    for status in ("in_progress", "worker_done", "blocked", "failed", "needs_recovery"):
        _lease_a_task(repo, status=status, title=status)

    res = prune()

    assert res["removed"] == []
    assert len(res["skipped"]) == 5
    assert all("not finished with" in s["reason"] for s in res["skipped"])


def test_prune_takes_every_terminal_status(repo):
    ids = [_lease_a_task(repo, status=s, title=s)[0] for s in ("delivered", "closed", "cancelled")]
    res = prune()
    assert sorted(r["task_id"] for r in res["removed"]) == sorted(ids)


def test_dry_run_changes_nothing_but_reports_the_bytes(repo):
    task_id, lease = _lease_a_task(repo, status="delivered")

    res = prune(dry_run=True)

    assert res["dry_run"] is True
    assert [r["task_id"] for r in res["removed"]] == [task_id]
    assert res["reclaimed_bytes"] > 0
    assert Path(lease.worktree_path).exists(), "a dry run must not touch the disk"
    conn = init_db()
    assert conn.execute("SELECT status FROM leases WHERE id = ?", (lease.id,)).fetchone()[0] == (
        "active"
    )
    assert not _events(task_id, "worktree_pruned")


def test_prune_filters_by_repo(ppy_home, tmp_path):
    first = repos.add_repo(make_git_repo(tmp_path / "one"))
    second = repos.add_repo(make_git_repo(tmp_path / "two"))
    _kept_id, kept = _lease_a_task(second, status="delivered")
    _lease_a_task(first, status="delivered")

    res = prune("one")

    assert [r["repo"] for r in res["removed"]] == ["one"]
    assert Path(kept.worktree_path).exists()


def test_prune_clears_a_lease_whose_worktree_is_already_gone(repo):
    task_id, lease = _lease_a_task(repo, status="delivered")
    repo_path = store.get_repo(init_db(), repo.name)["local_path"]
    subprocess.run(
        ["git", "-C", repo_path, "worktree", "remove", "--force", lease.worktree_path],
        check=True,
        capture_output=True,
    )

    res = prune()

    assert [r["task_id"] for r in res["removed"]] == [task_id]
    conn = init_db()
    assert conn.execute("SELECT status FROM leases WHERE id = ?", (lease.id,)).fetchone()[0] == (
        "released"
    )


def test_cli_prune_dry_run_reports_and_leaves_the_slot(repo, capsys):
    _task_id, lease = _lease_a_task(repo, status="delivered")
    assert main(["worktree", "prune", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would remove" in out
    assert Path(lease.worktree_path).exists()


# --------------------------------------------------------------------------- #
# Orphans: slot directories on disk that no active lease owns
#
# The first real prune reclaimed nine backend slots while thirteen frontend slot
# directories — clean, delivered, about 10 GB — were invisible, because their
# leases had been released or were created by an earlier ppy instance.
# --------------------------------------------------------------------------- #


def _base_clone(repo) -> str:
    return store.get_repo(init_db(), repo.name)["local_path"]


def _orphan_slot(repo, name: str, *, root: Path | None = None, nested: bool = False):
    """A pool slot on disk holding a real checkout that no lease ever recorded."""
    pool = root or worktree_pools_dir()
    pool.mkdir(parents=True, exist_ok=True)
    slot = pool / name
    checkout = slot / "repo" if nested else slot
    _git(_base_clone(repo), "worktree", "add", "-q", "-b", f"orphan/{name}", str(checkout), "HEAD")
    return slot, checkout


def _orphans(repo_filter=None):
    return {e.slot: e for e in orphan_slots(repo_filter)}


def test_orphaned_slots_are_found_with_branch_state_and_size(repo):
    slot, _checkout = _orphan_slot(repo, "clean-slot")

    entry = _orphans()["clean-slot"]

    assert entry.orphaned is True
    assert entry.task_id is None
    assert entry.repo == repo.name
    assert entry.branch == "orphan/clean-slot"
    assert entry.dirty is False
    assert entry.unpushed_commits == 0
    assert entry.size_bytes > 0
    assert entry.reclaimable is True
    assert "no active lease" in entry.reason
    assert slot.exists()


def test_a_dirty_orphan_is_reported_but_never_reclaimable(repo):
    _slot, checkout = _orphan_slot(repo, "dirty-slot")
    Path(checkout, "unsaved.txt").write_text("not committed anywhere\n")

    entry = _orphans()["dirty-slot"]

    assert entry.dirty is True
    assert entry.reclaimable is False
    assert "uncommitted" in entry.reason


def test_an_orphan_holding_an_unpushed_commit_is_never_reclaimable(repo):
    _slot, checkout = _orphan_slot(repo, "unpushed-slot")
    _commit_in(checkout)

    entry = _orphans()["unpushed-slot"]

    assert entry.unpushed_commits == 1
    assert entry.reclaimable is False
    assert "exist only here" in entry.reason


def test_a_leased_slot_is_never_also_reported_as_an_orphan(repo):
    task_id, lease = _lease_a_task(repo, status="delivered")

    entries = list_worktrees()

    assert [e.task_id for e in entries if not e.orphaned] == [task_id]
    assert [e for e in entries if e.orphaned] == []
    assert Path(lease.worktree_path).exists()


def test_list_shows_leases_and_orphans_together(repo):
    _lease_a_task(repo, status="in_progress")
    _orphan_slot(repo, "clean-slot")

    entries = list_worktrees()

    assert len(entries) == 2
    assert sorted(e.orphaned for e in entries) == [False, True]


def test_treehouse_pool_roots_are_walked_for_every_registered_repo(repo, tmp_path, monkeypatch):
    """Treehouse names its pool root ``<repo>-<hash>`` and nests the checkout inside."""
    home = tmp_path / "th"
    monkeypatch.setenv("PPY_TREEHOUSE_HOME", str(home))
    root = home / f"{repo.name}-bbcd55"
    _orphan_slot(repo, "1", root=root, nested=True)

    assert root in pool_roots()
    entry = _orphans()["1"]
    assert entry.orphaned is True
    assert entry.checkout.endswith("/1/repo")
    assert entry.path == str(root / "1")
    assert entry.reclaimable is True


def test_orphans_filter_by_repo(ppy_home, tmp_path):
    first = repos.add_repo(make_git_repo(tmp_path / "one"))
    second = repos.add_repo(make_git_repo(tmp_path / "two"))
    _orphan_slot(first, "from-one")
    _orphan_slot(second, "from-two")

    assert sorted(_orphans("one")) == ["from-one"]
    assert sorted(_orphans()) == ["from-one", "from-two"]


def test_prune_reclaims_a_clean_orphan_and_keeps_the_rest(repo):
    clean, _ = _orphan_slot(repo, "clean-slot")
    dirty, dirty_checkout = _orphan_slot(repo, "dirty-slot")
    Path(dirty_checkout, "unsaved.txt").write_text("not committed anywhere\n")
    held, held_checkout = _orphan_slot(repo, "unpushed-slot")
    _commit_in(held_checkout)

    res = prune()

    assert [r["path"] for r in res["removed"]] == [str(clean)]
    assert res["removed"][0]["orphaned"] is True
    assert res["removed"][0]["task_status"] == "orphaned"
    assert not clean.exists()
    assert dirty.exists() and held.exists()
    assert {Path(r["path"]).name for r in res["skipped"]} == {"dirty-slot", "unpushed-slot"}


def test_prune_removes_the_whole_treehouse_slot_not_just_the_checkout(repo, tmp_path, monkeypatch):
    home = tmp_path / "th"
    monkeypatch.setenv("PPY_TREEHOUSE_HOME", str(home))
    slot, checkout = _orphan_slot(repo, "1", root=home / f"{repo.name}-bbcd55", nested=True)

    prune()

    assert not checkout.exists()
    assert not slot.exists(), "the numbered slot directory is the space; it must go too"


def test_dry_run_leaves_an_orphan_on_disk(repo):
    slot, _ = _orphan_slot(repo, "clean-slot")

    res = prune(dry_run=True)

    assert [r["path"] for r in res["removed"]] == [str(slot)]
    assert slot.exists()


def test_prune_records_an_event_for_a_reclaimed_orphan(repo):
    _orphan_slot(repo, "clean-slot")
    prune()
    conn = init_db()
    payloads = [
        json.loads(r["payload"])
        for r in conn.execute(
            "SELECT payload FROM events WHERE kind = 'worktree_pruned'"
        ).fetchall()
    ]
    assert [p["orphaned"] for p in payloads] == [True]


def test_a_pool_directory_that_is_not_a_checkout_is_left_alone(repo):
    pool = worktree_pools_dir()
    pool.mkdir(parents=True, exist_ok=True)
    (pool / "not-a-worktree").mkdir()
    (pool / "not-a-worktree" / "notes.txt").write_text("someone's scratch dir\n")

    assert _orphans() == {}
    res = prune()
    assert res["removed"] == [] and res["skipped"] == []
    assert (pool / "not-a-worktree").exists()


# --------------------------------------------------------------------------- #
# Sitting in a pool root is not proof of ownership
#
# One treehouse home holds this instance's pools next to the user's own for the
# same repositories — papaya-backend-monorepo-58a49b is ours, -e5cc8b is theirs —
# and both match the same name glob. A clean, fully pushed personal worktree is
# the most reclaimable-looking thing on the disk and the least ours to take.
# --------------------------------------------------------------------------- #


def _their_pool_slot(source_repo, tmp_path, home, repo_name):
    """A pool root matching our name glob, holding a slot from someone else's clone."""
    theirs = tmp_path / "their-clone"
    subprocess.run(["git", "clone", "-q", source_repo, str(theirs)], check=True)
    root = home / f"{repo_name}-e5cc8b"  # same glob as ours, different hash
    root.mkdir(parents=True)
    slot = root / "1"
    _git(theirs, "worktree", "add", "-q", "-b", "their/work", str(slot / "repo"), "HEAD")
    return slot


def test_a_slot_from_a_clone_we_do_not_manage_is_never_reclaimable(
    repo, source_repo, tmp_path, monkeypatch
):
    home = tmp_path / "th"
    monkeypatch.setenv("PPY_TREEHOUSE_HOME", str(home))
    _orphan_slot(repo, "1", root=home / f"{repo.name}-58a49b", nested=True)
    theirs = _their_pool_slot(source_repo, tmp_path, home, repo.name)

    found = {e.path: e for e in orphan_slots()}
    mine = found[str(home / f"{repo.name}-58a49b" / "1")]
    yours = found[str(theirs)]

    assert mine.managed is True and mine.reclaimable is True
    # Clean, and every commit is on a remote — reclaimable by every rule but ownership.
    assert yours.managed is False
    assert yours.reclaimable is False
    assert "not managed by this instance" in yours.reason


def test_prune_removes_our_slot_and_does_not_touch_theirs(repo, source_repo, tmp_path, monkeypatch):
    home = tmp_path / "th"
    monkeypatch.setenv("PPY_TREEHOUSE_HOME", str(home))
    ours, _ = _orphan_slot(repo, "1", root=home / f"{repo.name}-58a49b", nested=True)
    theirs = _their_pool_slot(source_repo, tmp_path, home, repo.name)

    res = prune()

    assert [r["path"] for r in res["removed"]] == [str(ours)]
    assert not ours.exists()
    assert (theirs / "repo" / "README.md").exists(), "that is the user's own worktree"
    assert any("not managed by this instance" in r["reason"] for r in res["skipped"])


def test_removing_an_unmanaged_slot_is_refused_even_if_something_asks_directly(
    repo, source_repo, tmp_path, monkeypatch
):
    """The delete itself re-checks ownership; being reached at all is a bug, not a licence."""
    home = tmp_path / "th"
    monkeypatch.setenv("PPY_TREEHOUSE_HOME", str(home))
    theirs = _their_pool_slot(source_repo, tmp_path, home, repo.name)
    entry = next(e for e in orphan_slots() if e.path == str(theirs))

    with pytest.raises(LeaseError):
        remove_orphan_slot(entry)
    assert (theirs / "repo").exists()


def test_a_slot_whose_base_clone_is_gone_is_unmanaged(repo, tmp_path, monkeypatch):
    """No answer to "whose is it" is never treated as "ours"."""
    home = tmp_path / "th"
    monkeypatch.setenv("PPY_TREEHOUSE_HOME", str(home))
    slot, checkout = _orphan_slot(repo, "1", root=home / f"{repo.name}-58a49b", nested=True)
    shutil.rmtree(_base_clone(repo))

    entry = next(e for e in orphan_slots() if e.path == str(slot))

    assert entry.managed is False
    assert entry.reclaimable is False
    assert checkout.exists()


def test_a_repo_registered_outside_the_ppy_home_owns_nothing(repo, tmp_path):
    """Ownership means a base clone under `.ppy/repos/`, not any path in the table."""
    conn = init_db()
    conn.execute(
        "UPDATE repos SET local_path = ? WHERE name = ?", (str(tmp_path / "elsewhere"), repo.name)
    )
    conn.commit()
    assert managed_base_clones(conn) == {}


def test_cli_list_labels_unmanaged_slots_separately(
    repo, source_repo, tmp_path, monkeypatch, capsys
):
    home = tmp_path / "th"
    monkeypatch.setenv("PPY_TREEHOUSE_HOME", str(home))
    _their_pool_slot(source_repo, tmp_path, home, repo.name)

    assert main(["worktree", "list"]) == 0
    out = capsys.readouterr().out

    assert "unmanaged" in out
    assert "never pruned" in out
    assert "0 B prunable" in out


def test_cli_list_labels_orphans(repo, capsys):
    _orphan_slot(repo, "clean-slot")
    assert main(["worktree", "list"]) == 0
    out = capsys.readouterr().out
    assert "orphaned" in out


# --------------------------------------------------------------------------- #
# The dispatch disk floor names the command and the space it would give back
# --------------------------------------------------------------------------- #


def test_disk_refusal_names_worktree_prune_and_what_it_would_reclaim(repo):
    _lease_a_task(repo, status="delivered")
    freeable = reclaimable_bytes()
    assert freeable > 0

    with pytest.raises(preflight.PreflightError) as exc:
        preflight.check_disk(floor_gb=10**9)

    message = str(exc.value)
    assert "ppy worktree prune" in message
    assert human_bytes(freeable) in message
    assert "ppy worktree list" in message


def test_disk_refusal_says_so_when_there_is_nothing_to_reclaim(repo):
    _lease_a_task(repo, status="in_progress")
    with pytest.raises(preflight.PreflightError) as exc:
        preflight.check_disk(floor_gb=10**9)
    assert "nothing to reclaim right now" in str(exc.value)


def test_prune_never_takes_a_slot_a_resumable_task_still_names(repo):
    """Task 158 (issue #58): lease released on failure, slot still on disk, task
    still resumable — prune called it an orphan and removed it."""
    task_id, lease = _lease_a_task(repo, status="failed")
    # The lease is handed back but the checkout stays on disk (a pooled slot).
    store.release_lease(init_db(), lease.id)

    res = prune()

    assert res["removed"] == []
    [skipped] = res["skipped"]
    assert skipped["task_id"] == task_id
    assert skipped["orphaned"] is True
    assert f"task {task_id} (failed)" in skipped["reason"]
    assert "can be resumed into it" in skipped["reason"]
    assert Path(lease.worktree_path).exists()

    # Once the task is over, the same slot is an ordinary clean orphan again.
    store.set_task_status(init_db(), task_id, "closed")
    res = prune()
    assert [r["path"] for r in res["removed"]] == [lease.worktree_path]
