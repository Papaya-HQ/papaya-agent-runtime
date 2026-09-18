"""Preflight a brief against the worker's command surface and the repo's history (#94, item 4)."""

from __future__ import annotations

import pytest

from papaya_agent_runtime import brief_lint, prior_attempts
from papaya_agent_runtime.brief_lint import allowlist_findings, literal_commands
from papaya_agent_runtime.config import default_claude_allowed_tools
from papaya_agent_runtime.state import init_db, store

ALLOWED = default_claude_allowed_tools()

BRIEF = """# Package the CLI

## Goals

`ppy-client --help` works from a wheel. Contract §2.

## Intent

Users install one thing.

## In scope

`packaging/` and its tests. Pre-authorised adjacent changes: the pyproject entry.

## Out of scope

No new commands.

## Steps

Unpack the fixture with `unzip fixtures/sample.zip -d /tmp/x`, then:

```
chmod +x bin/ppy-client
git add bin/ppy-client && git commit -m "exec bit"
./bin/ppy progress 7 --phase plan --note "planned"
```

Run `make test` and `uv run pytest -q`.
"""


# --------------------------------------------------------------------------- #
# Allowlist
# --------------------------------------------------------------------------- #


def test_literal_commands_are_read_from_fences_and_command_like_inline_code() -> None:
    found = [c for _line, c in literal_commands(BRIEF)]
    assert "unzip fixtures/sample.zip -d /tmp/x" in found
    assert "chmod +x bin/ppy-client" in found
    # A compound line is split: each piece is matched on its own.
    assert "git add bin/ppy-client" in found and 'git commit -m "exec bit"' in found
    assert './bin/ppy progress 7 --phase plan --note "planned"' in found
    assert "make test" in found and "uv run pytest -q" in found
    # Identifiers in backticks are not commands.
    assert not any(c.startswith("ppy-client --help") for c in found)
    assert not any(c.startswith("packaging/") for c in found)


def test_allowlist_findings_name_the_denied_command_and_its_substitute() -> None:
    found = allowlist_findings(BRIEF, ALLOWED)
    heads = {f.message.split("`")[3] for f in found}
    assert heads == {"unzip", "chmod"}
    by_head = {f.message.split("`")[3]: f for f in found}
    assert "git update-index --chmod=+x <file>" in by_head["chmod"].message
    assert "python3 -m zipfile -e" in by_head["unzip"].message
    assert (
        by_head["unzip"].line
        == BRIEF.splitlines().index(
            "Unpack the fixture with `unzip fixtures/sample.zip -d /tmp/x`, then:"
        )
        + 1
    )
    # Covered commands, with or without a path, are not reported.
    assert not any(h in heads for h in ("git", "make", "uv", "ppy"))


def test_an_allowlisted_command_passes() -> None:
    assert allowlist_findings("Run `git status` then `uv run pytest -q`.\n", ALLOWED) == []


def test_a_bare_bash_pattern_allows_everything_and_unknowns_get_generic_advice() -> None:
    assert allowlist_findings(BRIEF, ["Read", "Bash"]) == []
    (finding,) = allowlist_findings("Run `brew install jq` first.\n", ["Bash(git:*)"])
    assert "`brew` is not covered" in finding.message
    assert "let the manager do it" in finding.message


def test_preflight_checks_the_allowlist_only_when_given_one() -> None:
    assert any(
        "claude allowlist" in f.message for f in brief_lint.preflight(BRIEF, allowed=ALLOWED)
    )
    # A Codex brief is dispatched without an allowlist and gets no allowlist findings.
    assert brief_lint.preflight(BRIEF) == []


def test_claude_allowlist_is_read_for_claude_only(monkeypatch) -> None:
    monkeypatch.setenv("PPY_CLAUDE_ALLOWED_TOOLS", "Read,Bash(git:*)")
    assert brief_lint.claude_allowlist("claude") == ["Read", "Bash(git:*)"]
    assert brief_lint.claude_allowlist("codex") is None
    assert brief_lint.claude_allowlist(None) is None


# --------------------------------------------------------------------------- #
# Pre-authorised adjacent changes
# --------------------------------------------------------------------------- #


def test_lint_brief_asks_for_the_pre_authorised_line_under_in_scope() -> None:
    without = BRIEF.replace(" Pre-authorised adjacent changes: the pyproject entry.", "")
    (finding,) = [f for f in brief_lint.lint_brief(without) if "pre-authorised" in f.message]
    assert finding.line == BRIEF.splitlines().index("## In scope") + 1
    assert "Outside scope, required to build" in finding.message
    assert not any("pre-authorised" in f.message for f in brief_lint.lint_brief(BRIEF))
    # Either spelling satisfies it.
    spelled = without.replace(
        "## Out of scope", "Pre-authorized adjacent changes: none.\n\n## Out of scope"
    )
    assert not any("pre-authorised" in f.message for f in brief_lint.lint_brief(spelled))


# --------------------------------------------------------------------------- #
# Prior attempts
# --------------------------------------------------------------------------- #


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    (tmp_path / ".ppy").mkdir()
    connection = init_db()
    yield connection
    connection.close()


def _repo(conn, name: str = "client") -> int:
    return store.add_repo(
        conn,
        name=name,
        origin=f"/src/{name}",
        local_path=f"/src/{name}",
        default_branch="main",
        base_sha=None,
    )


def _task(conn, repo_id: int, title: str, status: str, reason: str | None = None) -> int:
    run_id = store.create_run(conn, title)
    task_id = store.add_task(conn, run_id=run_id, title=title, repo_id=repo_id)
    store.set_task_status(conn, task_id, status)
    if reason is not None:
        store.append_event(
            conn,
            kind="task_closed",
            payload={"task_id": task_id, "reason": reason},
            run_id=run_id,
            task_id=task_id,
        )
    return task_id


def test_a_same_title_closed_task_without_a_prior_attempt_section_is_one_finding(conn) -> None:
    repo_id = _repo(conn)
    prior = _task(conn, repo_id, "Package the CLI", "closed", "under-delivered: no tests")

    note = prior_attempts.describe(conn, "client", "  package THE cli ")
    assert note is not None
    assert note.startswith(f'task {prior} "Package the CLI" ended closed on ')
    assert note.endswith(": under-delivered: no tests")

    (finding,) = [
        f for f in brief_lint.preflight(BRIEF, prior=note) if "Prior attempt" in f.message
    ]
    assert f"task {prior}" in finding.message

    with_section = BRIEF.replace(
        "## Steps",
        "## Prior attempt\n\nTask closed for no tests; this one ships them first.\n\n## Steps",
    )
    assert not any(
        "Prior attempt" in f.message for f in brief_lint.preflight(with_section, prior=note)
    )


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_and_cancelled_tasks_are_prior_attempts_too(conn, status) -> None:
    repo_id = _repo(conn)
    prior = _task(conn, repo_id, "Package the CLI", status)
    found = prior_attempts.for_repo(conn, "client", "Package the CLI")
    assert found is not None and found.task_id == prior and found.status == status


def test_live_delivered_other_repo_and_other_title_tasks_are_not_prior_attempts(conn) -> None:
    repo_id = _repo(conn)
    other_repo = _repo(conn, "server")
    _task(conn, repo_id, "Package the CLI", "in_progress")
    _task(conn, repo_id, "Package the CLI", "delivered")
    _task(conn, other_repo, "Package the CLI", "closed", "elsewhere")
    _task(conn, repo_id, "Something else", "closed", "different work")
    assert prior_attempts.describe(conn, "client", "Package the CLI") is None
    assert prior_attempts.describe(conn, "missing", "Package the CLI") is None
    assert prior_attempts.describe(conn, "client", "") is None


def test_the_newest_ended_attempt_is_the_one_named(conn) -> None:
    repo_id = _repo(conn)
    _task(conn, repo_id, "Package the CLI", "closed", "first try")
    newest = _task(conn, repo_id, "Package the CLI", "failed")
    found = prior_attempts.for_repo(conn, "client", "Package the CLI")
    assert found is not None and found.task_id == newest


def test_a_first_attempt_gets_no_prior_attempt_finding() -> None:
    assert brief_lint.prior_attempt_findings(BRIEF, None) == []
