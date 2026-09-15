"""Hermetic tests for discovery parsing and the scriptable setup flow."""

from __future__ import annotations

import pytest

from papaya_agent_runtime import cli
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
    cfg.claude.allowed_tools = ["Read", "Bash(make:*)"]
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
