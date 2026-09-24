"""A worker's branch never picks up build artifacts from the runtime's auto-commit.

On 2026-09-23, three times in one local E2E on a repository with no `.gitignore`, a
worker pushed a clean branch, the runtime committed again on its own and put
`__pycache__/*.pyc` and `uv.lock` on the lease branch, and review — reading the
worktree's local head — sent the worker back to remove files nobody wrote. These
tests pin both halves of the fix: the auto-commit leaves untracked artifacts out and
names them, and a local head that adds nothing but artifacts over the pushed branch is
dropped for the pushed head before review and delivery.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from conftest import approve_with_description, make_git_repo, wait_until
from papaya_agent_runtime import delivery, repos, review, serve, stacks
from papaya_agent_runtime.providers.base import TaskSpec
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor import autocommit
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.runner import _finalize_worktree
from papaya_agent_runtime.supervisor.server import SupervisorServer


def _git(path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(root, rel: str, text: str = "x\n") -> Path:
    target = Path(root) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


def _commit_all(path, message: str) -> str:
    _git(path, "add", "-A")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)
    return _git(path, "rev-parse", "HEAD")


def _committed_paths(worktree) -> set[str]:
    return set(_git(worktree, "show", "--name-only", "--pretty=format:", "HEAD").split())


def _spec(worktree) -> TaskSpec:
    return TaskSpec(
        task_id=381,
        title="the endpoint",
        instructions="",
        worktree_path=str(worktree),
        base_sha="",
        provider="fake",
        run_id=1,
    )


def _events(task_id: int, kind: str) -> list[dict]:
    rows = (
        init_db()
        .execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id",
            (task_id, kind),
        )
        .fetchall()
    )
    return [json.loads(row["payload"]) for row in rows]


# --------------------------------------------------------------------------- #
# What counts as an artifact
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "root"),
    [
        ("__pycache__/app.cpython-313.pyc", "__pycache__/"),
        ("src/pkg/__pycache__/mod.cpython-313.pyc", "src/pkg/__pycache__/"),
        ("stray.pyc", "stray.pyc"),
        (".venv/lib/python3.13/site-packages/x.py", ".venv/"),
        ("web/node_modules/left-pad/index.js", "web/node_modules/"),
        (".pytest_cache/v/cache/nodeids", ".pytest_cache/"),
        (".ruff_cache/0.6/123", ".ruff_cache/"),
        (".mypy_cache/3.13/x.json", ".mypy_cache/"),
        ("dist/pkg-1.0.tar.gz", "dist/"),
        ("build/lib/pkg/__init__.py", "build/"),
        ("src/pkg.egg-info/PKG-INFO", "src/pkg.egg-info/"),
        (".ppy-evidence/gate.txt", ".ppy-evidence/"),
        (".mm-evidence/probes.md", ".mm-evidence/"),
        ("uv.lock", "uv.lock"),
        ("services/api/package-lock.json", "services/api/package-lock.json"),
        ("pnpm-lock.yaml", "pnpm-lock.yaml"),
        ("yarn.lock", "yarn.lock"),
        ("poetry.lock", "poetry.lock"),
        # Real work that only resembles an artifact by name.
        ("src/app.py", None),
        ("src/build_helper.py", None),
        ("docs/build.md", None),
        ("dist.py", None),
        ("uv.lock.md", None),
    ],
)
def test_artifacts_are_named_by_their_root(path, root) -> None:
    assert autocommit.artifact_root(path) == root
    assert autocommit.is_artifact(path) is (root is not None)


def test_replacing_the_exclusion_list_does_not_let_artifacts_back_in(monkeypatch, tmp_path):
    monkeypatch.setenv(autocommit.EXCLUDE_ENV, "*.log")
    repo = Path(make_git_repo(tmp_path / "wt"))
    _write(repo, "src/app.py")
    _write(repo, "__pycache__/app.cpython-313.pyc")

    staged, excluded = autocommit.stage(str(repo))

    assert staged == ["src/app.py"]
    assert excluded == ["__pycache__/"]


# --------------------------------------------------------------------------- #
# Goal 1: the auto-commit leaves untracked artifacts out, and says so
# --------------------------------------------------------------------------- #


def test_a_repo_without_gitignore_keeps_pyc_and_an_untracked_lockfile_off_the_commit(tmp_path):
    repo = Path(make_git_repo(tmp_path / "wt"))
    assert not (repo / ".gitignore").exists()
    _write(repo, "src/app.py", "print('hi')\n")
    _write(repo, "__pycache__/app.cpython-313.pyc")
    _write(repo, "src/__pycache__/app.cpython-313.pyc")
    _write(repo, "src/__pycache__/util.cpython-313.pyc")
    _write(repo, "uv.lock", "version = 1\n")
    _write(repo, ".venv/bin/python")
    _write(repo, ".venv/lib/site.py")

    finalized = _finalize_worktree(_spec(repo), "the endpoint")

    assert _committed_paths(repo) == {"src/app.py"}
    assert finalized.committed == ["src/app.py"]
    # Named once per artifact, however many files it holds.
    assert sorted(finalized.excluded) == [".venv/", "__pycache__/", "src/__pycache__/", "uv.lock"]
    # Held back, not destroyed.
    assert (repo / "uv.lock").exists()
    assert (repo / "src/__pycache__/app.cpython-313.pyc").exists()


def test_an_artifact_only_worktree_makes_no_commit(tmp_path):
    repo = Path(make_git_repo(tmp_path / "wt"))
    before = _git(repo, "rev-parse", "HEAD")
    _write(repo, "__pycache__/app.cpython-313.pyc")
    _write(repo, "uv.lock")

    finalized = _finalize_worktree(_spec(repo), "s")

    assert finalized.committed == []
    assert finalized.head_sha == before


def test_a_lockfile_the_repository_tracks_is_committed_when_it_changes(tmp_path):
    repo = Path(make_git_repo(tmp_path / "wt"))
    _write(repo, "uv.lock", "version = 1\n")
    _write(repo, "package-lock.json", "{}\n")
    _commit_all(repo, "track the lockfiles")
    _write(repo, "uv.lock", "version = 1\n[[package]]\nname = 'httpx'\n")
    _write(repo, "package-lock.json", '{"lockfileVersion": 3}\n')
    _write(repo, "pyproject.toml", "[project]\nname = 'x'\n")

    finalized = _finalize_worktree(_spec(repo), "add httpx")

    assert _committed_paths(repo) == {"uv.lock", "package-lock.json", "pyproject.toml"}
    assert finalized.excluded == []


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


def _wait_terminal(client, task_id: int, timeout: float = 15.0) -> str:
    def finished():
        status = client.task_status(task_id)["task"]["status"]
        return status if status in {"worker_done", "blocked", "failed"} else None

    return wait_until(finished, timeout, what=f"task {task_id} to finish", interval=0.1)


def test_the_autocommit_event_names_the_artifacts_it_left_out(server, source_repo):
    _srv, client = server
    added = repos.add_repo(source_repo)
    task_id = client.dispatch_task(
        repo=added.name, title="ship", instructions="NOCOMMIT LEFTOVERS"
    )["task_id"]
    wait_until(lambda: _events(task_id, "worker_done"), 20, what="worker_done", interval=0.05)

    (recorded,) = _events(task_id, "autocommit")
    assert recorded["committed"] == [f"ppy-fake-{task_id}.txt"]
    assert sorted(recorded["excluded"]) == ["__pycache__/", "evidence/after.png", "uv.lock"]
    assert "__pycache__/" in recorded["summary"] and "uv.lock" in recorded["summary"]

    worktree = store.get_task(init_db(), task_id)["worktree_path"]
    assert _committed_paths(worktree) == {f"ppy-fake-{task_id}.txt"}


# --------------------------------------------------------------------------- #
# Goal 2: review and delivery use the pushed head, not an artifact-only local one
# --------------------------------------------------------------------------- #


def _task_on_a_pushed_branch(client, source_repo) -> dict:
    added = repos.add_repo(source_repo)
    resp = client.dispatch_task(repo=added.name, title="layer")
    _wait_terminal(client, resp["task_id"])
    task = client.task_status(resp["task_id"])["task"]
    _git(task["worktree_path"], "push", "-q", "origin", f"HEAD:{task['branch']}")
    return task


def _row(task_id: int):
    return store.get_task(init_db(), task_id)


def test_an_artifact_only_local_commit_is_dropped_for_the_pushed_head(server, source_repo):
    _srv, client = server
    task = _task_on_a_pushed_branch(client, source_repo)
    worktree = task["worktree_path"]
    pushed = _git(worktree, "rev-parse", "HEAD")
    _write(worktree, "__pycache__/app.cpython-313.pyc")
    _write(worktree, "src/__pycache__/util.cpython-313.pyc")
    _write(worktree, "uv.lock")
    local = _commit_all(worktree, f"task {task['id']}: the runtime's own commit")

    line = serve.drop_artifact_commits(task["id"])

    assert _git(worktree, "rev-parse", "HEAD") == pushed
    # The worktree is on the pushed head and nothing of the branch's is dirty; the
    # artifacts stay on disk, untracked, and are not uncommitted work.
    assert (Path(worktree) / "uv.lock").exists()
    assert serve.uncommitted_files(task["id"]) == []
    (event,) = _events(task["id"], stacks.ARTIFACT_COMMIT_DROPPED)
    assert (event["from"], event["to"], event["commits"]) == (local, pushed, 1)
    assert sorted(event["paths"]) == ["__pycache__/", "src/__pycache__/", "uv.lock"]
    assert line == f"Worker task {task['id']}: {event['summary']}."
    assert pushed[:8] in line and "uv.lock" in line
    # Said once: a second look finds nothing to drop.
    assert serve.drop_artifact_commits(task["id"]) == ""


def test_delivery_uses_the_pushed_head_when_the_local_one_only_adds_artifacts(server, source_repo):
    _srv, client = server
    task = _task_on_a_pushed_branch(client, source_repo)
    worktree = task["worktree_path"]
    pushed = _git(worktree, "rev-parse", "HEAD")
    approve_with_description(task["id"], head=pushed)
    _write(worktree, "__pycache__/app.cpython-313.pyc")
    _write(worktree, "uv.lock")
    _commit_all(worktree, "the runtime's own commit")

    result = delivery.deliver(task["id"], open_pr=False)

    assert result.head_sha == pushed
    assert _git(worktree, "rev-parse", "HEAD") == pushed
    assert _git(source_repo, "rev-parse", task["branch"]) == pushed
    assert "uv.lock" not in _git(source_repo, "ls-tree", "-r", "--name-only", task["branch"])
    assert len(_events(task["id"], stacks.ARTIFACT_COMMIT_DROPPED)) == 1


def test_an_interactive_review_reads_the_pushed_head_too(server, source_repo):
    """`ppy review show` is how a session reviews; it must not see the runtime's commit."""
    _srv, client = server
    task = _task_on_a_pushed_branch(client, source_repo)
    worktree = task["worktree_path"]
    pushed = _git(worktree, "rev-parse", "HEAD")
    _write(worktree, "__pycache__/app.cpython-313.pyc")
    _commit_all(worktree, "the runtime's own commit")

    bundle = review.build_bundle(task["id"])

    assert bundle.head_sha == pushed
    assert "__pycache__" not in bundle.diffstat
    assert len(_events(task["id"], stacks.ARTIFACT_COMMIT_DROPPED)) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"src/feature.py": "def feature(): ...\n"},
        # A real change riding with artifacts is still a real change.
        {"src/feature.py": "def feature(): ...\n", "__pycache__/app.cpython-313.pyc": "x"},
        # A modification of a tracked file is never an artifact, whatever it is next to.
        {"README.md": "# changed\n", "uv.lock": "version = 1\n"},
    ],
)
def test_a_real_change_beyond_the_pushed_head_is_kept(server, source_repo, extra):
    _srv, client = server
    task = _task_on_a_pushed_branch(client, source_repo)
    worktree = task["worktree_path"]
    for rel, text in extra.items():
        _write(worktree, rel, text)
    local = _commit_all(worktree, "work the remote has not seen")

    assert stacks.drop_artifact_commits(init_db(), _row(task["id"])) is None
    assert serve.drop_artifact_commits(task["id"]) == ""
    result = stacks.sync_worktree_with_remote(task["id"])

    assert result["action"] == "none"
    assert _git(worktree, "rev-parse", "HEAD") == local
    assert _events(task["id"], stacks.ARTIFACT_COMMIT_DROPPED) == []


def test_a_lockfile_the_branch_already_tracks_is_not_dropped_when_it_changes(server, source_repo):
    _srv, client = server
    task = _task_on_a_pushed_branch(client, source_repo)
    worktree = task["worktree_path"]
    _write(worktree, "uv.lock", "version = 1\n")
    _commit_all(worktree, "track uv.lock")
    _git(worktree, "push", "-q", "origin", f"HEAD:{task['branch']}")
    _write(worktree, "uv.lock", "version = 1\n[[package]]\n")
    local = _commit_all(worktree, "bump a dependency")

    assert serve.drop_artifact_commits(task["id"]) == ""
    assert _git(worktree, "rev-parse", "HEAD") == local


def test_a_head_the_remote_already_holds_is_left_alone(server, source_repo):
    _srv, client = server
    task = _task_on_a_pushed_branch(client, source_repo)
    head = _git(task["worktree_path"], "rev-parse", "HEAD")

    assert serve.drop_artifact_commits(task["id"]) == ""
    assert _git(task["worktree_path"], "rev-parse", "HEAD") == head


def test_uncommitted_work_ignores_untracked_artifacts_but_not_real_files(server, source_repo):
    _srv, client = server
    task = _task_on_a_pushed_branch(client, source_repo)
    worktree = task["worktree_path"]
    _write(worktree, "__pycache__/app.cpython-313.pyc")
    _write(worktree, "uv.lock")
    _write(worktree, ".venv/bin/python")
    assert serve.uncommitted_files(task["id"]) == []

    _write(worktree, "src/forgot.py")
    assert serve.uncommitted_files(task["id"]) == ["src/forgot.py"]
