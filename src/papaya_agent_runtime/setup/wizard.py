"""`ppy setup` and `ppy config` flows.

Setup is scriptable: with ``--non-interactive`` and flags (or an
``overrides`` dict) it writes the config without prompting, which is how tests
and the setup skill drive it. Without those, it prompts, offering only detected,
authenticated harnesses.
"""

from __future__ import annotations

from papaya_agent_runtime.config import (
    REASONING_LEVELS,
    Authority,
    ConfigError,
    ManagerProfile,
    MMConfig,
    WorkerCeiling,
    load_config,
    save_config,
)
from papaya_agent_runtime.memory import ensure_memory_layout
from papaya_agent_runtime.paths import config_path, ensure_layout
from papaya_agent_runtime.setup.discovery import discover, usable_harnesses
from papaya_agent_runtime.state.db import init_db


def _choose(prompt: str, options: list[str], default: str) -> str:
    opts = "/".join(options)
    while True:
        raw = input(f"{prompt} [{opts}] ({default}): ").strip() or default
        if raw in options:
            return raw
        print(f"  please choose one of: {opts}")


def build_config(overrides: dict) -> MMConfig:
    """Construct and validate a config from an overrides dict (fail-closed)."""
    report = discover()
    usable = usable_harnesses(report)
    if not usable:
        raise ConfigError(
            "no authenticated Claude/Codex harness detected; run `ppy doctor` and "
            "sign in before setup"
        )

    manager_provider = overrides.get("manager_provider", usable[0])
    if manager_provider not in usable:
        raise ConfigError(f"manager provider {manager_provider!r} is not usable; usable: {usable}")
    worker_provider = overrides.get("worker_provider", usable[-1])
    if worker_provider not in usable:
        raise ConfigError(f"worker provider {worker_provider!r} is not usable; usable: {usable}")

    cfg = MMConfig(
        manager=ManagerProfile(
            provider=manager_provider,
            model=overrides.get("manager_model", _default_model(manager_provider)),
            reasoning=overrides.get("manager_reasoning", "high"),
        ),
        worker=WorkerCeiling(
            provider=worker_provider,
            max_model=overrides.get("worker_max_model", _default_model(worker_provider)),
            max_reasoning=overrides.get("worker_max_reasoning", "medium"),
            default_model=overrides.get(
                "worker_default_model",
                overrides.get("worker_max_model", _default_model(worker_provider)),
            ),
            default_reasoning=overrides.get(
                "worker_default_reasoning", overrides.get("worker_max_reasoning", "medium")
            ),
            max_concurrent=overrides.get("worker_max_concurrent", 2),
        ),
        cost_posture=overrides.get("cost_posture", "lean"),
        authority=Authority(
            merge=bool(overrides.get("allow_merge", False)),
        ),
    )
    cfg.validate()
    return cfg


def _default_model(provider: str) -> str:
    return {"claude": "opus", "codex": "gpt-5-codex"}.get(provider, "unknown")


def run_setup(non_interactive: bool = False, overrides: dict | None = None) -> MMConfig:
    ensure_layout()
    init_db()
    ensure_memory_layout()
    overrides = dict(overrides or {})

    if not non_interactive:
        report = discover()
        usable = usable_harnesses(report)
        if not usable:
            raise ConfigError("no authenticated Claude/Codex harness detected; run `ppy doctor`")
        print("Detected, authenticated harnesses:", ", ".join(usable))
        overrides.setdefault("manager_provider", _choose("Manager provider", usable, usable[0]))
        overrides.setdefault(
            "manager_reasoning",
            _choose("Manager reasoning", list(REASONING_LEVELS), "high"),
        )
        overrides.setdefault("worker_provider", _choose("Worker provider", usable, usable[-1]))
        overrides.setdefault(
            "worker_max_reasoning",
            _choose("Max worker reasoning", list(REASONING_LEVELS), "medium"),
        )

    cfg = build_config(overrides)
    save_config(cfg)
    return cfg


def config_models(overrides: dict) -> MMConfig:
    """`ppy config models` — rerun discovery and change either profile."""
    # Only an absent file means first-time construction. An existing but invalid
    # config may hold authority and custom policy that must not be replaced by a
    # newly built default config; surface the error and leave its bytes untouched.
    path = config_path()
    current = load_config(path) if path.exists() else None
    if current is None:
        cfg = build_config(overrides)
    else:
        report = discover()
        usable = usable_harnesses(report)
        manager_provider = overrides.get("manager_provider", current.manager.provider)
        worker_provider = overrides.get("worker_provider", current.worker.provider)
        for role, provider in (("manager", manager_provider), ("worker", worker_provider)):
            if provider not in usable:
                raise ConfigError(f"{role} provider {provider!r} is not usable; usable: {usable}")

        current.manager.provider = manager_provider
        current.worker.provider = worker_provider
        for key, target, attr in (
            ("manager_model", current.manager, "model"),
            ("manager_reasoning", current.manager, "reasoning"),
            ("worker_max_model", current.worker, "max_model"),
            ("worker_max_reasoning", current.worker, "max_reasoning"),
            ("worker_default_model", current.worker, "default_model"),
            ("worker_default_reasoning", current.worker, "default_reasoning"),
            ("worker_max_concurrent", current.worker, "max_concurrent"),
        ):
            if key in overrides:
                setattr(target, attr, overrides[key])
        if "cost_posture" in overrides:
            current.cost_posture = overrides["cost_posture"]
        current.validate()
        cfg = current
    save_config(cfg)
    return cfg


def config_authority(overrides: dict) -> MMConfig:
    """`ppy config authority` — adjust standing authority, e.g. merge policy."""
    cfg = load_config()
    auth = cfg.authority
    for key in (
        "clone_fetch",
        "create_worktrees",
        "commit",
        "push_branches",
        "open_pr",
        "merge",
    ):
        if key in overrides:
            setattr(auth, key, bool(overrides[key]))
    save_config(cfg)
    return cfg
