"""The Papaya connection is read, reported, and never required.

This runtime has no persona of its own — it is whichever Papaya agent the machine
is connected as. That makes two things load-bearing: reading the identity has to be
right (connecting as the wrong agent would post to the workspace under someone
else's name), and every state short of connected has to stay usable, because a
runtime that refuses to build code when Papaya is unreachable is worse than one
that builds code quietly.
"""

from __future__ import annotations

import json

import pytest

from papaya_agent_runtime import papaya


@pytest.fixture
def client_home(tmp_path, monkeypatch):
    home = tmp_path / ".papaya-agent"
    home.mkdir()
    monkeypatch.setenv(papaya.HOME_ENV, str(home))
    return home


def _write(home, payload: dict) -> None:
    (home / "config.json").write_text(json.dumps(payload), encoding="utf-8")


AGENT = {
    "agent_id": "a-1",
    "agent_name": "Engineering Agent",
    "agent_handle": "engineering_agent",
    "agent_role_label": "Engineering activity relay",
    "workspace_id": "w-1",
    "connection_id": "c-1",
}


def test_no_config_is_absent_not_an_error(client_home, monkeypatch) -> None:
    monkeypatch.setattr(papaya, "installed", lambda: None)
    status = papaya.status()
    assert status["state"] == "absent"
    assert status["identity"] is None
    assert papaya.identity() is None


def test_signed_in_without_a_pinned_agent_is_not_connected(client_home, monkeypatch) -> None:
    """Signing in is half the flow; the runtime must not claim an identity it lacks."""
    monkeypatch.setattr(papaya, "installed", lambda: "/usr/local/bin/papaya-agent")
    _write(client_home, {"session": {"refresh_token": "t"}, "agents": {}})
    assert papaya.identity() is None
    assert papaya.status()["state"] == "signed_in"


def test_the_pinned_agent_wins_when_several_are_configured(client_home) -> None:
    """One machine can hold tokens for several agents; only the pinned one is us."""
    other = {**AGENT, "agent_id": "a-2", "agent_handle": "qa_agent", "agent_name": "QA Agent"}
    _write(
        client_home,
        {
            "session": {"refresh_token": "t"},
            "agents": {"a-1": AGENT, "a-2": other},
            "connect": {"agent_id": "a-2", "harness": "claude"},
        },
    )
    who = papaya.identity()
    assert who is not None
    assert who.handle == "qa_agent"
    assert who.harness == "claude"


def test_several_agents_and_no_pin_refuses_to_guess(client_home) -> None:
    """Guessing here would act in the workspace as the wrong agent."""
    other = {**AGENT, "agent_id": "a-2", "agent_handle": "qa_agent"}
    _write(client_home, {"agents": {"a-1": AGENT, "a-2": other}})
    assert papaya.identity() is None


def test_a_lone_agent_without_a_pin_is_still_us(client_home) -> None:
    """Older clients wrote no `connect` block; one agent is unambiguous."""
    _write(client_home, {"agents": {"a-1": AGENT}})
    who = papaya.identity()
    assert who is not None
    assert who.handle == "engineering_agent"


def test_identity_is_addressed_by_handle_not_display_name(client_home) -> None:
    """People and agents are addressed by handle; the display name is a fallback."""
    _write(client_home, {"agents": {"a-1": AGENT}, "connect": {"agent_id": "a-1"}})
    assert papaya.status()["addressed"] == "@engineering_agent"


def test_a_handleless_agent_falls_back_to_its_name(client_home) -> None:
    _write(client_home, {"agents": {"a-1": {**AGENT, "agent_handle": ""}}})
    who = papaya.identity()
    assert who is not None
    assert who.addressed == "Engineering Agent"


def test_corrupt_config_reports_absent_rather_than_crashing(client_home, monkeypatch) -> None:
    """A half-written config must not take the whole runtime down at preflight."""
    monkeypatch.setattr(papaya, "installed", lambda: None)
    (client_home / "config.json").write_text("{not json", encoding="utf-8")
    assert papaya.identity() is None
    assert papaya.status()["state"] == "absent"


def test_connect_argv_prefers_the_installed_client(monkeypatch) -> None:
    monkeypatch.setattr(papaya, "installed", lambda: "/usr/local/bin/papaya-agent")
    assert papaya.connect_argv() == [
        "/usr/local/bin/papaya-agent",
        "connect",
        "--harness",
        "claude",
    ]


def test_connect_argv_falls_back_to_the_npm_shim(monkeypatch) -> None:
    """With no client installed there is still a way in, so preflight is never stuck."""
    monkeypatch.setattr(papaya, "installed", lambda: None)
    argv = papaya.connect_argv(harness="codex")
    assert argv[: len(papaya.BOOTSTRAP)] == list(papaya.BOOTSTRAP)
    assert argv[-2:] == ["--harness", "codex"]


def test_connect_reports_a_timeout_without_raising(client_home, monkeypatch) -> None:
    """A person who never clicks Approve must degrade, not break the session."""
    import subprocess

    def boom(argv, *, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(papaya, "installed", lambda: "/usr/local/bin/papaya-agent")
    monkeypatch.setattr(papaya, "_run", boom)
    result = papaya.connect(timeout=1)
    assert result["ok"] is False
    assert result["reason"] == "timeout"


def test_context_returns_none_when_the_client_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(papaya, "installed", lambda: None)
    assert papaya.context() is None


# ── Finding a connection wherever it was made ───────────────────────────────
#
# A connection can be established two ways: `papaya-agent connect` in a terminal,
# which writes `~/.papaya-agent`, or the Papaya desktop app, which runs the same
# client as a child process with `PAPAYA_AGENT_HOME` pointed into its own
# application-support directory. That variable only exists inside the process the
# app launched, so a shell the person opens themselves inherits nothing — and a
# runtime that only checked `~/.papaya-agent` told a genuinely connected user they
# were not connected. Found on a real machine on 2026-09-15.


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Two candidate homes and no explicit override, as on a real machine."""
    monkeypatch.delenv(papaya.HOME_ENV, raising=False)
    cli_home = tmp_path / "cli"
    desktop = tmp_path / "desktop"
    cli_home.mkdir()
    desktop.mkdir()
    monkeypatch.setattr(papaya.Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
    (tmp_path / "fakehome").mkdir()
    monkeypatch.setattr(papaya, "_desktop_home", lambda: desktop)
    monkeypatch.setenv(papaya.CLIENT_HOME_ENV, str(cli_home))
    return cli_home, desktop


def test_a_desktop_connection_is_found_without_the_apps_environment(homes, monkeypatch) -> None:
    """The app sets PAPAYA_AGENT_HOME for itself; our shell never sees it."""
    cli_home, desktop = homes
    monkeypatch.delenv(papaya.CLIENT_HOME_ENV, raising=False)
    _write(desktop, {"agents": {"a-1": AGENT}, "connect": {"agent_id": "a-1"}})
    who = papaya.identity()
    assert who is not None
    assert who.handle == "engineering_agent"
    assert papaya.client_home() == desktop


def test_the_client_home_env_is_honored_when_it_is_set(homes) -> None:
    cli_home, _ = homes
    _write(cli_home, {"agents": {"a-1": AGENT}, "connect": {"agent_id": "a-1"}})
    assert papaya.client_home() == cli_home


def test_the_most_recently_pinned_connection_wins(homes) -> None:
    """A machine can hold both; the one the person chose last is who they are."""
    cli_home, desktop = homes
    older = {**AGENT, "agent_handle": "old_agent"}
    _write(
        cli_home,
        {
            "agents": {"a-1": older},
            "connect": {"agent_id": "a-1", "updated_at": "2026-01-01T00:00:00Z"},
        },
    )
    _write(
        desktop,
        {
            "agents": {"a-2": {**AGENT, "agent_id": "a-2", "agent_handle": "new_agent"}},
            "connect": {"agent_id": "a-2", "updated_at": "2026-09-15T20:00:00Z"},
        },
    )
    who = papaya.identity()
    assert who is not None and who.handle == "new_agent"


def test_an_explicit_override_is_the_whole_world(tmp_path, monkeypatch) -> None:
    """So a test says exactly what exists, and never reads the real machine."""
    only = tmp_path / "only"
    only.mkdir()
    monkeypatch.setenv(papaya.HOME_ENV, str(only))
    monkeypatch.setenv(papaya.CLIENT_HOME_ENV, str(tmp_path / "ignored"))
    assert papaya.candidate_homes() == [only]


def test_status_names_everywhere_it_looked(homes) -> None:
    """An unconnected answer has to be checkable, not just asserted."""
    searched = papaya.status()["searched"]
    assert len(searched) >= 2
    assert str(homes[1]) in searched
