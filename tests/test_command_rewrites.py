"""Every shape a worker actually gets refused on has ONE exact replacement.

Issue #121, 30 occurrences: workers keep running commands the rules refuse for
their shape, and the steer answered by reprinting the same rules. 16 of the 28
`command_shape` denials since 2026-09-18 are `cd <worktree> && …`, which the
rules never actually addressed — they say "`cd` is its own call", and never said
the shell already starts in the worktree, so "run it without the cd" was not
derivable from the text.

The first delivery of this work answered with three generic rows per refused
command (`cd <your worktree> && <command>`, `cd <worktree>/<subdir> && git
<args>`, …). That is still a rule, not a command. `rewrite_for` now builds the
actual replacement from the refused text and the worktree, so a worker refused
`cd /wt/backend && git status` is handed `git -C backend status`.

Every command tested here is a real refused one, copied from the live state
database on 2026-09-20 (abridged where a commit message ran to forty lines), and
each is asserted against the single exact string the worker is now sent.
"""

from __future__ import annotations

import pytest

from papaya_agent_runtime import tool_learning
from papaya_agent_runtime.providers.command_rules import (
    command_rules,
    rewrite_for,
    rewrite_table,
    rewrites_for,
)


@pytest.fixture
def wt(tmp_path):
    """A worktree with the subdirectories the real commands name."""
    for sub in ("backend/app", "services/credential-broker", "frontend/src"):
        (tmp_path / sub).mkdir(parents=True)
    return tmp_path


def test_a_cd_into_the_worktree_itself_becomes_the_bare_command(wt) -> None:
    found = rewrite_for(f"cd {wt} && git status --short", str(wt))

    assert found is not None
    assert found.instead == "`git status --short`, on its own — your shell starts there"
    assert found.runnable is True


def test_a_cd_into_a_subdirectory_before_git_becomes_git_dash_C(wt) -> None:
    found = rewrite_for(f"cd {wt}/backend && git status --short", str(wt))

    assert found is not None
    assert found.instead == "`git -C backend status --short`"


def test_a_cd_into_a_subdirectory_uses_that_programs_own_directory_flag(wt) -> None:
    found = rewrite_for(
        f"cd {wt}/services/credential-broker && uv run --extra dev python -c 'import x'", str(wt)
    )

    assert found is not None
    assert found.instead == (
        "`uv run --directory services/credential-broker run --extra dev python -c 'import x'`"
    )


def test_a_cd_to_somewhere_outside_the_worktree_says_it_cannot_be_reached(wt) -> None:
    """Not a rewrite: there is no command that reaches it, and saying so is the answer."""
    found = rewrite_for("cd /Users/someone/elsewhere && ls", str(wt))

    assert found is not None
    assert found.runnable is False
    assert "outside your worktree" in found.instead
    assert "Flagged, not done" in found.instead


def test_git_editor_gets_the_real_alternative_not_a_capability_request(wt) -> None:
    """`GIT_EDITOR=true git rebase --continue` is not a missing PROGRAM.

    `ppy need` asks for a program, and `git` is already allowed. git's own `-c`
    sets the same thing for one call.
    """
    found = rewrite_for("GIT_EDITOR=true git rebase --continue", str(wt))

    assert found is not None
    assert found.instead == "`git -c core.editor=true rebase --continue`"
    assert found.runnable is True
    assert "ppy need" not in found.instead


def test_the_cache_variables_are_already_set_so_the_rewrite_says_so(wt) -> None:
    found = rewrite_for(
        "UV_CACHE_DIR=/tmp/uv RUFF_CACHE_DIR=/tmp/ruff uv run ruff check .", str(wt)
    )

    assert found is not None
    assert found.instead.startswith("`uv run ruff check .`")
    assert "already set in your environment" in found.instead


def test_an_inline_variable_with_no_alternative_says_there_is_none(wt) -> None:
    found = rewrite_for("TEST_RUNNER_SNAPSHOTS=/tmp/out xcodebuild test", str(wt))

    assert found is not None
    assert found.runnable is False
    assert "nothing sets an environment variable for one call here" in found.instead


@pytest.mark.parametrize(
    ("command", "instead"),
    [
        (
            "make ios-test 2>&1 | tee /tmp/ios-test-output.txt | tail -80",
            "`ppy gate run --task <task id>` when `make ios-test` is your gate — it "
            "records the output for you; otherwise run it and read what it printed",
        ),
        ("cat notes.md | head -200", "the Read tool on `notes.md`"),
        (
            "uv run pytest -q tests/test_x.py > /tmp/out.txt",
            "run `uv run pytest -q tests/test_x.py`, read its output, and write "
            "`/tmp/out.txt` with the file-writing tool",
        ),
        (
            "cat >> backend/tests/test_x.py <<'EOF'\nbody\nEOF",
            "write `backend/tests/test_x.py` with the file-writing tool (or the "
            "file-editing tool to append)",
        ),
        (
            "for i in 1 2 3; do uv run pytest -q tests/test_dm.py; done",
            "`uv run pytest -q tests/test_dm.py`, once per value — 3 separate calls",
        ),
        (
            'git commit -m "$(cat .ppy-evidence/msg.txt)"',
            "`git commit -F .ppy-evidence/msg.txt`",
        ),
        (
            'ppy progress 164 --phase done --note "a very long note"',
            "write the text with the file-writing tool, then "
            "`ppy progress 164 --phase done --note-file <path>`",
        ),
    ],
    ids=["tee", "head", "redirect", "heredoc", "loop", "commit", "note"],
)
def test_each_real_refused_shape_gets_one_exact_command(command, instead, wt) -> None:
    found = rewrite_for(command, str(wt))

    assert found is not None, f"no rewrite for {command!r}"
    assert found.instead == instead


def test_a_grep_pipe_names_the_file_and_the_pattern(wt) -> None:
    found = rewrite_for('cat notes.md | grep -n "UV_CACHE_DIR"', str(wt))

    assert found is not None
    assert found.instead == """the Grep tool for "UV_CACHE_DIR" in `notes.md`"""


def test_a_command_that_is_not_a_known_shape_gets_no_rewrite(wt) -> None:
    assert rewrite_for("terraform plan", str(wt)) is None
    assert rewrites_for(["terraform plan"], str(wt)) == []


# ── what the worker is actually sent ────────────────────────────────────────


def test_the_steer_carries_the_rewritten_command_not_the_rule_again(wt) -> None:
    commands = [f"cd {wt}/backend && git status --short", "cat notes.md | head -200"]

    steer = tool_learning.shape_steer_message(commands, "ppy/task-1-abc", str(wt))

    assert "`git -C backend status --short`" in steer
    assert "the Read tool on `notes.md`" in steer
    assert "your shell already starts in your worktree" in steer
    # The rules still follow the replacement, rather than standing in for it.
    assert steer.index("Run this instead") < steer.index("held to these rules")


def test_the_steer_names_only_what_was_refused(wt) -> None:
    steer = tool_learning.shape_steer_message([f"cd {wt}/backend && git status"], "b", str(wt))
    targeted = steer[steer.index("Run this instead") : steer.index("held to these rules")]

    assert "`git -C backend status`" in targeted
    assert "once per value" not in targeted
    assert "git commit -F" not in targeted


def test_a_shape_with_no_rewrite_is_still_named_in_the_steer(wt) -> None:
    """Never silently dropped: the worker has to know it cannot just retry."""
    refused = "git checkout `git rev-parse HEAD~1`"

    steer = tool_learning.shape_steer_message([refused], "b", str(wt))

    assert rewrite_for(refused, str(wt)) is None
    assert "No single replacement fits these" in steer
    assert refused in steer


def test_the_rules_say_the_shell_already_starts_in_the_worktree() -> None:
    """The one fact 16 of 28 shape denials turn on, and the rules never said it."""
    rules = command_rules("claude", "ppy/task-1-abc")

    assert "Your shell already starts in your worktree" in rules
    assert "git -C <subdir>" in rules
    assert "your shell already starts there" in rewrite_table()


def test_no_rewrite_tells_a_worker_to_widen_its_profile_or_skip_a_hook(wt) -> None:
    """Every replacement has to be something the worker can run today."""
    said = rewrite_table().lower()
    for command in (
        f"cd {wt} && git status",
        "GIT_EDITOR=true git rebase --continue",
        "cat notes.md | head -200",
    ):
        found = rewrite_for(command, str(wt))
        assert found is not None
        said += " " + found.instead.lower()

    assert "--no-verify" not in said
    assert "sudo" not in said
    assert "ppy config claude --allow" not in said
