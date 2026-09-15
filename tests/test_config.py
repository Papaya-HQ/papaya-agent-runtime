"""Hermetic tests for config load/validate/save."""

from __future__ import annotations

import pytest

from papaya_agent_runtime.config import (
    Authority,
    ConfigError,
    ManagerProfile,
    MMConfig,
    WorkerCeiling,
    load_config,
    save_config,
)


def test_round_trip(tmp_path) -> None:
    path = tmp_path / "config.toml"
    cfg = MMConfig(
        manager=ManagerProfile("claude", "opus", "high"),
        worker=WorkerCeiling("codex", "gpt-5-codex", "medium"),
        cost_posture="lean",
        authority=Authority(merge=False),
    )
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.manager.provider == "claude"
    assert loaded.worker.max_model == "gpt-5-codex"
    assert loaded.worker.default_model == "gpt-5-codex"
    assert loaded.worker.default_reasoning == "medium"
    assert loaded.worker.max_concurrent == 2
    assert loaded.authority.merge is False
    assert loaded.cost_posture == "lean"
    assert loaded.assessments.completed_runs == 5
    assert loaded.health.max_stale_stacks == 4
    assert loaded.usage.input_ceiling_per_task == 12_000_000
    assert loaded.usage.input_ceiling_per_review == 3_000_000


def test_older_config_gets_default_assessment_policy(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
cost_posture = "lean"
[manager]
provider = "claude"
model = "opus"
reasoning = "high"
[worker]
provider = "codex"
max_model = "gpt-5-codex"
max_reasoning = "medium"
"""
    )
    loaded = load_config(path)
    assert loaded.assessments.enabled is True
    assert loaded.assessments.max_days == 14
    assert loaded.worker.default_model == "gpt-5-codex"
    assert loaded.worker.default_reasoning == "medium"
    assert loaded.worker.max_concurrent == 2


def test_missing_config_raises(tmp_path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml")


def test_invalid_reasoning_rejected() -> None:
    cfg = MMConfig(manager=ManagerProfile("claude", "opus", "ludicrous"))
    with pytest.raises(ConfigError):
        cfg.validate()


def test_invalid_provider_rejected() -> None:
    cfg = MMConfig(worker=WorkerCeiling("gemini", "x", "low"))
    with pytest.raises(ConfigError):
        cfg.validate()


def test_merge_defaults_off() -> None:
    assert Authority().merge is False


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_worker_concurrency_must_be_a_positive_integer(value) -> None:
    cfg = MMConfig(worker=WorkerCeiling(max_concurrent=value))
    with pytest.raises(ConfigError, match="positive integer"):
        cfg.validate()


def test_worker_default_must_fit_model_and_reasoning_ceiling() -> None:
    cfg = MMConfig(worker=WorkerCeiling("codex", "gpt-5.6-sol", "high", "gpt-6-astra", "xhigh"))
    with pytest.raises(ConfigError, match="worker default exceeds"):
        cfg.validate()
