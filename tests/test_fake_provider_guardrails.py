"""The fake provider writes a stub, so it may only push to a local path.

On 2026-09-04 a dispatch that fell back to the fake provider pushed a stub branch
to github.com within seconds and the branch had to be deleted by hand (issue
#49). The default is fixed elsewhere; this pins the second lock — the worker
itself refuses the push, whatever sent it.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from conftest import make_git_repo
from papaya_agent_runtime.providers import fake_worker
from papaya_agent_runtime.providers.base import ProviderEvent
from papaya_agent_runtime.providers.fake import FakeProvider

BRANCH = "ppy/task-1-guardrail"
REMOTE_URL = "https://example.invalid/x.git"


def _git(path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


@pytest.fixture
def worktree(tmp_path):
    """A repo the fake worker can work in, with no origin yet."""
    return make_git_repo(tmp_path / "worktree")


@pytest.fixture
def git_spy(monkeypatch):
    """Every git command the worker runs, so "pushed nothing" is provable."""
    calls: list[list[str]] = []
    real = fake_worker._git

    def spy(args: list[str], cwd: str) -> str:
        calls.append(list(args))
        return real(args, cwd)

    monkeypatch.setattr(fake_worker, "_git", spy)
    return calls


def _run(worktree: str, instructions: str = "NODONE") -> int:
    spec = {
        "task_id": 1,
        "title": "stub work",
        "instructions": instructions,
        "worktree_path": worktree,
        "branch": BRANCH,
    }
    return fake_worker.main(["--spec", json.dumps(spec)])


def test_fake_worker_refuses_to_push_to_a_remote_and_names_it(
    worktree, git_spy, capsys, monkeypatch
) -> None:
    monkeypatch.delenv(fake_worker.ALLOW_REMOTE_PUSH_ENV, raising=False)
    _git(worktree, "remote", "add", "origin", REMOTE_URL)

    rc = _run(worktree)

    assert rc != 0
    assert not any(call[0] == "push" for call in git_spy), "the stub must never reach a remote"
    emitted = capsys.readouterr().out
    assert REMOTE_URL in emitted
    assert "refuses to push" in emitted


def test_fake_worker_still_pushes_to_a_local_bare_repo(
    worktree, git_spy, tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv(fake_worker.ALLOW_REMOTE_PUSH_ENV, raising=False)
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(worktree, "remote", "add", "origin", str(bare))

    rc = _run(worktree)

    assert rc == 0
    assert any(call[0] == "push" for call in git_spy)
    # The branch is really there: a local remote is what this worker is for.
    assert _git(bare, "rev-parse", "--verify", BRANCH)


def test_the_escape_hatch_allows_a_deliberate_push_to_a_url(
    worktree, git_spy, tmp_path, monkeypatch, capsys
) -> None:
    """A ``file://`` URL is a scheme, so it is refused — until the hatch is set.

    Same bare repo either way, so this pins the hatch and not the reachability of
    whatever is on the other end.
    """
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(worktree, "remote", "add", "origin", f"file://{bare}")

    monkeypatch.delenv(fake_worker.ALLOW_REMOTE_PUSH_ENV, raising=False)
    assert _run(worktree) != 0
    assert not any(call[0] == "push" for call in git_spy)
    assert "refuses to push" in capsys.readouterr().out

    monkeypatch.setenv(fake_worker.ALLOW_REMOTE_PUSH_ENV, "1")
    assert _run(worktree) == 0
    assert any(call[0] == "push" for call in git_spy)
    assert _git(bare, "rev-parse", "--verify", BRANCH)


def test_the_adapter_reports_the_refusal_as_a_failure_naming_the_remote() -> None:
    events = [
        ProviderEvent(kind="session", raw={}, session_id="s1", text=None),
        ProviderEvent(
            kind="error",
            raw={},
            session_id="s1",
            text=f"the fake provider refuses to push to {REMOTE_URL} — that is not a local path",
        ),
    ]

    result = FakeProvider().result(events, exit_code=4)

    assert result.status == "failed"
    assert REMOTE_URL in result.summary
    assert "exit=4" in result.summary
