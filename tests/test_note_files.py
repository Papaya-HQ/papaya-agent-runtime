"""A note of any length or punctuation is recorded first time.

Issue #115: `ppy progress <n> --phase done --note "…"` and `ppy need <n>
--capability docker --why "…"` were refused as `Bash(ppy:*)` although that
pattern is in the profile. The program was never the problem. Claude Code's own
`decision_reason` for four of the recorded ones is

    Newline followed by # inside a quoted argument can hide arguments from path
    validation

and for the others "Contains brace with quote character (expansion obfuscation)"
and "Contains command_substitution". Every refused note on record is several
paragraphs with a markdown heading, a backtick, or `$(`. No plain single-line
note has ever been refused.

So the text goes in a file and the path is passed, which no shell has to parse.
"""

from __future__ import annotations

import json

import pytest

from papaya_agent_runtime import cli, progress
from papaya_agent_runtime.state import init_db, store

#: A note in the shape that is actually refused: paragraphs, a heading after a
#: newline, backticks, `$`, braces and quotes. Modelled on task 112's real report.
HARD_NOTE = """Rebased ppy/task-112 onto origin/main and pushed. NEW HEAD fffb1c342.

Gate green: `make lint-check` — All checks passed! Env was
`{"triggered_by": {"type": "user"}}` and $HOME was untouched.

## Flagged, not done
- `cd /wt && git status --short` — refused for chaining cd.
"""


@pytest.fixture
def task(ppy_home) -> int:
    conn = init_db()
    run_id = store.create_run(conn, "run")
    repo_id = store.add_repo(
        conn,
        name="repo",
        origin="https://github.com/acme/repo",
        local_path="/tmp/x",
        default_branch="main",
        base_sha=None,
    )
    task_id = store.add_task(conn, run_id=run_id, title="w", repo_id=repo_id)
    conn.commit()
    conn.close()
    return task_id


def _latest_note(task_id: int) -> str:
    return str(progress.latest(task_id).get("note") or "")


def test_a_note_file_records_the_text_exactly_as_written(task, tmp_path, capsys) -> None:
    path = tmp_path / "note.txt"
    path.write_text(HARD_NOTE, encoding="utf-8")

    assert cli.main(["progress", str(task), "--phase", "done", "--note-file", str(path)]) == 0

    assert _latest_note(task) == HARD_NOTE.strip()
    assert "## Flagged, not done" in _latest_note(task)
    assert "`make lint-check`" in _latest_note(task)


def test_the_inline_note_still_works(task) -> None:
    assert cli.main(["progress", str(task), "--phase", "plan", "--note", "short and plain"]) == 0

    assert _latest_note(task) == "short and plain"


def test_a_note_file_wins_over_an_inline_note(task, tmp_path) -> None:
    """The file exists because the inline flag could not carry the text."""
    path = tmp_path / "note.txt"
    path.write_text(HARD_NOTE, encoding="utf-8")

    cli.main(
        ["progress", str(task), "--phase", "done", "--note", "truncated", "--note-file", str(path)]
    )

    assert _latest_note(task) == HARD_NOTE.strip()


@pytest.mark.parametrize(
    ("make", "says"),
    [
        (lambda p: None, "No such file"),
        (lambda p: p.write_text("   \n", encoding="utf-8"), "the file is empty"),
        (lambda p: p.write_bytes(b"\xff\xfe\x00binary"), "not UTF-8 text"),
        (lambda p: p.write_text("x" * (cli.MAX_NOTE_BYTES + 1), encoding="utf-8"), "more than the"),
    ],
)
def test_a_note_file_that_cannot_be_read_fails_loudly(task, tmp_path, capsys, make, says) -> None:
    """A wrong path must not quietly record an empty note over a real report."""
    path = tmp_path / "note.txt"
    make(path)

    code = cli.main(["progress", str(task), "--phase", "done", "--note-file", str(path)])

    assert code == 1
    assert says in capsys.readouterr().err
    assert progress.latest(task) is None  # nothing was recorded at all


def test_a_why_file_reaches_the_capability_request(task, tmp_path, capsys) -> None:
    path = tmp_path / "why.txt"
    path.write_text(HARD_NOTE, encoding="utf-8")

    assert (
        cli.main(["need", str(task), "--capability", "docker", "--why-file", str(path)])
        in (0, 1)  # granted or put to a person, both of which record the reason
    )

    conn = init_db()
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? ORDER BY id", (task,)
    ).fetchall()
    conn.close()
    said = " ".join(str(json.loads(r["payload"])) for r in rows)
    assert "Flagged, not done" in said


def test_the_rules_tell_a_worker_to_use_the_file_and_not_a_substitution() -> None:
    """The worker on task 164 invented `--note "$(cat file)"` and was refused for it.

    The idea was right and the mechanism was wrong, so the rules name both: pass the
    path, and do not wrap it in a command substitution.
    """
    from papaya_agent_runtime.providers.command_rules import command_rules

    rules = command_rules("claude", "ppy/task-1-abc")

    assert "--note-file <path>" in rules
    assert "--why-file <path>" in rules
    assert "git commit -F <path>" in rules
    assert "do not use `$(cat …)`" in rules


def test_both_flags_are_real_commands_the_parser_accepts() -> None:
    """The task-322 corpus test hands every command a prompt names to this parser."""
    parser = cli.build_parser()

    parser.parse_args(["progress", "1", "--phase", "done", "--note-file", "x.txt"])
    parser.parse_args(["need", "1", "--capability", "docker", "--why-file", "x.txt"])
