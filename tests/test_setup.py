"""Hermetic tests for discovery parsing and the scriptable setup flow."""

from __future__ import annotations

import json

import pytest

from papaya_agent_runtime import cli, papaya
from papaya_agent_runtime.config import (
    ConfigError,
    MMConfig,
    WorkerCeiling,
    load_config,
    save_config,
)
from papaya_agent_runtime.setup import discovery, wizard


@pytest.fixture
def ppy_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    return tmp_path / ".ppy"


@pytest.fixture
def connected_as(tmp_path, monkeypatch):
    """Fake this machine's Papaya connection, harness and all.

    `PPY_PAPAYA_HOME` is the whole world for connection discovery, so a test can
    say precisely which harness the person chose at connect time — the thing the
    provider defaults are supposed to follow.
    """

    def connect(harness: str) -> None:
        home = tmp_path / "papaya-client"
        home.mkdir(exist_ok=True)
        (home / "config.json").write_text(
            json.dumps(
                {
                    "agents": {
                        "a-1": {
                            "agent_id": "a-1",
                            "agent_name": "Ada",
                            "agent_handle": "ada",
                            "workspace_id": "w-1",
                            "connection_id": "c-1",
                        }
                    },
                    "connect": {"agent_id": "a-1", "harness": harness},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv(papaya.HOME_ENV, str(home))

    return connect


def _fake_report(usable: list[str]):
    def make(name: str) -> dict:
        return {
            "name": name,
            "kind": "harness",
            "path": f"/usr/bin/{name}",
            "version": "1.0.0",
            "authenticated": name in usable,
            "available": name in usable,
            "detail": "",
        }

    return {
        "harnesses": [make("claude"), make("codex")],
        "requirements": [],
        "companions": [],
    }


def test_usable_harnesses_filters_unauthenticated() -> None:
    report = _fake_report(["claude"])
    assert discovery.usable_harnesses(report) == ["claude"]


def test_setup_non_interactive_writes_config(ppy_home, monkeypatch) -> None:
    report = _fake_report(["claude", "codex"])
    monkeypatch.setattr(wizard, "discover", lambda: report)
    monkeypatch.setattr(wizard, "usable_harnesses", lambda r: discovery.usable_harnesses(r))
    cfg = wizard.run_setup(
        non_interactive=True,
        overrides={
            "manager_provider": "claude",
            "worker_provider": "codex",
            "worker_max_reasoning": "medium",
        },
    )
    assert cfg.manager.provider == "claude"
    assert cfg.worker.provider == "codex"
    # Persisted and reloadable.
    reloaded = load_config()
    assert reloaded.worker.provider == "codex"


# ── One harness unless somebody asked otherwise ─────────────────────────────
#
# The worker used to be `usable[-1]` and the manager `usable[0]`, so any machine
# with both harnesses signed in got a Claude manager driving Codex workers —
# chosen by list position rather than by anyone. Shane's rule, 2026-09-16: the
# runtime does not mix agents by default. The person already chose a harness when
# they connected this machine; that choice is the default for everyone it launches.


@pytest.fixture
def both_harnesses(monkeypatch):
    report = _fake_report(["claude", "codex"])
    monkeypatch.setattr(wizard, "discover", lambda: report)
    monkeypatch.setattr(wizard, "usable_harnesses", lambda r: discovery.usable_harnesses(r))
    return report


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_the_connections_harness_is_the_default_for_both_roles(
    ppy_home, both_harnesses, connected_as, harness
) -> None:
    connected_as(harness)
    cfg = wizard.run_setup(non_interactive=True)
    assert (cfg.manager.provider, cfg.worker.provider) == (harness, harness)


@pytest.mark.parametrize("manager", [None, "claude", "codex"])
def test_without_a_connection_workers_match_the_manager(ppy_home, both_harnesses, manager) -> None:
    """A fresh runtime never drives its own workers with the other harness."""
    overrides = {} if manager is None else {"manager_provider": manager}
    cfg = wizard.run_setup(non_interactive=True, overrides=overrides)
    assert cfg.worker.provider == cfg.manager.provider
    if manager is not None:
        assert cfg.manager.provider == manager


def test_one_usable_harness_still_drives_and_works(ppy_home, monkeypatch, connected_as) -> None:
    """Even a connection naming the *other* harness cannot conjure one that is not signed in."""
    report = _fake_report(["codex"])  # claude not authenticated
    monkeypatch.setattr(wizard, "discover", lambda: report)
    monkeypatch.setattr(wizard, "usable_harnesses", lambda r: discovery.usable_harnesses(r))
    connected_as("claude")
    cfg = wizard.run_setup(non_interactive=True)
    assert (cfg.manager.provider, cfg.worker.provider) == ("codex", "codex")


def test_an_explicit_worker_provider_still_wins_over_the_connection(
    ppy_home, both_harnesses, connected_as
) -> None:
    """Mixing stays possible — on purpose, never by list position."""
    connected_as("claude")
    cfg = wizard.run_setup(non_interactive=True, overrides={"worker_provider": "codex"})
    assert (cfg.manager.provider, cfg.worker.provider) == ("claude", "codex")


def test_interactive_setup_offers_the_connections_harness_for_both_roles(
    ppy_home, both_harnesses, connected_as, monkeypatch
) -> None:
    connected_as("codex")
    prompts: list[tuple[str, str]] = []

    def fake_input(prompt: str) -> str:
        label, _, rest = prompt.partition(" [")
        prompts.append((label, rest.split("(")[1].rstrip("): ")))
        return ""  # accept the offered default

    monkeypatch.setattr("builtins.input", fake_input)
    cfg = wizard.run_setup(non_interactive=False)

    offered = dict(prompts)
    assert offered["Manager provider"] == "codex"
    assert offered["Worker provider"] == "codex"
    assert (cfg.manager.provider, cfg.worker.provider) == ("codex", "codex")


def test_setup_rejects_unusable_worker(ppy_home, monkeypatch) -> None:
    report = _fake_report(["claude"])  # codex not authenticated
    monkeypatch.setattr(wizard, "discover", lambda: report)
    monkeypatch.setattr(wizard, "usable_harnesses", lambda r: discovery.usable_harnesses(r))
    with pytest.raises(ConfigError):
        wizard.build_config({"manager_provider": "claude", "worker_provider": "codex"})


def test_config_authority_toggles_merge(ppy_home, monkeypatch) -> None:
    report = _fake_report(["claude", "codex"])
    monkeypatch.setattr(wizard, "discover", lambda: report)
    monkeypatch.setattr(wizard, "usable_harnesses", lambda r: discovery.usable_harnesses(r))
    wizard.run_setup(non_interactive=True, overrides={"manager_provider": "claude"})
    assert load_config().authority.merge is False
    wizard.config_authority({"merge": True})
    assert load_config().authority.merge is True


def test_config_models_preserves_every_unrequested_setting(ppy_home, monkeypatch) -> None:
    report = _fake_report(["claude", "codex"])
    monkeypatch.setattr(wizard, "discover", lambda: report)
    monkeypatch.setattr(wizard, "usable_harnesses", lambda r: discovery.usable_harnesses(r))
    cfg = MMConfig(worker=WorkerCeiling("codex", "gpt-5.6-sol", "xhigh", "gpt-5.6-sol", "high", 3))
    cfg.authority.merge = True
    cfg.authority.open_pr = False
    cfg.tools.location = "custom/tools"
    cfg.health.quiet_minutes = 27
    cfg.assessments.completed_runs = 9
    cfg.claude.extra_tools = ["Bash(go:*)"]
    cfg.claude.dropped_tools = ["Bash(pnpm:*)"]
    save_config(cfg)
    expected = cfg.to_dict()
    expected["manager"]["model"] = "sonnet"

    wizard.config_models({"manager_model": "sonnet"})

    assert load_config().to_dict() == expected


def test_config_models_leaves_invalid_existing_config_bytes_untouched(ppy_home) -> None:
    path = ppy_home / "config.toml"
    path.parent.mkdir(parents=True)
    original = b"""cost_posture = "lean"
[manager]
provider = "codex"
model = "gpt-6-astra"
reasoning = "xhigh"
[worker]
provider = "codex"
max_model = "gpt-5.6-sol"
max_reasoning = "xhigh"
default_model = "gpt-5.6-sol"
default_reasoning = "high"
max_concurrent = 0
[authority]
merge = true
"""
    path.write_bytes(original)

    with pytest.raises(ConfigError, match="positive integer"):
        wizard.config_models({"manager_model": "gpt-6-astra"})

    assert path.read_bytes() == original


def test_config_models_cli_parses_worker_defaults_and_concurrency(monkeypatch, capsys) -> None:
    captured = {}
    cfg = MMConfig(worker=WorkerCeiling("codex", "gpt-5.6-sol", "xhigh", "gpt-5.6-sol", "high", 4))

    def update(overrides):
        captured.update(overrides)
        return cfg

    monkeypatch.setattr(wizard, "config_models", update)
    assert (
        cli.main(
            [
                "config",
                "models",
                "--worker-default-model",
                "gpt-5.6-sol",
                "--worker-default-reasoning",
                "high",
                "--worker-max-concurrent",
                "4",
            ]
        )
        == 0
    )
    assert captured == {
        "worker_default_model": "gpt-5.6-sol",
        "worker_default_reasoning": "high",
        "worker_max_concurrent": 4,
    }
    assert "concurrency 4" in capsys.readouterr().out
