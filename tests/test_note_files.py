"""A note of any length or punctuation is recorded first time — and only from the worktree.

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

So the text goes in a file and the path is passed. The path is **confined to the
task's own worktree or its evidence directory**: a note reaches the event ledger
and from there pull request bodies and comments, so an unconfined flag would be a
way to publish `~/.ssh/id_rsa` or the state database with one allowed `ppy` call,
and a worker cannot Read those files but can run `ppy`.
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
def task(ppy_home, tmp_path) -> dict:
    """A task with a real worktree and an evidence directory inside it."""
    worktree = tmp_path / "wt"
    (worktree / ".ppy-evidence").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("a private key", encoding="utf-8")

    conn = init_db()
    run_id = store.create_run(conn, "run")
    repo_id = store.add_repo(
        conn,
        name="repo",
        origin="https://github.com/acme/repo",
        local_path=str(worktree),
        default_branch="main",
        base_sha=None,
    )
    task_id = store.add_task(conn, run_id=run_id, title="w", repo_id=repo_id)
    conn.execute("UPDATE tasks SET worktree_path = ? WHERE id = ?", (str(worktree), task_id))
    # A second task with no worktree at all: it may not pass a file.
    homeless = store.add_task(conn, run_id=run_id, title="no worktree", repo_id=repo_id)
    conn.commit()
    conn.close()
    return {
        "id": task_id,
        "homeless": homeless,
        "worktree": worktree,
        "evidence": worktree / ".ppy-evidence",
        "outside": outside,
    }


def _latest_note(task_id: int) -> str:
    return str((progress.latest(task_id) or {}).get("note") or "")


def _progress(task: dict, path, task_id: int | None = None) -> int:
    return cli.main(
        [
            "progress",
            str(task["id"] if task_id is None else task_id),
            "--phase",
            "done",
            "--note-file",
            str(path),
        ]
    )


# ── it records the text ─────────────────────────────────────────────────────


def test_a_note_file_in_the_worktree_records_the_text_exactly(task) -> None:
    path = task["worktree"] / "note.txt"
    path.write_text(HARD_NOTE, encoding="utf-8")

    assert _progress(task, path) == 0

    assert _latest_note(task["id"]) == HARD_NOTE.strip()
    assert "## Flagged, not done" in _latest_note(task["id"])
    assert "`make lint-check`" in _latest_note(task["id"])


def test_a_note_file_in_the_evidence_directory_is_accepted(task) -> None:
    path = task["evidence"] / "note.txt"
    path.write_text(HARD_NOTE, encoding="utf-8")

    assert _progress(task, path) == 0

    assert _latest_note(task["id"]) == HARD_NOTE.strip()


def test_a_relative_path_is_resolved_and_accepted(task, monkeypatch) -> None:
    (task["worktree"] / "note.txt").write_text("plain enough", encoding="utf-8")
    monkeypatch.chdir(task["worktree"])

    assert _progress(task, "note.txt") == 0

    assert _latest_note(task["id"]) == "plain enough"


def test_the_inline_note_still_works(task) -> None:
    assert (
        cli.main(["progress", str(task["id"]), "--phase", "plan", "--note", "short and plain"]) == 0
    )

    assert _latest_note(task["id"]) == "short and plain"


def test_a_note_file_wins_over_an_inline_note(task) -> None:
    """The file exists because the inline flag could not carry the text."""
    path = task["worktree"] / "note.txt"
    path.write_text(HARD_NOTE, encoding="utf-8")

    cli.main(
        [
            "progress",
            str(task["id"]),
            "--phase",
            "done",
            "--note",
            "truncated",
            "--note-file",
            str(path),
        ]
    )

    assert _latest_note(task["id"]) == HARD_NOTE.strip()


# ── it refuses everything outside the worktree ──────────────────────────────


def test_an_absolute_path_outside_the_worktree_is_refused(task, capsys) -> None:
    outside = task["outside"] / "secret.txt"

    code = _progress(task, outside)

    assert code == 1
    said = capsys.readouterr().err
    assert "must be inside this task's worktree" in said
    assert "a private key" not in said
    assert progress.latest(task["id"]) is None


def test_a_dot_dot_escape_is_refused(task, capsys) -> None:
    escape = task["worktree"] / ".." / "outside" / "secret.txt"

    code = _progress(task, escape)

    assert code == 1
    assert "must be inside this task's worktree" in capsys.readouterr().err
    assert progress.latest(task["id"]) is None


def test_a_symlink_inside_the_worktree_pointing_outside_is_refused(task, capsys) -> None:
    """Resolved before the check, so the link is judged on what it points AT."""
    link = task["worktree"] / "looks-fine.txt"
    link.symlink_to(task["outside"] / "secret.txt")

    code = _progress(task, link)

    assert code == 1
    assert "must be inside this task's worktree" in capsys.readouterr().err
    assert progress.latest(task["id"]) is None


def test_a_home_relative_path_is_refused(task, capsys, tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    code = _progress(task, "~/.ssh/id_rsa")

    assert code == 1
    said = capsys.readouterr().err
    assert "must be inside this task's worktree" in said
    assert "PRIVATE KEY" not in said
    assert progress.latest(task["id"]) is None


def test_the_state_database_is_refused(task, capsys) -> None:
    from papaya_agent_runtime.paths import db_path

    code = _progress(task, db_path())

    assert code == 1
    assert "must be inside this task's worktree" in capsys.readouterr().err
    assert progress.latest(task["id"]) is None


def test_a_task_with_no_worktree_may_not_pass_a_file_at_all(task, capsys) -> None:
    path = task["worktree"] / "note.txt"
    path.write_text("fine text", encoding="utf-8")

    code = _progress(task, path, task_id=task["homeless"])

    assert code == 1
    assert "has no worktree to read a note file from" in capsys.readouterr().err
    assert progress.latest(task["homeless"]) is None


def test_the_confinement_is_to_the_NAMED_task_not_the_working_directory(task, monkeypatch) -> None:
    """Another task's worktree is outside this one, whatever the shell's cwd is."""
    monkeypatch.chdir(task["worktree"])
    other = task["outside"] / "note.txt"
    other.write_text("fine text", encoding="utf-8")

    assert _progress(task, other) == 1


@pytest.mark.parametrize(
    ("make", "says"),
    [
        (lambda p: None, "No such file"),
        (lambda p: p.write_text("   \n", encoding="utf-8"), "the file is empty"),
        (lambda p: p.write_bytes(b"\xff\xfe\x00binary"), "not UTF-8 text"),
        (lambda p: p.write_text("x" * (cli.MAX_NOTE_BYTES + 1), encoding="utf-8"), "more than the"),
    ],
)
def test_a_note_file_that_cannot_be_read_fails_loudly(task, capsys, make, says) -> None:
    """A wrong path must not quietly record an empty note over a real report."""
    path = task["worktree"] / "note.txt"
    make(path)

    code = _progress(task, path)

    assert code == 1
    assert says in capsys.readouterr().err
    assert progress.latest(task["id"]) is None


# ── the same for `ppy need --why-file` ──────────────────────────────────────


def test_a_why_file_reaches_the_capability_request(task) -> None:
    path = task["worktree"] / "why.txt"
    path.write_text(HARD_NOTE, encoding="utf-8")

    cli.main(["need", str(task["id"]), "--capability", "docker", "--why-file", str(path)])

    conn = init_db()
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? ORDER BY id", (task["id"],)
    ).fetchall()
    conn.close()
    said = " ".join(str(json.loads(r["payload"])) for r in rows)
    assert "Flagged, not done" in said


def test_a_why_file_outside_the_worktree_is_refused(task, capsys) -> None:
    code = cli.main(
        [
            "need",
            str(task["id"]),
            "--capability",
            "docker",
            "--why-file",
            str(task["outside"] / "secret.txt"),
        ]
    )

    assert code == 1
    said = capsys.readouterr().err
    assert "must be inside this task's worktree" in said
    assert "a private key" not in said


# ── what the rules tell a worker ────────────────────────────────────────────


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
    # And where the file may live, because the flag refuses anything else.
    assert "evidence directory" in rules


def test_both_flags_are_real_commands_the_parser_accepts() -> None:
    """The task-322 corpus test hands every command a prompt names to this parser."""
    parser = cli.build_parser()

    parser.parse_args(["progress", "1", "--phase", "done", "--note-file", "x.txt"])
    parser.parse_args(["need", "1", "--capability", "docker", "--why-file", "x.txt"])
