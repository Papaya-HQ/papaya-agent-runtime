"""Reading a target repository's Claude settings without trusting them.

`registered_hooks` runs at every dispatch and every resume
(`supervisor/core.py`), and it reads a file that belongs to somebody else's
repository. Its docstring promised it never raises, and it raised
`UnicodeDecodeError` on a non-UTF-8 settings file — which would have taken the
dispatch down, not the read.

It also has to read matchers the way Claude Code does: they are regular
expressions, so `Bash.*` is real and in use.
"""

from __future__ import annotations

import json

from papaya_agent_runtime import tool_learning
from papaya_agent_runtime.providers.claude import (
    MAX_MATCHER_CHARS,
    MAX_SETTINGS_BYTES,
    ClaudeAdapter,
    registered_hooks,
)
from test_hook_refusals import _hook_block, _result, _tool_use, worktree  # noqa: F401


def _settings(path, value) -> None:
    (path / ".claude").mkdir(exist_ok=True)
    (path / ".claude" / "settings.json").write_text(json.dumps(value), encoding="utf-8")


def _matcher(path, matcher: str, script: str = "./a.sh") -> None:
    _settings(
        path, {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [{"command": script}]}]}}
    )


# ── a file the runtime does not control ─────────────────────────────────────


def test_a_non_utf8_settings_file_never_raises(tmp_path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_bytes(b'{"hooks": "\xff\xfe binary"}')

    assert registered_hooks(str(tmp_path), "Bash") == []


def test_an_enormous_settings_file_is_not_read(tmp_path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        " " * (MAX_SETTINGS_BYTES + 10), encoding="utf-8"
    )

    assert registered_hooks(str(tmp_path), "Bash") == []


def test_a_directory_where_the_settings_file_should_be_never_raises(tmp_path) -> None:
    (tmp_path / ".claude" / "settings.json").mkdir(parents=True)

    assert registered_hooks(str(tmp_path), "Bash") == []


def test_malformed_json_never_raises(tmp_path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")

    assert registered_hooks(str(tmp_path), "Bash") == []


def test_settings_that_are_not_an_object_never_raise(tmp_path) -> None:
    _settings(tmp_path, ["not", "an", "object"])

    assert registered_hooks(str(tmp_path), "Bash") == []


# ── matchers ────────────────────────────────────────────────────────────────


def test_a_regex_matcher_is_read_the_way_claude_code_reads_it(tmp_path) -> None:
    _settings(
        tmp_path,
        {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash.*", "hooks": [{"command": "./a.sh"}]},
                    {"matcher": "Notebook.*", "hooks": [{"command": "./b.sh"}]},
                ]
            }
        },
    )

    assert [h["script"] for h in registered_hooks(str(tmp_path), "Bash")] == ["a.sh"]
    assert [h["script"] for h in registered_hooks(str(tmp_path), "NotebookEdit")] == ["b.sh"]


def test_a_matcher_that_is_not_a_valid_regex_is_ignored_not_raised(tmp_path) -> None:
    _matcher(tmp_path, "(Bash")

    assert registered_hooks(str(tmp_path), "Bash") == []


def test_a_matcher_regex_must_match_the_whole_tool_name(tmp_path) -> None:
    """`Ba` is not `Bash`: a partial match would make every hook match everything."""
    _matcher(tmp_path, "Ba")

    assert registered_hooks(str(tmp_path), "Bash") == []


def test_an_absurdly_long_matcher_is_not_run_as_a_regex(tmp_path) -> None:
    """A pattern this size is not a tool name, and could be a slow one."""
    _matcher(tmp_path, "(a+)+" * MAX_MATCHER_CHARS)

    assert registered_hooks(str(tmp_path), "Bash") == []


def test_a_plain_name_a_pipe_list_and_a_star_all_still_work(tmp_path) -> None:
    _settings(
        tmp_path,
        {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Write|Edit", "hooks": [{"command": "./a.sh"}]},
                    {"matcher": "*", "hooks": [{"command": "./b.sh"}]},
                    {"hooks": [{"command": "./c.sh"}]},
                    {"matcher": "Bash", "hooks": [{"command": "./d.sh"}]},
                ]
            }
        },
    )

    assert [h["script"] for h in registered_hooks(str(tmp_path), "Bash")] == [
        "b.sh",
        "c.sh",
        "d.sh",
    ]
    assert [h["script"] for h in registered_hooks(str(tmp_path), "Edit")] == [
        "a.sh",
        "b.sh",
        "c.sh",
    ]


# ── what the hook said ──────────────────────────────────────────────────────


def test_the_hooks_last_lines_are_kept_because_that_is_where_the_reason_is() -> None:
    """A failing `make verify` opens with "make verify failed" and ends with why.

    Keeping the FIRST twenty lines kept the banner and threw away the failure.
    """
    said = "\n".join(
        ["make verify failed — fix issues before pushing.", *(f"log line {n}" for n in range(60))]
        + ["make: *** [harness-check] Error 1"]
    )

    kept = tool_learning._hook_said({"tool_result": f"Error: PreToolUse:Bash hook error: {said}"})

    assert "make: *** [harness-check] Error 1" in kept
    assert "log line 59" in kept
    assert "log line 0" not in kept
    # The hook's own verdict line survives the trim: it says what it decided.
    assert kept.startswith("make verify failed")


def test_a_short_hook_message_is_kept_whole() -> None:
    kept = tool_learning._hook_said({"tool_result": "Error: make verify failed\nfix it"})

    assert kept == "make verify failed\nfix it"


def test_a_harness_deny_rule_reported_only_in_the_tool_result_is_not_a_hook(worktree) -> None:  # noqa: F811
    """An older Claude Code reports a configured deny rule in the result alone.

    Reading that as a hook refusal would be the same class of lie this change
    exists to stop, pointing the other way: it would send a maintainer to a
    repository's hook for something their own tool policy decided.
    """
    command = "docker ps"
    events = [
        _tool_use(command, "toolu_9"),
        _hook_block("toolu_9", "Error: Permission to use Bash has been denied."),
        _result(("toolu_9", command)),
    ]

    (denial,) = ClaudeAdapter().permission_denials(events)
    verdict = tool_learning.classify("Bash", command, str(worktree), refusal=denial["refusal"])

    assert denial["refusal"]["harness_line"] is True
    assert verdict.kind == tool_learning.POLICY_REFUSAL
