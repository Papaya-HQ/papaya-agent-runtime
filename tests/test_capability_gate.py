"""Hermetic tests for the provider capability gate (fail-closed)."""

from __future__ import annotations

import json

from papaya_agent_runtime.providers import capability


def _write(path, providers) -> str:
    path.write_text(json.dumps({"providers": providers}), encoding="utf-8")
    capability._load.cache_clear()
    return str(path)


def test_missing_file_is_all_false(tmp_path) -> None:
    caps = capability.capabilities_for("claude", path=str(tmp_path / "none.json"))
    assert all(v is False for v in caps.values())


def test_reads_recorded_flags(tmp_path) -> None:
    p = _write(
        tmp_path / "cap.json",
        {
            "claude": {
                "cli_version": "2.1.251",
                "capabilities": {
                    "resume_after_sigint_tool": True,
                    "resume_after_sigkill": True,
                    "session_survives_process_exit": True,
                },
            }
        },
    )
    caps = capability.capabilities_for("claude", path=p)
    assert caps["resume_after_sigint_tool"] is True
    assert caps["mid_process_steer"] is False  # absent -> false
    assert capability.allows_interrupt_steer("claude", path=p) is True


def test_version_drift_disables_record(tmp_path) -> None:
    p = _write(
        tmp_path / "cap.json",
        {
            "codex": {
                "cli_version": "0.150.1",
                "capabilities": {
                    "resume_after_sigint_tool": True,
                    "resume_after_sigkill": True,
                    "session_survives_process_exit": True,
                },
            }
        },
    )
    # A different installed version must not trust the old probe row.
    assert capability.allows_interrupt_steer("codex", cli_version="0.200.0", path=p) is False
    assert capability.allows_interrupt_steer("codex", cli_version="0.150.1", path=p) is True


def test_committed_matrix_matches_m0(tmp_path) -> None:
    # The committed matrix records the real M0 outcome for the versions it was
    # probed at; read them from the record so a re-probe does not stale the test.
    capability._load.cache_clear()
    for provider in ("claude", "codex"):
        assert capability.record_source(provider) == capability.TRACKED
        version = capability.recorded_version(provider)
        assert version  # None would skip the version check and pass vacuously
        assert capability.allows_interrupt_steer(provider, cli_version=version) is True
        # Any other installed version is still untrusted by the committed row.
        assert capability.allows_interrupt_steer(provider, cli_version=f"{version}.0") is False
    # Codex is single-shot: mid-process steer stays false even so.
    assert capability.capabilities_for("codex")["mid_process_steer"] is False


def _write_local(tmp_path, monkeypatch, providers) -> None:
    """Point PPY_HOME at a scratch dir and write the machine-local record there."""
    home = tmp_path / "ppy-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PPY_HOME", str(home))
    (home / "provider-capabilities.json").write_text(
        json.dumps({"providers": providers}), encoding="utf-8"
    )
    capability.clear_cache()


def test_local_record_overrides_tracked(tmp_path, monkeypatch) -> None:
    # The tracked record proves claude at a newer version; this machine runs 2.1.226.
    assert capability.allows_interrupt_steer("claude", cli_version="2.1.226") is False
    _write_local(
        tmp_path,
        monkeypatch,
        {
            "claude": {
                "cli_version": "2.1.226",
                "capabilities": {
                    "resume_after_sigint_tool": True,
                    "resume_after_sigkill": True,
                    "session_survives_process_exit": True,
                },
            }
        },
    )
    assert capability.allows_interrupt_steer("claude", cli_version="2.1.226") is True
    assert capability.recorded_version("claude") == "2.1.226"
    assert capability.record_source("claude") == capability.LOCAL


def test_local_override_is_per_provider(tmp_path, monkeypatch) -> None:
    # Probing only claude must not demote the tracked codex row to unproven.
    _write_local(
        tmp_path,
        monkeypatch,
        {"claude": {"cli_version": "2.1.226", "capabilities": {}}},
    )
    assert capability.record_source("claude") == capability.LOCAL
    assert capability.record_source("codex") == capability.TRACKED
    assert capability.allows_interrupt_steer("codex", cli_version="0.150.1") is True


def test_missing_local_record_falls_back_to_tracked(tmp_path, monkeypatch) -> None:
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("PPY_HOME", str(home))
    capability.clear_cache()
    assert capability.record_source("claude") == capability.TRACKED
    version = capability.recorded_version("claude")
    assert version
    assert capability.allows_interrupt_steer("claude", cli_version=version) is True


def test_explicit_path_ignores_local_override(tmp_path, monkeypatch) -> None:
    # An explicit path means "exactly this file" — no override consulted.
    _write_local(
        tmp_path,
        monkeypatch,
        {
            "claude": {
                "cli_version": "9.9.9",
                "capabilities": {
                    "resume_after_sigint_tool": True,
                    "resume_after_sigkill": True,
                    "session_survives_process_exit": True,
                },
            }
        },
    )
    p = _write(tmp_path / "explicit.json", {"claude": {"cli_version": "1.0.0"}})
    assert capability.recorded_version("claude", path=p) == "1.0.0"
    assert capability.allows_interrupt_steer("claude", cli_version="9.9.9", path=p) is False


def test_unknown_provider_has_no_source(tmp_path, monkeypatch) -> None:
    home = tmp_path / "no-home"
    home.mkdir()
    monkeypatch.setenv("PPY_HOME", str(home))
    capability.clear_cache()
    assert capability.record_source("nope") is None
    assert capability.recorded_version("nope") is None
