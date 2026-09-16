"""The runtime says what it is, so the client never has to guess.

The client used to learn what a runtime checkout supported by reading the
runtime's `cli.py` and looking for a subcommand name. When that subcommand was
removed the probe kept "working" and kept being wrong, silently. These tests hold
the replacement to the two properties that make it safe to read at connect time:
the object's shape is a contract, and the command cannot fail — an absent client
is reported, not raised.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import scale
from papaya_agent_runtime import capabilities, cli

ROOT = Path(__file__).resolve().parents[1]


class _BlockClient:
    """A meta-path finder that makes the client package unimportable."""

    def find_spec(self, name, path=None, target=None):
        if name == "papaya_agent_client" or name.startswith("papaya_agent_client."):
            raise ImportError(f"blocked for test: {name}")
        return None


@pytest.fixture
def client_unimportable(monkeypatch):
    """A checkout where `uv sync` has not run, or ran against a broken index."""
    for name in list(sys.modules):
        if name == "papaya_agent_client" or name.startswith("papaya_agent_client."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_BlockClient(), *sys.meta_path])


def _json_output(capsys) -> dict:
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 1, f"expected exactly one JSON object, got:\n{out}"
    return json.loads(out)


def test_capabilities_json_is_one_object_with_exactly_the_contracted_keys(capsys) -> None:
    """The shape is the contract. Anything extra or missing breaks a client's read."""
    assert cli.main(["capabilities", "--json"]) == 0
    data = _json_output(capsys)

    assert set(data) == {"runtime", "version", "client_version", "protocol", "modes"}
    assert data["runtime"] == "papaya-agent-runtime"
    assert isinstance(data["version"], str) and data["version"]
    assert data["client_version"] is None or isinstance(data["client_version"], str)
    assert isinstance(data["protocol"], int) and not isinstance(data["protocol"], bool)
    assert isinstance(data["modes"], list)
    assert all(isinstance(mode, str) for mode in data["modes"])


def test_a_checkout_without_the_client_still_answers_and_still_exits_zero(
    client_unimportable, capsys
) -> None:
    """Reporting the gap beats raising: "I cannot tell you" is the worse answer."""
    assert cli.main(["capabilities", "--json"]) == 0
    data = _json_output(capsys)

    assert data["client_version"] is None
    assert data["protocol"] == capabilities.DEFAULT_PROTOCOL
    assert data["runtime"] == "papaya-agent-runtime"


def test_the_client_version_is_the_one_this_runtime_actually_imports(capsys) -> None:
    assert cli.main(["capabilities", "--json"]) == 0
    data = _json_output(capsys)

    client = importlib.import_module("papaya_agent_client")
    assert client is not None
    assert data["client_version"] == importlib.metadata.version("papaya-agent-client")


def test_the_protocol_is_read_from_the_client_rather_than_restated_here(monkeypatch) -> None:
    """The client owns the protocol version; this command only reports it.

    Version 1 is both what the client declares today and the fallback, so asserting
    "it printed 1" proves nothing. Move the client's constant and watch the report
    move with it.
    """
    supervisor = importlib.import_module("papaya_agent_client.supervisor")
    assert capabilities.protocol() == supervisor.PROTOCOL_VERSION

    monkeypatch.setattr(supervisor, "PROTOCOL_VERSION", 7)
    assert capabilities.protocol() == 7


def test_an_unreadable_protocol_value_falls_back_to_version_one(monkeypatch) -> None:
    client = importlib.import_module("papaya_agent_client")
    monkeypatch.setattr(client, "PROTOCOL_VERSION", "not a number", raising=False)
    assert capabilities.protocol() == 1


def test_both_modes_are_announced_now_that_serve_can_keep_the_promise(capsys) -> None:
    """A mode is announced only once the command that serves it exists.

    This list is what a client execs on: reading `supervised` here and finding
    nothing that speaks the protocol is a connection that hangs rather than one
    that fails. `ppy serve` speaks both, so both are named — and the check that
    they are the same two `serve` implements is what keeps this honest.
    """
    from papaya_agent_runtime import serve

    assert capabilities.MODES == ("supervised", "terminal")
    assert cli.main(["capabilities", "--json"]) == 0
    assert _json_output(capsys)["modes"] == ["supervised", "terminal"]
    assert serve.parse_args(["--supervised"]).supervised is True
    assert serve.parse_args([]).supervised is False


def test_without_json_the_same_fields_print_one_per_line(capsys) -> None:
    assert cli.main(["capabilities"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()

    assert [line.split(":", 1)[0] for line in lines] == [
        "runtime",
        "version",
        "client_version",
        "protocol",
        "modes",
    ]


def test_it_reads_local_state_only_and_never_opens_the_database(ppy_home, capsys) -> None:
    """Connect time is before setup: a probe that needed state would answer nothing."""
    assert cli.main(["capabilities", "--json"]) == 0
    _json_output(capsys)

    assert not ppy_home.exists(), "capabilities created instance state just to answer"


def test_the_probe_answers_before_the_environment_has_been_built(tmp_path) -> None:
    """The one command that has to work on a checkout nobody has synced yet.

    `bin/ppy` runs everything through `uv run`, which *builds* the environment on
    first use — sixty-odd packages on a machine with no uv cache. The client gives
    this probe ten seconds when it connects a machine, so a fresh clone (exactly
    the machine somebody has just pointed the desktop app at) would time out on
    the only question asked before anything is installed.
    """
    if shutil.which("python3") is None:
        pytest.skip("no python3 on PATH to answer without the project environment")

    env = {
        **os.environ,
        # An environment that does not exist and is not this checkout's, so the
        # launcher is in exactly the state a fresh clone is in.
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / "never-built"),
        "PPY_HOME": str(tmp_path / ".ppy"),
    }
    started = time.monotonic()
    proc = subprocess.run(
        [str(ROOT / "bin" / "ppy"), "capabilities", "--json"],
        capture_output=True,
        text=True,
        timeout=scale(60),
        env=env,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert set(data) == {"runtime", "version", "client_version", "protocol", "modes"}
    assert data["runtime"] == "papaya-agent-runtime"
    assert data["modes"] == list(capabilities.MODES)
    assert elapsed < scale(2.0), f"the cold probe took {elapsed:.1f}s"
    assert not (tmp_path / ".ppy").exists(), "the cold probe created instance state"
    assert not (tmp_path / "never-built").exists(), "the cold probe built an environment"


def test_a_built_checkout_still_answers_from_its_own_environment(tmp_path) -> None:
    """The cold path must not hijack a checkout that has an environment.

    `client_version` is the whole reason: answered from the standard library it
    is `null`, which is the truth before anything is installed and a lie
    afterwards. So the shortcut is conditional on the environment being absent,
    and this is the other half of that condition.
    """
    if not (ROOT / ".venv").is_dir():
        pytest.skip("this checkout has no built environment to answer from")

    env = {**os.environ, "PPY_HOME": str(tmp_path / ".ppy")}
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    proc = subprocess.run(
        [str(ROOT / "bin" / "ppy"), "capabilities", "--json"],
        capture_output=True,
        text=True,
        timeout=scale(120),
        env=env,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["client_version"] == importlib.metadata.version(
        "papaya-agent-client"
    )


def test_the_release_comparison_ignores_pre_release_suffixes() -> None:
    """`0.15.0rc1` is still the 0.15.0 line; `main` is not a version at all."""
    assert capabilities._release("0.14.0") == (0, 14, 0)
    assert capabilities._release("0.15.0rc1") == (0, 15, 0)
    assert capabilities._release("1.2.3+local") == (1, 2, 3)
    assert capabilities._release("main") is None
