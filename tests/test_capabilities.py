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
import sys

import pytest

from papaya_agent_runtime import capabilities, cli


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


def test_modes_is_empty_until_there_is_something_to_serve(capsys) -> None:
    """A mode announced before `ppy serve` exists is a promise the exec cannot keep."""
    assert capabilities.MODES == ()
    assert cli.main(["capabilities", "--json"]) == 0
    assert _json_output(capsys)["modes"] == []


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


def test_the_release_comparison_ignores_pre_release_suffixes() -> None:
    """`0.15.0rc1` is still the 0.15.0 line; `main` is not a version at all."""
    assert capabilities._release("0.14.0") == (0, 14, 0)
    assert capabilities._release("0.15.0rc1") == (0, 15, 0)
    assert capabilities._release("1.2.3+local") == (1, 2, 3)
    assert capabilities._release("main") is None
