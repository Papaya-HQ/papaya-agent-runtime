"""The brief lint: symptom first, a probe per hypothesis, a discrepancy row per
hypothesis, scope rules that agree with the required cases, no evidence under /tmp."""

from __future__ import annotations

import pytest

from papaya_agent_runtime import brief_lint, cli
from papaya_agent_runtime.brief_lint import is_defect_brief, lint_brief

GOOD_DEFECT = """# Fix the missing relevance marker

## Symptom

QA marker `qa-relevance-3` is absent on staging rev 00323; row 812 in
`radar_cards` has `score = NULL`.

## Hypotheses

- The scorer skips cards without a `thread_id`.
  - Probe: `SELECT count(*) FROM radar_cards WHERE thread_id IS NULL AND score IS NULL`.
- The nightly backfill never ran — Check: the `backfill` job log for 09-05.

## Expected discrepancies

| Claim | If negative |
| --- | --- |
| scorer skips null threads | flag it; do not touch the scorer |
| backfill never ran | rerun it by hand and re-probe |

## Scope

- Never add a `relationship_bucket` predicate.

## Acceptance

- `make test` passes.

## Evidence

Read `docs/evidence/task-170/score-rows.txt` inside the worktree.

## Goals

`qa-relevance-3` renders on staging; `make test` green. Contract: CONTRACT §4.

## Intent

QA cannot sign off Radar while a marker is missing; the outcome is the marker, not
a particular scorer change.

## In scope

The scorer's null-thread handling and the backfill job, plus their tests.

## Out of scope

The Radar page, card families, and any new predicate.
"""


OUTCOME_SECTIONS = """
## Goals

The thing works and `make test` is green.

## Intent

Users need it; the outcome is what matters, not the approach.

## In scope

This module and its tests.

## Out of scope

Everything adjacent.
"""


def messages(text: str) -> list[str]:
    return [f.message for f in lint_brief(text)]


def test_a_well_shaped_defect_brief_has_no_findings() -> None:
    assert is_defect_brief(GOOD_DEFECT)
    assert lint_brief(GOOD_DEFECT) == []


def test_symptom_section_must_exist_and_precede_the_cause() -> None:
    no_symptom = GOOD_DEFECT.replace("## Symptom\n", "## Observed\n")
    (finding,) = [f for f in lint_brief(no_symptom) if "no Symptom" in f.message]
    assert finding.line == 8  # reported at the Hypotheses heading

    cause_first = GOOD_DEFECT.replace("## Symptom\n", "## Root cause\n").replace(
        "## Hypotheses\n", "## Symptom\n"
    )
    (finding,) = [f for f in lint_brief(cause_first) if "comes before" in f.message]
    assert finding.line == 3
    assert "`root cause` comes before `Symptom` (line 8)" in finding.message


def test_every_hypothesis_needs_a_probe_as_sub_bullet_or_trailing_clause() -> None:
    bare = GOOD_DEFECT.replace(
        "  - Probe: `SELECT count(*) FROM radar_cards WHERE thread_id IS NULL AND score IS NULL`"
        ".\n",
        "",
    )
    (finding,) = [f for f in lint_brief(bare) if "no probe" in f.message]
    assert finding.line == 10
    # The second hypothesis carries "Check:" as a trailing clause and is not flagged.
    assert all("no probe" not in f.message or f.line == 10 for f in lint_brief(bare))


def test_probe_keywords_are_matched_only_as_a_clause_opener() -> None:
    text = GOOD_DEFECT.replace(
        "- The nightly backfill never ran — Check: the `backfill` job log for 09-05.\n",
        "- The nightly backfill never ran, which the CI check: status page would show.\n",
    )
    assert any("no probe" in m for m in messages(text))
    text = GOOD_DEFECT.replace(
        "- The nightly backfill never ran — Check: the `backfill` job log for 09-05.\n",
        "- The nightly backfill never ran.\n  - **Reproduce:** run the job once by hand.\n",
    )
    assert not any("no probe" in m for m in messages(text))


def test_a_prose_cause_section_counts_each_paragraph_as_a_hypothesis() -> None:
    text = """# Fix it

## Symptom

It fails.

## Cause

The cache is stale after a deploy.

The scorer skips null threads. Probe: count the null rows.
"""
    (finding,) = [f for f in lint_brief(text) if "no probe" in f.message]
    assert finding.line == 9


def test_expected_discrepancies_table_needs_a_row_per_hypothesis() -> None:
    missing = GOOD_DEFECT.replace("## Expected discrepancies\n", "## Notes\n")
    (finding,) = [f for f in lint_brief(missing) if "no Expected discrepancies" in f.message]
    assert finding.line == 8
    assert "(2 here)" in finding.message

    short = GOOD_DEFECT.replace("| backfill never ran | rerun it by hand and re-probe |\n", "")
    (finding,) = [f for f in lint_brief(short) if "row(s)" in f.message]
    assert finding.line == 14
    assert "1 row(s) for 2 hypothesis(es)" in finding.message

    as_list = GOOD_DEFECT.replace(
        "| Claim | If negative |\n| --- | --- |\n| scorer skips null threads | flag it; do not"
        " touch the scorer |\n| backfill never ran | rerun it by hand and re-probe |\n",
        "- scorer skips null threads: flag it\n- backfill never ran: rerun it\n",
    )
    assert lint_brief(as_list) == []


def test_scope_rule_and_required_case_naming_the_same_token_are_pointed_at() -> None:
    text = GOOD_DEFECT.replace(
        "- `make test` passes.\n",
        "- `make test` passes.\n- Cards bucketed by `relationship_bucket` are detected.\n",
    )
    (finding,) = [f for f in lint_brief(text) if "same token" in f.message]
    assert finding.line == 28
    assert finding.message == (
        "rule (line 23) and required case name the same token `relationship_bucket`; "
        "check they agree"
    )


def test_self_consistency_ignores_rules_without_a_negation_or_after_the_case() -> None:
    positive_rule = GOOD_DEFECT.replace(
        "- Never add a `relationship_bucket` predicate.\n",
        "- Keep the `relationship_bucket` predicate.\n",
    ).replace("- `make test` passes.\n", "- `relationship_bucket` cases pass.\n")
    assert not any("same token" in m for m in messages(positive_rule))

    case_first = (
        """# Do it

## Tests

- `foo` is covered.

## Scope

- No `foo` outside the adapter.
"""
        + OUTCOME_SECTIONS
    )
    assert lint_brief(case_first) == []


def test_tmp_paths_are_flagged_only_in_an_evidence_section() -> None:
    in_evidence = GOOD_DEFECT.replace(
        "Read `docs/evidence/task-170/score-rows.txt` inside the worktree.\n",
        "Read /private/tmp/claude-501/scratch/rows.txt and /tmp/out.log.\n",
    )
    found = [f for f in lint_brief(in_evidence) if "evidence points at" in f.message]
    assert [f.line for f in found] == [31, 31]
    assert "/private/tmp/claude-501/scratch/rows.txt" in found[0].message
    assert "/tmp/out.log" in found[1].message

    elsewhere = GOOD_DEFECT.replace(
        "- `make test` passes.\n", "- `make test` passes; logs land in /tmp/out.log.\n"
    )
    assert lint_brief(elsewhere) == []


def test_a_non_defect_brief_gets_only_the_consistency_and_evidence_checks() -> None:
    feature = (
        """# Add the export button

## Scope

- No `csv` writer outside `exports/`.

## Acceptance

- The `csv` download works from the toolbar.

## Evidence

Screenshots under /tmp/shots/.
"""
        + OUTCOME_SECTIONS
    )
    assert not is_defect_brief(feature)
    found = messages(feature)
    assert len(found) == 2
    assert any("same token `csv`" in m for m in found)
    assert any("/tmp/shots/" in m for m in found)
    assert not any("Symptom" in m for m in found)

    # The words alone make it a defect brief, and then a Symptom section is owed.
    assert is_defect_brief("# Fix the FAIL in CI\n\nIt is red.\n")
    assert any("no Symptom" in m for m in messages("# Fix the FAIL in CI\n\nIt is red.\n"))
    assert not is_defect_brief("# Failing over gracefully\n\nAdd a fallback.\n")


def test_render_prefixes_the_path_and_line() -> None:
    findings = [brief_lint.Finding(4, "something")]
    assert brief_lint.render(findings, "b.md") == "b.md:4: something"
    assert brief_lint.render(findings) == "line 4: something"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    (tmp_path / ".ppy").mkdir()
    return tmp_path


def test_cli_brief_lint_exits_one_on_findings_and_zero_when_clean(home, capsys) -> None:
    clean = home / "clean.md"
    clean.write_text(GOOD_DEFECT)
    assert cli.main(["brief", "lint", str(clean)]) == 0
    assert "defect brief, no findings" in capsys.readouterr().out

    bad = home / "bad.md"
    bad.write_text(GOOD_DEFECT.replace("## Expected discrepancies\n", "## Notes\n"))
    assert cli.main(["brief", "lint", str(bad)]) == 1
    out = capsys.readouterr().out
    assert f"{bad}:8: no Expected discrepancies table" in out
    assert "1 finding(s)" in out

    assert cli.main(["brief", "lint", str(home / "missing.md")]) == 1
    assert "not found" in capsys.readouterr().err


@pytest.fixture
def fake_supervisor(monkeypatch):
    sent: dict = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def dispatch_task(self, **kwargs):
            sent.update(kwargs)
            return {"ok": True, "task_id": 7, "run_id": 3, "branch": "ppy/task-7-abc"}

    import papaya_agent_runtime.supervisor.client as client_mod

    monkeypatch.setattr(client_mod, "SupervisorClient", FakeClient)
    return sent


def test_dispatch_warns_by_default_and_refuses_with_strict(home, fake_supervisor, capsys) -> None:
    brief = home / "brief.md"
    brief.write_text(GOOD_DEFECT.replace("## Expected discrepancies\n", "## Notes\n"))
    rc = cli.main(["dispatch", "--repo", "papaya", "--brief", str(brief), "--provider", "claude"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "brief lint: 1 finding(s)" in captured.err
    assert f"{brief}:8: no Expected discrepancies table" in captured.err
    assert "dispatched task 7" in captured.out
    assert fake_supervisor["title"] == "Fix the missing relevance marker"

    fake_supervisor.clear()
    rc = cli.main(
        ["dispatch", "--repo", "papaya", "--brief", str(brief), "--provider", "claude", "--strict"]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "dispatch refused: --strict" in captured.err
    assert fake_supervisor == {}


def test_dispatch_of_a_clean_brief_says_nothing_about_lint(home, fake_supervisor, capsys) -> None:
    brief = home / "brief.md"
    brief.write_text(GOOD_DEFECT)
    rc = cli.main(
        ["dispatch", "--repo", "papaya", "--brief", str(brief), "--provider", "claude", "--strict"]
    )
    assert rc == 0
    assert "brief lint" not in capsys.readouterr().err


def test_terminal_phase_lint_finds_both_mismatch_directions() -> None:
    review_brief = GOOD_DEFECT + "\nStop at `--phase review`; do not call done.\n"
    done_brief = GOOD_DEFECT + (
        '\nThen finish with `ppy progress 7 --phase done --note "summary"`.\n'
    )

    mismatch = [f for f in lint_brief(review_brief, ends_at="done") if "--ends-at" in f.message]
    assert len(mismatch) == 1 and "--ends-at done" in mismatch[0].message
    assert not [f for f in lint_brief(review_brief, ends_at="review") if "--ends-at" in f.message]

    mismatch = [f for f in lint_brief(done_brief, ends_at="review") if "--ends-at" in f.message]
    assert len(mismatch) == 1 and "--ends-at review" in mismatch[0].message
    assert not [f for f in lint_brief(done_brief, ends_at="done") if "--ends-at" in f.message]


def test_dispatch_passes_ends_at_to_lint_and_supervisor(home, fake_supervisor, capsys) -> None:
    brief = home / "review.md"
    brief.write_text(GOOD_DEFECT + "\nEnd with phase review, never done.\n")
    assert (
        cli.main(
            [
                "dispatch",
                "--repo",
                "papaya",
                "--brief",
                str(brief),
                "--provider",
                "claude",
                "--ends-at",
                "review",
                "--strict",
            ]
        )
        == 0
    )
    assert "brief lint" not in capsys.readouterr().err
    assert fake_supervisor["ends_at"] == "review"


# --------------------------------------------------------------------------- #
# Goals, Intent, In scope, Out of scope (issue #77)
# --------------------------------------------------------------------------- #


def _without(text: str, heading: str) -> str:
    return text.replace(f"## {heading}\n", f"## Notes about {heading.lower()}\n")


def test_every_brief_owes_the_four_outcome_sections() -> None:
    bare = "# Add the export button\n\nPut a button on the toolbar.\n"
    found = messages(bare)
    assert len(found) == 4
    for name in ("Goals", "Intent", "In scope", "Out of scope"):
        assert any(f"no {name} section — add `## {name}`" in m for m in found), name
    # Each message says what the section is for, not just that it is missing.
    assert any("acceptance criteria" in m for m in found)
    assert any("who benefits" in m for m in found)
    assert any("authorised to do" in m for m in found)
    assert any("stopping boundaries" in m for m in found)


def test_a_missing_or_empty_section_is_named_with_its_line() -> None:
    missing = _without(GOOD_DEFECT, "Out of scope")
    (finding,) = [f for f in lint_brief(missing) if "Out of scope" in f.message]
    assert finding.line == 1 and "no Out of scope section" in finding.message

    empty = GOOD_DEFECT.replace(
        "## Out of scope\n\nThe Radar page, card families, and any new predicate.\n",
        "## Out of scope\n\n",
    )
    (finding,) = [f for f in lint_brief(empty) if "Out of scope" in f.message]
    assert finding.message.startswith("`## Out of scope` is empty — say the explicit exclusions")
    assert finding.line == GOOD_DEFECT.splitlines().index("## Out of scope") + 1

    # A heading with only a sub-heading under it is still empty.
    hollow = GOOD_DEFECT.replace(
        "## In scope\n\nThe scorer's null-thread handling and the backfill job, plus their "
        "tests.\n",
        "## In scope\n\n### Later\n",
    )
    assert any("`## In scope` is empty" in m for m in messages(hollow))


def test_heading_variants_are_recognised_and_lookalikes_are_not() -> None:
    variants = (
        GOOD_DEFECT.replace("## Goals\n", "## Goal\n")
        .replace("## In scope\n", "### In-scope\n")
        .replace("## Out of scope\n", "## Not in scope\n")
    )
    assert not any("section" in m for m in messages(variants))

    non_goals = GOOD_DEFECT.replace("## Goals\n", "## Non-goals\n")
    assert any("no Goals section" in m for m in messages(non_goals))


def test_outcome_sections_are_extracted_verbatim_and_carried_as_standing_scope() -> None:
    sections = brief_lint.outcome_sections(GOOD_DEFECT)
    assert set(sections) == {"Goals", "Intent", "In scope", "Out of scope"}
    assert sections["Out of scope"] == "The Radar page, card families, and any new predicate."
    assert sections["Goals"].startswith("`qa-relevance-3` renders on staging")

    block = brief_lint.standing_scope(GOOD_DEFECT)
    assert block is not None
    assert block.startswith("--- Standing scope, carried from the brief.")
    assert "anything it excludes stays excluded" in block
    for name in ("## Goals", "## Intent", "## In scope", "## Out of scope"):
        assert name in block
    assert block.index("## Goals") < block.index("## Intent") < block.index("## In scope")
    assert "## Symptom" not in block  # only the four sections travel

    # A brief from before the rule has nothing to carry, and says so with None.
    assert brief_lint.standing_scope("# Old brief\n\nJust do it.\n") is None
    assert brief_lint.outcome_sections("# Old\n\n## Goals\n\n## Intent\nwhy\n") == {"Intent": "why"}


def test_dispatch_strict_refuses_a_brief_with_no_scope_boundaries(
    home, fake_supervisor, capsys
) -> None:
    brief = home / "brief.md"
    brief.write_text(_without(GOOD_DEFECT, "Out of scope"))
    rc = cli.main(
        ["dispatch", "--repo", "papaya", "--brief", str(brief), "--provider", "claude", "--strict"]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "no Out of scope section" in err
    assert fake_supervisor == {}


def test_the_documented_example_brief_lints_clean_and_carries_its_scope() -> None:
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "docs" / "brief-example.md"
    text = example.read_text(encoding="utf-8")
    brief_part = text.split("<!-- brief ends -->")[0]
    assert lint_brief(brief_part) == [], brief_lint.render(lint_brief(brief_part))
    block = brief_lint.standing_scope(brief_part)
    assert block is not None
    for name in ("## Goals", "## Intent", "## In scope", "## Out of scope"):
        assert name in block
    # The continuation packet the document shows is the one the runtime builds.
    assert block in text.split("<!-- brief ends -->")[1]
