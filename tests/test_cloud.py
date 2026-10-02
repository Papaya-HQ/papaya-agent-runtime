"""`ppy serve --cloud`'s start: the token out of the environment, the sign-in terminal (PAP-334)."""

from __future__ import annotations

import pytest

from papaya_agent_runtime import cloud


@pytest.fixture
def client_config(monkeypatch):
    """`prepare_environment` points the client's config module at a home; put it back after."""
    from papaya_agent_client import config as config_module

    monkeypatch.delenv("PAPAYA_AGENT_HOME", raising=False)
    for name in ("_CONFIG_DIR", "_CONFIG_FILE", "_TOKENS_FILE"):
        monkeypatch.setattr(config_module, name, getattr(config_module, name))
    return config_module


def test_the_token_leaves_the_environment_and_the_home_is_on_the_data_disk(
    tmp_path, client_config, monkeypatch
) -> None:
    env = {
        "PAPAYA_AGENT_TOKEN": " pagc_runner ",
        "PAPAYA_AGENT_HARNESS": "codex",
        cloud.DATA_DIR_ENV: str(tmp_path),
    }

    start = cloud.take_start(env)

    assert start.token == "pagc_runner"
    assert "PAPAYA_AGENT_TOKEN" not in env
    assert start.harness == "codex"
    assert str(client_config._CONFIG_FILE).startswith(str(tmp_path))
    assert "pagc_runner" not in repr(start)


def test_no_token_is_a_start_up_failure_naming_only_the_variable(tmp_path, client_config) -> None:
    from papaya_agent_client.cloud_host import CloudHostError

    with pytest.raises(CloudHostError) as raised:
        cloud.take_start({cloud.DATA_DIR_ENV: str(tmp_path)})

    assert "PAPAYA_AGENT_TOKEN" in str(raised.value)


def test_the_harness_defaults_to_claude_code(tmp_path, client_config) -> None:
    start = cloud.take_start({"PAPAYA_AGENT_TOKEN": "pagc_x", cloud.DATA_DIR_ENV: str(tmp_path)})

    assert start.harness == "claude-code"


def test_the_terminal_runs_this_checkouts_setup_then_a_shell(tmp_path) -> None:
    command = cloud.terminal_command(tmp_path / "with space")

    assert command[:3] == ["bash", "-l", "-c"]
    assert command[3] == f"'{tmp_path}/with space/bin/ppy' setup; exec bash -l"


def test_the_checkout_is_this_repository() -> None:
    assert (cloud.CHECKOUT / "bin" / "ppy").is_file()
