"""Shared fixtures for hermetic tests."""

from __future__ import annotations

import os
import subprocess

import pytest

from papaya_agent_runtime import capabilities, papaya
from papaya_agent_runtime.providers import capability


@pytest.fixture(autouse=True)
def _force_git_lease_backend(monkeypatch):
    """Keep the hermetic suite on the git lease backend.

    The treehouse binary may be installed on the dev machine; without this, the
    supervisor's auto-selected backend would mutate the global ~/.treehouse pool.
    The opt-in live test constructs ``LeaseManager(backend="treehouse")``
    explicitly, which overrides this env.
    """
    monkeypatch.setenv("PPY_LEASE_BACKEND", "git")


@pytest.fixture(autouse=True)
def _fixture_repos_are_their_own_forge(request, monkeypatch):
    """A hermetic fixture repo's forge is the local source repo it was cloned from.

    ``ppy repo add`` refuses a path with no forge remote, because a repo with
    nowhere to open a pull request is a delivery that strands. Temp git repos on
    disk have no origin at all, so every test that registers one would have to
    invent a GitHub URL — and then delivery would try to push to github.com. So
    the fixtures register the source path as their own forge: no second remote is
    created, and push/PR resolution stays local and offline.

    Tests of the registration rule itself carry ``@pytest.mark.strict_forge`` and
    get the real function.
    """
    if "strict_forge" in request.keywords:
        return
    from papaya_agent_runtime import repos

    real = repos.add_repo

    def add_repo(url_or_path, name=None, forge_url=None):
        return real(url_or_path, name=name, forge_url=forge_url or url_or_path)

    monkeypatch.setattr(repos, "add_repo", add_repo)


@pytest.fixture(autouse=True)
def _hermetic_dispatch_defaults_to_fake(request, monkeypatch):
    """A hermetic dispatch that names no provider means the fake one.

    In production an unnamed provider resolves to the configured worker ceiling
    (``config.default_worker_provider``) and is never ``fake`` — defaulting to
    ``fake`` is what pushed a stub branch to a live remote on 2026-09-04 (issue
    #49). The hermetic suite has no config and wants the deterministic local
    worker, so the default is substituted here, in the tests, rather than left as
    a fallback in the shipped code where a real dispatch could land on it.

    Tests of the resolution rule itself carry ``@pytest.mark.real_provider_default``
    and see the real function.
    """
    if "real_provider_default" in request.keywords:
        return
    from papaya_agent_runtime import config

    monkeypatch.setattr(config, "default_worker_provider", lambda: "fake")


@pytest.fixture(autouse=True)
def _isolated_treehouse_home(tmp_path, monkeypatch):
    """Keep the pool scan off the developer's real treehouse pool.

    ``ppy worktree list`` walks the treehouse pool roots of every registered repo,
    and ``prune`` deletes what it finds there. A test registering a repo whose
    name happened to prefix a real pool directory would otherwise put the
    developer's own worktrees in range.
    """
    monkeypatch.setenv("PPY_TREEHOUSE_HOME", str(tmp_path / "treehouse-home"))


@pytest.fixture(autouse=True)
def _isolated_capability_record(tmp_path, monkeypatch):
    """Keep the capability gate off the developer's machine-local probe record.

    ``.ppy/provider-capabilities.json`` overrides the tracked matrix per provider,
    and ``PPY_HOME`` falls back to the cwd — so on a developer machine that has run
    `make probe`, any test not requesting ``ppy_home`` would read that real record
    and assert against whatever this laptop happens to prove. Point the override
    at an empty temp home so the hermetic suite sees only the tracked matrix.
    """
    monkeypatch.setenv("PPY_HOME", str(tmp_path / "capability-home"))
    capability.clear_cache()
    yield
    capability.clear_cache()


@pytest.fixture(autouse=True)
def _no_real_papaya_connection(tmp_path_factory, monkeypatch):
    """Keep the hermetic suite off this machine's real Papaya connection.

    The runtime discovers a connection wherever one was made — a terminal's
    `~/.papaya-agent`, or the desktop app's own directory — which means that
    without this, every test reads whichever agent the developer happens to be
    connected as. Three pull-request-body tests started asserting against a real
    handle the moment discovery landed. Point it at an empty directory so a test
    sees "not connected" unless it says otherwise, and so CI and a laptop agree.
    """
    monkeypatch.setenv(
        papaya.HOME_ENV, str(tmp_path_factory.mktemp("papaya-client-home", numbered=True))
    )
    # A developer running the suite inside a supervised session inherits the host's
    # client version, which readiness reads. Without this, whether the suite sees a
    # `client_behind_host` warning depends on how the shell was launched.
    monkeypatch.delenv(capabilities.HOST_CLIENT_VERSION_ENV, raising=False)


@pytest.fixture
def ppy_home(tmp_path, monkeypatch):
    """Point .ppy state at an isolated temp dir for the test."""
    home = tmp_path / ".ppy"
    monkeypatch.setenv("PPY_HOME", str(home))
    return home


def make_git_repo(path) -> str:
    path.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)

    run("init", "-q", "-b", "main")
    run("config", "user.name", "Tester")
    run("config", "user.email", "tester@example.com")
    (path / "README.md").write_text("# fixture\n")
    run("add", ".")
    run("commit", "-qm", "init")
    return str(path)


@pytest.fixture
def source_repo(tmp_path):
    return make_git_repo(tmp_path / "source")


#: How much longer a poll may wait than it would on a developer's laptop.
#:
#: Every asynchronous assertion in this suite is a poll with a deadline: it returns
#: the instant its predicate is true, so a generous deadline costs nothing on a
#: green run and only makes a genuine failure slower to report. On a two-core CI
#: runner the same suite takes roughly twice as long as it does locally, which is
#: how three unrelated supervisor tests failed on the first CI run — different ones
#: on each Python version, none of them actually broken. One knob scales every
#: deadline rather than fifteen hand-tuned numbers drifting apart.
TIMEOUT_SCALE = float(os.environ.get("PPY_TEST_TIMEOUT_SCALE", "1") or "1")


def scale(seconds: float) -> float:
    """A polling deadline, stretched for slower machines."""
    return seconds * TIMEOUT_SCALE
