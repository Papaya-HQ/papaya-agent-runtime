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


def fake_forge(monkeypatch, url: str, path: str) -> None:
    """Every git command reaching for ``url`` reaches the repository at ``path`` instead.

    `url.<base>.insteadOf` through the environment, so clone, fetch and ls-remote all
    go there, while the URL each clone records as its `origin` is still ``url``.
    """
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{path}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", url)


def _forge_with_a_feature_branch(tmp_path) -> str:
    """A forge whose HEAD is `main`, and which also has `feature/x` one commit ahead."""
    forge = _make_source_repo(tmp_path / "forge")
    _git(forge, "checkout", "-q", "-b", "feature/x")
    _commit_on_origin(forge, "feature work")
    _git(forge, "checkout", "-q", "main")
    return forge


def _checkout_of(forge: str, path, *, branch: str, origin: str) -> str:
    """A person's checkout of the forge, on ``branch``, whose origin names the forge's URL."""
    subprocess.run(["git", "clone", "-q", forge, str(path)], check=True, capture_output=True)
    _git(path, "checkout", "-q", branch)
    _git(path, "remote", "set-url", "origin", origin)
    return str(path)


@pytest.mark.strict_forge
def test_add_from_a_path_clones_the_forge_and_follows_its_head_not_the_checkout(
    tmp_path, ppy_home, monkeypatch
) -> None:
    """The backend incident: registered from a checkout on a feature branch."""
    forge = _forge_with_a_feature_branch(tmp_path)
    fake_forge(monkeypatch, GITHUB_URL, forge)
    checkout = _checkout_of(forge, tmp_path / "papaya-infra", branch="feature/x", origin=GITHUB_URL)

    added = repos.add_repo(checkout)

    assert added.forge_url == GITHUB_URL
    assert added.default_branch == "main"
    assert repos.list_repos()[0]["default_branch"] == "main"
    # The clone's origin is the forge itself, and it sits on the forge's HEAD.
    assert repos.remote_url(added.local_path) == GITHUB_URL
    assert repos.remote_url(added.local_path, repos.FORGE_REMOTE) is None
    assert repos.upstream_remote(repos.list_repos()[0]) == "origin"
    assert _git(added.local_path, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert added.base_sha == _git(forge, "rev-parse", "main")
    (note,) = added.notes
    assert "feature/x" in note and "main" in note


@pytest.mark.strict_forge
def test_add_from_a_path_with_a_local_origin_refuses_until_a_forge_is_named(
    tmp_path, ppy_home, monkeypatch
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

    fake_forge(monkeypatch, GITHUB_URL, upstream)
    added = repos.add_repo(source, forge_url=GITHUB_URL)
    assert added.forge_url == GITHUB_URL
    assert repos.remote_url(added.local_path) == GITHUB_URL


@pytest.mark.strict_forge
def test_add_from_a_path_with_no_origin_at_all_refuses(tmp_path, ppy_home) -> None:
    source = _make_source_repo(tmp_path / "source")
    with pytest.raises(repos.RepoError) as exc:
        repos.add_repo(source)
    assert "no origin remote" in str(exc.value)


@pytest.mark.strict_forge
def test_add_from_a_url_records_that_url_as_the_forge(tmp_path, ppy_home, monkeypatch) -> None:
    """A repo registered by URL is its own forge."""
    source = _make_source_repo(tmp_path / "source")
    fake_forge(monkeypatch, GITHUB_URL, source)
    added = repos.add_repo(GITHUB_URL)
    assert added.name == "papaya-infra"
    assert added.forge_url == GITHUB_URL
    assert added.default_branch == "main"
    assert added.notes == []


@pytest.mark.strict_forge
def test_repo_list_and_doctor_surface_a_repo_with_no_forge(
    tmp_path, ppy_home, capsys, monkeypatch, machine
) -> None:
    from papaya_agent_runtime import readiness
    from papaya_agent_runtime.cli import main
    from papaya_agent_runtime.setup.doctor import render_text

    source = _make_source_repo(tmp_path / "source")
    fake_forge(monkeypatch, GITHUB_URL, source)
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

    # With no forge there is nothing to rewrite and nothing to block: a local origin
    # stays, sync still works from it, the start remedy is silent, readiness only warns.
    clone = repos.list_repos()[0]["local_path"]
    _git(clone, "remote", "set-url", "origin", source)
    assert main(["repo", "sync", "source"]) == 0
    assert repos.remote_url(clone) == source
    assert repos.keep_base_clones_right() == []
    machine.answers[("git", "-C", clone, "config", "--get", "remote.origin.url")] = source
    codes = {p.code for p in readiness.check().problems}
    assert "repo_without_forge" in codes
    assert readiness.REPO_ORIGIN_IS_LOCAL not in codes


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
# The base branch and the fetch source are the forge's (2026-09-16)
# --------------------------------------------------------------------------- #


def _wrong_default_branch(tmp_path) -> tuple[str, repos.AddedRepo]:
    """A clone registered back when the checkout's branch became the default."""
    from papaya_agent_runtime.state import init_db, store

    forge = _forge_with_a_feature_branch(tmp_path)
    added = repos.add_repo(forge)
    _git(added.local_path, "checkout", "-q", "-b", "feature/x", "--track", "origin/feature/x")
    store.update_repo_fields(init_db(), added.name, default_branch="feature/x")
    return forge, added


def test_sync_corrects_a_wrong_stored_default_branch_and_logs_it(
    tmp_path, ppy_home, caplog, capsys
) -> None:
    from papaya_agent_runtime.cli import main

    forge, added = _wrong_default_branch(tmp_path)

    with caplog.at_level("INFO", logger="papaya_agent_runtime.repos"):
        assert main(["repo", "sync", added.name]) == 0

    row = repos.list_repos()[0]
    assert row["default_branch"] == "main"
    assert row["base_sha"] == _git(forge, "rev-parse", "main")
    assert _git(added.local_path, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    line = "default branch was feature/x; it is now main, the forge's HEAD"
    assert line in capsys.readouterr().out
    assert any(line in record.getMessage() for record in caplog.records)


def test_a_pinned_default_branch_is_kept_over_the_forge_until_unpinned(
    tmp_path, ppy_home, capsys
) -> None:
    from papaya_agent_runtime.cli import main

    _forge, added = _wrong_default_branch(tmp_path)
    assert main(["repo", "set", added.name, "--default-branch", "feature/x"]) == 0
    assert "pinned to feature/x" in capsys.readouterr().out

    res = repos.sync_repo(added.name)
    assert res.default_branch == "feature/x"
    assert res.default_branch_was is None
    assert repos.keep_base_clones_right() == []

    assert main(["repo", "set", added.name, "--default-branch", ""]) == 0
    assert repos.list_repos()[0]["default_branch"] == "main"


@pytest.mark.strict_forge
def test_sync_and_the_start_remedy_make_a_local_path_origin_the_forge(
    tmp_path, ppy_home, monkeypatch
) -> None:
    """The backend clone fetched from Shane's checkout; `set-head -a` followed their branch."""
    forge = _forge_with_a_feature_branch(tmp_path)
    fake_forge(monkeypatch, GITHUB_URL, forge)
    added = repos.add_repo(GITHUB_URL)
    checkout = _checkout_of(forge, tmp_path / "checkout", branch="feature/x", origin=GITHUB_URL)
    _git(added.local_path, "remote", "set-url", "origin", checkout)

    (line,) = repos.keep_base_clones_right()

    assert line.startswith("repaired papaya-infra's base clone: ")
    assert f"origin was the local path {checkout}" in line
    assert repos.remote_url(added.local_path) == GITHUB_URL
    assert _git(added.local_path, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert repos.keep_base_clones_right() == []


@pytest.mark.strict_forge
def test_a_local_path_origin_the_forge_cannot_repair_is_refused_and_is_a_blocker(
    tmp_path, ppy_home, monkeypatch, machine
) -> None:
    from papaya_agent_runtime import readiness

    forge = _make_source_repo(tmp_path / "forge")
    fake_forge(monkeypatch, GITHUB_URL, forge)
    added = repos.add_repo(GITHUB_URL)
    checkout = str(tmp_path / "checkout")
    _git(added.local_path, "remote", "set-url", "origin", checkout)
    fake_forge(monkeypatch, GITHUB_URL, str(tmp_path / "nowhere"))  # the forge is unreachable

    with pytest.raises(repos.RepoError, match="cannot be reached"):
        repos.sync_repo(added.name)
    assert repos.remote_url(added.local_path) == checkout
    (line,) = repos.keep_base_clones_right()
    assert line.startswith("could not put papaya-infra's base clone back on its forge")

    machine.answers[("git", "-C", added.local_path, "config", "--get", "remote.origin.url")] = (
        checkout
    )
    problem = next(
        p for p in readiness.check().problems if p.code == readiness.REPO_ORIGIN_IS_LOCAL
    )
    assert problem.repos == ("papaya-infra",)
    assert "ppy repo sync papaya-infra" in problem.steps
    assert readiness.setup_blocker(readiness.check(), "papaya-infra") is not None


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
    fake_forge(monkeypatch, GITHUB_URL, source)
    added = repos.add_repo(source, forge_url=GITHUB_URL)

    conn = init_db()
    run_id = store.create_run(conn, "deliver")
    repo_row = store.get_repo(conn, added.name)
    task_id = store.add_task(conn, run_id=run_id, title="ship it", repo_id=repo_row["id"])
    store.update_task_fields(conn, task_id, worktree_path=added.local_path, branch="ppy/task-1-abc")

    calls: list[list[str]] = []

    def fake_run(argv, cwd=None):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")  # no open PR yet
        return SimpleNamespace(returncode=0, stdout="https://github.com/pr/1\n", stderr="")

    monkeypatch.setattr(delivery, "_run", fake_run)
    monkeypatch.setattr(delivery, "is_approved_at_head", lambda tid: (True, ""))
    monkeypatch.setattr(delivery, "head_sha", lambda wt: "f" * 40)
    monkeypatch.setattr(delivery, "_pr_tool", lambda: "gh")

    delivery.deliver(task_id)
    push_argv = next(a for a in calls if a[:2] == ["git", "push"])
    assert push_argv[2] == "origin"  # the base clone's origin is the forge
    pr_argv = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    assert pr_argv[pr_argv.index("--repo") + 1] == "Papaya-HQ/papaya-infra"

    # An explicit remote still wins.
    calls.clear()
    delivery.deliver(task_id, remote="origin")
    push_argv = next(a for a in calls if a[:2] == ["git", "push"])
    assert push_argv[2] == "origin"


def _commit_file(repo_path, relative: str, text: str) -> None:
    target = repo_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo_path), "commit", "-qm", f"add {relative}"],
        check=True,
        capture_output=True,
    )


def test_locate_reports_hits_only_in_the_clone_that_contains_the_string(
    tmp_path, ppy_home, capsys
) -> None:
    """`ppy repo locate "hover card"` points at the code, and says nothing about the rest.

    Two registered clones, one of which renders a hover card. The locate reads the
    base clones the runtime owns, and finds the one that does — without being told
    which repository sounds like it should.
    """
    from papaya_agent_runtime.cli import main

    desktop = tmp_path / "desktop"
    _make_source_repo(desktop)
    _commit_file(desktop, "src/components/HoverCard.tsx", "export const label = 'Hover card';\n")
    _commit_file(desktop, "src/components/Profile.tsx", "// opens the hover card on focus\n")
    backend = tmp_path / "backend"
    _make_source_repo(backend)
    _commit_file(backend, "api/cards.py", "def card():\n    return 'hover'\n")
    repos.add_repo(str(desktop))
    repos.add_repo(str(backend))

    hits = {hit.repo: hit for hit in repos.locate(["hover card"])}

    assert hits["desktop"].found
    assert hits["desktop"].file_count == 2
    assert set(hits["desktop"].files) == {
        "src/components/HoverCard.tsx",
        "src/components/Profile.tsx",
    }
    # "hover" and "card" both occur in the backend, but never the phrase.
    assert not hits["backend"].found
    assert hits["backend"].files == []

    assert main(["repo", "locate", "hover card"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "desktop: 2 hit(s) in 2 file(s)"
    assert "backend: no hits" in out


def test_locate_with_several_terms_needs_every_term_in_the_same_file(tmp_path, ppy_home) -> None:
    source = tmp_path / "web"
    _make_source_repo(source)
    _commit_file(source, "a.txt", "tooltip only\n")
    _commit_file(source, "b.txt", "tooltip and clipping\n")
    repos.add_repo(str(source))

    (hit,) = repos.locate(["Tooltip", "clipping"])

    assert hit.files == ["b.txt"]


def test_locate_needs_a_term(ppy_home) -> None:
    with pytest.raises(repos.RepoError, match="at least one term"):
        repos.locate(["  "])
