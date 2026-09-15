"""The launcher addresses one instance regardless of the caller's working directory."""

from __future__ import annotations

from pathlib import Path

from papaya_agent_runtime import paths

ROOT = Path(__file__).resolve().parents[1]


def test_launcher_pins_ppy_home_to_its_own_checkout() -> None:
    launcher = (ROOT / "bin" / "ppy").read_text()
    # The export must derive from the launcher's ROOT and still defer to an explicit
    # PPY_HOME, so tests and multi-instance setups keep working.
    assert 'export PPY_HOME="${PPY_HOME:-$ROOT/.ppy}"' in launcher


def test_paths_honor_ppy_home_over_cwd(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PPY_HOME", raising=False)
    assert paths.ppy_home() == tmp_path / ".ppy"

    pinned = tmp_path / "elsewhere" / ".ppy"
    monkeypatch.setenv("PPY_HOME", str(pinned))
    assert paths.ppy_home() == pinned
    assert paths.db_path() == pinned / "state.db"
