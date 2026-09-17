"""The build is read from git, and the next release is computed once, in Python.

Two things are being held here, and they are the same thing from opposite ends.
`.github/workflows/release.yml` tags every merge to `main` with the next patch, and
the runtime reads that tag back out of the checkout to say what build it is — so the
tests that matter are the ones where the two could disagree: a tag sorted as a string
instead of a number, a tag on a commit two behind HEAD, a working tree with edits in
it, a checkout with no tags at all, and a git that is missing, hung, or pointed
somewhere else entirely.

None of it may raise, and none of it may be slow: `ppy capabilities --json` is read
at connect time by a client that gives the answer ten seconds, and the string it
prints is what Papaya Desktop shows on the machine card, verbatim.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import make_git_repo, scale
from papaya_agent_runtime import capabilities, cli
from papaya_agent_runtime import version as version_module

ROOT = Path(__file__).resolve().parents[1]

SHA7 = r"[0-9a-f]{7}"


class Repo:
    """A temporary git repository, tagged and dirtied to order."""

    def __init__(self, path: Path, *, empty: bool = False) -> None:
        self.path = path
        if empty:
            path.mkdir(parents=True, exist_ok=True)
            self.git("init", "-q", "-b", "main")
        else:
            make_git_repo(path)

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()

    def commit(self, message: str) -> None:
        (self.path / "file.txt").write_text(f"{message}\n")
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def tag(self, name: str) -> None:
        self.git("tag", "-a", name, "-m", name)

    def soil(self) -> None:
        """Leave an uncommitted edit to a tracked file — git's own "dirty"."""
        (self.path / "README.md").write_text("# edited, not committed\n")

    def litter(self) -> None:
        """Leave an untracked file, which is not "dirty" to git and must not be here."""
        (self.path / "scratch.txt").write_text("not tracked\n")

    def head(self) -> str:
        return self.git("rev-parse", "--short=7", "HEAD")

    def derive(self) -> str:
        return version_module.derive(str(self.path))


@pytest.fixture
def repo(tmp_path) -> Repo:
    return Repo(tmp_path / "repo")


# ── the version of a checkout (G2) ──────────────────────────────────────────


def test_the_exact_tag_on_a_clean_tree_is_the_bare_release(repo) -> None:
    """The one case that gets to look like a release, because it is one."""
    repo.tag("v0.1.3")

    assert repo.derive() == "0.1.3"


def test_commits_past_the_tag_are_a_post_release_naming_the_commit(repo) -> None:
    """`0.1.3.post2+g<sha7>`: sorts after 0.1.3, and says which build it is."""
    repo.tag("v0.1.3")
    repo.commit("two")
    repo.commit("three")

    assert repo.derive() == f"0.1.3.post2+g{repo.head()}"


def test_a_dirty_tagged_tree_puts_the_dirt_in_the_local_segment(repo) -> None:
    """Not `0.1.3+dirty`, and emphatically not `0.1.3`.

    PEP 440 has nowhere in the *public* part of a version to say "and some edits",
    so `0.1.3+dirty` would claim to be release 0.1.3 — the one thing a modified
    checkout is not. The commit and the dirt share the local segment instead, so
    the string still sorts as 0.1.3 while telling anyone reading it that this is
    not the release.
    """
    repo.tag("v0.1.3")
    repo.soil()

    assert repo.derive() == f"0.1.3+g{repo.head()}.dirty"


def test_a_dirty_tree_past_a_tag_carries_both_the_distance_and_the_dirt(repo) -> None:
    repo.tag("v0.1.3")
    repo.commit("two")
    repo.soil()

    assert repo.derive() == f"0.1.3.post1+g{repo.head()}.dirty"


def test_an_untracked_file_beside_the_source_is_not_a_different_build(repo) -> None:
    """git's own meaning of dirty, and the one anybody would want.

    Every worker in this repository runs in a worktree with a scratch directory
    beside the source. A version that changed because of one would make
    `takeover`'s build id flicker, and flickering there retires a live supervisor.
    """
    repo.tag("v0.1.3")
    repo.litter()

    assert repo.derive() == "0.1.3"


def test_a_checkout_with_no_release_tag_still_names_its_commit(repo) -> None:
    """`0.0.0+g<sha7>` — which is exactly what this repository was until today."""
    assert repo.derive() == f"0.0.0+g{repo.head()}"

    repo.soil()
    assert repo.derive() == f"0.0.0+g{repo.head()}.dirty"


def test_a_tag_that_is_not_a_release_tag_is_not_read_as_one(repo) -> None:
    """`desktop-v1` belongs to another product in the same namespace."""
    repo.git("tag", "desktop-v1")
    repo.git("tag", "nightly")

    assert repo.derive() == f"0.0.0+g{repo.head()}"


def test_a_tag_the_glob_allows_but_the_strict_form_rejects_degrades_to_the_commit(repo) -> None:
    """git's `--match` is fnmatch, so it cannot express "exactly three numbers".

    `v0.1.3-rc1` gets past the glob and is then refused by the regex that actually
    decides. The answer degrades to naming the commit rather than to nothing, and
    above all does not claim to be release 0.1.3.
    """
    repo.tag("v0.1.3-rc1")

    assert repo.derive() == f"0.0.0+g{repo.head()}"


def test_a_linked_worktree_resolves_like_any_other_checkout(repo, tmp_path) -> None:
    """`.git` is a pointer file in a worktree, and every worker here runs in one."""
    repo.tag("v0.2.0")
    worktree = tmp_path / "linked"
    repo.git("worktree", "add", "-q", "-b", "probe", str(worktree))

    assert (worktree / ".git").is_file(), "this is not testing the pointer-file case"
    assert version_module.derive(str(worktree)) == "0.2.0"


def test_a_directory_that_is_not_a_checkout_is_a_flat_zero(tmp_path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    assert version_module.derive(str(plain)) == "0.0.0"


def test_a_repository_with_no_commits_is_a_flat_zero(tmp_path) -> None:
    """There is no commit to name, so there is nothing to put in the local segment."""
    empty = Repo(tmp_path / "empty", empty=True)

    assert empty.derive() == "0.0.0"


def test_no_git_on_the_machine_is_a_flat_zero_and_does_not_raise(repo, monkeypatch) -> None:
    def no_git(argv, timeout):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(version_module, "_run", no_git)

    assert repo.derive() == "0.0.0"


def test_a_describe_that_hangs_still_names_the_commit(repo, monkeypatch) -> None:
    """The reason there are two invocations rather than one.

    `git describe --dirty` walks the working tree, so a large or cold checkout can
    exceed its deadline where `git rev-parse HEAD` — one ref, no tree — cannot. A
    flat `0.0.0` is reserved for having nothing to say; a slow git still knows
    which commit this is.
    """
    real = version_module._run
    calls: list[list[str]] = []

    def slow_describe(argv, timeout):
        calls.append(list(argv))
        if "describe" in argv:
            raise subprocess.TimeoutExpired(argv, timeout)
        return real(argv, timeout)

    monkeypatch.setattr(version_module, "_run", slow_describe)
    repo.tag("v0.1.3")

    assert repo.derive() == f"0.0.0+g{repo.head()}"
    # ["git", "-C", <root>, <subcommand>, ...]
    assert [args[3] for args in calls] == ["describe", "rev-parse"]


def test_both_invocations_failing_is_a_flat_zero(repo, monkeypatch) -> None:
    def everything_hangs(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(version_module, "_run", everything_hangs)

    assert repo.derive() == "0.0.0"


def test_the_two_timeouts_are_the_ones_the_workflow_and_the_probe_can_afford() -> None:
    """Not a latency budget: the point a hung git stops being worth waiting for.

    Both together stay far inside the ten seconds the Papaya client gives its
    connect-time probe, which is the deadline that actually exists.
    """
    budget = version_module.DESCRIBE_TIMEOUT_SECONDS + version_module.REV_PARSE_TIMEOUT_SECONDS
    assert budget < 10.0


def test_a_git_hook_environment_cannot_redirect_the_answer(repo, tmp_path, monkeypatch) -> None:
    """`GIT_DIR` is set inside every git hook, and points at somebody else's repo.

    A runtime that answered with the version of whatever repository happened to
    invoke it would be wrong in the least visible way possible.
    """
    other = Repo(tmp_path / "other")
    other.tag("v9.9.9")
    repo.tag("v0.1.3")
    monkeypatch.setenv("GIT_DIR", str(other.path / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other.path))

    assert repo.derive() == "0.1.3"


def test_it_answers_far_inside_the_clients_connect_time_probe() -> None:
    """Measured on this checkout, which is a real repository with real history."""
    started = time.monotonic()
    version_module.derive(str(ROOT))
    elapsed = time.monotonic() - started

    assert elapsed < scale(3.0), f"deriving this checkout's version took {elapsed:.2f}s"


def test_it_reads_local_state_only(repo, monkeypatch) -> None:
    """No network, no database, no client: it is read before any of those exist."""
    seen: list[list[str]] = []
    real = version_module._run

    def record(argv, timeout):
        seen.append(list(argv))
        return real(argv, timeout)

    monkeypatch.setattr(version_module, "_run", record)
    repo.derive()

    assert seen and all(argv[0] == "git" for argv in seen)
    assert all("--no-tags" not in argv for argv in seen)
    for argv in seen:
        assert "://" not in " ".join(argv), "a version read reached for a remote"


# ── remembering it, but not remembering a failure ───────────────────────────


def test_the_answer_is_computed_once_per_process(repo, monkeypatch) -> None:
    version_module.cache_clear()
    calls = []
    real = version_module._run

    def count(argv, timeout):
        calls.append(argv)
        return real(argv, timeout)

    monkeypatch.setattr(version_module, "_run", count)
    repo.tag("v0.4.0")

    assert version_module.version(str(repo.path)) == "0.4.0"
    before = len(calls)
    assert version_module.version(str(repo.path)) == "0.4.0"
    assert len(calls) == before, "the second read started a subprocess"

    version_module.cache_clear()


def test_a_fallback_is_never_remembered(tmp_path, monkeypatch) -> None:
    """A loaded moment must not cost a process its version for the rest of its life.

    `0.0.0` can mean "git was busy for two seconds". Caching that would reproduce
    the exact bug this module exists to remove, only intermittently.
    """
    version_module.cache_clear()
    repo = Repo(tmp_path / "late")
    repo.tag("v0.5.0")
    hung = True

    real = version_module._run

    def sometimes(argv, timeout):
        if hung:
            raise subprocess.TimeoutExpired(argv, timeout)
        return real(argv, timeout)

    monkeypatch.setattr(version_module, "_run", sometimes)
    assert version_module.version(str(repo.path)) == "0.0.0"

    hung = False
    assert version_module.version(str(repo.path)) == "0.5.0"

    version_module.cache_clear()


# ── the next release (G1) ───────────────────────────────────────────────────


def test_a_repository_that_has_never_been_released_starts_at_the_first_release() -> None:
    """Not `0.0.1`: `0.0.x` reads as "no version yet", which is what we are leaving."""
    assert version_module.next_version([]) == "0.1.0"
    assert version_module.next_tag([]) == "v0.1.0"


def test_the_patch_is_bumped_by_one() -> None:
    assert version_module.next_version(["v0.1.4"]) == "0.1.5"
    assert version_module.next_tag(["v0.1.0", "v0.1.4"]) == "v0.1.5"


def test_tags_are_ordered_numerically_and_not_as_strings() -> None:
    """`git tag` sorts `v0.1.10` before `v0.1.9`, which would release backwards.

    This is the whole reason the workflow calls Python instead of piping `git tag`
    through `sort | tail -1`.
    """
    tags = ["v0.1.9", "v0.1.10", "v0.1.3"]

    assert sorted(tags) == ["v0.1.10", "v0.1.3", "v0.1.9"], "the lexical trap moved"
    assert version_module.newest_release(tags) == (0, 1, 10)
    assert version_module.next_version(tags) == "0.1.11"


def test_a_minor_cut_by_hand_is_what_the_next_patch_follows() -> None:
    """Minors and majors are `git tag` by a person; the workflow only ever adds patches."""
    assert version_module.next_version(["v0.1.9", "v0.2.0"]) == "0.2.1"
    assert version_module.next_version(["v0.9.1", "v1.0.0"]) == "1.0.1"


def test_tags_belonging_to_something_else_are_ignored() -> None:
    assert version_module.newest_release(["desktop-v1", "nightly", "release-2"]) is None
    assert version_module.next_version(["desktop-v1", "v0.1.4"]) == "0.1.5"
    assert version_module.next_version(["desktop-v9"]) == "0.1.0"


def test_only_three_numbers_after_a_v_is_a_release_tag() -> None:
    assert version_module.parse_tag("v1.2.3") == (1, 2, 3)
    assert version_module.parse_tag("v10.20.30") == (10, 20, 30)
    for other in ("v0.2", "v1.2.3.4", "v1.0.0-rc1", "1.2.3", "desktop-v1", "vx.y.z", ""):
        assert version_module.parse_tag(other) is None, other


def test_a_commit_that_is_already_tagged_has_nothing_to_do(repo) -> None:
    """The workflow's idempotence, and the reason a re-run is green and silent."""
    repo.tag("v0.1.4")

    assert version_module.release_tag_to_create(str(repo.path)) is None


def test_an_untagged_commit_is_given_the_next_tag(repo) -> None:
    repo.tag("v0.1.9")
    repo.tag("v0.1.10")
    repo.commit("merged a pull request")

    assert version_module.release_tag_to_create(str(repo.path)) == "v0.1.11"


def test_a_tag_on_head_that_is_not_a_release_tag_does_not_count_as_released(repo) -> None:
    repo.git("tag", "desktop-v1")

    assert version_module.release_tag_to_create(str(repo.path)) == "v0.1.0"


def test_the_release_path_refuses_to_guess_when_git_cannot_be_read(repo, monkeypatch) -> None:
    """A shallow clone has no tags, and so does a repository that was never released.

    `derive` may fall back; this may not. Answering `v0.1.0` to a git that could
    not be read would re-release the first version over an existing history.
    """
    monkeypatch.setattr(version_module, "_run", _refusing_git)

    with pytest.raises(version_module.GitUnreadable):
        version_module.release_tag_to_create(str(repo.path))


def _refusing_git(argv, timeout):
    raise subprocess.TimeoutExpired(argv, timeout)


# ── the command line the workflow calls ─────────────────────────────────────


@pytest.fixture
def release_checkout(tmp_path) -> Repo:
    """A repository holding this package, the way the workflow's checkout does.

    The command answers about *its own* checkout — the one the module was imported
    from — because that is the only question the runtime and the release job both
    ask. So exercising it means putting the module in a repository and tagging
    that, which has a second virtue: it only works if `version.py` imports nothing
    from its siblings, which is what the bare-interpreter path depends on.
    """
    repo = Repo(tmp_path / "checkout")
    package = repo.path / "src" / "papaya_agent_runtime"
    package.mkdir(parents=True)
    for name in ("__init__.py", "version.py"):
        shutil.copy2(ROOT / "src" / "papaya_agent_runtime" / name, package / name)
    repo.git("add", "-A")
    repo.git("commit", "-qm", "the runtime, as the release job checks it out")
    return repo


def _module_run(*args: str, cwd: Path, expect: int = 0) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "papaya_agent_runtime.version", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env={**os.environ, "PYTHONPATH": str(cwd / "src")},
        timeout=scale(60),
        check=False,
    )
    assert proc.returncode == expect, proc.stderr
    return proc.stdout


def test_next_prints_the_tag_to_create(release_checkout) -> None:
    release_checkout.tag("v0.1.4")
    release_checkout.commit("merged a pull request")

    assert _module_run("--next", cwd=release_checkout.path).strip() == "v0.1.5"


def test_next_prints_nothing_and_exits_green_when_head_is_already_tagged(release_checkout) -> None:
    """ "Already released" is a success, not a failure: the workflow reads the line
    and stops. An exit code would make every re-run of a push look broken."""
    release_checkout.tag("v0.1.4")

    assert _module_run("--next", cwd=release_checkout.path).strip() == ""


def test_next_exits_two_when_there_is_no_git_to_read(release_checkout) -> None:
    """The package is there, the repository is not: a broken checkout, not a release."""
    shutil.rmtree(release_checkout.path / ".git")

    assert _module_run("--next", cwd=release_checkout.path, expect=2) == ""


def test_the_module_run_bare_prints_the_version_of_the_checkout_it_is_in(
    release_checkout,
) -> None:
    release_checkout.tag("v0.7.2")

    assert _module_run(cwd=release_checkout.path).strip() == "0.7.2"


def test_the_workflows_two_steps_agree_on_one_release(release_checkout) -> None:
    """What the release job actually does, end to end, minus the network.

    `--next` names the tag, the tag is created, and the runtime then reports
    exactly that release — the check the workflow runs before it publishes,
    because `ppy capabilities --json` feeds the desktop machine card verbatim and
    a derivation that drifted from the tag would be visible to users first.
    """
    release_checkout.tag("v0.1.9")
    release_checkout.tag("v0.1.10")
    release_checkout.commit("merged a pull request")

    tag = _module_run("--next", cwd=release_checkout.path).strip()
    assert tag == "v0.1.11"

    release_checkout.tag(tag)
    assert _module_run(cwd=release_checkout.path).strip() == tag.removeprefix("v")
    assert _module_run("--next", cwd=release_checkout.path).strip() == ""


def test_the_workflow_calls_the_package_rather_than_reimplementing_the_sort() -> None:
    """If the sort ever moves into shell, `v0.1.10` will be released as `v0.1.9`."""
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()

    assert "python3 -m papaya_agent_runtime.version --next" in workflow
    steps = workflow.split("\n    - ")[1:]
    shell = "\n".join(
        line for step in steps for line in step.splitlines() if not line.strip().startswith("#")
    )
    assert "git tag --points-at" not in shell, "the idempotence check belongs in the package"
    assert "sort" not in shell, "the version sort must not be reimplemented in shell"
    assert "git describe" not in shell, "the derivation belongs in the package"


def test_the_workflow_is_the_shape_the_release_process_needs() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()

    assert "branches: [main]" in workflow
    assert "workflow_dispatch:" in workflow
    assert "contents: write" in workflow
    assert "fetch-depth: 0" in workflow and "fetch-tags: true" in workflow
    assert "git tag -a" in workflow and "git push origin" in workflow
    assert "gh release create" in workflow and "--generate-notes" in workflow
    assert "secrets.GITHUB_TOKEN" in workflow
    # Nothing here may reach for the project environment: `version.py` is standard
    # library only so this job needs no install at all.
    assert "uv " not in workflow


def test_the_idempotence_check_reads_the_release_tags_on_head(repo) -> None:
    """Named in the brief as the workflow's check; it lives in the package instead."""
    repo.tag("v0.1.4")
    repo.git("tag", "desktop-v1")

    assert version_module.tags_at_head(str(repo.path)) == ["v0.1.4"]

    repo.commit("after the release")
    assert version_module.tags_at_head(str(repo.path)) == []


# ── everything that reports a version reports the same one (G2) ─────────────


def test_capabilities_version_is_the_derived_version_not_a_literal(capsys) -> None:
    """The string Papaya Desktop prints on the machine card, verbatim."""
    assert cli.main(["capabilities", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert data["version"] == version_module.version()
    assert re.fullmatch(rf"\d+\.\d+\.\d+(\.post\d+)?(\+g{SHA7}(\.dirty)?)?", data["version"]), data[
        "version"
    ]


def test_the_package_the_cli_and_capabilities_cannot_disagree(capsys) -> None:
    import papaya_agent_runtime

    assert cli.main(["version"]) == 0
    printed = capsys.readouterr().out.strip()

    assert printed == f"papaya-agent-runtime {papaya_agent_runtime.__version__}"
    assert papaya_agent_runtime.__version__ == capabilities.collect()["version"]
    assert papaya_agent_runtime.__version__ == version_module.version()


def test_the_package_version_is_derived_lazily_and_is_still_importable(tmp_path) -> None:
    """`from papaya_agent_runtime import __version__` still works through `__getattr__`.

    Lazy because importing this package costs nothing today and every `ppy`
    command imports it; a module-level assignment would spend a `git describe` on
    a number most commands never read.
    """
    import papaya_agent_runtime

    assert "__version__" not in vars(papaya_agent_runtime)
    assert "__version__" in dir(papaya_agent_runtime)
    from papaya_agent_runtime import __version__

    assert __version__ == version_module.version()
    with pytest.raises(AttributeError):
        getattr(papaya_agent_runtime, "__nonesuch__")  # noqa: B009 - the point is the raise


def test_the_bare_interpreter_path_reports_the_same_version(tmp_path) -> None:
    """`bin/ppy` answers `capabilities` from the standard library on an unsynced
    checkout, and the desktop reads that answer. It must not be a different number.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "papaya_agent_runtime.capabilities", "--json"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "PPY_HOME": str(tmp_path / ".ppy"),
        },
        timeout=scale(60),
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert json.loads(proc.stdout)["version"] == version_module.version()


def test_pyproject_no_longer_states_a_version_of_its_own() -> None:
    """One source of truth, and it is the tag.

    `[tool.uv] package = false` makes this a virtual project, so `dynamic` needs
    no build backend: uv records no version for the root in `uv.lock` and
    `uv sync --frozen` — what CI runs — resolves exactly as it did before.
    """
    pyproject = (ROOT / "pyproject.toml").read_text()

    assert 'dynamic = ["version"]' in pyproject
    assert 'version = "0.0.0"' not in pyproject
    assert "package = false" in pyproject
