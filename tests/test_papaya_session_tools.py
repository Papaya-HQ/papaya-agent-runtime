"""A session connected as a Papaya agent can act as that agent, however it was opened.

2026-09-17: the machine was connected as @engineering_agent and `ppy serve`'s turns
read and commented on work items, but a manager session opened in the runtime
directory had no Papaya tools at all and needed a one-off headless turn to read six
tickets. The same connection now gives both the same tools.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from papaya_agent_runtime import hooks, papaya
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.manager.launch import MANAGER_TURN_ENV

SERVER = {
    "command": "/opt/ppy/.venv/bin/python",
    "args": ["-m", "papaya_agent_client", "mcp", "passthrough", "--agent", "engineering-agent"],
    "env": {"PAPAYA_AGENT_HOME": "/home/papaya", "PAPAYA_API_URL": "https://api.example"},
}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A connected machine, a runtime directory, and a Claude Code user config."""
    home = tmp_path / ".papaya-agent"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps(
            {
                "session": {"refresh_token": "t"},
                "agents": {
                    "a-1": {
                        "agent_id": "a-1",
                        "agent_name": "Engineering Agent",
                        "agent_handle": "engineering_agent",
                        "workspace_id": "w-1",
                        "connection_id": "c-1",
                    }
                },
                "connect": {"agent_id": "a-1", "harness": "claude"},
            }
        ),
        "utf-8",
    )
    monkeypatch.setenv(papaya.HOME_ENV, str(home))
    root = tmp_path / "runtime"
    root.mkdir()
    user_config = tmp_path / ".claude.json"
    monkeypatch.setenv(papaya.CLAUDE_USER_CONFIG_ENV, str(user_config))
    monkeypatch.setattr(papaya.shutil, "which", lambda name, **_kw: f"/bin/{name}")
    calls: list[tuple[list[str], dict]] = []

    def run(argv, *, timeout, env=None, cwd=None):
        calls.append((argv, {"env": env, "cwd": cwd}))
        if "runner-config" in argv:
            out = json.dumps({"mcpServers": {"papaya": SERVER}})
            return subprocess.CompletedProcess(argv, 0, out, "")
        if argv[1:3] == ["mcp", "add-json"]:
            data = json.loads(user_config.read_text()) if user_config.exists() else {}
            project = data.setdefault("projects", {}).setdefault(str(root.resolve()), {})
            project.setdefault("mcpServers", {})[argv[3]] = json.loads(argv[4])
            user_config.write_text(json.dumps(data))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(papaya, "_run", run)
    return {"root": root, "calls": calls, "home": home, "user_config": user_config}


def test_tools_are_installed_for_the_directory_from_the_clients_own_config(world) -> None:
    assert not papaya.session_tools_ready(world["root"])

    result = papaya.install_session_tools(world["root"])

    assert result["ok"] and result["addressed"] == "@engineering_agent"
    assert papaya.session_tools_ready(world["root"]) is False  # the command path is fake
    assert papaya.session_server(world["root"]) == SERVER
    runner_config, remove, add = (argv for argv, _ in world["calls"] if argv[0] != "git")
    assert "runner-config" in runner_config and "claude-code" in runner_config
    # The client is pointed at the home the connection lives in, not this shell's.
    [(_, extra)] = [c for c in world["calls"] if "runner-config" in c[0]]
    assert extra["env"][papaya.CLIENT_HOME_ENV] == str(world["home"])
    assert remove[1:] == ["mcp", "remove", "papaya", "--scope", "local"]
    assert add[1:3] == ["mcp", "add-json"] and add[-2:] == ["--scope", "local"]


def test_a_server_whose_interpreter_moved_is_not_ready(world, tmp_path) -> None:
    python = tmp_path / "python"
    python.write_text("")
    world["user_config"].write_text(
        json.dumps(
            {
                "projects": {
                    str(world["root"].resolve()): {
                        "mcpServers": {"papaya": {**SERVER, "command": str(python)}}
                    }
                }
            }
        )
    )
    assert papaya.session_tools_ready(world["root"])
    python.unlink()
    assert not papaya.session_tools_ready(world["root"])


def test_an_unconnected_machine_is_told_to_connect_and_nothing_is_written(
    world, monkeypatch
) -> None:
    monkeypatch.setattr(papaya, "status", lambda: {"state": "signed_in", "addressed": None})

    result = papaya.install_session_tools(world["root"])

    assert not result["ok"] and result["reason"] == "not_connected"
    assert world["calls"] == []


def test_a_client_that_refuses_is_reported_with_its_words(world, monkeypatch) -> None:
    def refuse(argv, *, timeout, env=None, cwd=None):
        return subprocess.CompletedProcess(argv, 1, "", "No connection is configured")

    monkeypatch.setattr(papaya, "_run", refuse)

    result = papaya.install_session_tools(world["root"])

    assert result == {
        "ok": False,
        "reason": "client_failed",
        "detail": "runner-config exited 1: No connection is configured",
    }


def test_session_start_sets_the_tools_up_and_says_how_to_load_them(world, monkeypatch) -> None:
    monkeypatch.delenv("PPY_DEV", raising=False)
    monkeypatch.delenv(MANAGER_TURN_ENV, raising=False)
    monkeypatch.setattr("papaya_agent_runtime.manager.launch.repo_root", lambda: str(world["root"]))

    said = hooks.papaya_tools_context()

    assert said is not None and "`/mcp`" in said and "@engineering_agent" in said
    assert papaya.session_server(world["root"]) == SERVER

    monkeypatch.setattr(papaya, "session_tools_ready", lambda root: True)
    assert hooks.papaya_tools_context() is None
    monkeypatch.setattr(papaya, "session_tools_ready", lambda root: False)
    monkeypatch.setenv(MANAGER_TURN_ENV, "1")
    assert hooks.papaya_tools_context() is None


def test_the_cli_installs_and_checks(world, monkeypatch, capsys) -> None:
    monkeypatch.setattr("papaya_agent_runtime.manager.launch.repo_root", lambda: str(world["root"]))

    assert main(["papaya", "tools", "--check"]) == 1
    assert main(["papaya", "tools"]) == 0
    assert "@engineering_agent" in capsys.readouterr().out


def test_context_reads_the_home_the_connection_lives_in(world, monkeypatch) -> None:
    seen = {}

    def run(argv, *, timeout, env=None, cwd=None):
        seen.update(argv=argv, env=env)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"block": "hi"}), "")

    monkeypatch.setattr(papaya, "_run", run)

    assert papaya.context() == {"block": "hi"}
    assert seen["env"][papaya.CLIENT_HOME_ENV] == str(world["home"])
    assert seen["argv"][-2:] == ["context", "--json"]
