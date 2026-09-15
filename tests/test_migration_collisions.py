"""Where a repo keeps its migrations, and what an added revision says it builds on."""

from __future__ import annotations

import pytest

from papaya_agent_runtime import cli, migrations, repos
from papaya_agent_runtime.state import init_db
from papaya_agent_runtime.state.db import _column_names


def test_schema_gains_the_migrations_glob_on_fresh_and_existing_databases(ppy_home) -> None:
    conn = init_db()
    assert "migrations_glob" in _column_names(conn, "repos")
    conn.execute("ALTER TABLE repos DROP COLUMN migrations_glob")
    conn.commit()
    assert "migrations_glob" not in _column_names(conn, "repos")
    conn = init_db()
    assert "migrations_glob" in _column_names(conn, "repos")
    init_db()  # a second run must not fail on the now-present column


def test_a_repo_with_nothing_configured_uses_the_ordinary_alembic_layout(
    ppy_home, source_repo, capsys
) -> None:
    added = repos.add_repo(source_repo)
    assert cli.main(["repo", "set", added.name]) == 0
    out = capsys.readouterr().out
    assert "**/alembic/versions/*.py" in out
    assert "the default" in out


def test_setting_a_migrations_glob_changes_which_added_files_count(
    ppy_home, source_repo, capsys
) -> None:
    added = repos.add_repo(source_repo)
    assert cli.main(["repo", "set", added.name, "--migrations-glob", "db/changes/*.sql"]) == 0
    assert "db/changes/*.sql" in capsys.readouterr().out

    conn = init_db()
    glob = migrations.glob_for_repo(conn.execute("SELECT * FROM repos").fetchone())
    assert glob == "db/changes/*.sql"
    assert migrations.matches("db/changes/0007-events.sql", glob)
    assert not migrations.matches("backend/alembic/versions/aaa.py", glob)

    # An empty string puts the repo back on the default rather than matching nothing.
    assert cli.main(["repo", "set", added.name, "--migrations-glob", ""]) == 0
    assert "the default" in capsys.readouterr().out


def test_the_default_glob_finds_alembic_revisions_at_any_depth() -> None:
    glob = migrations.DEFAULT_MIGRATIONS_GLOB
    assert migrations.matches("alembic/versions/aaa.py", glob)
    assert migrations.matches("backend/alembic/versions/aaa.py", glob)
    assert migrations.matches("services/api/alembic/versions/aaa.py", glob)
    assert not migrations.matches("backend/models/events.py", glob)
    assert not migrations.matches("backend/alembic/env.py", glob)


def test_down_revision_is_read_from_every_shape_alembic_writes() -> None:
    assert migrations.parse_down_revision('down_revision = "abc123"\n') == "abc123"
    assert migrations.parse_down_revision("down_revision: str | None = 'abc123'\n") == "abc123"
    assert migrations.parse_down_revision("down_revision = None\n") == "None"
    # A merge revision names both heads it joins.
    assert migrations.parse_down_revision('down_revision = ("aaa", "bbb")\n') == "aaa, bbb"
    # A file mid-edit still answers, from the line itself.
    assert migrations.parse_down_revision('def upgrade(\ndown_revision = "abc"\n') == "abc"
    assert migrations.parse_down_revision("revision = 'aaa'\n") is None
    assert migrations.parse_down_revision("down_revision = some_name\n") is None


# ── The glob matcher itself ─────────────────────────────────────────────────
#
# `PurePosixPath.full_match` is Python 3.13+. This project supports 3.12, where
# that call raised `AttributeError` inside the broad `except` around every
# dispatch advisory — so on 3.12 the migration check silently reported nothing
# and every dispatch looked clean. These pin the replacement's semantics on both
# versions, because the matcher is now the only thing standing between two
# migrations off one head and a red migration-graph test after both merge.


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("alembic/versions/aaa.py", True),  # `**` matches zero segments
        ("backend/alembic/versions/aaa.py", True),
        ("services/api/alembic/versions/aaa.py", True),
        ("backend/models/events.py", False),
        ("backend/alembic/env.py", False),  # right directory, wrong depth
    ],
)
def test_the_default_glob_spans_any_depth_including_none(path: str, expected: bool) -> None:
    assert migrations.matches(path, migrations.DEFAULT_MIGRATIONS_GLOB) is expected


def test_a_star_stops_at_a_separator() -> None:
    """Otherwise a pattern silently reaches into directories it never named."""
    assert migrations.matches("db/changes/0007.sql", "db/changes/*.sql")
    assert not migrations.matches("db/changes/sub/0007.sql", "db/changes/*.sql")
    assert migrations.matches("b.py", "*.py")
    assert not migrations.matches("a/b.py", "*.py")


def test_a_pattern_without_a_leading_star_star_is_anchored_at_the_repo_root() -> None:
    assert migrations.matches("db/changes/x.sql", "db/changes/*.sql")
    assert not migrations.matches("backend/db/changes/x.sql", "db/changes/*.sql")


def test_character_classes_work_the_way_a_shell_spells_them() -> None:
    assert migrations.matches("x/0007.sql", "x/[0-9][0-9][0-9][0-9].sql")
    assert not migrations.matches("x/abcd.sql", "x/[0-9][0-9][0-9][0-9].sql")
    assert migrations.matches("x/abcd.sql", "x/[!0-9]*.sql")
    assert not migrations.matches("x/0007.sql", "x/[!0-9]*.sql")


def test_a_question_mark_matches_one_character_but_not_a_separator() -> None:
    assert migrations.matches("x/ab.sql", "x/a?.sql")
    assert not migrations.matches("x/a/b.sql", "x/a?b.sql")


def test_an_empty_or_malformed_glob_matches_nothing_rather_than_raising() -> None:
    """A bad pattern must not take a dispatch down; it just checks nothing."""
    assert not migrations.matches("anything", "")
    assert not migrations.matches("anything", "x/[unterminated")
