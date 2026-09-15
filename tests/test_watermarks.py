"""Comment-sweep watermarks: the newest processed timestamp per external record."""

from __future__ import annotations

import json

import pytest

from papaya_agent_runtime import cli, watermarks
from papaya_agent_runtime.state import store
from papaya_agent_runtime.state.db import init_db

TICKET = "https://papaya.example/work-items/abc-123"


@pytest.fixture
def ppy_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    return tmp_path / ".ppy"


def test_set_get_and_list_round_trip(tmp_path) -> None:
    conn = init_db(tmp_path / "state.db")
    assert watermarks.get_watermark(conn, TICKET) is None
    assert watermarks.list_watermarks(conn) == []

    stored, previous = watermarks.set_watermark(conn, TICKET, "2026-09-06T14:03:00Z")
    assert (stored, previous) == ("2026-09-06T14:03:00+00:00", None)
    row = watermarks.get_watermark(conn, TICKET)
    assert row["watermark"] == "2026-09-06T14:03:00+00:00"
    assert row["recorded_at"].endswith("+00:00")
    assert row["note"] is None

    # A second set replaces the value and reports the previous one.
    stored, previous = watermarks.set_watermark(
        conn, TICKET, "2026-09-06T15:00:00+00:00", note="two comments, both relayed"
    )
    assert (stored, previous) == ("2026-09-06T15:00:00+00:00", "2026-09-06T14:03:00+00:00")
    assert watermarks.get_watermark(conn, TICKET)["note"] == "two comments, both relayed"

    watermarks.set_watermark(conn, "PAP-7", "2026-09-05T09:00:00Z")
    assert [r["key"] for r in watermarks.list_watermarks(conn)] == ["PAP-7", TICKET]
    assert watermarks.delete_watermark(conn, "PAP-7") is True
    assert watermarks.delete_watermark(conn, "PAP-7") is False
    assert [r["key"] for r in store.list_watermarks(conn)] == [TICKET]


def test_timestamps_are_validated_and_normalised_to_utc() -> None:
    assert watermarks.normalize("2026-09-06T14:03:00Z") == "2026-09-06T14:03:00+00:00"
    assert watermarks.normalize("2026-09-06T16:03:00+02:00") == "2026-09-06T14:03:00+00:00"
    assert watermarks.normalize("2026-09-06T14:03:00") == "2026-09-06T14:03:00+00:00"
    assert watermarks.normalize("2026-09-06T14:03:00.250Z") == "2026-09-06T14:03:00.250000+00:00"
    with pytest.raises(watermarks.WatermarkError, match="not an ISO-8601 timestamp"):
        watermarks.normalize("yesterday")
    with pytest.raises(watermarks.WatermarkError, match="record key"):
        watermarks.set_watermark(init_db(), "   ", "2026-09-06T14:03:00Z")


def test_cli_set_get_list_and_clear(ppy_home, capsys) -> None:
    assert cli.main(["watermark", "set", TICKET, "2026-09-06T14:03:00Z"]) == 0
    assert capsys.readouterr().out == f"{TICKET}: watermark 2026-09-06T14:03:00+00:00 (was unset)\n"

    assert cli.main(["watermark", "get", TICKET]) == 0
    assert capsys.readouterr().out == "2026-09-06T14:03:00+00:00\n"

    assert cli.main(["watermark", "set", TICKET, "2026-09-06T15:00:00Z", "--note", "relayed"]) == 0
    assert "(was 2026-09-06T14:03:00+00:00)" in capsys.readouterr().out

    assert cli.main(["watermark", "show", TICKET, "--json"]) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["key"] == TICKET
    assert row["watermark"] == "2026-09-06T15:00:00+00:00"
    assert row["note"] == "relayed"

    assert cli.main(["watermark", "set", "PAP-7", "2026-09-05T09:00:00Z"]) == 0
    capsys.readouterr()
    assert cli.main(["watermark", "list"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2
    assert out[0].startswith("2026-09-05T09:00:00+00:00  recorded ")
    assert out[0].endswith("  PAP-7")
    assert out[1].endswith(f"  {TICKET}  relayed")

    assert cli.main(["watermark", "list", "--json"]) == 0
    assert [r["key"] for r in json.loads(capsys.readouterr().out)] == ["PAP-7", TICKET]

    assert cli.main(["watermark", "clear", "PAP-7"]) == 0
    assert "cleared" in capsys.readouterr().out
    assert cli.main(["watermark", "clear", "PAP-7"]) == 1
    assert "no watermark for PAP-7" in capsys.readouterr().err


def test_cli_get_of_an_unset_key_says_what_to_do_and_exits_one(ppy_home, capsys) -> None:
    assert cli.main(["watermark", "get", "PAP-9"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no watermark for PAP-9" in captured.err
    assert "the first sweep reads everything" in captured.err
    assert cli.main(["watermark", "list"]) == 0
    assert capsys.readouterr().out == "no watermarks recorded\n"


def test_cli_set_refuses_a_bad_timestamp_and_names_a_move_back(ppy_home, capsys) -> None:
    assert cli.main(["watermark", "set", "PAP-9", "yesterday"]) == 1
    assert "not an ISO-8601 timestamp" in capsys.readouterr().err
    assert cli.main(["watermark", "get", "PAP-9"]) == 1
    capsys.readouterr()

    assert cli.main(["watermark", "set", "PAP-9", "2026-09-06T15:00:00Z"]) == 0
    capsys.readouterr()
    assert cli.main(["watermark", "set", "PAP-9", "2026-09-06T14:00:00Z"]) == 0
    assert "moved back from 2026-09-06T15:00:00+00:00; the next sweep re-reads" in (
        capsys.readouterr().out
    )
