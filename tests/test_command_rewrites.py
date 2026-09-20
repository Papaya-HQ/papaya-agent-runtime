"""Every shape a worker actually gets refused on has a replacement in the refusal.

Issue #121, 30 occurrences: workers keep running commands the rules refuse for
their shape, and the steer answered by reprinting the same rules. 16 of the 28
`command_shape` denials since 2026-09-18 are `cd <worktree> && …`, which the
rules never actually address — they say "`cd` is its own call", and never say the
shell already starts in the worktree, so "run it without the cd" was not derivable
from the text.

`COMMANDS` below is the real refused commands, copied from the live state
database on 2026-09-20 (abridged where a commit message ran to forty lines). Each
must be matched by a rewrite row, and that row's replacement must be in the steer
the worker is sent.
"""

from __future__ import annotations

import pytest

from papaya_agent_runtime import tool_learning
from papaya_agent_runtime.providers.command_rules import (
    REWRITES,
    command_rules,
    rewrite_table,
    rewrites_for,
)

WT = "/Users/shanewolf/.treehouse/papaya-backend-monorepo-e5cc8b/1/papaya-backend-monorepo"

#: (the refused command, the rewrite row that must match it).
COMMANDS: list[tuple[str, str]] = [
    (f"cd {WT} && git status --short", "cd <worktree>/<subdir> && git <args>"),
    (
        f"cd {WT} && git add frontend/src/components/agentDm/connectYourCode.ts",
        "cd <worktree>/<subdir> && git <args>",
    ),
    (
        f"cd {WT}/services/credential-broker && uv run --extra dev python -c 'import x'",
        "cd <worktree>/<subdir> && <command>",
    ),
    (
        "make ios-test 2>&1 | tee /tmp/ios-test-output.txt | tail -80",
        "<command> 2>&1 | tee <file> | tail -<n>",
    ),
    (
        "cat /Users/shanewolf/notes.md 2>/dev/null | head -200",
        "<command> | head -<n>, | tail -<n>, | less",
    ),
    ("uv run pytest -q tests/test_x.py > /tmp/out.txt", "<command> > <file>, >> <file>, 2>&1"),
    (
        "cat >> backend/tests/test_agent_on_call_idle_db.py <<'EOF'\nx\nEOF",
        "cat >> <file> <<'EOF' … EOF",
    ),
    ("GIT_EDITOR=true git rebase --continue", "FOO=1 <command>"),
    ("UV_CACHE_DIR=/tmp/uv RUFF_CACHE_DIR=/tmp/ruff uv run ruff check .", "FOO=1 <command>"),
    (
        "for i in 1 2 3; do uv run pytest -q tests/test_dm.py; done",
        "for x in a b c; do <command>; done",
    ),
    ("git commit -m \"$(cat <<'EOF'\nmessage\nEOF\n)\"", 'git commit -m "$(cat <file>)"'),
    (
        'ppy progress 164 --phase done --note "$(cat .ppy-evidence/note.txt)"',
        'ppy progress <id> --note "<long text>"',
    ),
    ('env | grep -E "UV_CACHE_DIR|RUFF_CACHE_DIR"', "<command> | grep <pattern>"),
]


@pytest.mark.parametrize(("command", "expected"), COMMANDS, ids=[c[:40] for c, _ in COMMANDS])
def test_every_real_refused_command_gets_its_own_rewrite(command: str, expected: str) -> None:
    matched = rewrites_for([command])

    assert matched is not REWRITES, f"no rewrite row matched {command!r}"
    assert expected in [row[0] for row in matched]


def test_the_cd_rewrite_says_the_shell_already_starts_in_the_worktree() -> None:
    """The one fact 16 of 28 shape denials turn on, and the rules never said it."""
    rules = command_rules("claude", "ppy/task-1-abc")

    assert "Your shell already starts in your worktree" in rules
    assert "git -C <subdir>" in rules
    assert "your shell already starts there" in rewrite_table()


def test_the_steer_carries_the_rewritten_command_not_only_the_rule() -> None:
    commands = [f"cd {WT} && git status --short", "cat notes.md | head -200"]

    steer = tool_learning.shape_steer_message(commands, "ppy/task-1-abc")

    assert "git -C <subdir> <args>" in steer
    assert "the Read tool on the file" in steer
    assert "your shell already starts in your worktree" in steer
    # And the rules are still there, after the replacement rather than instead of it.
    assert steer.index("Run these instead") < steer.index("held to these rules")


def test_a_steer_names_only_the_shapes_that_apply() -> None:
    """A worker refused one shape is told about that shape first, not all thirteen.

    The rules that follow still carry the whole table — a worker reading them should
    see every replacement — so this is about the steer's own section, which is the
    part written for the command that was just refused.
    """
    steer = tool_learning.shape_steer_message([f"cd {WT} && git status --short"], "b")
    targeted = steer[steer.index("Run these instead") : steer.index("held to these rules")]

    assert "git -C <subdir> <args>" in targeted
    assert "one call per value — run it three times" not in targeted
    assert "git commit -F <file>" not in targeted


def test_a_shape_nothing_matches_still_gets_the_whole_table() -> None:
    assert rewrites_for(["some totally unfamiliar thing"]) is REWRITES


def test_no_rewrite_tells_a_worker_to_widen_its_profile_or_skip_a_hook() -> None:
    """Every replacement has to be something the worker can actually run today."""
    said = rewrite_table().lower()

    assert "--no-verify" not in said
    assert "sudo" not in said
    assert "ppy config claude --allow" not in said
