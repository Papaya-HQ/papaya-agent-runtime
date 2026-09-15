"""The end-of-task auto-commit stages the branch's work, not the review's evidence.

The supervisor commits for a worker that finished without committing, so the
manager always has a reviewable head. That used to be a blanket `git add -A`,
which on 2026-09-02 swept a task's untracked evidence directory into the commit
and forced a rebuilt clean commit before it could ship.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from conftest import scale
from papaya_agent_runtime import repos
from papaya_agent_runtime.providers.base import TaskSpec
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor import autocommit
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.runner import _finalize_worktree
from papaya_agent_runtime.supervisor.server import SupervisorServer


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _write(root: Path, rel: str, text: str = "x\n"):
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


@pytest.fixture
def worktree(tmp_path):
    """A git repo with a .gitignore, standing in for a worker's leased worktree."""
    from conftest import make_git_repo as _make

    path = Path(_make(tmp_path / "wt"))
    _write(path, ".gitignore", "node_modules/\n*.tmp\n")
    _git(path, "add", ".gitignore")
    _git(path, "-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-qm", "ignore rules")
    return path


def _spec(worktree):
    return TaskSpec(
        task_id=76,
        title="polish the sheet",
        instructions="",
        worktree_path=str(worktree),
        base_sha="",
        provider="fake",
        run_id=1,
    )


def _committed_paths(worktree) -> set[str]:
    return set(_git(worktree, "show", "--name-only", "--pretty=format:", "HEAD").split())


# --------------------------------------------------------------------------- #
# The exclusion rules themselves
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "excluded"),
    [
        ("src/app.py", False),
        ("evidence/after.png", True),
        ("evidence/notes.md", True),
        ("backend/evidence/gate.txt", True),
        ("receipts/run.json", True),
        ("shot.png", True),
        ("src/ui/preview.png", True),
        ("docs/architecture.png", False),
        ("docs/images/flow.png", False),
        ("evidence.md", False),
        ("src/evidence_helper.py", False),
    ],
)
def test_the_default_rules_hold_back_evidence_but_keep_docs_images(path, excluded, tmp_path):
    assert autocommit.is_excluded(path, worktree=str(tmp_path)) is excluded


def test_the_default_rules_name_the_scratch_directory():
    assert "/private/tmp/" in autocommit.DEFAULT_EXCLUDES


def test_a_path_that_really_lives_in_an_excluded_location_is_held_back(tmp_path):
    """Location rules follow symlinks, so a link into scratch space cannot smuggle it in."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "receipt.json").write_text("{}")
    (tmp_path / "link.json").symlink_to(scratch / "receipt.json")
    rules = [f"{scratch}/"]
    assert autocommit.is_excluded("link.json", worktree=str(tmp_path), rules=rules) is True
    assert autocommit.is_excluded("other.json", worktree=str(tmp_path), rules=rules) is False


def test_the_rules_are_configurable(monkeypatch, tmp_path):
    monkeypatch.setenv(autocommit.EXCLUDE_ENV, "*.log, scratch/")
    assert autocommit.exclude_rules() == ["*.log", "scratch/"]
    assert autocommit.is_excluded("run.log", worktree=str(tmp_path)) is True
    assert autocommit.is_excluded("scratch/x.txt", worktree=str(tmp_path)) is True
    # The defaults no longer apply once the list is replaced.
    assert autocommit.is_excluded("evidence/after.png", worktree=str(tmp_path)) is False


# --------------------------------------------------------------------------- #
# Staging
# --------------------------------------------------------------------------- #


def test_the_commit_takes_the_work_and_leaves_the_evidence(worktree):
    _write(worktree, "src/app.py", "print('hi')\n")
    _write(worktree, "docs/diagram.png", "fake png\n")
    _write(worktree, "evidence/after.png")
    _write(worktree, "receipts/gate.json")
    _write(worktree, "screenshot.png")

    finalized = _finalize_worktree(_spec(worktree), "polish the sheet")

    assert _committed_paths(worktree) == {"src/app.py", "docs/diagram.png"}
    assert sorted(finalized.excluded) == [
        "evidence/after.png",
        "receipts/gate.json",
        "screenshot.png",
    ]
    assert sorted(finalized.committed) == ["docs/diagram.png", "src/app.py"]
    assert finalized.head_sha


def test_what_was_excluded_is_left_in_the_worktree_not_destroyed(worktree):
    _write(worktree, "src/app.py")
    _write(worktree, "evidence/after.png")

    _finalize_worktree(_spec(worktree), "s")

    assert (worktree / "evidence/after.png").exists()
    assert "evidence/after.png" in _git(worktree, "status", "--porcelain", "-uall")


def test_gitignored_paths_are_never_candidates(worktree):
    _write(worktree, "src/app.py")
    _write(worktree, "node_modules/pkg/index.js")
    _write(worktree, "build.tmp")

    finalized = _finalize_worktree(_spec(worktree), "s")

    assert finalized.committed == ["src/app.py"]
    # Ignored paths are not "excluded" — git never offered them at all.
    assert finalized.excluded == []
    assert _committed_paths(worktree) == {"src/app.py"}


def test_a_deletion_is_still_committed(worktree):
    _write(worktree, "src/app.py")
    _finalize_worktree(_spec(worktree), "s")
    (worktree / "src/app.py").unlink()

    finalized = _finalize_worktree(_spec(worktree), "remove it")

    assert finalized.committed == ["src/app.py"]
    assert "src/app.py" not in _git(worktree, "ls-files")


def test_nothing_is_committed_when_only_excluded_paths_changed(worktree):
    before = _git(worktree, "rev-parse", "HEAD")
    _write(worktree, "evidence/after.png")

    finalized = _finalize_worktree(_spec(worktree), "s")

    assert finalized.committed == []
    assert finalized.excluded == ["evidence/after.png"]
    assert finalized.head_sha == before, "an evidence-only worktree must not create a commit"


def test_paths_with_spaces_survive_intact(worktree):
    """Status is read with `-z`, so git's quoting never turns one path into two."""
    _write(worktree, "src/a file.py")
    finalized = _finalize_worktree(_spec(worktree), "s")
    assert finalized.committed == ["src/a file.py"]
    assert "src/a file.py" in _git(worktree, "ls-files").splitlines()


# --------------------------------------------------------------------------- #
# End to end: the exclusion is recorded in the task's event
# --------------------------------------------------------------------------- #


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


def _events(task_id, kind):
    conn = init_db()
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def test_the_task_event_names_what_the_autocommit_left_out(server, source_repo):
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = client.dispatch_task(repo=added.name, title="ship", instructions="NOCOMMIT")[
        "task_id"
    ]
    deadline = time.monotonic() + scale(20)
    while time.monotonic() < deadline and not _events(task_id, "worker_done"):
        time.sleep(0.05)

    recorded = _events(task_id, "autocommit")
    assert recorded, "an auto-commit must say what it did"
    assert recorded[-1]["excluded"] == ["evidence/after.png"]
    assert recorded[-1]["committed"] == [f"ppy-fake-{task_id}.txt"]
    assert "evidence/after.png" in recorded[-1]["summary"]

    done = _events(task_id, "worker_done")
    assert done[-1]["excluded_from_commit"] == ["evidence/after.png"]

    worktree = store.get_task(init_db(), task_id)["worktree_path"]
    assert _committed_paths(worktree) == {f"ppy-fake-{task_id}.txt"}
    assert Path(worktree, "evidence/after.png").exists()
