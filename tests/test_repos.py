"""Hermetic tests for repository registration (offline, local git only).

The sync tests pin the thing that made every un-based dispatch start from stale
code: sync recorded the base clone's local ``HEAD``, which nothing ever moved, so
a base clone registered weeks earlier kept handing workers an old commit.
"""

from __future__ import annotations

import subprocess

import pytest

from papaya_agent_runtime import repos


@pytest.fixture
def ppy_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    return tmp_path / ".ppy"


def _git(path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _make_source_repo(path) -> str:
    path.mkdir(parents=True)
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", *a], cwd=path, check=True, capture_output=True
    )
    run("init", "-q", "-b", "main")
    run("config", "user.name", "t")
    run("config", "user.email", "t@t")
    (path / "file.txt").write_text("hello\n")
    run("add", ".")
    run("commit", "-qm", "init")
    return str(path)


def _commit_on_origin(source: str, message: str) -> str:
    subprocess.run(["git", "-C", source, "commit", "--allow-empty", "-qm", message], check=True)
    return _git(source, "rev-parse", "HEAD")


def test_derive_name() -> None:
    assert repos.derive_name("https://github.com/acme/widgets.git") == "widgets"
    assert repos.derive_name("/home/me/projects/thing/") == "thing"


def test_add_list_sync(tmp_path, ppy_home) -> None:
    source = _make_source_repo(tmp_path / "source")
    added = repos.add_repo(source)
    assert added.name == "source"
    assert added.default_branch == "main"
    assert added.base_sha

    listed = repos.list_repos()
    assert len(listed) == 1
    assert listed[0]["name"] == "source"

    # Add a new commit to the source, then sync.
    _commit_on_origin(source, "next")
    res = repos.sync_repo("source")
    assert res.base_sha


def test_duplicate_registration_rejected(tmp_path, ppy_home) -> None:
    source = _make_source_repo(tmp_path / "source")
    repos.add_repo(source)
    with pytest.raises(repos.RepoError):
        repos.add_repo(source)


# --------------------------------------------------------------------------- #
# Sync fast-forwards the base clone rather than re-recording its stale HEAD
# --------------------------------------------------------------------------- #


def test_sync_fast_forwards_the_base_clone_to_the_remote_tip(tmp_path, ppy_home) -> None:
    source = _make_source_repo(tmp_path / "source")
    added = repos.add_repo(source)
    clone = added.local_path

    tip = _commit_on_origin(source, "moved on without us")
    res = repos.sync_repo("source")

    assert res.fast_forwarded is True
    assert res.base_sha == tip
    assert res.previous_sha == added.base_sha
    # The clone's own branch moved too — a worker leased from it starts at the tip.
    assert _git(clone, "rev-parse", "HEAD") == tip
    assert _git(clone, "rev-parse", "main") == tip
    assert repos.list_repos()[0]["base_sha"] == tip


def test_sync_is_a_no_op_when_the_base_clone_is_already_current(tmp_path, ppy_home) -> None:
    source = _make_source_repo(tmp_path / "source")
    added = repos.add_repo(source)
    res = repos.sync_repo("source")
    assert res.fast_forwarded is False
    assert res.already_current is True
    assert res.base_sha == added.base_sha


def test_sync_refuses_a_dirty_base_clone_and_names_the_paths(tmp_path, ppy_home) -> None:
    source = _make_source_repo(tmp_path / "source")
    added = repos.add_repo(source)
    clone = added.local_path
    (tmp_path / "source" / "file.txt").write_text("hello again\n")
    _commit_on_origin(source, "remote moved")

    # An edit to a tracked file and an untracked file: both must stop the sync.
    with open(f"{clone}/file.txt", "w", encoding="utf-8") as fh:
        fh.write("edited by hand\n")
    with open(f"{clone}/scratch.txt", "w", encoding="utf-8") as fh:
        fh.write("notes\n")

    with pytest.raises(repos.RepoError) as exc:
        repos.sync_repo("source")
    message = str(exc.value)
    assert "file.txt" in message
    assert "scratch.txt" in message
    # Nothing moved: the refusal changes no state.
    assert repos.list_repos()[0]["base_sha"] == added.base_sha
    assert _git(clone, "rev-parse", "HEAD") == added.base_sha


def test_sync_refuses_rather_than_rewriting_a_base_clone_that_has_diverged(
    tmp_path, ppy_home
) -> None:
    """A base clone holding its own commits is never reset — that would destroy them."""
    source = _make_source_repo(tmp_path / "source")
    added = repos.add_repo(source)
    clone = added.local_path
    subprocess.run(
        ["git", "-C", clone, "-c", "user.name=t", "-c", "user.email=t@t"]
        + ["commit", "--allow-empty", "-qm", "local only"],
        check=True,
    )
    local_only = _git(clone, "rev-parse", "HEAD")
    _commit_on_origin(source, "remote moved")

    with pytest.raises(repos.RepoError) as exc:
        repos.sync_repo("source")
    assert "fast-forward" in str(exc.value)
    assert _git(clone, "rev-parse", "HEAD") == local_only


def test_sync_reports_a_stray_ppy_directory_and_clears_it_on_request(tmp_path, ppy_home) -> None:
    source = _make_source_repo(tmp_path / "source")
    added = repos.add_repo(source)
    stray = tmp_path / ".ppy" / "repos" / "source" / ".ppy"
    (stray / "repos").mkdir(parents=True)

    res = repos.sync_repo("source")
    assert res.stray_ppy is True
    assert res.stray_ppy_removed is False
    assert any(".ppy" in note and "--clean-stray-ppy" in note for note in res.notes)
    assert stray.exists(), "reporting must not delete anything on its own"
    # The stray directory is not treated as a dirty base clone.
    assert repos.dirty_paths(added.local_path) == []

    res = repos.sync_repo("source", clean_stray_ppy=True)
    assert res.stray_ppy is True
    assert res.stray_ppy_removed is True
    assert not stray.exists()


def test_sync_of_an_unregistered_repo_says_so(ppy_home) -> None:
    with pytest.raises(repos.RepoError) as exc:
        repos.sync_repo("nope")
    assert "not registered" in str(exc.value)


# --------------------------------------------------------------------------- #
# Registration carries the forge — the place a pull request can be opened
# --------------------------------------------------------------------------- #

GITHUB_URL = "https://github.com/Papaya-HQ/papaya-infra"


def test_forge_urls_are_recognised_and_reduced_to_owner_and_name() -> None:
    for url in (
        GITHUB_URL,
        GITHUB_URL + ".git",
        "git@github.com:Papaya-HQ/papaya-infra.git",
        "ssh://git@github.com/Papaya-HQ/papaya-infra",
    ):
        assert repos.is_forge_url(url), url
        assert repos.forge_slug(url) == "Papaya-HQ/papaya-infra"
    for not_a_forge in ("/Users/me/workspace/papaya-infra", "", None, "https://example.com/x/y"):
        assert not repos.is_forge_url(not_a_forge)


def test_only_a_plain_path_counts_as_a_local_remote() -> None:
    """What decides whether the fake provider may push (issue #49)."""
    for local in ("/Users/me/workspace/papaya-infra", "../peer.git", "./x", "relative/path"):
        assert repos.is_local_remote(local), local
    for elsewhere in (
        GITHUB_URL,
        "git@github.com:Papaya-HQ/papaya-infra.git",
        "ssh://git@github.com/Papaya-HQ/papaya-infra",
        "http://internal.example/x.git",
        "file:///Users/me/workspace/papaya-infra",
        # Unreadable is not a licence to push.
        "",
        "   ",
        None,
    ):
        assert not repos.is_local_remote(elsewhere), elsewhere


@pytest.mark.strict_forge
def test_add_from_a_path_takes_the_forge_from_that_path_s_origin(tmp_path, ppy_home) -> None:
    source = _make_source_repo(tmp_path / "source")
    subprocess.run(["git", "-C", source, "remote", "add", "origin", GITHUB_URL], check=True)

    added = repos.add_repo(source)
    assert added.forge_url == GITHUB_URL
    assert repos.list_repos()[0]["forge_url"] == GITHUB_URL
    # The base clone's own origin is the local path, so the forge is reachable
    # through a second remote that worktrees inherit.
    assert repos.remote_url(added.local_path) == source
    assert repos.remote_url(added.local_path, repos.FORGE_REMOTE) == GITHUB_URL
    assert repos.upstream_remote(repos.list_repos()[0]) == repos.FORGE_REMOTE


@pytest.mark.strict_forge
def test_add_from_a_path_with_a_local_origin_refuses_until_a_forge_is_named(
    tmp_path, ppy_home
) -> None:
    upstream = _make_source_repo(tmp_path / "upstream")
    source = _make_source_repo(tmp_path / "source")
    subprocess.run(["git", "-C", source, "remote", "add", "origin", upstream], check=True)

    with pytest.raises(repos.RepoError) as exc:
        repos.add_repo(source)
    message = str(exc.value)
    assert "--forge-url" in message
    assert upstream in message  # the refusal names what it found instead of a forge
    assert repos.list_repos() == []

    added = repos.add_repo(source, forge_url=GITHUB_URL)
    assert added.forge_url == GITHUB_URL


@pytest.mark.strict_forge
def test_add_from_a_path_with_no_origin_at_all_refuses(tmp_path, ppy_home) -> None:
    source = _make_source_repo(tmp_path / "source")
    with pytest.raises(repos.RepoError) as exc:
        repos.add_repo(source)
    assert "no origin remote" in str(exc.value)


@pytest.mark.strict_forge
def test_add_from_a_url_records_that_url_as_the_forge(tmp_path, ppy_home, monkeypatch) -> None:
    """A repo registered by URL is its own forge; only the clone needs the network."""
    source = _make_source_repo(tmp_path / "source")
    real_git = repos._git

    def fake_git(args, cwd=None):
        if args[:2] == ["clone", "--quiet"]:
            return real_git(["clone", "--quiet", source, args[-1]], cwd=cwd)
        return real_git(args, cwd=cwd)

    monkeypatch.setattr(repos, "_git", fake_git)
    added = repos.add_repo(GITHUB_URL)
    assert added.name == "papaya-infra"
    assert added.forge_url == GITHUB_URL


@pytest.mark.strict_forge
def test_repo_list_and_doctor_surface_a_repo_with_no_forge(tmp_path, ppy_home, capsys) -> None:
    from papaya_agent_runtime.cli import main
    from papaya_agent_runtime.setup.doctor import render_text

    source = _make_source_repo(tmp_path / "source")
    assert main(["repo", "add", source, "--forge-url", GITHUB_URL]) == 0
    assert GITHUB_URL in capsys.readouterr().out

    assert main(["repo", "list"]) == 0
    assert GITHUB_URL in capsys.readouterr().out

    # A registration made before forge URLs existed has none; doctor says so.
    from papaya_agent_runtime.state import init_db, store

    store.update_repo_fields(init_db(), "source", forge_url=None)
    assert main(["repo", "list"]) == 0
    assert "NO FORGE" in capsys.readouterr().out
    rendered = render_text({**_doctor_stub(), "repos": [{"name": "source", "forge_url": None}]})
    assert "NO FORGE" in rendered
    assert "--forge-url" in rendered


def _doctor_stub() -> dict:
    return {
        "ppy_home": "/tmp/.ppy",
        "config": {"path": "/tmp/.ppy/config.toml", "present": False},
        "state_db": {"present": False},
        "environment": {"harnesses": [], "requirements": [], "companions": []},
        "usable_harnesses": [],
        "capability_drift": [],
    }


def test_repo_sync_cli_reports_the_fast_forward(tmp_path, ppy_home, capsys) -> None:
    from papaya_agent_runtime.cli import main

    source = _make_source_repo(tmp_path / "source")
    repos.add_repo(source)
    tip = _commit_on_origin(source, "remote moved")

    assert main(["repo", "sync", "source"]) == 0
    out = capsys.readouterr().out
    assert "fast-forwarded main" in out
    assert tip[:8] in out


# --------------------------------------------------------------------------- #
# Delivery follows the forge, not the base clone's local origin
# --------------------------------------------------------------------------- #


@pytest.mark.strict_forge
def test_delivery_pushes_and_opens_the_pr_on_the_registered_forge(
    tmp_path, ppy_home, monkeypatch
) -> None:
    """The incident: a path-registered repo pushed fine and had no forge for the PR."""
    from types import SimpleNamespace

    from papaya_agent_runtime import delivery
    from papaya_agent_runtime.state import init_db, store

    source = _make_source_repo(tmp_path / "papaya-infra")
    added = repos.add_repo(source, forge_url=GITHUB_URL)

    conn = init_db()
    run_id = store.create_run(conn, "deliver")
    repo_row = store.get_repo(conn, added.name)
    task_id = store.add_task(conn, run_id=run_id, title="ship it", repo_id=repo_row["id"])
    store.update_task_fields(conn, task_id, worktree_path=added.local_path, branch="ppy/task-1-abc")

    calls: list[list[str]] = []

    def fake_run(argv, cwd=None):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="https://github.com/pr/1\n", stderr="")

    monkeypatch.setattr(delivery, "_run", fake_run)
    monkeypatch.setattr(delivery, "is_approved_at_head", lambda tid: (True, ""))
    monkeypatch.setattr(delivery, "head_sha", lambda wt: "f" * 40)
    monkeypatch.setattr(delivery, "_pr_tool", lambda: "gh")

    delivery.deliver(task_id)
    push_argv = next(a for a in calls if a[:2] == ["git", "push"])
    assert push_argv[2] == repos.FORGE_REMOTE
    pr_argv = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    assert pr_argv[pr_argv.index("--repo") + 1] == "Papaya-HQ/papaya-infra"

    # An explicit remote still wins.
    calls.clear()
    delivery.deliver(task_id, remote="origin")
    push_argv = next(a for a in calls if a[:2] == ["git", "push"])
    assert push_argv[2] == "origin"
