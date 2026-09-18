"""The brief contract (#94, item 5): a plan note says whether to block, a release file
list comes from git, and the stale "read it before you proceed" wording is gone."""

from __future__ import annotations

import re
from pathlib import Path

from papaya_agent_runtime.brief_lint import lint_brief

ROOT = Path(__file__).resolve().parent.parent

OUTCOME_SECTIONS = """
## Goals

The thing works and `make test` is green.

## Intent

Users need it; the outcome is what matters, not the approach.

## In scope

This module and its tests. Pre-authorised adjacent changes: none.

## Out of scope

Everything adjacent.
"""

FILE_LIST = """
## Files

- `pyproject.toml`
- `src/papaya_agent_runtime/__init__.py`
- `CHANGELOG.md`
"""


def plan_findings(text: str) -> list[str]:
    return [f.message for f in lint_brief(text) if "plan note" in f.message]


def release_findings(text: str) -> list[str]:
    return [f.message for f in lint_brief(text) if "release brief" in f.message]


def test_a_plan_note_that_never_says_whether_to_block_is_one_finding() -> None:
    brief = "# Add a button\n" + OUTCOME_SECTIONS + "\n## Plan note\n\nPost your plan first.\n"
    (finding,) = [f for f in lint_brief(brief) if "plan note" in f.message]
    assert finding.line == brief.splitlines().index("## Plan note") + 1
    assert "`blocking: stop after posting and wait for the manager's reply`" in finding.message
    assert "`non-blocking: post it, then proceed`" in finding.message


def test_a_plan_note_gate_heading_is_held_to_the_same_rule() -> None:
    brief = "# Add a button\n" + OUTCOME_SECTIONS + "\n## Plan-note gate\n\nPost it.\n"
    assert len(plan_findings(brief)) == 1


def test_a_plan_note_saying_blocking_or_non_blocking_is_clean() -> None:
    for phrase in (
        "blocking: stop after posting and wait for the manager's reply",
        "non-blocking: post it, then proceed",
    ):
        brief = "# Add a button\n" + OUTCOME_SECTIONS + f"\n## Plan note\n\n{phrase}. Map goals.\n"
        assert plan_findings(brief) == [], phrase


def test_a_brief_without_a_plan_note_section_gets_no_plan_note_finding() -> None:
    brief = "# Add a button\n" + OUTCOME_SECTIONS + "\n## Verification\n\n`make test`.\n"
    assert plan_findings(brief) == []


def test_a_release_brief_listing_files_without_git_show_stat_is_one_finding() -> None:
    brief = "# Release 0.17.0\n" + OUTCOME_SECTIONS + FILE_LIST
    (finding,) = [f for f in lint_brief(brief) if "release brief" in f.message]
    assert finding.line == brief.splitlines().index("- `pyproject.toml`") + 1
    assert "`git show --stat <previous release commit>`" in finding.message
    assert "not the merge" in finding.message


def test_a_version_bump_title_is_a_release_brief_whatever_its_case() -> None:
    brief = "# Version Bump for the CLI\n" + OUTCOME_SECTIONS + FILE_LIST
    assert len(release_findings(brief)) == 1


def test_a_release_brief_carrying_git_show_stat_is_clean() -> None:
    brief = (
        "# Release 0.17.0\n"
        + OUTCOME_SECTIONS
        + FILE_LIST
        + "\nThe list above is the output of `git show --stat 1a2b3c4`.\n"
    )
    assert release_findings(brief) == []


def test_a_non_release_brief_listing_files_gets_no_release_finding() -> None:
    brief = "# Add the export button\n" + OUTCOME_SECTIONS + FILE_LIST
    assert release_findings(brief) == []


def test_a_release_brief_naming_no_files_gets_no_release_finding() -> None:
    brief = "# Release 0.17.0\n" + OUTCOME_SECTIONS + "\n## Notes\n\n- e.g. tag it, i.e. push.\n"
    assert release_findings(brief) == []


def test_a_missing_pre_authorised_line_is_one_finding() -> None:
    brief = "# Add a button\n" + OUTCOME_SECTIONS.replace(
        " Pre-authorised adjacent changes: none.", ""
    )
    found = [f for f in lint_brief(brief) if "pre-authorised" in f.message]
    assert len(found) == 1
    assert found[0].line == brief.splitlines().index("## In scope") + 1


def test_the_skills_and_docs_no_longer_promise_to_read_the_plan_before_you_proceed() -> None:
    stale = re.compile(
        r"read it before you proceed|reads it against this brief before you go on"
        r"|say you will read it against",
        re.IGNORECASE,
    )
    hits = [
        f"{path.relative_to(ROOT)}:{number}"
        for tree in (ROOT / ".agents", ROOT / "docs")
        for path in sorted(tree.rglob("*"))
        if path.is_file() and path.suffix in {".md", ".txt"}
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if stale.search(line)
    ]
    assert hits == []


def test_the_brief_skill_states_both_plan_gate_phrasings_and_the_release_rule() -> None:
    skill = (ROOT / ".agents" / "skills" / "brief-a-worker" / "SKILL.md").read_text("utf-8")
    flat = " ".join(skill.split())
    assert "`blocking: stop after posting and wait for the manager's reply`" in flat
    assert "`non-blocking: post it, then proceed`" in flat
    assert "`git show --stat <previous release commit>`" in flat
    assert "the release commit, not the merge" in flat
    assert "explicitly marked `unknown`" in flat
    assert "`Pre-authorised adjacent changes:`" in flat
    assert "## Prior attempt" in flat
    # The Claude copy is a link to the same file, never a copy that can drift.
    claude_copy = ROOT / ".claude" / "skills" / "brief-a-worker"
    assert claude_copy.is_symlink()
    assert (claude_copy / "SKILL.md").read_text("utf-8") == skill
