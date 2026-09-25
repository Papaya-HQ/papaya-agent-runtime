"""`ppy setup`: the guided first run, driven through fake terminals and fake programs.

Nothing here signs anything in. Every program setup would run goes through a
`FakeShell` that answers from a table and records what it was asked; every question
goes through a `ScriptedPicker`; the Papaya connect flow is a fake that writes the
client config a real connect would leave behind.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from papaya_agent_runtime import cli, papaya, readiness, repos, serve
from papaya_agent_runtime.setup import discovery, guided, wizard

ROOT = Path(__file__).resolve().parents[1]

# ── fakes ───────────────────────────────────────────────────────────────────


class FakeShell(guided.Shell):
    """A machine described by a table: which programs exist and what they answer."""

    def __init__(self, *, missing=(), failing=(), answers=None, on_attach=None):
        self.missing = set(missing)
        #: argv prefixes (tuples) that exit 1.
        self.failing = set(failing)
        self.answers = dict(answers or {})
        self.on_attach = on_attach or {}
        self.captured: list[list[str]] = []
        self.attached: list[list[str]] = []

    def which(self, name):
        return None if name in self.missing else f"/usr/bin/{name}"

    def capture(self, argv, timeout=60.0):
        self.captured.append(list(argv))
        for prefix in self.failing:
            if tuple(argv[: len(prefix)]) == prefix:
                return 1, "not signed in"
        for prefix, answer in self.answers.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return 0, answer
        return 0, ""

    def attach(self, argv):
        self.attached.append(list(argv))
        fix = self.on_attach.get(tuple(argv))
        if fix is not None:
            fix(self)
        return 0


class ScriptedPicker(guided.Picker):
    """Answers from a script; anything unscripted fails the test."""

    def __init__(self, *, switch=False, owners=(), repos=None, urls=(), one=None):
        self.switch = switch
        self.owners = list(owners)
        self.repo_answers = dict(repos or {})
        self.urls = list(urls)
        self.one = one
        self.asked: list[str] = []
        self.shown_ticks: list[tuple[str, set[str]]] = []

    def yes_no(self, question, default=False):
        self.asked.append(question)
        return self.switch

    def owner(self, owners, selected, default):
        self.asked.append("owner")
        return self.owners.pop(0) if self.owners else guided.FINISHED

    def repos(self, owner, rows, ticked):
        self.asked.append(f"repos:{owner}")
        self.shown_ticks.append((owner, set(ticked)))
        answers = self.repo_answers.get(owner, [])
        return answers.pop(0) if answers else None

    def url(self):
        self.asked.append("url")
        return self.urls.pop(0)

    def one_of(self, kind, choices):
        self.asked.append(f"one_of:{kind}")
        return self.one


class Silent(guided.Picker):
    """A picker for runs that must ask nothing."""

    def __getattribute__(self, name):
        if name in ("yes_no", "owner", "repos", "url", "one_of"):
            raise AssertionError(f"setup asked something ({name}) it should not have")
        return super().__getattribute__(name)


def _write_connection(home: Path, *, agent_id="a-1", name="Ada", handle="ada", **entry):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps(
            {
                "agents": {
                    agent_id: {
                        "agent_id": agent_id,
                        "agent_name": name,
                        "agent_handle": handle,
                        "workspace_id": "w-1",
                        "connection_id": "c-1",
                        **entry,
                    }
                },
                "connect": {"agent_id": agent_id, "harness": "claude"},
            }
        ),
        encoding="utf-8",
    )


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A PPY_HOME, an empty Papaya home, a built environment and in-memory repositories."""
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    client = tmp_path / "papaya-client"
    client.mkdir()
    monkeypatch.setenv(papaya.HOME_ENV, str(client))
    monkeypatch.setattr(readiness, "environment_imports", lambda env: True)

    report = {
        "harnesses": [
            {"name": "claude", "path": "/usr/bin/claude", "available": True, "detail": ""},
            {"name": "codex", "path": None, "available": False, "detail": ""},
        ],
        "requirements": [],
        "companions": [],
    }
    monkeypatch.setattr(wizard, "discover", lambda: report)
    monkeypatch.setattr(wizard, "usable_harnesses", discovery.usable_harnesses)

    registered: list[dict] = []
    added: list[str] = []

    def add_repo(url, name=None, forge_url=None):
        slug = repos.forge_slug(url) or url
        added.append(url)
        registered.append({"name": slug.split("/")[-1], "forge_url": url, "origin": url})
        return repos.AddedRepo(
            name=slug.split("/")[-1],
            origin=url,
            local_path="/tmp/x",
            default_branch="main",
            base_sha="0" * 40,
            forge_url=url,
        )

    monkeypatch.setattr(repos, "add_repo", add_repo)
    monkeypatch.setattr(repos, "list_repos", lambda: list(registered))

    class World:
        pass

    w = World()
    w.client = client
    w.registered = registered
    w.added = added
    w.connects: list[dict] = []

    def connect_ok(**kwargs):
        w.connects.append(kwargs)
        _write_connection(client)
        return {"ok": True}

    w.connect_ok = connect_ok
    w.connect = connect_ok
    return w


def _setup(world, options=None, *, shell=None, picker=None, platform="darwin", env=None, **kw):
    out = io.StringIO()
    code = guided.Setup(
        options or guided.Options(skip_tools=True),
        shell=shell or FakeShell(),
        picker=picker or Silent(),
        platform=platform,
        env={} if env is None else env,
        out=out,
        connect=kw.pop("connect", world.connect),
        wsl=lambda: False,
        **kw,
    ).run()
    return code, out.getvalue().splitlines()


def _set_up_fully(world):
    _write_connection(world.client)
    world.registered.append(
        {
            "name": "api",
            "forge_url": "https://github.com/acme/api",
            "origin": "https://github.com/acme/api",
        }
    )
    wizard.run_setup(non_interactive=True)


def _ok(name: str, detail: str) -> str:
    return guided.step_line(guided.OK, name, detail)


def _doing(name: str, detail: str) -> str:
    return guided.step_line(guided.DOING, name, detail)


def _framed(label: str, *said: str) -> list[str]:
    """What a child program's output looks like in setup's own output."""
    return ["", guided.rule(label), *said, guided.rule(), ""]


ADA = _ok(guided.PAPAYA, "Connected as Ada (@ada)")
FINAL = ["", "Done. Start it with:", "  ./bin/ppy serve"]

TICKS = [
    "✓ Machine       macOS, git, uv and the runtime's environment",
    "✓ Claude Code   Signed in",
    "✓ GitHub        Signed in",
    "✓ Papaya        Connected as Ada (@ada)",
    "✓ Repositories  1 repository (./bin/ppy setup --repos to change)",
    *FINAL,
]

GH_HELPER = ("git", "config", "--global", "--get-all", "credential.https://github.com.helper")


def _healthy(**kw) -> FakeShell:
    return FakeShell(answers={GH_HELPER: "!/usr/bin/gh auth git-credential"}, **kw)


# ── a machine already set up ────────────────────────────────────────────────


def test_a_fully_set_up_machine_says_one_line_per_step_and_asks_only_to_switch(world):
    _set_up_fully(world)
    shell = _healthy()
    picker = ScriptedPicker(switch=False)

    code, lines = _setup(world, shell=shell, picker=picker)

    assert code == 0
    assert lines == TICKS
    assert picker.asked == ["Switch to another agent?"]
    assert shell.attached == []
    assert world.connects == [] and world.added == []


def test_an_older_papaya_client_is_updated_before_papaya_is_checked_and_said_in_one_line(world):
    _set_up_fully(world)
    updated = {"state": "updated", "line": "Updated papaya-agent 0.17.0 → 0.18.1"}

    code, lines = _setup(
        world, shell=_healthy(), picker=ScriptedPicker(), client_currency=lambda: updated
    )

    assert code == 0
    papaya_at = TICKS.index("✓ Papaya        Connected as Ada (@ada)")
    assert lines == [
        *TICKS[:papaya_at],
        "✓ Papaya        Updated papaya-agent 0.17.0 → 0.18.1",
        *TICKS[papaya_at:],
    ]


def test_a_papaya_client_that_could_not_be_updated_is_one_line_and_setup_carries_on(world):
    _set_up_fully(world)
    failed = {
        "state": "failed",
        "line": "papaya-agent 0.17.0 is older than 0.18.1 and could not be updated: "
        "run uv tool install --force papaya-agent-client==0.18.1",
    }

    code, lines = _setup(
        world, shell=_healthy(), picker=ScriptedPicker(), client_currency=lambda: failed
    )

    assert code == 0
    assert (
        "! Papaya        papaya-agent 0.17.0 is older than 0.18.1 and could not be updated: "
        "run uv tool install --force papaya-agent-client==0.18.1"
    ) in lines
    assert "✓ Papaya        Connected as Ada (@ada)" in lines


def test_a_current_papaya_client_says_nothing(world):
    _set_up_fully(world)
    current = {"state": "current", "line": None}

    code, lines = _setup(
        world, shell=_healthy(), picker=ScriptedPicker(), client_currency=lambda: current
    )

    assert code == 0 and lines == TICKS


def test_a_fully_set_up_machine_run_by_a_script_asks_nothing(world):
    _set_up_fully(world)
    options = guided.Options(interactive=False, repos=("acme/api",), skip_tools=True)

    code, lines = _setup(world, options, shell=_healthy(), picker=Silent())

    assert code == 0
    assert lines == TICKS
    assert world.added == []  # already registered, matched by its slug


def test_switching_agents_on_a_rerun_connects_again(world):
    """Ending on the agent already connected is not reported as a new connection."""
    _set_up_fully(world)
    picker = ScriptedPicker(switch=True)

    code, lines = _setup(world, shell=_healthy(), picker=picker)

    assert code == 0
    assert len(world.connects) == 1
    assert lines.count(ADA) == 1
    assert _ok(guided.PAPAYA, "Still connected as Ada (@ada)") in lines


def test_an_interactive_switch_is_one_connect_on_the_terminal(world):
    """The owner, 2026-09-24: two sign-ins, and no chance to pick the agent. On a
    terminal the client asks the workspace and agent inside the one sign-in."""
    _set_up_fully(world)

    def connect(**kwargs):
        world.connects.append(kwargs)
        _write_connection(world.client, agent_id="a-2", name="Bea", handle="bea")
        return {"ok": True, "agent_choice": "asked", "workspace": "Papaya HQ"}

    picker = ScriptedPicker(switch=True)
    code, lines = _setup(world, shell=_healthy(), picker=picker, connect=connect)

    assert code == 0
    assert len(world.connects) == 1
    assert world.connects[0]["interactive"] is True
    assert world.connects[0]["agent"] is None and world.connects[0]["workspace"] is None
    assert picker.asked == ["Switch to another agent?"]  # no second picker of our own
    assert lines[3:10] == [
        ADA,
        _doing(guided.PAPAYA, "Connecting: approve in the browser that opens…"),
        *_framed("papaya-agent connect"),
        _ok(guided.PAPAYA, "Connected as Bea (@bea)"),
    ]


def test_a_switch_with_only_one_agent_says_so_instead_of_claiming_a_switch(world):
    _set_up_fully(world)

    def connect(**kwargs):
        world.connects.append(kwargs)
        _write_connection(world.client)  # the same agent, freshly pinned
        return {"ok": True, "agent_choice": "only", "workspace": "Papaya HQ"}

    code, lines = _setup(
        world, shell=_healthy(), picker=ScriptedPicker(switch=True), connect=connect
    )

    assert code == 0
    assert (
        _ok(
            guided.PAPAYA,
            "Ada (@ada) is the only agent you can connect in Papaya HQ. To use another, create "
            "it in Papaya (Agents → New agent), then run ./bin/ppy setup again.",
        )
        in lines
    )
    assert lines.count(ADA) == 1  # the "before" tick only
    assert not any("Still connected" in line for line in lines)


def test_a_switch_that_does_not_finish_says_not_switched(world, capsys):
    _set_up_fully(world)

    def connect(**kwargs):
        world.connects.append(kwargs)
        return {"ok": False, "reason": "failed", "detail": "exit 1"}

    code, lines = _setup(
        world, shell=_healthy(), picker=ScriptedPicker(switch=True), connect=connect
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "✗ Not switched: still connected as Ada (@ada) (exit 1)." in err
    assert lines.count(ADA) == 1
    assert guided.DONE not in lines


def test_a_script_connects_captured_with_the_agent_it_was_given(world):
    options = guided.Options(
        interactive=False, agent="Bea", workspace="papaya-hq", repos=("acme/api",), skip_tools=True
    )

    code, _ = _setup(world, options, shell=_healthy(), picker=Silent())

    assert code == 0
    assert len(world.connects) == 1
    call = world.connects[0]
    assert call["interactive"] is False
    assert (call["agent"], call["workspace"]) == ("Bea", "papaya-hq")


def test_setup_asks_the_client_for_its_quiet_connect(world):
    """Passed as asked; `papaya.connect_argv` drops it for a client that lacks it."""
    world.registered.append({"name": "api", "forge_url": "https://github.com/acme/api"})
    code, _ = _setup(world, shell=_healthy(), picker=ScriptedPicker())

    assert code == 0
    assert world.connects[0]["quiet"] is True


# ── how it looks ────────────────────────────────────────────────────────────


class Terminal(io.StringIO):
    """Output that says it is a terminal."""

    def isatty(self):
        return True


def _on(out, world, *, shell=None, env=None):
    code = guided.Setup(
        guided.Options(skip_tools=True),
        shell=shell or _healthy(),
        picker=ScriptedPicker(),
        platform="darwin",
        env={} if env is None else env,
        out=out,
        connect=world.connect,
        wsl=lambda: False,
    ).run()
    return code, out.getvalue()


def test_on_a_terminal_marks_are_coloured_names_bold_and_the_command_stands_out(world):
    _set_up_fully(world)

    code, text = _on(Terminal(), world)

    assert code == 0
    lines = text.splitlines()
    assert lines[0] == (
        "\x1b[32m✓\x1b[0m \x1b[1mMachine\x1b[0m       macOS, git, uv and the runtime's environment"
    )
    assert lines[3] == "\x1b[32m✓\x1b[0m \x1b[1mPapaya\x1b[0m        Connected as Ada (@ada)"
    assert lines[-3:] == [
        "",
        "\x1b[1mDone.\x1b[0m Start it with:",
        "  \x1b[1;36m./bin/ppy serve\x1b[0m",
    ]


def test_on_a_terminal_a_sign_in_is_yellow_and_its_output_sits_between_dim_rules(world):
    _set_up_fully(world)
    status = ("claude", "auth", "status")
    shell = _healthy(
        failing={status},
        on_attach={("claude", "auth", "login"): lambda sh: sh.failing.discard(status)},
    )

    code, text = _on(Terminal(), world, shell=shell)

    assert code == 0
    assert text.splitlines()[1:7] == [
        "\x1b[33m→\x1b[0m \x1b[1mClaude Code\x1b[0m   Not signed in; starting its sign-in…",
        "",
        f"\x1b[2m{guided.rule('claude auth login')}\x1b[0m",
        f"\x1b[2m{guided.rule()}\x1b[0m",
        "",
        "\x1b[32m✓\x1b[0m \x1b[1mClaude Code\x1b[0m   Signed in",
    ]


def test_on_a_terminal_a_stop_is_a_red_cross(world, monkeypatch):
    err = Terminal()
    monkeypatch.setattr(sys, "stderr", err)

    code, _ = _on(Terminal(), world, shell=_healthy(missing={"claude"}))

    assert code == 1
    assert err.getvalue().startswith("\x1b[31m✗\x1b[0m Claude Code is not installed.")


def test_no_color_leaves_a_terminal_plain(world, monkeypatch):
    _set_up_fully(world)
    err = Terminal()
    monkeypatch.setattr(sys, "stderr", err)

    code, text = _on(Terminal(), world, env={"NO_COLOR": "1"})

    assert code == 0
    assert text.splitlines() == TICKS
    assert "\x1b" not in text

    code, _ = _on(Terminal(), world, shell=_healthy(missing={"claude"}), env={"NO_COLOR": "1"})
    assert code == 1
    assert err.getvalue().startswith("✗ Claude Code is not installed.")


def test_piped_output_has_no_escape_codes_at_all(world, capsys):
    """Every kind of line: a step under way, framed child output, ticks, the end, a stop."""
    status = ("claude", "auth", "status")
    shell = _healthy(
        failing={status},
        on_attach={("claude", "auth", "login"): lambda sh: sh.failing.discard(status)},
    )
    world.registered.append({"name": "api", "forge_url": "https://github.com/acme/api"})

    code, text = _on(io.StringIO(), world, shell=shell)
    assert code == 0
    assert guided.rule("papaya-agent connect") in text and "Done." in text

    code, _ = _on(io.StringIO(), world, shell=_healthy(missing={"claude"}))
    assert code == 1
    captured = capsys.readouterr()
    assert "\x1b" not in text + captured.out + captured.err
    assert captured.err.startswith("✗ Claude Code is not installed.")


def test_a_child_that_fails_still_closes_its_rule(world):
    def boom(**kwargs):
        raise RuntimeError("client crashed")

    out = io.StringIO()
    setup = guided.Setup(guided.Options(), shell=_healthy(), out=out, connect=boom, env={})
    with pytest.raises(RuntimeError):
        setup._connect_papaya()

    assert out.getvalue().splitlines()[-2:] == [guided.rule(), ""]


# ── picking up where it stopped ─────────────────────────────────────────────


def test_quitting_at_papaya_resumes_at_papaya(world, capsys):
    shell = _healthy()

    def nobody_approved(**kwargs):
        world.connects.append(kwargs)
        return {"ok": False, "reason": "timeout"}

    code, lines = _setup(world, shell=shell, picker=ScriptedPicker(), connect=nobody_approved)
    assert code == 1
    assert "Nobody approved the Papaya sign-in in time" in capsys.readouterr().err
    assert lines[:3] == TICKS[:3]
    assert world.registered == []

    picker = ScriptedPicker(owners=["acme"], repos={"acme": [{"acme/api"}]})
    shell.answers[("gh", "api", "user")] = json.dumps({"login": "acme"})
    shell.answers[("gh", "api", "user/orgs")] = "[]"
    shell.answers[("gh", "repo", "list")] = json.dumps(
        [{"nameWithOwner": "acme/api", "description": "", "updatedAt": "2026-09-01"}]
    )
    code, lines = _setup(world, shell=shell, picker=picker)

    assert code == 0
    assert lines[:3] == TICKS[:3]  # the first three steps only tick
    assert shell.attached == []
    assert len(world.connects) == 2
    assert ADA in lines
    assert world.added == ["https://github.com/acme/api"]
    assert lines[-3:] == FINAL


# ── step 1: the machine ─────────────────────────────────────────────────────


def test_native_windows_stops_with_the_wsl_steps_and_touches_nothing(world, capsys):
    shell = FakeShell()

    code, lines = _setup(world, shell=shell, platform="win32")

    assert code == 1
    err = capsys.readouterr().err
    assert "wsl --install -d Ubuntu" in err and "fcntl" in err
    assert lines == []
    assert shell.captured == [] and shell.attached == []


@pytest.mark.parametrize(
    ("missing", "expect"),
    [("git", "xcode-select --install"), ("uv", guided.UV_INSTALL)],
)
def test_a_missing_tool_stops_with_its_one_install_line(world, capsys, missing, expect):
    code, _ = _setup(world, shell=FakeShell(missing={missing}))
    assert code == 1
    assert expect in capsys.readouterr().err


def test_a_broken_environment_is_built(world, monkeypatch):
    built = []
    state = {"ok": False}
    monkeypatch.setattr(readiness, "environment_imports", lambda env: state["ok"])

    def sync():
        built.append(1)
        state["ok"] = True
        return 0

    _set_up_fully(world)
    code, lines = _setup(world, shell=_healthy(), picker=ScriptedPicker(), sync_env=sync)

    assert code == 0
    assert built == [1]
    assert lines[:6] == [
        "→ Machine       Building the runtime's environment…",
        *_framed("uv sync"),
        TICKS[0],
    ]


def test_an_environment_built_from_an_older_lockfile_is_updated_in_step_one(world, monkeypatch):
    """2026-09-24: a pull added a dependency and step 1 ticked the old environment."""
    built = []
    state = {"current": False}
    monkeypatch.setattr(readiness, "environment_current", lambda env: state["current"])

    def sync():
        built.append(1)
        state["current"] = True
        return 0

    _set_up_fully(world)
    code, lines = _setup(world, shell=_healthy(), picker=ScriptedPicker(), sync_env=sync)

    assert code == 0
    assert built == [1]
    updating = _doing(guided.MACHINE, "Updating the runtime's environment…")
    assert lines[:6] == [updating, *_framed("uv sync"), TICKS[0]]


def test_a_stale_environment_held_by_a_running_serve_stops_step_one(world, monkeypatch, capsys):
    from papaya_agent_runtime import envsync

    monkeypatch.setattr(readiness, "environment_current", lambda env: False)

    code, lines = _setup(world, shell=_healthy(), sync_env=lambda: envsync.REFUSED)

    assert code == 1
    assert lines == [
        _doing(guided.MACHINE, "Updating the runtime's environment…"),
        *_framed("uv sync"),
    ]
    err = capsys.readouterr().err
    assert "./bin/ppy supervisor stop" in err and "./bin/ppy setup again" in err


def test_a_missing_picker_package_stops_in_step_one_not_at_a_prompt(world, monkeypatch, capsys):
    """The owner's crash: `import questionary` failed inside the first `yes_no`."""
    monkeypatch.setitem(sys.modules, "questionary", None)  # so importing it raises
    _set_up_fully(world)

    code, lines = _setup(world, shell=_healthy(), picker=Silent())

    assert code == 1
    assert lines == []
    err = capsys.readouterr().err.strip()
    assert err == (
        "✗ The runtime's environment is missing questionary: run ./bin/ppy env sync, "
        "then ./bin/ppy setup again"
    )


def test_a_non_interactive_setup_does_not_need_the_picker(world, monkeypatch):
    monkeypatch.setitem(sys.modules, "questionary", None)
    _set_up_fully(world)

    code, lines = _setup(
        world, guided.Options(interactive=False, skip_tools=True), shell=_healthy()
    )

    assert code == 0
    assert lines[0] == TICKS[0]


def test_wsl_is_named(world):
    _set_up_fully(world)
    out = io.StringIO()
    guided.Setup(
        guided.Options(skip_tools=True),
        shell=_healthy(),
        picker=ScriptedPicker(),
        platform="linux",
        env={"DISPLAY": ":0"},
        out=out,
        connect=world.connect,
        wsl=lambda: True,
    ).run()
    assert out.getvalue().splitlines()[0] == _ok(
        guided.MACHINE, "Linux (WSL2), git, uv and the runtime's environment"
    )


def test_wsl_is_read_from_the_kernel_version():
    assert guided.is_wsl(lambda: "Linux version 5.15.153.1-microsoft-standard-WSL2")
    assert not guided.is_wsl(lambda: "Linux version 6.8.0-45-generic")


# ── step 2 and 3: sign-ins with the terminal attached ───────────────────────


def test_claude_code_not_signed_in_runs_its_sign_in_on_the_terminal(world):
    _set_up_fully(world)
    status = ("claude", "auth", "status")
    shell = _healthy(
        failing={status},
        on_attach={("claude", "auth", "login"): lambda sh: sh.failing.discard(status)},
    )

    code, lines = _setup(world, shell=shell, picker=ScriptedPicker())

    assert code == 0
    assert shell.attached == [["claude", "auth", "login"]]
    assert lines[1:7] == [
        "→ Claude Code   Not signed in; starting its sign-in…",
        *_framed("claude auth login"),
        "✓ Claude Code   Signed in",
    ]


def test_claude_code_still_not_signed_in_stops(world, capsys):
    shell = _healthy(failing={("claude", "auth", "status")})
    code, _ = _setup(world, shell=shell, picker=ScriptedPicker())
    assert code == 1
    assert "run claude auth login" in capsys.readouterr().err


def test_claude_code_missing_stops_with_the_install_line(world, capsys):
    code, _ = _setup(world, shell=_healthy(missing={"claude"}))
    assert code == 1
    assert guided.CLAUDE_INSTALL in capsys.readouterr().err


def test_a_script_never_attaches_a_terminal(world, capsys):
    shell = _healthy(failing={("claude", "auth", "status")})
    options = guided.Options(interactive=False, repos=("acme/api",), skip_tools=True)

    code, _ = _setup(world, options, shell=shell)

    assert code == 1
    assert shell.attached == []
    err = capsys.readouterr().err.strip()
    assert err == "✗ Claude Code is not signed in: run claude auth login"


def test_github_not_signed_in_runs_gh_auth_login_then_setup_git(world):
    _set_up_fully(world)
    status = ("gh", "auth", "status")
    login = ("gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https")
    shell = FakeShell(failing={status}, on_attach={login: lambda sh: sh.failing.discard(status)})

    code, lines = _setup(world, shell=shell, picker=ScriptedPicker())

    assert code == 0
    assert shell.attached == [list(login)]
    assert ["gh", "auth", "setup-git", "--hostname", "github.com"] in shell.captured
    assert lines[2:8] == [
        _doing(guided.GITHUB, "Not signed in; starting gh auth login…"),
        *_framed("gh auth login"),
        _ok(guided.GITHUB, "Signed in"),
    ]


def test_git_already_pushing_with_gh_is_left_alone(world):
    _set_up_fully(world)
    shell = _healthy()
    _setup(world, shell=shell, picker=ScriptedPicker())
    assert not any(argv[:3] == ["gh", "auth", "setup-git"] for argv in shell.captured)


# ── step 4: Papaya ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("env", "platform", "device"),
    [
        ({"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"}, "darwin", True),
        ({"SSH_TTY": "/dev/pts/0", "DISPLAY": ":0"}, "linux", True),
        ({}, "linux", True),
        ({"DISPLAY": ":0"}, "linux", False),
        ({"WAYLAND_DISPLAY": "wayland-0"}, "linux", False),
        ({}, "darwin", False),
    ],
)
def test_the_device_code_is_chosen_with_no_browser_to_open(env, platform, device):
    assert guided.wants_device_code(env, platform) is device


def test_over_ssh_setup_connects_with_a_device_code(world):
    world.registered.append({"name": "api", "forge_url": "https://github.com/acme/api"})
    code, lines = _setup(
        world,
        shell=_healthy(),
        picker=ScriptedPicker(),
        env={"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"},
    )
    assert code == 0
    assert world.connects[0]["device"] is True
    assert (
        _doing(guided.PAPAYA, "Connecting with a device code: open the link on any device…")
        in lines
    )
    assert ADA in lines


def test_several_agents_are_chosen_in_a_list_then_connected(world):
    world.registered.append({"name": "api", "forge_url": "https://github.com/acme/api"})

    def connect(**kwargs):
        world.connects.append(kwargs)
        if not kwargs.get("agent"):
            return {
                "ok": False,
                "reason": "choose",
                "kind": "agent",
                "flag": "--agent",
                "choices": ["Ada", "Bea"],
            }
        _write_connection(world.client, name=kwargs["agent"], handle=kwargs["agent"].lower())
        return {"ok": True}

    picker = ScriptedPicker(one="Bea")
    code, lines = _setup(world, shell=_healthy(), picker=picker, connect=connect)

    assert code == 0
    assert [c["agent"] for c in world.connects] == [None, "Bea"]
    assert _ok(guided.PAPAYA, "Connected as Bea (@bea)") in lines


def test_a_script_with_several_agents_stops_naming_the_flag(world, capsys):
    def connect(**kwargs):
        return {
            "ok": False,
            "reason": "choose",
            "kind": "agent",
            "flag": "--agent",
            "choices": ["Ada", "Bea"],
        }

    options = guided.Options(interactive=False, repos=("acme/api",), skip_tools=True)
    code, _ = _setup(world, options, shell=_healthy(), connect=connect)
    assert code == 1
    assert "pass --agent <agent>" in capsys.readouterr().err


# ── step 5: repositories ────────────────────────────────────────────────────


def _github(shell: FakeShell, lists: dict[str, list[str]]) -> None:
    owners = list(lists)
    shell.answers[("gh", "api", "user")] = json.dumps({"login": owners[0]})
    shell.answers[("gh", "api", "user/orgs")] = json.dumps([{"login": o} for o in owners[1:]])
    for owner, names in lists.items():
        rows = [
            {
                "nameWithOwner": f"{owner}/{n}",
                "description": f"the {n}",
                "updatedAt": f"2026-0{i + 1}",
            }
            for i, n in enumerate(names)
        ]
        shell.answers[("gh", "repo", "list", owner)] = json.dumps(rows)


def test_picking_across_owners_keeps_every_selection(world):
    _write_connection(world.client)
    shell = _healthy()
    _github(shell, {"ada": ["notes", "site"], "acme": ["api", "web"]})
    picker = ScriptedPicker(
        owners=["ada", "acme", "ada"],
        repos={"ada": [{"ada/site"}, None], "acme": [{"acme/api", "acme/web"}]},
    )

    code, lines = _setup(world, shell=shell, picker=picker)

    assert code == 0
    # Back in the first owner's list, its tick is still there.
    assert picker.shown_ticks == [("ada", set()), ("acme", set()), ("ada", {"ada/site"})]
    assert sorted(world.added) == [
        "https://github.com/acme/api",
        "https://github.com/acme/web",
        "https://github.com/ada/site",
    ]
    assert "✓ Repositories  3 repositories (./bin/ppy setup --repos to change)" in lines


def test_a_typed_url_is_registered_too(world):
    _write_connection(world.client)
    shell = _healthy()
    _github(shell, {"ada": ["notes"]})
    picker = ScriptedPicker(owners=[guided.TYPE_URL], urls=["https://gitlab.com/ada/tool.git"])

    code, _ = _setup(world, shell=shell, picker=picker)

    assert code == 0
    assert world.added == ["https://gitlab.com/ada/tool.git"]


def test_repositories_are_listed_newest_first(world):
    shell = _healthy()
    shell.answers[("gh", "repo", "list", "ada")] = json.dumps(
        [
            {"nameWithOwner": "ada/old", "updatedAt": "2025-01-01T00:00:00Z"},
            {"nameWithOwner": "ada/new", "updatedAt": "2026-09-01T00:00:00Z"},
        ]
    )
    setup = guided.Setup(guided.Options(), shell=shell, picker=Silent(), out=io.StringIO())
    assert [r.slug for r in setup._owner_repos("ada")] == ["ada/new", "ada/old"]
    assert [
        "gh",
        "repo",
        "list",
        "ada",
        "--limit",
        "1000",
        "--json",
        "nameWithOwner,description,updatedAt",
    ] in shell.captured


def test_picking_nothing_stops_with_one_line(world, capsys):
    _write_connection(world.client)
    shell = _healthy()
    _github(shell, {"ada": ["notes"]})

    code, _ = _setup(world, shell=shell, picker=ScriptedPicker(owners=[]))

    assert code == 1
    assert "At least one repository is needed" in capsys.readouterr().err


def test_repos_reopens_the_picker_with_the_registered_ones_ticked(world):
    _set_up_fully(world)  # acme/api is registered
    shell = _healthy()
    _github(shell, {"ada": ["notes"], "acme": ["api", "web"]})
    picker = ScriptedPicker(owners=["acme"], repos={"acme": [{"acme/web"}]})
    options = guided.Options(pick_repos=True, skip_tools=True)

    code, lines = _setup(world, options, shell=shell, picker=picker)

    assert code == 0
    assert picker.shown_ticks == [("acme", {"acme/api"})]
    assert "Switch to another agent?" not in picker.asked
    assert world.added == ["https://github.com/acme/web"]
    assert " " * 16 + guided.KEPT_LINE in lines  # under the detail column
    assert "✓ Repositories  Registered web" in lines
    assert "✓ Repositories  2 repositories (./bin/ppy setup --repos to change)" in lines


def test_a_script_registers_its_repo_flags(world):
    _write_connection(world.client)
    options = guided.Options(interactive=False, repos=("acme/api", "https://github.com/acme/web"))
    options.skip_tools = True

    code, lines = _setup(world, options, shell=_healthy())

    assert code == 0
    assert world.added == ["https://github.com/acme/api", "https://github.com/acme/web"]
    assert lines[-3:] == FINAL


def test_the_plain_picker_filters_and_ticks_by_number():
    answers = iter(["we", "1"])
    out = io.StringIO()
    picker = guided.PlainPicker(ask=lambda _prompt: next(answers), out=out)
    rows = [guided.Repo("acme/api", "the api"), guided.Repo("acme/web", "the web")]

    picked = picker.repos("acme", rows, set())

    assert picked == {"acme/web"}
    assert "acme/api" not in out.getvalue()


def test_no_terminal_to_answer_on_stops_with_one_line(world, capsys):
    _write_connection(world.client)
    shell = _healthy()
    _github(shell, {"ada": ["notes"]})

    def eof(_prompt):
        raise EOFError

    code, _ = _setup(world, shell=shell, picker=guided.PlainPicker(ask=eof))

    assert code == 1
    assert capsys.readouterr().err.strip() == f"✗ {guided.NO_TERMINAL}"


# ── the command line ────────────────────────────────────────────────────────


def test_an_existing_non_interactive_profile_caller_still_only_writes_the_profile(
    world, monkeypatch, capsys
):
    def guided_ran(*args, **kwargs):
        raise AssertionError("the profile-only path ran the guided setup")

    monkeypatch.setattr(guided, "run", guided_ran)

    code = cli.main(["setup", "--non-interactive", "--manager-provider", "claude", "--skip-tools"])

    assert code == 0
    assert "configured manager claude/" in capsys.readouterr().out
    from papaya_agent_runtime.config import load_config

    assert load_config().manager.provider == "claude"


@pytest.mark.parametrize(
    ("argv", "is_guided", "interactive"),
    [
        (["setup"], True, True),
        (["setup", "--repos"], True, True),
        (["setup", "--non-interactive"], False, None),
        (["setup", "--non-interactive", "--manager-provider", "codex"], False, None),
        (["setup", "--non-interactive", "--repo", "acme/api"], True, False),
        (["setup", "--non-interactive", "--agent", "Ada"], True, False),
        (["setup", "--profile-only"], False, None),
    ],
)
def test_which_setup_a_command_line_runs(monkeypatch, argv, is_guided, interactive):
    seen = []
    monkeypatch.setattr(guided, "run", lambda options: seen.append(options) or 0)
    monkeypatch.setattr(wizard, "run_setup", lambda **kw: (_ for _ in ()).throw(SystemExit(9)))

    try:
        code = cli.main([*argv, "--skip-tools"])
    except SystemExit as exc:
        code = exc.code

    if is_guided:
        assert code == 0
        assert seen[0].interactive is interactive
    else:
        assert code == 9 and seen == []


# ── `ppy serve`'s working directory ─────────────────────────────────────────


def test_serve_uses_an_explicit_working_directory_first():
    options = serve.parse_args(["--working-directory", "/srv/runtime"])
    chosen = serve.working_directory_for(options, stored=lambda: "/stored", cwd=lambda: "/here")
    assert chosen == "/srv/runtime"


def test_serve_uses_the_connections_stored_directory_next(tmp_path, monkeypatch):
    home = tmp_path / "client"
    _write_connection(home, working_directory="/stored/checkout")
    monkeypatch.setenv(papaya.HOME_ENV, str(home))
    options = serve.parse_args([])

    assert papaya.stored_working_directory() == "/stored/checkout"
    assert serve.working_directory_for(options, cwd=lambda: "/here") == "/stored/checkout"


def test_serve_defaults_to_the_current_directory(tmp_path, monkeypatch):
    home = tmp_path / "client"
    _write_connection(home)
    monkeypatch.setenv(papaya.HOME_ENV, str(home))
    monkeypatch.chdir(tmp_path)

    assert papaya.stored_working_directory() is None
    assert serve.working_directory_for(serve.parse_args([])) == os.getcwd()


def test_a_supervised_serve_leaves_the_default_to_the_client():
    options = serve.parse_args(["--supervised"])
    assert serve.working_directory_for(options, stored=lambda: None, cwd=lambda: "/") is None


# ── the launcher ────────────────────────────────────────────────────────────


def test_the_launcher_prints_the_uv_install_line_when_uv_is_missing(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("dirname", "tr"):
        found = shutil.which(tool)
        assert found, tool
        (bin_dir / tool).symlink_to(found)
    sh = shutil.which("sh")
    assert sh

    proc = subprocess.run(
        [sh, str(ROOT / "bin" / "ppy"), "setup"],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": str(bin_dir), "HOME": str(tmp_path), "PPY_HOME": str(tmp_path / ".ppy")},
        check=False,
    )

    assert proc.returncode == 1
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in proc.stderr
