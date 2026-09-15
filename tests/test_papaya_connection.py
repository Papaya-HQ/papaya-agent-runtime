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


def _task(conn, title: str = "Add a health endpoint") -> int:
    from papaya_agent_runtime.state import store

    repo_id = store.add_repo(
        conn, name="app", origin="/o", local_path="/l", default_branch="main", base_sha="a"
    )
    run_id = store.create_run(conn, "ship it")
    return store.add_task(conn, run_id=run_id, title=title, repo_id=repo_id, ends_at="done")


def test_linking_a_task_to_a_work_item_survives_a_reread(ppy_home) -> None:
    from papaya_agent_runtime.state import init_db

    conn = init_db()
    task_id = _task(conn)
    papaya.link_task(
        conn,
        task_id,
        work_item="PAP-148",
        url="https://papaya.example/w/PAP-148",
        title="Health endpoint for the API",
    )
    link = papaya.task_link(conn, task_id)
    assert link == {
        "work_item": "PAP-148",
        "url": "https://papaya.example/w/PAP-148",
        "title": "Health endpoint for the API",
    }


def test_an_unlinked_task_has_no_link(ppy_home) -> None:
    from papaya_agent_runtime.state import init_db

    conn = init_db()
    task_id = _task(conn, title="Small fix")
    assert papaya.task_link(conn, task_id) is None
    assert papaya.link_sentence(None) == ""


def test_the_link_sentence_describes_the_item_rather_than_citing_an_id() -> None:
    """A reader outside the workspace cannot look up a bare identifier."""
    sentence = papaya.link_sentence(
        {"work_item": "PAP-148", "url": "https://papaya.example/w/PAP-148", "title": "QA sweep"}
    )
    assert '"QA sweep"' in sentence
    assert "https://papaya.example/w/PAP-148" in sentence
