"""Every skill under `.agents/skills/` must be mirrored into `.claude/skills/`.

Skills live canonically in `.agents/skills/<name>/SKILL.md` (Codex reads that
directory natively). Claude Code only scans `.claude/skills/` but follows
symlinks, so each skill needs a relative symlink there or Claude never sees it.
This test is the reminder: add a skill, add its symlink.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENTS_SKILLS = ROOT / ".agents" / "skills"
CLAUDE_SKILLS = ROOT / ".claude" / "skills"


def _skill_names() -> list[str]:
    return sorted(p.parent.name for p in AGENTS_SKILLS.glob("*/SKILL.md"))


def test_at_least_one_skill_exists() -> None:
    assert _skill_names(), f"no skills found under {AGENTS_SKILLS}"


@pytest.mark.parametrize("name", _skill_names())
def test_claude_symlink_mirrors_agents_skill(name: str) -> None:
    link = CLAUDE_SKILLS / name
    assert link.is_symlink(), (
        f"missing .claude/skills/{name} — Claude Code only scans .claude/skills/. "
        f"Fix: (cd .claude/skills && ln -s ../../.agents/skills/{name} {name})"
    )
    expected = Path("..") / ".." / ".agents" / "skills" / name
    assert Path(link.readlink()) == expected, (
        f".claude/skills/{name} must be the relative symlink {expected}, got {link.readlink()}"
    )
    assert (link / "SKILL.md").is_file(), f".claude/skills/{name} does not resolve"


def test_no_stray_claude_skills() -> None:
    stray = sorted(p.name for p in CLAUDE_SKILLS.iterdir()) if CLAUDE_SKILLS.exists() else []
    stray = [n for n in stray if n not in _skill_names()]
    assert not stray, (
        f"unexpected entries in .claude/skills/: {stray} — "
        "skills are authored under .agents/skills/ and only symlinked here"
    )
