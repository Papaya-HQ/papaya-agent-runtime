"""Fixing a delivered pull request keeps its worktree, its base, and its PR (task 288).

PAP-222 (ticket task 20, worker task 21, frontend PR 710) on 2026-09-17: the reconcile
lane's first real run met a review that diffed from a stale base after a rebase, a
worktree hygiene had removed while PR 710 was open and that came back at the base
commit, and a second `ppy deliver` that could not tell an open pull request from a
failure. The delivery half is in `test_delivery_pr_url.py`; this file holds the rest.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import wait_until
from papaya_agent_runtime import deficiencies, repos, review, team
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.core import Supervisor, SupervisorError
from papaya_agent_runtime.worktree import reclaim
from papaya_agent_runtime.worktree.lease import LeaseManager

PR_URL = "https://github.com/acme/frontend/pull/710"


def _git(path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=T", "-c", "user.email=t@e.com", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(path, name: str) -> str:
    Path(path, name).write_text(f"{name}\n")
    _git(path, "add", name)
    _git(path, "commit", "-qm", f"add {name}")
    return _git(path, "rev-parse", "HEAD")


def _leased_task(added, *, status: str = "in_progress") -> tuple[int, object]:
    conn = init_db()
    repo_row = store.get_repo(conn, added.name)
    run_id = store.create_run(conn, "PAP-222")
    task_id = store.add_task(conn, run_id=run_id, title="the worker", repo_id=repo_row["id"])
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


def _event(task_id: int, kind: str, payload: dict) -> None:
    conn = init_db()
    task = store.get_task(conn, task_id)
    store.append_event(conn, kind=kind, payload=payload, run_id=task["run_id"], task_id=task_id)


# --------------------------------------------------------------------------- #
# The review diffs the worker's commits only
# --------------------------------------------------------------------------- #


def test_a_rebased_branch_reviews_only_its_own_commits(ppy_home, source_repo) -> None:
    added = repos.add_repo(source_repo)
    task_id, lease = _leased_task(added)
    _commit(lease.worktree_path, "worker-change.txt")

    # Main moves on at the forge, and the branch is rebased onto it.
    _commit(source_repo, "someone-else-1.txt")
    _commit(source_repo, "someone-else-2.txt")
    _git(lease.worktree_path, "fetch", "-q", "origin")
    _git(lease.worktree_path, "rebase", "-q", "origin/main")

    bundle = review.build_bundle(task_id)
    changed = _git(lease.worktree_path, "diff", "--name-only", f"{bundle.base_sha}..HEAD")
    assert changed.splitlines() == ["worker-change.txt"]
    assert bundle.files_changed == 1
    assert bundle.base_sha == _git(source_repo, "rev-parse", "main")
    assert bundle.base_sha != lease.base_sha
    assert bundle.base_from == "merge-base with origin/main (fetched now)"
    # The dispatch-time base is what used to be diffed, and it shows main's files.
    stale = _git(lease.worktree_path, "diff", "--name-only", f"{lease.base_sha}..HEAD")
    assert len(stale.splitlines()) == 3

    line = review.review_base_line(task_id)
    assert line.startswith(f"{bundle.base_sha[:8]}..{bundle.head_sha[:8]}: merge-base with origin")


def test_a_review_that_cannot_reach_the_forge_says_which_base_it_used(
    ppy_home, source_repo
) -> None:
    added = repos.add_repo(source_repo)
    task_id, lease = _leased_task(added)
    _commit(lease.worktree_path, "worker-change.txt")
    Path(source_repo).rename(Path(source_repo).parent / "unreachable")

    bundle = review.build_bundle(task_id)
    assert bundle.files_changed == 1
    assert (
        bundle.base_from == "merge-base with origin/main (could not fetch; the last fetched copy)"
    )


# --------------------------------------------------------------------------- #
# A delivered task's worktree stays until its pull request merges or closes
# --------------------------------------------------------------------------- #


def _forge(monkeypatch, entry: dict) -> list[str]:
    asked: list[str] = []

    def lookup(branch, cwd):
        asked.append(branch)
        return {"known": True, "ci": "fail", "failing": [], **entry}

    monkeypatch.setattr(reclaim, "lookup_pr", lookup)
    monkeypatch.setattr(reclaim, "_asked", {})
    return asked


def test_a_delivered_task_with_an_open_pr_is_kept_and_a_merged_one_is_reclaimable(
    ppy_home, source_repo, monkeypatch
) -> None:
    added = repos.add_repo(source_repo)
    task_id, lease = _leased_task(added, status="delivered")
    _event(task_id, "delivered", {"task_id": task_id, "pr_url": PR_URL, "pr_exists": True})

    asked = _forge(monkeypatch, {"pr": 710, "state": "OPEN", "merged": False, "url": PR_URL})
    [entry] = reclaim.list_worktrees(task_id=task_id)
    assert entry.reclaimable is False
    assert entry.reason == "kept: PR #710 open"
    assert entry.open_pr == 710
    assert asked == [lease.branch]  # no record yet, so the forge was asked

    result = reclaim.prune(task_id=task_id, managed_only=True)
    assert result["removed"] == []
    assert [r["reason"] for r in result["skipped"]] == ["kept: PR #710 open"]
    assert Path(lease.worktree_path).is_dir()

    # The forge read became the PR watch record.
    rows = init_db().execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ?",
        (task_id, team.PR_OBSERVED_EVENT),
    )
    assert [json.loads(r["payload"])["state"] for r in rows] == ["OPEN"]

    # It merges: the forge says so once the record is older than a round.
    asked = _forge(
        monkeypatch,
        {"pr": 710, "state": "MERGED", "merged": True, "url": PR_URL, "merge_commit": "a" * 40},
    )
    later = datetime(2099, 1, 1, tzinfo=UTC)
    assert reclaim.open_pull_request(task_id, now=later) is None
    assert asked == [lease.branch]

    [entry] = reclaim.list_worktrees(task_id=task_id)
    assert entry.reclaimable is True
    result = reclaim.prune(task_id=task_id, managed_only=True)
    assert [r["task_id"] for r in result["removed"]] == [task_id]
    assert not Path(lease.worktree_path).exists()


def test_a_fresh_pr_record_is_trusted_without_asking_the_forge(
    ppy_home, source_repo, monkeypatch
) -> None:
    added = repos.add_repo(source_repo)
    task_id, _lease = _leased_task(added, status="delivered")
    _event(task_id, "delivered", {"task_id": task_id, "pr_url": PR_URL, "pr_exists": True})
    _event(task_id, team.PR_OBSERVED_EVENT, {"task_id": task_id, "pr": 710, "state": "OPEN"})
    asked = _forge(monkeypatch, {"pr": 710, "state": "MERGED", "merged": True})

    held = reclaim.open_pull_request(task_id, refresh_after=3600)
    assert held is not None and held.reason == "kept: PR #710 open"
    assert asked == []


def test_a_pr_whose_state_cannot_be_read_keeps_the_slot(ppy_home, source_repo, monkeypatch):
    added = repos.add_repo(source_repo)
    task_id, _lease = _leased_task(added, status="delivered")
    _event(task_id, "delivered", {"task_id": task_id, "pr_url": PR_URL, "pr_exists": True})
    monkeypatch.setattr(reclaim, "lookup_pr", lambda branch, cwd: {"known": False})

    [entry] = reclaim.list_worktrees(task_id=task_id)
    assert entry.reclaimable is False
    assert entry.reason == f"kept: {PR_URL} exists and its state could not be read from the forge"


def test_a_delivered_task_with_no_pr_on_record_keeps_the_old_rules(
    ppy_home, source_repo, monkeypatch
) -> None:
    added = repos.add_repo(source_repo)
    task_id, _lease = _leased_task(added, status="delivered")
    asked = _forge(monkeypatch, {"pr": 710, "state": "OPEN"})

    [entry] = reclaim.list_worktrees(task_id=task_id)
    assert entry.reclaimable is True
    assert asked == []  # nothing on record says a pull request exists; the forge is not asked


# --------------------------------------------------------------------------- #
# A rebuilt worktree for the reconcile lane starts at the pull request head
# --------------------------------------------------------------------------- #


def _status(task_id: int) -> str:
    return store.get_task(init_db(), task_id)["status"]


def _delivered_then_slot_lost(supervisor: Supervisor, source_repo) -> tuple[int, dict, str]:
    """A delivered task whose slot hygiene took, with its local branch back at the base."""
    added = repos.add_repo(source_repo)
    task = supervisor.dispatch_task(repo=added.name, title="fix the frontend", provider="fake")
    task_id = task["task_id"]
    wait_until(lambda: _status(task_id) == "worker_done", 15)
    row = store.get_task(init_db(), task_id)
    head = _git(row["worktree_path"], "rev-parse", "HEAD")
    assert head != row["base_sha"]
    # The pull request the forge knows, at the worker's head.
    _git(row["worktree_path"], "push", "-q", "origin", "HEAD:refs/pull/710/head")
    store.set_task_status(init_db(), task_id, "delivered")
    _event(task_id, "delivered", {"task_id": task_id, "pr_url": PR_URL, "pr_exists": True})
    supervisor.release_task_lease(task_id, remove_branch=False)
    assert not Path(row["worktree_path"]).exists()
    # What PAP-222 found: the kept branch no longer pointed at the work.
    repo_row = store.get_repo(init_db(), added.name)
    _git(repo_row["local_path"], "branch", "-f", row["branch"], row["base_sha"])
    return task_id, row, head


def test_a_delivered_task_rebuilt_for_its_pr_starts_at_the_pr_head_not_the_base(
    ppy_home, source_repo
) -> None:
    supervisor = Supervisor()
    task_id, row, head = _delivered_then_slot_lost(supervisor, source_repo)
    # Even the forge's branch is stale: the pull request head is what counts.
    _git(source_repo, "branch", "-f", row["branch"], row["base_sha"])

    resumed = supervisor.resume_task(task_id, "CI is red on PR 710")
    rebuilt = resumed["worktree_rebuilt"]
    assert rebuilt is not None
    assert rebuilt["started_from"] == f"PR #710 head at {head[:8]} (from origin)"
    assert resumed["reconciler"] is True
    assert Path(rebuilt["worktree_path"], f"ppy-fake-{task_id}.txt").exists()
    wait_until(lambda: _status(task_id) != "in_progress", 15)


def test_a_delivered_task_whose_pr_head_cannot_be_fetched_is_never_rebuilt_at_the_base(
    ppy_home, source_repo
) -> None:
    supervisor = Supervisor()
    task_id, row, _head = _delivered_then_slot_lost(supervisor, source_repo)
    _git(source_repo, "update-ref", "-d", "refs/pull/710/head")
    _git(source_repo, "branch", "-D", row["branch"])

    with pytest.raises(SupervisorError, match="refusing to rebuild it at the base commit"):
        supervisor.resume_task(task_id, "CI is red on PR 710")
    assert _status(task_id) == "delivered"


# --------------------------------------------------------------------------- #
# The three ledger rows close from the fix
# --------------------------------------------------------------------------- #

#: The three `RUNTIME:` details as the live ledger recorded them on 2026-09-17.
PAP_222_REPORTS = {
    "74fccc066045a150": (
        "`ppy review show 21` compared against an older starting commit (3f3c0181) and listed "
        "51 changed files instead of the real 3. Also, `ppy gate run --task 21` ran the repo's "
        "registered gate (`make test`) instead of the brief's checks (lint, typecheck and the "
        "unit suite), so I ran those by hand."
    ),
    "ecfb07ec73b22d9a": (
        "task 21's worktree slot had been reset to the base commit after delivery, so `ppy gate "
        "run --task 21` and `ppy review approve 21` ran against the base instead of the pushed PR "
        "head. `ppy deliver 21` then refused, and nothing in `ppy` can point the review back at "
        "that head."
    ),
    "73e968fea9ac0d8e": (
        '`ppy deliver 21` reported "PR creation failed" with no reason, even though pull request '
        "#710 already existed for that branch."
    ),
}


def test_the_three_pap_222_reports_are_closed_from_the_fix_and_a_recurrence_is_not(
    ppy_home,
) -> None:
    from test_deficiencies import FakeGh, _reporter

    before = lambda: datetime(2026, 9, 17, 1, 49, 12, tzinfo=UTC)  # noqa: E731
    for fingerprint, detail in PAP_222_REPORTS.items():
        row = deficiencies.record(deficiencies.TURN_REPORT, detail, clock=before)
        assert row is not None and row.fingerprint == fingerprint
    # The same words again after the fix is a recurrence, and stays open.
    after = lambda: datetime(2026, 9, 18, 9, 0, tzinfo=UTC)  # noqa: E731
    later = deficiencies.record(
        deficiencies.TURN_REPORT,
        PAP_222_REPORTS["73e968fea9ac0d8e"].replace("21", "34"),
        clock=after,
    )
    assert later is not None and later.fingerprint == "73e968fea9ac0d8e"

    gh = FakeGh()
    reporter = _reporter(gh)
    reporter.reclassify()

    status = {d.fingerprint: d.status for d in deficiencies.ledger(include_all=True)}
    assert status["74fccc066045a150"] == deficiencies.RECLASSIFIED
    assert status["ecfb07ec73b22d9a"] == deficiencies.RECLASSIFIED
    assert status["73e968fea9ac0d8e"] != deficiencies.RECLASSIFIED  # it recurred after the fix


def test_a_reported_pap_222_issue_is_closed_with_what_the_fix_changed(ppy_home) -> None:
    from test_deficiencies import FakeGh, _reporter

    detail = PAP_222_REPORTS["73e968fea9ac0d8e"]
    before = lambda: datetime(2026, 9, 17, 1, 49, 12, tzinfo=UTC)  # noqa: E731
    deficiencies.record(deficiencies.TURN_REPORT, detail, clock=before)
    gh = FakeGh()
    reporter = _reporter(gh, clock=before)
    assert reporter.flush()  # opened an issue for it

    reporter.reclassify()
    closes = [(args, stdin) for args, stdin in gh.calls if args[:2] == ["issue", "close"]]
    assert len(closes) == 1
    said = " ".join(closes[0][0]) + (closes[0][1] or "")
    assert "PR #N updated" in said


def test_every_fixed_report_names_its_reduced_fingerprint_too() -> None:
    """Rows are rekeyed to the reduced turn-report fingerprint (task 285); both close."""
    for fix in deficiencies.FIXED_TURN_REPORTS:
        assert len(fix.fingerprints) == 2
    assert {"af2aa9532fde4fce", "7ba58c5a7c72a5b0", "c4b7a8dab0a58df5"} <= set().union(
        *(fix.fingerprints for fix in deficiencies.FIXED_TURN_REPORTS)
    )
